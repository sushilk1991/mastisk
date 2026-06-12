from __future__ import annotations

import asyncio
import json
import os
import time
import types
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.app import create_app

    return TestClient(create_app())


@pytest.mark.asyncio
async def test_capture_audio_rejects_bad_token_before_reading_body(
    vault_tmp, data_tmp, db
):
    from mastisk.app import create_app
    from mastisk.settings import reload_settings

    (data_tmp / "config.toml").write_text("[capture]\nbearer_token = \"tok\"\n", encoding="utf-8")
    reload_settings()
    messages: list[dict] = []
    receive_called = False
    try:
        app = create_app()

        async def receive() -> dict:
            nonlocal receive_called
            receive_called = True
            raise AssertionError("unauthenticated capture request consumed body")

        async def send(message: dict) -> None:
            messages.append(message)

        await app(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/api/capture/audio",
                "raw_path": b"/api/capture/audio",
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"authorization", b"Bearer wrong"),
                    (b"content-type", b"multipart/form-data; boundary=x"),
                    (b"content-length", str(50 * 1024 * 1024).encode()),
                ],
                "client": ("127.0.0.1", 1),
                "server": ("testserver", 80),
                "root_path": "",
            },
            receive,
            send,
        )
    finally:
        (data_tmp / "config.toml").unlink(missing_ok=True)
        reload_settings()

    starts = [message for message in messages if message["type"] == "http.response.start"]
    assert starts[0]["status"] == 401
    assert receive_called is False


def test_document_converter_prefers_docling_when_importable(tmp_path, monkeypatch):
    from mastisk.ingest import converters

    class FakeDocument:
        def export_to_markdown(self):
            return "# docling"

    class FakeConverter:
        def convert(self, path):
            assert path.endswith("source.pdf")
            return types.SimpleNamespace(document=FakeDocument())

    def fake_import(name: str):
        if name == "docling.document_converter":
            return types.SimpleNamespace(DocumentConverter=FakeConverter)
        raise ImportError(name)

    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    monkeypatch.setattr(converters.importlib, "import_module", fake_import)

    result = converters.convert_document(source)

    assert result.provider == "docling"
    assert result.markdown == "# docling"


def test_document_converter_falls_back_to_markitdown(tmp_path, monkeypatch):
    from mastisk.ingest import converters

    class FakeMarkItDown:
        def convert(self, path):
            assert path.endswith("source.docx")
            return types.SimpleNamespace(text_content="# markitdown")

    def fake_import(name: str):
        if name == "docling.document_converter":
            raise ImportError(name)
        if name == "markitdown":
            return types.SimpleNamespace(MarkItDown=FakeMarkItDown)
        raise ImportError(name)

    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    monkeypatch.setattr(converters.importlib, "import_module", fake_import)

    result = converters.convert_document(source)

    assert result.provider == "markitdown"
    assert result.markdown == "# markitdown"


def test_document_converter_missing_is_loud(tmp_path, monkeypatch):
    from mastisk.ingest import converters

    monkeypatch.setattr(
        converters.importlib,
        "import_module",
        lambda name: (_ for _ in ()).throw(ImportError(name)),
    )

    with pytest.raises(converters.MissingDocumentConverter, match="mastisk\\[ingest\\]"):
        converters.convert_document(tmp_path / "source.pdf")


def test_document_ingest_queues_job_and_job_writes_inbox_note(
    vault_tmp, data_tmp, db, monkeypatch
):
    from mastisk.agents.ingest import IngestAgent
    from mastisk.ingest.converters import ConversionResult
    from mastisk.ingest.pipeline import SourceMetadata

    monkeypatch.setattr("mastisk.routes.ingest.document_converter_available", lambda: True)
    monkeypatch.setattr(
        "mastisk.ingest.pipeline.convert_document",
        lambda path: ConversionResult(markdown="# Converted\n\nBody", provider="markitdown"),
    )
    monkeypatch.setattr(
        "mastisk.ingest.pipeline.extract_source_metadata",
        AsyncMock(
            return_value=SourceMetadata(
                summary="A useful report.",
                tags=["reports"],
                entities=["Mastisk"],
                source_type="report",
            )
        ),
    )

    with _client(vault_tmp, data_tmp, db) as client:
        queued = client.post(
            "/api/ingest/document",
            files={"file": ("report.pdf", b"fake-pdf", "application/pdf")},
        )
        assert queued.status_code == 202, queued.text
        job_id = queued.json()["job_id"]

        asyncio.run(IngestAgent().run_once())
        job = client.get(f"/api/ingest/jobs/{job_id}")

    assert job.status_code == 200, job.text
    assert job.json()["job"]["status"] == "done"
    assert job.json()["job"]["result"]["provider"] == "markitdown"
    source_path = queued.json()["source_path"]
    assert source_path.startswith("sources/")
    assert (vault_tmp / source_path).read_bytes() == b"fake-pdf"

    note = db.execute("SELECT * FROM notes WHERE source = 'document'").fetchone()
    assert note is not None
    assert note["path"].startswith("_notes/inbox/")
    note_text = (vault_tmp / note["path"]).read_text(encoding="utf-8")
    from mastisk.agents.notetaker import strip_frontmatter

    assert strip_frontmatter(note_text) == note["body"]
    assert "source_type: report" in note_text
    assert "summary: A useful report." in note_text
    assert f"original: {source_path}" in note_text
    assert "# Converted" in note_text
    assert f"[vault/{source_path}]" in note["body"]
    assert note["classified_at"] is not None
    assert note["classification"] == "observation"
    assert note["summary"] == "A useful report."
    assert json.loads(note["tags_json"]) == ["reports"]
    assert note["escalation_state"] == "none"


