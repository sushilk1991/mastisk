from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def _capture(**overrides):
    from mastisk.capture.router import Capture

    data = {
        "type": "person",
        "confidence": 0.94,
        "title": "Anjali Rao",
        "body": "Anjali's daughter started college.",
        "domain": None,
        "project": None,
        "person": None,
        "routine": None,
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


def test_scan_people_round_trips_handmade_file(db, vault_tmp):
    from mastisk.people.sync import scan_people

    path = vault_tmp / "people" / "anjali-rao.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: Anjali Rao\n"
        "birthday: 09-14\n"
        "anniversary: 2012-02-20\n"
        "facts:\n"
        "  kids:\n"
        "    - Mira\n"
        "  interests:\n"
        "    - climbing\n"
        "---\n\n"
        "Met through Mastisk.\n\n"
        "## Interactions\n"
        "- 2026-06-10 09:15 Discussed school applications.\n",
        encoding="utf-8",
    )

    result = scan_people()

    assert result["upserted"] == 1
    person = db.execute("SELECT * FROM people WHERE slug = 'anjali-rao'").fetchone()
    assert person["name"] == "Anjali Rao"
    assert person["birthday"] == "09-14"
    assert person["anniversary"] == "2012-02-20"
    assert json.loads(person["facts_json"]) == {
        "kids": ["Mira"],
        "interests": ["climbing"],
    }
    assert person["last_interaction_at"] == "2026-06-10 09:15"
    interactions = db.execute(
        "SELECT person_slug, ts, text FROM interactions WHERE person_slug = 'anjali-rao'"
    ).fetchall()
    assert [dict(row) for row in interactions] == [
        {
            "person_slug": "anjali-rao",
            "ts": "2026-06-10 09:15",
            "text": "Discussed school applications.",
        }
    ]


def test_append_interaction_updates_file_and_last_interaction(db, vault_tmp):
    from mastisk.people.sync import append_interaction, create_person_file

    create_person_file(name="Anjali Rao")
    updated = append_interaction(
        "anjali-rao",
        "Talked about the college move.",
        ts="2026-06-11 18:30",
    )

    assert updated is not None
    assert updated["last_interaction_at"] == "2026-06-11 18:30"
    file_text = (vault_tmp / "people" / "anjali-rao.md").read_text(encoding="utf-8")
    assert "## Interactions\n- 2026-06-11 18:30 Talked about the college move.\n" in file_text
    row = db.execute(
        "SELECT text FROM interactions WHERE person_slug = 'anjali-rao'"
    ).fetchone()
    assert row["text"] == "Talked about the college move."


def test_people_mutations_preserve_unparsed_interaction_lines(db, vault_tmp):
    from mastisk.people.sync import append_interaction, patch_person, scan_people

    path = vault_tmp / "people" / "anjali-rao.md"
    path.parent.mkdir(parents=True)
    original = (
        "---\n"
        "name: Anjali Rao\n"
        "---\n\n"
        "Met through Mastisk.\n\n"
        "## Interactions\n"
        "- 2026-06-10 09:15 Discussed school applications.\n"
        "- Hand-written bullet without a timestamp.\n"
        "  - Sub-bullet with context.\n"
        "Loose prose that should survive.\n"
    )
    path.write_text(original, encoding="utf-8")
    scan_people([path])

    append_interaction(
        "anjali-rao",
        "Talked about the college move.",
        ts="2026-06-11 18:30",
    )

    after_append = (
        original
        + "- 2026-06-11 18:30 Talked about the college move.\n"
    )
    assert path.read_text(encoding="utf-8") == after_append

    patch_person("anjali-rao", {"birthday": "09-14"})

    after_patch = after_append.replace(
        "---\nname: Anjali Rao\n---",
        "---\nname: Anjali Rao\nbirthday: 09-14\n---",
        1,
    )
    assert path.read_text(encoding="utf-8") == after_patch


