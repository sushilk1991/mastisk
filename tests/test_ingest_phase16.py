from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.app import create_app

    return TestClient(create_app())


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
    assert "source_type: report" in note_text
    assert "summary: A useful report." in note_text
    assert f"original: {source_path}" in note_text
    assert "# Converted" in note_text
    assert note["escalation_state"] == "none"


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
