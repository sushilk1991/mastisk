"""Thin push notification wrapper."""
from __future__ import annotations

import logging
from email.header import Header

import httpx

from mastisk.settings import get_settings

log = logging.getLogger("mastisk.notify")


def send(title: str, body: str, url: str | None = None) -> bool:
    settings = get_settings().notify
    backend = settings.backend.lower().strip()
    try:
        if backend == "none":
            log.info("notify backend disabled; dropping notification %r", title)
            return False
        if backend == "pushover":
            return _send_pushover(body=body, url=url)
        if backend == "ntfy":
            return _send_ntfy(title=title, body=body)
        log.warning("unknown notify backend %r; dropping notification %r", settings.backend, title)
        return False
    except Exception as exc:
        log.warning("notify send failed via %s: %s", backend, exc)
        return False


def _send_pushover(*, body: str, url: str | None) -> bool:
    settings = get_settings().notify
    if not settings.pushover_token or not settings.pushover_user:
        log.warning("pushover notify backend missing token/user")
        return False
    payload = {
        "token": settings.pushover_token,
        "user": settings.pushover_user,
        "message": body,
    }
    if url:
        payload["url"] = url
    response = httpx.post(
        "https://api.pushover.net/1/messages.json",
        data=payload,
        timeout=10,
    )
    response.raise_for_status()
    return True


def _send_ntfy(*, title: str, body: str) -> bool:
    settings = get_settings().notify
    if not settings.ntfy_topic:
        log.warning("ntfy notify backend missing topic")
        return False
    server = settings.ntfy_server.rstrip("/")
    topic = settings.ntfy_topic.strip("/")
    response = httpx.post(
        f"{server}/{topic}",
        content=body.encode("utf-8"),
        headers={"Title": _header_value(title)},
        timeout=10,
    )
    response.raise_for_status()
    return True


def _header_value(value: str) -> str:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return Header(value, "utf-8").encode()
    return value
