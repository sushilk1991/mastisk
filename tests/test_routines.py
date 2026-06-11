from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def test_streak_math_handles_gaps_and_fixed_challenges():
    from mastisk.routines.streaks import (
        completion_rate_30d,
        current_streak,
        fixed_challenge_progress,
        longest_streak,
    )

    dates = {"2026-06-07", "2026-06-09", "2026-06-10"}

    assert current_streak(dates, today="2026-06-11") == 2
    assert current_streak({"2026-06-11"}, today="2026-06-11") == 1
    assert current_streak({"2026-06-09"}, today="2026-06-11") == 0
    assert longest_streak(dates) == 2
    assert completion_rate_30d(dates, today="2026-06-11") == 3 / 30
    assert fixed_challenge_progress(
        dates | {"2026-06-01"},
        target_days=3,
        start_date="2026-06-01",
        today="2026-06-11",
    ) == {
        "days_done": 4,
        "target_days": 3,
        "remaining": 0,
        "complete": True,
    }


def test_routine_routes_create_toggle_progress_and_archive_file_first(vault_tmp, data_tmp, db):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/routines",
            json={
                "name": "Morning Vitamins",
                "domain": "health",
                "time_of_day": "morning",
                "notify": True,
            },
        )
        assert created.status_code == 201, created.text
        routine = created.json()
        assert routine["slug"] == "morning-vitamins"

        toggled = client.post("/api/routines/morning-vitamins/toggle?date=2026-06-11")
        assert toggled.status_code == 200, toggled.text
        assert toggled.json()["completed"] is True
        assert toggled.json()["streak"]["current"] == 1

        file_text = (vault_tmp / "routines" / "morning-vitamins.md").read_text(
            encoding="utf-8"
        )
        assert file_text.count("- 2026-06-11") == 1
        count = db.execute(
            "SELECT COUNT(*) AS n FROM routine_completions WHERE routine_id = ?",
            ("morning-vitamins",),
        ).fetchone()["n"]
        assert count == 1

        untoggled = client.post("/api/routines/morning-vitamins/toggle?date=2026-06-11")
        assert untoggled.status_code == 200, untoggled.text
        assert untoggled.json()["completed"] is False
        file_text = (vault_tmp / "routines" / "morning-vitamins.md").read_text(
            encoding="utf-8"
        )
        assert "- 2026-06-11" not in file_text

        client.post("/api/routines/morning-vitamins/toggle?date=2026-06-10")
        progress = client.get("/api/routines/morning-vitamins/progress?days=3")
        assert progress.status_code == 200, progress.text
        assert progress.json()["completion_dates"] == ["2026-06-10"]

        listed = client.get("/api/routines")
        assert [r["slug"] for r in listed.json()["morning"]] == ["morning-vitamins"]
        archived = client.post("/api/routines/morning-vitamins/archive")
        assert archived.status_code == 200, archived.text
        assert archived.json()["archived"] is True
        assert client.get("/api/routines").json()["morning"] == []
        assert client.get("/api/routines?archived=true").json()["morning"][0]["slug"] == (
            "morning-vitamins"
        )


def test_scan_routines_reconciles_hand_edited_completion_section(db, vault_tmp):
    from mastisk.routines.sync import scan_routines

    path = vault_tmp / "routines" / "reading.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: Reading\n"
        "time_of_day: evening\n"
        "notify: false\n"
        "streak_type: fixed\n"
        "target_days: 30\n"
        "start_date: 2026-06-01\n"
        "---\n\n"
        "## Completions\n"
        "- 2026-06-10\n"
        "- 2026-06-11\n",
        encoding="utf-8",
    )

    result = scan_routines()

    assert result["upserted"] == 1
    row = db.execute("SELECT * FROM routines WHERE slug = 'reading'").fetchone()
    assert row["time_of_day"] == "evening"
    assert row["streak_type"] == "fixed"
    completions = db.execute(
        "SELECT date FROM routine_completions WHERE routine_id = 'reading' ORDER BY date"
    ).fetchall()
    assert [row["date"] for row in completions] == ["2026-06-10", "2026-06-11"]


