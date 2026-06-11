from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient
from starlette.responses import Response


@pytest.fixture
def client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    with TestClient(create_app()) as c:
        yield c


def _create_task(client: TestClient, text: str) -> dict:
    response = client.post("/api/tasks", json={"text": text})
    assert response.status_code == 201, response.text
    return response.json()


def test_focus_cap_409_shape_and_atomic_swap(client):
    tasks = [_create_task(client, f"Focus {idx}") for idx in range(1, 5)]
    day = "2026-06-11"

    for task in tasks[:3]:
        response = client.post(f"/api/focus/{day}", json={"task_uid": task["uid"]})
        assert response.status_code == 201, response.text

    full = client.post(f"/api/focus/{day}", json={"task_uid": tasks[3]["uid"]})
    assert full.status_code == 409
    detail = full.json()["detail"]
    assert detail["error"] == "focus_full"
    assert [row["uid"] for row in detail["focus"]] == [task["uid"] for task in tasks[:3]]

    swapped = client.post(
        f"/api/focus/{day}",
        json={"task_uid": tasks[3]["uid"], "replace_uid": tasks[0]["uid"]},
    )
    assert swapped.status_code == 200, swapped.text
    assert [row["uid"] for row in swapped.json()] == [
        tasks[3]["uid"],
        tasks[1]["uid"],
        tasks[2]["uid"],
    ]


def test_focus_keeps_completed_tasks_visible_for_the_day(client):
    task = _create_task(client, "Keep historical focus")
    day = "2026-06-11"
    assert client.post(f"/api/focus/{day}", json={"task_uid": task["uid"]}).status_code == 201

    toggled = client.patch(f"/api/tasks/{task['uid']}/toggle")
    assert toggled.status_code == 200

    focus = client.get(f"/api/focus/{day}")
    assert focus.status_code == 200
    assert focus.json()[0]["uid"] == task["uid"]
    assert focus.json()[0]["status"] == "done"
    assert focus.json()[0]["checked"] is True


def test_slipping_scan_windows_overrides_snooze_mute_and_rebuild_idempotency(
    db, data_tmp
):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        "[dashboard]\nslipping_project_days = 14\nslipping_task_days = 7\n",
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    db.execute(
        """INSERT INTO projects
           (slug, path, name, type, status, last_activity_at)
           VALUES
             ('stale-project', 'projects/stale-project.md', 'Stale Project', 'project', 'active', '2026-05-20T09:00:00+00:00'),
             ('fresh-project', 'projects/fresh-project.md', 'Fresh Project', 'project', 'active', '2026-06-05T09:00:00+00:00'),
             ('stale-area', 'projects/stale-area.md', 'Stale Area', 'area', 'active', '2026-05-20T09:00:00+00:00'),
             ('snoozed-project', 'projects/snoozed-project.md', 'Snoozed', 'project', 'active', '2026-05-20T09:00:00+00:00')"""
    )
    db.execute(
        "UPDATE projects SET slipping_muted_until = '2026-06-18' WHERE slug = 'snoozed-project'"
    )
    db.execute(
        """INSERT INTO tasks
           (uid, host_path, line_number, text, checked, status, last_activity_at, staleness_days, slipping_muted, tags_json, links_json)
           VALUES
             ('stale-task', 'journal/2026-06-01.md', 1, 'Stale Task', 0, 'open', '2026-06-01T09:00:00+00:00', NULL, 0, '[]', '[]'),
             ('override-task', 'journal/2026-06-01.md', 2, 'Override Task', 0, 'open', '2026-06-01T09:00:00+00:00', 30, 0, '[]', '[]'),
             ('muted-task', 'journal/2026-06-01.md', 3, 'Muted Task', 0, 'open', '2026-06-01T09:00:00+00:00', NULL, 1, '[]', '[]'),
             ('done-task', 'journal/2026-06-01.md', 4, 'Done Task', 1, 'done', '2026-06-01T09:00:00+00:00', NULL, 0, '[]', '[]')"""
    )

    from mastisk.dashboard.intelligence import slipping_scan

    first = slipping_scan(now=datetime(2026, 6, 11, 12, 0, tzinfo=UTC))
    second = slipping_scan(now=datetime(2026, 6, 11, 12, 0, tzinfo=UTC))

    rows = db.execute(
        "SELECT entity_type, entity_id, stale_since FROM slipping ORDER BY entity_type, entity_id"
    ).fetchall()
    assert first == 3
    assert second == 3
    assert [tuple(row) for row in rows] == [
        ("project", "stale-area", "2026-06-03"),
        ("project", "stale-project", "2026-06-03"),
        ("task", "stale-task", "2026-06-08"),
    ]


