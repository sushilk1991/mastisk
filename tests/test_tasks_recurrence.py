from __future__ import annotations

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def test_recurrence_parser_small_documented_grammar():
    from mastisk.tasks.recurrence import next_due_date

    cases = [
        ("daily", "2026-06-11", "2026-06-12"),
        ("every day", "2026-06-11", "2026-06-12"),
        ("weekly", "2026-06-11", "2026-06-18"),
        ("every monday", "2026-06-09", "2026-06-15"),
        ("every 3 days", "2026-06-11", "2026-06-14"),
        ("every 2 weeks", "2026-06-11", "2026-06-25"),
        ("monthly", "2026-01-31", "2026-02-28"),
        ("every 2 months", "2026-01-31", "2026-03-31"),
        ("every month on the 15th", "2026-01-31", "2026-02-15"),
        ("every month on the 31st", "2026-01-31", "2026-02-28"),
    ]

    for rule, base, expected in cases:
        assert next_due_date(rule, base) == expected

    assert next_due_date("every other business day", "2026-06-11") is None


def test_recurring_task_materializes_next_instance_once_after_toggle(
    vault_tmp, data_tmp, db
):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/tasks",
            json={
                "text": "Pay rent",
                "due": "2026-01-31T09:30:00",
                "recurrence": "monthly",
                "priority": "high",
                "tags": ["home"],
            },
        )
        assert created.status_code == 201, created.text
        uid = created.json()["uid"]

        toggled = client.patch(f"/api/tasks/{uid}/toggle")
        assert toggled.status_code == 200, toggled.text
        host_path = vault_tmp / toggled.json()["host_path"]
        file_text = host_path.read_text(encoding="utf-8")
        assert file_text.count("Pay rent") == 2
        assert "📅 2026-02-28 ⏰ 09:30" in file_text
        assert "🔁 monthly" in file_text
        assert "⏫" in file_text
        assert "#home" in file_text

        from mastisk.tasks.recurrence import recurrence_tick
        from mastisk.tasks.sync import scan_task_hosts

        scan_task_hosts([host_path])
        assert recurrence_tick() == 0
        file_text = host_path.read_text(encoding="utf-8")
        assert file_text.count("Pay rent") == 2

        row = db.execute(
            "SELECT recurrence_materialized_key, recurrence_unparsed FROM tasks WHERE uid = ?",
            (uid,),
        ).fetchone()
        assert row["recurrence_materialized_key"] == f"{uid}:2026-01-31:monthly"
        assert row["recurrence_unparsed"] == 0


def test_scheduled_only_recurring_task_advances_schedule_without_due(
    vault_tmp, data_tmp, db
):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/tasks",
            json={
                "text": "Plan tomorrow",
                "scheduled": "2026-06-11",
                "recurrence": "daily",
            },
        )
        uid = created.json()["uid"]

        toggled = client.patch(f"/api/tasks/{uid}/toggle")
        assert toggled.status_code == 200, toggled.text
        file_text = (vault_tmp / toggled.json()["host_path"]).read_text(encoding="utf-8")

        assert file_text.count("Plan tomorrow") == 2
        next_line = [line for line in file_text.splitlines() if "- [ ] Plan tomorrow" in line][0]
        assert "⏳ 2026-06-12" in next_line
        assert "📅" not in next_line


def test_scheduled_only_recurrence_tick_materializes_once_across_days(
    vault_tmp, data_tmp, db
):
    from datetime import datetime
    from zoneinfo import ZoneInfo

    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/tasks",
            json={
                "text": "Plan tomorrow",
                "scheduled": "2026-06-10",
                "recurrence": "daily",
            },
        )
        assert created.status_code == 201, created.text
        uid = created.json()["uid"]
        host_path = vault_tmp / created.json()["host_path"]

        host_path.write_text(
            host_path.read_text(encoding="utf-8").replace("- [ ] Plan tomorrow", "- [x] Plan tomorrow"),
            encoding="utf-8",
        )

        from mastisk.tasks.recurrence import recurrence_tick
        from mastisk.tasks.sync import scan_task_hosts

        scan_task_hosts([host_path])
        tz = ZoneInfo("UTC")
        assert recurrence_tick(now=datetime(2026, 6, 11, 0, 10, tzinfo=tz)) == 1
        assert recurrence_tick(now=datetime(2026, 6, 12, 0, 10, tzinfo=tz)) == 0

        file_text = host_path.read_text(encoding="utf-8")
        assert file_text.count("Plan tomorrow") == 2
        open_rows = db.execute(
            """SELECT scheduled FROM tasks
               WHERE text = 'Plan tomorrow' AND status = 'open' AND deleted_at IS NULL"""
        ).fetchall()
        assert [row["scheduled"] for row in open_rows] == ["2026-06-11"]
        row = db.execute(
            "SELECT recurrence_materialized_key FROM tasks WHERE uid = ?",
            (uid,),
        ).fetchone()
        assert row["recurrence_materialized_key"] == f"{uid}:2026-06-10:daily"


def test_unparseable_recurring_task_is_flagged_without_materializing(
    vault_tmp, data_tmp, db
):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/tasks",
            json={
                "text": "Complex recurrence",
                "due": "2026-06-11",
                "recurrence": "every other business day",
            },
        )
        uid = created.json()["uid"]

        toggled = client.patch(f"/api/tasks/{uid}/toggle")
        assert toggled.status_code == 200, toggled.text
        file_text = (vault_tmp / toggled.json()["host_path"]).read_text(encoding="utf-8")
        assert file_text.count("Complex recurrence") == 1

        row = db.execute(
            "SELECT recurrence_unparsed FROM tasks WHERE uid = ?",
            (uid,),
        ).fetchone()
        assert row["recurrence_unparsed"] == 1
        listed = client.get("/api/tasks").json()
        assert next(task for task in listed if task["uid"] == uid)["recurrence_unparsed"] is True
