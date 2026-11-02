"""Bounded, read-only model metadata: Settings -> catalog driver -> native CLI/API."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import signal
import tempfile
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import httpx

from mastisk.bridges import anthropic_bridge, ollama_bridge
from mastisk.bridges.executables import _find_cli
from mastisk.settings import get_settings

_PROBE_SECONDS = 15
_STOP_SECONDS = 1
_MAX_BYTES = 1024 * 1024
_MAX_PAGES = 20
_PAGE_SIZE = 100
_CACHE_SECONDS = 300
_CACHE_ENTRIES = 32
_SHOW_CONCURRENCY = 4
_CLI_PROVIDERS = ("claude", "codex", "muse")
_cache: OrderedDict[str, tuple[float, dict]] = OrderedDict()
_locks: dict[str, asyncio.Lock] = {}


class _CatalogError(RuntimeError):
    """Only controlled messages reach the UI; raw replies may contain account identities."""

    def __init__(self, message: str, *, status: str = "unavailable"):
        super().__init__(message)
        self._status = status


def _row(model: str, name: str, *, thinking=None, default=None, kind="chat") -> dict:
    """Normalize picker data, not account details or provider-specific wire objects."""
    return {"id": model, "name": name if isinstance(name, str) else model, "thinking": thinking,
            "default_thinking": default if isinstance(default, str) else None, "kind": kind}


def _levels(raw) -> list[str] | None:
    """Absent/unknown capability is not evidence of a supported reasoning level."""
    if not isinstance(raw, list):
        return None
    return list(dict.fromkeys(v for v in raw if isinstance(v, str) and v))


def _cli_detection() -> dict:
    """Passive detection only; opening ordinary Settings must never execute a CLI."""
    settings = get_settings()
    return {provider: {"path": _find_cli(getattr(settings, f"{provider}_cmd")), "auto_path": _find_cli(provider)}
            for provider in _CLI_PROVIDERS}


def _cache_key(provider: str) -> str:
    """Invalidate on connection, credential, executable or executable-version changes."""
    settings = get_settings()
    if provider in _CLI_PROVIDERS:
        command = getattr(settings, f"{provider}_cmd")
        path = _find_cli(command)
        try:
            stat = Path(path).stat() if path else None
        except OSError:
            stat = None
        identity = (provider, command, path, stat.st_mtime_ns if stat else None,
                    stat.st_size if stat else None)
    elif provider == "ollama":
        identity = (provider, ollama_bridge._endpoints())
    else:
        identity = (provider, settings.anthropic_api_key)
    return hashlib.sha256(repr(identity).encode()).hexdigest()


async def _stop_child(process) -> None:
    """Reap the whole probe group, including children a wrapper started before exiting."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(process.wait(), _STOP_SECONDS)

    # A wrapper can exit before a stubborn grandchild; the group still needs final cleanup.
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGKILL)
    await process.wait()


class _Metadata:
    """Sequential request/reply reader; no session or inference methods are exposed."""

    def __init__(self, process):
        self._process = process
        self._received = 0

    async def _send(self, message: dict) -> None:
        self._process.stdin.write((json.dumps(message) + "\n").encode())
        await self._process.stdin.drain()

    async def _exchange(self, message: dict) -> dict:
        # CLI -> bounded JSONL -> matching id. Notifications are ignored, not surfaced.
        await self._send(message)
        while True:
            line = await self._process.stdout.readline()
            self._received += len(line)
            if not line or self._received > _MAX_BYTES:
                raise _CatalogError("The CLI did not return a readable model list.")
            try:
                reply = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(reply, dict):
                continue
            if "id" in message and reply.get("id") == message["id"]:
                return reply
            response = reply.get("response")
            if isinstance(response, dict) and response.get("request_id") == message.get("request_id"):
                return response

    async def _request(self, method: str, params: dict) -> dict:
        reply = await self._exchange({"jsonrpc": "2.0", "id": method, "method": method, "params": params})
        result = reply.get("result")
        if "error" in reply or not isinstance(result, dict):
            raise _CatalogError("The CLI could not list models. Check its sign-in and version.")
        return result


@asynccontextmanager
async def _probe(provider: str, executable: str):
    """Temporary cwd + metadata-only handshake + deadline + unconditional process cleanup."""
    args = [executable, "app-server"]
    if provider == "claude":
        args = [executable, "-p", "--input-format", "stream-json", "--output-format", "stream-json",
                "--verbose", "--tools", "", "--strict-mcp-config", "--settings", '{"disableAllHooks":true}']
    elif provider == "muse":
        args = [executable, "serve", "--preset", "empty",
                "--no-session-log", "--disable-write", "--disable-shell"]

    with tempfile.TemporaryDirectory(prefix="mastisk-models-") as workdir:
        process = await asyncio.create_subprocess_exec(
            *args, cwd=workdir, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, limit=_MAX_BYTES,
            env={**os.environ, "MUSE_NO_AUTO_UPDATE": "1"},
        )
        try:
            yield _Metadata(process)
        finally:
            await _stop_child(process)


