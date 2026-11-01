"""Private drain transport and launchd driver.

CLI --Unix socket--> pause admission --> wait for saved results
    --hold connection--> launchctl restart --> verify replacement
A disconnected CLI releases its drain; active runs are never cancelled here.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import plistlib
import re
import socket
import stat
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager, contextmanager, suppress
from pathlib import Path

import httpx
from apscheduler.schedulers.base import STATE_PAUSED, STATE_RUNNING

from mastisk.paths import data_dir
from mastisk.runtime import _RunGate
from mastisk.settings import get_settings

_DEFAULT_DRAIN_TIMEOUT = 1800.0
_CONTROL_INTERVAL = 0.1
_CONTROL_TIMEOUT = 5.0
_HEALTH_TIMEOUT = 60.0
_HEALTH_INTERVAL = 0.5
_MAX_MESSAGE_BYTES = 4096
_PRIVATE_DIR_MODE = 0o700
_PRIVATE_SOCKET_MODE = 0o600
_SERVICE_LABEL = "com.mastisk.agents"


class _RestartError(RuntimeError):
    """A restart precondition or verification failed; never force a fallback."""


def _control_path() -> Path:
    # Hash the data directory to isolate installs and avoid Unix socket path limits.
    digest = hashlib.sha256(str(data_dir().resolve()).encode()).hexdigest()[:16]
    return Path("/tmp") / f"mastisk-{os.getuid()}-{digest}" / "control.sock"


def _prepare_socket(path: Path) -> None:
    # Only unlink our own stale socket; refuse symlinks or another live daemon.
    path.parent.mkdir(mode=_PRIVATE_DIR_MODE, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid():
        raise _RestartError("Unsafe restart control directory")
    path.parent.chmod(_PRIVATE_DIR_MODE)
    if not path.exists() and not path.is_symlink():
        return
    entry = path.lstat()
    if not stat.S_ISSOCK(entry.st_mode) or entry.st_uid != os.getuid():
        raise _RestartError("Unsafe restart control socket")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
        probe.settimeout(_CONTROL_TIMEOUT)
        try:
            probe.connect(str(path))
        except (ConnectionRefusedError, FileNotFoundError):
            path.unlink(missing_ok=True)
            return
    raise _RestartError("Another daemon owns the restart control socket")


@asynccontextmanager
async def _control_server(gate: _RunGate, scheduler):
    # The filesystem restricts control to this user; no HTTP management surface exists.
    path = _control_path()
    _prepare_socket(path)
    identity = uuid.uuid4().hex
    handlers = set()
    closing = False

    async def send_state(writer, **extra):
        state = {
            **gate._snapshot(), "pid": os.getpid(), "identity": identity,
            "python": str(Path(sys.executable).resolve()), **extra,
        }
        writer.write(json.dumps(state).encode() + b"\n")
        await writer.drain()

    async def handle(reader, writer):
        task = asyncio.current_task()
        handlers.add(task)
        owns_drain = False
        resume_scheduler = False
        try:
            command = await asyncio.wait_for(reader.readline(), _CONTROL_TIMEOUT)
            if command == b"status\n":
                await send_state(writer)
                return
            if command != b"drain\n":
                await send_state(writer, error="Unknown control command")
                return
            if not gate._begin_drain():
                await send_state(writer, error="Another restart is already draining")
                return

            # Hold admission even after idle; the CLI keeps this connection until kickstart.
            owns_drain = True
            resume_scheduler = scheduler is not None and scheduler.state == STATE_RUNNING
            if resume_scheduler:
                scheduler.pause()
            while True:
                await send_state(writer, ready=gate._snapshot()["active"] == 0)
                try:
                    command = await asyncio.wait_for(reader.readline(), _CONTROL_INTERVAL)
                except TimeoutError:
                    continue
                if not command or command == b"cancel\n":
                    return
        except (OSError, TimeoutError, ValueError):
            # Disconnect or malformed input releases only this connection's drain.
            pass
        finally:
            if owns_drain and not closing:
                gate._end_drain()
                if resume_scheduler and scheduler.state == STATE_PAUSED:
                    scheduler.resume()
            writer.close()
            with suppress(OSError):
                await writer.wait_closed()
            handlers.discard(task)

    server = await asyncio.start_unix_server(handle, path=str(path), limit=_MAX_MESSAGE_BYTES)
    path.chmod(_PRIVATE_SOCKET_MODE)
    try:
        yield path
    finally:
        # Stop control sessions without cancelling any tracked run.
        closing = True
        gate._close()
        server.close()
        await server.wait_closed()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*list(handlers), return_exceptions=True)
        path.unlink(missing_ok=True)


def _service_info() -> int:
    # Validate the replacement executable before closing admission or signalling launchd.
    plist = Path.home() / "Library/LaunchAgents" / f"{_SERVICE_LABEL}.plist"
    try:
        config = plistlib.loads(plist.read_bytes())
        program = config.get("Program") or config["ProgramArguments"][0]
        if not os.access(program, os.X_OK):
            raise _RestartError("launchd executable is missing or not executable")
        result = subprocess.run(
            ["launchctl", "print", f"gui/{os.getuid()}/{_SERVICE_LABEL}"],
            check=True, capture_output=True, text=True, timeout=_CONTROL_TIMEOUT,
        )
    except (OSError, KeyError, IndexError, plistlib.InvalidFileException, subprocess.SubprocessError) as exc:
        raise _RestartError("Cannot verify the running launchd service; no restart attempted") from exc
    match = re.search(r"^\s*pid = (\d+)\s*$", result.stdout, re.MULTILINE)
    if not match:
        raise _RestartError("launchd service has no running PID; no restart attempted")
    return int(match.group(1))


@contextmanager
def _connection():
    # No network fallback: an old daemon must not be force-restarted accidentally.
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(_CONTROL_TIMEOUT)
        try:
            client.connect(str(_control_path()))
        except OSError as exc:
            raise _RestartError(
                "Daemon has no usable safe-restart control socket. "
                "Initial deployment needs an approved maintenance window; no restart attempted."
            ) from exc
        yield client


def _read_reply(stream) -> dict:
    message = stream.readline(_MAX_MESSAGE_BYTES)
    if not message:
        raise _RestartError("Daemon disconnected before restart was authorized")
    state = json.loads(message)
    if not isinstance(state, dict) or not {"pid", "identity", "active", "draining"} <= state.keys():
        raise _RestartError("Invalid restart control response")
    if state.get("error"):
        raise _RestartError(str(state["error"]))
    return state


@contextmanager
def _drain_session(timeout=_DEFAULT_DRAIN_TIMEOUT, progress: Callable[[int], None] | None = None):
    # A live connection is the lease: timeout, Ctrl-C, or CLI death resumes dispatch.
    if not math.isfinite(timeout) or timeout <= 0:
        raise _RestartError("Restart timeout must be a positive finite number")
    deadline = time.monotonic() + timeout
    expected_pid = _service_info()
    try:
        with _connection() as client, client.makefile("rb") as stream:
            client.sendall(b"drain\n")
            previous_active = None
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _RestartError("Drain timed out; restart cancelled, active runs left running")
                client.settimeout(min(_CONTROL_TIMEOUT, remaining))
                state = _read_reply(stream)
                if state["pid"] != expected_pid:
                    raise _RestartError("Daemon identity does not match launchd; no restart attempted")
                if progress is not None and state["active"] != previous_active:
                    progress(state["active"])
                    previous_active = state["active"]
                if state.get("ready") and state["draining"] and state["active"] == 0:
                    yield state
                    return
    except TimeoutError as exc:
        raise _RestartError("Drain timed out; restart cancelled, active runs left running") from exc
    except (OSError, ValueError) as exc:
        raise _RestartError("Restart control failed; no forced restart attempted") from exc


def _kickstart() -> None:
    # This is the sole forced launchctl operation, reached only under an idle drain lease.
    try:
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{os.getuid()}/{_SERVICE_LABEL}"],
            check=True, capture_output=True, text=True, timeout=_CONTROL_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise _RestartError("launchctl restart failed") from exc


def _read_status() -> dict:
    with _connection() as client, client.makefile("rb") as stream:
        client.sendall(b"status\n")
        return _read_reply(stream)


def _wait_healthy(previous: dict) -> None:
    # Confirm a replacement process and HTTP readiness, not merely a successful kickstart.
    deadline = time.monotonic() + _HEALTH_TIMEOUT
    url = f"http://127.0.0.1:{get_settings().port}/api/sidebar"
    with httpx.Client(trust_env=False, timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                state = _read_status()
                replaced = state["identity"] != previous["identity"]
                if not replaced or state["draining"] or state["pid"] != _service_info():
                    time.sleep(_HEALTH_INTERVAL)
                    continue
                if client.get(url).is_success:
                    return
            except (_RestartError, OSError, ValueError, httpx.HTTPError):
                pass
            time.sleep(_HEALTH_INTERVAL)
    raise _RestartError("Restart issued, but the replacement daemon did not become healthy")


def _restart_idle(state: dict) -> None:
    # Revalidate the live hold after an update; an earlier idle snapshot is not authority.
    current = _read_status()
    if (
        current["identity"] != state["identity"]
        or current["pid"] != state["pid"]
        or not current["draining"]
        or current["active"] != 0
    ):
        raise _RestartError("Idle drain was lost; no restart attempted")
    if _service_info() != state["pid"]:
        raise _RestartError("Daemon identity changed during drain; no restart attempted")
    _kickstart()
    _wait_healthy(state)


def _restart_daemon(timeout=_DEFAULT_DRAIN_TIMEOUT, progress=None) -> None:
    with _drain_session(timeout, progress) as state:
        _restart_idle(state)