def test_document_requeue_after_result_does_not_duplicate_companion_note(
    vault_tmp, data_tmp, db, monkeypatch
):
    from mastisk.agents.base import enqueue
    from mastisk.agents.ingest import IngestAgent
    from mastisk.ingest.converters import ConversionResult
    from mastisk.ingest.pipeline import (
        SourceMetadata,
        process_document_job,
        store_vault_source_file,
    )

    source_path, rel_path, digest = store_vault_source_file(b"pdf", "report.pdf")
    payload = {
        "source_path": str(source_path),
        "source_rel_path": rel_path,
        "filename": "report.pdf",
        "content_type": "application/pdf",
        "sha256": digest,
    }
    job_id = enqueue("ingest", "document", payload)
    IngestAgent()._mark_running(job_id)
    convert_calls = 0

    def fake_convert(path):
        nonlocal convert_calls
        convert_calls += 1
        return ConversionResult(markdown="# Converted\n\nBody", provider="markitdown")

    monkeypatch.setattr("mastisk.ingest.pipeline.convert_document", fake_convert)
    monkeypatch.setattr(
        "mastisk.ingest.pipeline.extract_source_metadata",
        AsyncMock(
            return_value=SourceMetadata(
                summary="A useful report.",
                tags=["reports"],
                entities=[],
                source_type="report",
            )
        ),
    )
    monkeypatch.setattr(
        "mastisk.ingest.pipeline.emit_ingest_feed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after result")),
    )

    with pytest.raises(RuntimeError, match="crash after result"):
        asyncio.run(process_document_job(job_id, payload))

    job_payload = json.loads(
        db.execute("SELECT payload_json FROM jobs WHERE id = ?", (job_id,)).fetchone()["payload_json"]
    )
    assert job_payload["result"]["note_id"]
    assert db.execute("SELECT COUNT(*) AS n FROM notes WHERE source='document'").fetchone()["n"] == 1

    db.execute("UPDATE jobs SET status='queued', started_at=NULL WHERE id = ?", (job_id,))
    monkeypatch.setattr("mastisk.ingest.pipeline.emit_ingest_feed", lambda *args, **kwargs: None)

    asyncio.run(IngestAgent().run_once())

    job = db.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert dict(job) == {"status": "done", "error": None}
    assert db.execute("SELECT COUNT(*) AS n FROM notes WHERE source='document'").fetchone()["n"] == 1
    assert convert_calls == 1


