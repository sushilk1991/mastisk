from __future__ import annotations

from fastapi.testclient import TestClient


def test_integration_health_endpoint_shape(db, data_tmp, monkeypatch):
    (data_tmp / "config.toml").write_text(
        "[notify]\n"
        'backend = "ntfy"\n'
        'ntfy_topic = "mastisk-test"\n'
        "[calendar]\n"
        'client_id = "client"\n'
        'client_secret = "secret"\n'
        'calendar_ids = ["primary"]\n'
        "[intelligence]\n"
        'provider_order = ["ollama", "codex"]\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    db.execute(
        """INSERT INTO feed (agent, verb, obj, kind, ts)
           VALUES ('reminder_engine', 'notify_failed', '1', 'reminder',
                   datetime('now', '-1 hour'))"""
    )
    monkeypatch.setattr("mastisk.routes.integration_health._module_available", lambda name: name == "markitdown")

    from mastisk.app import create_app

    response = TestClient(create_app()).get("/api/health/integrations")
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"calendar", "push", "bridges", "ingest"}
    assert body["calendar"]["status"] in {"not_synced", "unconfigured", "connected", "disconnected"}
    assert body["push"] == {
        "backend": "ntfy",
        "configured": True,
        "notify_failed_last_24h": 1,
    }
    assert body["bridges"]["claude"]["configured"] is True
    assert body["bridges"]["provider_order"] == ["ollama", "codex"]
    assert "ollama_chat" in body["bridges"]
    assert body["ingest"]["markitdown"] is True
    assert body["ingest"]["docling"] is False
    assert body["ingest"]["mlx_whisper"] is False
