from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    with TestClient(create_app()) as c:
        yield c


def test_domains_create_list_and_explicit_config_seed(client, data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[domains]\nnames = ["Work", "Home"]\n', encoding="utf-8")

    from mastisk.settings import reload_settings
    from mastisk.routes.domains import sync_config_domains

    reload_settings()
    sync_config_domains()
    listed = client.get("/api/domains")
    assert listed.status_code == 200
    assert {d["slug"] for d in listed.json()} >= {"work", "home"}

    created = client.post("/api/domains", json={"name": "Side Quests"})
    assert created.status_code == 201, created.text
    assert created.json()["slug"] == "side-quests"
    assert any(d["slug"] == "side-quests" for d in client.get("/api/domains").json())


def test_domains_list_does_not_reload_settings(client, monkeypatch):
    from mastisk.routes import domains as domains_route

    def fail_reload():
        raise AssertionError("GET /api/domains must not reload settings")

    monkeypatch.setattr(domains_route, "reload_settings", fail_reload, raising=False)

    listed = client.get("/api/domains")

    assert listed.status_code == 200


def test_project_create_slug_collision_and_patch_status(client, vault_tmp):
    first = client.post(
        "/api/projects",
        json={"name": "Mastisk", "type": "project", "domain": "work"},
    )
    second = client.post(
        "/api/projects",
        json={"name": "Mastisk", "type": "project", "domain": "work"},
    )

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["slug"] == "mastisk"
    assert second.json()["slug"] == "mastisk-2"
    assert (vault_tmp / "projects" / "mastisk.md").exists()
    assert (vault_tmp / "projects" / "mastisk-2.md").exists()

    patched = client.patch("/api/projects/mastisk", json={"status": "paused"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["status"] == "paused"
    file_text = (vault_tmp / "projects" / "mastisk.md").read_text(encoding="utf-8")
    assert "status: paused" in file_text


def test_task_routes_create_filter_toggle_and_patch_file_first(client, vault_tmp):
    project = client.post(
        "/api/projects",
        json={"name": "Mastisk", "type": "project", "domain": "work"},
    ).json()

    created = client.post(
        "/api/tasks",
        json={
            "text": "Ship parser",
            "project": project["slug"],
            "due": "2026-06-12",
            "priority": "high",
            "tags": ["phase3"],
        },
    )
    assert created.status_code == 201, created.text
    task = created.json()
    assert task["status"] == "open"
    assert task["project"] == "mastisk"
    assert task["domain"] == "work"

    listing = client.get("/api/tasks?status=open&due_before=2026-06-13&domain=work&project=mastisk")
    assert listing.status_code == 200
    assert [t["uid"] for t in listing.json()] == [task["uid"]]

    toggled = client.patch(f"/api/tasks/{task['uid']}/toggle")
    assert toggled.status_code == 200, toggled.text
    assert toggled.json()["status"] == "done"
    host_text = (vault_tmp / "projects" / "mastisk.md").read_text(encoding="utf-8")
    assert f"- [x] Ship parser" in host_text

    patched = client.patch(
        f"/api/tasks/{task['uid']}",
        json={"due": "2026-06-20", "priority": "low"},
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["due"] == "2026-06-20"
    assert patched.json()["priority"] == "low"
    host_text = (vault_tmp / "projects" / "mastisk.md").read_text(encoding="utf-8")
    assert "📅 2026-06-20" in host_text
    assert "🔽" in host_text


def test_task_patch_staleness_override_survives_file_rescan(client, db, vault_tmp):
    created = client.post("/api/tasks", json={"text": "Custom slipping window"})
    assert created.status_code == 201, created.text
    task = created.json()

    patched = client.patch(f"/api/tasks/{task['uid']}", json={"staleness_days": 30})

    assert patched.status_code == 200, patched.text
    assert patched.json()["staleness_days"] == 30

    from mastisk.tasks.sync import scan_task_hosts

    scan_task_hosts([vault_tmp / task["host_path"]])
    row = db.execute(
        "SELECT staleness_days FROM tasks WHERE uid = ?",
        (task["uid"],),
    ).fetchone()
    assert row["staleness_days"] == 30


def test_task_patch_due_reschedules_pending_task_due_reminder(
    client, db, data_tmp, vault_tmp
):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "Asia/Kolkata"\n', encoding="utf-8")
    from mastisk.settings import reload_settings
    from mastisk.tasks.sync import append_task_to_host

    reload_settings()
    task = append_task_to_host(
        vault_tmp / "journal" / "2026-06-11.md",
        text="Call Sam",
        due="2099-01-01T09:00:00",
        uid="patch1",
        reminder_lead_minutes=15,
    )
    db.execute(
        """INSERT INTO reminders
           (entity_type, entity_id, fire_at, lead_minutes, kind, status, title, body)
           VALUES ('task', ?, '2099-01-01T08:45:00+00:00', 15,
                   'task_due', 'pending', 'Task due', 'Call Sam')""",
        (task["uid"],),
    )

    patched = client.patch(
        f"/api/tasks/{task['uid']}",
        json={"due": "2099-01-01T10:00:00"},
    )

    assert patched.status_code == 200, patched.text
    row = db.execute(
        "SELECT status, fire_at, body FROM reminders WHERE entity_id = ?",
        (task["uid"],),
    ).fetchone()
    assert dict(row) == {
        "status": "pending",
        "fire_at": "2099-01-01T04:15:00+00:00",
        "body": "Call Sam",
    }


def test_task_patch_due_removed_cancels_pending_task_due_reminder(
    client, db, data_tmp, vault_tmp
):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "Asia/Kolkata"\n', encoding="utf-8")
    from mastisk.settings import reload_settings
    from mastisk.tasks.sync import append_task_to_host

    reload_settings()
    task = append_task_to_host(
        vault_tmp / "journal" / "2026-06-11.md",
        text="Call Sam",
        due="2099-01-01T09:00:00",
        uid="patch2",
        reminder_lead_minutes=15,
    )
    db.execute(
        """INSERT INTO reminders
           (entity_type, entity_id, fire_at, lead_minutes, kind, status, title, body)
           VALUES ('task', ?, '2099-01-01T08:45:00+00:00', 15,
                   'task_due', 'pending', 'Task due', 'Call Sam')""",
        (task["uid"],),
    )

    patched = client.patch(f"/api/tasks/{task['uid']}", json={"due": None})

    assert patched.status_code == 200, patched.text
    row = db.execute(
        "SELECT status, last_error FROM reminders WHERE entity_id = ?",
        (task["uid"],),
    ).fetchone()
    assert dict(row) == {
        "status": "cancelled",
        "last_error": "task due removed",
    }


def test_task_routes_validate_due_and_emit_time_marker(client, vault_tmp):
    invalid = client.post("/api/tasks", json={"text": "Bad date", "due": "someday"})
    assert invalid.status_code == 422

    invalid_scheduled = client.post(
        "/api/tasks",
        json={"text": "Bad scheduled", "scheduled": "later-ish"},
    )
    assert invalid_scheduled.status_code == 422

    created = client.post(
        "/api/tasks",
        json={"text": "Timed task", "due": "2026-06-10T14:00:00-07:00"},
    )
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["due"] == "2026-06-10T14:00:00"
    host_text = (vault_tmp / body["host_path"]).read_text(encoding="utf-8")
    assert "📅 2026-06-10 ⏰ 14:00" in host_text


def test_task_toggle_missing_file_line_returns_404_and_refreshes_mirror(client, vault_tmp, db):
    created = client.post("/api/tasks", json={"text": "Delete me"})
    assert created.status_code == 201, created.text
    task = created.json()
    path = vault_tmp / task["host_path"]
    file_text = path.read_text(encoding="utf-8")
    path.write_text(
        "\n".join(line for line in file_text.splitlines() if task["uid"] not in line) + "\n",
        encoding="utf-8",
    )

    toggled = client.patch(f"/api/tasks/{task['uid']}/toggle")

    assert toggled.status_code == 404
    assert "task line not found" in toggled.json()["detail"]
    row = db.execute(
        "SELECT deleted_at FROM tasks WHERE uid = ?",
        (task["uid"],),
    ).fetchone()
    assert row["deleted_at"] is not None


def test_full_task_scan_includes_existing_non_default_hosts(client, vault_tmp, db):
    created = client.post(
        "/api/tasks",
        json={"text": "Keep note task", "host_path": "_notes/somefile.md"},
    )
    assert created.status_code == 201, created.text
    uid = created.json()["uid"]

    from mastisk.tasks.sync import scan_tasks

    scan_tasks()
    row = db.execute(
        "SELECT deleted_at FROM tasks WHERE uid = ?",
        (uid,),
    ).fetchone()
    assert row is not None
    assert row["deleted_at"] is None

    (vault_tmp / "_notes" / "somefile.md").unlink()
    scan_tasks()
    row = db.execute(
        "SELECT deleted_at FROM tasks WHERE uid = ?",
        (uid,),
    ).fetchone()
    assert row["deleted_at"] is not None


def test_project_list_includes_open_task_count(client):
    project = client.post(
        "/api/projects",
        json={"name": "Mastisk", "type": "project", "domain": "work"},
    ).json()
    client.post("/api/tasks", json={"text": "Open task", "project": project["slug"]})

    rows = client.get("/api/projects").json()

    mastisk = next(p for p in rows if p["slug"] == project["slug"])
    assert mastisk["open_task_count"] == 1
