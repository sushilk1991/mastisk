from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient


def _configure_notify(data_tmp, extra: str = "") -> None:
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[notify]\nbackend = "ntfy"\nntfy_topic = "test"\n' + extra,
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()


@pytest.fixture
def client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    with TestClient(create_app()) as c:
        yield c


def _insert_reminder(
    db,
    *,
    entity_id: str = "abc123",
    fire_at: str = "2026-06-11T09:00:00+00:00",
    status: str = "pending",
    attempts: int = 0,
    title: str = "Task due",
    body: str = "Call Sam",
) -> int:
    cur = db.execute(
        """INSERT INTO reminders
           (entity_type, entity_id, fire_at, lead_minutes, kind, status, attempts, title, body)
           VALUES ('task', ?, ?, 15, 'task_due', ?, ?, ?, ?)""",
        (entity_id, fire_at, status, attempts, title, body),
    )
    return int(cur.lastrowid)


def test_reminder_tick_claims_due_row_once_with_overlapping_ticks(db, data_tmp, monkeypatch):
    _configure_notify(data_tmp)
    reminder_id = _insert_reminder(db)
    sent: list[tuple[str, str, str | None]] = []

    def fake_send(title: str, body: str, url: str | None = None) -> bool:
        sent.append((title, body, url))
        return True

    monkeypatch.setattr("mastisk.agents.reminder_engine.notify.send", fake_send)

    from mastisk.agents.reminder_engine import reminder_tick

    now = datetime(2026, 6, 11, 9, 0, tzinfo=UTC)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: reminder_tick(now=now, ensure_daily_summary=False), range(2)))

    row = db.execute("SELECT status, fired_at, attempts FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    assert sent == [("Task due", "Call Sam", None)]
    assert row["status"] == "sent"
    assert row["fired_at"] is not None
    assert row["attempts"] == 0


def test_late_on_wake_marks_late_but_still_sends(db, data_tmp, monkeypatch):
    _configure_notify(data_tmp)
    reminder_id = _insert_reminder(db, fire_at="2026-06-11T08:30:00+00:00")
    sent: list[str] = []
    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda title, body, url=None: sent.append(body) or True,
    )

    from mastisk.agents.reminder_engine import reminder_tick

    reminder_tick(now=datetime(2026, 6, 11, 9, 0, tzinfo=UTC), ensure_daily_summary=False)

    row = db.execute("SELECT status, fired_at FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    assert sent == ["Call Sam"]
    assert row["status"] == "late"
    assert row["fired_at"] is not None


def test_notify_failure_retries_then_marks_failed_and_emits_feed(db, data_tmp, monkeypatch):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        "[reminders]\nmax_attempts = 2\nretry_backoff_seconds = [0]\n"
        '[notify]\nbackend = "ntfy"\nntfy_topic = "test"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    reminder_id = _insert_reminder(db)
    monkeypatch.setattr("mastisk.agents.reminder_engine.notify.send", lambda *args, **kwargs: False)

    from mastisk.agents.reminder_engine import reminder_tick

    now = datetime(2026, 6, 11, 9, 0, tzinfo=UTC)
    reminder_tick(now=now, ensure_daily_summary=False)
    first = db.execute(
        "SELECT status, attempts, next_attempt_at FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    assert first["status"] == "pending"
    assert first["attempts"] == 1
    assert first["next_attempt_at"] is not None

    reminder_tick(now=now, ensure_daily_summary=False)
    failed = db.execute(
        "SELECT status, attempts, last_error FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    feed = db.execute(
        "SELECT agent, verb, obj, kind FROM feed ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert failed["status"] == "notify_failed"
    assert failed["attempts"] == 2
    assert failed["last_error"] == "notifier returned False"
    assert dict(feed) == {
        "agent": "reminder_engine",
        "verb": "notify_failed",
        "obj": str(reminder_id),
        "kind": "reminder",
    }


def test_backend_none_leaves_due_reminder_pending_without_attempts(db, data_tmp, monkeypatch):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[notify]\nbackend = "none"\n', encoding="utf-8")
    from mastisk.settings import reload_settings

    reload_settings()
    reminder_id = _insert_reminder(db)
    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda *args, **kwargs: pytest.fail("backend none should not call notifier"),
    )

    from mastisk.agents.reminder_engine import reminder_tick

    assert reminder_tick(now=datetime(2026, 6, 11, 9, 0, tzinfo=UTC)) == 0
    row = db.execute("SELECT status, attempts FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["attempts"] == 0


def test_task_due_reminder_cancels_if_task_is_already_done(db, data_tmp, monkeypatch):
    _configure_notify(data_tmp)
    db.execute(
        """INSERT INTO tasks
           (uid, host_path, line_number, text, checked, status, due, tags_json, links_json)
           VALUES ('abc123', 'journal/2026-06-11.md', 1, 'Call Sam', 1, 'done',
                   '2026-06-11T09:00:00', '[]', '[]')"""
    )
    reminder_id = _insert_reminder(db)
    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda *args, **kwargs: pytest.fail("done task should not notify"),
    )

    from mastisk.agents.reminder_engine import reminder_tick

    reminder_tick(now=datetime(2026, 6, 11, 9, 0, tzinfo=UTC), ensure_daily_summary=False)

    row = db.execute("SELECT status, last_error FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    assert row["status"] == "cancelled"
    assert row["last_error"] == "task is not open"


def test_task_due_reminder_reschedules_at_fire_time_when_due_moved_later(
    db, data_tmp, monkeypatch
):
    _configure_notify(data_tmp)
    db.execute(
        """INSERT INTO tasks
           (uid, host_path, line_number, text, checked, status, due,
            reminder_lead_minutes, tags_json, links_json)
           VALUES ('abc123', 'journal/2026-06-11.md', 1, 'Call Sam later', 0, 'open',
                   '2026-06-11T10:00:00+00:00', 15, '[]', '[]')"""
    )
    reminder_id = _insert_reminder(
        db,
        fire_at="2026-06-11T09:00:00+00:00",
        body="Old task text",
    )
    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda *args, **kwargs: pytest.fail("future task reminder should reschedule"),
    )

    from mastisk.agents.reminder_engine import reminder_tick

    reminder_tick(now=datetime(2026, 6, 11, 9, 0, tzinfo=UTC), ensure_daily_summary=False)

    row = db.execute(
        "SELECT status, fire_at, body, fired_at FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    assert row["status"] == "pending"
    assert row["fire_at"] == "2026-06-11T09:45:00+00:00"
    assert row["body"] == "Call Sam later"
    assert row["fired_at"] is None


def test_task_due_reminder_cancels_if_due_was_removed_before_fire(
    db, data_tmp, monkeypatch
):
    _configure_notify(data_tmp)
    db.execute(
        """INSERT INTO tasks
           (uid, host_path, line_number, text, checked, status, due,
            reminder_lead_minutes, tags_json, links_json)
           VALUES ('abc123', 'journal/2026-06-11.md', 1, 'Call Sam', 0, 'open',
                   NULL, 15, '[]', '[]')"""
    )
    reminder_id = _insert_reminder(db)
    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda *args, **kwargs: pytest.fail("task without due should not notify"),
    )

    from mastisk.agents.reminder_engine import reminder_tick

    reminder_tick(now=datetime(2026, 6, 11, 9, 0, tzinfo=UTC), ensure_daily_summary=False)

    row = db.execute(
        "SELECT status, last_error FROM reminders WHERE id = ?",
        (reminder_id,),
    ).fetchone()
    assert row["status"] == "cancelled"
    assert row["last_error"] == "task due removed"


def test_reclaim_firing_reminders_returns_orphaned_claims_to_pending(db):
    reminder_id = _insert_reminder(db, status="firing")

    from mastisk.agents.reminder_engine import reclaim_firing_reminders

    assert reclaim_firing_reminders() == 1
    row = db.execute("SELECT status FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    assert row["status"] == "pending"


def test_reminder_tick_creates_missed_daily_summary_row_after_summary_time(
    db, data_tmp, monkeypatch
):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "UTC"\n'
        '[reminders]\ndaily_summary_time = "07:30"\n'
        '[notify]\nbackend = "ntfy"\nntfy_topic = "test"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    db.execute(
        """INSERT INTO tasks
           (uid, host_path, line_number, text, status, due, tags_json, links_json)
           VALUES ('today1', 'journal/2026-06-11.md', 1, 'Call Sam', 'open', '2026-06-11', '[]', '[]')"""
    )
    sent: list[str] = []
    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda title, body, url=None: sent.append(body) or True,
    )

    from mastisk.agents.reminder_engine import reminder_tick

    reminder_tick(now=datetime(2026, 6, 11, 8, 0, tzinfo=UTC))

    row = db.execute(
        "SELECT kind, status, entity_id FROM reminders WHERE kind = 'daily_summary'"
    ).fetchone()
    assert sent and "1 due today" in sent[0]
    assert row["entity_id"] == "2026-06-11"
    assert row["status"] == "late"


def test_daily_summary_composition_is_pure():
    from mastisk.agents.reminder_engine import compose_daily_summary

    title, body = compose_daily_summary(
        today=date(2026, 6, 11),
        open_tasks=[
            {"uid": "late1", "text": "Pay rent", "due": "2026-06-10"},
            {"uid": "today1", "text": "Call Sam", "due": "2026-06-11T14:00:00"},
            {"uid": "today2", "text": "Ship reminders", "due": "2026-06-11"},
            {"uid": "future1", "text": "Future task", "due": "2026-06-12"},
        ],
    )

    assert title == "Mastisk daily summary"
    assert "2 due today" in body
    assert "1 overdue" in body
    assert "Due today (2)" in body
    assert "- Call Sam" in body
    assert "- Ship reminders" in body
    assert "Overdue (1)" in body
    assert "- Pay rent" in body
    assert "Future task" not in body


def test_daily_summary_tick_is_idempotent_per_date(db, data_tmp, monkeypatch):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "UTC"\n'
        '[notify]\nbackend = "ntfy"\nntfy_topic = "test"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    db.execute(
        """INSERT INTO tasks
           (uid, host_path, line_number, text, status, due, tags_json, links_json)
           VALUES ('today1', 'journal/2026-06-11.md', 1, 'Call Sam', 'open', '2026-06-11', '[]', '[]')"""
    )
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda title, body, url=None: sent.append((title, body)) or True,
    )

    from mastisk.agents.reminder_engine import daily_summary_tick

    now = datetime(2026, 6, 11, 7, 30, tzinfo=UTC)
    daily_summary_tick(now=now)
    daily_summary_tick(now=now)

    rows = db.execute(
        "SELECT kind, status, entity_id FROM reminders WHERE kind = 'daily_summary'"
    ).fetchall()
    assert len(sent) == 1
    assert len(rows) == 1
    assert rows[0]["status"] == "sent"
    assert rows[0]["entity_id"] == "2026-06-11"


def test_reminders_route_creates_lists_and_cancels_custom_reminder(client, db):
    created = client.post(
        "/api/reminders",
        json={
            "kind": "custom",
            "fire_at": "2099-01-01T09:00:00+00:00",
            "title": "Check oven",
            "body": "Turn it off",
            "entity_type": "task",
            "entity_id": "abc123",
        },
    )
    assert created.status_code == 201, created.text
    reminder = created.json()
    assert reminder["kind"] == "custom"
    assert reminder["status"] == "pending"

    listed = client.get("/api/reminders?status=pending")
    assert listed.status_code == 200
    assert [r["id"] for r in listed.json()] == [reminder["id"]]

    cancelled = client.delete(f"/api/reminders/{reminder['id']}")
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "cancelled"
    row = db.execute("SELECT status FROM reminders WHERE id = ?", (reminder["id"],)).fetchone()
    assert row["status"] == "cancelled"
