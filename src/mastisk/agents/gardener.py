"""Gardener — daily consolidation + reflection over the wiki.

Writer agents only ever add: the Compiler mints stub Entity pages that stay
empty, and nothing ever looks back across accumulated activity to extract
what it means. Every serious agent-memory system converges on a background
consolidation pass (Letta/MemGPT sleep-time compute, Stanford
generative-agents reflection, Zep/Graphiti edge invalidation); this agent is
Mastisk's, adapted to a wiki whose pages are overwritten rather than
appended:

- **Weave pass** — a stub/thin Entity or Concept page that has accumulated
  enough backlinks gets written into a real article, derived ONLY from what
  the referring articles already say about it (deterministic entity-keyed
  context assembly, no embeddings). Quality contract: no new facts, no
  invented sources, dated Key facts with supersession.
- **Reflection pass** — once a day, recent wiki activity (what was compiled,
  what the user actually read, what they escalated) is distilled into 0-2
  durable, dated learnings appended to ``vault/_self/learnings.md`` — which
  every writer agent already loads via ``load_identity()``. This is the M2
  Reflection slot the README documents.

Timer-driven like TopicSuggester: hourly tick, self-gating on per-page
``curated_at`` cooldowns, a daily weave cap, and a 22h reflection cadence.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

from mastisk.agents.base import Agent
from mastisk.agents.registry import resolve_prompt
from mastisk.bridges import claude_bridge, intelligence
from mastisk.db.queries import connect
from mastisk.memory_conventions import DATED_FACTS_PROMPT
from mastisk.paths import self_dir
from mastisk.settings import get_settings
from mastisk.vault_io import read_vault_text, write_vault_text

log = logging.getLogger("mastisk.gardener")

LINK_RE = re.compile(r'<span class="link"\s+data-target="([^"]+)"[^>]*>([^<]*)</span>')

WEAVE_PROMPT = """You are Mastisk's Gardener, weaving a stub wiki page into a real article.

The page below has no content of its own, but other articles in the wiki keep
referencing it. Write the article FROM THE REFERENCES ALONE.

# NON-NEGOTIABLE RULES
1. **No new facts.** Every claim must be derivable from the reference excerpts
   below. You consolidate what the wiki already knows; you never research,
   never embellish, never invent sources or URLs.
2. **Keep the id `__ARTICLE_ID__` and kind `__ARTICLE_KIND__` exactly.**
3. If the excerpts genuinely don't support an article (too thin, mere
   name-drops), set "skip": true with a one-line "skip_reason".
4. Confidence is capped by construction: this is secondhand synthesis, so
   report at most 0.6.

{identity}

{registry}

# The stub page
id: __ARTICLE_ID__
title: __ARTICLE_TITLE__
kind: __ARTICLE_KIND__

# What the wiki says about it (excerpts from referring articles)
__CONTEXT__

Structure guidance:
- 300–800 words. Lead with what this thing IS, then what the wiki's articles
  collectively say about it, then how the views differ (attribute claims to
  their source articles with <span class="link"> references).
- Include a "Key facts" section when the excerpts carry durable specifics.
__DATED_FACTS__

__SCHEMA__
"""

REFLECT_PROMPT = """You maintain `learnings.md` — durable, dated observations about the user that
their agents load into every prompt. You are shown the last week of wiki
activity and the current tail of the file.

Look for patterns no single event shows: topics the user keeps returning to,
what they actually read versus what merely lands in the wiki, threads they
escalate, appetites shifting. Distill AT MOST {max_learnings} genuinely
durable learnings.

Rules:
- Derive only from the activity shown. No speculation about the user's life.
- A learning must be worth reading in three months. "User read an article
  about X today" is not a learning; "keeps returning to X across weeks —
  treat it as a core interest" is.
- Never restate what the identity files already say, and never duplicate an
  existing learning from the tail shown below.
- One line each, concrete, no hedging. If nothing durable emerged, return an
  empty list — most days should.

# Current tail of learnings.md
{learnings_tail}

# The week's activity
{activity}

