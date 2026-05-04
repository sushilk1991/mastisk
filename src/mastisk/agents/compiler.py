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

from mastisk.agents.base import Agent
from mastisk.bridges import claude_bridge, intelligence
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import vault_dir

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
    {"h": "Mechanism", "body": "HTML-safe paragraph. Use <span class=\\"link\\" data-target=\\"slug\\">wiki link</span> for cross-references."},
    {"h": "Open questions", "kind": "open", "body": "HTML-safe paragraph."}
  ],
  "related": [
    {"id": "other-slug", "label": "Other concept", "weight": 0.8}
  ]
}
```

Rules:
- If the source isn't relevant to the user's interests (see their profile above), set "skip": true and "skip_reason": "...".
- "id" and slugs must be kebab-case, ASCII, no spaces.
- Weight in [0, 1]. Confidence in [0, 1] — your subjective calibration of how solid this page is.
- Never invent sources you didn't see. Never hallucinate URLs.
- Write body text in HTML. Use <em> for emphasis, <span class="link" data-target="slug"> for cross-references.
- Match the user's writing style from their profile.
- "open" sections are ANALYTICAL threads the article leaves unresolved — genuine conceptual loose ends a reader would still be thinking about. NEVER use "open" to flag missing metadata ("what's the publish date?", "who is the author?", "what's his role?", "when was this written?"). If you lacked a fact, leave the field empty; do not convert metadata gaps into open questions. If the source has no genuine analytical loose ends, omit the "Open questions" section entirely.
"""


class Compiler(Agent):
    name = "compiler"
    tick_seconds = 300  # 5 min

    # Drain up to this many jobs per tick. Keeps a single tick bounded so a
    # giant backlog can't monopolise the scheduler; APScheduler's max_instances=1
    # prevents overlap with the next tick anyway.
    max_jobs_per_tick = 20

    async def run_once(self) -> None:
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

        prompt = (
            f"You are Mastisk's Compiler. Transform the raw source below into a wiki article.\n\n"
            f"{identity}\n\n"
            f"{registry}\n\n"
            f"# Raw source\nTitle: {src['title']}\nURL: {src['url']}\nKind: {src['kind']}\n\n"
            f"{raw_text[:8000]}\n\n"
            f"{SCHEMA_MD}"
        )

        resp, provider = await intelligence.run_intelligence(prompt)
        # extract_json_block tolerates both fenced and naked-braces JSON,
        # so this works whether Claude / Codex / Ollama served.
        data = claude_bridge.extract_json_block(resp.get("text") or "")
        if not data:
            log.warning(
                "compiler: no JSON block in %s response for source %s",
                provider, source_id,
            )
            return

        if data.get("skip"):
            self.emit_feed(verb="skipped", obj=src["title"][:80], kind="compile",
                           payload={"source_id": source_id, "reason": data.get("skip_reason")})
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
        self._persist_article(data, source_id=source_id)
        self.emit_feed(
            verb="wrote" if self._is_new(data["id"]) else "updated",
            obj=data["title"][:80],
            kind=data["kind"].lower(),
            touched=1,
            payload={"article_id": data["id"], "source_id": source_id},
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
        note_body = (note["body"] or "")[:8000]

        prompt = (
            f"You are Mastisk's Compiler. Enrich the escalated note below into a full wiki article.\n\n"
            f"{identity}\n\n"
            f"{registry}\n\n"
            f"# The escalated note\n"
            f"Working title: {stub['title']}\nProvisional kind: {stub['kind']}\n\n"
            f"{note_body}\n\n"
            f"Use the existing article id `{article_id}` exactly — do not coin a new slug, "
            f"this enrichment overwrites the stub in place.\n\n"
            f"{SCHEMA_MD}"
        )

        resp, provider = await intelligence.run_intelligence(prompt)
        data = claude_bridge.extract_json_block(resp.get("text") or "")
        if not data:
            log.warning(
                "compiler: enrich_stub no JSON block in %s response for article %s",
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

    def _persist_article(self, data: dict, *, source_id: str | None) -> None:
        article_id = data["id"]
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
                "updated_by": "Compiler",
                "vault_path": str(vault_path),
                "hero_image_url": data.get("hero_image_url"),
            })
            q.replace_sections(conn, article_id, data.get("sections", []))
            # Stub any body-referenced targets that don't have their own article
            # yet. This happens before set_related so related-link reconciliation
            # sees the stubs. Self-links are skipped.
            for target_id, display_label in link_refs.items():
                if target_id == article_id:
                    continue
                q.ensure_stub_article(conn, id=target_id, title=display_label, kind="Entity")
            q.set_related(conn, article_id, data.get("related", []))
            if source_id is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO article_sources (article_id, source_id) VALUES (?, ?)",
                    (article_id, source_id),
                )

        # Mirror to vault
        vault_path.parent.mkdir(parents=True, exist_ok=True)
        vault_path.write_text(_render_markdown(data))

    def _vault_path_for(self, kind: str, slug: str) -> Path:
        folder = {
            "Concept": "concepts",
            "Entity": "entities",
            "Source": "sources",
            "Synthesis": "synthesis",
        }.get(kind, "concepts")
        return vault_dir() / folder / f"{slug}.md"


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
        out.append(_strip_html(s.get("body", "")))
        out.append("")
    return "\n".join(out)


def _strip_html(html: str) -> str:
    # Preserve link targets as markdown-like refs
    s = re.sub(r'<span class="link" data-target="([^"]+)">([^<]+)</span>', r"[[\2|\1]]", html)
    s = re.sub(r"<em>([^<]+)</em>", r"*\1*", s)
    s = re.sub(r"<[^>]+>", "", s)
    return s


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
