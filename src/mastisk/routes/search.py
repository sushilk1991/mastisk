from __future__ import annotations

from fastapi import APIRouter

from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(tags=["search"])


@router.get("/search")
def search(q_param: str = "", limit: int = 20):
    """Unified keyword search across wiki, notes, blog, and personal-OS mirrors.

    Returns ``{results: [{kind, id, title, excerpt, link_target, ...}, ...]}``.
    Existing FTS tables keep their BM25 ranking; typed mirrors use the same
    LIKE-over-mirror fallback that powers Ask context for non-FTS data.
    """
    # FastAPI doesn't love "q" as a param name in some tooling; accept the
    # ``q_param`` alias used by the existing frontend client.
    with connect() as conn:
        return {"results": q.search_all(conn, q_param, limit=limit)}


@router.get("/search/{q_param}")
def search_path(q_param: str, limit: int = 20):
    with connect() as conn:
        return {"results": q.search_all(conn, q_param, limit=limit)}
