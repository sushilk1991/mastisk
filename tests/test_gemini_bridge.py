"""Gemini bridge unit tests. Mocks subprocess so tests don't call the real CLI."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_run_gemini_returns_text_on_success():
    from mastisk.bridges.gemini_bridge import run_gemini

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"the response body", b"")
    with patch("mastisk.bridges.gemini_bridge.shutil.which", return_value="/usr/bin/gemini"), \
         patch("mastisk.bridges.gemini_bridge.asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await run_gemini("what is 2+2?")
    assert result["text"] == "the response body"
    assert result["raw"] == "the response body"


@pytest.mark.asyncio
async def test_run_gemini_raises_on_missing_binary():
    from mastisk.bridges.gemini_bridge import GeminiError, run_gemini
    with patch("mastisk.bridges.gemini_bridge.shutil.which", return_value=None):
        with pytest.raises(GeminiError, match="not on PATH"):
            await run_gemini("anything")


@pytest.mark.asyncio
async def test_run_gemini_raises_on_nonzero_exit():
    from mastisk.bridges.gemini_bridge import GeminiError, run_gemini
    mock_proc = AsyncMock()
    mock_proc.returncode = 3
    mock_proc.communicate.return_value = (b"", b"bad thing")
    with patch("mastisk.bridges.gemini_bridge.shutil.which", return_value="/usr/bin/gemini"), \
         patch("mastisk.bridges.gemini_bridge.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(GeminiError, match="bad thing"):
            await run_gemini("anything")


@pytest.mark.asyncio
async def test_run_gemini_passes_prompt_flag():
    """Verify `--prompt <text>` makes it into the command."""
    from mastisk.bridges.gemini_bridge import run_gemini

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"ok", b"")
    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with patch("mastisk.bridges.gemini_bridge.shutil.which", return_value="/usr/bin/gemini"), \
         patch("mastisk.bridges.gemini_bridge.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await run_gemini("hello world")

    assert "--prompt" in captured_args
    idx = captured_args.index("--prompt")
    assert captured_args[idx + 1] == "hello world"


@pytest.mark.asyncio
async def test_run_gemini_passes_skip_trust_flag():
    """Verify `--skip-trust` is in the command so the headless daemon isn't
    blocked by the CLI's directory-trust check."""
    from mastisk.bridges.gemini_bridge import run_gemini

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"ok", b"")
    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with patch("mastisk.bridges.gemini_bridge.shutil.which", return_value="/usr/bin/gemini"), \
         patch("mastisk.bridges.gemini_bridge.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await run_gemini("hello world")

    assert "--skip-trust" in captured_args


@pytest.mark.asyncio
async def test_run_gemini_passes_model_flag():
    """Verify `-m <model>` flag is in the command when model is provided."""
    from mastisk.bridges.gemini_bridge import run_gemini

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"ok", b"")
    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with patch("mastisk.bridges.gemini_bridge.shutil.which", return_value="/usr/bin/gemini"), \
         patch("mastisk.bridges.gemini_bridge.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await run_gemini("prompt", model="gemini-2.5-pro")

    assert "-m" in captured_args
    idx = captured_args.index("-m")
    assert captured_args[idx + 1] == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_run_gemini_timeout_kills_proc():
    from mastisk.bridges.gemini_bridge import GeminiError, run_gemini
    # Use MagicMock for the process so sync methods (.kill) don't become
    # coroutines (which would trigger "coroutine never awaited" warnings).
    mock_proc = MagicMock()
    mock_proc.kill = MagicMock()
    mock_proc.wait = AsyncMock(return_value=None)
    mock_proc.returncode = 0

    async def slow_communicate():
        await asyncio.sleep(10)
        return (b"", b"")

    mock_proc.communicate = slow_communicate
    with patch("mastisk.bridges.gemini_bridge.shutil.which", return_value="/usr/bin/gemini"), \
         patch("mastisk.bridges.gemini_bridge.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(GeminiError, match="timed out"):
            await run_gemini("p", timeout=0.05)
    mock_proc.kill.assert_called_once()