def test_slipping_route_lists_and_snoozes_items(client, db):
    db.execute(
        """INSERT INTO tasks
           (uid, host_path, line_number, text, checked, status, last_activity_at, tags_json, links_json)
           VALUES ('slip1', 'journal/2026-06-01.md', 1, 'Slipping task', 0, 'open', '2026-06-01T09:00:00+00:00', '[]', '[]')"""
    )
    from mastisk.dashboard.intelligence import slipping_scan

    slipping_scan(now=datetime(2026, 6, 11, 12, 0, tzinfo=UTC))
    listed = client.get("/api/slipping")
    assert listed.status_code == 200
    assert listed.json()[0]["entity_id"] == "slip1"

    snoozed = client.post("/api/slipping/task/slip1/snooze", json={"days": 7})
    assert snoozed.status_code == 200, snoozed.text
    slipping_scan(now=datetime(2026, 6, 11, 12, 0, tzinfo=UTC))
    assert client.get("/api/slipping").json() == []


def test_resurfacing_is_deterministic_and_empty_pool_hides_card(client, db):
    empty = client.get("/api/resurface/2026-06-11")
    assert empty.status_code == 204

    for idx in range(1, 4):
        db.execute(
            """INSERT INTO notes
               (slug, path, body, body_sha256, source, created_at, classified_at, classification, summary, confidence)
               VALUES (?, ?, ?, ?, 'cli', ?, ?, 'idea', ?, 0.8)""",
            (
                f"note-{idx}",
                f"_notes/note-{idx}.md",
                f"Body {idx} with enough text for an excerpt.",
                f"sha-{idx}",
                f"2026-06-0{idx}T09:00:00+00:00",
                f"2026-06-0{idx}T09:01:00+00:00",
                f"Summary {idx}",
            ),
        )

    first = client.get("/api/resurface/2026-06-11").json()
    repeat = client.get("/api/resurface/2026-06-11").json()
    next_day = client.get("/api/resurface/2026-06-12").json()
    assert first == repeat
    assert first["id"] != next_day["id"]
    assert first["kind"] == "note"
    assert first["link"].startswith("/notes/")


def test_resurfacing_empty_pool_route_returns_explicit_empty_204(monkeypatch):
    from mastisk.routes import dashboard_intelligence as route

    monkeypatch.setattr(route, "resurface_for_date", lambda day: None)

    response = asyncio.run(route.resurface_endpoint("2026-06-11"))

    assert isinstance(response, Response)
    assert response.status_code == 204
    assert response.body == b""


def test_needs_review_scan_reasons_dismiss_and_triage_age_rule(
    client, db, vault_tmp, data_tmp
):
    cfg = data_tmp / "config.toml"
    cfg.write_text("[dashboard]\ntriage_reminder_days = 3\n", encoding="utf-8")
    from mastisk.settings import reload_settings

    reload_settings()
    note_path = vault_tmp / "_notes" / "review-note.md"
    note_path.parent.mkdir(parents=True)
    note_path.write_text(
        "---\n"
        "capture:\n"
        "  type: note\n"
        "  review_at: '2026-06-11'\n"
        "needs_triage: false\n"
        "---\n\nReview this note.",
        encoding="utf-8",
    )
    db.execute(
        """INSERT INTO notes
           (id, slug, path, body, body_sha256, source, created_at, classified_at, classification, summary)
           VALUES (101, 'review-note', '_notes/review-note.md', 'Review this note.', 'sha-note', 'cli',
                   '2026-06-01T09:00:00+00:00', '2026-06-01T09:01:00+00:00', 'idea', 'Review note')"""
    )
    db.execute(
        """INSERT INTO tasks
           (uid, host_path, line_number, text, checked, status, review_at, updated_at, tags_json, links_json)
           VALUES
             ('review-task', 'journal/2026-06-01.md', 1, 'Review Task', 0, 'open', '2026-06-11', '2026-06-10T09:00:00+00:00', '[]', '[]'),
             ('triage-task', 'journal/2026-06-01.md', 2, 'Triage Task', 0, 'open', NULL, '2026-06-07T09:00:00+00:00', '[\"needs-triage\"]', '[]')"""
    )
    db.execute("UPDATE tasks SET needs_triage = 1 WHERE uid = 'triage-task'")

    from mastisk.dashboard.intelligence import needs_review_scan

    assert needs_review_scan(today=date(2026, 6, 11)) == 3
    listed = client.get("/api/needs-review")
    assert listed.status_code == 200
    reasons = {item["reason"] for item in listed.json()}
    assert reasons == {"note_review_due", "task_review_due", "triage_stale"}

    dismissed_id = listed.json()[0]["id"]
    dismissed = client.post(f"/api/needs-review/{dismissed_id}/dismiss")
    assert dismissed.status_code == 200, dismissed.text
    assert len(client.get("/api/needs-review").json()) == 2


def test_needs_review_triage_age_survives_passive_task_scan(db, vault_tmp, data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text("[dashboard]\ntriage_reminder_days = 3\n", encoding="utf-8")
    from mastisk.settings import reload_settings
    from mastisk.tasks.sync import append_task_to_host, scan_task_hosts

    reload_settings()
    task = append_task_to_host(
        vault_tmp / "journal" / "2026-06-01.md",
        text="Passive scan should not refresh triage age",
        uid="passive-triage",
        tags=["needs-triage"],
    )
    db.execute(
        "UPDATE tasks SET updated_at = '2026-06-07T09:00:00+00:00' WHERE uid = ?",
        (task["uid"],),
    )

    scan_task_hosts([vault_tmp / task["host_path"]])

    from mastisk.dashboard.intelligence import needs_review_scan

    needs_review_scan(today=date(2026, 6, 11))
    rows = db.execute(
        "SELECT entity_id, reason FROM needs_review WHERE dismissed_at IS NULL"
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"entity_id": "passive-triage", "reason": "triage_stale"}
    ]


