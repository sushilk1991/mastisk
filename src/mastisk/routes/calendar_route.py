from __future__ import annotations

import html
import secrets
import time
from datetime import date
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from mastisk.google_calendar import (
    CalendarAuthError,
    CalendarSyncError,
    calendar_status,
    clear_calendar_connection,
    events_for_day,
    exchange_authorization_code,
    make_authorization_url,
    make_pkce_pair,
    sync_calendar,
    write_calendar_tokens,
)
from mastisk.settings import get_settings, reload_settings, update_toml_key

router = APIRouter(tags=["calendar"])
_oauth_pending: dict[str, dict[str, str | float]] = {}
_OAUTH_TTL_SECONDS = 10 * 60


class CalendarCredentials(BaseModel):
    client_id: str = Field(min_length=1, max_length=500)
    client_secret: str = Field(min_length=1, max_length=500)


@router.get("/calendar/status")
def get_calendar_status() -> dict[str, str | bool | None]:
    return _calendar_status_payload()


@router.put("/calendar/config")
def save_calendar_config(req: CalendarCredentials) -> dict:
    update_toml_key("calendar", "client_id", req.client_id.strip())
    update_toml_key("calendar", "client_secret", req.client_secret.strip())
    reload_settings()
    return {"ok": True, "status": _calendar_status_payload()}


@router.post("/calendar/connection/start")
def start_calendar_connection(request: Request) -> dict[str, str]:
    calendar = reload_settings().calendar
    if not calendar.client_id or not calendar.client_secret:
        raise HTTPException(
            status_code=409,
            detail="Google OAuth credentials are missing. Add a Desktop app client ID and secret first.",
        )
    _prune_oauth_states()
    code_verifier, code_challenge = make_pkce_pair()
    state = secrets.token_urlsafe(24)
    port = request.url.port or 5555
    redirect_uri = f"http://127.0.0.1:{port}/api/calendar/connection/callback"
    _oauth_pending[state] = {
        "code_verifier": code_verifier,
        "redirect_uri": redirect_uri,
        "created_at": time.monotonic(),
    }
    return {
        "authorization_url": make_authorization_url(
            client_id=calendar.client_id,
            redirect_uri=redirect_uri,
            state=state,
            code_challenge=code_challenge,
        ),
        "redirect_uri": redirect_uri,
    }


@router.get(
    "/calendar/connection/callback",
    response_class=HTMLResponse,
    name="calendar_oauth_callback",
)
def calendar_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
) -> HTMLResponse:
    _prune_oauth_states()
    pending = _oauth_pending.pop(state or "", None)
    if error:
        return _oauth_result_page(False, f"Google declined access: {error}", status_code=400)
    if not code or pending is None:
        return _oauth_result_page(False, "This calendar connection link is invalid or expired.", status_code=400)

    calendar = get_settings().calendar
    try:
        tokens = exchange_authorization_code(
            client_id=calendar.client_id,
            client_secret=calendar.client_secret,
            code=code,
            redirect_uri=str(pending["redirect_uri"]),
            code_verifier=str(pending["code_verifier"]),
        )
        write_calendar_tokens(tokens, replace_connection=True)
        try:
            sync_calendar()
            message = "Google Calendar is connected and synced."
        except (CalendarAuthError, CalendarSyncError):
            message = "Google Calendar is connected. Return to Mastisk and try Sync now."
    except CalendarAuthError as exc:
        return _oauth_result_page(False, f"Calendar connection failed: {exc}", status_code=502)
    return _oauth_result_page(True, message)


def _prune_oauth_states() -> None:
    cutoff = time.monotonic() - _OAUTH_TTL_SECONDS
    for state, pending in list(_oauth_pending.items()):
        if float(pending["created_at"]) < cutoff:
            _oauth_pending.pop(state, None)


def _calendar_status_payload() -> dict[str, str | bool | None]:
    calendar = get_settings().calendar
    return {
        **calendar_status(),
        "credentials_configured": bool(calendar.client_id and calendar.client_secret),
    }


def _oauth_result_page(ok: bool, message: str, *, status_code: int = 200) -> HTMLResponse:
    title = "Calendar connected" if ok else "Calendar connection failed"
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    js_ok = "true" if ok else "false"
    content = f"""<!doctype html>
<html><head><meta charset=\"utf-8\"><title>{safe_title}</title></head>
<body style=\"font-family:system-ui;padding:48px;max-width:560px;margin:auto\">
  <h1>{safe_title}</h1><p>{safe_message}</p><p>You can close this window.</p>
  <script>
    window.opener?.postMessage({{type:'mastisk-calendar-connected', ok:{js_ok}}}, '*');
    if ({js_ok}) window.setTimeout(() => window.close(), 600);
  </script>
</body></html>"""
    return HTMLResponse(content=content, status_code=status_code)


@router.get("/calendar/today")
def get_today_calendar(date_: Annotated[date | None, Query(alias="date")] = None) -> dict:
    day = date_ or date.today()
    status = _calendar_status_payload()
    return {
        "date": day.isoformat(),
        "status": status,
        "events": events_for_day(day) if status["status"] == "connected" else [],
    }


@router.post("/calendar/sync")
def force_calendar_sync() -> dict:
    try:
        result = sync_calendar()
    except CalendarAuthError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except CalendarSyncError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {"ok": True, **result, "status": _calendar_status_payload()}


@router.delete("/calendar/connection")
def delete_calendar_connection() -> dict[str, bool]:
    clear_calendar_connection()
    return {"ok": True}
