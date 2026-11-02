"""Read real subprocess protocol replies, never infer a model list from vendor names."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mastisk.routes import settings_route
from mastisk.settings import reload_settings

_HTTP_OK = 200
_MODEL = "deepseek-v4.1-flash:1017-cloud"


@pytest.fixture
def client(data_tmp, vault_tmp, monkeypatch):
    # Each case has isolated config and metadata cache, with no scheduler or live DB.
    monkeypatch.setenv("HOME", str(data_tmp))
    reload_settings()
    module = sys.modules.get("mastisk.bridges.model_catalog")
    if module is not None:
        module._cache.clear()
        module._locks.clear()
    app = FastAPI()
    app.include_router(settings_route.router, prefix="/api")
    with TestClient(app) as instance:
        yield instance


def _fake_cli(tmp_path, provider, extra=""):
    # Fake native CLIs admit only metadata handshakes; any user turn fails the child.
    path = tmp_path / provider
    log = tmp_path / f"{provider}.jsonl"
    code = f"""\
import json, os, sys
from pathlib import Path
Path({str(path.with_suffix('.pid'))!r}).write_text(str(os.getpid()))
{extra}
for line in sys.stdin:
    msg = json.loads(line)
    with open({str(log)!r}, 'a') as out:
        out.write(json.dumps(msg) + '\\n')
    if msg.get('type') == 'control_request':
        assert msg['request']['subtype'] == 'initialize'
        assert '--tools' in sys.argv and sys.argv[sys.argv.index('--tools') + 1] == ''
        assert '--strict-mcp-config' in sys.argv
        assert 'disableAllHooks' in ' '.join(sys.argv)
        reply = {{'type': 'control_response', 'response': {{'subtype': 'success',
            'request_id': msg['request_id'], 'response': {{'email': 'PRIVATE_ACCOUNT',
            'models': [{{'value': 'sonnet', 'displayName': 'Native Sonnet',
            'supportedEffortLevels': ['low', 'high']}}]}}}}}}
    else:
        method = msg['method']
        if method == 'initialized':
            continue
        if method == 'initialize':
            result = {{}}
        elif method == 'account/read':
            assert msg['params']['refreshToken'] is False
            result = {{'account': {{'type': 'chatgpt', 'email': 'PRIVATE_ACCOUNT'}}}}
        elif method == 'model/list' and {provider!r} == 'codex':
            cursor = msg['params'].get('cursor')
            result = {{'data': [{{'model': 'native-b' if cursor else 'native-a',
                'displayName': 'Native B' if cursor else 'Native A',
                'supportedReasoningEfforts': [{{'reasoningEffort': 'low'}}, {{'reasoningEffort': 'high'}}]}}],
                'nextCursor': None if cursor else 'next'}}
        elif method == 'model/list' and {provider!r} == 'muse':
            assert '--disable-shell' in sys.argv and '--disable-write' in sys.argv
            assert '--no-session-log' in sys.argv
            assert '--provider' not in sys.argv, 'respect native Muse provider configuration'
            assert os.environ['MUSE_NO_AUTO_UPDATE'] == '1'
            result = {{'source': 'providerCatalog', 'providerId': 'meta', 'models': [{{
                'modelId': 'native-muse', 'displayLabel': 'Native Muse',
                'variants': ['low', 'high'], 'defaultReasoningEffort': 'high'}}]}}
        else:
            raise RuntimeError('unexpected inference request')
        reply = {{'id': msg['id'], 'result': result}}
    print(json.dumps(reply), flush=True)
