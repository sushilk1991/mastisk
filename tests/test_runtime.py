"""Drain admission must cover whole runs, including result persistence."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import httpx
import pytest
from fastapi import BackgroundTasks, FastAPI


def test_gate_admission():
    """Closing admission preserves existing work and rejects late starts."""
    from mastisk.runtime import _RunGate

    gate = _RunGate()
    assert gate._enter()
    assert gate._begin_drain()
    assert not gate._begin_drain()
    assert not gate._enter()
    assert gate._snapshot() == {"active": 1, "draining": True}
    gate._leave()
    assert gate._snapshot()["active"] == 0
    gate._end_drain()
    assert gate._enter()
    gate._leave()


@pytest.mark.asyncio
async def test_async_run_persists(tmp_path):
    """A finished subprocess is not an idle run until its result is saved."""
    from mastisk.runtime import _RunGate, _track_job

    gate = _RunGate()
    generated = asyncio.Event()
    persist = asyncio.Event()
    target = tmp_path / "result"

    async def run():
        generated.set()
        await persist.wait()
        target.write_text("saved")

    task = asyncio.create_task(_track_job(gate, run)())
    await generated.wait()
    gate._begin_drain()
    assert gate._snapshot()["active"] == 1
    assert not target.exists()
    persist.set()
    await task
    assert target.read_text() == "saved"
    assert gate._snapshot()["active"] == 0


def test_sync_run_drains():
    """Thread-pool scheduled work uses the same admission boundary."""
    from mastisk.runtime import _RunGate, _track_job

    gate = _RunGate()
    started = Event()
    finish = Event()
    saved = []

    def run():
        started.set()
        assert finish.wait(5)
        saved.append("saved")

    with ThreadPoolExecutor() as pool:
        future = pool.submit(_track_job(gate, run))
        try:
            assert started.wait(5)
            gate._begin_drain()
            assert gate._snapshot()["active"] == 1
            assert _track_job(gate, lambda: saved.append("late"))() is None
        finally:
            finish.set()
        future.result()

    assert saved == ["saved"]
    assert gate._snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_job_errors_release():
    """Failed work releases admission without hiding the original error."""
    from mastisk.runtime import _RunGate, _track_job

    gate = _RunGate()

    async def fail():
        raise ValueError("failed")

    with pytest.raises(ValueError, match="failed"):
        await _track_job(gate, fail)()
    assert gate._snapshot()["active"] == 0
    gate._begin_drain()
    assert await _track_job(gate, fail)() is None


@pytest.mark.asyncio
async def test_http_background_drains():
    """HTTP background tasks remain counted after the response is emitted."""
    from mastisk.runtime import _DrainMiddleware, _RunGate

    gate = _RunGate()
    app = FastAPI()
    generated = asyncio.Event()
    persist = asyncio.Event()
    saved = []

    async def save():
        generated.set()
        await persist.wait()
        saved.append("saved")

    @app.post("/work")
    async def work(background: BackgroundTasks):
        background.add_task(save)
        return {"accepted": True}

    app.add_middleware(_DrainMiddleware, gate=gate)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
        task = asyncio.create_task(client.post("/work"))
        await generated.wait()
        gate._begin_drain()
        try:
            assert gate._snapshot()["active"] == 1
            blocked = await client.post("/work")
            assert blocked.status_code == 503
            assert blocked.headers["retry-after"]
            assert saved == []
        finally:
            persist.set()
            await task

    assert saved == ["saved"]
    assert gate._snapshot()["active"] == 0


@pytest.mark.asyncio
async def test_feed_does_not_block():
    """Passive feed subscriptions must not prevent restart forever."""
    from mastisk.runtime import _DrainMiddleware, _RunGate

    gate = _RunGate()
    observed = []

    async def app(scope, receive, send):
        observed.append(gate._snapshot()["active"])

    wrapped = _DrainMiddleware(app, gate=gate)
    await wrapped({"type": "http", "method": "GET", "path": "/api/feed/stream"}, None, None)
    assert observed == [0]
