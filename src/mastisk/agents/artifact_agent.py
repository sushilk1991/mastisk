"""ArtifactAgent — generates 1–3 visual artifacts per article via Claude Code.

Unlike Scout/Compiler/Linter, this agent is **purely job-driven**: it doesn't
spontaneously scan the corpus. The frontend (or any other caller) enqueues an
``artifact-agent`` ``generate`` job via
``POST /api/articles/{id}/artifacts/regenerate`` and we drain the queue on each
tick.

Pipeline per job:
  1. Load the article.
  2. Ask Claude for a JSON array of 1–3 artifacts (chart/comparison/timeline/
     stat) that illuminate the article.
  3. Parse + validate. Drop any malformed entries rather than failing the whole
     batch — one bad entry shouldn't blank the right-rail.
  4. Replace-strategy: delete prior ``artifact-agent``-authored artifacts for
     this article (preserving user-created ones), insert the new batch.
  5. Emit a ``feed`` row so the live rail shows the regeneration.

Fail-loud: if Claude is unreachable or returns unparseable output, the job is
marked failed so the Queue view shows the error instead of a silent no-op.
"""
from __future__ import annotations

import json
import logging
import re

from mastisk.agents.base import Agent
from mastisk.agents.registry import resolve_prompt
from mastisk.bridges import intelligence
from mastisk.db import queries as q
from mastisk.db.queries import connect

log = logging.getLogger("mastisk.artifact_agent")


# Allowed artifact kinds; anything else gets dropped during validation.
_ALLOWED_KINDS = {"chart", "comparison", "timeline", "stat"}


# The prompt shown to the model. Spelled out verbatim so the behaviour is
# reviewable without running the agent. Schemas crib directly from the UI
# contract (see src/mastisk/db/schema.sql + the right-rail renderer).
_ARTIFACT_PROMPT_SCHEMA = """\
You generate at most 3 *visual artifacts* that help a reader understand a wiki
article. Pick the MOST illuminating 1–3 — do not pad. A single high-quality
chart beats four mediocre cards. If the article has no quantitative or
comparative structure, a single well-chosen comparison or timeline is fine.

Return a single fenced ```json``` block containing a JSON ARRAY. Each element
is an object of the form:

  { "kind": "...", "title": "...", "description": "...", "spec": { ... } }

where:
- "kind" is one of: "chart", "comparison", "timeline", "stat"
- "title" is a short headline (<= 80 chars)
- "description" is 1–2 sentences of narrative (<= 240 chars) that sits next
  to the visualisation
- "spec" is the declarative payload for the given kind, per below.

─── kind: "chart" — Chart.js config ────────────────────────────────────────
{
  "type": "bar" | "line" | "pie" | "doughnut" | "radar" | "scatter",
  "data": {
    "labels": ["..."],
    "datasets": [
      { "label": "...", "data": [1, 2, 3], "backgroundColor": "...optional..." }
    ]
  },
  "options": {
    "maintainAspectRatio": false,
    "responsive": true
  }
}
Use this for anything quantitative — proportions, trends, comparisons of
magnitudes. "options.maintainAspectRatio" MUST be false so the canvas fills
the card. Do not invent numbers; only chart what the article supports.

─── kind: "comparison" — side-by-side columns ──────────────────────────────
{
  "columns": [
    { "label": "Approach A", "rows": ["row 1", "row 2", "row 3"] },
    { "label": "Approach B", "rows": ["row 1", "row 2", "row 3"] }
  ]
}
Each column has the SAME NUMBER of rows; rows are aligned positionally (row 0
of column A compares to row 0 of column B). Use this for trade-offs, A-vs-B,
or feature matrices.

─── kind: "timeline" — ordered events ──────────────────────────────────────
{
  "events": [
    { "date": "2024-03-15" | "Q1 2025" | "March 2024",
      "label": "Short event name",
      "detail": "One-sentence context." }
  ]
}
Dates can be ISO (YYYY-MM-DD), a quarter ("Q1 2025"), a month ("March 2024"),
or a bare year. Use this for releases, milestones, historical arcs.

─── kind: "stat" — big-number callout ──────────────────────────────────────
{
  "value": 42 | "42%",
  "unit": "tok/s" | "ms" | "%" (optional),
  "label": "peak throughput",
  "delta": 12 (optional — signed number, the delta vs. baseline),
  "deltaPositive": true (optional — true if the delta is a good thing)
}
Use this sparingly — only when the article hinges on a single striking
number that belongs front-and-centre.

Rules:
- Pick 1–3 artifacts, not more, not fewer.
- Do not invent data the article doesn't contain. It is better to skip a
  chart than to fabricate numbers. If the article is non-numeric, prefer
  comparison/timeline/stat over chart.
- Titles must be headline-cased, concise. Descriptions must not repeat the
  title.
- Return ONLY a fenced ```json``` block. No prose before or after it.
"""


