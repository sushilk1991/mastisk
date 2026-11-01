"""Wire the drain boundary into the daemon, CLI, and update paths."""
from __future__ import annotations

import asyncio
import os
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner


@pytest.mark.asyncio
async def test_scheduler_tracks_runs():
    """Scheduler tracking extends through the callable's final persistence step."""
    from mastisk.runtime import _RunGate
    from mastisk.scheduler import _TrackedScheduler

    gate = _RunGate()
    sched = _TrackedScheduler(gate, timezone="UTC")
    started = asyncio.Event()
    finish = asyncio.Event()
    done = asyncio.Event()
    saved = []

    async def run():
        started.set()
        await finish.wait()
        saved.append("saved")
        done.set()

    sched.add_job(run, "date")
    sched.start()
    try:
        await asyncio.wait_for(started.wait(), 3)
        gate._begin_drain()
        assert gate._snapshot()["active"] == 1
        finish.set()
        await asyncio.wait_for(done.wait(), 3)
        await asyncio.sleep(0)
        assert saved == ["saved"]
        assert gate._snapshot()["active"] == 0
    finally:
        finish.set()
        sched.shutdown(wait=False)


def test_app_control_lifespan(db, data_tmp, monkeypatch):
    """The socket and HTTP middleware must share the scheduler's exact gate."""
    from mastisk import scheduler
    from mastisk.app import create_app
    from mastisk.restart_control import _read_status
    from mastisk.settings import reload_settings

    gates = []

    async def start(gate=None):
        gates.append(gate)
        return None

    monkeypatch.setattr(scheduler, "start_scheduler", start)
    reload_settings()
    app = create_app()
    with TestClient(app) as client:
        state = _read_status()
        assert state["pid"] == os.getpid()
        assert gates and gates[0] is not None
        gates[0]._begin_drain()
        assert client.get("/api/sidebar").status_code == 503
        gates[0]._end_drain()
        assert client.get("/api/sidebar").status_code == 200


def test_restart_cli(monkeypatch):
    """Expose one command with bounded waiting and visible completion."""
    from mastisk import restart_control as control
    from mastisk.cli import app

    calls = []

    def restart(timeout, progress):
        calls.append(timeout)
        progress(2)
        progress(0)

    monkeypatch.setattr(control, "_restart_daemon", restart)
    result = CliRunner().invoke(app, ["restart", "--timeout", "12"])
    assert result.exit_code == 0, result.output
    assert calls == [12]
    assert "2 active" in result.output
    assert "healthy" in result.output


def test_restart_cli_failure(monkeypatch):
    """Unsupported/failed restarts return nonzero without claiming success."""
    from mastisk import restart_control as control
    from mastisk.cli import app

    def fail(timeout, progress):
        raise control._RestartError("no control socket")

    monkeypatch.setattr(control, "_restart_daemon", fail)
    result = CliRunner().invoke(app, ["restart"])
    assert result.exit_code != 0
    assert "no control socket" in result.output
    assert "healthy" not in result.output


def test_update_drains_first(data_tmp, tmp_path, monkeypatch):
    """Reinstallation must not replace package files underneath an active run."""
    from mastisk import cli
    from mastisk import restart_control as control

    events = []

    @contextmanager
    def drain(*args, **kwargs):
        events.append("drained")
        try:
            yield {"pid": os.getpid(), "identity": "old", "python": "/known/python3.14"}
        finally:
            events.append("released")

    def run(cwd, cmd, **kwargs):
        assert events == ["drained"]
        assert "--python" in cmd
        assert cmd[cmd.index("--python") + 1] == "/known/python3.14"
        events.append("installed")

    def git(cwd, args):
        if args[0] == "status":
            return " M README.md"
        return "1"

    monkeypatch.setattr(cli, "_load_install_source", lambda: str(tmp_path))
    monkeypatch.setattr(cli, "_git_output", git)
    monkeypatch.setattr(cli, "_launchd_loaded", lambda: True)
    monkeypatch.setattr(cli, "_run_in", lambda cwd, cmd, **kw: run(cwd, cmd) if cmd[0] == "uv" else None)
    monkeypatch.setattr(control, "_drain_session", drain)
    monkeypatch.setattr(control, "_restart_idle", lambda state: events.append("restarted"))
    result = CliRunner().invoke(cli.app, ["update", "--skip-build"])
    assert result.exit_code == 0, result.output
    assert events == ["drained", "installed", "restarted", "released"]


def test_update_uses_checkout(tmp_path, monkeypatch):
    """The shell updater must update its own checkout, not an older install source."""
    from mastisk import cli

    calls = []
    monkeypatch.setenv("MASTISK_UPDATE_SOURCE", str(tmp_path))
    monkeypatch.setattr(cli, "_load_install_source", lambda: "/missing/old-checkout")
    monkeypatch.setattr(cli, "_run_in", lambda cwd, cmd, **kw: calls.append(cwd))
    monkeypatch.setattr(cli, "_git_output", lambda cwd, args: "0")
    result = CliRunner().invoke(cli.app, ["update", "--check"])
    assert result.exit_code == 0, result.output
    assert calls == [tmp_path]


def test_install_update_is_safe():
    """The shell updater uses checkout code and cannot bypass the drain gate."""
    script = (Path(__file__).resolve().parents[1] / "install.sh").read_text()
    section = script.split('if [ "$UPDATE" -eq 1 ]; then', 1)[1].split("# ─", 1)[0]
    assert 'uv run --project "$REPO_ROOT" mastisk update' in section
    assert "kickstart -k" not in section
    assert "pkill" not in section
