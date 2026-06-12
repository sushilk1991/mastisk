"""Phase 16 document and media ingestion worker."""
from __future__ import annotations

import json
from pathlib import Path

from mastisk.agents.base import Agent
from mastisk.ingest.pipeline import (
    cleanup_temp_audio_file,
    emit_ingest_feed,
    merge_job_payload,
    process_document_job,
    sweep_stale_capture_audio_files,
)
from mastisk.integrations import whisper
from mastisk.routes.capture import route_and_persist_capture


class IngestAgent(Agent):
    name = "ingest"
    tick_seconds = 15

    async def run_once(self) -> None:
        sweep_stale_capture_audio_files()
        await super().run_once()

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job.get("payload_json") or "{}")
        kind = job.get("kind") or ""
        try:
            if kind == "document":
                await process_document_job(int(job["id"]), payload)
                return
            if kind == "capture_audio":
                await self._handle_capture_audio(int(job["id"]), payload)
                return
            raise RuntimeError(f"ingest: unknown job kind {kind!r}")
        except Exception as exc:
            if kind == "capture_audio":
                cleanup_temp_audio_file(payload.get("audio_path"))
            emit_ingest_feed(
                "failed",
                str(payload.get("filename") or kind or "ingest"),
                kind or "ingest",
                {"error": str(exc)},
            )
            raise

    async def _handle_capture_audio(self, job_id: int, payload: dict) -> None:
        if isinstance(payload.get("result"), dict):
            cleanup_temp_audio_file(payload.get("audio_path"))
            return
        audio_path = Path(str(payload.get("audio_path") or ""))
        if not audio_path.exists():
            raise RuntimeError(f"audio file missing: {audio_path}")
        result = await whisper.transcribe(audio_path)
        transcript = result.text.strip()
        if not transcript:
            raise RuntimeError("whisper returned an empty transcript")
        filed = await route_and_persist_capture(
            transcript,
            source="phone",
            ts=payload.get("ts"),
        )
        job_result = {"transcript": transcript, "capture": filed}
        merge_job_payload(job_id, {"result": job_result})
        try:
            emit_ingest_feed(
                "captured",
                str(filed.get("destination") or payload.get("filename") or "audio"),
                "audio",
                {"job_id": job_id, "capture": filed},
            )
        finally:
            cleanup_temp_audio_file(audio_path)