"""
    path.write_text(f"#!{sys.executable}\n" + textwrap.dedent(code))
    path.chmod(0o755)
    return path, log


@pytest.mark.parametrize("provider,ids", [
    ("codex", ["native-a", "native-b"]), ("claude", ["sonnet"]), ("muse", ["native-muse"]),
])
def test_native_picker_catalog(client, tmp_path, provider, ids):
    path, log = _fake_cli(tmp_path, provider)
    assert client.post("/api/settings", json={f"{provider}_cmd": str(path)}).status_code == _HTTP_OK
    response = client.get(f"/api/settings/models/{provider}")
    assert response.status_code == _HTTP_OK, response.text
    body = response.json()
    assert body["status"] == "ready", body
    assert [row["id"] for row in body["models"]] == ids
    assert body["models"][0]["thinking"] == ["low", "high"]
    assert "PRIVATE_ACCOUNT" not in response.text
    assert body["source"] == "native-cli"
    assert log.exists()
    pid = int(path.with_suffix(".pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_catalog_cache_refresh(client, tmp_path):
    path, log = _fake_cli(tmp_path, "codex")
    client.post("/api/settings", json={"codex_cmd": str(path)})
    first = client.get("/api/settings/models/codex")
    assert first.status_code == _HTTP_OK
    count = len(log.read_text().splitlines())
    assert client.get("/api/settings/models/codex").json() == first.json()
    assert len(log.read_text().splitlines()) == count
    assert client.get("/api/settings/models/codex?refresh=true").json()["status"] == "ready"
    assert len(log.read_text().splitlines()) == count * 2


def test_missing_override(client, tmp_path):
    client.post("/api/settings", json={"codex_cmd": str(tmp_path / "missing-codex")})
    response = client.get("/api/settings/models/codex")
    assert response.status_code == _HTTP_OK
    assert response.json()["status"] == "not_installed"
    assert response.json()["models"] == []


def test_ollama_thinking_metadata(client, monkeypatch):
    # The actual model metadata, not a guessed global level list, defines the picker.
    original = httpx.AsyncClient
    seen = []

    def handler(request):
        seen.append(request.url.path)
        if request.url.path == "/api/tags":
            return httpx.Response(_HTTP_OK, json={"models": [{"name": _MODEL}, {"name": "plain-model"}]})
        assert request.url.path == "/api/show"
        if json.loads(request.content)["model"] == "plain-model":
            return httpx.Response(_HTTP_OK, json={"capabilities": ["completion"]})
        assert json.loads(request.content)["model"] == _MODEL
        return httpx.Response(_HTTP_OK, json={
            "capabilities": ["completion", "thinking"],
            "thinking": {"values": [False, "low", "high", "max"], "default": "high"},
        })

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    response = client.get("/api/settings/models/ollama", params={"model": _MODEL})
    assert response.status_code == _HTTP_OK
    body = response.json()
    assert body["status"] == "ready", body
    row = next(r for r in body["models"] if r["id"] == _MODEL)
    assert row["thinking"] == ["off", "low", "high", "max"]
    assert row["default_thinking"] == "high"
    assert "/api/chat" not in seen


def test_stale_catalog(client, monkeypatch):
    original = httpx.AsyncClient
    fail = False

    def handler(request):
        if fail:
            raise httpx.ConnectError("PRIVATE_CREDENTIAL", request=request)
        return httpx.Response(_HTTP_OK, json={"models": [{"name": "plain-model"}]})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    first = client.get("/api/settings/models/ollama")
    assert first.status_code == _HTTP_OK
    assert first.json()["status"] == "ready"
    fail = True
    response = client.get("/api/settings/models/ollama?refresh=true")
    assert response.json()["stale"] is True
    assert response.json()["models"] == first.json()["models"]
    assert "PRIVATE_CREDENTIAL" not in response.text


def test_passive_detection_route(client, tmp_path, monkeypatch):
    path, _ = _fake_cli(tmp_path, "codex")
    client.post("/api/settings", json={"codex_cmd": str(path), "intelligence": {
        "provider_order": ["codex", "ollama"], "anthropic_auto": False,
    }})
    body = client.get("/api/settings").json()
    assert body["cli_detection"]["codex"]["path"] == str(path)
    assert body["effective_order"] == ["codex", "ollama"]
    assert not path.with_suffix(".pid").exists(), "ordinary settings GET must not execute a CLI"


def test_timeout_reaps_child(client, tmp_path, monkeypatch):
    """A stalled metadata command cannot become an orphaned background service."""
    from mastisk.bridges import model_catalog

    path, _ = _fake_cli(tmp_path, "codex", "import time; time.sleep(30)")
    client.post("/api/settings", json={"codex_cmd": str(path)})
    # Allow a cold interpreter to write its PID before testing a stalled metadata reply.
    monkeypatch.setattr(model_catalog, "_PROBE_SECONDS", 1.0)
    response = client.get("/api/settings/models/codex")
    assert response.status_code == _HTTP_OK
    assert response.json()["status"] == "unavailable"
    assert "timed out" in response.json()["message"]
    pid = int(path.with_suffix(".pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_command_invalidates_cache(client, tmp_path):
    """A fresh catalog from another executable/account cannot reuse the old command's cache."""
    path, _ = _fake_cli(tmp_path, "codex")
    client.post("/api/settings", json={"codex_cmd": str(path)})
    assert client.get("/api/settings/models/codex").json()["status"] == "ready"
    client.post("/api/settings", json={"codex_cmd": str(tmp_path / "absent")})
    changed = client.get("/api/settings/models/codex").json()
    assert changed["status"] == "not_installed"
    assert changed["models"] == [] and changed["stale"] is False


