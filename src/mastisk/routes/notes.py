"""Notes API — capture, list, detail, delete."""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
from datetime import datetime
from html import unescape
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field, field_validator
from slugify import slugify

from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import notes_inbox_dir, vault_dir

router = APIRouter(prefix="/api/notes", tags=["notes"])


class CaptureContext(BaseModel):
    """Origin metadata for notes captured against an article (e.g. Open Questions).

    All fields other than ``article_id`` are optional so a minimal "Add thought
    on this article" path works without a section/question, but the full shape
    is used by the Open Questions flow so the Notetaker sees the originating
    question during classification.
    """
    article_id: str
    section_heading: str | None = None
    question_html: str | None = None


class TranscriptAnchor(BaseModel):
    """Anchor a note to a moment inside a podcast/youtube transcript.

    The PodcastView's "+" button opens NoteCaptureModal pre-filled with this
    pointing at the segment the user clicked, so on a subsequent load the
    note shows up inline beneath that segment.
    """
    source_id: str
    segment_idx: int
    start_sec: float


class CaptureRequest(BaseModel):
    text: str = Field(min_length=1)
    source: Literal["pwa", "cli"] = "pwa"
    context: CaptureContext | None = None
    transcript_anchor: TranscriptAnchor | None = None

    @field_validator("text")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must be non-blank")
        return v


def _build_body_with_context(
    text: str, ctx: CaptureContext, article_title: str | None
) -> str:
    """Prepend a structured preamble so the Notetaker sees context during classification.

    The preamble uses markdown blockquote lines (``> ...``) so it reads naturally
    when the raw note file is viewed. ``question_html`` is stripped of tags +
    HTML-unescaped so entities like ``&amp;`` don't leak into the stored body.
    """
    lines = [f"> From article: {article_title or ctx.article_id}"]
    if ctx.section_heading:
        lines.append(f"> Section: {ctx.section_heading}")
    if ctx.question_html:
        bare = re.sub(r"<[^>]+>", "", ctx.question_html)
        bare = unescape(bare).strip()
        if bare:
            lines.append(f"> Question: {bare}")
    preamble = "\n".join(lines)
    return f"{preamble}\n\n{text}"


def derive_slug(body: str, ts: datetime) -> str:
    """`<HHMMSS>-<slugified first 60 chars>`. See spec §5."""
    first_line = body.strip().splitlines()[0] if body.strip() else "note"
    slug_part = slugify(first_line[:60])[:40] or "note"
    return f"{ts.strftime('%H%M%S')}-{slug_part}"


