"""Compiler — turns a raw source into a structured wiki article via Claude.

Loads identity from `vault/_self/*.md` as system context. Prompts Claude for a JSON
response matching the article schema; writes into SQLite + mirrors to vault/.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from slugify import slugify

from mastisk import wiki_suggestions
from mastisk.agents.base import Agent
from mastisk.agents.registry import resolve_prompt
from mastisk.bridges import claude_bridge, imagegen_bridge, intelligence
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.memory_conventions import DATED_FACTS_PROMPT
from mastisk.paths import vault_dir
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.compiler")

# Extracts (slug, label) pairs from body HTML so we can know what Claude
# referenced and stub in any targets that don't have their own article yet.
LINK_RE = re.compile(r'<span class="link"\s+data-target="([^"]+)"[^>]*>([^<]*)</span>')

SCHEMA_MD = """
Return a single JSON object in a ```json``` fenced block, matching this shape:

```json
{
  "skip": false,
  "skip_reason": "",
  "id": "lowercase-slug",
  "kind": "Concept | Entity | Source | Synthesis",
  "title": "Human-readable title",
  "aka": ["alternate name 1", "alternate name 2"],
  "summary": "1–2 sentence italic summary. Leads the article.",
  "confidence": 0.0,
  "reading_minutes": 5,
  "sections": [
    {"h": "TL;DR", "kind": "callout", "body": "HTML-safe paragraph."},
    {"h": "Mechanism", "body": "HTML-safe paragraphs. Use <span class=\\"link\\" data-target=\\"slug\\">wiki link</span> for cross-references."},
    {"h": "How it works", "kind": "diagram", "body": "flowchart TD\\n  A[Input] --> B{Decision}\\n  B -->|yes| C[Outcome]"},
    {"h": "Open questions", "kind": "open", "body": "HTML-safe paragraph."}
  ],
  "related": [
    {"id": "other-slug", "label": "Other concept", "weight": 0.8}
  ]
}
```

Rules:
- If the source isn't relevant to the user's interests (see their profile above), set "skip": true and "skip_reason": "...".
- "title" must be a single clause, max __TITLE_MAX_CHARS__ characters. No subtitles or subtitle sentences; no em-dash appendages.
- "id" and slugs must be kebab-case, ASCII, no spaces.
- Weight in [0, 1]. Confidence in [0, 1] — your subjective calibration of how solid this page is.
- Never invent sources you didn't see. Never hallucinate URLs.
- Write body text in HTML. Use <em> for emphasis, <span class="link" data-target="slug"> for cross-references.
- Match the user's writing style from their profile.

Depth — this is a reference wiki, not a feed of summaries:
- Aim for 700–1400 words across 4–8 sections for a substantive source; fewer only when the source is genuinely thin.
- A section body may contain MULTIPLE <p> paragraphs. Develop the ideas: the mechanism behind the claim, concrete numbers and examples from the source, what the author gets right or misses, and how it connects to concepts already in the wiki.
- Prefer analytical headings ("Why the bottleneck is memory bandwidth") over generic ones ("Details", "More").
- Include one section that situates the topic in the wider wiki: how it relates to, extends, or contradicts existing articles (use wiki links).

