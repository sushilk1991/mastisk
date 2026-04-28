from __future__ import annotations

from fastapi import APIRouter

from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(tags=["search"])


@router.get("/search")
def search(q_param: str = "", limit: int = 20):
    """Unified keyword search across articles, notes, and blog posts.

    Returns ``{results: [{kind, id, title, subtitle, snippet, score}, ...]}``
    ranked by FTS5 BM25 score with a small per-kind tilt favouring articles
    (canonical wiki content). Powers the ⌘K command palette.

    Note: ``ask.py`` deliberately calls ``search_articles`` directly — the
    Ask flow grounds answers in articles only and uses different (OR-join,
    stopwords-stripped) FTS semantics tuned for retrieval, not narrowing.
    """
    # FastAPI doesn't love "q" as a param name in some tooling; accept the
    # ``q_param`` alias used by the existing frontend client.
    with connect() as conn:
        return {"results": q.search_all(conn, q_param, limit=limit)}


@router.get("/search/{q_param}")
def search_path(q_param: str, limit: int = 20):
    with connect() as conn:
        return {"results": q.search_all(conn, q_param, limit=limit)}
