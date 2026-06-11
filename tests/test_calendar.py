from __future__ import annotations

import json
import logging
import stat
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest
from fastapi.testclient import TestClient


def _write_calendar_config(
    data_tmp: Path,
    *,
    client_id: str = "client-id",
    client_secret: str = "client-secret",
    calendar_ids: list[str] | None = None,
    sync_interval_minutes: int = 15,
) -> None:
    ids = calendar_ids or []
    ids_toml = "[" + ", ".join(json.dumps(v) for v in ids) + "]"
    (data_tmp / "config.toml").write_text(
        "[capture]\n"
        'default_timezone = "Asia/Kolkata"\n'
        "[calendar]\n"
        f"client_id = {json.dumps(client_id)}\n"
        f"client_secret = {json.dumps(client_secret)}\n"
        f"calendar_ids = {ids_toml}\n"
        f"sync_interval_minutes = {sync_interval_minutes}\n",
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def test_calendar_oauth_exchange_refresh_and_token_file_permissions(data_tmp, db):
    _write_calendar_config(data_tmp)
    calls: list[tuple[str, dict[str, str]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://oauth2.googleapis.com/token"
        form = dict(httpx.QueryParams(request.content.decode()))
        calls.append((request.method, form))
        if form["grant_type"] == "authorization_code":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-1",
                    "refresh_token": "refresh-1",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                    "scope": "https://www.googleapis.com/auth/calendar.readonly",
                },
            )
        if form["grant_type"] == "refresh_token":
            return httpx.Response(
                200,
                json={
                    "access_token": "access-2",
                    "expires_in": 1800,
                    "token_type": "Bearer",
                    "scope": "https://www.googleapis.com/auth/calendar.readonly",
                },
            )
        raise AssertionError(form)

    http_client = httpx.Client(transport=httpx.MockTransport(handler))

    from mastisk.google_calendar import (
        CALENDAR_READONLY_SCOPE,
        exchange_authorization_code,
        read_calendar_tokens,
        refresh_access_token,
        token_file_path,
        write_calendar_tokens,
    )

    tokens = exchange_authorization_code(
        client_id="client-id",
        client_secret="client-secret",
        code="oauth-code",
        redirect_uri="http://127.0.0.1:49152",
        code_verifier="verifier",
        http_client=http_client,
        now=datetime(2026, 6, 12, 8, 0, tzinfo=ZoneInfo("UTC")),
    )
    assert tokens["scope"] == CALENDAR_READONLY_SCOPE
    assert tokens["refresh_token"] == "refresh-1"
    write_calendar_tokens(tokens)

    mode = stat.S_IMODE(token_file_path().stat().st_mode)
    assert mode == 0o600
    assert read_calendar_tokens()["access_token"] == "access-1"

    refreshed = refresh_access_token(
        read_calendar_tokens(),
        client_id="client-id",
        client_secret="client-secret",
        http_client=http_client,
        now=datetime(2026, 6, 12, 9, 0, tzinfo=ZoneInfo("UTC")),
    )
    assert refreshed["access_token"] == "access-2"
    assert refreshed["refresh_token"] == "refresh-1"
    assert calls == [
        (
            "POST",
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "code": "oauth-code",
                "code_verifier": "verifier",
                "grant_type": "authorization_code",
                "redirect_uri": "http://127.0.0.1:49152",
            },
        ),
        (
            "POST",
            {
                "client_id": "client-id",
                "client_secret": "client-secret",
                "grant_type": "refresh_token",
                "refresh_token": "refresh-1",
            },
        ),
    ]


