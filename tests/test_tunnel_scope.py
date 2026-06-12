from __future__ import annotations

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def _mock_capture_router():
    from mastisk.capture.router import Capture

    return AsyncMock(
        return_value=Capture(
            type="note",
            confidence=0.91,
            body="captured",
        )
    )


def test_tunnel_marked_quick_capture_is_hidden(vault_tmp, data_tmp, db):
    with _client(vault_tmp, data_tmp, db) as client:
        response = client.post(
            "/api/quick-capture",
            json={"text": "this should not invoke an LLM"},
            headers={"Host": "capture.example.com", "Cf-Connecting-IP": "203.0.113.10"},
        )

    assert response.status_code == 404


def test_tunnel_marked_vault_file_is_hidden(vault_tmp, data_tmp, db):
    target = vault_tmp / "journal" / "2026-06-11.md"
    target.parent.mkdir(parents=True)
    target.write_text("## Log\nprivate\n", encoding="utf-8")

    with _client(vault_tmp, data_tmp, db) as client:
        response = client.get(
            "/api/vault/file?path=journal/2026-06-11.md",
            headers={"Host": "capture.example.com", "X-Forwarded-Host": "capture.example.com"},
        )

    assert response.status_code == 404


def test_tunnel_marked_capture_still_allows_valid_bearer(vault_tmp, data_tmp, db):
    (data_tmp / "config.toml").write_text(
        '[capture]\nbearer_token = "test-token"\n',
        encoding="utf-8",
    )
    mocked_router = _mock_capture_router()

    with patch("mastisk.routes.capture.route_capture", mocked_router):
        with _client(vault_tmp, data_tmp, db) as client:
            response = client.post(
                "/api/capture",
                json={"text": "watch note", "source": "watch"},
                headers={
                    "Host": "capture.example.com",
                    "Cf-Connecting-IP": "203.0.113.10",
                    "Authorization": "Bearer test-token",
                },
            )

    assert response.status_code == 201, response.text
    assert response.json()["type"] == "note"


def test_local_host_quick_capture_is_unchanged(vault_tmp, data_tmp, db):
    mocked_router = _mock_capture_router()

    with patch("mastisk.routes.capture.route_capture", mocked_router):
        with _client(vault_tmp, data_tmp, db) as client:
            response = client.post(
                "/api/quick-capture",
                json={"text": "local note"},
                headers={"Host": "localhost:5555"},
            )

    assert response.status_code == 201, response.text
    assert response.json()["type"] == "note"


def test_configured_extra_local_hostname_keeps_tailnet_pwa_flow(vault_tmp, data_tmp, db):
    (data_tmp / "config.toml").write_text(
        '[server]\nlocal_hostnames = ["mastisk.tailnet.ts.net"]\n',
        encoding="utf-8",
    )
    mocked_router = _mock_capture_router()

    with patch("mastisk.routes.capture.route_capture", mocked_router):
        with _client(vault_tmp, data_tmp, db) as client:
            response = client.post(
                "/api/quick-capture",
                json={"text": "phone pwa note"},
                headers={"Host": "mastisk.tailnet.ts.net:5555"},
            )

    assert response.status_code == 201, response.text
    assert response.json()["type"] == "note"