def test_people_routes_file_first_crud_and_fact_merge(db, vault_tmp, data_tmp):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/people",
            json={
                "name": "Anjali Rao",
                "birthday": "09-14",
                "facts": {"kids": ["Mira"]},
                "body": "Met through Mastisk.",
            },
        )
        assert created.status_code == 201, created.text
        assert created.json()["slug"] == "anjali-rao"

        patched = client.patch(
            "/api/people/anjali-rao",
            json={
                "anniversary": "2012-02-20",
                "facts": {"interests": ["climbing"]},
                "follow_up_at": "2026-06-20T09:00:00+00:00",
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["facts"] == {
            "kids": ["Mira"],
            "interests": ["climbing"],
        }

        added = client.post(
            "/api/people/anjali-rao/interactions",
            json={"text": "Sent intro to Mira.", "ts": "2026-06-11 08:05"},
        )
        assert added.status_code == 201, added.text
        detail = client.get("/api/people/anjali-rao").json()
        assert detail["body"].strip() == "Met through Mastisk."
        assert detail["interactions"] == [
            {"ts": "2026-06-11 08:05", "text": "Sent intro to Mira."}
        ]
        assert detail["follow_up_at"] == "2026-06-20T09:00:00+00:00"
        file_text = (vault_tmp / "people" / "anjali-rao.md").read_text(encoding="utf-8")
        assert "follow_up_at: '2026-06-20T09:00:00+00:00'" in file_text

        listed = client.get("/api/people").json()
        assert listed[0]["birthday_soon"] is False
        assert listed[0]["last_interaction_at"] == "2026-06-11 08:05"

        deleted = client.delete("/api/people/anjali-rao")
        assert deleted.status_code == 200, deleted.text
        assert client.get("/api/people").json() == []
        assert "archived: true" in (vault_tmp / "people" / "anjali-rao.md").read_text(
            encoding="utf-8"
        )


@pytest.mark.asyncio
async def test_route_capture_injects_active_people_context_and_excludes_archived(
    db, vault_tmp, data_tmp
):
    from mastisk.people.sync import archive_person, create_person_file
    from mastisk.settings import reload_settings

    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "America/Los_Angeles"\n')
    reload_settings()
    create_person_file(name="Anjali Rao")
    create_person_file(name="Archived Person")
    archive_person("archived-person")

    from mastisk.capture.router import route_capture

    response = (
        {"text": json.dumps(_capture(type="person", person="anjali-rao").model_dump())},
        "claude",
    )
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ) as run_mock:
        capture = await route_capture(
            "Anjali's daughter started college",
            source="watch",
            ts="2026-06-11T09:00:00-07:00",
        )

    assert capture.person == "anjali-rao"
    prompt = run_mock.call_args.args[0]
    assert '"slug": "anjali-rao"' in prompt
    assert '"name": "Anjali Rao"' in prompt
    assert "archived-person" not in prompt


def test_capture_person_matched_auto_stub_and_low_confidence_paths(
    db, vault_tmp, data_tmp
):
    from mastisk.people.sync import create_person_file

    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\nbearer_token = "test-token"\n')
    create_person_file(name="Anjali Rao")
    with _client(vault_tmp, data_tmp, db) as client, patch(
        "mastisk.routes.capture.route_capture", new_callable=AsyncMock
    ) as router:
        router.return_value = _capture(person="anjali-rao", body="Mentioned a new role.")
        matched = client.post(
            "/api/capture",
            json={"text": "Anjali mentioned a new role", "source": "watch"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert matched.status_code == 201, matched.text
        assert matched.json()["type"] == "person"
        assert "Mentioned a new role." in (
            vault_tmp / "people" / "anjali-rao.md"
        ).read_text(encoding="utf-8")

        router.return_value = _capture(
            person=None,
            title="Sam Lee",
            body="Sam moved to Seattle.",
            confidence=0.92,
        )
        stubbed = client.post(
            "/api/capture",
            json={"text": "Sam moved to Seattle", "source": "watch"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert stubbed.status_code == 201, stubbed.text
        assert stubbed.json()["type"] == "person"
        assert stubbed.json()["id"] == "sam-lee"
        assert (vault_tmp / "people" / "sam-lee.md").exists()

        router.return_value = _capture(
            person=None,
            title="Priya",
            body="Priya might be a person.",
            confidence=0.72,
        )
        triaged = client.post(
            "/api/capture",
            json={"text": "Priya might be a person", "source": "watch"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert triaged.status_code == 201, triaged.text
        assert triaged.json()["type"] == "person"
        assert triaged.json()["needs_triage"] is True
        assert not (vault_tmp / "people" / "priya.md").exists()
        note_text = (vault_tmp / triaged.json()["destination"]).read_text(encoding="utf-8")
        assert "needs_triage: true" in note_text


def test_triage_person_target_creates_person_from_low_confidence_note(
    db, vault_tmp, data_tmp
):
    from mastisk.routes.notes import persist_note_capture

    note = persist_note_capture(
        body="Priya moved to Seattle.",
        source="watch",
        file_content=(
            "---\n"
            "capture:\n"
            "  type: person\n"
            "  title: Priya Shah\n"
            "  confidence: 0.72\n"
            "  body: Priya moved to Seattle.\n"
            "needs_triage: true\n"
            "---\n\n"
            "Priya moved to Seattle."
        ),
    )

    with _client(vault_tmp, data_tmp, db) as client:
        accepted = client.post(
            f"/api/triage/note:{note['id']}/reclassify",
            json={"type": "person"},
        )

    assert accepted.status_code == 200, accepted.text
    assert (vault_tmp / "people" / "priya-shah.md").exists()
    assert db.execute("SELECT name FROM people WHERE slug = 'priya-shah'").fetchone()[
        "name"
    ] == "Priya Shah"


def test_birthday_reminders_dedupe_per_person_year_and_parse_yearless(
    db, vault_tmp, data_tmp
):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "UTC"\n'
        '[reminders]\ndaily_summary_time = "07:30"\n',
        encoding="utf-8",
    )
    from mastisk.people.sync import create_person_file
    from mastisk.settings import reload_settings

    reload_settings()
    create_person_file(name="Anjali Rao", birthday="06-11")

    from mastisk.agents.reminder_engine import people_dates_tick

    now = datetime(2026, 6, 11, 8, 0, tzinfo=UTC)
    assert people_dates_tick(now=now) == 1
    assert people_dates_tick(now=now) == 0

    row = db.execute(
        """SELECT entity_type, entity_id, fire_at, kind, status, title, body
           FROM reminders WHERE kind = 'followup'"""
    ).fetchone()
    assert dict(row) == {
        "entity_type": "person",
        "entity_id": "birthday:anjali-rao:2026",
        "fire_at": "2026-06-11T07:30:00+00:00",
        "kind": "followup",
        "status": "pending",
        "title": "Birthday today: Anjali Rao",
        "body": "Birthday today: Anjali Rao",
    }


def test_anniversary_reminder_uses_separate_yearly_key(db, vault_tmp, data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "UTC"\n'
        '[reminders]\ndaily_summary_time = "07:30"\n',
        encoding="utf-8",
    )
    from mastisk.people.sync import create_person_file
    from mastisk.settings import reload_settings

    reload_settings()
    create_person_file(name="Anjali Rao", anniversary="2012-06-11")

    from mastisk.agents.reminder_engine import people_dates_tick

    assert people_dates_tick(now=datetime(2026, 6, 11, 8, 0, tzinfo=UTC)) == 1
    row = db.execute(
        "SELECT entity_id, title FROM reminders WHERE kind = 'followup'"
    ).fetchone()
    assert dict(row) == {
        "entity_id": "anniversary:anjali-rao:2026",
        "title": "Anniversary today: Anjali Rao",
    }


def test_reminder_tick_continues_when_people_dates_tick_fails(
    db, data_tmp, monkeypatch, caplog
):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[notify]\nbackend = "ntfy"\nntfy_topic = "test"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    db.execute(
        """INSERT INTO reminders
           (fire_at, kind, status, title, body)
           VALUES ('2026-06-11T09:00:00+00:00', 'custom', 'pending', 'Reminder', 'Ship it')"""
    )
    sent: list[str] = []
    from mastisk.agents import reminder_engine

    monkeypatch.setattr(
        reminder_engine,
        "people_dates_tick",
        lambda *, now=None: (_ for _ in ()).throw(RuntimeError("people boom")),
    )
    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda title, body, url=None: sent.append(body) or True,
    )

    with caplog.at_level("ERROR", logger="mastisk.reminder_engine"):
        reminder_engine.reminder_tick(
            now=datetime(2026, 6, 11, 9, 0, tzinfo=UTC),
            ensure_daily_summary=False,
        )

    assert sent == ["Ship it"]
    assert "people_dates_tick failed; continuing reminder tick" in caplog.text


def test_person_follow_up_at_reminder_lifecycle(db, vault_tmp, data_tmp):
    from mastisk.people.sync import create_person_file, patch_person

    create_person_file(name="Anjali Rao")
    patch_person("anjali-rao", {"follow_up_at": "2026-06-20T09:00:00+00:00"})
    row = db.execute(
        """SELECT entity_type, entity_id, fire_at, kind, status, title, body
           FROM reminders WHERE kind = 'followup' AND entity_id = 'followup:anjali-rao'"""
    ).fetchone()
    assert dict(row) == {
        "entity_type": "person",
        "entity_id": "followup:anjali-rao",
        "fire_at": "2026-06-20T09:00:00+00:00",
        "kind": "followup",
        "status": "pending",
        "title": "Follow up: Anjali Rao",
        "body": "Follow up with Anjali Rao",
    }

    patch_person("anjali-rao", {"follow_up_at": "2026-06-21T10:00:00+00:00"})
    moved = db.execute(
        "SELECT fire_at, status FROM reminders WHERE entity_id = 'followup:anjali-rao'"
    ).fetchone()
    assert dict(moved) == {
        "fire_at": "2026-06-21T10:00:00+00:00",
        "status": "pending",
    }

    patch_person("anjali-rao", {"follow_up_at": None})
    cancelled = db.execute(
        "SELECT status, last_error FROM reminders WHERE entity_id = 'followup:anjali-rao'"
    ).fetchone()
    assert dict(cancelled) == {
        "status": "cancelled",
        "last_error": "person follow_up_at cleared",
    }


def test_person_follow_up_reminder_fires_once_and_clears_file(
    db, vault_tmp, data_tmp, monkeypatch
):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "UTC"\n'
        '[notify]\nbackend = "ntfy"\nntfy_topic = "test"\n',
        encoding="utf-8",
    )
    from mastisk.people.sync import create_person_file, patch_person, scan_people
    from mastisk.settings import reload_settings

    reload_settings()
    create_person_file(name="Anjali Rao")
    patch_person("anjali-rao", {"follow_up_at": "2026-06-20T09:00:00+00:00"})

    sent: list[tuple[str, str]] = []
    from mastisk.agents import reminder_engine

    monkeypatch.setattr(
        "mastisk.agents.reminder_engine.notify.send",
        lambda title, body, url=None: sent.append((title, body)) or True,
    )

    assert reminder_engine.reminder_tick(
        now=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        ensure_daily_summary=False,
    ) == 1
    assert sent == [("Follow up: Anjali Rao", "Follow up with Anjali Rao")]

    row = db.execute(
        "SELECT status FROM reminders WHERE entity_id = 'followup:anjali-rao'"
    ).fetchone()
    assert row["status"] == "sent"
    file_text = (vault_tmp / "people" / "anjali-rao.md").read_text(encoding="utf-8")
    assert "follow_up_at" not in file_text

    scan_people()
    assert reminder_engine.people_dates_tick(
        now=datetime(2026, 6, 20, 9, 1, tzinfo=UTC)
    ) == 0
    rows = db.execute(
        """SELECT status FROM reminders
           WHERE entity_id = 'followup:anjali-rao'
           ORDER BY id"""
    ).fetchall()
    assert [row["status"] for row in rows] == ["sent"]


def test_person_follow_up_can_be_scheduled_again_after_firing(
    db, vault_tmp, data_tmp, monkeypatch
):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "UTC"\n'
        '[notify]\nbackend = "ntfy"\nntfy_topic = "test"\n',
        encoding="utf-8",
    )
    from mastisk.people.sync import create_person_file, patch_person
    from mastisk.settings import reload_settings

    reload_settings()
    create_person_file(name="Anjali Rao")
    patch_person("anjali-rao", {"follow_up_at": "2026-06-20T09:00:00+00:00"})

    from mastisk.agents import reminder_engine

    monkeypatch.setattr("mastisk.agents.reminder_engine.notify.send", lambda *args, **kwargs: True)
    assert reminder_engine.reminder_tick(
        now=datetime(2026, 6, 20, 9, 0, tzinfo=UTC),
        ensure_daily_summary=False,
    ) == 1

    patch_person("anjali-rao", {"follow_up_at": "2026-06-25T10:00:00+00:00"})

    rows = db.execute(
        """SELECT fire_at, status, deleted_at FROM reminders
           WHERE entity_id = 'followup:anjali-rao'
           ORDER BY id"""
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {
            "fire_at": "2026-06-20T09:00:00+00:00",
            "status": "sent",
            "deleted_at": rows[0]["deleted_at"],
        },
        {
            "fire_at": "2026-06-25T10:00:00+00:00",
            "status": "pending",
            "deleted_at": None,
        },
    ]
    assert rows[0]["deleted_at"] is not None


def test_person_follow_up_reconcile_does_not_resurrect_cancelled_rows(
    db, vault_tmp, data_tmp
):
    from mastisk.agents.reminder_engine import cancel_reminder, people_dates_tick
    from mastisk.people.sync import create_person_file, patch_person, scan_people

    create_person_file(name="Anjali Rao")
    patch_person("anjali-rao", {"follow_up_at": "2026-06-20T09:00:00+00:00"})
    row = db.execute(
        "SELECT id FROM reminders WHERE entity_id = 'followup:anjali-rao'"
    ).fetchone()
    assert cancel_reminder(row["id"]) is not None

    scan_people()
    assert people_dates_tick(now=datetime(2026, 6, 19, 9, 0, tzinfo=UTC)) == 0

    rows = db.execute(
        """SELECT status FROM reminders
           WHERE entity_id = 'followup:anjali-rao'
           ORDER BY id"""
    ).fetchall()
    assert [row["status"] for row in rows] == ["cancelled"]
