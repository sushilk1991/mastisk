from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db, **kwargs):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app(), **kwargs)


def _capture(**overrides):
    from mastisk.capture.router import Capture

    data = {
        "type": "quote",
        "confidence": 0.72,
        "title": "Conversation with Ada",
        "body": "The map is not the territory.",
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
        "tags": ["epistemics"],
        "related": [],
        "command_detected": False,
    }
    data.update(overrides)
    return Capture(**data)


def test_capture_triage_frontend_client_stays_outside_capture_tunnel_scope():
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "`${BASE}/triage?limit=${limit}`" in api_source
    assert "/capture/triage" not in api_source


def test_capture_triage_frontend_hides_routine_done_without_candidate():
    view_source = Path("frontend/src/components/DashboardViews.tsx").read_text(
        encoding="utf-8"
    )
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "function hasRoutineCandidate" in view_source
    assert "target !== 'routine_done' || hasRoutineCandidate(item)" in view_source
    assert "await throwApiError(r)" in api_source


def test_capture_triage_frontend_labels_task_dismiss_as_keep_task():
    view_source = Path("frontend/src/components/DashboardViews.tsx").read_text(
        encoding="utf-8"
    )

    assert "item.kind === 'task' ? 'keep task' : 'dismiss'" in view_source


def test_capture_triage_lists_persisted_triage_shapes(db, vault_tmp, data_tmp):
    from mastisk.journal import append_log
    from mastisk.projects.sync import append_project_log, create_project_file
    from mastisk.routes.notes import persist_note_capture
    from mastisk.tasks.sync import append_task_to_host

    append_task_to_host(
        vault_tmp / "journal" / "2026-06-11.md",
        text="Call Sam",
        tags=["needs-triage"],
        uid="triagetask",
    )
    append_log("2026-06-11", "Felt scattered #needs-triage", datetime(2026, 6, 11, 9, 0))
    create_project_file(name="Mastisk", domain="work")
    append_project_log("mastisk", "shipped capture routing #needs-triage")
    persist_note_capture(
        body="save quote about local-first",
        source="watch",
        file_content=(
            "---\n"
            "capture:\n"
            "  type: quote\n"
            "  confidence: 0.72\n"
            "  body: save quote about local-first\n"
            "needs_triage: true\n"
            "---\n\n"
            "save quote about local-first"
        ),
    )

    with _client(vault_tmp, data_tmp, db) as client:
        r = client.get("/api/triage")
        old = client.get("/api/capture/triage")

    assert r.status_code == 200, r.text
    assert old.status_code == 404
    rows = r.json()
    ids = {row["id"] for row in rows}
    assert "task:triagetask" in ids
    assert any(row_id.startswith("journal:2026-06-11:") for row_id in ids)
    assert any(row_id.startswith("project:mastisk:") for row_id in ids)
    note = next(row for row in rows if row["kind"] == "note")
    assert note["detected_type"] == "quote"
    assert note["confidence"] == 0.72


def test_capture_triage_accept_task_clears_task_marker(db, vault_tmp, data_tmp):
    from mastisk.tasks.sync import append_task_to_host

    append_task_to_host(
        vault_tmp / "journal" / "2026-06-11.md",
        text="Call Sam",
        tags=["needs-triage"],
        uid="accepttask",
    )

    with _client(vault_tmp, data_tmp, db) as client:
        r = client.post("/api/triage/task:accepttask/reclassify", json={"type": "task"})

    assert r.status_code == 200, r.text
    file_text = (vault_tmp / "journal" / "2026-06-11.md").read_text(encoding="utf-8")
    assert "#needs-triage" not in file_text
    row = db.execute("SELECT needs_triage FROM tasks WHERE uid = 'accepttask'").fetchone()
    assert row["needs_triage"] == 0


def test_capture_triage_reclassifies_task_away_by_demoting_original_task(
    db, vault_tmp, data_tmp
):
    from mastisk.tasks.sync import append_task_to_host

    append_task_to_host(
        vault_tmp / "journal" / "2026-06-11.md",
        text="Call Sam",
        due="2026-06-12",
        tags=["needs-triage"],
        uid="awaytask",
    )

    with _client(vault_tmp, data_tmp, db) as client:
        r = client.post("/api/triage/task:awaytask/reclassify", json={"type": "journal"})

    assert r.status_code == 200, r.text
    file_text = (vault_tmp / "journal" / "2026-06-11.md").read_text(encoding="utf-8")
    assert "- Call Sam" in file_text
    assert "- [ ] Call Sam" not in file_text
    assert "awaytask" not in file_text
    assert "#needs-triage" not in file_text
    open_task = db.execute(
        "SELECT uid FROM tasks WHERE uid = 'awaytask' AND deleted_at IS NULL"
    ).fetchone()
    assert open_task is None


