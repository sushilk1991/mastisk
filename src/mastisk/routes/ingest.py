"""Phase 16 document and media ingestion routes."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile, status
from pydantic import BaseModel, Field, field_validator
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mastisk.agents.base import enqueue
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.ingest.converters import document_converter_available
from mastisk.ingest.journal_ocr import VisionUnavailable, extract_journal_photo_text
from mastisk.ingest.pipeline import (
    ALLOWED_DOCUMENT_EXTS,
    AUDIO_EXTS,
    extension_for_filename,
    safe_display_name,
    store_temp_audio_file,
    store_vault_source_file,
)
from mastisk.integrations import whisper
from mastisk.journal import JournalFrontmatterError, append_log
from mastisk.paths import raw_dir, vault_dir
from mastisk.routes.attachments import upload_attachment
from mastisk.routes.capture import require_capture_token
from mastisk.settings import get_settings

router = APIRouter(prefix="/api/ingest", tags=["ingest"])
capture_router = APIRouter(prefix="/api/capture", tags=["capture"])

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_DOCUMENT_INSTALL_HINT = "Document ingest needs MarkItDown. Install with: mastisk[ingest]"
_WHISPER_INSTALL_HINT = "Audio transcription needs mlx-whisper. Install with: mastisk[audio]"


class CaptureBearerAuthMiddleware:
    """Reject tunnel-exposed capture requests before request body parsing."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] == "http"
            and _is_capture_scope(str(scope.get("path") or ""))
            and scope.get("method") == "POST"
        ):
            authorization = Headers(scope=scope).get("authorization")
            try:
                require_capture_token(authorization)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _is_capture_scope(path: str) -> bool:
    return path == "/api/capture" or path.startswith("/api/capture/")


_WEB_CLIP_KINDS = {"web", "blog", "youtube", "twitter", "paper"}
_WEB_CLIP_MAX_CHARS = 400_000


class WebClipRequest(BaseModel):
    """A page (or selection) clipped by the browser extension."""

    url: str = Field(min_length=1, max_length=2000)
    title: str = Field(default="", max_length=500)
    content: str = Field(default="", max_length=_WEB_CLIP_MAX_CHARS)
    selection: str | None = Field(default=None, max_length=_WEB_CLIP_MAX_CHARS)
    author: str | None = Field(default=None, max_length=200)
    hero_image_url: str | None = Field(default=None, max_length=2000)
    kind: str = "web"

    @field_validator("url")
    @classmethod
    def _url_scheme(cls, v: str) -> str:
        v = v.strip()
        if not v.startswith(("http://", "https://")):
            raise ValueError("url must be http(s)")
        return v

    @field_validator("kind")
    @classmethod
    def _kind_allowed(cls, v: str) -> str:
        if v not in _WEB_CLIP_KINDS:
            raise ValueError(f"kind must be one of {sorted(_WEB_CLIP_KINDS)}")
        return v

    @field_validator("hero_image_url")
    @classmethod
    def _hero_scheme(cls, v: str | None) -> str | None:
        # The hero lands in an <img src> — page-supplied values must be http(s).
        if v is not None and not v.strip().startswith(("http://", "https://")):
            return None
        return v


@router.post("/web", status_code=status.HTTP_202_ACCEPTED)
async def ingest_web_clip(req: WebClipRequest) -> dict:
    """Index a clipped web page: store as a source and queue the compiler.

    The compiler turns the raw text into a wiki article (summary, sections,
    open questions, wiki-links) exactly like RSS-scouted sources.
    """
    body = (req.selection or "").strip() or req.content.strip()
    if not body:
        raise HTTPException(status_code=422, detail="content or selection required")
    # Selections get their own source id so clipping a passage after (or
    # before) saving the full page never collides on the URL-derived id.
    seed = req.url if not (req.selection or "").strip() else f"{req.url}\n{req.selection}"
    src_id = hashlib.sha256(seed.encode()).hexdigest()[:16]

    with connect() as conn:
        # The URL may already be indexed under a different id (e.g. the RSS
        # scout clipped it first) — reuse that row instead of orphaning a job.
        existing = conn.execute(
            "SELECT id FROM sources WHERE id=? OR url=?", (src_id, req.url)
        ).fetchone() if seed == req.url else conn.execute(
            "SELECT id FROM sources WHERE id=?", (src_id,)
        ).fetchone()
        if existing is not None:
            existing_id = str(existing["id"])
            state = _web_clip_status(conn, existing_id)
            # Self-heal: a source with no compile job (e.g. a crash between
            # insert and enqueue) would stay pending forever — re-enqueue.
            if state["status"] == "pending":
                job_id = enqueue("compiler", "compile", {"source_id": existing_id})
                return JSONResponse(
                    {"source_id": existing_id, "job_id": job_id, "status": "queued"},
                    status_code=200,
                )
            return JSONResponse(state, status_code=200)

        title = req.title.strip() or req.url
        raw_path = raw_dir() / f"{src_id}.txt"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(f"# {title}\n\n{req.url}\n\n{body}")
        cursor = conn.execute(
            """INSERT INTO sources
                 (id, kind, url, title, raw_path, author, hero_image_url)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(url) DO NOTHING""",
            (
                src_id,
                req.kind,
                # Keep the URL unique per source row: selection clips carry a
                # fragment so the page URL stays free for a full-page clip.
                req.url if seed == req.url else f"{req.url}#mastisk-clip-{src_id}",
                title,
                str(raw_path),
                req.author,
                req.hero_image_url,
            ),
        )
        if cursor.rowcount == 0:
            # Lost a URL race to a concurrent insert — report the winner's
            # status instead of enqueueing a job for a row that doesn't exist.
            winner = conn.execute(
                "SELECT id FROM sources WHERE url=?", (req.url,)
            ).fetchone()
            winner_id = str(winner["id"]) if winner else src_id
            return JSONResponse(_web_clip_status(conn, winner_id), status_code=200)
        q.append_feed(
            conn,
            agent="ingest",
            verb="clipped",
            obj=title[:120],
            kind="extension",
            touched_pages=1,
            payload={"source_id": src_id, "url": req.url},
        )
    job_id = enqueue("compiler", "compile", {"source_id": src_id})
    return {"source_id": src_id, "job_id": job_id, "status": "queued"}


