from __future__ import annotations

import calendar
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_project_scan_syncs_milestones_and_task_scan_excludes_them(db, vault_tmp):
    from mastisk.projects.sync import project_payload, scan_projects
    from mastisk.tasks.sync import scan_task_hosts

    path = vault_tmp / "projects" / "mastisk.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "name: Mastisk\n"
        "type: project\n"
        "status: active\n"
        "---\n\n"
        "## Tasks\n"
        "- [ ] Real task 🆔 task1\n\n"
        "## Milestones\n"
        "- [ ] Alpha 🆔 oldmilestone\n"
        "- [x] Beta\n",
        encoding="utf-8",
    )

    scan_projects([path])
    scan_task_hosts([path], uid_factory=lambda: "baduid")

    milestones = db.execute(
        "SELECT position, text, done FROM milestones WHERE project_slug = 'mastisk' ORDER BY position"
    ).fetchall()
    assert [dict(row) for row in milestones] == [
        {"position": 1, "text": "Alpha", "done": 0},
        {"position": 2, "text": "Beta", "done": 1},
    ]
    tasks = db.execute(
        "SELECT uid, text FROM tasks WHERE deleted_at IS NULL ORDER BY uid"
    ).fetchall()
    assert [dict(row) for row in tasks] == [{"uid": "task1", "text": "Real task"}]
    assert "baduid" not in path.read_text(encoding="utf-8")
    assert "oldmilestone" in path.read_text(encoding="utf-8")

    payload = project_payload("mastisk")
    assert payload is not None
    assert payload["milestone_progress"] == {"done": 1, "total": 2, "percent": 50}


def test_milestone_routes_append_and_toggle_file_first(client, db, vault_tmp):
    project = client.post("/api/projects", json={"name": "Mastisk"}).json()

    created = client.post(
        f"/api/projects/{project['slug']}/milestones",
        json={"text": "Launch beta"},
    )
    assert created.status_code == 201, created.text
    assert created.json()["milestone_progress"] == {"done": 0, "total": 1, "percent": 0}

    toggled = client.patch(
        f"/api/projects/{project['slug']}/milestones/1",
        json={"done": True},
    )
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["milestone_progress"] == {"done": 1, "total": 1, "percent": 100}

    file_text = (vault_tmp / "projects" / "mastisk.md").read_text(encoding="utf-8")
    assert "- [x] Launch beta" in file_text
    row = db.execute(
        "SELECT done FROM milestones WHERE project_slug = ? AND position = 1",
        (project["slug"],),
    ).fetchone()
    assert row["done"] == 1


def test_milestone_toggle_ignores_blank_checkbox_lines(db, vault_tmp):
    from mastisk.projects.sync import scan_projects, set_project_milestone_done

    path = vault_tmp / "projects" / "mastisk.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: Mastisk\nstatus: active\n---\n\n"
        "## Milestones\n"
        "- [ ]\n"
        "- [ ] Alpha\n",
        encoding="utf-8",
    )
    scan_projects([path])

    updated = set_project_milestone_done("mastisk", 1, done=True)

    assert updated is not None
    file_text = path.read_text(encoding="utf-8")
    assert "- [ ]\n- [x] Alpha" in file_text


def test_checklist_template_application_creates_mirrored_tasks_with_uids(client, db, vault_tmp):
    template = vault_tmp / "templates" / "checklists" / "launch.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "# Launch\n\n"
        "- [ ] Confirm DNS 🆔 copied1\n"
        "- [x] Ignored completed item\n"
        "- [ ] Configure hosting #ops\n",
        encoding="utf-8",
    )

    listed = client.get("/api/templates/checklists")
    assert listed.status_code == 200
    assert listed.json() == [{"name": "launch", "task_count": 2}]

    created = client.post(
        "/api/projects",
        json={"name": "Launch Site", "template": "launch"},
    )
    assert created.status_code == 201, created.text
    file_text = (vault_tmp / "projects" / "launch-site.md").read_text(encoding="utf-8")
    task_lines = [line for line in file_text.splitlines() if line.startswith("- [ ]")]
    assert len(task_lines) == 2
    assert all("🆔" in line for line in task_lines)
    assert "copied1" not in file_text

    rows = db.execute(
        """SELECT text, project, tags_json FROM tasks
           WHERE project = 'launch-site' AND deleted_at IS NULL
           ORDER BY line_number"""
    ).fetchall()
    assert [(row["text"], row["project"], row["tags_json"]) for row in rows] == [
        ("Confirm DNS", "launch-site", "[]"),
        ("Configure hosting", "launch-site", '["ops"]'),
    ]


def test_time_entry_scan_route_and_totals(client, db, vault_tmp):
    from mastisk.projects.sync import scan_projects

    today = date.today()
    old_day = today - timedelta(days=45)
    path = vault_tmp / "projects" / "client.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: Client\nstatus: active\n---\n\n"
        "## Activity\n"
        f"- {today.isoformat()} 1.5h fixed deploy\n"
        f"- {old_day.isoformat()} 2.25h old work\n"
        "- not parseable\n"
        "- 2026-99-99 3h bad date\n",
        encoding="utf-8",
    )
    scan_projects([path])

    detail = client.get("/api/projects/client")
    assert detail.status_code == 200, detail.text
    assert detail.json()["time_totals"]["total_hours"] == 3.75
    assert detail.json()["time_totals"]["last_30_days_hours"] == 1.5

    added = client.post(
        "/api/projects/client/time",
        json={"date": today.isoformat(), "hours": 0.5, "text": "reviewed notes"},
    )
    assert added.status_code == 201, added.text
    assert added.json()["time_totals"]["total_hours"] == 4.25
    file_text = path.read_text(encoding="utf-8")
    assert f"- {today.isoformat()} 0.5h reviewed notes" in file_text
    assert db.execute("SELECT COUNT(*) AS n FROM time_entries").fetchone()["n"] == 3


