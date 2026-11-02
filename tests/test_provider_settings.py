"""Provider controls execute real API and bridge seams without external model calls."""
from __future__ import annotations

import asyncio
import json
import stat
import tomllib
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mastisk.bridges import intelligence, ollama_bridge
from mastisk.routes import settings_route
from mastisk.settings import reload_settings

_MODEL = "deepseek-v4.1-flash:1017-cloud"
_ORDER = ["ollama", "codex", "claude", "muse"]


@pytest.fixture
def client(data_tmp, vault_tmp):
    """Test settings persistence without starting the daemon or scheduler."""
    reload_settings()
    app = FastAPI()
    app.include_router(settings_route.router, prefix="/api")
    with TestClient(app) as instance:
        yield instance


def _provider_patch():
    return {
        "codex_cmd": "/custom/codex", "muse_cmd": "/custom/muse",
        "summarize_model_heavy": _MODEL, "ollama_think": "high",
        "intelligence": {
            "provider_order": _ORDER, "anthropic_auto": False,
            "codex_model": "codex-model", "codex_reasoning_effort": "high",
            "muse_model": "muse-model", "muse_reasoning_effort": "high",
        },
    }


def test_provider_settings_roundtrip(client, data_tmp):
    """Executable paths and nested model settings must survive an actual save/reload."""
    response = client.post("/api/settings", json=_provider_patch())
    assert response.status_code == 200, response.text
    values = client.get("/api/settings").json()["values"]
    for key, value in _provider_patch().items():
        if key == "intelligence":
            for name, expected in value.items():
                assert values[key][name] == expected
            continue
        assert values.get(key) == value

    reload_settings()
    assert intelligence.effective_order() == _ORDER
    path = data_tmp / "config.toml"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_provider_save_keeps_config(client, data_tmp):
    """A partial settings update must not drop secrets or nested extension tables."""
    path = data_tmp / "config.toml"
    path.write_text(
        'anthropic_api_key = "test-private-key"\n'
        '[server]\nlocal_hostnames = ["keep.example"]\n'
        '[intelligence]\ncodex_model = "keep-model"\n'
        '[custom.rules]\nname = "keep-nested"\n',
        encoding="utf-8",
    )
    response = client.post("/api/settings", json={
        "ollama_think": "high", "intelligence": {"provider_order": _ORDER},
    })
    assert response.status_code == 200, response.text
    data = tomllib.loads(path.read_text())
    assert data["custom"]["rules"]["name"] == "keep-nested"
    assert data["server"]["local_hostnames"] == ["keep.example"]
    assert data["anthropic_api_key"] == "test-private-key"
    assert data["intelligence"]["codex_model"] == "keep-model"
    assert data["intelligence"]["provider_order"] == _ORDER
    assert "test-private-key" not in client.get("/api/settings").text


@pytest.mark.parametrize("order", [[], ["unknown"], ["ollama", "ollama"]])
def test_invalid_order_keeps_file(client, data_tmp, order):
    """Reject invalid provider orders before any part of the patch is persisted."""
    path = data_tmp / "config.toml"
    original = 'summarize_model_heavy = "keep-original"\n'
    path.write_text(original)
    response = client.post("/api/settings", json={
        "summarize_model_heavy": _MODEL, "intelligence": {"provider_order": order},
    })
    assert response.status_code == 422
    assert path.read_text() == original


