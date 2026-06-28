"""Defense-in-depth scoping for cloud/tunnel traffic."""
from __future__ import annotations

import ipaddress

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from mastisk.settings import get_settings

_TUNNELED_INGRESS_PATHS = frozenset({"/api/capture", "/api/capture/audio"})
_TAILNET_HOST_SUFFIX = ".ts.net"
_TAILSCALE_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("fd7a:115c:a1e0::/48"),
)
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
    host = _normalize_host(headers.get("host") or "")
    if host in allowed_local_hosts or _is_direct_tailnet_request(scope, host):
        return _has_proxy_marker_headers(scope)
    return True


def _allowed_local_hosts() -> set[str]:
    configured = get_settings().server.local_hostnames
    return {
        normalized
        for raw in configured
        if (normalized := _normalize_host(str(raw)))
    }


def _is_direct_tailnet_request(scope: Scope, host: str) -> bool:
    return _is_tailnet_host(host) and _client_is_tailscale(scope)


def _is_tailnet_host(host: str) -> bool:
    return host.endswith(_TAILNET_HOST_SUFFIX) or _is_tailscale_ip(host)


def _client_is_tailscale(scope: Scope) -> bool:
    client = scope.get("client")
    if not client:
        return False
    host = str(client[0])
    return _is_tailscale_ip(host)


def _is_tailscale_ip(value: str) -> bool:
    try:
        ip = ipaddress.ip_address(value)
    except ValueError:
        return False
    return any(ip in network for network in _TAILSCALE_NETWORKS)


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
