"""Phase 16 document and media ingestion routes."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import APIRouter, File, Form, Header, HTTPException, UploadFile, status
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
from mastisk.paths import vault_dir
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


@router.post("/document", status_code=status.HTTP_202_ACCEPTED)
async def ingest_document(file: Annotated[UploadFile, File(...)]) -> dict:
    if not document_converter_available():
        raise HTTPException(status_code=503, detail=_DOCUMENT_INSTALL_HINT)
    ext = extension_for_filename(file.filename)
    if ext not in ALLOWED_DOCUMENT_EXTS:
        raise HTTPException(status_code=415, detail="unsupported document type")
    raw = await _read_capped(file, kind="document")
    source_path, rel_path, digest = store_vault_source_file(raw, file.filename or f"source.{ext}")
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
        line = (
            "OCR pending: vision path unavailable. "
            f"{uploaded['markdown']} #needs-triage"
        )
        try:
            result = append_log(day, line, now, source="handwritten")
        except JournalFrontmatterError as fm_exc:
            raise HTTPException(status_code=409, detail=str(fm_exc)) from fm_exc
        _emit_feed("needs_triage", uploaded["path"], "journal-photo", {"reason": str(exc)})
        return {
            "status": "needs_triage",
            "ocr_status": "unavailable",
            "status_code": 501,
            "reason": str(exc),
            "attachment": uploaded,
            "journal": result,
        }
    if not extracted:
        raise HTTPException(status_code=422, detail="OCR returned no text")
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
