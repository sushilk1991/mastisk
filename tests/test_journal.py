from __future__ import annotations

from datetime import datetime

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def test_journal_day_creation_is_shared_with_task_hosting(db, vault_tmp):
    from mastisk.journal import append_log
    from mastisk.tasks.sync import append_task_to_host, journal_host_for_today

    host = journal_host_for_today("2026-06-11T09:00:00-07:00")
    task = append_task_to_host(host, text="Call Sam", uid="journal1")
    append_log("2026-06-11", "Felt focused", datetime(2026, 6, 11, 10, 15))

    assert task["host_path"] == "journal/2026-06-11.md"
    assert host == vault_tmp / "journal" / "2026-06-11.md"
    text = host.read_text(encoding="utf-8")
    assert text.count("## Tasks") == 1
    assert text.count("## Log") == 1
    assert text.count("## Reflections") == 1
    assert "- [ ] Call Sam" in text
    assert "- 10:15 Felt focused" in text


def test_journal_append_log_creates_missing_section_without_rewriting_existing_body(
    db, vault_tmp
):
    from mastisk.journal import append_log

    path = vault_tmp / "journal" / "2026-06-11.md"
    path.parent.mkdir(parents=True)
    original = "Intro stays\n\n## Tasks\n- [ ] keep this line\n\n## Reflections\nsame\n"
    path.write_text(original, encoding="utf-8")

    append_log("2026-06-11", "Recovered section", datetime(2026, 6, 11, 9, 30))

    updated = path.read_text(encoding="utf-8")
    assert updated.startswith(original)
    assert updated.endswith("\n## Log\n- 09:30 Recovered section\n")


def test_journal_mood_energy_frontmatter_preserves_body_content(db, vault_tmp):
    from mastisk.journal import append_log, set_mood_energy
    from mastisk.tasks.sync import append_task_to_host, journal_host_for_today

    host = journal_host_for_today("2026-06-11T09:00:00")
    append_task_to_host(host, text="Ship journal", uid="journal2")
    append_log("2026-06-11", "Implemented the write path", datetime(2026, 6, 11, 11, 0))

    set_mood_energy("2026-06-11", mood=4, energy=2)
    set_mood_energy("2026-06-11", energy=5)

    text = host.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    assert "mood: 4\n" in text
    assert "energy: 5\n" in text
    assert "- [ ] Ship journal" in text
    assert "- 11:00 Implemented the write path" in text


def test_journal_scan_mirrors_handmade_files_and_soft_deletes_removed_days(
    db, vault_tmp
):
    from mastisk.journal import scan_journal_days

    path = vault_tmp / "journal" / "2026-06-11.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "mood: 3\n"
        "energy: 4\n"
        "unrelated: keep\n"
        "---\n\n"
        "## Tasks\n\n"
        "## Log\n"
        "- 09:00 first\n"
        "- 10:00 second #needs-triage\n\n"
        "## Reflections\n"
        "A useful day.\n",
        encoding="utf-8",
    )

    result = scan_journal_days()

    assert result["upserted"] == 1
    row = db.execute("SELECT * FROM journal_days WHERE date = '2026-06-11'").fetchone()
    assert row["path"] == "journal/2026-06-11.md"
    assert row["mood"] == 3
    assert row["energy"] == 4
    assert row["log_count"] == 2
    assert row["has_reflections"] == 1
    assert row["deleted_at"] is None

    path.unlink()
    scan_journal_days()

    row = db.execute("SELECT deleted_at FROM journal_days WHERE date = '2026-06-11'").fetchone()
    assert row["deleted_at"] is not None


def test_journal_timeline_reads_mirror_without_scanning_disk(db, vault_tmp):
    from mastisk.journal import list_journal_days, scan_journal_days

    journal_dir = vault_tmp / "journal"
    journal_dir.mkdir()
    mirrored = journal_dir / "2026-06-11.md"
    mirrored.write_text("## Tasks\n\n## Log\n\n## Reflections\n", encoding="utf-8")
    scan_journal_days([mirrored])

    unscanned = journal_dir / "2026-06-12.md"
    unscanned.write_text("## Tasks\n\n## Log\n\n## Reflections\n", encoding="utf-8")

    assert [row["date"] for row in list_journal_days(limit=10)] == ["2026-06-11"]

    scan_journal_days()

    assert [row["date"] for row in list_journal_days(limit=10)] == [
        "2026-06-12",
        "2026-06-11",
    ]


