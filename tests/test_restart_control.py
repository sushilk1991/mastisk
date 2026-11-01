"""Exercise real control sockets; fake only launchd and health-check boundaries."""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
from contextlib import suppress
from pathlib import Path

import pytest


async def _open_control():
    from mastisk.restart_control import _control_path

    return await asyncio.open_unix_connection(str(_control_path()))


async def _read_state(reader):
    return json.loads(await asyncio.wait_for(reader.readline(), 3))


@pytest.mark.asyncio
async def test_drain_waits_for_save(data_tmp):
    """Readiness is emitted only after the complete run leaves the gate."""
    from mastisk.restart_control import _control_server
    from mastisk.runtime import _RunGate, _track_job

    gate = _RunGate()
    started = asyncio.Event()
    finish = asyncio.Event()
    saved = []

    async def run():
        started.set()
        await finish.wait()
        saved.append("saved")

    async with _control_server(gate, None):
        task = asyncio.create_task(_track_job(gate, run)())
        await started.wait()
        reader, writer = await _open_control()
        writer.write(b"drain\n")
        await writer.drain()
        try:
            state = await _read_state(reader)
            assert state["active"] == 1
            assert not state["ready"]
            assert state["pid"] == os.getpid()
            finish.set()
            await task
            state = await _read_state(reader)
            assert state["ready"]
            assert saved == ["saved"]
            assert not gate._enter()
        finally:
            finish.set()
            await task
            writer.close()
            await writer.wait_closed()

        for _ in range(100):
            if not gate._snapshot()["draining"]:
                break
            await asyncio.sleep(0.01)
        assert gate._enter()
        gate._leave()


@pytest.mark.asyncio
async def test_one_drain_owner(data_tmp):
    """A second client cannot release another client's admission hold."""
    from mastisk.restart_control import _control_server
    from mastisk.runtime import _RunGate

    gate = _RunGate()
    async with _control_server(gate, None):
        reader, writer = await _open_control()
        writer.write(b"drain\n")
        await writer.drain()
        assert (await _read_state(reader))["ready"]
        second_reader, second_writer = await _open_control()
        second_writer.write(b"drain\n")
        await second_writer.drain()
        assert "error" in await _read_state(second_reader)
        second_writer.close()
        await second_writer.wait_closed()
        assert gate._snapshot()["draining"]
        writer.close()
        await writer.wait_closed()


@pytest.mark.asyncio
async def test_control_permissions(data_tmp):
    """Only this macOS user may issue drain commands, outside HTTP."""
    from mastisk.restart_control import _control_path, _control_server
    from mastisk.runtime import _RunGate

    path = _control_path()
    async with _control_server(_RunGate(), None):
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        reader, writer = await _open_control()
        writer.write(b"status\n")
        await writer.drain()
        state = await _read_state(reader)
        assert not state["draining"]
        assert state["identity"]
        assert state["python"] == str(Path(sys.executable).resolve())
        writer.close()
        await writer.wait_closed()
    assert not path.exists()


@pytest.mark.asyncio
async def test_timeout_never_restarts(data_tmp, monkeypatch):
    """A timed-out CLI releases admission while existing work continues."""
    from mastisk import restart_control as control
    from mastisk.runtime import _RunGate

    gate = _RunGate()
    assert gate._enter()
    restarted = []
    monkeypatch.setattr(control, "_service_info", lambda: os.getpid())
    monkeypatch.setattr(control, "_kickstart", lambda: restarted.append(True))
    async with control._control_server(gate, None):
        with pytest.raises(control._RestartError, match="timed out"):
            await asyncio.to_thread(control._restart_daemon, 0.1)
        for _ in range(100):
            if not gate._snapshot()["draining"]:
                break
            await asyncio.sleep(0.01)
        assert restarted == []
        assert gate._snapshot() == {"active": 1, "draining": False}
        gate._leave()


