"""Admission boundary for drain-before-restart.

admitted run -> generate -> persist -> leave
                    drain closes new admission, not existing runs
"""
from __future__ import annotations

import inspect
from functools import wraps
from threading import Lock

from starlette.responses import JSONResponse
from starlette.status import HTTP_503_SERVICE_UNAVAILABLE
from starlette.types import ASGIApp, Receive, Scope, Send

_RETRY_AFTER_SECONDS = 5
_PASSIVE_FEED_PATH = "/api/feed/stream"


class _RunGate:
    """Count whole runs across the event loop and scheduler worker threads."""

    def __init__(self) -> None:
        self._lock = Lock()
        self._active = 0
        self._draining = False
        self._closed = False

    def _enter(self) -> bool:
        # Admission and draining share a lock so idle cannot race a late start.
        with self._lock:
            if self._draining or self._closed:
                return False
            self._active += 1
            return True

    def _leave(self) -> None:
        with self._lock:
            self._active -= 1

    def _begin_drain(self) -> bool:
        # One owner holds admission closed until restart or disconnect.
        with self._lock:
            if self._draining or self._closed:
                return False
            self._draining = True
            return True

    def _end_drain(self) -> None:
        with self._lock:
            if not self._closed:
                self._draining = False

    def _close(self) -> None:
        # Shutdown must not reopen admission when the control client disconnects.
        with self._lock:
            self._closed = True
            self._draining = True

    def _snapshot(self) -> dict:
        with self._lock:
            return {"active": self._active, "draining": self._draining}


def _track_job(gate: _RunGate, func):
    """Wrap the complete callable, preserving APScheduler's async detection."""
    if inspect.iscoroutinefunction(func):
        @wraps(func)
        async def run_async(*args, **kwargs):
            if not gate._enter():
                return None
            try:
                return await func(*args, **kwargs)
            finally:
                gate._leave()

        return run_async

    # Synchronous scheduled jobs run in APScheduler's thread pool.
    @wraps(func)
    def run_sync(*args, **kwargs):
        if not gate._enter():
            return None
        try:
            return func(*args, **kwargs)
        finally:
            gate._leave()

    return run_sync


class _DrainMiddleware:
    """Track ASGI completion, including response background tasks."""

    def __init__(self, app: ASGIApp, gate: _RunGate) -> None:
        self._app = app
        self._gate = gate

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        # Only the passive GET feed is exempt; it starts no agent work.
        passive_feed = (
            scope.get("method") == "GET" and scope.get("path") == _PASSIVE_FEED_PATH
        )
        if scope["type"] != "http" or passive_feed:
            await self._app(scope, receive, send)
            return

        if not self._gate._enter():
            response = JSONResponse(
                {"detail": "Mastisk is draining for restart; retry shortly."},
                status_code=HTTP_503_SERVICE_UNAVAILABLE,
                headers={"Retry-After": str(_RETRY_AFTER_SECONDS)},
            )
            await response(scope, receive, send)
            return

        try:
            await self._app(scope, receive, send)
        finally:
            self._gate._leave()