class ArtifactAgent(Agent):
    name = "artifact-agent"
    tick_seconds = 120  # 2 min — responsive without hammering.

    # Drain up to this many jobs per tick; APScheduler's max_instances=1 keeps
    # ticks from overlapping, so a giant backlog won't pile up in memory.
    max_jobs_per_tick = 5

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
        payload = json.loads(job.get("payload_json") or "{}")
        article_id = payload.get("article_id")
        if not article_id:
            log.warning("artifact-agent: no article_id in job %s", job["id"])
            return

        with connect() as conn:
            row = conn.execute(
                "SELECT id, kind, title, summary, body_md FROM articles WHERE id = ?",
                (article_id,),
            ).fetchone()
        if not row:
            log.warning("artifact-agent: article %s not found", article_id)
            return
        article = dict(row)

        prompt = self._build_prompt(article)

        # Fail loud if every bridge is unreachable — the user clicked regenerate and
        # deserves to see the failure in the queue row instead of a silent no-op.
        try:
            reply, _ = await intelligence.run_intelligence(prompt, timeout_s=300)
        except Exception as e:
            log.warning("artifact-agent: all LLM bridges unreachable for article %s: %s",
                        article_id, e)
            raise RuntimeError(
                f"all LLM bridges unreachable — check your `claude` / `codex` / "
                f"`ollama` install (article {article_id})"
            ) from e

        text = reply.get("text", "") if isinstance(reply, dict) else str(reply)
        artifacts = self._parse_response(text)
        if not artifacts:
            log.info("artifact-agent: no valid artifacts parsed for article %s "
                     "(reply len=%d)", article_id, len(text))
            raise RuntimeError(
                f"model returned no parseable artifacts for {article_id} "
                f"(reply len={len(text)})"
            )

        valid = [a for a in artifacts if self._is_valid(a)]
        if not valid:
            log.info("artifact-agent: all %d artifacts failed validation for %s",
                     len(artifacts), article_id)
            raise RuntimeError(
                f"all {len(artifacts)} generated artifacts failed validation "
                f"for {article_id}"
            )

        # Cap at 3 regardless of what the model returned — defensive against a
        # model that ignores the "1–3" instruction.
        valid = valid[:3]

        # Replace-strategy: delete existing *agent-authored* artifacts, then
        # insert the new batch. User-created artifacts (created_by='user' or
        # anything other than 'artifact-agent') are left alone.
        inserted = 0
        insert_errors: list[str] = []
        with connect() as conn, q.txn(conn):
            existing = q.list_artifacts(conn, article_id)
            for old in existing:
                if old.get("created_by") == self.name:
                    q.delete_artifact(conn, old["id"])
            for a in valid:
                try:
                    q.create_artifact(
                        conn,
                        article_id=article_id,
                        kind=a["kind"],
                        title=str(a["title"])[:200],
                        description=(str(a["description"])[:1000]
                                     if a.get("description") else None),
                        spec_json=json.dumps(a["spec"]),
                        created_by=self.name,
                    )
                    inserted += 1
                except Exception as e:
                    # Don't blow up the whole batch if one insert fails; just
                    # log, collect the reason, and keep going. Most likely
                    # cause is a spec that isn't JSON-serializable.
                    log.warning("artifact-agent: insert failed for %s: %s",
                                article_id, e)
                    insert_errors.append(str(e))

        if not inserted:
            # Every insert failed (or valid was somehow empty at this point).
            # Surface this as a failed job so the Queue view shows the error
            # instead of silently reporting "done" with an empty panel.
            reason = insert_errors[0] if insert_errors else "no artifacts inserted"
            raise RuntimeError(
                f"no artifacts inserted for {article_id} "
                f"({len(valid)} valid, {len(insert_errors)} insert errors): {reason}"
            )

        self.emit_feed(
            verb="generated",
            obj=(article.get("title") or article_id)[:80],
            kind="artifact",
            touched=inserted,
            payload={"article_id": article_id, "count": inserted},
        )

    # ───── prompt construction ─────

    def _build_prompt(self, article: dict) -> str:
        title = article.get("title") or article.get("id") or ""
        summary = article.get("summary") or ""
        body = (article.get("body_md") or "")[:4000]
        kind = article.get("kind") or ""
        schema = resolve_prompt("artifact-agent", "generate", _ARTIFACT_PROMPT_SCHEMA)
        return (
            f"{schema}\n\n"
            f"─── Article ────────────────────────────────────────────────\n"
            f"Title: {title}\n"
            f"Kind: {kind}\n"
            f"Summary: {summary}\n\n"
            f"Body (first 4000 chars):\n{body}\n"
        )

    # ───── response parsing ─────

    @staticmethod
    def _parse_response(text: str) -> list[dict]:
        """Pull a JSON array out of the model response.

        Strategy:
        1. Try to extract a fenced ```json ... ``` (or plain ``` ... ```) block
           and parse it. If parsing succeeds and yields a list → use it. If it
           yields a dict with an ``artifacts`` / ``items`` key that is a list,
           unwrap that.
        2. Fall back to hunting for the first top-level ``[...]`` in the raw
           text (handles models that forgot the fence).
        3. On any parse failure, return ``[]`` — the caller logs and bails.

        We deliberately accept a dict wrapper because some models really like
        wrapping arrays in objects even when told not to.
        """
        if not text:
            return []

        # 1. Try fenced block first.
        fenced = ArtifactAgent._extract_fenced(text)
        candidates = [fenced] if fenced is not None else []

        # 2. Also try any top-level array as a fallback (cheap to try).
        m = re.search(r"\[\s*\{.*\}\s*\]", text, flags=re.DOTALL)
        if m:
            candidates.append(m.group(0))

        for raw in candidates:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(data, list):
                return [x for x in data if isinstance(x, dict)]
            if isinstance(data, dict):
                for key in ("artifacts", "items", "results"):
                    v = data.get(key)
                    if isinstance(v, list):
                        return [x for x in v if isinstance(x, dict)]
        return []

    @staticmethod
    def _extract_fenced(text: str) -> str | None:
        """Return the content of the first ```json ... ``` (or ``` ... ```)
        block, or None."""
        start = text.find("```json")
        if start == -1:
            start = text.find("```")
            if start == -1:
                return None
            nl = text.find("\n", start)
            if nl == -1:
                return None
            start = nl + 1
        else:
            nl = text.find("\n", start)
            if nl == -1:
                return None
            start = nl + 1
        end = text.find("```", start)
        if end == -1:
            return None
        return text[start:end].strip()

    # ───── validation ─────

    @staticmethod
    def _is_valid(a: dict) -> bool:
        """Structural validation — kind in allowed set, title non-empty,
        spec is a dict. We *don't* deep-validate spec shape here — the UI
        renders defensively, and a half-broken chart is more useful than
        no artifact at all."""
        if not isinstance(a, dict):
            return False
        kind = a.get("kind")
        if kind not in _ALLOWED_KINDS:
            return False
        title = a.get("title")
        if not isinstance(title, str) or not title.strip():
            return False
        spec = a.get("spec")
        if not isinstance(spec, dict):
            return False
        # Best-effort: ensure chart configs don't lock the canvas aspect
        # ratio. We *enforce* the rule rather than drop the artifact because
        # it's a purely cosmetic concern and the model may forget it.
        if kind == "chart":
            options = spec.setdefault("options", {})
            if isinstance(options, dict):
                options.setdefault("maintainAspectRatio", False)
        return True