def test_document_companion_note_is_bounded_and_skips_notetaker(
    vault_tmp, data_tmp, db, monkeypatch
):
    from mastisk.agents.ingest import IngestAgent
    from mastisk.agents.notetaker import Notetaker, strip_frontmatter
    from mastisk.ingest.converters import ConversionResult
    from mastisk.ingest.pipeline import SourceMetadata

    long_markdown = "# Big PDF\n\n" + ("important detail " * 1000)
    monkeypatch.setattr("mastisk.routes.ingest.document_converter_available", lambda: True)
    monkeypatch.setattr(
        "mastisk.ingest.pipeline.convert_document",
        lambda path: ConversionResult(markdown=long_markdown, provider="markitdown"),
    )
    monkeypatch.setattr(
        "mastisk.ingest.pipeline.extract_source_metadata",
        AsyncMock(
            return_value=SourceMetadata(
                summary="A long source worth keeping.",
                tags=["long-source"],
                entities=["Mastisk"],
                source_type="paper",
            )
        ),
    )

    with _client(vault_tmp, data_tmp, db) as client:
        queued = client.post(
            "/api/ingest/document",
            files={"file": ("long.pdf", b"fake-pdf", "application/pdf")},
        )
        assert queued.status_code == 202, queued.text
        asyncio.run(IngestAgent().run_once())

    note = db.execute("SELECT * FROM notes WHERE source = 'document'").fetchone()
    assert note is not None
    note_text = (vault_tmp / note["path"]).read_text(encoding="utf-8")
    body_from_file = strip_frontmatter(note_text)
    assert body_from_file == note["body"]
    assert len(note["body"]) <= 5600
    assert "important detail" in note["body"]
    assert note["body"].count("important detail") < long_markdown.count("important detail")
    assert f"[vault/{queued.json()['source_path']}]" in note["body"]
    assert note["classification"] == "observation"
    assert note["classified_at"] is not None
    assert Notetaker()._enqueue_reclassify_ready() == 0
    assert db.execute("SELECT COUNT(*) AS n FROM jobs WHERE agent='notetaker'").fetchone()["n"] == 0


def test_document_ingest_enforces_type_size_and_converter_503(
    vault_tmp, data_tmp, db, monkeypatch
):
    from mastisk.settings import reload_settings

    with _client(vault_tmp, data_tmp, db) as client:
        monkeypatch.setattr("mastisk.routes.ingest.document_converter_available", lambda: False)
        missing = client.post(
            "/api/ingest/document",
            files={"file": ("report.pdf", b"pdf", "application/pdf")},
        )
        assert missing.status_code == 503, missing.text

        monkeypatch.setattr("mastisk.routes.ingest.document_converter_available", lambda: True)
        bad_type = client.post(
            "/api/ingest/document",
            files={"file": ("image.png", b"png", "image/png")},
        )
        assert bad_type.status_code == 415, bad_type.text

        (data_tmp / "config.toml").write_text("[attachments]\nmax_mb = 0\n", encoding="utf-8")
        reload_settings()
        try:
            too_large = client.post(
                "/api/ingest/document",
                files={"file": ("report.pdf", b"x", "application/pdf")},
            )
        finally:
            (data_tmp / "config.toml").unlink(missing_ok=True)
            reload_settings()
        assert too_large.status_code == 413, too_large.text


def test_capture_audio_auth_503_type_and_size_caps(vault_tmp, data_tmp, db, monkeypatch):
    from mastisk.settings import reload_settings

    with _client(vault_tmp, data_tmp, db) as client:
        unconfigured = client.post(
            "/api/capture/audio",
            files={"file": ("note.m4a", b"audio", "audio/mp4")},
        )
        assert unconfigured.status_code == 503, unconfigured.text

        (data_tmp / "config.toml").write_text("[capture]\nbearer_token = \"tok\"\n", encoding="utf-8")
        reload_settings()
        try:
            bad_token = client.post(
                "/api/capture/audio",
                headers={"Authorization": "Bearer wrong"},
                files={"file": ("note.m4a", b"audio", "audio/mp4")},
            )
            assert bad_token.status_code == 401, bad_token.text

            monkeypatch.setattr("mastisk.routes.ingest.whisper.is_available", lambda: False)
            unavailable = client.post(
                "/api/capture/audio",
                headers={"Authorization": "Bearer tok"},
                files={"file": ("note.m4a", b"audio", "audio/mp4")},
            )
            assert unavailable.status_code == 503, unavailable.text

            monkeypatch.setattr("mastisk.routes.ingest.whisper.is_available", lambda: True)
            bad_type = client.post(
                "/api/capture/audio",
                headers={"Authorization": "Bearer tok"},
                files={"file": ("note.txt", b"audio", "text/plain")},
            )
            assert bad_type.status_code == 415, bad_type.text

            (data_tmp / "config.toml").write_text(
                "[capture]\nbearer_token = \"tok\"\n[attachments]\nmax_mb = 0\n",
                encoding="utf-8",
            )
            reload_settings()
            too_large = client.post(
                "/api/capture/audio",
                headers={"Authorization": "Bearer tok"},
                files={"file": ("note.m4a", b"x", "audio/mp4")},
            )
            assert too_large.status_code == 413, too_large.text
        finally:
            (data_tmp / "config.toml").unlink(missing_ok=True)
            reload_settings()


