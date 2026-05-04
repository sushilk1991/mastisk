"""Podcasts API — list + detail for articles whose source is a podcast/youtube.

Composes existing pieces: an article (Compiler-written wiki page) gets joined
with its single audio source row, the verbatim transcript text from
sources.raw_path, the optional whisper-derived segments, and any notes the
user has anchored to segments of this source. Phase 1 surfaces the article
and the raw transcript; Phase 2 surfaces segments + anchored notes once the
backfill has run.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from mastisk.db import queries as q
from mastisk.db.queries import connect

router = APIRouter(tags=["podcasts"])


@router.get("/podcasts")
def list_podcasts(limit: int = 100):
    """List articles compiled from podcast/youtube sources, newest first."""
    with connect() as conn:
        return {"items": q.list_podcast_articles(conn, limit=limit)}


@router.get("/podcasts/{article_id}")
def get_podcast(article_id: str):
    """Joined view: article + source metadata + transcript + segments + notes.

    404s when the article exists but isn't a podcast/youtube article — keeps
    the route's contract honest so the frontend can decide whether to redirect
    to the generic ArticleView instead of rendering an empty PodcastView.
    """
    with connect() as conn:
        view = q.get_podcast_view(conn, article_id)
        if not view:
            raise HTTPException(404, "podcast view not found")
        return view
