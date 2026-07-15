from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _capture(**overrides):
    from mastisk.capture.router import Capture

    data = {
        "type": "note",
        "confidence": 0.91,
        "title": None,
        "body": overrides.pop("body", "captured body"),
        "domain": None,
        "project": None,
        "person": None,
        "due": None,
        "scheduled": None,
        "priority": None,
        "recurrence": None,
        "reminder_lead_minutes": None,
        "no_reminder": False,
        "review_at": None,
        "tags": [],
        "related": [],
    }
    data.update(overrides)
    return Capture(**data)


def _client(data_tmp, *, config: str = "") -> TestClient:
    (data_tmp / "config.toml").write_text(
        '[capture]\nbearer_token = "test-token"\ndefault_timezone = "America/Los_Angeles"\n'
        + config,
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def test_e2e_watch_capture_task_due_reminder_and_filters(db, data_tmp, vault_tmp, monkeypatch):
    async def fake_route(text: str, source: str, ts: str | None):
        assert source == "watch"
        return _capture(
            type="task",
            confidence=0.95,
            body="change the water filter",
            domain="home",
            due="2026-06-13T14:00:00-07:00",
            reminder_lead_minutes=0,
            no_reminder=False,
            command_detected=True,
        )

    sent: list[tuple[str, str, str | None]] = []
    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda title, body, url=None: sent.append((title, body, url)) or True,
    )
    from mastisk.agents.reminder_engine import create_task_due_reminder

    monkeypatch.setattr(
        "mastisk.routes.capture.create_task_due_reminder",
        lambda **kwargs: create_task_due_reminder(
            **kwargs,
            now=datetime(2026, 6, 12, 15, 0, tzinfo=UTC),
        ),
    )

    with patch("mastisk.routes.capture.route_capture", new_callable=AsyncMock, side_effect=fake_route):
        client = _client(
            data_tmp,
            config='[domains]\nnames = ["home"]\n[notify]\nbackend = "ntfy"\nntfy_topic = "mastisk"\n',
        )
        response = client.post(
            "/api/capture",
            json={
                "text": "change the water filter at 2pm tomorrow, remind me",
                "source": "watch",
                "ts": "2026-06-12T08:00:00-07:00",
            },
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 201, response.text
    uid = response.json()["id"]
    task = db.execute("SELECT * FROM tasks WHERE uid = ?", (uid,)).fetchone()
    assert task["text"] == "change the water filter"
    assert task["domain"] == "home"
    assert task["due"] == "2026-06-13T14:00:00"
    assert task["reminder_lead_minutes"] == 0
    host_text = (vault_tmp / task["host_path"]).read_text(encoding="utf-8")
    assert "📅 2026-06-13 ⏰ 14:00" in host_text

    due_today = client.get("/api/tasks", params={"status": "open", "due_before": "2026-06-14"})
    upcoming = client.get("/api/tasks", params={"status": "open", "due_before": "2026-06-20"})
    assert [row["uid"] for row in due_today.json()] == [uid]
    assert [row["uid"] for row in upcoming.json()] == [uid]
    reminder = db.execute(
        "SELECT status, fire_at, lead_minutes FROM reminders WHERE entity_id = ?",
        (uid,),
    ).fetchone()
    assert dict(reminder) == {
        "status": "pending",
        "fire_at": "2026-06-13T21:00:00+00:00",
        "lead_minutes": 0,
    }

    from mastisk.agents.reminder_engine import reminder_tick

    assert reminder_tick(now=datetime(2026, 6, 13, 21, 0, tzinfo=UTC), ensure_daily_summary=False) == 1
    assert sent == [("Task due", "change the water filter", None)]
    assert db.execute("SELECT status FROM reminders WHERE entity_id = ?", (uid,)).fetchone()["status"] == "sent"


def test_e2e_routine_today_toggle_and_progress(db, data_tmp, vault_tmp):
    client = _client(data_tmp)

    created = client.post("/api/routines", json={"name": "Morning Vitamins", "time_of_day": "morning"})
    assert created.status_code == 201, created.text
    morning = client.get("/api/routines").json()["morning"]
    assert [row["slug"] for row in morning] == ["morning-vitamins"]

    toggled = client.post("/api/routines/morning-vitamins/toggle?date=2026-06-12")
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["streak"]["current"] == 1
    progress = client.get("/api/routines/morning-vitamins/progress")
    assert progress.json()["completion_dates"] == ["2026-06-12"]
    assert "- 2026-06-12" in (vault_tmp / "routines" / "morning-vitamins.md").read_text(encoding="utf-8")


def test_e2e_kindle_import_book_quotes_files_and_thought(db, data_tmp, vault_tmp):
    client = _client(data_tmp)
    fixture = (
        "Designing Data-Intensive Applications (Martin Kleppmann)\n"
        "- Your Highlight on page 42 | location 100-101 | Added on Friday, June 12, 2026 8:00:00 AM\n"
        "\n"
        "Reliable systems compound through explicit logs.\n"
        "==========\n"
    )
    imported = client.post(
        "/api/import/kindle",
        files={"file": ("My Clippings.txt", fixture.encode("utf-8"), "text/plain")},
    )
    assert imported.status_code == 200, imported.text
    assert imported.json()["imported"] == 1

    books = client.get("/api/books").json()
    assert books[0]["slug"] == "designing-data-intensive-applications"
    quotes = client.get("/api/quotes").json()
    assert len(quotes) == 1
    quote_id = quotes[0]["id"]
    thought = client.post(f"/api/quotes/{quote_id}/thoughts", json={"text": "Use this in the reliability essay."})
    assert thought.status_code == 201, thought.text
    assert thought.json()["thoughts"][0]["text"] == "Use this in the reliability essay."
    assert (vault_tmp / "library" / "books" / "designing-data-intensive-applications.md").exists()
    assert (vault_tmp / "library" / "quotes" / f"{quote_id}.md").exists()


def test_e2e_capture_triage_reclassify_medium_confidence(db, data_tmp):
    async def fake_route(text: str, source: str, ts: str | None):
        return _capture(
            type="task",
            confidence=0.7,
            body="follow up on ambiguous lead",
            due="2026-06-13",
            reminder_lead_minutes=None,
            command_detected=False,
        )

    with patch("mastisk.routes.capture.route_capture", new_callable=AsyncMock, side_effect=fake_route):
        client = _client(data_tmp)
        response = client.post(
            "/api/capture",
            json={"text": "maybe follow up", "source": "watch", "ts": "2026-06-12T08:00:00-07:00"},
            headers={"Authorization": "Bearer test-token"},
        )

    assert response.status_code == 201, response.text
    uid = response.json()["id"]
    triage = client.get("/api/triage").json()
    item = next(row for row in triage if row["id"] == f"task:{uid}")
    assert item["confidence"] is None
    assert item["kind"] == "task"
    kept = client.post(f"/api/triage/task:{uid}/reclassify", json={"type": "dismiss"})
    assert kept.status_code == 200, kept.text
    assert db.execute("SELECT needs_triage FROM tasks WHERE uid = ?", (uid,)).fetchone()["needs_triage"] == 0
