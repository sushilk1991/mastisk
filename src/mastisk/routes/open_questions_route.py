"""Open questions — every article_section with kind='open', grouped by article."""
from __future__ import annotations

from fastapi import APIRouter, Query

from mastisk.db.queries import connect

router = APIRouter(tags=["open_questions"])


@router.get("/open-questions")
def open_questions(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """One page of open questions, newest article first.

    The full set is tens of megabytes on a large wiki, so callers page with
    ``limit``/``offset``; ``total`` and ``articles`` give the overall counts.
    """
    with connect() as conn:
        totals = conn.execute(
            """SELECT COUNT(*) AS total, COUNT(DISTINCT article_id) AS articles
               FROM article_sections WHERE kind = 'open'"""
        ).fetchone()
        rows = conn.execute(
            """SELECT articles.id        AS article_id,
                      articles.title     AS article_title,
                      articles.kind      AS article_kind,
                      article_sections.heading AS heading,
                      article_sections.body    AS body,
                      articles.updated_at      AS updated_at
               FROM article_sections
               JOIN articles ON articles.id = article_sections.article_id
               WHERE article_sections.kind = 'open'
               ORDER BY articles.updated_at DESC, articles.id, article_sections.idx
               LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return {
            "questions": [dict(r) for r in rows],
            "total": totals["total"],
            "articles": totals["articles"],
            "limit": limit,
            "offset": offset,
        }
