"""Wiki-suggestions queue — the stub gate's promote/dismiss surface."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mastisk import wiki_suggestions
from mastisk.db.queries import connect, list_wiki_suggestions

router = APIRouter(tags=["suggestions"])


@router.get("/suggestions")
def list_suggestions(status: str = "pending", limit: int = 50):
    if status not in ("pending", "promoted", "dismissed"):
        raise HTTPException(status_code=422, detail=f"unknown status {status!r}")
    with connect() as conn:
        rows = list_wiki_suggestions(conn, status=status, limit=max(1, min(limit, 200)))
    return {"suggestions": rows}


@router.post("/suggestions/{slug}/promote")
def promote(slug: str):
    row = wiki_suggestions.decide(slug, action="promote")
    if row is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    return row


@router.post("/suggestions/{slug}/dismiss")
def dismiss(slug: str):
    row = wiki_suggestions.decide(slug, action="dismiss")
    if row is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    return row


@router.post("/suggestions/{slug}/restore")
def restore(slug: str):
    row = wiki_suggestions.decide(slug, action="restore")
    if row is None:
        raise HTTPException(status_code=404, detail="suggestion not found")
    return row
