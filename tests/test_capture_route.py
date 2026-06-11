"""Integration tests for the /api/capture ingress."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


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
