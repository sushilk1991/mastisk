"""Signal capture — opens, time-read, pins, deletes, asks, skips.

M1 only captures. M2's Reflection agent reads from here.
"""
from __future__ import annotations

import json

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


class ReasonIn(BaseModel):
    article_id: str
    reason: str


@router.post("/signals/disliked-reason")
def disliked_reason(body: ReasonIn):
    """Attach a reason to the article's most recent 'disliked' signal.

    The thumbs-down is recorded on click (so a vote counts even if the user
    navigates away); the optional reason arrives later from the inline box.
    Patching the latest row keeps one signal per click — no double-count into
    the distiller's threshold."""
    reason = body.reason.strip()[:200]
    if not reason:
        return {"ok": False, "error": "empty reason"}
    with connect() as conn:
        row = conn.execute(
            """SELECT id FROM signals
               WHERE article_id = ? AND kind = 'disliked'
               ORDER BY id DESC LIMIT 1""",
            (body.article_id,),
        ).fetchone()
        if row is None:
            return {"ok": False, "error": "no disliked signal to annotate"}
        conn.execute(
            "UPDATE signals SET value_json = ? WHERE id = ?",
            (json.dumps({"reason": reason}), row["id"]),
        )
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