def test_project_detail_returns_404_when_backing_file_is_deleted(client, db, vault_tmp):
    project = client.post("/api/projects", json={"name": "Missing File"}).json()
    (vault_tmp / project["path"]).unlink()

    response = client.get(f"/api/projects/{project['slug']}")

    assert response.status_code == 404
    row = db.execute(
        "SELECT deleted_at FROM projects WHERE slug = ?",
        (project["slug"],),
    ).fetchone()
    assert row["deleted_at"] is not None


def test_retainer_rollover_idempotent_carries_forward_and_skips_inactive(
    db, vault_tmp, data_tmp
):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "UTC"\n', encoding="utf-8")
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.agents.retainer_rollover import retainer_rollover
    from mastisk.projects.sync import scan_projects
    from mastisk.tasks.sync import scan_task_hosts

    active = vault_tmp / "projects" / "client.md"
    paused = vault_tmp / "projects" / "paused-client.md"
    active.parent.mkdir(parents=True)
    active.write_text(
        "---\n"
        "name: Client\n"
        "type: retainer\n"
        "status: active\n"
        "recurring_items:\n"
        "  - Monthly report\n"
        "  - Client call\n"
        "---\n\n"
        "## Tasks\n"
        "- [ ] Old followup 📅 2026-06-20 🆔 old1\n"
        "- [x] Done old item 📅 2026-06-20 🆔 done1\n",
        encoding="utf-8",
    )
    paused.write_text(
        "---\n"
        "name: Paused Client\n"
        "type: retainer\n"
        "status: paused\n"
        "recurring_items:\n"
        "  - Should not appear\n"
        "---\n\n"
        "## Tasks\n",
        encoding="utf-8",
    )
    scan_projects([active, paused])
    scan_task_hosts([active, paused])

    now = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)
    assert retainer_rollover(now=now, uid_factory=iter(["new1", "new2"]).__next__) == 1
    assert retainer_rollover(now=now, uid_factory=iter(["dupe1", "dupe2"]).__next__) == 0

    file_text = active.read_text(encoding="utf-8")
    assert file_text.count("### 2026-07") == 1
    assert "- [ ] Monthly report 📅 2026-07-31 🆔 new1" in file_text
    assert "- [ ] Client call 📅 2026-07-31 🆔 new2" in file_text
    assert "- [ ] Old followup 📅 2026-07-31 🆔 old1" in file_text
    assert "- [x] Done old item 📅 2026-06-20 🆔 done1" in file_text
    assert "2026-07" in file_text
    paused_text = paused.read_text(encoding="utf-8")
    assert "Should not appear" in paused_text
    assert "### 2026-07" not in paused_text

    rows = db.execute(
        """SELECT uid, due FROM tasks
           WHERE project = 'client' AND status = 'open' AND deleted_at IS NULL
           ORDER BY uid"""
    ).fetchall()
    assert [dict(row) for row in rows] == [
        {"uid": "new1", "due": "2026-07-31"},
        {"uid": "new2", "due": "2026-07-31"},
        {"uid": "old1", "due": "2026-07-31"},
    ]


def test_retainer_current_month_state_includes_month_end_due_times(db, vault_tmp):
    from mastisk.projects.sync import project_payload, scan_projects
    from mastisk.tasks.sync import scan_task_hosts

    today = date.today()
    last_day = calendar.monthrange(today.year, today.month)[1]
    month_end = today.replace(day=last_day).isoformat()
    path = vault_tmp / "projects" / "client.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\nname: Client\ntype: retainer\nstatus: active\n---\n\n"
        "## Tasks\n"
        f"- [ ] Month end call 📅 {month_end} ⏰ 10:00 🆔 timed1\n",
        encoding="utf-8",
    )
    scan_projects([path])
    scan_task_hosts([path])

    payload = project_payload("client")

    assert payload is not None
    assert payload["retainer"]["total"] == 1
    assert payload["retainer"]["tasks"][0]["uid"] == "timed1"


def test_retainer_creation_via_api(client, vault_tmp):
    created = client.post(
        "/api/projects",
        json={
            "name": "Client Retainer",
            "type": "retainer",
            "recurring_items": ["Monthly report"],
        },
    )

    assert created.status_code == 201, created.text
    body = created.json()
    assert body["type"] == "retainer"
    detail = client.get(f"/api/projects/{body['slug']}").json()
    assert detail["frontmatter"]["recurring_items"] == ["Monthly report"]
    assert detail["retainer"]["current_month"] is not None
    assert "type: retainer" in (
        vault_tmp / "projects" / "client-retainer.md"
    ).read_text(encoding="utf-8")
