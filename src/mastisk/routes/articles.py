from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(tags=["articles"])


@router.get("/articles")
def list_articles(kind: str | None = None, limit: int = Query(200, ge=1, le=5000)):
    with connect() as conn:
        return {"articles": q.list_articles(conn, kind=kind, limit=limit)}


@router.get("/articles/{article_id}")
def get_article(article_id: str):
    with connect() as conn:
        art = q.get_article(conn, article_id)
        if not art:
            raise HTTPException(404, "article not found")
        return art


@router.get("/articles/{article_id}/preview")
def get_article_preview(article_id: str):
    """Lightweight preview for wiki-link hover cards.

    One SELECT against ``articles`` — id/kind/title/summary only. No sections,
    related, or sources. Returns ``exists: false`` for unknown ids rather than
    404-ing so the client doesn't have to distinguish network failures from
    "this slug isn't compiled yet" (the common state right now).
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT id, kind, title, summary FROM articles WHERE id = ?",
            (article_id,),
        ).fetchone()
        if not row:
            return {"id": article_id, "exists": False}
        return {
            "id": row["id"],
            "kind": row["kind"],
            "title": row["title"],
            "summary": row["summary"] or "",
            "exists": True,
        }


@router.get("/sidebar")
def sidebar():
    with connect() as conn:
        return {
            "vault":  q.vault_tree(conn),
            "pinned": q.pinned_list(conn),
            "user":   q.user_info(conn),
        }


def _require_article(conn, article_id: str) -> None:
    # pinned.article_id and signals.article_id are enforced foreign keys.
    if not conn.execute("SELECT 1 FROM articles WHERE id = ?", (article_id,)).fetchone():
        raise HTTPException(404, "article not found")


@router.post("/articles/{article_id}/pin")
def pin(article_id: str):
    with connect() as conn:
        _require_article(conn, article_id)
        q.pin_article(conn, article_id)
        q.add_signal(conn, article_id=article_id, kind="pinned")
    return {"ok": True}


@router.delete("/articles/{article_id}/pin")
def unpin(article_id: str):
    with connect() as conn:
        _require_article(conn, article_id)
        q.unpin_article(conn, article_id)
        q.add_signal(conn, article_id=article_id, kind="unpinned")
    return {"ok": True}
