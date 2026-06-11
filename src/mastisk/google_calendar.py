"""Read-only Google Calendar integration.

Uses plain ``httpx`` against the documented OAuth token endpoint and Calendar
``events.list`` REST endpoint. No Google SDK is needed for this narrow surface.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import httpx

from mastisk.db.queries import connect, init_schema, txn
from mastisk.paths import data_dir
from mastisk.settings import get_settings

CALENDAR_READONLY_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALENDAR_API = "https://www.googleapis.com/calendar/v3"


class CalendarAuthError(RuntimeError):
    """OAuth token exchange or refresh failed."""


class CalendarSyncError(RuntimeError):
    """Google Calendar read failed for a non-auth reason."""


def token_file_path() -> Path:
    return data_dir() / "calendar_tokens.json"


def calendar_token_exists() -> bool:
    return token_file_path().exists()


def read_calendar_tokens() -> dict[str, Any] | None:
    path = token_file_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_calendar_tokens(tokens: dict[str, Any], *, replace_connection: bool = False) -> Path:
    path = token_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if replace_connection:
        _clear_calendar_cache_and_state()
    # Phase 9 deviation from the spec's "encrypted in data dir": tokens are
    # protected by local single-user file permissions (0600). The headless
    # launchctl daemon would hit Keychain ACL prompts during background sync,
    # while FileVault covers at-rest disk encryption for this local Mac model.
    # Keep this plaintext unless the daemon auth flow is redesigned.
    fd, tmp = tempfile.mkstemp(prefix=".calendar_tokens.", suffix=".json", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(tokens, f, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
        path.chmod(0o600)
    except Exception:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


def delete_calendar_tokens() -> None:
    token_file_path().unlink(missing_ok=True)


def make_pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def make_authorization_url(
    *,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": CALENDAR_READONLY_SCOPE,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_authorization_code(
    *,
    client_id: str,
    client_secret: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "code_verifier": code_verifier,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }
    return _token_request(form, http_client=http_client, now=now, require_refresh=True)


def refresh_access_token(
    tokens: dict[str, Any] | None,
    *,
    client_id: str,
    client_secret: str,
    http_client: httpx.Client | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    if not tokens or not tokens.get("refresh_token"):
        mark_calendar_disconnected("missing refresh token")
        raise CalendarAuthError("missing refresh token")
    form = {
        "client_id": client_id,
        "client_secret": client_secret,
        "grant_type": "refresh_token",
        "refresh_token": str(tokens["refresh_token"]),
    }
    try:
        refreshed = _token_request(form, http_client=http_client, now=now, require_refresh=False)
    except CalendarAuthError as e:
        mark_calendar_disconnected(str(e))
        raise
    refreshed["refresh_token"] = refreshed.get("refresh_token") or tokens["refresh_token"]
    write_calendar_tokens(refreshed)
    return refreshed


def _token_request(
    form: dict[str, str],
    *,
    http_client: httpx.Client | None,
    now: datetime | None,
    require_refresh: bool,
) -> dict[str, Any]:
    client = http_client or httpx.Client(timeout=15.0)
    close = http_client is None
    try:
        resp = client.post(TOKEN_URL, data=form)
    except httpx.HTTPError as e:
        raise CalendarAuthError(f"token request failed: {e}") from e
    finally:
        if close:
            client.close()
    if not resp.is_success:
        detail = _response_error(resp)
        raise CalendarAuthError(detail)
    data = resp.json()
    scope = str(data.get("scope") or "")
    if CALENDAR_READONLY_SCOPE not in scope.split():
        raise CalendarAuthError("Google did not grant calendar.readonly scope")
    if require_refresh and not data.get("refresh_token"):
        raise CalendarAuthError("Google did not return a refresh token")
    issued_at = now or datetime.now(UTC)
    expires_in = int(data.get("expires_in") or 0)
    data["expires_at"] = int(issued_at.timestamp()) + max(expires_in, 0)
    return data


def sync_calendar(
    *,
    now: datetime | None = None,
    http_client: httpx.Client | None = None,
) -> dict[str, int]:
    init_schema()
    settings = get_settings()
    tokens = read_calendar_tokens()
    if not tokens:
        if calendar_token_exists():
            mark_calendar_disconnected("calendar token file unreadable")
            raise CalendarAuthError("calendar token file unreadable")
        raise CalendarAuthError("calendar connection is unconfigured")
    now_dt = now or datetime.now(ZoneInfo(settings.capture.default_timezone))
    try:
        tokens = _valid_access_tokens(tokens, http_client=http_client, now=now_dt)
        calendar_ids = _calendar_ids(settings.calendar.calendar_ids)
        start, end = _sync_window(now_dt, ZoneInfo(settings.capture.default_timezone))
        synced_at = now_dt.isoformat()
        fetched_rows: dict[str, tuple[list[dict[str, Any]], list[str]]] = {}

        client = http_client or httpx.Client(timeout=20.0)
        close = http_client is None
        try:
            for calendar_id in calendar_ids:
                events = _fetch_events(
                    client=client,
                    tokens=tokens,
                    calendar_id=calendar_id,
                    time_min=start.isoformat(),
                    time_max=end.isoformat(),
                )
                rows: list[dict[str, Any]] = []
                seen_ids: list[str] = []
                for event in events:
                    row = _normalize_event(calendar_id, event, synced_at)
                    if row is None:
                        continue
                    rows.append(row)
                    seen_ids.append(row["id"])
                fetched_rows[calendar_id] = (rows, seen_ids)
        finally:
            if close:
                client.close()

        with connect() as conn, txn(conn):
            _prune_events_before(conn, start)
            for calendar_id, (rows, seen_ids) in fetched_rows.items():
                for row in rows:
                    _upsert_calendar_event(conn, row)
                _prune_missing_events(conn, calendar_id, start, end, seen_ids)
            _mark_calendar_connected(conn, synced_at)
    except CalendarSyncError as e:
        mark_calendar_sync_error(str(e), at=now_dt.isoformat())
        raise
    event_count = sum(len(rows) for rows, _seen_ids in fetched_rows.values())
    return {"calendar_count": len(calendar_ids), "event_count": event_count}


def calendar_status() -> dict[str, str | None]:
    init_schema()
    with connect() as conn:
        row = conn.execute("SELECT * FROM calendar_state WHERE id = 1").fetchone()
    state = dict(row) if row else {}
    tokens = read_calendar_tokens()
    if not calendar_token_exists():
        return {
            "status": "unconfigured",
            "last_synced_at": state.get("last_synced_at"),
            "error": None,
            "last_error": None,
            "last_error_at": None,
        }
    if tokens is None:
        return {
            "status": "disconnected",
            "last_synced_at": state.get("last_synced_at"),
            "error": "calendar token file unreadable",
            "last_error": state.get("last_error"),
            "last_error_at": state.get("last_error_at"),
        }
    if state.get("status") == "disconnected":
        return {
            "status": "disconnected",
            "last_synced_at": state.get("last_synced_at"),
            "error": state.get("error"),
            "last_error": state.get("last_error"),
            "last_error_at": state.get("last_error_at"),
        }
    if not state.get("last_synced_at"):
        return {
            "status": "not_synced",
            "last_synced_at": None,
            "error": state.get("error"),
            "last_error": state.get("last_error"),
            "last_error_at": state.get("last_error_at"),
        }
    return {
        "status": "connected",
        "last_synced_at": state.get("last_synced_at"),
        "error": state.get("error"),
        "last_error": state.get("last_error"),
        "last_error_at": state.get("last_error_at"),
    }


def mark_calendar_connected(last_synced_at: str) -> None:
    init_schema()
    with connect() as conn:
        _mark_calendar_connected(conn, last_synced_at)


def mark_calendar_disconnected(error: str) -> None:
    init_schema()
    with connect() as conn:
        conn.execute(
            """INSERT INTO calendar_state (id, status, error, updated_at)
               VALUES (1, 'disconnected', ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 status='disconnected',
                 error=excluded.error,
                 updated_at=CURRENT_TIMESTAMP""",
            (error,),
        )


def _mark_calendar_connected(conn, last_synced_at: str) -> None:
    conn.execute(
        """INSERT INTO calendar_state
             (id, status, last_synced_at, error, last_error, last_error_at, updated_at)
           VALUES (1, 'connected', ?, NULL, NULL, NULL, CURRENT_TIMESTAMP)
           ON CONFLICT(id) DO UPDATE SET
             status='connected',
             last_synced_at=excluded.last_synced_at,
             error=NULL,
             last_error=NULL,
             last_error_at=NULL,
             updated_at=CURRENT_TIMESTAMP""",
        (last_synced_at,),
    )


def mark_calendar_sync_error(error: str, *, at: str | None = None) -> None:
    init_schema()
    failed_at = at or datetime.now(UTC).isoformat()
    with connect() as conn:
        conn.execute(
            """INSERT INTO calendar_state
                 (id, status, last_error, last_error_at, updated_at)
               VALUES (1, 'not_synced', ?, ?, CURRENT_TIMESTAMP)
               ON CONFLICT(id) DO UPDATE SET
                 status=CASE
                   WHEN calendar_state.last_synced_at IS NULL THEN 'not_synced'
                   ELSE calendar_state.status
                 END,
                 last_error=excluded.last_error,
                 last_error_at=excluded.last_error_at,
                 updated_at=CURRENT_TIMESTAMP""",
            (error, failed_at),
        )


def clear_calendar_connection() -> None:
    delete_calendar_tokens()
    _clear_calendar_cache_and_state()


def events_for_day(day: date) -> list[dict[str, Any]]:
    status = calendar_status()
    if status["status"] != "connected" or not status["last_synced_at"]:
        return []
    tz = ZoneInfo(get_settings().capture.default_timezone)
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    start_key = _stored_datetime(start)
    end_key = _stored_datetime(end)
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, calendar_id, summary, start, end, all_day, location, status, updated_at, synced_at
               FROM calendar_events
               WHERE start < ? AND end > ?
               ORDER BY all_day DESC, start ASC, summary ASC""",
            (end_key, start_key),
        ).fetchall()
    matching = [
        dict(row) for row in rows
        if _row_overlaps_window(dict(row), start, end, tz)
    ]
    matching.sort(key=lambda row: (0 if row["all_day"] else 1, _event_sort_dt(row, tz), row["summary"]))
    return [_event_response(row) for row in matching]