async def _cli_models(provider: str) -> tuple[list[dict], str]:
    """Read native picker metadata, following YoYo's Codex/Claude handshake without a dependency."""
    executable = _find_cli(getattr(get_settings(), f"{provider}_cmd"))
    if executable is None:
        raise _CatalogError(f"{provider.title()} was not detected on the Mastisk server. Check Advanced if it is installed elsewhere.", status="not_installed")

    async with _probe(provider, executable) as wire:
        if provider == "claude":
            response = await wire._exchange({"type": "control_request", "request_id": "models",
                                            "request": {"subtype": "initialize"}})
            payload = response.get("response")
            if response.get("subtype") != "success" or not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
                raise _CatalogError("Claude could not list models. Run claude auth status, then refresh.")
            rows = [_row(r["value"], r.get("displayName") or r["value"],
                         thinking=_levels(r.get("supportedEffortLevels")))
                    for r in payload["models"] if isinstance(r, dict) and isinstance(r.get("value"), str)]
            return rows, "native-cli"

        await wire._request("initialize", {"clientInfo": {"name": "mastisk", "version": "0.1.0"}})
        await wire._send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        if provider == "muse":
            result = await wire._request("model/list", {})
            if not isinstance(result.get("models"), list):
                raise _CatalogError("Muse returned an unreadable model list. Check its version.")
            rows = [_row(r["modelId"], r.get("displayLabel") or r["modelId"],
                         thinking=_levels(r.get("variants")), default=r.get("defaultReasoningEffort"))
                    for r in result["models"] if isinstance(r, dict) and isinstance(r.get("modelId"), str)]
            # A bundled catalog is still native metadata, but must not be called live account access.
            source = "native-cli" if result.get("source") == "providerCatalog" else "cli-bundled"
            return rows, source

        account = await wire._request("account/read", {"refreshToken": False})
        if not account.get("account"):
            raise _CatalogError("Sign in with codex login, then refresh models.", status="needs_auth")
        rows, cursor, seen = [], None, set()
        for _ in range(_MAX_PAGES):
            result = await wire._request("model/list", {"limit": _PAGE_SIZE, "includeHidden": False, "cursor": cursor})
            if not isinstance(result.get("data"), list):
                raise _CatalogError("Codex returned an unreadable model list. Check its version.")
            for row in result["data"]:
                if not isinstance(row, dict) or not isinstance(row.get("model"), str) or row.get("hidden"):
                    continue
                raw = row.get("supportedReasoningEfforts")
                levels = _levels([r.get("reasoningEffort") for r in raw if isinstance(r, dict)]) if isinstance(raw, list) else None
                rows.append(_row(row["model"], row.get("displayName") or row["model"],
                                 thinking=levels, default=row.get("defaultReasoningEffort")))
            cursor = result.get("nextCursor")
            if cursor is None:
                return rows, "native-cli"
            if not isinstance(cursor, str) or cursor in seen:
                break
            seen.add(cursor)
        raise _CatalogError("Codex returned too many or repeated catalog pages.")


async def _http_json(client, method: str, url: str, **kwargs) -> dict:
    """Cap response bytes before decoding; do not forward provider error bodies or credentials."""
    async with client.stream(method, url, **kwargs) as response:
        response.raise_for_status()
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body.extend(chunk)
            if len(body) > _MAX_BYTES:
                raise _CatalogError("The model catalog response was too large.")
    result = json.loads(body)
    if not isinstance(result, dict):
        raise _CatalogError("The provider returned an unreadable model list.")
    return result


def _ollama_thinking(info: dict) -> tuple[list[str] | None, str | None]:
    """Ollama advertises named levels in show.thinking; capability-only builds use a toggle."""
    capabilities = info.get("capabilities")
    if not isinstance(capabilities, list):
        return None, None
    if "thinking" not in capabilities:
        return ["off"], "off"
    thinking = info.get("thinking")
    if not isinstance(thinking, dict) or not isinstance(thinking.get("values"), list):
        return ["off", "on"], None

    def encode(value):
        if isinstance(value, bool):
            return "on" if value else "off"
        return value if isinstance(value, str) else None

    levels = _levels([encode(v) for v in thinking["values"]])
    return levels, encode(thinking.get("default"))