Diagrams — visualize structure whenever the source has it:
- Sections with "kind": "diagram" have a body that is PURE Mermaid source (no ``` fences, no HTML). Escape newlines as \\n inside the JSON string.
- Use one when the content describes a process, architecture, pipeline, hierarchy, timeline, or comparison: `flowchart TD/LR` for processes and architectures, `sequenceDiagram` for interactions, `timeline` for chronology, `mindmap` for concept maps.
- Keep node labels under ~6 words; quote labels containing special characters, e.g. A["label (detail)"]. 5–15 nodes is the sweet spot.
- Most substantive articles deserve exactly one diagram; use two only if the source covers two genuinely distinct structures. Skip diagrams for thin or purely narrative sources.

- "open" sections are ANALYTICAL threads the article leaves unresolved — genuine conceptual loose ends a reader would still be thinking about. NEVER use "open" to flag missing metadata ("what's the publish date?", "who is the author?", "what's his role?", "when was this written?"). If you lacked a fact, leave the field empty; do not convert metadata gaps into open questions. If the source has no genuine analytical loose ends, omit the "Open questions" section entirely.

Key facts — durable memory the wiki maintains over time:
- When the source contains durable, specific facts (numbers, dates, pricing, launches, org changes, commitments), include one section {"h": "Key facts", "body": "<ul><li>(2026-07-03) fact</li>…</ul>"} with 3–8 bullets.
- When re-compiling a page that already has a "Key facts" section (see existing articles), carry its bullets forward and supersede in place rather than restating from scratch.
- Omit the section entirely when the source has no durable facts — never pad.
__DATED_FACTS_PROMPT__
""".replace("__TITLE_MAX_CHARS__", str(q.MAX_GENERATED_ARTICLE_TITLE_CHARS)).replace(
    "__DATED_FACTS_PROMPT__", DATED_FACTS_PROMPT
)


# Machine schema for the anthropic tier's tool-forced JSON (json_object=True).
# Mirrors SCHEMA_MD; keeps long HTML-in-JSON bodies syntactically valid where
# free-text JSON routinely arrives with unescaped inner quotes.
ARTICLE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean"},
        "skip_reason": {"type": "string"},
        "id": {"type": "string"},
        "kind": {"type": "string", "enum": ["Concept", "Entity", "Source", "Synthesis"]},
        "title": {"type": "string"},
        "aka": {"type": "array", "items": {"type": "string"}},
        "summary": {"type": "string"},
        "confidence": {"type": "number"},
        "reading_minutes": {"type": "integer"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "h": {"type": "string"},
                    "kind": {"type": "string", "enum": ["section", "callout", "open", "diagram"]},
                    "body": {"type": "string"},
                },
                "required": ["h", "body"],
            },
        },
        "related": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "label": {"type": "string"},
                    "weight": {"type": "number"},
                },
                "required": ["id"],
            },
        },
    },
    "required": ["skip", "id", "kind", "title", "summary", "sections"],
}


class Compiler(Agent):
    name = "compiler"
    tick_seconds = 300  # 5 min

    # Drain up to this many jobs per tick. Keeps a single tick bounded so a
    # giant backlog can't monopolise the scheduler; APScheduler's max_instances=1
    # prevents overlap with the next tick anyway.
    max_jobs_per_tick = 20

    async def run_once(self) -> None:
        if self.disabled_tick():
            return
        for _ in range(self.max_jobs_per_tick):
            job = self._pick_job()
            if not job:
                return
            log.info("%s: picking job %s (%s)", self.name, job["id"], job["kind"])
            self._mark_running(job["id"])
            try:
                await self._handle(job)
                self._mark_done(job["id"])
            except Exception as e:
                log.exception("%s: job %s failed", self.name, job["id"])
                self._mark_failed(job["id"], str(e))

    async def _handle(self, job: dict) -> None:
        if job["kind"] == "enrich_stub":
            await self._handle_enrich_stub(job)
            return

        payload = json.loads(job["payload_json"] or "{}")
        source_id = payload.get("source_id")
        if not source_id:
            log.warning("compiler: no source_id in job %s", job["id"])
            return

        with connect() as conn:
            src = conn.execute(
                "SELECT id, kind, url, title, raw_path, hero_image_url FROM sources WHERE id=?",
                (source_id,),
            ).fetchone()
        if not src:
            log.warning("compiler: source %s not found", source_id)
            return

        raw_text = Path(src["raw_path"]).read_text() if src["raw_path"] else (src["title"] or "")
        identity = self.load_identity()
        registry = self._known_articles_block()
        schema = resolve_prompt("compiler", "schema", SCHEMA_MD)
        max_chars = get_settings().compiler.max_source_chars

        prompt = (
            f"You are Mastisk's Compiler. Transform the raw source below into a wiki article.\n\n"
            f"{identity}\n\n"
            f"{registry}\n\n"
            f"# Raw source\nTitle: {src['title']}\nURL: {src['url']}\nKind: {src['kind']}\n\n"
            f"{raw_text[:max_chars]}\n\n"
            f"{schema}"
        )

        data, provider = await self._run_for_json(prompt, label=f"source {source_id}")
        if not data:
            return

        if data.get("skip"):
            self.emit_feed(verb="skipped", obj=src["title"][:80], kind="compile",
                           payload={"source_id": source_id, "reason": data.get("skip_reason")})
            return

        missing = [k for k in ("id", "kind", "title", "sections") if not data.get(k)]
        if missing:
            log.warning(
                "compiler: %s response for source %s missing required keys %s",
                provider, source_id, missing,
            )
            return
        _normalize_article_data(data)
        if not data["sections"]:
            log.warning(
                "compiler: %s response for source %s had no usable sections",
                provider, source_id,
            )
            return

        # Guard against mis-classification: a single-source article is almost
        # never a genuine Synthesis — that kind is reserved for cross-source
        # weaving. Demote to Concept so the UI counts stay meaningful.
        if data.get("kind") == "Synthesis":
            data["kind"] = "Concept"

        # Pass the source's hero through so upsert_article can COALESCE it
        # into articles.hero_image_url. A recompile without a hero leaves the
        # previous one intact (see queries.upsert_article).
        data["hero_image_url"] = src["hero_image_url"]
        if not data["hero_image_url"]:
            data["hero_image_url"] = await self._maybe_generate_hero(data)
        self._persist_article(data, source_id=source_id)
        self.emit_feed(
            verb="wrote" if self._is_new(data["id"]) else "updated",
            obj=data["title"][:80],
            kind=data["kind"].lower(),
            touched=1,
            payload={"article_id": data["id"], "source_id": source_id, "provider": provider},
        )

    async def _handle_enrich_stub(self, job: dict) -> None:
        """Turn an escalator stub into a fully-rendered wiki article.

        The escalator creates a placeholder row (updated_by='escalator (stub)',
        confidence=0, no sections/related/links resolved) when it promotes a
        note. This handler runs the standard Compiler pipeline against the
        note body and forces the article id back to the stub's existing id
        so the upsert overwrites the placeholder in place — preserving the
        source_note_id back-reference and the URL the user already has.
        """
        payload = json.loads(job["payload_json"] or "{}")
        article_id = payload.get("article_id")
        note_id = payload.get("note_id")
        if not article_id or not note_id:
            log.warning(
                "compiler: enrich_stub missing article_id/note_id in job %s", job["id"],
            )
            return

        with connect() as conn:
            stub = conn.execute(
                "SELECT id, title, kind FROM articles WHERE id=?", (article_id,),
            ).fetchone()
            note = conn.execute(
                "SELECT id, body FROM notes WHERE id=?", (note_id,),
            ).fetchone()
        if not stub:
            log.warning("compiler: enrich_stub article %s not found", article_id)
            return
        if not note:
            log.warning("compiler: enrich_stub note %s not found", note_id)
            return

        identity = self.load_identity()
        registry = self._known_articles_block()
        note_body = (note["body"] or "")[: get_settings().compiler.max_source_chars]
        schema = resolve_prompt("compiler", "schema", SCHEMA_MD)

        prompt = (
            f"You are Mastisk's Compiler. Enrich the escalated note below into a full wiki article.\n\n"
            f"{identity}\n\n"
            f"{registry}\n\n"
            f"# The escalated note\n"
            f"Working title: {stub['title']}\nProvisional kind: {stub['kind']}\n\n"
            f"{note_body}\n\n"
            f"Use the existing article id `{article_id}` exactly — do not coin a new slug, "
            f"this enrichment overwrites the stub in place.\n\n"
            f"{schema}"
        )

        data, provider = await self._run_for_json(
            prompt, label=f"enrich_stub article {article_id}",
        )
        if not data:
            return

        _normalize_article_data(data)
        if not data.get("title") or not data.get("sections"):
            log.warning(
                "compiler: enrich_stub %s response for article %s lacks title/sections",
                provider, article_id,
            )
            return

        # Force the id so the upsert lands on the existing stub. Skip flag is
        # ignored — the user already escalated this note; refusing to enrich
        # would leave the stub forever.
        data["id"] = article_id
        if data.get("kind") == "Synthesis":
            data["kind"] = "Concept"

        self._persist_article(data, source_id=None)
        self.emit_feed(
            verb="enriched",
            obj=data.get("title", article_id)[:80],
            kind=data.get("kind", "concept").lower(),
            touched=1,
            payload={"article_id": article_id, "note_id": note_id, "provider": provider},
        )

    async def _run_for_json(self, prompt: str, *, label: str) -> tuple[dict | None, str]:
        """Run the intelligence chain and parse a JSON article out of it.

        The anthropic tier (when configured) tool-forces valid JSON; the CLI
        tiers return free text where long HTML-in-JSON bodies occasionally
        arrive malformed — one retry recovers most of those instead of
        dropping the source.
        """
        provider = "?"
        for attempt in (1, 2):
            resp, provider = await intelligence.run_intelligence(
                prompt, json_object=True, json_schema=ARTICLE_JSON_SCHEMA,
            )
            data = claude_bridge.extract_json_block(resp.get("text") or "")
            if data:
                return data, provider
            log.warning(
                "compiler: no JSON block in %s response for %s (attempt %d)",
                provider, label, attempt,
            )
        return None, provider

    async def _maybe_generate_hero(self, data: dict) -> str | None:
        """Generate a hero image via yoyo when the source shipped none.

        Best-effort and budget-guarded: "auto" mode requires the yoyo CLI on
        PATH, a daily cap bounds spend, and any failure just leaves the
        article hero-less (never fails the compile job).
        """
        cfg = get_settings().compiler
        if cfg.hero_images != "auto" or not imagegen_bridge.available():
            return None
        with connect() as conn:
            # Recompiles must not regenerate: the article may already carry a
            # previously generated hero even when the *source* has none.
            existing = conn.execute(
                "SELECT hero_image_url FROM articles WHERE id=?",
                (data.get("id"),),
            ).fetchone()
            if existing and existing["hero_image_url"]:
                return None
            generated_today = conn.execute(
                "SELECT COUNT(*) FROM feed WHERE agent='compiler' AND verb='illustrated' "
                "AND ts >= datetime('now', 'start of day')",
            ).fetchone()[0]
        if generated_today >= cfg.hero_images_daily_cap:
            return None

        title = data.get("title") or data.get("id") or "concept"
        summary = data.get("summary") or ""
        prompt = (
            f"Minimal editorial illustration for a knowledge-wiki article titled "
            f"\"{title}\". {summary} Abstract, conceptual, muted palette, flat "
            f"shapes, no text or lettering in the image, 16:9 composition."
        )
        try:
            url = await imagegen_bridge.generate_hero(
                prompt, timeout_s=cfg.hero_image_timeout_s,
            )
        except Exception as e:
            log.warning("compiler: hero generation failed for %s: %s", data.get("id"), e)
            return None
        self.emit_feed(
            verb="illustrated", obj=title[:80], kind="image",
            payload={"article_id": data.get("id"), "hero_image_url": url},
        )
        return url

    def _is_new(self, article_id: str) -> bool:
        with connect() as conn:
            return conn.execute("SELECT 1 FROM articles WHERE id=?", (article_id,)).fetchone() is None

    def _known_articles_block(self) -> str:
        """Top-80-by-recency list of existing article ids + titles + kinds.

        Injected into the Compiler prompt so Claude reuses canonical ids when
        the raw source mentions a concept we already track. Prevents the
        graph-disconnection issue where Claude coins a fresh slug for every
        reference. If the vault has fewer than 80 articles, we include them all.
        """
        with connect() as conn:
            rows = conn.execute(
                "SELECT id, title, kind FROM articles ORDER BY updated_at DESC LIMIT 80"
            ).fetchall()
        if not rows:
            return (
                "# Existing articles you can reference\n"
                "(none yet — coin kebab-case ids; stub pages will be auto-created)"
            )
        lines = [f"- `{r['id']}` — {r['title']} ({r['kind']})" for r in rows]
        body = "\n".join(lines)
        return (
            "# Existing articles you can reference\n"
            'When writing `<span class="link" data-target="X">` use the exact id '
            "from this list whenever the concept matches. If the concept is new, "
            "coin a kebab-case id — it'll auto-become a stub article.\n\n"
            f"{body}"
        )

    def _persist_article(
        self, data: dict, *, source_id: str | None, updated_by: str = "Compiler",
    ) -> None:
        article_id = data["id"]
        data["title"] = q.clamp_generated_title(data.get("title") or article_id)
        slug = slugify(data["title"])[:80] or article_id
        vault_path = self._vault_path_for(data["kind"], slug)

        # Harvest (target, label) pairs BEFORE persist so we can stub missing
        # targets inside the same transaction — that way set_related's existence
        # check sees freshly-created stubs.
        link_refs = _extract_link_refs(data.get("sections", []))

        with connect() as conn, q.txn(conn):
            q.upsert_article(conn, {
                "id": article_id,
                "kind": data["kind"],
                "title": data["title"],
                "slug": slug,
                "aka": data.get("aka", []),
                "summary": data.get("summary", ""),
                "body_md": _sections_to_md(data.get("sections", [])),
                "confidence": float(data.get("confidence", 0.6)),
                "reading_minutes": int(data.get("reading_minutes", 5)),
                "updated_by": updated_by,
                "vault_path": str(vault_path),
                "hero_image_url": data.get("hero_image_url"),
            })
            q.replace_sections(conn, article_id, data.get("sections", []))
            # Body-referenced targets without an article no longer stub
            # unconditionally — the gate accumulates them in wiki_suggestions
            # and mints only once enough distinct articles reference the same
            # target (or the user promotes from the queue). Runs before
            # set_related so related-link reconciliation sees fresh mints.
            minted = q.gate_stub_targets(
                conn,
                from_article=article_id,
                refs=link_refs,
                min_sources=get_settings().compiler.stub_gate_min_sources,
            )
            q.set_related(conn, article_id, data.get("related", []))
            if source_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO article_sources (article_id, source_id) VALUES (?, ?)",
                    (article_id, source_id),
                )

        # Mirror to vault
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        vault_path.write_text(_render_markdown(data))

        for slug, title in minted.items():
            self.emit_feed(
                verb="minted", obj=title[:80], kind="entity",
                payload={"article_id": slug, "via": "stub gate"},
            )
        # Refresh the phone-readable shortlist; content-diffed, so this is a
        # no-op write on compiles that didn't touch the queue.
        wiki_suggestions.render_vault_file()

    def _vault_path_for(self, kind: str, slug: str) -> Path:
        folder = {
            "Concept": "concepts",
            "Entity": "entities",
            "Source": "sources",
            "Synthesis": "synthesis",
        }.get(kind, "concepts")
        return vault_dir() / folder / f"{slug}.md"


def _normalize_article_data(data: dict) -> None:
    """Coerce LLM output lists to the shapes downstream code indexes into.

    Tool-forcing guarantees syntactically valid JSON, not schema conformance —
    observed in prod: a bare string inside "sections" crashed
    ``_extract_link_refs`` with ``'str' object has no attribute 'get'``.
    Non-dict entries are dropped (logged), never fatal.
    """
    sections = data.get("sections") or []
    clean_sections = []
    for s in sections if isinstance(sections, list) else []:
        if isinstance(s, dict) and isinstance(s.get("body"), str):
            s["h"] = str(s.get("h") or "")
            clean_sections.append(s)
        else:
            log.warning("compiler: dropping malformed section entry: %r", str(s)[:120])
    data["sections"] = clean_sections

    related = data.get("related") or []
    data["related"] = [
        r for r in (related if isinstance(related, list) else [])
        if isinstance(r, dict) and r.get("id")
    ]

    aka = data.get("aka") or []
    data["aka"] = [a for a in (aka if isinstance(aka, list) else []) if isinstance(a, str)]


def _extract_link_refs(sections: list[dict]) -> dict[str, str]:
    """Walk section bodies and return {target_slug: best_display_label}.

    If the same target appears with multiple labels across sections, we keep
    the most common one (ties broken by shortest label, then alphabetical) so
    the stub title reads naturally. This mirrors the tie-break used by the
    backfill in ``repair-graph``.
    """
    from collections import Counter
    per_slug: dict[str, Counter] = {}
    for s in sections:
        body = s.get("body", "") or ""
        for m in LINK_RE.finditer(body):
            slug = m.group(1).strip()
            label = (m.group(2) or "").strip()
            if not slug:
                continue
            per_slug.setdefault(slug, Counter())[label or slug] += 1
    out: dict[str, str] = {}
    for slug, counter in per_slug.items():
        best_count = max(counter.values())
        tied = [lbl for lbl, n in counter.items() if n == best_count]
        tied.sort(key=lambda s: (len(s), s))
        out[slug] = tied[0]
    return out


def _sections_to_md(sections: list[dict]) -> str:
    out: list[str] = []
    for s in sections:
        out.append(f"## {s.get('h', '')}\n")
        if s.get("kind") == "diagram":
            # Diagram bodies are raw Mermaid source, not HTML — keep them
            # intact in a fenced block (Obsidian and most markdown renderers
            # pick this up natively).
            out.append(f"```mermaid\n{s.get('body', '').strip()}\n```")
        else:
            out.append(_strip_html(s.get("body", "")))
        out.append("")
    return "\n".join(out)


def _strip_html(html: str) -> str:
    # Preserve link targets as markdown-like refs
    s = re.sub(r'<span class="link" data-target="([^"]+)">([^<]+)</span>', r"[[\2|\1]]", html)
    s = re.sub(r"<em>([^<]+)</em>", r"*\1*", s)
    # Keep block boundaries: multi-paragraph bodies would otherwise collapse
    # into a single run of words ("...end.Next paragraph...").
    s = re.sub(r"</p>\s*<p[^>]*>", "\n\n", s)
    s = re.sub(r"<br\s*/?>", "\n", s)
    s = re.sub(r"<[^>]+>", "", s)
    return s.strip()


def _render_markdown(data: dict) -> str:
    lines = [
        "---",
        f"id: {data['id']}",
        f"kind: {data['kind']}",
        f"title: {data['title']}",
        f"confidence: {data.get('confidence', 0.6)}",
        f"reading_minutes: {data.get('reading_minutes', 5)}",
        "---",
        "",
        f"# {data['title']}",
        "",
        f"*{data.get('summary', '')}*",
        "",
        _sections_to_md(data.get("sections", [])),
    ]
    return "\n".join(lines)