Return STRICT JSON only: {{"learnings": ["...", "..."]}} (or {{"learnings": []}}).
"""

DISTILL_PROMPT = """You distill the user's explicit article feedback into short preference rules
their agents apply from now on. You are shown the new thumbs-up/down verdicts
(with optional reasons) and the rules that already exist.

Rules:
- At most {max_rules} new rules; return an empty list unless a real pattern
  repeats across verdicts. One noisy dislike is not a rule.
- Each rule is one imperative line an agent can act on ("Skip funding-round
  news unless it names a technical shift", not "user disliked an article").
- When a rule is a pure content filter — a topic to never ingest — phrase it
  exactly as `avoid: <keyword or phrase>`. The RSS Scout applies those
  mechanically against titles and summaries.
- Never duplicate or contradict an existing rule; refine it instead (return
  the refined replacement and it will be appended as the newer word).

# Existing preference rules
{existing_rules}

# New feedback
{feedback}

Return STRICT JSON only: {{"rules": ["...", "..."]}} (or {{"rules": []}}).
"""

PREFERENCE_RULES_HEADING = "## Preference rules"


class Gardener(Agent):
    """Daily consolidation + reflection. See module docstring."""

    name: ClassVar[str] = "gardener"
    tick_seconds: ClassVar[int] = 3600  # hourly; passes self-gate on cadence/caps

    # Timer-driven, not job-driven (same pattern as TopicSuggester).
    async def _handle(self, job: dict) -> None:  # pragma: no cover - never invoked
        raise NotImplementedError("gardener is timer-driven, not job-driven")

    async def run_once(self) -> None:
        if self.disabled_tick():
            return
        try:
            await self._weave_pass()
        except Exception:
            log.exception("gardener: weave pass failed")
        try:
            await self._reflect_pass()
        except Exception:
            log.exception("gardener: reflection pass failed")
        try:
            await self._distill_pass()
        except Exception:
            log.exception("gardener: distill pass failed")

    # ───── weave pass ─────

    async def _weave_pass(self) -> None:
        cfg = get_settings().gardener
        with connect() as conn:
            woven_today = conn.execute(
                "SELECT COUNT(*) FROM feed WHERE agent='gardener' AND verb='wove' "
                "AND ts >= datetime('now', 'start of day')",
            ).fetchone()[0]
        remaining = cfg.weave_daily_cap - woven_today
        if remaining <= 0:
            return

        candidates = self._weave_candidates(limit=remaining)
        for cand in candidates:
            try:
                await self._weave_one(cand)
            except Exception:
                log.exception("gardener: weaving %s failed", cand["id"])

    def _weave_candidates(self, *, limit: int) -> list[dict]:
        """Stub/thin Entity+Concept pages with enough backlinks, cooldown
        respected, most-referenced first. Pure SQL — the gate is code, not
        the model."""
        cfg = get_settings().gardener
        with connect() as conn:
            rows = conn.execute(
                """SELECT a.id, a.title, a.kind,
                          (SELECT COUNT(*) FROM links WHERE to_article = a.id) AS backlinks
                   FROM articles a
                   WHERE a.kind IN ('Entity', 'Concept')
                     AND (a.confidence <= 0.2 OR length(COALESCE(a.body_md, '')) < 200)
                     AND (a.curated_at IS NULL
                          OR a.curated_at <= datetime('now', ?))
                     AND (SELECT COUNT(*) FROM links WHERE to_article = a.id) >= ?
                   ORDER BY backlinks DESC, a.updated_at ASC
                   LIMIT ?""",
                (f"-{int(cfg.cooldown_days)} days", cfg.weave_min_backlinks, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def _weave_context(self, article_id: str, *, max_chars: int = 24000) -> str:
        """Entity-keyed context assembly: for each referring article, its
        title/summary plus only the sections that actually mention the target."""
        with connect() as conn:
            referrers = conn.execute(
                """SELECT a.id, a.title, a.summary
                   FROM links l JOIN articles a ON a.id = l.from_article
                   WHERE l.to_article = ?
                   ORDER BY a.updated_at DESC LIMIT 12""",
                (article_id,),
            ).fetchall()
            blocks: list[str] = []
            used = 0
            for ref in referrers:
                sections = conn.execute(
                    "SELECT heading, body FROM article_sections WHERE article_id = ? ORDER BY idx",
                    (ref["id"],),
                ).fetchall()
                excerpts = [
                    f"  [{s['heading']}] {_strip_tags(s['body'])[:1500]}"
                    for s in sections
                    if f'data-target="{article_id}"' in (s["body"] or "")
                ]
                if not excerpts:
                    continue
                block = (
                    f"## From `{ref['id']}` — {ref['title']}\n"
                    f"{(ref['summary'] or '').strip()}\n" + "\n".join(excerpts)
                )
                if used + len(block) > max_chars:
                    break
                blocks.append(block)
                used += len(block)
        return "\n\n".join(blocks)

    async def _weave_one(self, cand: dict) -> None:
        # Import here: Compiler owns article persistence (normalize + upsert +
        # vault mirror + stub gate); reusing it keeps one write path.
        from mastisk.agents.compiler import (
            ARTICLE_JSON_SCHEMA,
            SCHEMA_MD,
            Compiler,
            _normalize_article_data,
        )

        context = self._weave_context(cand["id"])
        if not context:
            # Backlinks exist but no section actually discusses the target
            # (e.g. related-only links). Stamp the cooldown so we don't
            # re-check this page every hour for a week.
            self._stamp_curated(cand["id"])
            return

        compiler = Compiler()
        # .format() first (its placeholders carry no other braces), THEN the
        # __TOKEN__ substitutions — context and SCHEMA_MD are full of JSON
        # braces that would blow up str.format.
        prompt = (
            resolve_prompt("gardener", "weave", WEAVE_PROMPT)
            .format(identity=self.load_identity(), registry=compiler._known_articles_block())
            .replace("__ARTICLE_ID__", cand["id"])
            .replace("__ARTICLE_TITLE__", cand["title"] or cand["id"])
            .replace("__ARTICLE_KIND__", cand["kind"])
            .replace("__CONTEXT__", context)
            .replace("__DATED_FACTS__", DATED_FACTS_PROMPT)
            .replace("__SCHEMA__", SCHEMA_MD)
        )

        resp, provider = await intelligence.run_intelligence(
            prompt, json_object=True, json_schema=ARTICLE_JSON_SCHEMA,
        )
        data = claude_bridge.extract_json_block(resp.get("text") or "")
        if not data:
            log.warning("gardener: no JSON from %s for %s", provider, cand["id"])
            return
        if data.get("skip"):
            self._stamp_curated(cand["id"])
            self.emit_feed(
                verb="skipped", obj=(cand["title"] or cand["id"])[:80], kind="weave",
                payload={"article_id": cand["id"], "reason": data.get("skip_reason")},
            )
            return

        _normalize_article_data(data)
        if not data.get("title") or not data.get("sections"):
            log.warning("gardener: %s reply for %s lacks title/sections", provider, cand["id"])
            return
        data["id"] = cand["id"]  # never let the model coin a new slug
        data["kind"] = cand["kind"]
        data["confidence"] = min(float(data.get("confidence", 0.5)), 0.6)

        compiler._persist_article(data, source_id=None, updated_by="Gardener")
        self._stamp_curated(cand["id"])
        self.emit_feed(
            verb="wove", obj=data["title"][:80], kind=cand["kind"].lower(), touched=1,
            payload={"article_id": cand["id"], "provider": provider},
        )

    def _stamp_curated(self, article_id: str) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE articles SET curated_at = datetime('now') WHERE id = ?",
                (article_id,),
            )

    # ───── reflection pass ─────

    async def _reflect_pass(self) -> None:
        cfg = get_settings().gardener
        with connect() as conn:
            recent = conn.execute(
                "SELECT 1 FROM feed WHERE agent='gardener' AND verb='reflected' "
                "AND ts >= datetime('now', '-22 hours') LIMIT 1",
            ).fetchone()
        if recent is not None:
            return

        activity = self._activity_digest()
        if activity.count("\n") < 3:
            # Not enough signal to reflect on; retry next tick without
            # burning an LLM call or the cadence.
            return

        prompt = resolve_prompt("gardener", "reflect", REFLECT_PROMPT).format(
            max_learnings=cfg.reflect_max_learnings,
            learnings_tail=self._learnings_tail(),
            activity=activity,
        )
        resp, provider = await intelligence.run_intelligence(prompt, timeout_s=180)
        data = claude_bridge.extract_json_block(resp.get("text") or "")
        learnings = (data or {}).get("learnings")
        if not isinstance(learnings, list):
            log.warning("gardener: reflection reply from %s not parseable", provider)
            return
        cleaned = [
            entry.strip() for entry in learnings
            if isinstance(entry, str) and entry.strip()
        ][: cfg.reflect_max_learnings]

        if cleaned:
            self._append_learnings(cleaned)
        self.emit_feed(
            verb="reflected",
            obj=f"{len(cleaned)} learning{'s' if len(cleaned) != 1 else ''}",
            kind="reflection",
            touched=len(cleaned),
            payload={"learnings": cleaned, "provider": provider},
        )

    def _activity_digest(self) -> str:
        """A week of wiki activity, deterministic pull: what was written,
        what was actually read, what the user escalated."""
        lines: list[str] = []
        with connect() as conn:
            wrote = conn.execute(
                """SELECT verb, obj FROM feed
                   WHERE agent IN ('compiler', 'synthesizer') AND verb IN ('wrote', 'updated', 'enriched')
                     AND ts >= datetime('now', '-7 days')
                   ORDER BY ts DESC LIMIT 15""",
            ).fetchall()
            if wrote:
                lines.append("Written into the wiki this week:")
                lines += [f"- {r['verb']}: {r['obj']}" for r in wrote]
            read = conn.execute(
                """SELECT a.title, COUNT(*) AS n,
                          SUM(CASE WHEN s.kind = 'time_read' THEN 1 ELSE 0 END) AS reads
                   FROM signals s JOIN articles a ON a.id = s.article_id
                   WHERE s.ts >= datetime('now', '-7 days')
                     AND s.kind IN ('opened', 'time_read', 'pinned', 'asked')
                   GROUP BY s.article_id ORDER BY n DESC LIMIT 8""",
            ).fetchall()
            if read:
                lines.append("What the user actually engaged with:")
                lines += [f"- {r['title']} ({r['n']} interactions)" for r in read]
            escalated = conn.execute(
                """SELECT obj FROM feed
                   WHERE agent = 'escalator' AND ts >= datetime('now', '-7 days')
                   ORDER BY ts DESC LIMIT 8""",
            ).fetchall()
            if escalated:
                lines.append("Notes the user's pipeline escalated to research:")
                lines += [f"- {r['obj']}" for r in escalated]
        return "\n".join(lines)

    # ───── feedback distillation pass ─────

    async def _distill_pass(self) -> None:
        """Rowboat's correction loop: once enough explicit thumbs verdicts
        accumulate, distill them into preference rules under
        `## Preference rules` in learnings.md. Rules override the generic
        rubric by riding into every prompt via load_identity(); `avoid:`
        rules are additionally applied mechanically by Scout."""
        cfg = get_settings().gardener
        watermark = self._distill_watermark()
        with connect() as conn:
            rows = conn.execute(
                """SELECT s.id, s.kind, s.value_json, a.title
                   FROM signals s LEFT JOIN articles a ON a.id = s.article_id
                   WHERE s.kind IN ('liked', 'disliked') AND s.id > ?
                   ORDER BY s.id ASC LIMIT 100""",
                (watermark,),
            ).fetchall()
        if len(rows) < cfg.distill_every:
            return

        feedback_lines = []
        for r in rows:
            reason = ""
            try:
                value = json.loads(r["value_json"] or "{}")
                if isinstance(value, dict) and value.get("reason"):
                    reason = f' — reason: "{value["reason"]}"'
            except ValueError:
                pass
            feedback_lines.append(f"- {r['kind']}: {r['title'] or '(deleted article)'}{reason}")

        prompt = resolve_prompt("gardener", "distill", DISTILL_PROMPT).format(
            max_rules=cfg.distill_max_rules,
            existing_rules=self._preference_rules_tail(),
            feedback="\n".join(feedback_lines),
        )
        resp, provider = await intelligence.run_intelligence(prompt, timeout_s=180)
        data = claude_bridge.extract_json_block(resp.get("text") or "")
        rules = (data or {}).get("rules")
        if not isinstance(rules, list):
            log.warning("gardener: distill reply from %s not parseable", provider)
            return
        cleaned = [
            r.strip() for r in rules if isinstance(r, str) and r.strip()
        ][: cfg.distill_max_rules]

        if cleaned:
            self._append_preference_rules(cleaned)
        # Advance the watermark even on zero rules — the batch was judged;
        # re-feeding it every tick would never converge.
        self.emit_feed(
            verb="distilled",
            obj=f"{len(cleaned)} rule{'s' if len(cleaned) != 1 else ''} from {len(rows)} verdicts",
            kind="reflection",
            touched=len(cleaned),
            payload={"rules": cleaned, "last_signal_id": rows[-1]["id"], "provider": provider},
        )

    def _distill_watermark(self) -> int:
        with connect() as conn:
            row = conn.execute(
                """SELECT payload_json FROM feed
                   WHERE agent='gardener' AND verb='distilled'
                   ORDER BY id DESC LIMIT 1""",
            ).fetchone()
        if not row:
            return 0
        try:
            return int(json.loads(row["payload_json"] or "{}").get("last_signal_id") or 0)
        except (ValueError, TypeError):
            return 0

    def _preference_rules_tail(self, *, max_lines: int = 20) -> str:
        path = self_dir() / "learnings.md"
        if not path.exists():
            return "(none yet)"
        text = read_vault_text(path)
        idx = text.find(PREFERENCE_RULES_HEADING)
        if idx == -1:
            return "(none yet)"
        section = text[idx + len(PREFERENCE_RULES_HEADING):]
        nxt = section.find("\n## ")
        if nxt != -1:
            section = section[:nxt]
        lines = [ln for ln in section.strip().splitlines() if ln.strip()]
        return "\n".join(lines[-max_lines:]) or "(none yet)"

    def _append_preference_rules(self, rules: list[str]) -> None:
        tz = ZoneInfo(get_settings().capture.default_timezone)
        today = datetime.now(tz).date().isoformat()
        path = self_dir() / "learnings.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        text = read_vault_text(path) if path.exists() else "# Learnings\n"
        block = "".join(f"- ({today}) {rule}\n" for rule in rules)
        if PREFERENCE_RULES_HEADING in text:
            # Append at the end of the existing section.
            idx = text.find(PREFERENCE_RULES_HEADING)
            section_start = idx + len(PREFERENCE_RULES_HEADING)
            nxt = text.find("\n## ", section_start)
            insert_at = len(text) if nxt == -1 else nxt
            text = text[:insert_at].rstrip("\n") + "\n" + block + text[insert_at:].lstrip("\n")
        else:
            text = text.rstrip("\n") + f"\n\n{PREFERENCE_RULES_HEADING}\n\n" + block
        write_vault_text(path, text)

    def _learnings_tail(self, *, max_lines: int = 30) -> str:
        path = self_dir() / "learnings.md"
        if not path.exists():
            return "(learnings.md does not exist yet)"
        tail = read_vault_text(path).splitlines()[-max_lines:]
        return "\n".join(tail) or "(empty)"

    def _append_learnings(self, learnings: list[str]) -> None:
        tz = ZoneInfo(get_settings().capture.default_timezone)
        today = datetime.now(tz).date().isoformat()
        path = self_dir() / "learnings.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        existing = read_vault_text(path) if path.exists() else "# Learnings\n"
        block = "".join(f"- ({today}) {entry}\n" for entry in learnings)
        write_vault_text(path, existing.rstrip("\n") + "\n" + block)


def _strip_tags(html: str) -> str:
    s = re.sub(r"</p>\s*<p[^>]*>", " ", html or "")
    return re.sub(r"<[^>]+>", "", s).strip()