@router.get("/web/{source_id}")
def get_web_clip_status(source_id: str) -> dict:
    with connect() as conn:
        source = conn.execute(
            "SELECT id FROM sources WHERE id=?", (source_id,)
        ).fetchone()
        if source is None:
            raise HTTPException(status_code=404, detail="source not found")
        return _web_clip_status(conn, source_id)


def _web_clip_status(conn, source_id: str) -> dict:
    article = conn.execute(
        """SELECT a.id, a.slug, a.title, a.summary
           FROM article_sources a_s JOIN articles a ON a.id = a_s.article_id
           WHERE a_s.source_id = ?
           LIMIT 1""",
        (source_id,),
    ).fetchone()
    if article is not None:
        return {
            "source_id": source_id,
            "status": "done",
            "article": dict(article),
        }
    job = conn.execute(
        """SELECT id, status, error FROM jobs
           WHERE agent='compiler' AND kind='compile'
             AND json_valid(payload_json)
             AND json_extract(payload_json, '$.source_id') = ?
           ORDER BY id DESC LIMIT 1""",
        (source_id,),
    ).fetchone()
    if job is None:
        return {"source_id": source_id, "status": "pending", "article": None}
    status_name = str(job["status"])
    return {
        "source_id": source_id,
        "status": status_name if status_name in {"queued", "running", "failed", "done"} else "pending",
        "error": job["error"],
        "article": None,
    }


@router.post("/document", status_code=status.HTTP_202_ACCEPTED)
async def ingest_document(file: Annotated[UploadFile, File(...)]) -> dict:
    if not document_converter_available():
        raise HTTPException(status_code=503, detail=_DOCUMENT_INSTALL_HINT)
    ext = extension_for_filename(file.filename)
    if ext not in ALLOWED_DOCUMENT_EXTS:
        raise HTTPException(status_code=415, detail="unsupported document type")
    raw = await _read_capped(file, kind="document")
    source_path, rel_path, digest = store_vault_source_file(raw, file.filename or f"source.{ext}")
    existing = _existing_document_job_for_sha256(digest)
    if existing is not None:
        existing_status = str(existing["status"])
        return JSONResponse(
            {
                "queued": existing_status != "done",
                "job_id": existing["job_id"],
                "status": existing_status,
                "source_path": existing["source_path"],
                "result": existing["result"],
            },
            status_code=200 if existing_status == "done" else status.HTTP_202_ACCEPTED,
        )
    job_id = enqueue(
        "ingest",
        "document",
        {
            "source_path": str(source_path),
            "source_rel_path": rel_path,
            "filename": safe_display_name(file.filename, f"{digest[:12]}.{ext}"),
            "content_type": file.content_type,
            "sha256": digest,
        },
    )
    return {
        "queued": True,
        "job_id": job_id,
        "status": "queued",
        "source_path": rel_path,
    }


def _existing_document_job_for_sha256(digest: str) -> dict | None:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, status, payload_json
               FROM jobs
               WHERE agent = 'ingest'
                 AND kind = 'document'
                 AND status IN ('queued', 'running', 'done')
                 AND json_valid(payload_json)
                 AND json_extract(payload_json, '$.sha256') = ?
               ORDER BY CASE status
                          WHEN 'done' THEN 0
                          WHEN 'running' THEN 1
                          ELSE 2
                        END,
                        id ASC""",
            (digest,),
        ).fetchall()
    for row in rows:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            continue
        result = payload.get("result")
        if row["status"] == "done" and not isinstance(result, dict):
            continue
        return {
            "job_id": row["id"],
            "status": row["status"],
            "source_path": payload.get("source_rel_path"),
            "result": result if isinstance(result, dict) else None,
        }
    return None


@router.get("/jobs/{job_id}")
def get_ingest_job(job_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT id, agent, kind, status, attempts, error,
                      created_at, started_at, finished_at, payload_json
               FROM jobs
               WHERE id = ? AND agent = 'ingest'""",
            (job_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="job not found")
    payload = {}
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except json.JSONDecodeError:
        payload = {}
    job = dict(row)
    job.pop("payload_json", None)
    job["result"] = payload.get("result")
    job["detail"] = {
        "title": payload.get("filename"),
        "source_path": payload.get("source_rel_path"),
    }
    return {"job": job}