def test_journal_scan_rejects_impossible_date_filenames(db, vault_tmp):
    from mastisk.journal import scan_journal_days

    path = vault_tmp / "journal" / "2026-99-99.md"
    path.parent.mkdir(parents=True)
    path.write_text("## Tasks\n\n## Log\n\n## Reflections\n", encoding="utf-8")
    db.execute(
        """INSERT INTO journal_days (date, path)
           VALUES ('2026-99-99', 'journal/2026-99-99.md')"""
    )

    scan_journal_days()

    row = db.execute(
        "SELECT deleted_at FROM journal_days WHERE date = '2026-99-99'",
    ).fetchone()
    assert row["deleted_at"] is not None


def test_journal_assembled_reminders_use_local_journal_day(db, vault_tmp, data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "Asia/Kolkata"\n', encoding="utf-8")
    from mastisk.journal import assemble_journal_day, ensure_day
    from mastisk.settings import reload_settings

    reload_settings()
    ensure_day("2026-06-11")
    db.execute(
        """INSERT INTO reminders
           (entity_type, entity_id, fire_at, kind, status, title, body, fired_at)
           VALUES ('task', 'local-day', '2026-06-10T19:00:00+00:00',
                   'task_due', 'sent', 'Task due', 'Included',
                   '2026-06-10T19:00:01+00:00')"""
    )
    db.execute(
        """INSERT INTO reminders
           (entity_type, entity_id, fire_at, kind, status, title, body, fired_at)
           VALUES ('task', 'next-local-day', '2026-06-11T19:00:00+00:00',
                   'task_due', 'sent', 'Task due', 'Excluded',
                   '2026-06-11T19:00:01+00:00')"""
    )

    day = assemble_journal_day("2026-06-11")

    assert day is not None
    assert [row["entity_id"] for row in day["fired_reminders"]] == ["local-day"]


def test_journal_routes_append_patch_assemble_and_validate_dates(db, vault_tmp, data_tmp):
    from mastisk.routines.sync import create_routine_file, toggle_routine_completion
    from mastisk.tasks.sync import append_task_to_host

    with _client(vault_tmp, data_tmp, db) as client:
        invalid = client.get("/api/journal/2026-6-11")
        assert invalid.status_code == 422

        future = client.get("/api/journal/2999-01-01")
        assert future.status_code == 422

        blank = client.post("/api/journal/2026-06-11/log", json={"text": "   "})
        assert blank.status_code == 422

        logged = client.post(
            "/api/journal/2026-06-11/log",
            json={"text": "Today had shape"},
        )
        assert logged.status_code == 201, logged.text
        assert logged.json()["destination"] == "journal/2026-06-11.md"

        patched = client.patch(
            "/api/journal/2026-06-11",
            json={"mood": 5, "energy": 4, "reflections": "Keep the mornings clean."},
        )
        assert patched.status_code == 200, patched.text

        append_task_to_host(
            vault_tmp / "journal" / "2026-06-11.md",
            text="Hosted task",
            uid="hosted1",
        )
        append_task_to_host(
            vault_tmp / "journal" / "2026-06-10.md",
            text="Due today",
            due="2026-06-11",
            uid="due1",
        )
        create_routine_file(name="Morning Vitamins", time_of_day="morning")
        toggle_routine_completion("morning-vitamins", date_value="2026-06-11")
        db.execute(
            """INSERT INTO reminders
               (entity_type, entity_id, fire_at, kind, status, title, body, fired_at)
               VALUES ('task', 'hosted1', '2026-06-11T08:00:00+00:00',
                       'task_due', 'sent', 'Task due', 'Hosted task',
                       '2026-06-11T08:00:01+00:00')"""
        )

        day = client.get("/api/journal/2026-06-11")

    assert day.status_code == 200, day.text
    body = day.json()
    assert body["date"] == "2026-06-11"
    assert body["path"] == "journal/2026-06-11.md"
    assert body["frontmatter"]["mood"] == 5
    assert "Today had shape" in body["sections"]["Log"]
    assert body["sections"]["Reflections"].strip() == "Keep the mornings clean."
    assert {task["uid"] for task in body["tasks"]} == {"hosted1", "due1"}
    assert body["routine_completions"][0]["routine_id"] == "morning-vitamins"
    assert body["fired_reminders"][0]["entity_id"] == "hosted1"

    with _client(vault_tmp, data_tmp, db) as client:
        timeline = client.get("/api/journal?limit=1")
    assert timeline.status_code == 200, timeline.text
    assert timeline.json()[0]["date"] == "2026-06-11"