def test_capture_audio_job_transcribes_and_routes_as_phone(vault_tmp, data_tmp, db, monkeypatch):
    from mastisk.agents.ingest import IngestAgent
    from mastisk.integrations.whisper import TranscriptResult
    from mastisk.settings import reload_settings

    (data_tmp / "config.toml").write_text("[capture]\nbearer_token = \"tok\"\n", encoding="utf-8")
    reload_settings()
    try:
        monkeypatch.setattr("mastisk.routes.ingest.whisper.is_available", lambda: True)
        monkeypatch.setattr(
            "mastisk.agents.ingest.whisper.transcribe",
            AsyncMock(return_value=TranscriptResult(text="remember to call Sam", segments=[])),
        )
        routed = AsyncMock(return_value={
            "id": 123,
            "type": "task",
            "destination": "journal/2026-06-12.md",
            "needs_triage": False,
        })
        monkeypatch.setattr("mastisk.agents.ingest.route_and_persist_capture", routed)

        with _client(vault_tmp, data_tmp, db) as client:
            queued = client.post(
                "/api/capture/audio",
                headers={"Authorization": "Bearer tok"},
                files={"file": ("note.m4a", b"audio", "audio/mp4")},
            )
            assert queued.status_code == 202, queued.text
            job_id = queued.json()["job_id"]

            asyncio.run(IngestAgent().run_once())
            job = client.get(f"/api/ingest/jobs/{job_id}").json()["job"]
    finally:
        (data_tmp / "config.toml").unlink(missing_ok=True)
        reload_settings()

    routed.assert_awaited_once_with("remember to call Sam", source="phone", ts=None)
    assert job["status"] == "done"
    assert job["result"]["capture"]["type"] == "task"


def test_capture_audio_requeue_after_result_does_not_duplicate_capture(
    vault_tmp, data_tmp, db, monkeypatch
):
    from mastisk.agents.base import enqueue
    from mastisk.agents.ingest import IngestAgent
    from mastisk.ingest.pipeline import store_temp_audio_file
    from mastisk.integrations.whisper import TranscriptResult

    audio_path, digest = store_temp_audio_file(b"audio", "note.m4a")
    payload = {
        "audio_path": str(audio_path),
        "filename": "note.m4a",
        "sha256": digest,
    }
    job_id = enqueue("ingest", "capture_audio", payload)
    agent = IngestAgent()
    agent._mark_running(job_id)

    monkeypatch.setattr(
        "mastisk.agents.ingest.whisper.transcribe",
        AsyncMock(return_value=TranscriptResult(text="remember Sam", segments=[])),
    )
    routed = AsyncMock(return_value={
        "id": 123,
        "type": "task",
        "destination": "journal/2026-06-12.md",
        "needs_triage": False,
    })
    monkeypatch.setattr("mastisk.agents.ingest.route_and_persist_capture", routed)
    monkeypatch.setattr(
        "mastisk.agents.ingest.emit_ingest_feed",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("crash after result")),
    )

    with pytest.raises(RuntimeError, match="crash after result"):
        asyncio.run(agent._handle_capture_audio(job_id, payload))

    job_payload = json.loads(
        db.execute("SELECT payload_json FROM jobs WHERE id = ?", (job_id,)).fetchone()["payload_json"]
    )
    assert job_payload["result"]["capture"]["type"] == "task"

    db.execute("UPDATE jobs SET status='queued', started_at=NULL WHERE id = ?", (job_id,))
    monkeypatch.setattr("mastisk.agents.ingest.emit_ingest_feed", lambda *args, **kwargs: None)

    asyncio.run(IngestAgent().run_once())

    job = db.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert dict(job) == {"status": "done", "error": None}
    assert routed.await_count == 1
    assert not audio_path.exists()


def test_capture_audio_requeue_after_temp_cleanup_uses_payload_result(
    vault_tmp, data_tmp, db, monkeypatch
):
    from mastisk.agents.base import enqueue
    from mastisk.agents.ingest import IngestAgent
    from mastisk.ingest.pipeline import store_temp_audio_file

    audio_path, digest = store_temp_audio_file(b"audio", "note.m4a")
    audio_path.unlink()
    job_id = enqueue(
        "ingest",
        "capture_audio",
        {
            "audio_path": str(audio_path),
            "filename": "note.m4a",
            "sha256": digest,
            "result": {
                "transcript": "remember Sam",
                "capture": {
                    "id": 123,
                    "type": "task",
                    "destination": "journal/2026-06-12.md",
                    "needs_triage": False,
                },
            },
        },
    )
    routed = AsyncMock()
    monkeypatch.setattr("mastisk.agents.ingest.route_and_persist_capture", routed)

    asyncio.run(IngestAgent().run_once())

    job = db.execute("SELECT status, error FROM jobs WHERE id = ?", (job_id,)).fetchone()
    assert dict(job) == {"status": "done", "error": None}
    routed.assert_not_awaited()