def atomic_write(target: Path, content: str) -> None:
    """Write `content` to `target` via tempfile + rename. Avoids half-synced files on iCloud."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.rename(tmp_path, target)
    except Exception:
        Path(tmp_path).unlink(missing_ok=True)
        raise


def persist_note_capture(
    *,
    body: str,
    source: str,
    slug_text: str | None = None,
    ts: datetime | None = None,
) -> dict:
    """Write a captured note to the inbox and index it. Returns the DB row dict."""
    ts = ts or datetime.now().astimezone()
    slug = derive_slug(slug_text if slug_text is not None else body, ts)
    target = notes_inbox_dir() / f"{slug}.md"
    atomic_write(target, body)
    rel_path = str(target.relative_to(vault_dir()))
    with connect() as conn:
        note_id = q.insert_note(
            conn,
            slug=slug,
            path=rel_path,
            body=body,
            source=source,
            created_at=ts,
        )
        row = q.get_note(conn, note_id)
    # If insert_note hit a slug collision, the DB's final path has a `-N` suffix.
    # The file was written with the original name; rename it to match.
    actual_path = vault_dir() / row["path"]
    if actual_path != target:
        target.rename(actual_path)
    return dict(row)


@router.post("", status_code=201)
async def capture_note(req: CaptureRequest) -> dict:
    # If context was provided, validate the article exists *before* writing
    # anything to disk — a 404 here means nothing to clean up.
    article_title: str | None = None
    if req.context is not None:
        with connect() as conn:
            art = conn.execute(
                "SELECT id, title FROM articles WHERE id = ?",
                (req.context.article_id,),
            ).fetchone()
        if art is None:
            raise HTTPException(status_code=404, detail="article not found")
        article_title = art["title"]
        body_for_file = _build_body_with_context(req.text, req.context, article_title)
    else:
        body_for_file = req.text

    row = persist_note_capture(
        body=body_for_file,
        source=req.source,
        slug_text=req.text,
    )
    note_id = row["id"]

    # Direct user-action link (rank=0). Predates the Notetaker's classifier-
    # driven linking — the user explicitly hit "Add thoughts" on this article.
    if req.context is not None:
        with connect() as conn:
            q.insert_note_link(
                conn,
                note_id=note_id,
                article_id=req.context.article_id,
                rank=0,
            )

    # Persist the transcript anchor so the PodcastView can render this note
    # inline beneath its segment on next load. JSON-encoded for forward
    # compat — additions like end_sec/highlight_text won't need a migration.
    if req.transcript_anchor is not None:
        import json as _json
        with connect() as conn:
            conn.execute(
                "UPDATE notes SET transcript_anchor_json = ? WHERE id = ?",
                (_json.dumps(req.transcript_anchor.model_dump()), note_id),
            )

    return {
        "id": note_id,
        "slug": row["slug"],
        "path": row["path"],
        "created_at": row["created_at"],
        "linked_article_id": req.context.article_id if req.context else None,
    }


@router.get("")
async def list_notes_endpoint(
    limit: int = 50,
    before: int | None = None,
    classification: str | None = None,
) -> list[dict]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    with connect() as conn:
        rows = q.list_notes(conn, limit=limit, before_id=before, classification=classification)
    return [_note_summary(r) for r in rows]


@router.get("/{note_id}/file", response_class=PlainTextResponse)
async def get_note_file_endpoint(note_id: int) -> PlainTextResponse:
    with connect() as conn:
        row = q.get_note(conn, note_id)
    if row is None or row["deleted_at"] is not None:
        raise HTTPException(status_code=404, detail="note not found")
    file_path = vault_dir() / row["path"]
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="note file missing")
    return PlainTextResponse(
        file_path.read_text(encoding="utf-8"), media_type="text/markdown"
    )


@router.get("/{note_id}")
async def get_note_endpoint(note_id: int) -> dict:
    with connect() as conn:
        row = q.get_note(conn, note_id)
        if row is None:
            raise HTTPException(status_code=404, detail="note not found")
        return _note_detail(row, conn)


@router.delete("/{note_id}", status_code=204)
async def delete_note_endpoint(note_id: int) -> None:
    with connect() as conn:
        row = q.get_note(conn, note_id)
        if row is None or row["deleted_at"] is not None:
            raise HTTPException(status_code=404, detail="note not found")
        q.soft_delete_note(conn, note_id)
    file_path = vault_dir() / row["path"]
    try:
        file_path.unlink()
    except FileNotFoundError:
        pass


@router.post("/{note_id}/escalate", status_code=202)
async def manual_escalate_endpoint(note_id: int) -> dict:
    """Manually trigger the Escalator on a note. Bypasses the auto-rule.

    Enqueues an ``evaluate`` job with ``trigger='manual'``; the Escalator
    picks it up on its next tick (within 60s). The note's state flips to
    ``pending`` only when the handler starts — callers should poll the detail
    endpoint for the final outcome.
    """
    with connect() as conn:
        row = q.get_note(conn, note_id)
    if row is None or row["deleted_at"] is not None:
        raise HTTPException(status_code=404, detail="note not found")
    from mastisk.agents.base import enqueue
    enqueue("escalator", "evaluate", {"note_id": note_id, "trigger": "manual"})
    return {"note_id": note_id, "escalation_state": "pending"}


def _note_summary(row: dict) -> dict:
    """Compact view for list endpoints — omits body + escalation plumbing."""
    import json
    return {
        "id": row["id"],
        "slug": row["slug"],
        "path": row["path"],
        "source": row["source"],
        "created_at": row["created_at"],
        "classified_at": row["classified_at"],
        "classification": row["classification"],
        "summary": row["summary"],
        "tags": json.loads(row["tags_json"]) if row["tags_json"] else [],
        "escalation_state": row["escalation_state"],
    }


def _note_detail(row: dict, conn: sqlite3.Connection | None = None) -> dict:
    """Full view — includes body, escalation fields, and related articles.

    ``conn`` is optional so the function stays callable in isolation; when
    provided, we join ``note_links`` → ``articles`` to surface the articles
    the Notetaker (or a direct user-action link) associated with this note,
    ordered by ``rank`` (0 = direct user-action, 1+ = classifier-driven).
    """
    related: list[dict] = []
    if conn is not None:
        rows = conn.execute(
            """SELECT nl.article_id, a.title, nl.rank
               FROM note_links nl
               JOIN articles a ON a.id = nl.article_id
               WHERE nl.note_id = ?
               ORDER BY nl.rank ASC""",
            (row["id"],),
        ).fetchall()
        related = [
            {"article_id": r["article_id"], "title": r["title"], "rank": r["rank"]}
            for r in rows
        ]
    return {
        **_note_summary(row),
        "body": row["body"],
        "body_sha256": row["body_sha256"],
        "confidence": row["confidence"],
        "escalation_trigger": row["escalation_trigger"],
        "escalation_article_id": row["escalation_article_id"],
        "escalation_retry_count": row["escalation_retry_count"],
        "deleted_at": row["deleted_at"],
        "related_articles": related,
    }


# Separate router for ``/api/articles/{id}/notes`` — lives in this module
# because it's logically part of the notes API, even though the URL prefix
# groups it with articles. Same tag as the notes router for OpenAPI grouping.
articles_notes_router = APIRouter(prefix="/api/articles", tags=["notes"])


@articles_notes_router.get("/{article_id}/notes")
async def list_notes_for_article_endpoint(
    article_id: str, limit: int = 50
) -> list[dict]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    with connect() as conn:
        rows = q.list_notes_for_article(conn, article_id=article_id, limit=limit)
    return [_note_summary(r) for r in rows]