def test_needs_review_clears_when_triage_item_is_resolved(client, db, vault_tmp, data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text("[dashboard]\ntriage_reminder_days = 3\n", encoding="utf-8")
    from mastisk.settings import reload_settings
    from mastisk.tasks.sync import append_task_to_host

    reload_settings()
    task = append_task_to_host(
        vault_tmp / "journal" / "2026-06-01.md",
        text="Resolve triage card",
        uid="resolve-triage",
        tags=["needs-triage"],
    )
    db.execute(
        "UPDATE tasks SET updated_at = '2026-06-07T09:00:00+00:00' WHERE uid = ?",
        (task["uid"],),
    )
    from mastisk.dashboard.intelligence import needs_review_scan

    needs_review_scan(today=date(2026, 6, 11))
    assert any(item["entity_id"] == task["uid"] for item in client.get("/api/needs-review").json())

    resolved = client.post(f"/api/triage/task:{task['uid']}/reclassify", json={"type": "dismiss"})
    assert resolved.status_code == 200, resolved.text
    assert not any(item["entity_id"] == task["uid"] for item in client.get("/api/needs-review").json())


def test_daily_summary_includes_needs_review_count_when_nonzero():
    from mastisk.agents.reminder_engine import compose_daily_summary

    _title, body = compose_daily_summary(
        today=date(2026, 6, 11),
        open_tasks=[{"text": "Due", "due": "2026-06-11"}],
        needs_review_count=2,
    )

    assert "2 need review." in body


def test_activity_bumps_task_edits_project_task_capture_and_journal_project_log(
    db, vault_tmp
):
    from mastisk.journal import append_log
    from mastisk.projects.sync import create_project_file
    from mastisk.tasks.sync import append_task_to_host, rewrite_task

    project = create_project_file(name="Activity Project", type="project")
    db.execute(
        "UPDATE projects SET last_activity_at = '2026-06-01T09:00:00+00:00' WHERE slug = ?",
        (project["slug"],),
    )
    project_task = append_task_to_host(
        vault_tmp / project["path"],
        text="Captured into project",
        uid="activity-project-task",
    )
    bumped_project = db.execute(
        "SELECT last_activity_at FROM projects WHERE slug = ?",
        (project["slug"],),
    ).fetchone()["last_activity_at"]
    assert bumped_project != "2026-06-01T09:00:00+00:00"

    db.execute(
        "UPDATE tasks SET last_activity_at = '2026-06-01T09:00:00+00:00' WHERE uid = ?",
        (project_task["uid"],),
    )
    rewrite_task(project_task["uid"], due="2026-06-20")
    edited_task = db.execute(
        "SELECT last_activity_at FROM tasks WHERE uid = ?",
        (project_task["uid"],),
    ).fetchone()["last_activity_at"]
    assert edited_task != "2026-06-01T09:00:00+00:00"

    journal_task = append_task_to_host(
        vault_tmp / "journal" / "2026-06-11.md",
        text="Journal-hosted project action",
        uid="journal-project-task",
    )
    db.execute(
        "UPDATE tasks SET project = ?, last_activity_at = '2026-06-01T09:00:00+00:00' WHERE uid = ?",
        (project["slug"], journal_task["uid"]),
    )
    db.execute(
        "UPDATE projects SET last_activity_at = '2026-06-01T09:00:00+00:00' WHERE slug = ?",
        (project["slug"],),
    )
    append_log("2026-06-11", "Moved the project forward", datetime(2026, 6, 11, 10, 0))
    journal_bump = db.execute(
        "SELECT last_activity_at FROM projects WHERE slug = ?",
        (project["slug"],),
    ).fetchone()["last_activity_at"]
    assert journal_bump != "2026-06-01T09:00:00+00:00"


def test_slipping_cache_invalidates_on_task_activity(db, vault_tmp):
    from mastisk.dashboard.intelligence import list_slipping, slipping_scan
    from mastisk.tasks.sync import append_task_to_host, rewrite_task

    task = append_task_to_host(
        vault_tmp / "journal" / "2026-06-01.md",
        text="Stale until touched",
        uid="slip-touch",
    )
    db.execute(
        "UPDATE tasks SET last_activity_at = '2026-06-01T09:00:00+00:00' WHERE uid = ?",
        (task["uid"],),
    )
    slipping_scan(now=datetime(2026, 6, 11, 12, 0, tzinfo=UTC))
    assert [item["entity_id"] for item in list_slipping()] == [task["uid"]]

    rewrite_task(task["uid"], due="2026-06-20")

    assert list_slipping() == []