def test_capture_audio_failed_job_removes_temp_file(vault_tmp, data_tmp, db, monkeypatch):
    from mastisk.agents.base import enqueue
    from mastisk.agents.ingest import IngestAgent
    from mastisk.ingest.pipeline import store_temp_audio_file
    from mastisk.integrations.whisper import TranscriptResult

    audio_path, digest = store_temp_audio_file(b"audio", "note.m4a")
    enqueue(
        "ingest",
        "capture_audio",
        {
            "audio_path": str(audio_path),
            "filename": "note.m4a",
            "sha256": digest,
        },
    )
    monkeypatch.setattr(
        "mastisk.agents.ingest.whisper.transcribe",
        AsyncMock(return_value=TranscriptResult(text=" ", segments=[])),
    )

    asyncio.run(IngestAgent().run_once())

    assert not audio_path.exists()
    job = db.execute("SELECT status, error FROM jobs WHERE agent='ingest'").fetchone()
    assert job["status"] == "failed"
    assert "empty transcript" in job["error"]


def test_capture_audio_sweep_removes_only_stale_orphan_temp_files(
    vault_tmp, data_tmp, db
):
    from mastisk.agents.base import enqueue
    from mastisk.ingest.pipeline import store_temp_audio_file, sweep_stale_capture_audio_files

    stale_orphan, _ = store_temp_audio_file(b"old", "old.m4a")
    stale_active, digest = store_temp_audio_file(b"active", "active.m4a")
    recent_orphan, _ = store_temp_audio_file(b"recent", "recent.m4a")
    old_time = time.time() - 7200
    os.utime(stale_orphan, (old_time, old_time))
    os.utime(stale_active, (old_time, old_time))
    enqueue(
        "ingest",
        "capture_audio",
        {
            "audio_path": str(stale_active),
            "filename": "active.m4a",
            "sha256": digest,
        },
    )

    removed = sweep_stale_capture_audio_files(max_age_seconds=3600)

    assert removed == 1
    assert not stale_orphan.exists()
    assert stale_active.exists()
    assert recent_orphan.exists()


def test_capture_audio_duplicate_uploads_get_independent_temp_files(
    vault_tmp, data_tmp, db, monkeypatch
):
    from mastisk.agents.ingest import IngestAgent
    from mastisk.integrations.whisper import TranscriptResult
    from mastisk.settings import reload_settings

    (data_tmp / "config.toml").write_text(
        "[capture]\nbearer_token = \"tok\"\ndefault_timezone = \"Asia/Kolkata\"\n",
        encoding="utf-8",
    )
    reload_settings()
    try:
        monkeypatch.setattr("mastisk.routes.ingest.whisper.is_available", lambda: True)
        monkeypatch.setattr(
            "mastisk.agents.ingest.whisper.transcribe",
            AsyncMock(return_value=TranscriptResult(text="same transcript", segments=[])),
        )
        routed = AsyncMock(return_value={
            "id": 123,
            "type": "note",
            "destination": "_notes/inbox/same.md",
            "needs_triage": False,
        })
        monkeypatch.setattr("mastisk.agents.ingest.route_and_persist_capture", routed)

        with _client(vault_tmp, data_tmp, db) as client:
            first = client.post(
                "/api/capture/audio",
                headers={"Authorization": "Bearer tok"},
                files={"file": ("note.m4a", b"same-audio", "audio/mp4")},
            )
            second = client.post(
                "/api/capture/audio",
                headers={"Authorization": "Bearer tok"},
                files={"file": ("note.m4a", b"same-audio", "audio/mp4")},
            )
            assert first.status_code == 202, first.text
            assert second.status_code == 202, second.text

            rows = db.execute(
                "SELECT id, payload_json FROM jobs WHERE agent = 'ingest' ORDER BY id"
            ).fetchall()
            payloads = [json.loads(row["payload_json"]) for row in rows]
            paths = [payload["audio_path"] for payload in payloads]
            assert len(paths) == 2
            assert paths[0] != paths[1]

            asyncio.run(IngestAgent().run_once())
            asyncio.run(IngestAgent().run_once())

        statuses = [
            row["status"]
            for row in db.execute("SELECT status FROM jobs WHERE agent = 'ingest' ORDER BY id")
        ]
    finally:
        (data_tmp / "config.toml").unlink(missing_ok=True)
        reload_settings()

    assert statuses == ["done", "done"]
    assert routed.await_count == 2