def test_provider_is_not_shell(client, tmp_path):
    """A path parameter names a provider, never an executable or shell expression."""
    response = client.get("/api/settings/models/codex;echo-injected")
    assert response.status_code == 422


def test_api_model_persists(client):
    """The top-level picker must also control the existing Anthropic API model field."""
    response = client.post("/api/settings", json={"intelligence": {"anthropic_model": "selected-api-model"}})
    assert response.status_code == _HTTP_OK
    assert client.get("/api/settings").json()["values"]["intelligence"]["anthropic_model"] == "selected-api-model"


def test_cancel_reaps_child(client, tmp_path):
    """Cancelling a request must reap the CLI as well as timeouts do."""
    from mastisk.bridges import model_catalog

    path, _ = _fake_cli(tmp_path, "codex", "import time; time.sleep(30)")
    client.post("/api/settings", json={"codex_cmd": str(path)})

    async def cancel_after_start():
        task = asyncio.create_task(model_catalog._get_catalog("codex"))
        try:
            async with asyncio.timeout(5):
                while not path.with_suffix(".pid").exists():
                    await asyncio.sleep(0.01)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(cancel_after_start())
    with pytest.raises(ProcessLookupError):
        os.kill(int(path.with_suffix(".pid").read_text()), 0)


def test_api_pages_are_private(client, data_tmp, monkeypatch):
    """Use real API pagination fields, but return only selectable model metadata."""
    (data_tmp / "config.toml").write_text('anthropic_api_key = "PRIVATE_TEST_KEY"\n')
    reload_settings()
    original = httpx.AsyncClient
    seen = []

    def handler(request):
        assert request.url.path == "/v1/models"
        assert request.headers["x-api-key"] == "PRIVATE_TEST_KEY"
        seen.append(request.url.params.get("after_id"))
        later = bool(request.url.params.get("after_id"))
        return httpx.Response(_HTTP_OK, json={"data": [{"id": "api-b" if later else "api-a",
            "display_name": "Native API model", "account": "PRIVATE_ACCOUNT"}],
            "has_more": not later, "last_id": "cursor"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: original(transport=httpx.MockTransport(handler), **kw))
    response = client.get("/api/settings/models/anthropic")
    assert response.json()["status"] == "ready"
    assert [r["id"] for r in response.json()["models"]] == ["api-a", "api-b"]
    assert seen == [None, "cursor"]
    assert "PRIVATE" not in response.text


def test_queue_deadline(client, monkeypatch):
    """A busy catalog must not make a second browser wait outside the discovery deadline."""
    from mastisk.bridges import model_catalog

    monkeypatch.setattr(model_catalog, "_PROBE_SECONDS", 0.03)

    async def wait_behind_probe():
        lock = asyncio.Lock()
        model_catalog._locks["codex"] = lock
        async with lock:
            result = await asyncio.wait_for(model_catalog._get_catalog("codex"), timeout=0.5)
            assert result["status"] == "unavailable"
            assert "timed out" in result["message"]

    asyncio.run(wait_behind_probe())
