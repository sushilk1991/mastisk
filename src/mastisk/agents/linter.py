"""Linter — periodic health checker for the wiki.

Deterministic passes (no LLM needed):

- Dangling wiki links: ``<span class="link" data-target="slug">`` in any
  article body whose ``slug`` doesn't exist in ``articles``.
- Orphans: articles with zero incoming + zero outgoing links (excluding
  self-links).
- Empty articles: no sections or only empty section bodies.
- Unresolved related links: the Compiler drops related-link targets that
  don't exist yet. When a sibling article later appears, we try to
  reconnect it.

Findings emit ``feed`` rows with ``verb="flagged"`` / ``"cleaned"`` so the
Agents view and live rail surface them. We don't block on an LLM — if
``summarize_model_cheap`` is reachable we ask it to write a one-line
suggestion per flagged article; if not, we just emit the structural finding.
"""
from __future__ import annotations

import asyncio
import logging
import re

from mastisk.agents.base import Agent
from mastisk.bridges import ollama_bridge
from mastisk.db import queries as q
from mastisk.db.queries import connect

log = logging.getLogger("mastisk.linter")

LINK_RE = re.compile(r'<span class="link"\s+data-target="([^"]+)"[^>]*>')


class Linter(Agent):
    name = "linter"
    tick_seconds = 900  # 15 min

    # Cap per-tick advisory LLM calls so a large vault doesn't grind the
    # local model. The structural pass always runs; the LLM pass is best-effort.
    max_llm_suggestions = 5

    async def run_once(self) -> None:
        if self.disabled_tick():
            return
        # Linter usually has no queued work — its tick IS its scan. We still
        # drain any externally-queued jobs (future: targeted re-lints of a
        # single article id).
        job = self._pick_job()
        if job:
            self._mark_running(job["id"])
            try:
                await self._handle(job)
                self._mark_done(job["id"])
            except Exception as e:
                log.exception("linter: job %s failed", job["id"])
                self._mark_failed(job["id"], str(e))

        # Graph-repair runs before the structural scan so the scan's orphan
        # check sees an up-to-date links table (otherwise articles that just
        # got reconnected would still be "flagged orphan" for one tick).
        self._graph_repair()
        await self._scan()

    async def _handle(self, job: dict) -> None:
        # Targeted re-scan of a single article: payload {"article_id": "..."}
        import json
        payload = json.loads(job.get("payload_json") or "{}")
        article_id = payload.get("article_id")
        if not article_id:
            return
        await self._scan(article_id=article_id)

    async def _scan(self, *, article_id: str | None = None) -> None:
        with connect() as conn:
            if article_id:
                articles = conn.execute(
                    "SELECT id, title, body_md FROM articles WHERE id = ?", (article_id,)
                ).fetchall()
            else:
                articles = conn.execute(
                    "SELECT id, title, body_md FROM articles"
                ).fetchall()
            known_ids = {
                r["id"] for r in conn.execute("SELECT id FROM articles")
            }

        # Findings collected this tick. Each carries its (kind, article_id,
        # target) tuple plus a display title; we dedup via record_finding and
        # only emit a feed row when the finding is newly seen.
        findings: list[dict] = []
        # (article_id, set_of_dangling_targets_seen_this_tick). Used after the
        # loop to clear any *previously* open 'dangling' findings whose target
        # either no longer appears or has since resolved.
        dangling_by_article: dict[str, set[str]] = {}
        articles_scanned: list[dict] = []

        for row in articles:
            body = row["body_md"] or ""
            with connect() as conn:
                sections = [
                    dict(r) for r in conn.execute(
                        "SELECT heading, body FROM article_sections WHERE article_id = ?",
                        (row["id"],),
                    )
                ]

            articles_scanned.append({"id": row["id"], "title": row["title"]})
            dangling = self._dangling_targets(sections, known_ids, self_id=row["id"])
            dangling_by_article[row["id"]] = set(dangling)
            for t in dangling:
                findings.append({
                    "kind": "dangling", "article_id": row["id"],
                    "title": row["title"], "target": t,
                })

            if self._is_empty(sections, body):
                findings.append({
                    "kind": "empty", "article_id": row["id"],
                    "title": row["title"], "target": None,
                })

        orphans = self._orphans() if article_id is None else []
        orphan_ids = {o["id"] for o in orphans}
        for o in orphans:
            findings.append({
                "kind": "orphan", "article_id": o["id"],
                "title": o["title"], "target": None,
            })

        # Emit structural findings only when first seen. Bump last_seen
        # silently otherwise. Also clear stale dangling findings whose
        # target is no longer in the article body (target may have been
        # created since, or the link was edited away).
        new_findings: list[dict] = []
        with connect() as conn:
            for f in findings:
                is_new = q.record_finding(
                    conn,
                    kind=f["kind"],
                    article_id=f["article_id"],
                    target=f.get("target"),
                )
                if is_new:
                    new_findings.append(f)

            # For every article we scanned, close any stale 'dangling' rows
            # whose target is no longer in the current set of dangling
            # targets (either resolved or removed).
            for scanned in articles_scanned:
                q.resolve_findings_for_article(
                    conn,
                    kind="dangling",
                    article_id=scanned["id"],
                    keep_targets=dangling_by_article.get(scanned["id"], set()),
                )

            # Full-scan orphan housekeeping: if we scanned every article
            # this tick, articles NOT in the orphan list are no longer
            # orphans — resolve their open 'orphan' finding if one exists.
            if article_id is None:
                open_orphan_rows = conn.execute(
                    """SELECT hash, article_id FROM lint_findings
                       WHERE kind = 'orphan' AND resolved_at IS NULL"""
                ).fetchall()
                for r in open_orphan_rows:
                    if r["article_id"] not in orphan_ids:
                        q.resolve_finding(conn, hash=r["hash"])

        for f in new_findings:
            obj = (f.get("title") or f["article_id"])[:80]
            payload = {k: f[k] for k in ("kind", "article_id", "title", "target") if f.get(k) is not None}
            self.emit_feed(verb="flagged", obj=obj, kind=f["kind"], payload=payload)

        # Best-effort advisory LLM pass on a handful of newly-flagged items.
        advisories = [f for f in new_findings if f["kind"] in ("empty", "orphan")][: self.max_llm_suggestions]
        if advisories:
            await self._llm_advise(advisories)

    # ───── graph repair ─────

    def _graph_repair(self) -> int:
        """Ensure every resolvable ``data-target="slug"`` reference has a row
        in ``links``. Emits one ``verb="repaired"`` feed row per tick summarizing
        the count, but only if N > 0.

        This runs every Linter tick AND once on scheduler startup to close the
        gap between "Compiler wrote articles before this pass existed" and the
        first Linter tick.

        Returns the number of new link rows inserted.
        """
        reconnected = self._repair_graph_once()
        if reconnected:
            self.emit_feed(
                verb="repaired",
                obj=f"{reconnected} links reconnected",
                kind="reconnect",
                touched=reconnected,
                payload={"count": reconnected},
            )
        return reconnected

    @staticmethod
    def _repair_graph_once() -> int:
        """Pure SQL pass: walk all article sections, extract data-target slugs,
        insert a links row for every pair where the target exists. Returns the
        count of *newly* inserted rows (not the total count of resolvable refs).
        """
        inserted = 0
        with connect() as conn:
            known_ids = {r["id"] for r in conn.execute("SELECT id FROM articles")}
            article_ids = list(known_ids)

            for aid in article_ids:
                sections = conn.execute(
                    "SELECT body FROM article_sections WHERE article_id = ?",
                    (aid,),
                ).fetchall()
                # Collect every referenced, existing target in this article.
                targets: set[str] = set()
                for s in sections:
                    for m in LINK_RE.finditer(s["body"] or ""):
                        t = m.group(1)
                        if t and t != aid and t in known_ids:
                            targets.add(t)
                for t in targets:
                    cur = conn.execute(
                        """INSERT OR IGNORE INTO links
                           (from_article, to_article, weight, snippet)
                           VALUES (?, ?, 0.5, ?)""",
                        (aid, t, "[linter] graph repair"),
                    )
                    # sqlite3 rowcount is 1 on insert, 0 when IGNORE blocks.
                    if cur.rowcount and cur.rowcount > 0:
                        inserted += cur.rowcount
        return inserted

    @classmethod
    def repair_graph(cls) -> int:
        """Public classmethod for callers outside a running Linter instance
        (e.g. scheduler startup). Runs the repair pass without emitting a feed
        row — the scheduler logs the count instead.
        """
        return cls._repair_graph_once()

    # ───── pure checks ─────

    def _dangling_targets(self, sections: list[dict], known: set[str], *, self_id: str) -> list[str]:
        seen: set[str] = set()
        for s in sections:
            for m in LINK_RE.finditer(s.get("body", "") or ""):
                t = m.group(1)
                if t and t != self_id and t not in known:
                    seen.add(t)
        return sorted(seen)

    def _resolvable_in_body(self, sections: list[dict], known: set[str], *, self_id: str) -> list[str]:
        seen: set[str] = set()
        for s in sections:
            for m in LINK_RE.finditer(s.get("body", "") or ""):
                t = m.group(1)
                if t and t != self_id and t in known:
                    seen.add(t)
        return sorted(seen)

    def _is_empty(self, sections: list[dict], body_md: str) -> bool:
        if not sections:
            return not body_md.strip()
        return all(not (s.get("body") or "").strip() for s in sections)

    def _orphans(self) -> list[dict]:
        with connect() as conn:
            rows = conn.execute(
                """SELECT a.id, a.title FROM articles a
                   WHERE (SELECT COUNT(*) FROM links
                             WHERE from_article = a.id AND to_article != a.id) = 0
                     AND (SELECT COUNT(*) FROM links
                             WHERE to_article = a.id AND from_article != a.id) = 0
                   ORDER BY a.updated_at DESC LIMIT 10"""
            ).fetchall()
        return [dict(r) for r in rows]

    # ───── advisory pass (cheap local model) ─────

    async def _llm_advise(self, items: list[dict]) -> None:
        """Ask the cheap local model for a one-line fix suggestion per item.

        Fails open — if Ollama is unreachable, we just skip this pass. The
        structural findings are already emitted above.
        """
        for item in items:
            try:
                prompt = (
                    f"You are advising a wiki janitor. An article titled {item['title']!r} "
                    f"was flagged as {item['kind']!r}. Suggest one concrete next step in "
                    f"<= 20 words. Plain text, no markdown, no preamble."
                )
                reply = await asyncio.wait_for(
                    ollama_bridge.chat(prompt, cheap=True), timeout=30,
                )
                if reply:
                    self.emit_feed(
                        verb="advised",
                        obj=item["title"][:80],
                        kind=item["kind"],
                        payload={"article_id": item["article_id"], "suggestion": reply.strip()[:400]},
                    )
            except Exception as e:
                log.info("linter advisory skipped: %s", e)
                break  # one failure means the model is down; don't hammer it