def test_capture_triage_dismiss_task_keeps_task_and_clears_triage_marker(
    db, vault_tmp, data_tmp
):
    from mastisk.tasks.sync import append_task_to_host

    append_task_to_host(
        vault_tmp / "journal" / "2026-06-11.md",
        text="Call Sam",
        tags=["needs-triage"],
        uid="dismisstask",
    )

    with _client(vault_tmp, data_tmp, db) as client:
        r = client.post("/api/triage/task:dismisstask/reclassify", json={"type": "dismiss"})

    assert r.status_code == 200, r.text
    file_text = (vault_tmp / "journal" / "2026-06-11.md").read_text(encoding="utf-8")
    assert "- [ ] Call Sam" in file_text
    assert "dismisstask" in file_text
    assert "#needs-triage" not in file_text
    row = db.execute(
        "SELECT needs_triage, deleted_at FROM tasks WHERE uid = 'dismisstask'"
    ).fetchone()
    assert dict(row) == {"needs_triage": 0, "deleted_at": None}


def test_capture_triage_reclassifies_journal_line_to_task_without_deleting_log(
    db, vault_tmp, data_tmp
):
    from mastisk.journal import append_log

    append_log("2026-06-11", "Call Sam #needs-triage", datetime(2026, 6, 11, 9, 0))

    with _client(vault_tmp, data_tmp, db) as client:
        item_id = next(
            row["id"]
            for row in client.get("/api/triage").json()
            if row["kind"] == "journal"
        )
        r = client.post(
            f"/api/triage/{item_id}/reclassify",
            json={"type": "task"},
        )

    assert r.status_code == 200, r.text
    file_text = (vault_tmp / "journal" / "2026-06-11.md").read_text(encoding="utf-8")
    assert "- 09:00 Call Sam" in file_text
    assert "- [ ] Call Sam" in file_text
    assert "#needs-triage" not in file_text
    task = db.execute("SELECT text, needs_triage FROM tasks").fetchone()
    assert dict(task) == {"text": "Call Sam", "needs_triage": 0}


def test_capture_triage_clear_failure_does_not_file_duplicate_on_retry(
    db, vault_tmp, data_tmp, monkeypatch
):
    from mastisk.capture import triage
    from mastisk.journal import append_log

    append_log("2026-06-11", "Call Sam #needs-triage", datetime(2026, 6, 11, 9, 0))
    original_clear = triage._clear_triage_marker
    calls = {"n": 0}

    def fail_first_clear(item, *, target_type):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("clear failed")
        return original_clear(item, target_type=target_type)

    monkeypatch.setattr(triage, "_clear_triage_marker", fail_first_clear)

    with _client(
        vault_tmp, data_tmp, db, raise_server_exceptions=False
    ) as client:
        item_id = next(
            row["id"]
            for row in client.get("/api/triage").json()
            if row["kind"] == "journal"
        )
        first = client.post(f"/api/triage/{item_id}/reclassify", json={"type": "task"})
        retry_id = next(
            row["id"]
            for row in client.get("/api/triage").json()
            if row["kind"] == "journal"
        )
        retry = client.post(f"/api/triage/{retry_id}/reclassify", json={"type": "task"})

    assert first.status_code == 500, first.text
    assert retry.status_code == 200, retry.text
    file_text = (vault_tmp / "journal" / "2026-06-11.md").read_text(encoding="utf-8")
    assert "#needs-triage" not in file_text
    tasks = db.execute(
        "SELECT text, needs_triage FROM tasks WHERE deleted_at IS NULL"
    ).fetchall()
    assert [dict(row) for row in tasks] == [{"text": "Call Sam", "needs_triage": 0}]


def test_capture_triage_reclassifies_typed_note_to_task_and_clears_frontmatter(
    db, vault_tmp, data_tmp
):
    from mastisk.routes.notes import persist_note_capture

    note = persist_note_capture(
        body="call Sam",
        source="watch",
        file_content=(
            "---\n"
            "capture:\n"
            "  type: journal\n"
            "  confidence: 0.66\n"
            "  body: call Sam\n"
            "needs_triage: true\n"
            "---\n\n"
            "call Sam"
        ),
    )

    with _client(vault_tmp, data_tmp, db) as client:
        r = client.post(
            f"/api/triage/note:{note['id']}/reclassify",
            json={"type": "task"},
        )

    assert r.status_code == 200, r.text
    note_text = (vault_tmp / note["path"]).read_text(encoding="utf-8")
    assert "needs_triage: false" in note_text
    assert "type: task" in note_text
    task = db.execute("SELECT text, needs_triage FROM tasks").fetchone()
    assert dict(task) == {"text": "call Sam", "needs_triage": 0}


