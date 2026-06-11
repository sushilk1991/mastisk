"""Integration tests for the /api/capture ingress."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch


def _capture(**overrides):
    from mastisk.capture.router import Capture

    data = {
        "type": "note",
        "confidence": 0.91,
        "title": None,
        "body": overrides.pop("body", "captured body"),
        "domain": None,
        "project": None,
        "person": None,
        "due": None,
        "scheduled": None,
        "priority": None,
        "recurrence": None,
        "reminder_lead_minutes": None,
        "no_reminder": False,
        "review_at": None,
        "tags": [],
        "related": [],
    }
    data.update(overrides)
    return Capture(**data)


@pytest.fixture(autouse=True)
def fake_capture_router():
    async def _default(text: str, source: str, ts: str | None):
        return _capture(body=text)

    with patch(
        "mastisk.routes.capture.route_capture",
        new_callable=AsyncMock,
        side_effect=_default,
    ) as mock:
        yield mock


@pytest.fixture
def client_with_token(vault_tmp, data_tmp, db):
    """App with a configured capture token."""
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\nbearer_token = "test-token"\n')
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def client_no_token(vault_tmp, data_tmp, db):
    """App with no capture token configured."""
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_capture_rejects_missing_token(client_with_token):
    r = client_with_token.post("/api/capture", json={"text": "hi", "source": "watch"})
    assert r.status_code == 401


def test_capture_rejects_bad_token(client_with_token):
    r = client_with_token.post(
        "/api/capture",
        json={"text": "hi", "source": "watch"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_capture_rejects_non_ascii_token_without_500(client_with_token):
    r = client_with_token.post(
        "/api/capture",
        json={"text": "hi", "source": "watch"},
        headers=[(b"authorization", "Bearer café".encode("utf-8"))],
    )
    assert r.status_code == 401


def test_capture_503_when_unconfigured(client_no_token):
    r = client_no_token.post(
        "/api/capture",
        json={"text": "hi", "source": "watch"},
        headers={"Authorization": "Bearer anything"},
    )
    assert r.status_code == 503


def test_capture_reads_token_file_changes_after_startup(client_no_token, data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\nbearer_token = "runtime-token"\n')

    created = client_no_token.post(
        "/api/capture",
        json={"text": "written after startup", "source": "watch"},
        headers={"Authorization": "Bearer runtime-token"},
    )
    assert created.status_code == 201, created.text

    cfg.write_text('[capture]\nbearer_token = "rotated-token"\n')
    old = client_no_token.post(
        "/api/capture",
        json={"text": "old token should fail", "source": "watch"},
        headers={"Authorization": "Bearer runtime-token"},
    )
    assert old.status_code == 401


def test_capture_persists_note_with_watch_source(client_with_token, vault_tmp):
    r = client_with_token.post(
        "/api/capture",
        json={"text": "remember to water the plants", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "note"
    assert body["needs_triage"] is False
    file_path = vault_tmp / body["destination"]
    assert file_path.exists()
    assert "water the plants" in file_path.read_text()

    from mastisk.db.queries import connect, get_note

    with connect() as conn:
        row = get_note(conn, body["id"])
        assert row["source"] == "watch"


def test_capture_persists_typed_intent_metadata(client_with_token, vault_tmp, fake_capture_router):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.93,
        body="call Sam",
        due="2026-06-10T14:00:00-07:00",
        reminder_lead_minutes=15,
        tags=["follow-up"],
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "remind me to call Sam tomorrow 2pm", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "task"
    assert body["needs_triage"] is False

    file_text = (vault_tmp / body["destination"]).read_text()
    assert file_text.startswith("---\n")
    assert "capture:" in file_text
    assert "type: task" in file_text
    assert "due: '2026-06-10T14:00:00-07:00'" in file_text
    assert "needs_triage: false" in file_text
    assert file_text.endswith("call Sam")


def test_capture_medium_confidence_marks_needs_triage(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="journal",
        confidence=0.72,
        body="felt scattered",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "felt scattered today", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "journal"
    assert body["needs_triage"] is True
    file_text = (vault_tmp / body["destination"]).read_text()
    assert "needs_triage: true" in file_text
    assert "confidence: 0.72" in file_text


def test_capture_low_confidence_falls_back_to_raw_inbox(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.2,
        body="cleaned task",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "ambiguous raw text", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "inbox"
    assert body["needs_triage"] is True
    file_text = (vault_tmp / body["destination"]).read_text()
    assert file_text == "ambiguous raw text"


def test_capture_router_failure_falls_back_to_raw_inbox(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = RuntimeError("router unavailable")

    r = client_with_token.post(
        "/api/capture",
        json={"text": "do not lose this", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "inbox"
    assert body["needs_triage"] is True
    assert (vault_tmp / body["destination"]).read_text() == "do not lose this"


def test_command_detected_capture_skips_confidence_gate(
    client_with_token, vault_tmp, fake_capture_router
):
    command_capture = _capture(type="task", confidence=0.2, body="call Sam")
    command_capture._command_detected = True
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = command_capture

    r = client_with_token.post(
        "/api/capture",
        json={"text": "remind me to call Sam", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "task"
    assert body["needs_triage"] is False
    file_text = (vault_tmp / body["destination"]).read_text()
    assert "confidence: 0.2" in file_text


def test_capture_appears_in_notes_list(client_with_token):
    client_with_token.post(
        "/api/capture",
        json={"text": "captured from the wrist", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    listing = client_with_token.get("/api/notes").json()
    assert any("wrist" in (n.get("summary") or n.get("slug") or "") for n in listing)


def test_capture_rejects_blank_text(client_with_token):
    r = client_with_token.post(
        "/api/capture",
        json={"text": "   ", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 422
