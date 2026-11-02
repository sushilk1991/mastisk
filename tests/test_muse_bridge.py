"""Muse completion and process-lifecycle contracts use recorded JSONL event shapes."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from mastisk.bridges import muse_bridge


def _terminal(kind, text):
    return json.dumps({
        "payload_type": f"run.terminal.{kind}",
        "payload": {"terminal": kind, "text": text},
    })


@pytest.fixture
def spawn(monkeypatch, data_tmp):
    """Replace only OS spawning; real adapter parsing and cleanup still execute."""
    process = MagicMock(returncode=0)
    process.communicate = AsyncMock(return_value=(b"", b""))
    process.wait = AsyncMock()
    factory = AsyncMock(return_value=process)
    monkeypatch.setattr(muse_bridge, "_find_cli", lambda command: "/test/muse")
    monkeypatch.setattr(muse_bridge.asyncio, "create_subprocess_exec", factory)
    return process, factory


@pytest.mark.asyncio
async def test_terminal_answer_only(spawn):
    """Partial deltas must never be mistaken for a final answer."""
    process, factory = spawn
    partial = json.dumps({"payload_type": "run.output.delta", "payload": {"text": "unfinished"}})
    process.communicate.return_value = ((partial + "\n" + _terminal("completed", "FINAL")).encode(), b"")

    result = await muse_bridge._run_muse("Reply")

    assert result["text"] == "FINAL"
    assert not Path(factory.call_args.kwargs["cwd"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("output", [
    "not-json", _terminal("failed", "partial"), _terminal("cancelled", "partial"),
    _terminal("completed", " "),
    _terminal("completed", "old") + "\n" + _terminal("failed", "later failure"),
])
async def test_incomplete_result_fails(spawn, output):
    """Exit zero without a valid final result must still trigger fallback."""
    process, factory = spawn
    process.communicate.return_value = (output.encode(), b"")
    with pytest.raises(RuntimeError):
        await muse_bridge._run_muse("Reply")
    assert not Path(factory.call_args.kwargs["cwd"]).exists()


@pytest.mark.asyncio
async def test_timeout_reaps_child(spawn):
    """A timeout must report its cause, reap the process, and remove its working directory."""
    process, factory = spawn
    process.communicate.side_effect = TimeoutError()
    with pytest.raises(RuntimeError, match="Muse timed out"):
        await muse_bridge._run_muse("Reply", timeout=0.01)
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    assert not Path(factory.call_args.kwargs["cwd"]).exists()


@pytest.mark.asyncio
async def test_cancel_reaps_child(spawn):
    """Cancellation propagates rather than silently trying another provider."""
    process, factory = spawn
    process.communicate.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await muse_bridge._run_muse("Reply")
    process.kill.assert_called_once()
    process.wait.assert_awaited_once()
    assert not Path(factory.call_args.kwargs["cwd"]).exists()
