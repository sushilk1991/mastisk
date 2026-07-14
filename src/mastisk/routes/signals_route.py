"""Signal capture — opens, time-read, pins, deletes, asks, skips.

M1 only captures. M2's Reflection agent reads from here.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(tags=["signals"])

_ALLOWED = {
    "opened", "time_read", "pinned", "unpinned", "deleted", "edited", "asked", "skipped",
    "liked", "disliked",
}


class SignalIn(BaseModel):
    article_id: str | None = None
    kind: str
    value: dict | None = None


@router.post("/signals")
def record(sig: SignalIn):
    if sig.kind not in _ALLOWED:
        return {"ok": False, "error": f"unknown signal kind: {sig.kind}"}
    with connect() as conn:
        q.add_signal(conn, article_id=sig.article_id, kind=sig.kind, value=sig.value)
    return {"ok": True}


@router.get("/signals/verdict")
def verdict(article_id: str):
    """Latest explicit thumbs verdict for an article (survives PWA remounts)."""
    with connect() as conn:
        row = conn.execute(
            """SELECT kind FROM signals
               WHERE article_id = ? AND kind IN ('liked', 'disliked')
               ORDER BY id DESC LIMIT 1""",
            (article_id,),
        ).fetchone()
    return {"verdict": row["kind"] if row else None}


@router.get("/signals/summary")
def summary(days: int = 7):
    """Debug aid — see what's been captured."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT kind, COUNT(*) AS n
               FROM signals WHERE ts >= datetime('now', ?)
               GROUP BY kind ORDER BY n DESC""",
            (f"-{days} day",),
        ).fetchall()
    return {"by_kind": [dict(r) for r in rows]}