def test_calendar_sync_upserts_prunes_and_only_reads_google_calendar(data_tmp, db):
    _write_calendar_config(data_tmp, calendar_ids=["work@example.com"])
    seen: list[httpx.Request] = []
    sync_round = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "www.googleapis.com":
            assert request.method == "GET"
            params = dict(request.url.params)
            assert params["singleEvents"] == "true"
            assert params["orderBy"] == "startTime"
            assert "timeMin" in params
            assert "timeMax" in params
            if request.url.path.endswith("/primary/events"):
                items = [
                    {
                        "id": "timed-1",
                        "summary": "Standup",
                        "start": {"dateTime": "2026-06-12T10:00:00+05:30"},
                        "end": {"dateTime": "2026-06-12T10:30:00+05:30"},
                        "location": "Meet",
                        "status": "confirmed",
                        "updated": "2026-06-10T01:00:00Z",
                    }
                ]
                if sync_round["n"] == 0:
                    items.append(
                        {
                            "id": "all-day-1",
                            "summary": "Travel day",
                            "start": {"date": "2026-06-12"},
                            "end": {"date": "2026-06-13"},
                            "status": "confirmed",
                            "updated": "2026-06-09T01:00:00Z",
                        }
                    )
                return httpx.Response(200, json={"items": items})
            if request.url.path.endswith("/work@example.com/events"):
                return httpx.Response(
                    200,
                    json={
                        "items": [
                            {
                                "id": "work-1",
                                "summary": "Client review",
                                "start": {"dateTime": "2026-06-12T15:00:00+05:30"},
                                "end": {"dateTime": "2026-06-12T16:00:00+05:30"},
                                "status": "confirmed",
                                "updated": "2026-06-11T01:00:00Z",
                            }
                        ]
                    },
                )
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    from mastisk.google_calendar import sync_calendar, write_calendar_tokens

    write_calendar_tokens(
        {
            "access_token": "access-ok",
            "refresh_token": "refresh-ok",
            "expires_at": 4102444800,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
        }
    )
    db.execute(
        """INSERT INTO calendar_events
           (id, calendar_id, summary, start, end, all_day, status, updated_at, synced_at)
           VALUES
           ('old-cache', 'primary', 'Old cached event',
            '2026-06-09T10:00:00+00:00', '2026-06-10T18:29:00+00:00',
            0, 'confirmed', '2026-06-09T00:00:00Z', '2026-06-09T08:00:00+05:30')"""
    )
    now = datetime(2026, 6, 12, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    result = sync_calendar(now=now, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert result["event_count"] == 3

    rows = db.execute(
        "SELECT calendar_id, id, summary, start, end, all_day FROM calendar_events ORDER BY calendar_id, id"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "calendar_id": "primary",
            "id": "all-day-1",
            "summary": "Travel day",
            "start": "2026-06-11T18:30:00+00:00",
            "end": "2026-06-12T18:30:00+00:00",
            "all_day": 1,
        },
        {
            "calendar_id": "primary",
            "id": "timed-1",
            "summary": "Standup",
            "start": "2026-06-12T04:30:00+00:00",
            "end": "2026-06-12T05:00:00+00:00",
            "all_day": 0,
        },
        {
            "calendar_id": "work@example.com",
            "id": "work-1",
            "summary": "Client review",
            "start": "2026-06-12T09:30:00+00:00",
            "end": "2026-06-12T10:30:00+00:00",
            "all_day": 0,
        },
    ]

    sync_round["n"] = 1
    result = sync_calendar(now=now, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert result["event_count"] == 2
    remaining = db.execute("SELECT id FROM calendar_events ORDER BY id").fetchall()
    assert [row["id"] for row in remaining] == ["timed-1", "work-1"]
    assert all(
        req.method == "GET"
        for req in seen
        if req.url.host == "www.googleapis.com"
    )


def test_expired_refresh_marks_disconnected_and_today_stays_loud_but_200(
    vault_tmp, data_tmp, db
):
    _write_calendar_config(data_tmp)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "oauth2.googleapis.com"
        return httpx.Response(400, json={"error": "invalid_grant"})

    from mastisk.google_calendar import CalendarAuthError, sync_calendar, write_calendar_tokens

    write_calendar_tokens(
        {
            "access_token": "expired",
            "refresh_token": "bad-refresh",
            "expires_at": 0,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
        }
    )
    with pytest.raises(CalendarAuthError):
        sync_calendar(
            now=datetime(2026, 6, 12, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    with _client(vault_tmp, data_tmp, db) as client:
        today = client.get("/api/calendar/today")
        assert today.status_code == 200, today.text
        assert today.json()["events"] == []
        assert today.json()["status"]["status"] == "disconnected"
        assert "invalid_grant" in today.json()["status"]["error"]


def test_calendar_sync_failure_persists_last_error_without_disconnect(
    vault_tmp, data_tmp, db
):
    _write_calendar_config(data_tmp)
    now = datetime(2026, 6, 12, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    mode = {"failure": True}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.googleapis.com"
        if mode["failure"]:
            return httpx.Response(500, json={"error": "quota exhausted"})
        return httpx.Response(200, json={"items": []})

    from mastisk.google_calendar import (
        CalendarSyncError,
        mark_calendar_connected,
        sync_calendar,
        write_calendar_tokens,
    )

    write_calendar_tokens(
        {
            "access_token": "access-ok",
            "refresh_token": "refresh-ok",
            "expires_at": 4102444800,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
        }
    )
    mark_calendar_connected("2026-06-11T08:00:00+05:30")

    with pytest.raises(CalendarSyncError):
        sync_calendar(now=now, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with _client(vault_tmp, data_tmp, db) as client:
        status = client.get("/api/calendar/status")
    assert status.status_code == 200
    assert status.json()["status"] == "connected"
    assert status.json()["last_synced_at"] == "2026-06-11T08:00:00+05:30"
    assert "quota exhausted" in status.json()["last_error"]
    assert status.json()["last_error_at"] == now.isoformat()

    mode["failure"] = False
    sync_calendar(now=now, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    with _client(vault_tmp, data_tmp, db) as client:
        healed = client.get("/api/calendar/status")
    assert healed.status_code == 200
    assert healed.json()["status"] == "connected"
    assert healed.json()["last_error"] is None
    assert healed.json()["last_error_at"] is None


def test_calendar_status_transitions_unconfigured_not_synced_connected(
    vault_tmp, data_tmp, db, monkeypatch
):
    _write_calendar_config(data_tmp)
    from mastisk.google_calendar import write_calendar_tokens

    with _client(vault_tmp, data_tmp, db) as client:
        assert client.get("/api/calendar/status").json()["status"] == "unconfigured"

        write_calendar_tokens(
            {
                "access_token": "access-ok",
                "refresh_token": "refresh-ok",
                "expires_at": 4102444800,
                "token_type": "Bearer",
                "scope": "https://www.googleapis.com/auth/calendar.readonly",
            }
        )
        not_synced = client.get("/api/calendar/status")
        assert not_synced.status_code == 200
        assert not_synced.json()["status"] == "not_synced"
        assert not_synced.json()["last_synced_at"] is None

        today = client.get("/api/calendar/today?date=2026-06-12")
        assert today.status_code == 200
        assert today.json()["status"]["status"] == "not_synced"
        assert today.json()["events"] == []

        from mastisk.routes import calendar_route

        def fake_sync_calendar():
            from mastisk.google_calendar import mark_calendar_connected

            mark_calendar_connected("2026-06-12T08:00:00+05:30")
            return {"event_count": 0, "calendar_count": 1}

        monkeypatch.setattr(calendar_route, "sync_calendar", fake_sync_calendar)
        forced = client.post("/api/calendar/sync")
        assert forced.status_code == 200, forced.text
        assert forced.json()["status"]["status"] == "connected"


def test_calendar_sync_failure_rolls_back_partial_calendar_writes(data_tmp, db):
    _write_calendar_config(data_tmp, calendar_ids=["work@example.com"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "www.googleapis.com"
        if request.url.path.endswith("/primary/events"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "primary-1",
                            "summary": "Primary event",
                            "start": {"dateTime": "2026-06-12T10:00:00+05:30"},
                            "end": {"dateTime": "2026-06-12T10:30:00+05:30"},
                        }
                    ]
                },
            )
        if request.url.path.endswith("/work@example.com/events"):
            return httpx.Response(404, json={"error": "bad calendar id"})
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    from mastisk.google_calendar import (
        CalendarSyncError,
        calendar_status,
        sync_calendar,
        write_calendar_tokens,
    )

    write_calendar_tokens(
        {
            "access_token": "access-ok",
            "refresh_token": "refresh-ok",
            "expires_at": 4102444800,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
        }
    )

    with pytest.raises(CalendarSyncError):
        sync_calendar(
            now=datetime(2026, 6, 12, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata")),
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    assert db.execute("SELECT COUNT(*) AS n FROM calendar_events").fetchone()["n"] == 0
    status = calendar_status()
    assert status["status"] == "not_synced"
    assert status["last_synced_at"] is None
    assert "bad calendar id" in status["last_error"]


def test_calendar_routes_status_force_sync_disconnect_and_sorted_today(
    vault_tmp, data_tmp, db, monkeypatch
):
    _write_calendar_config(data_tmp)
    from mastisk.google_calendar import write_calendar_tokens

    with _client(vault_tmp, data_tmp, db) as client:
        assert client.get("/api/calendar/status").json()["status"] == "unconfigured"

        write_calendar_tokens(
            {
                "access_token": "access-ok",
                "refresh_token": "refresh-ok",
                "expires_at": 4102444800,
                "token_type": "Bearer",
                "scope": "https://www.googleapis.com/auth/calendar.readonly",
            }
        )

        from mastisk.routes import calendar_route

        def fake_sync_calendar():
            from mastisk.google_calendar import mark_calendar_connected

            db.execute(
                """INSERT INTO calendar_events
                   (id, calendar_id, summary, start, end, all_day, location, status, updated_at, synced_at)
                   VALUES
                   ('late', 'primary', 'Late call', '2026-06-12T18:00:00+05:30', '2026-06-12T18:30:00+05:30', 0, NULL, 'confirmed', '2026-06-11T00:00:00Z', '2026-06-12T08:00:00+05:30'),
                   ('all', 'primary', 'No school', '2026-06-12', '2026-06-13', 1, NULL, 'confirmed', '2026-06-11T00:00:00Z', '2026-06-12T08:00:00+05:30')"""
            )
            mark_calendar_connected("2026-06-12T08:00:00+05:30")
            return {"event_count": 2, "calendar_count": 1}

        monkeypatch.setattr(calendar_route, "sync_calendar", fake_sync_calendar)

        forced = client.post("/api/calendar/sync")
        assert forced.status_code == 200, forced.text
        assert forced.json()["event_count"] == 2

        status = client.get("/api/calendar/status")
        assert status.status_code == 200
        assert status.json()["status"] == "connected"
        assert status.json()["last_synced_at"] == "2026-06-12T08:00:00+05:30"

        today = client.get("/api/calendar/today?date=2026-06-12")
        assert today.status_code == 200, today.text
        assert [event["id"] for event in today.json()["events"]] == ["all", "late"]

        disconnected = client.delete("/api/calendar/connection")
        assert disconnected.status_code == 200
        assert client.get("/api/calendar/status").json()["status"] == "unconfigured"
        assert client.get("/api/calendar/today?date=2026-06-12").json()["events"] == []
        assert db.execute("SELECT COUNT(*) AS n FROM calendar_events").fetchone()["n"] == 0


def test_calendar_today_filters_timed_events_by_actual_timezone_day(
    vault_tmp, data_tmp, db
):
    _write_calendar_config(data_tmp)
    from mastisk.google_calendar import mark_calendar_connected, write_calendar_tokens

    write_calendar_tokens(
        {
            "access_token": "access-ok",
            "refresh_token": "refresh-ok",
            "expires_at": 4102444800,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
        }
    )
    db.execute(
        """INSERT INTO calendar_events
           (id, calendar_id, summary, start, end, all_day, status, updated_at, synced_at)
           VALUES
           ('la-night', 'primary', 'LA evening call',
            '2026-06-11T20:00:00-07:00', '2026-06-11T20:30:00-07:00',
            0, 'confirmed', '2026-06-11T00:00:00Z', '2026-06-12T08:00:00+05:30'),
           ('asia-prev', 'primary', 'Earlier local day',
            '2026-06-11T20:00:00+05:30', '2026-06-11T20:30:00+05:30',
            0, 'confirmed', '2026-06-11T00:00:00Z', '2026-06-12T08:00:00+05:30')"""
    )
    mark_calendar_connected("2026-06-12T08:00:00+05:30")

    with _client(vault_tmp, data_tmp, db) as client:
        today = client.get("/api/calendar/today?date=2026-06-12")

    assert today.status_code == 200, today.text
    assert [event["id"] for event in today.json()["events"]] == ["la-night"]


def test_calendar_corrupt_token_file_reports_disconnected(data_tmp, db):
    _write_calendar_config(data_tmp)
    token_file = data_tmp / "calendar_tokens.json"
    token_file.write_text("{not-json", encoding="utf-8")

    from mastisk.google_calendar import calendar_status

    assert calendar_status() == {
        "status": "disconnected",
        "last_synced_at": None,
        "error": "calendar token file unreadable",
        "last_error": None,
        "last_error_at": None,
    }


def test_calendar_reconnect_does_not_show_old_cache_until_new_sync(
    vault_tmp, data_tmp, db
):
    _write_calendar_config(data_tmp)
    from mastisk.google_calendar import (
        clear_calendar_connection,
        mark_calendar_connected,
        write_calendar_tokens,
    )

    write_calendar_tokens(
        {
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "expires_at": 4102444800,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
        }
    )
    db.execute(
        """INSERT INTO calendar_events
           (id, calendar_id, summary, start, end, all_day, status, updated_at, synced_at)
           VALUES
           ('old', 'primary', 'Old account event', '2026-06-12', '2026-06-13',
            1, 'confirmed', '2026-06-11T00:00:00Z', '2026-06-12T08:00:00+05:30')"""
    )
    mark_calendar_connected("2026-06-12T08:00:00+05:30")
    clear_calendar_connection()
    assert db.execute("SELECT COUNT(*) AS n FROM calendar_events").fetchone()["n"] == 0
    write_calendar_tokens(
        {
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_at": 4102444800,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
        }
    )
    mark_calendar_connected("2026-06-13T08:00:00+05:30")

    with _client(vault_tmp, data_tmp, db) as client:
        today = client.get("/api/calendar/today?date=2026-06-12")

    assert today.status_code == 200, today.text
    assert today.json()["status"]["status"] == "connected"
    assert today.json()["events"] == []


@pytest.mark.asyncio
async def test_scheduler_registers_calendar_sync_only_when_token_exists(
    data_tmp, db, monkeypatch, caplog
):
    _write_calendar_config(data_tmp, sync_interval_minutes=7)
    from mastisk import scheduler
    from mastisk.google_calendar import write_calendar_tokens

    monkeypatch.setattr(scheduler, "_reclaim_orphaned_running", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_running_blog_posts", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_running_tweet_threads", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_firing_reminders", lambda: None)
    monkeypatch.setattr(scheduler, "_graph_repair_once", lambda: None)

    class FakeScheduler:
        def __init__(self, timezone):
            self.timezone = timezone
            self.jobs: dict[str | None, dict] = {}

        def add_job(self, func, trigger, **kwargs):
            self.jobs[kwargs.get("id")] = {"trigger": trigger, **kwargs}

        def start(self):
            return None

        def shutdown(self, wait=False):
            return None

    monkeypatch.setattr(scheduler, "AsyncIOScheduler", FakeScheduler)

    without_token = await scheduler.start_scheduler()
    assert "calendar_sync" not in without_token.jobs

    write_calendar_tokens(
        {
            "access_token": "access-ok",
            "refresh_token": "refresh-ok",
            "expires_at": 4102444800,
            "token_type": "Bearer",
            "scope": "https://www.googleapis.com/auth/calendar.readonly",
        }
    )
    with caplog.at_level(logging.INFO, logger="mastisk.scheduler"):
        with_token = await scheduler.start_scheduler()

    assert with_token.jobs["calendar_sync"]["minutes"] == 7
    assert "scheduler: calendar_sync registered (7min tick)" in caplog.text


def test_scheduled_calendar_sync_noops_once_when_token_removed(
    data_tmp, db, caplog
):
    _write_calendar_config(data_tmp)
    from mastisk import scheduler

    scheduler._calendar_sync_missing_token_logged = False

    with caplog.at_level(logging.INFO, logger="mastisk.scheduler"):
        scheduler._scheduled_calendar_sync()
        scheduler._scheduled_calendar_sync()

    assert caplog.text.count("scheduler: calendar_sync skipped (no token)") == 1
