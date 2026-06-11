"""Integration tests for the /api/capture ingress."""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
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


@pytest.fixture(autouse=True)
def fake_capture_router():
    async def _default(text: str, source: str, ts: str | None):
        return _capture(body=text)

    with patch(
        "mastisk.routes.capture.route_capture",
        new_callable=AsyncMock,
        side_effect=_default,
    ) as mock:
        yield mock


@pytest.fixture
def client_with_token(vault_tmp, data_tmp, db):
    """App with a configured capture token."""
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\nbearer_token = "test-token"\n')
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    with TestClient(create_app()) as c:
        yield c


@pytest.fixture
def client_no_token(vault_tmp, data_tmp, db):
    """App with no capture token configured."""
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_capture_rejects_missing_token(client_with_token):
    r = client_with_token.post("/api/capture", json={"text": "hi", "source": "watch"})
    assert r.status_code == 401


def test_capture_rejects_bad_token(client_with_token):
    r = client_with_token.post(
        "/api/capture",
        json={"text": "hi", "source": "watch"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert r.status_code == 401


def test_capture_rejects_non_ascii_token_without_500(client_with_token):
    r = client_with_token.post(
        "/api/capture",
        json={"text": "hi", "source": "watch"},
        headers=[(b"authorization", "Bearer café".encode())],
    )
    assert r.status_code == 401


def test_capture_503_when_unconfigured(client_no_token):
    r = client_no_token.post(
        "/api/capture",
        json={"text": "hi", "source": "watch"},
        headers={"Authorization": "Bearer anything"},
    )
    assert r.status_code == 503


def test_capture_reads_token_file_changes_after_startup(client_no_token, data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\nbearer_token = "runtime-token"\n')

    created = client_no_token.post(
        "/api/capture",
        json={"text": "written after startup", "source": "watch"},
        headers={"Authorization": "Bearer runtime-token"},
    )
    assert created.status_code == 201, created.text

    cfg.write_text('[capture]\nbearer_token = "rotated-token"\n')
    old = client_no_token.post(
        "/api/capture",
        json={"text": "old token should fail", "source": "watch"},
        headers={"Authorization": "Bearer runtime-token"},
    )
    assert old.status_code == 401


def test_capture_persists_note_with_watch_source(client_with_token, vault_tmp):
    r = client_with_token.post(
        "/api/capture",
        json={"text": "remember to water the plants", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "note"
    assert body["needs_triage"] is False
    file_path = vault_tmp / body["destination"]
    assert file_path.exists()
    file_text = file_path.read_text()
    assert "water the plants" in file_text
    assert not file_text.startswith("---\n")

    from mastisk.db.queries import connect, get_note

    with connect() as conn:
        row = get_note(conn, body["id"])
        assert row["source"] == "watch"


def test_capture_task_writes_today_journal_host(client_with_token, vault_tmp, fake_capture_router):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.93,
        body="call Sam",
        due="2026-06-10T14:00:00-07:00",
        priority="high",
        reminder_lead_minutes=15,
        tags=["follow-up"],
    )

    r = client_with_token.post(
        "/api/capture",
        json={
            "text": "remind me to call Sam tomorrow 2pm",
            "source": "watch",
            "ts": "2026-06-11T09:00:00-07:00",
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "task"
    assert body["needs_triage"] is False
    assert body["destination"] == "journal/2026-06-11.md"
    assert body["id"]

    file_text = (vault_tmp / body["destination"]).read_text(encoding="utf-8")
    assert file_text.startswith("## Tasks\n")
    assert "- [ ] call Sam 📅 2026-06-10 ⏰ 14:00 ⏫ #follow-up" in file_text
    assert "🆔 " in file_text

    from mastisk.db.queries import connect

    with connect() as conn:
        row = conn.execute("SELECT * FROM tasks WHERE uid = ?", (body["id"],)).fetchone()
    assert row is not None
    assert row["host_path"] == body["destination"]


def test_capture_task_preserves_due_time_and_reminder_facts(
    client_with_token, vault_tmp, db, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.93,
        body="call Sam",
        due="2026-06-10T14:00:00-07:00",
        reminder_lead_minutes=15,
        no_reminder=False,
        review_at="2026-06-10T13:45:00-07:00",
    )

    r = client_with_token.post(
        "/api/capture",
        json={
            "text": "remind me to call Sam tomorrow 2pm",
            "source": "watch",
            "ts": "2026-06-11T09:00:00-07:00",
        },
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    path = vault_tmp / body["destination"]
    assert "📅 2026-06-10 ⏰ 14:00" in path.read_text(encoding="utf-8")

    row = db.execute("SELECT * FROM tasks WHERE uid = ?", (body["id"],)).fetchone()
    assert row["due"] == "2026-06-10T14:00:00"
    assert row["reminder_lead_minutes"] == 15
    assert row["no_reminder"] == 0
    assert row["review_at"] == "2026-06-10T13:45:00-07:00"

    from mastisk.tasks.sync import scan_task_hosts

    scan_task_hosts([path])
    row = db.execute("SELECT * FROM tasks WHERE uid = ?", (body["id"],)).fetchone()
    assert row["due"] == "2026-06-10T14:00:00"
    assert row["reminder_lead_minutes"] == 15
    assert row["no_reminder"] == 0
    assert row["review_at"] == "2026-06-10T13:45:00-07:00"


def test_capture_task_creates_due_reminder_from_router_fields(
    client_with_token, db, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.93,
        body="call Sam",
        due="2099-01-01T14:00:00+00:00",
        reminder_lead_minutes=15,
        no_reminder=False,
    )

    r = client_with_token.post(
        "/api/capture",
        json={
            "text": "remind me to call Sam at 2pm",
            "source": "watch",
            "ts": "2026-06-11T09:00:00-07:00",
        },
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    row = db.execute(
        """SELECT entity_type, entity_id, fire_at, lead_minutes, kind, status
           FROM reminders WHERE entity_type = 'task' AND entity_id = ?""",
        (body["id"],),
    ).fetchone()
    assert dict(row) == {
        "entity_type": "task",
        "entity_id": body["id"],
        "fire_at": "2099-01-01T13:45:00+00:00",
        "lead_minutes": 15,
        "kind": "task_due",
        "status": "pending",
    }


def test_capture_task_no_reminder_suppresses_due_reminder(
    client_with_token, db, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.93,
        body="call Sam",
        due="2099-01-01T14:00:00+00:00",
        reminder_lead_minutes=15,
        no_reminder=True,
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "call Sam at 2pm no reminder", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    assert db.execute("SELECT COUNT(*) AS n FROM reminders").fetchone()["n"] == 0


def test_capture_task_skips_due_reminder_when_lead_instant_is_past(
    client_with_token, db, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.93,
        body="archive old box",
        due="2000-01-01T14:00:00+00:00",
        reminder_lead_minutes=15,
        no_reminder=False,
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "archive old box", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    assert db.execute("SELECT COUNT(*) AS n FROM reminders").fetchone()["n"] == 0


def test_capture_medium_confidence_marks_needs_triage(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="journal",
        confidence=0.72,
        body="felt scattered",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "felt scattered today", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "journal"
    assert body["needs_triage"] is True
    file_text = (vault_tmp / body["destination"]).read_text()
    assert "needs_triage: true" in file_text
    assert "confidence: 0.72" in file_text


def test_capture_task_routes_to_existing_project_host(
    client_with_token, vault_tmp, fake_capture_router
):
    created_project = client_with_token.post(
        "/api/projects",
        json={"name": "Mastisk", "type": "project", "domain": "work"},
    )
    assert created_project.status_code == 201, created_project.text

    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.96,
        body="ship phase three",
        project="mastisk",
        tags=["phase3"],
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "add ship phase three to the Mastisk project", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "task"
    assert body["destination"] == "projects/mastisk.md"
    project_text = (vault_tmp / "projects" / "mastisk.md").read_text(encoding="utf-8")
    assert "- [ ] ship phase three #phase3" in project_text


def test_capture_task_accepts_project_name_when_router_does_not_return_slug(
    client_with_token, vault_tmp, fake_capture_router
):
    client_with_token.post(
        "/api/projects",
        json={"name": "Mastisk", "type": "project", "domain": "work"},
    )
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.96,
        body="ship phase three",
        project="Mastisk",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "add ship phase three to the Mastisk project", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    assert r.json()["destination"] == "projects/mastisk.md"
    project_text = (vault_tmp / "projects" / "mastisk.md").read_text(encoding="utf-8")
    assert "- [ ] ship phase three" in project_text


def test_capture_project_update_appends_to_project_log(
    client_with_token, vault_tmp, fake_capture_router
):
    created_project = client_with_token.post(
        "/api/projects",
        json={"name": "Mastisk", "type": "project", "domain": "work"},
    )
    assert created_project.status_code == 201, created_project.text

    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="project_update",
        confidence=0.94,
        body="shipped capture routing",
        project="mastisk",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "add to the Mastisk project shipped capture routing", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "project_update"
    assert body["destination"] == "projects/mastisk.md"
    project_text = (vault_tmp / "projects" / "mastisk.md").read_text(encoding="utf-8")
    assert "## Log" in project_text
    assert "shipped capture routing" in project_text


def test_capture_mid_confidence_project_update_persists_triage_tag(
    client_with_token, vault_tmp, fake_capture_router
):
    created_project = client_with_token.post(
        "/api/projects",
        json={"name": "Mastisk", "type": "project", "domain": "work"},
    )
    assert created_project.status_code == 201, created_project.text

    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="project_update",
        confidence=0.72,
        body="shipped capture routing",
        project="mastisk",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "add to the Mastisk project shipped capture routing", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    assert r.json()["needs_triage"] is True
    project_text = (vault_tmp / "projects" / "mastisk.md").read_text(encoding="utf-8")
    assert "shipped capture routing #needs-triage" in project_text


def test_capture_low_confidence_falls_back_to_raw_inbox(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.2,
        body="cleaned task",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "ambiguous raw text", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "inbox"
    assert body["needs_triage"] is True
    file_text = (vault_tmp / body["destination"]).read_text()
    assert file_text == "ambiguous raw text"


def test_capture_model_inbox_always_falls_back_to_raw_triage(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="inbox",
        confidence=0.99,
        body="model cleaned this",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "keep this ambiguous thing", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "inbox"
    assert body["needs_triage"] is True
    assert (vault_tmp / body["destination"]).read_text() == "keep this ambiguous thing"


def test_capture_confidence_point_five_files_with_triage(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.5,
        body="call Sam",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "call Sam", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "task"
    assert body["needs_triage"] is True
    file_text = (vault_tmp / body["destination"]).read_text()
    assert "- [ ] call Sam" in file_text


def test_capture_mid_confidence_task_persists_triage_tag_and_mirror(
    client_with_token, vault_tmp, db, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.72,
        body="call Sam",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "call Sam", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["needs_triage"] is True
    path = vault_tmp / body["destination"]
    file_text = path.read_text(encoding="utf-8")
    assert "#needs-triage" in file_text
    row = db.execute(
        "SELECT needs_triage FROM tasks WHERE uid = ?",
        (body["id"],),
    ).fetchone()
    assert row["needs_triage"] == 1

    path.write_text(file_text.replace(" #needs-triage", ""), encoding="utf-8")
    from mastisk.tasks.sync import scan_task_hosts

    scan_task_hosts([path])
    row = db.execute(
        "SELECT needs_triage FROM tasks WHERE uid = ?",
        (body["id"],),
    ).fetchone()
    assert row["needs_triage"] == 0


def test_capture_confidence_point_eighty_five_files_direct(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.85,
        body="call Sam",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "call Sam", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "task"
    assert body["needs_triage"] is False
    file_text = (vault_tmp / body["destination"]).read_text()
    assert "- [ ] call Sam" in file_text


def test_capture_router_failure_falls_back_to_raw_inbox(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = RuntimeError("router unavailable")

    r = client_with_token.post(
        "/api/capture",
        json={"text": "do not lose this", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "inbox"
    assert body["needs_triage"] is True
    assert (vault_tmp / body["destination"]).read_text() == "do not lose this"


def test_capture_router_timeout_falls_back_to_raw_inbox(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = TimeoutError("router timed out")

    r = client_with_token.post(
        "/api/capture",
        json={"text": "timeout should not duplicate this", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "inbox"
    assert body["needs_triage"] is True
    assert (vault_tmp / body["destination"]).read_text() == "timeout should not duplicate this"


def test_command_detected_capture_skips_confidence_gate(
    client_with_token, vault_tmp, fake_capture_router
):
    command_capture = _capture(type="task", confidence=0.2, body="call Sam")
    command_capture.command_detected = True
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = command_capture

    r = client_with_token.post(
        "/api/capture",
        json={"text": "remind me to call Sam", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "task"
    assert body["needs_triage"] is False
    file_text = (vault_tmp / body["destination"]).read_text()
    assert "- [ ] call Sam" in file_text


def test_capture_routine_done_marks_completion_and_reports_streak(
    client_with_token, vault_tmp, fake_capture_router
):
    from mastisk.routines.sync import create_routine_file

    create_routine_file(name="Morning Vitamins", time_of_day="morning")
    command_capture = _capture(type="routine_done", confidence=0.2, body="did my vitamins")
    command_capture.command_detected = True
    command_capture.routine = "morning-vitamins"
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = command_capture

    r = client_with_token.post(
        "/api/capture",
        json={
            "text": "did my vitamins",
            "source": "watch",
            "ts": "2026-06-11T09:00:00-07:00",
        },
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "routine_done"
    assert body["routine_slug"] == "morning-vitamins"
    assert body["streak"]["current"] == 1
    file_text = (vault_tmp / "routines" / "morning-vitamins.md").read_text()
    assert "- 2026-06-11" in file_text


def test_capture_routine_done_archived_routine_falls_back_to_inbox(
    client_with_token, vault_tmp, fake_capture_router
):
    from mastisk.routines.sync import archive_routine, create_routine_file

    create_routine_file(name="Morning Vitamins", time_of_day="morning")
    archive_routine("morning-vitamins")
    command_capture = _capture(type="routine_done", confidence=0.2, body="did my vitamins")
    command_capture.command_detected = True
    command_capture.routine = "morning-vitamins"
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = command_capture

    r = client_with_token.post(
        "/api/capture",
        json={
            "text": "did my vitamins",
            "source": "watch",
            "ts": "2026-06-11T09:00:00-07:00",
        },
        headers={"Authorization": "Bearer test-token"},
    )

    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "inbox"
    file_text = (vault_tmp / "routines" / "morning-vitamins.md").read_text()
    assert "- 2026-06-11" not in file_text


def test_typed_capture_write_failure_rolls_back_and_returns_inbox_fallback(
    client_with_token, vault_tmp, db, fake_capture_router, monkeypatch
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.93,
        body="call Sam",
    )

    from mastisk.routes import notes
    from mastisk.tasks import sync as task_sync

    real_note_atomic_write = notes.atomic_write
    real_task_atomic_write = task_sync.atomic_write
    calls = 0

    def fail_first_task_write(target, content):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("simulated frontmatter write failure")
        return real_task_atomic_write(target, content)

    monkeypatch.setattr(task_sync, "atomic_write", fail_first_task_write)
    monkeypatch.setattr(notes, "atomic_write", real_note_atomic_write)

    r = client_with_token.post(
        "/api/capture",
        json={"text": "remind me to call Sam", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["type"] == "inbox"
    assert body["needs_triage"] is True
    file_text = (vault_tmp / body["destination"]).read_text()
    assert file_text == "remind me to call Sam"
    assert calls == 1

    rows = db.execute("SELECT body, path FROM notes").fetchall()
    assert len(rows) == 1
    assert rows[0]["body"] == "remind me to call Sam"
    assert db.execute("SELECT COUNT(*) AS n FROM tasks").fetchone()["n"] == 0


def test_capture_appears_in_notes_list(client_with_token):
    client_with_token.post(
        "/api/capture",
        json={"text": "captured from the wrist", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    listing = client_with_token.get("/api/notes").json()
    assert any("wrist" in (n.get("summary") or n.get("slug") or "") for n in listing)


def test_capture_rejects_blank_text(client_with_token):
    r = client_with_token.post(
        "/api/capture",
        json={"text": "   ", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 422


def test_notetaker_skips_routed_typed_capture_without_classifying(
    client_with_token, vault_tmp, fake_capture_router
):
    fake_capture_router.side_effect = None
    fake_capture_router.return_value = _capture(
        type="task",
        confidence=0.93,
        body="call Sam",
        due="2026-06-10T14:00:00-07:00",
    )

    r = client_with_token.post(
        "/api/capture",
        json={"text": "remind me to call Sam tomorrow 2pm", "source": "watch"},
        headers={"Authorization": "Bearer test-token"},
    )
    assert r.status_code == 201, r.text
    destination = r.json()["destination"]
    assert not destination.startswith("_notes/")
    file_path = vault_tmp / destination
    before = file_path.read_text()
    assert "- [ ] call Sam" in before

    from mastisk.agents.notetaker import Notetaker

    notetaker = Notetaker()
    stat = file_path.stat()
    notetaker._stability_cache[str(file_path.resolve())] = (
        stat.st_mtime,
        stat.st_size,
        time.time() - 31,
    )
    with patch(
        "mastisk.agents.notetaker.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as classify_mock:
        asyncio.run(notetaker.run_once())

    classify_mock.assert_not_called()
    assert file_path.read_text() == before
