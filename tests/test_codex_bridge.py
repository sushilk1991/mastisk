"""Codex bridge unit tests. Mocks subprocess so tests don't call the real CLI."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_run_codex_returns_text_on_success():
    from mastisk.bridges.codex_bridge import run_codex

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"the response body", b"")
    with patch("mastisk.bridges.codex_bridge.shutil.which", return_value="/usr/bin/codex"), \
         patch("mastisk.bridges.codex_bridge.asyncio.create_subprocess_exec", return_value=mock_proc):
        result = await run_codex("what is 2+2?")
    assert result["text"] == "the response body"
    assert result["raw"] == "the response body"


@pytest.mark.asyncio
async def test_run_codex_raises_on_missing_binary():
    from mastisk.bridges.codex_bridge import CodexError, run_codex
    with patch("mastisk.bridges.codex_bridge.shutil.which", return_value=None):
        with pytest.raises(CodexError, match="not on PATH"):
            await run_codex("anything")


@pytest.mark.asyncio
async def test_run_codex_raises_on_nonzero_exit():
    from mastisk.bridges.codex_bridge import CodexError, run_codex
    mock_proc = AsyncMock()
    mock_proc.returncode = 2
    mock_proc.communicate.return_value = (b"", b"boom boom")
    with patch("mastisk.bridges.codex_bridge.shutil.which", return_value="/usr/bin/codex"), \
         patch("mastisk.bridges.codex_bridge.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(CodexError, match="boom"):
            await run_codex("anything")


@pytest.mark.asyncio
async def test_run_codex_passes_model_via_config_override():
    """Verify `-c model=...` flag gets into the command."""
    from mastisk.bridges.codex_bridge import run_codex

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"ok", b"")
    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with patch("mastisk.bridges.codex_bridge.shutil.which", return_value="/usr/bin/codex"), \
         patch("mastisk.bridges.codex_bridge.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await run_codex("prompt", model="gpt-5-codex")

    assert "-c" in captured_args
    idx = captured_args.index("-c")
    assert captured_args[idx + 1] == 'model="gpt-5-codex"'


@pytest.mark.asyncio
async def test_run_codex_passes_skip_git_repo_check():
    """The bridge always passes ``--skip-git-repo-check`` because the daemon
    runs from launchd's cwd (not a trusted codex directory). Without the flag
    codex refuses with ``Not inside a trusted directory``. Pin this so a
    refactor doesn't silently drop it and break the roundtable on launchd.
    """
    from mastisk.bridges.codex_bridge import run_codex

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"ok", b"")
    captured_args: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with patch("mastisk.bridges.codex_bridge.shutil.which", return_value="/usr/bin/codex"), \
         patch("mastisk.bridges.codex_bridge.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await run_codex("prompt")

    assert "--skip-git-repo-check" in captured_args


@pytest.mark.asyncio
async def test_run_codex_pins_read_only_sandbox_and_throwaway_cwd():
    """SECURITY: codex must never inherit ambient sandbox config. The user's
    ~/.codex/config.toml may set danger-full-access + approval never, and
    scheduled agents feed attacker-influenceable text (RSS, web clips,
    calendar invites) into the prompt. A read-only sandbox in a throwaway cwd
    is the hard barrier against unattended RCE via prompt injection."""
    import os

    from mastisk.bridges.codex_bridge import run_codex

    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b"ok", b"")
    captured_args: list = []
    seen_cwd: list = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        # -C <dir> must point at a real directory that exists during the call.
        idx = list(args).index("-C")
        seen_cwd.append(args[idx + 1])
        assert os.path.isdir(args[idx + 1])
        return mock_proc

    with patch("mastisk.bridges.codex_bridge.shutil.which", return_value="/usr/bin/codex"), \
         patch("mastisk.bridges.codex_bridge.asyncio.create_subprocess_exec", side_effect=fake_exec):
        await run_codex("prompt")

    assert "--sandbox" in captured_args
    assert captured_args[captured_args.index("--sandbox") + 1] == "read-only"
    assert "-C" in captured_args
    # Never leaks a working dir behind it.
    assert not os.path.exists(seen_cwd[0])
    # And never the dangerous bypass flags.
    assert "--dangerously-bypass-approvals-and-sandbox" not in captured_args
    assert "danger-full-access" not in captured_args


@pytest.mark.asyncio
async def test_run_codex_timeout_kills_proc():
    from mastisk.bridges.codex_bridge import CodexError, run_codex
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
    with patch("mastisk.bridges.codex_bridge.shutil.which", return_value="/usr/bin/codex"), \
         patch("mastisk.bridges.codex_bridge.asyncio.create_subprocess_exec", return_value=mock_proc):
        with pytest.raises(CodexError, match="timed out"):
            await run_codex("p", timeout=0.05)
    mock_proc.kill.assert_called_once()
