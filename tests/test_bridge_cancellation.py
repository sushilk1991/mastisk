"""CLI bridges must not leave the child running when the caller is cancelled.

Agents wrap bridge calls in an outer ``asyncio.wait_for``; when that fires the
bridge coroutine is cancelled rather than hitting its own timeout branch.
Each test runs a stand-in executable (a shell script that execs ``sleep``),
never the real CLI.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest


def _fake_cli(directory: Path, name: str) -> tuple[Path, Path]:
    pidfile = directory / f"{name}.pid"
    script = directory / name
    script.write_text(f'#!/bin/sh\necho $$ > "{pidfile}"\nexec sleep 30\n')
    script.chmod(0o755)
    return script, pidfile


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _cancel_and_check(coro, pidfile: Path) -> None:
    task = asyncio.create_task(coro)
    pid = None
    try:
        for _ in range(200):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await asyncio.sleep(0.02)
        pid = int(pidfile.read_text())
        assert _alive(pid)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not _alive(pid), "bridge left its child process running after cancellation"
    finally:
        if pid is not None and _alive(pid):
            os.kill(pid, 9)


@pytest.mark.asyncio
async def test_claude_cancel_kills_child(tmp_path, data_tmp, vault_tmp, monkeypatch):
    from mastisk.bridges import claude_bridge

    script, pidfile = _fake_cli(tmp_path, "claude")
    monkeypatch.setattr(claude_bridge, "_find_cli", lambda command: str(script))
    await _cancel_and_check(claude_bridge.run_claude("hi"), pidfile)


@pytest.mark.asyncio
async def test_codex_cancel_kills_child(tmp_path, monkeypatch):
    from mastisk.bridges import codex_bridge

    script, pidfile = _fake_cli(tmp_path, "codex")
    monkeypatch.setattr(codex_bridge, "_find_cli", lambda command: str(script))
    await _cancel_and_check(codex_bridge.run_codex("hi"), pidfile)


@pytest.mark.asyncio
async def test_gemini_cancel_kills_child(tmp_path, monkeypatch):
    from mastisk.bridges import gemini_bridge

    # The bridge spawns the bare name, so the stand-in must win PATH lookup.
    _, pidfile = _fake_cli(tmp_path, "gemini")
    monkeypatch.setenv("PATH", f"{tmp_path}:/bin:/usr/bin")
    await _cancel_and_check(gemini_bridge.run_gemini("hi"), pidfile)