def test_capture_triage_reclassifies_typed_note_to_note_in_place(
    db, vault_tmp, data_tmp
):
    from mastisk.routes.notes import persist_note_capture

    note = persist_note_capture(
        body="save this as a normal note",
        source="watch",
        file_content=(
            "---\n"
            "capture:\n"
            "  type: journal\n"
            "  confidence: 0.61\n"
            "  body: save this as a normal note\n"
            "needs_triage: true\n"
            "---\n\n"
            "save this as a normal note"
        ),
    )

    with _client(vault_tmp, data_tmp, db) as client:
        r = client.post(
            f"/api/triage/note:{note['id']}/reclassify",
            json={"type": "note"},
        )

    assert r.status_code == 200, r.text
    notes = db.execute(
        "SELECT id, path FROM notes WHERE deleted_at IS NULL ORDER BY id"
    ).fetchall()
    assert [row["id"] for row in notes] == [note["id"]]
    assert len(list((vault_tmp / "_notes" / "inbox").glob("*.md"))) == 1
    note_text = (vault_tmp / note["path"]).read_text(encoding="utf-8")
    assert "needs_triage: false" in note_text
    assert "type: note" in note_text


def test_capture_triage_accepts_medium_confidence_quote_to_library(
    db, vault_tmp, data_tmp
):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\nbearer_token = "test-token"\n', encoding="utf-8")

    with _client(vault_tmp, data_tmp, db) as client, patch(
        "mastisk.routes.capture.route_capture", new_callable=AsyncMock
    ) as router:
        router.return_value = _capture()
        captured = client.post(
            "/api/capture",
            json={"text": "save quote maybe: The map is not the territory.", "source": "watch"},
            headers={"Authorization": "Bearer test-token"},
        )
        assert captured.status_code == 201, captured.text
        assert captured.json()["type"] == "quote"
        assert captured.json()["needs_triage"] is True
        assert db.execute("SELECT COUNT(*) AS n FROM quotes").fetchone()["n"] == 0

        item = next(row for row in client.get("/api/triage").json() if row["detected_type"] == "quote")
        accepted = client.post(
            f"/api/triage/{item['id']}/reclassify",
            json={"type": "quote"},
        )

    assert accepted.status_code == 200, accepted.text
    quote = db.execute("SELECT id, path, source_type, source_ref FROM quotes").fetchone()
    assert quote["source_type"] == "conversation"
    assert quote["source_ref"] == "Conversation with Ada"
    assert (vault_tmp / quote["path"]).exists()
    note_text = (vault_tmp / captured.json()["destination"]).read_text(encoding="utf-8")
    assert "needs_triage: false" in note_text
    assert client.get("/api/triage").json() == []


def test_capture_triage_routine_done_without_candidate_returns_422_and_keeps_marker(
    db, vault_tmp, data_tmp
):
    from mastisk.routes.notes import persist_note_capture

    note = persist_note_capture(
        body="did something routine-ish",
        source="watch",
        file_content=(
            "---\n"
            "capture:\n"
            "  type: note\n"
            "  confidence: 0.58\n"
            "  body: did something routine-ish\n"
            "needs_triage: true\n"
            "---\n\n"
            "did something routine-ish"
        ),
    )

    with _client(vault_tmp, data_tmp, db) as client:
        r = client.post(
            f"/api/triage/note:{note['id']}/reclassify",
            json={"type": "routine_done"},
        )

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "routine_done requires a routine candidate"
    note_text = (vault_tmp / note["path"]).read_text(encoding="utf-8")
    assert "needs_triage: true" in note_text


def test_capture_triage_routine_done_unknown_candidate_returns_422_and_keeps_marker(
    db, vault_tmp, data_tmp
):
    from mastisk.routes.notes import persist_note_capture

    note = persist_note_capture(
        body="did my missing routine",
        source="watch",
        file_content=(
            "---\n"
            "capture:\n"
            "  type: routine_done\n"
            "  routine: missing-routine\n"
            "  confidence: 0.58\n"
            "  body: did my missing routine\n"
            "needs_triage: true\n"
            "---\n\n"
            "did my missing routine"
        ),
    )

    with _client(vault_tmp, data_tmp, db) as client:
        r = client.post(
            f"/api/triage/note:{note['id']}/reclassify",
            json={"type": "routine_done"},
        )

    assert r.status_code == 422, r.text
    assert r.json()["detail"] == "routine not found: missing-routine"
    note_text = (vault_tmp / note["path"]).read_text(encoding="utf-8")
    assert "needs_triage: true" in note_text