@pytest.mark.parametrize("entrypoint", ["chat", "run_ollama"])
def test_heavy_model_thinks(client, monkeypatch, entrypoint):
    """Both Ollama entry points must send high thinking and return only final content."""
    client.post("/api/settings", json={"summarize_model_heavy": _MODEL, "ollama_think": "high"})
    seen = []

    async def respond(_client, url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(
            200, request=httpx.Request("POST", url),
            json={"message": {"content": "FINAL_OK", "thinking": "private reasoning"}},
        )

    monkeypatch.setattr(ollama_bridge, "_endpoints", lambda: [("http://test", None)])
    monkeypatch.setattr(httpx.AsyncClient, "post", respond)
    if entrypoint == "chat":
        result = asyncio.run(ollama_bridge.chat("Reply briefly"))
    else:
        result = asyncio.run(ollama_bridge.run_ollama("Reply briefly", _MODEL))["text"]
    assert result == "FINAL_OK"
    assert seen[0]["model"] == _MODEL
    assert seen[0]["think"] == "high"


def test_cheap_model_stays_fast(client, monkeypatch):
    """High thinking for the main cloud model must not alter local cheap-model calls."""
    client.post("/api/settings", json={"summarize_model_heavy": _MODEL, "ollama_think": "high"})
    seen = []

    async def respond(_client, url, **kwargs):
        seen.append(kwargs["json"])
        return httpx.Response(200, request=httpx.Request("POST", url), json={"message": {"content": "OK"}})

    monkeypatch.setattr(ollama_bridge, "_endpoints", lambda: [("http://test", None)])
    monkeypatch.setattr(httpx.AsyncClient, "post", respond)
    assert asyncio.run(ollama_bridge.chat("Classify", cheap=True)) == "OK"
    assert seen[0]["think"] is False


def test_configured_codex_path(client, monkeypatch):
    """Codex's Settings executable must be the one run, retaining read-only isolation."""
    from mastisk.bridges import codex_bridge

    client.post("/api/settings", json={"codex_cmd": "/custom/codex"})
    proc = MagicMock(returncode=0)
    proc.communicate = AsyncMock(return_value=(b"CODEX_OK", b""))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(codex_bridge.shutil, "which", lambda _cmd: "/custom/codex")
    monkeypatch.setattr(codex_bridge.asyncio, "create_subprocess_exec", spawn)

    assert asyncio.run(codex_bridge.run_codex("Reply"))["text"] == "CODEX_OK"
    args = spawn.call_args.args
    assert args[0] == "/custom/codex"
    assert args[args.index("--sandbox") + 1] == "read-only"


def test_muse_dispatch_is_isolated(client, monkeypatch):
    """The Muse tier runs the real exec adapter, not an Ollama request with a false label."""
    client.post("/api/settings", json=_provider_patch())
    terminal = {
        "payload_type": "run.terminal.completed",
        "payload": {"terminal": "completed", "text": "MUSE_OK"},
    }
    proc = MagicMock(returncode=0)
    proc.communicate = AsyncMock(return_value=((json.dumps(terminal) + "\n").encode(), b""))
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr("shutil.which", lambda _cmd: "/custom/muse")
    monkeypatch.setattr(ollama_bridge, "chat", AsyncMock(side_effect=AssertionError("wrong provider")))

    result, provider = asyncio.run(intelligence.run_intelligence("Reply", provider_order=("muse",)))

    assert provider == "muse"
    assert result["text"] == "MUSE_OK"
    args = spawn.call_args.args
    assert args[0] == "/custom/muse"
    assert args[args.index("--preset") + 1] == "empty"
    assert args[args.index("--reasoning-effort") + 1] == "high"
    assert args[args.index("--model") + 1] == "muse-model"
    for flag in ["--disable-write", "--disable-shell", "--disable-web-tools", "--no-foreign-personal-context"]:
        assert flag in args
    assert "--yolo" not in args
    assert spawn.call_args.kwargs["stdin"] == asyncio.subprocess.DEVNULL
    assert not Path(spawn.call_args.kwargs["cwd"]).exists()


def test_muse_failure_falls_back(client, monkeypatch):
    """A failed Muse process must advance to the next configured provider."""
    client.post("/api/settings", json=_provider_patch())
    proc = MagicMock(returncode=1)
    proc.communicate = AsyncMock(return_value=(b"", b"provider unavailable"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    monkeypatch.setattr("shutil.which", lambda _cmd: "/custom/muse")
    chat = AsyncMock(return_value="OLLAMA_OK")
    monkeypatch.setattr(ollama_bridge, "chat", chat)

    result, provider = asyncio.run(intelligence.run_intelligence("Reply", provider_order=("muse", "ollama")))

    assert provider == "ollama"
    assert result["text"] == "OLLAMA_OK"
    assert chat.await_count == 1