@pytest.mark.asyncio
async def test_restart_requires_idle(data_tmp, monkeypatch):
    """The only kickstart occurs after persistence and while admission is closed."""
    from mastisk import restart_control as control
    from mastisk.runtime import _RunGate

    gate = _RunGate()
    assert gate._enter()
    saved = []
    restarted = []
    monkeypatch.setattr(control, "_service_info", lambda: os.getpid())

    def kickstart():
        assert saved == ["saved"]
        assert gate._snapshot() == {"active": 0, "draining": True}
        restarted.append(True)

    monkeypatch.setattr(control, "_kickstart", kickstart)
    monkeypatch.setattr(control, "_wait_healthy", lambda state: None)
    async with control._control_server(gate, None):
        task = asyncio.create_task(asyncio.to_thread(control._restart_daemon, 3))
        try:
            while not gate._snapshot()["draining"]:
                await asyncio.sleep(0.01)
            assert restarted == []
            saved.append("saved")
            gate._leave()
            await asyncio.wait_for(task, 4)
        finally:
            if not task.done():
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
    assert restarted == [True]


@pytest.mark.asyncio
async def test_pid_mismatch_fails(data_tmp, monkeypatch):
    """Never signal a launchd service that does not match the socket owner."""
    from mastisk import restart_control as control
    from mastisk.runtime import _RunGate

    monkeypatch.setattr(control, "_service_info", lambda: os.getpid() + 1)
    restarted = []
    monkeypatch.setattr(control, "_kickstart", lambda: restarted.append(True))
    async with control._control_server(_RunGate(), None):
        with pytest.raises(control._RestartError, match="identity"):
            await asyncio.to_thread(control._restart_daemon, 2)
    assert restarted == []


def test_old_daemon_fails_closed(data_tmp, monkeypatch):
    """Missing protocol support never falls back to force restart."""
    from mastisk import restart_control as control

    monkeypatch.setattr(control, "_service_info", lambda: os.getpid())
    restarted = []
    monkeypatch.setattr(control, "_kickstart", lambda: restarted.append(True))
    with pytest.raises(control._RestartError, match="control socket"):
        control._restart_daemon(1)
    assert restarted == []


@pytest.mark.asyncio
async def test_kickstart_failure_resumes(data_tmp, monkeypatch):
    """A failed service restart must not report success or strand admission."""
    from mastisk import restart_control as control
    from mastisk.runtime import _RunGate

    gate = _RunGate()
    monkeypatch.setattr(control, "_service_info", lambda: os.getpid())

    def fail():
        raise control._RestartError("launchctl failed")

    monkeypatch.setattr(control, "_kickstart", fail)
    async with control._control_server(gate, None):
        with pytest.raises(control._RestartError, match="launchctl failed"):
            await asyncio.to_thread(control._restart_daemon, 1)
        for _ in range(100):
            if not gate._snapshot()["draining"]:
                break
            await asyncio.sleep(0.01)
        assert not gate._snapshot()["draining"]