def _valid_access_tokens(
    tokens: dict[str, Any],
    *,
    http_client: httpx.Client | None,
    now: datetime,
) -> dict[str, Any]:
    expires_at = int(tokens.get("expires_at") or 0)
    if expires_at > int(now.timestamp()) + 60:
        return tokens
    settings = get_settings()
    return refresh_access_token(
        tokens,
        client_id=settings.calendar.client_id,
        client_secret=settings.calendar.client_secret,
        http_client=http_client,
        now=now,
    )


def _fetch_events(
    *,
    client: httpx.Client,
    tokens: dict[str, Any],
    calendar_id: str,
    time_min: str,
    time_max: str,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    page_token: str | None = None
    for _ in range(100):
        params = {
            "timeMin": time_min,
            "timeMax": time_max,
            "singleEvents": "true",
            "orderBy": "startTime",
        }
        if page_token:
            params["pageToken"] = page_token
        resp = client.get(
            f"{CALENDAR_API}/calendars/{quote(calendar_id, safe='@._-')}/events",
            params=params,
            headers={
                "Authorization": f"Bearer {tokens['access_token']}",
                "Accept": "application/json",
            },
        )
        if resp.status_code == 401:
            settings = get_settings()
            tokens = refresh_access_token(
                tokens,
                client_id=settings.calendar.client_id,
                client_secret=settings.calendar.client_secret,
                http_client=client,
                now=datetime.now(UTC),
            )
            resp = client.get(
                f"{CALENDAR_API}/calendars/{quote(calendar_id, safe='@._-')}/events",
                params=params,
                headers={
                    "Authorization": f"Bearer {tokens['access_token']}",
                    "Accept": "application/json",
                },
            )
        if not resp.is_success:
            raise CalendarSyncError(f"Google Calendar returned {resp.status_code}: {_response_error(resp)}")
        payload = resp.json()
        items.extend(payload.get("items") or [])
        page_token = payload.get("nextPageToken")
        if not page_token:
            return items
    raise CalendarSyncError("Google Calendar pagination did not finish")


def _normalize_event(
    calendar_id: str,
    event: dict[str, Any],
    synced_at: str,
) -> dict[str, Any] | None:
    event_id = event.get("id")
    if not isinstance(event_id, str) or not event_id:
        return None
    start_raw = event.get("start") or {}
    end_raw = event.get("end") or {}
    all_day = isinstance(start_raw, dict) and "date" in start_raw
    start = start_raw.get("date") if all_day else start_raw.get("dateTime")
    end = end_raw.get("date") if all_day else end_raw.get("dateTime")
    if not isinstance(start, str) or not start:
        return None
    if not isinstance(end, str) or not end:
        end = start
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return {
        "id": event_id,
        "calendar_id": calendar_id,
        "summary": str(event.get("summary") or "(No title)"),
        "start": _normalize_event_time(start, all_day=all_day, tz=tz),
        "end": _normalize_event_time(end, all_day=all_day, tz=tz),
        "all_day": 1 if all_day else 0,
        "location": event.get("location") if isinstance(event.get("location"), str) else None,
        "status": event.get("status") if isinstance(event.get("status"), str) else None,
        "updated_at": event.get("updated") if isinstance(event.get("updated"), str) else None,
        "synced_at": synced_at,
    }


def _upsert_calendar_event(conn, row: dict[str, Any]) -> None:
    conn.execute(
        """INSERT INTO calendar_events
             (id, calendar_id, summary, start, end, all_day,
              location, status, updated_at, synced_at)
           VALUES
             (:id, :calendar_id, :summary, :start, :end, :all_day,
              :location, :status, :updated_at, :synced_at)
           ON CONFLICT(calendar_id, id) DO UPDATE SET
             summary=excluded.summary,
             start=excluded.start,
             end=excluded.end,
             all_day=excluded.all_day,
             location=excluded.location,
             status=excluded.status,
             updated_at=excluded.updated_at,
             synced_at=excluded.synced_at""",
        row,
    )


def _prune_events_before(conn, window_start: datetime) -> None:
    conn.execute("DELETE FROM calendar_events WHERE end < ?", (_stored_datetime(window_start),))


def _prune_missing_events(
    conn,
    calendar_id: str,
    time_min: datetime,
    time_max: datetime,
    seen_ids: list[str],
) -> None:
    params: list[Any] = [calendar_id, _stored_datetime(time_max), _stored_datetime(time_min)]
    seen_clause = ""
    if seen_ids:
        placeholders = ",".join("?" for _ in seen_ids)
        seen_clause = f" AND id NOT IN ({placeholders})"
        params.extend(seen_ids)
    conn.execute(
        f"""DELETE FROM calendar_events
            WHERE calendar_id = ?
              AND start < ?
              AND end > ?
              {seen_clause}""",
        params,
    )


def _event_response(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "calendar_id": row["calendar_id"],
        "summary": row["summary"],
        "start": row["start"],
        "end": row["end"],
        "all_day": bool(row["all_day"]),
        "location": row["location"],
        "status": row["status"],
        "updated_at": row["updated_at"],
        "synced_at": row["synced_at"],
    }


def _calendar_ids(extra: list[str]) -> list[str]:
    out: list[str] = []
    for calendar_id in ["primary", *extra]:
        if calendar_id and calendar_id not in out:
            out.append(calendar_id)
    return out


def _sync_window(now: datetime, tz: ZoneInfo) -> tuple[datetime, datetime]:
    local_now = now.astimezone(tz)
    today = local_now.date()
    start = datetime.combine(today - timedelta(days=1), time.min, tzinfo=tz)
    end = datetime.combine(today + timedelta(days=15), time.min, tzinfo=tz)
    return start, end


def _normalize_event_time(value: str, *, all_day: bool, tz: ZoneInfo) -> str:
    if all_day:
        parsed = datetime.combine(date.fromisoformat(value[:10]), time.min, tzinfo=tz)
    else:
        parsed = _parse_datetime(value, tz)
    return _stored_datetime(parsed)


def _stored_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _clear_calendar_cache_and_state() -> None:
    init_schema()
    with connect() as conn:
        conn.execute("DELETE FROM calendar_state WHERE id = 1")
        conn.execute("DELETE FROM calendar_events")


def _row_overlaps_window(row: dict[str, Any], start: datetime, end: datetime, tz: ZoneInfo) -> bool:
    event_start, event_end = _event_bounds(row, tz)
    return event_start < end and event_end > start


def _event_sort_dt(row: dict[str, Any], tz: ZoneInfo) -> datetime:
    return _event_bounds(row, tz)[0]


def _event_bounds(row: dict[str, Any], tz: ZoneInfo) -> tuple[datetime, datetime]:
    if row.get("all_day"):
        start_raw = str(row["start"])
        end_raw = str(row["end"])
        start = _parse_datetime(start_raw, tz) if "T" in start_raw else _parse_date_boundary(start_raw, tz)
        end = _parse_datetime(end_raw, tz) if "T" in end_raw else _parse_date_boundary(end_raw, tz)
        return start, end
    start = _parse_datetime(str(row["start"]), tz)
    end = _parse_datetime(str(row["end"]), tz)
    return start, end


def _parse_date_boundary(value: str, tz: ZoneInfo) -> datetime:
    return datetime.combine(date.fromisoformat(value[:10]), time.min, tzinfo=tz)


def _parse_datetime(value: str, tz: ZoneInfo) -> datetime:
    normalized = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed


def _response_error(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except ValueError:
        return resp.text[:200] or str(resp.status_code)
    if isinstance(data, dict):
        if isinstance(data.get("error_description"), str):
            return data["error_description"]
        if isinstance(data.get("error"), str):
            return data["error"]
    return str(resp.status_code)