def test_capture_audio_inbox_fallback_preserves_client_timestamp(
    vault_tmp, data_tmp, db, monkeypatch
):
    from mastisk.agents.ingest import IngestAgent
    from mastisk.capture.router import Capture
    from mastisk.integrations.whisper import TranscriptResult
    from mastisk.settings import reload_settings

    (data_tmp / "config.toml").write_text(
        "[capture]\nbearer_token = \"tok\"\ndefault_timezone = \"Asia/Kolkata\"\n",
        encoding="utf-8",
    )
    reload_settings()
    try:
        monkeypatch.setattr("mastisk.routes.ingest.whisper.is_available", lambda: True)
        monkeypatch.setattr(
            "mastisk.agents.ingest.whisper.transcribe",
            AsyncMock(return_value=TranscriptResult(text="raw late-night note", segments=[])),
        )
        monkeypatch.setattr(
            "mastisk.routes.capture.route_capture",
            AsyncMock(return_value=Capture(type="inbox", confidence=0.1, body="raw late-night note")),
        )

        with _client(vault_tmp, data_tmp, db) as client:
            queued = client.post(
                "/api/capture/audio",
                headers={"Authorization": "Bearer tok"},
                data={"ts": "2026-06-11T23:58:00+05:30"},
                files={"file": ("note.m4a", b"audio", "audio/mp4")},
            )
            assert queued.status_code == 202, queued.text
            asyncio.run(IngestAgent().run_once())

        note = db.execute("SELECT * FROM notes WHERE source = 'phone'").fetchone()
    finally:
        (data_tmp / "config.toml").unlink(missing_ok=True)
        reload_settings()

    assert note is not None
    assert note["created_at"].startswith("2026-06-11T23:58:00")
    assert note["slug"].startswith("235800-raw-late-night-note")


def test_journal_photo_success_stores_attachment_and_appends_handwritten_log(
    vault_tmp, data_tmp, db, monkeypatch
):
    monkeypatch.setattr(
        "mastisk.routes.ingest.extract_journal_photo_text",
        AsyncMock(return_value="I felt calm after the walk."),
    )

    with _client(vault_tmp, data_tmp, db) as client:
        res = client.post(
            "/api/ingest/journal-photo",
            data={"date": "2026-06-12"},
            files={"photo": ("journal.jpg", b"jpg-bytes", "image/jpeg")},
        )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["ocr_status"] == "done"
    assert body["attachment"]["path"].startswith("attachments/")
    assert (vault_tmp / body["attachment"]["path"]).read_bytes() == b"jpg-bytes"
    journal_text = (vault_tmp / "journal" / "2026-06-12.md").read_text(encoding="utf-8")
    assert "Handwritten OCR: I felt calm after the walk." in journal_text
    assert "[source: handwritten]" in journal_text


def test_journal_photo_unavailable_path_is_honest_needs_triage(vault_tmp, data_tmp, db):
    with _client(vault_tmp, data_tmp, db) as client:
        res = client.post(
            "/api/ingest/journal-photo",
            data={"date": "2026-06-12"},
            files={"photo": ("journal.jpg", b"jpg-bytes", "image/jpeg")},
        )

    assert res.status_code == 201, res.text
    body = res.json()
    assert body["ocr_status"] == "unavailable"
    assert body["status_code"] == 501
    journal_text = (vault_tmp / "journal" / "2026-06-12.md").read_text(encoding="utf-8")
    assert "OCR pending: vision path unavailable." in journal_text
    assert "#needs-triage" in journal_text


def test_journal_photo_unavailable_path_maps_bad_frontmatter_to_409(
    vault_tmp, data_tmp, db
):
    journal_dir = vault_tmp / "journal"
    journal_dir.mkdir(parents=True)
    (journal_dir / "2026-06-12.md").write_text("---\nnot: [valid\n---\n## Log\n", encoding="utf-8")

    with _client(vault_tmp, data_tmp, db) as client:
        res = client.post(
            "/api/ingest/journal-photo",
            data={"date": "2026-06-12"},
            files={"photo": ("journal.jpg", b"jpg-bytes", "image/jpeg")},
        )

    assert res.status_code == 409, res.text
    assert "frontmatter" in res.json()["detail"]
