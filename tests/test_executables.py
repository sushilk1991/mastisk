"""CLI discovery must work under launchd without trusting a workspace PATH entry."""
from __future__ import annotations

import os
from pathlib import Path

import pytest


def _executable(path: Path) -> Path:
    # A tiny executable is enough: detection must never run the discovered file.
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)
    return path


@pytest.mark.parametrize("name", ["claude", "codex", "muse"])
def test_detect_in_user_bin(monkeypatch, tmp_path, name):
    from mastisk.bridges.executables import _find_cli

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    expected = _executable(tmp_path / ".local/bin" / name)
    assert _find_cli(name) == str(expected)


def test_keep_explicit_override(monkeypatch, tmp_path):
    from mastisk.bridges.executables import _find_cli

    override = _executable(tmp_path / "custom/codex")
    _executable(tmp_path / ".local/bin/codex")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _find_cli(str(override)) == str(override)
    assert _find_cli(str(tmp_path / "missing/codex")) is None


def test_ignore_relative_path(monkeypatch, tmp_path):
    from mastisk.bridges.executables import _find_cli

    _executable(tmp_path / "workspace/not-a-real-cli")
    monkeypatch.chdir(tmp_path / "workspace")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", os.pathsep.join(["", ".", "../workspace"]))
    assert _find_cli("not-a-real-cli") is None


def test_expand_home_override(monkeypatch, tmp_path):
    from mastisk.bridges.executables import _find_cli

    expected = _executable(tmp_path / "custom/muse")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert _find_cli("~/custom/muse") == str(expected)


def test_path_before_fallback(monkeypatch, tmp_path):
    from mastisk.bridges.executables import _find_cli

    preferred = _executable(tmp_path / "preferred/codex")
    _executable(tmp_path / ".local/bin/codex")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", str(preferred.parent))
    assert _find_cli("codex") == str(preferred)


def test_absolute_override(monkeypatch, tmp_path):
    from mastisk.bridges.executables import _find_cli

    expected = _executable(tmp_path / "custom/codex")
    monkeypatch.chdir(tmp_path)
    assert _find_cli("./custom/codex") == str(expected)


@pytest.mark.asyncio
async def test_claude_missing_binary(monkeypatch, data_tmp):
    from mastisk.bridges import claude_bridge

    monkeypatch.setattr(claude_bridge, "_find_cli", lambda _command: None)
    # Optional consumers still ask which(legacy_resolver(name)); they must receive a name, not an error.
    assert claude_bridge._resolve_cmd("missing-cli") == "missing-cli"
    with pytest.raises(claude_bridge.ClaudeError):
        await claude_bridge.run_claude("Never start an undetected CLI")
