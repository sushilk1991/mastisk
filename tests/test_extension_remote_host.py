"""The extension's requests as they arrive from another machine on the tailnet.

Tailscale Serve terminates TLS and forwards plain TCP from loopback, so the
daemon sees the public host name in ``Host``, a ``chrome-extension://`` origin
and no proxy headers. These are the routes the extension calls.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

# Placeholders only. The repository is public: never a real tailnet name.
HOST = "mastisk-host.example-tailnet.ts.net"
PUBLIC_URL = f"https://{HOST}:8443"
HEADERS = {
    "Host": f"{HOST}:8443",
    "Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop",
}


def _client(data_tmp) -> TestClient:
    (data_tmp / "config.toml").write_text(
        "[server]\n"
        f'local_hostnames = ["localhost", "127.0.0.1", "testserver", "{HOST}"]\n'
        f'public_url = "{PUBLIC_URL}"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app(), client=("127.0.0.1", 50000))


def test_health_and_web_clip_work_on_the_public_host(vault_tmp, data_tmp, db):
    client = _client(data_tmp)

    health = client.get("/api/health", headers=HEADERS)
    assert health.status_code == 200, health.text
    assert health.json() == {"ok": True, "public_url": PUBLIC_URL}

    clip = client.post(
        "/api/ingest/web",
        headers=HEADERS,
        json={
            "url": "https://example.com/essay",
            "title": "An Essay",
            "content": "Long-form body of the essay. " * 20,
        },
    )
    assert clip.status_code == 202, clip.text
    source_id = clip.json()["source_id"]
    assert clip.json()["status"] == "queued"

    status = client.get(f"/api/ingest/web/{source_id}", headers=HEADERS)
    assert status.status_code == 200, status.text
    assert status.json()["status"] in {"queued", "pending", "running"}


def test_listen_and_ask_are_reachable_on_the_public_host(vault_tmp, data_tmp, db):
    """An invalid body gets 422 from the route itself: proof the request was
    not turned away earlier (404 tunnel scope, 401 capture auth), with no work run."""
    client = _client(data_tmp)

    listen = client.post("/api/listen", headers=HEADERS, json={})
    ask = client.post("/api/ask", headers=HEADERS, json={})

    assert listen.status_code == 422, listen.text
    assert ask.status_code == 422, ask.text


def test_the_same_requests_are_hidden_when_they_carry_proxy_headers(vault_tmp, data_tmp, db):
    """Why Phone setup uses TLS-terminated TCP: an HTTP proxy in front adds
    forwarded headers, and the daemon hides everything but capture ingress."""
    client = _client(data_tmp)

    proxied = client.get("/api/health", headers={**HEADERS, "X-Forwarded-Host": HOST})

    assert proxied.status_code == 404