def test_scan_routines_ignores_date_bullets_outside_completions_section(db, vault_tmp):
    from mastisk.routines.sync import scan_routines

    path = vault_tmp / "routines" / "reading.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: Reading\n"
        "time_of_day: evening\n"
        "---\n\n"
        "## Completions\n\n"
        "## Notes\n"
        "- 2026-06-10\n",
        encoding="utf-8",
    )

    scan_routines()

    count = db.execute(
        "SELECT COUNT(*) AS n FROM routine_completions WHERE routine_id = 'reading'"
    ).fetchone()["n"]
    assert count == 0


def test_routine_toggle_does_not_resurrect_hand_deleted_completion(
    vault_tmp, data_tmp, db
):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/routines",
            json={"name": "Reading", "time_of_day": "evening"},
        )
        assert created.status_code == 201, created.text
        client.post("/api/routines/reading/toggle?date=2026-06-10")
        client.post("/api/routines/reading/toggle?date=2026-06-11")

        path = vault_tmp / "routines" / "reading.md"
        path.write_text(
            path.read_text(encoding="utf-8").replace("- 2026-06-10\n", ""),
            encoding="utf-8",
        )

        toggled = client.post("/api/routines/reading/toggle?date=2026-06-12")
        assert toggled.status_code == 200, toggled.text

        file_text = path.read_text(encoding="utf-8")
        assert "- 2026-06-10" not in file_text
        assert "- 2026-06-11" in file_text
        assert "- 2026-06-12" in file_text
        completions = db.execute(
            "SELECT date FROM routine_completions WHERE routine_id = 'reading' ORDER BY date"
        ).fetchall()
        assert [row["date"] for row in completions] == ["2026-06-11", "2026-06-12"]


def test_routine_missed_nudges_dedup_and_respect_windows(db, vault_tmp, data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "Asia/Kolkata"\n', encoding="utf-8")
    from mastisk.agents.reminder_engine import routine_missed_tick
    from mastisk.routines.sync import create_routine_file, toggle_routine_completion
    from mastisk.settings import reload_settings

    reload_settings()
    create_routine_file(name="Vitamins", time_of_day="morning", notify=True)
    create_routine_file(name="Walk", time_of_day="afternoon", notify=True)
    create_routine_file(name="Meditate", time_of_day="anytime", notify=True)
    toggle_routine_completion("walk", date_value="2026-06-11")
    tz = ZoneInfo("Asia/Kolkata")

    assert routine_missed_tick(now=datetime(2026, 6, 11, 11, 59, tzinfo=tz)) == 0
    assert routine_missed_tick(now=datetime(2026, 6, 11, 12, 0, tzinfo=tz)) == 1
    assert routine_missed_tick(now=datetime(2026, 6, 11, 12, 1, tzinfo=tz)) == 0

    rows = db.execute(
        "SELECT kind, entity_type, entity_id, status FROM reminders WHERE kind = 'routine_missed'"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "kind": "routine_missed",
            "entity_type": "routine",
            "entity_id": "vitamins:2026-06-11",
            "status": "pending",
        }
    ]


def test_routine_missed_reminder_cancels_if_completed_before_fire(
    db, vault_tmp, data_tmp, monkeypatch
):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "Asia/Kolkata"\n'
        '[notify]\nbackend = "ntfy"\nntfy_topic = "test"\n',
        encoding="utf-8",
    )
    from mastisk.agents.reminder_engine import reminder_tick, routine_missed_tick
    from mastisk.routines.sync import create_routine_file, toggle_routine_completion
    from mastisk.settings import reload_settings

    reload_settings()
    create_routine_file(name="Vitamins", time_of_day="morning", notify=True)
    tz = ZoneInfo("Asia/Kolkata")
    now = datetime(2026, 6, 11, 12, 0, tzinfo=tz)
    assert routine_missed_tick(now=now) == 1
    toggle_routine_completion("vitamins", date_value="2026-06-11")
    sends: list[tuple[str, str]] = []

    def fake_send(title, body, url=None):
        sends.append((title, body))
        return True

    monkeypatch.setattr("mastisk.notify.send", fake_send)

    assert reminder_tick(now=now, ensure_daily_summary=False) == 0
    assert sends == []
    row = db.execute(
        "SELECT status, last_error FROM reminders WHERE kind = 'routine_missed'"
    ).fetchone()
    assert dict(row) == {
        "status": "cancelled",
        "last_error": "routine completed",
    }