async def _describe_ollama(client, base, headers, name, listed, semaphore):
    """Fetch capability data in bounded parallelism; an unlisted saved id is not a discovery."""
    async with semaphore:
        try:
            info = await _http_json(client, "POST", base + "/api/show", headers=headers, json={"model": name})
        except (httpx.HTTPError, ValueError, _CatalogError):
            return _row(name, name, kind="unknown") if name in listed else None

    levels, default = _ollama_thinking(info)
    caps = info.get("capabilities")
    kind = "unknown"
    if isinstance(caps, list):
        kind = "chat" if "completion" in caps else "embedding" if "embedding" in caps else "unknown"
    return _row(name, name, thinking=levels, default=default, kind=kind)


async def _ollama_models(model: str) -> tuple[list[dict], str]:
    """List installed/connected models and bounded parallel show metadata; never pull or chat."""
    async with httpx.AsyncClient(timeout=_PROBE_SECONDS, trust_env=False) as client:
        for base, key in ollama_bridge._endpoints():
            headers = {"Authorization": f"Bearer {key}"} if key else {}
            try:
                result = await _http_json(client, "GET", base + "/api/tags", headers=headers)
                if not isinstance(result.get("models"), list):
                    continue
                names = list(dict.fromkeys(r["name"] for r in result["models"]
                                           if isinstance(r, dict) and isinstance(r.get("name"), str)))
                listed = set(names)
                if model and model not in listed:
                    names.append(model)
                semaphore = asyncio.Semaphore(_SHOW_CONCURRENCY)

                probes = (_describe_ollama(client, base, headers, name, listed, semaphore) for name in names)
                rows = [row for row in await asyncio.gather(*probes) if row is not None]
                if rows:
                    return rows, "ollama-api"
            except (httpx.HTTPError, ValueError, _CatalogError):
                continue
    raise _CatalogError("Could not reach Ollama. Check the connection in Advanced, then refresh.")


async def _anthropic_models() -> tuple[list[dict], str]:
    """Read the configured API's catalog; no key is returned and no Messages call is made."""
    key = get_settings().anthropic_api_key
    if not key:
        raise _CatalogError("No Anthropic API key is configured. Use a signed-in CLI instead, or configure the API key.", status="needs_auth")
    headers = {"x-api-key": key, "anthropic-version": anthropic_bridge.API_VERSION}
    rows, cursor, seen = [], None, set()
    async with httpx.AsyncClient(timeout=_PROBE_SECONDS, trust_env=False) as client:
        for _ in range(_MAX_PAGES):
            params = {"limit": _PAGE_SIZE}
            if cursor:
                params["after_id"] = cursor
            result = await _http_json(client, "GET", "https://api.anthropic.com/v1/models", headers=headers, params=params)
            if not isinstance(result.get("data"), list):
                break
            rows.extend(_row(r["id"], r.get("display_name") or r["id"])
                        for r in result["data"] if isinstance(r, dict) and isinstance(r.get("id"), str))
            if not result.get("has_more"):
                return rows, "provider-api"
            cursor = result.get("last_id")
            if not isinstance(cursor, str) or cursor in seen:
                break
            seen.add(cursor)
    raise _CatalogError("Anthropic returned an unreadable or repeated catalog page.")


async def _get_catalog(provider: str, *, model: str = "", refresh: bool = False) -> dict:
    """Short-lived last-good cache; failures retain prior choices and remain explicit failures."""
    key = _cache_key(provider)
    lock = _locks.setdefault(provider, asyncio.Lock())
    try:
        # The deadline includes waiting behind another browser, not just the child/API request.
        async with asyncio.timeout(_PROBE_SECONDS), lock:
            cached = _cache.get(key)
            if (cached and not refresh and time.monotonic() - cached[0] < _CACHE_SECONDS
                    and (provider != "ollama" or not model or any(r["id"] == model for r in cached[1]["models"]))):
                return cached[1]
            if provider in _CLI_PROVIDERS:
                rows, source = await _cli_models(provider)
            elif provider == "ollama":
                rows, source = await _ollama_models(model)
            else:
                rows, source = await _anthropic_models()
            if not rows:
                raise _CatalogError("No models were returned. Check the provider's sign-in and refresh.")
            result = {"provider": provider, "status": "ready", "models": rows, "source": source,
                      "checked_at": datetime.now(UTC).isoformat(), "stale": False, "message": None}
            _cache[key] = (time.monotonic(), result)
            _cache.move_to_end(key)
            while len(_cache) > _CACHE_ENTRIES:
                _cache.popitem(last=False)
            return result
    except _CatalogError as exc:
        status, message = exc._status, str(exc)
    except TimeoutError:
        status, message = "unavailable", "Model discovery timed out. Check the provider and refresh."
    except (OSError, ValueError, httpx.HTTPError):
        status, message = "unavailable", "Could not list models. Check the provider's connection, sign-in and version."
    cached = _cache.get(key)
    previous = cached[1] if cached else {}
    return {**previous, "provider": provider, "status": status, "models": previous.get("models", []),
            "source": previous.get("source"), "checked_at": previous.get("checked_at"),
            "stale": bool(cached), "message": message}