@router.post("/journal-photo", status_code=201)
async def ingest_journal_photo(
    photo: Annotated[UploadFile, File(...)],
    date_value: Annotated[str | None, Form(alias="date")] = None,
) -> dict:
    day = _validate_day(date_value) if date_value else _today()
    uploaded = await upload_attachment(photo)
    image_path = vault_dir() / uploaded["path"]
    now = _now()
    try:
        extracted = (await extract_journal_photo_text(image_path)).strip()
    except VisionUnavailable as exc:
        return _journal_photo_needs_triage(
            day,
            uploaded,
            now,
            reason=str(exc),
            line_reason="vision path unavailable",
            ocr_status="unavailable",
            status_code=501,
        )
    if not extracted:
        return _journal_photo_needs_triage(
            day,
            uploaded,
            now,
            reason="OCR returned no text",
            ocr_status="empty",
            status_code=422,
        )
    try:
        result = append_log(
            day,
            f"Handwritten OCR: {extracted} {uploaded['markdown']}",
            now,
            source="handwritten",
        )
    except JournalFrontmatterError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _emit_feed("ocr", uploaded["path"], "journal-photo", {"journal": result})
    return {
        "status": "done",
        "ocr_status": "done",
        "text": extracted,
        "attachment": uploaded,
        "journal": result,
    }


def _journal_photo_needs_triage(
    day: str,
    uploaded: dict,
    now: datetime,
    *,
    reason: str,
    line_reason: str | None = None,
    ocr_status: str,
    status_code: int,
) -> dict:
    journal_reason = line_reason or reason
    line = f"OCR pending: {journal_reason.rstrip('.')}. {uploaded['markdown']} #needs-triage"
    try:
        result = append_log(day, line, now, source="handwritten")
    except JournalFrontmatterError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _emit_feed(
        "needs_triage",
        uploaded["path"],
        "journal-photo",
        {"reason": reason, "ocr_status": ocr_status},
    )
    return {
        "status": "needs_triage",
        "ocr_status": ocr_status,
        "status_code": status_code,
        "reason": reason,
        "attachment": uploaded,
        "journal": result,
    }


@capture_router.post("/audio", status_code=status.HTTP_202_ACCEPTED)
async def capture_audio(
    file: Annotated[UploadFile, File(...)],
    authorization: str | None = Header(default=None),
    ts: Annotated[str | None, Form()] = None,
) -> dict:
    return await queue_capture_audio(file=file, authorization=authorization, ts=ts)


async def queue_capture_audio(
    *,
    file: UploadFile,
    authorization: str | None,
    ts: str | None = None,
) -> dict:
    require_capture_token(authorization)
    if not whisper.is_available():
        raise HTTPException(status_code=503, detail=_WHISPER_INSTALL_HINT)
    ext = extension_for_filename(file.filename)
    if ext not in AUDIO_EXTS:
        raise HTTPException(status_code=415, detail="unsupported audio type")
    raw = await _read_capped(file, kind="audio")
    audio_path, digest = store_temp_audio_file(raw, file.filename or f"audio.{ext}")
    job_id = enqueue(
        "ingest",
        "capture_audio",
        {
            "audio_path": str(audio_path),
            "filename": safe_display_name(file.filename, f"{digest[:12]}.{ext}"),
            "content_type": file.content_type,
            "sha256": digest,
            "ts": ts,
        },
    )
    return {
        "queued": True,
        "job_id": job_id,
        "status": "queued",
    }


async def _read_capped(file: UploadFile, *, kind: str) -> bytes:
    max_bytes = max(0, int(get_settings().attachments.max_mb)) * 1024 * 1024
    raw = await file.read(max_bytes + 1)
    if len(raw) > max_bytes:
        raise HTTPException(status_code=413, detail=f"{kind} exceeds configured size cap")
    if not raw:
        raise HTTPException(status_code=422, detail=f"{kind} file is empty")
    return raw


def _validate_day(value: str) -> str:
    if not _DATE_RE.match(value):
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD") from exc
    if parsed > _now().date() + timedelta(days=1):
        raise HTTPException(status_code=422, detail="date cannot be later than tomorrow")
    return parsed.isoformat()


def _today() -> str:
    return _now().date().isoformat()


def _now() -> datetime:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(tz)


def _emit_feed(verb: str, obj: str, kind: str, payload: dict | None = None) -> None:
    with connect() as conn:
        q.append_feed(
            conn,
            agent="ingest",
            verb=verb,
            obj=obj[:120],
            kind=kind,
            touched_pages=1,
            payload=payload,
        )
