"""Agent base. Each specialist subclasses and implements `_handle(job)`."""
from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar

from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import self_dir

log = logging.getLogger("mastisk.agent")


class Agent(ABC):
    name: ClassVar[str]
    tick_seconds: ClassVar[int] = 300

    async def run_once(self) -> None:
        """Pick one job for this agent and handle it. Called by the scheduler."""
        if self.disabled_tick():
            return
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

    @abstractmethod
    async def _handle(self, job: dict) -> None: ...

    def is_enabled(self) -> bool:
        from mastisk.agents.registry import agent_enabled

        return agent_enabled(self.name)

    def disabled_tick(self) -> bool:
        if self.is_enabled():
            return False
        log.info("%s: disabled by agent profile; skipping tick", self.name)
        return True

    # ───── job queue helpers ─────

    def _pick_job(self) -> dict | None:
        with connect() as conn:
            row = conn.execute(
                """SELECT * FROM jobs WHERE agent = ? AND status = 'queued'
                   ORDER BY id ASC LIMIT 1""",
                (self.name,),
            ).fetchone()
            return dict(row) if row else None

    def _mark_running(self, job_id: int) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='running', started_at=CURRENT_TIMESTAMP, attempts=attempts+1 WHERE id=?",
                (job_id,),
            )

    def _mark_done(self, job_id: int) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='done', finished_at=CURRENT_TIMESTAMP WHERE id=?",
                (job_id,),
            )

    def _mark_failed(self, job_id: int, err: str) -> None:
        with connect() as conn:
            conn.execute(
                "UPDATE jobs SET status='failed', finished_at=CURRENT_TIMESTAMP, error=? WHERE id=?",
                (err, job_id),
            )

    # ───── identity ─────

    @staticmethod
    def load_identity() -> str:
        parts = []
        for fname in ("identity", "interests", "dislikes", "style", "learnings"):
            p = self_dir() / f"{fname}.md"
            if p.exists():
                txt = p.read_text().strip()
                if txt:
                    parts.append(f"## {fname}\n{txt}")
        if not parts:
            return "# About the user\n(no identity files yet — run `mastisk init` to bootstrap)"
        return "# About the user\n" + "\n\n".join(parts)

    # ───── feed helpers ─────

    def emit_feed(self, *, verb: str, obj: str, kind: str | None = None, touched: int = 0, payload: dict | None = None) -> None:
        with connect() as conn:
            q.append_feed(conn, agent=self.name, verb=verb, obj=obj, kind=kind, touched_pages=touched, payload=payload)


def enqueue(agent: str, kind: str, payload: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO jobs (agent, kind, payload_json) VALUES (?, ?, ?)",
            (agent, kind, json.dumps(payload)),
        )
        return cur.lastrowid or 0
