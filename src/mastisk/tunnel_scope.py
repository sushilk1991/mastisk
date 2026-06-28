"""Defense-in-depth scoping for cloud/tunnel traffic."""
from __future__ import annotations

import json
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mastisk.settings import get_settings

_TUNNELED_INGRESS_PATHS = frozenset({"/api/capture", "/api/capture/audio"})
_PROXY_MARKER_HEADERS = frozenset(
    {
        "forwarded",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-port",
        "x-forwarded-proto",
        "x-forwarded-server",
        "x-real-ip",
    }
)


class TunnelScopeMiddleware:
    """Hide all non-ingress routes when a request looks tunnel/proxy-originated.

    Client-IP loopback checks are not sufficient for Mastisk because cloudflared
    runs on the same Mac: tunneled requests can arrive at FastAPI from
    127.0.0.1. Instead, this app-level backstop scopes by Host/proxy headers.
    It is defense in depth and does not replace the bearer token enforced on
    the allowed capture ingress routes.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app
        self.allowed_local_hosts = _allowed_local_hosts()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        if (
            _has_tunnel_provenance(scope, self.allowed_local_hosts)
            and path not in _TUNNELED_INGRESS_PATHS
        ):
            response = JSONResponse({"detail": "Not Found"}, status_code=404)
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


def _has_tunnel_provenance(scope: Scope, allowed_local_hosts: set[str]) -> bool:
    headers = Headers(scope=scope)
    host = headers.get("host") or ""
    if _normalize_host(host) not in allowed_local_hosts:
        return True
    return _has_proxy_marker_headers(scope)


def _allowed_local_hosts() -> set[str]:
    configured = get_settings().server.local_hostnames
    hosts = {
        normalized
        for raw in configured
        if (normalized := _normalize_host(str(raw)))
    }
    hosts.update(_discover_tailscale_local_hosts())
    return hosts


@lru_cache(maxsize=1)
def _discover_tailscale_local_hosts() -> set[str]:
    binary = _tailscale_binary()
    if not binary:
        return set()

    try:
        raw = subprocess.check_output(
            [binary, "status", "--json"],
            stderr=subprocess.DEVNULL,
            timeout=1,
        )
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except Exception:
        return set()

    self_node = data.get("Self") if isinstance(data, dict) else None
    if not isinstance(self_node, dict):
        return set()

    hosts: list[str] = []
    dns_name = self_node.get("DNSName")
    if isinstance(dns_name, str) and dns_name.strip():
        hosts.append(dns_name.rstrip("."))

    tailscale_ips = self_node.get("TailscaleIPs")
    if isinstance(tailscale_ips, list):
        hosts.extend(ip for ip in tailscale_ips if isinstance(ip, str))

    return {
        normalized
        for raw_host in hosts
        if (normalized := _normalize_host(raw_host))
    }


def _tailscale_binary() -> str | None:
    for candidate in [
        shutil.which("tailscale"),
        "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
        "/usr/local/bin/tailscale",
    ]:
        if candidate and Path(candidate).exists():
            return str(candidate)
    return None


def _normalize_host(value: str) -> str:
    host = value.strip().lower()
    if not host:
        return ""
    if host.startswith("["):
        end = host.find("]")
        if end != -1:
            return host[1:end]
    if host.count(":") == 1:
        return host.rsplit(":", 1)[0]
    return host


def _has_proxy_marker_headers(scope: Scope) -> bool:
    for raw_name, _raw_value in scope.get("headers") or []:
        name = raw_name.decode("latin1").lower()
        if name.startswith("cf-") or name in _PROXY_MARKER_HEADERS:
            return True
    return False
