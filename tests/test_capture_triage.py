from __future__ import annotations

from datetime import datetime
from pathlib import Path

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def test_capture_triage_frontend_client_stays_outside_capture_tunnel_scope():
    api_source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "`${BASE}/triage?limit=${limit}`" in api_source
    assert "/capture/triage" not in api_source


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