@pytest.mark.asyncio
async def test_pause_and_resume(data_tmp):
    """Disconnect restores real APScheduler dispatch, not just a status flag."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    from mastisk.restart_control import _control_server
    from mastisk.runtime import _RunGate

    gate = _RunGate()
    sched = AsyncIOScheduler()
    ran = asyncio.Event()
    sched.start()
    try:
        async with _control_server(gate, sched):
            reader, writer = await _open_control()
            writer.write(b"drain\n")
            await writer.drain()
            assert (await _read_state(reader))["ready"]
            sched.add_job(ran.set, "date")
            await asyncio.sleep(0.05)
            assert not ran.is_set()
            writer.close()
            await writer.wait_closed()
            await asyncio.wait_for(ran.wait(), 3)
    finally:
        sched.shutdown(wait=False)


@pytest.mark.asyncio
async def test_lost_drain_aborts(data_tmp, monkeypatch):
    """An expired/released idle observation cannot authorize a later restart."""
    from mastisk import restart_control as control
    from mastisk.runtime import _RunGate

    gate = _RunGate()
    restarted = []
    monkeypatch.setattr(control, "_service_info", lambda: os.getpid())
    monkeypatch.setattr(control, "_kickstart", lambda: restarted.append(True))
    monkeypatch.setattr(control, "_wait_healthy", lambda state: None)
    async with control._control_server(gate, None):
        state = await asyncio.to_thread(control._read_status)
        with pytest.raises(control._RestartError, match="drain"):
            await asyncio.to_thread(control._restart_idle, state)
    assert restarted == []


def test_health_needs_replacement(data_tmp, monkeypatch):
    """A healthy old process is not proof that restart succeeded."""
    import httpx

    from mastisk import restart_control as control

    previous = {"identity": "old", "pid": 10, "active": 0, "draining": False}
    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    monkeypatch.setattr(control.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
    monkeypatch.setattr(control, "_HEALTH_TIMEOUT", 0.02)
    monkeypatch.setattr(control, "_HEALTH_INTERVAL", 0.001)
    monkeypatch.setattr(control, "_read_status", lambda: previous)
    monkeypatch.setattr(control, "_service_info", lambda: 10)
    with pytest.raises(control._RestartError, match="did not become healthy"):
        control._wait_healthy(previous)

    monkeypatch.setattr(control, "_read_status", lambda: {**previous, "identity": "new", "pid": 11})
    monkeypatch.setattr(control, "_service_info", lambda: 11)
    control._wait_healthy(previous)


def test_health_rejects_errors(data_tmp, monkeypatch):
    """A replacement PID with a failing API is not a successful restart."""
    import httpx

    from mastisk import restart_control as control

    real_client = httpx.Client
    transport = httpx.MockTransport(lambda request: httpx.Response(503))
    monkeypatch.setattr(control.httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
    monkeypatch.setattr(control, "_HEALTH_TIMEOUT", 0.02)
    monkeypatch.setattr(control, "_HEALTH_INTERVAL", 0.001)
    monkeypatch.setattr(control, "_read_status", lambda: {"identity": "new", "pid": 11, "draining": False})
    monkeypatch.setattr(control, "_service_info", lambda: 11)
    with pytest.raises(control._RestartError, match="did not become healthy"):
        control._wait_healthy({"identity": "old", "pid": 10})


def test_missing_binary_aborts(tmp_path, monkeypatch):
    """The previous missing-entrypoint incident must fail before any signal."""
    import plistlib

    from mastisk import restart_control as control

    monkeypatch.setenv("HOME", str(tmp_path))
    plist = tmp_path / "Library/LaunchAgents/com.mastisk.agents.plist"
    plist.parent.mkdir(parents=True)
    plist.write_bytes(plistlib.dumps({"ProgramArguments": [str(tmp_path / "missing")]}))
    with pytest.raises(control._RestartError, match="executable is missing"):
        control._service_info()


@pytest.mark.asyncio
async def test_child_finishes_before_stop(data_tmp, tmp_path, monkeypatch):
    """A real headless child exits normally and its parent persists before restart."""
    from mastisk import restart_control as control
    from mastisk.runtime import _RunGate, _track_job

    gate = _RunGate()
    started = asyncio.Event()
    generated = asyncio.Event()
    persist = asyncio.Event()
    result_file = tmp_path / "saved-result"
    children = []
    restarted = []
    monkeypatch.setattr(control, "_service_info", lambda: os.getpid())
    monkeypatch.setattr(control, "_wait_healthy", lambda state: None)

    async def run():
        child = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(0.1); print('result')",
            stdout=asyncio.subprocess.PIPE,
        )
        children.append(child)
        started.set()
        output, _ = await child.communicate()
        generated.set()
        await persist.wait()
        result_file.write_bytes(output)

    def kickstart():
        assert children[0].returncode == 0
        assert result_file.read_text() == "result\n"
        restarted.append(True)

    monkeypatch.setattr(control, "_kickstart", kickstart)
    async with control._control_server(gate, None):
        run_task = asyncio.create_task(_track_job(gate, run)())
        await started.wait()
        restart_task = asyncio.create_task(asyncio.to_thread(control._restart_daemon, 3))
        try:
            await asyncio.wait_for(generated.wait(), 3)
            assert not result_file.exists()
            assert restarted == []
            assert gate._snapshot()["active"] == 1
        finally:
            persist.set()
            await run_task
            await restart_task
    assert restarted == [True]


def test_invalid_timeout(data_tmp):
    """Reject unbounded/invalid numeric input before touching launchd."""
    from mastisk import restart_control as control

    for timeout in (0, -1, float("nan"), float("inf")):
        with pytest.raises(control._RestartError, match="timeout"):
            control._restart_daemon(timeout)
