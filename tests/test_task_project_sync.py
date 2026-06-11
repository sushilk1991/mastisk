from __future__ import annotations


def test_task_scan_assigns_missing_uid_file_first(db, vault_tmp):
    from mastisk.tasks.sync import scan_task_hosts

    host = vault_tmp / "journal" / "2026-06-11.md"
    host.parent.mkdir(parents=True)
    host.write_text("## Tasks\n- [ ] change filter 📅 2026-06-12\n", encoding="utf-8")

    result = scan_task_hosts([host], uid_factory=lambda: "abc123")

    assert result["upserted"] == 1
    assert "🆔 abc123" in host.read_text(encoding="utf-8")
    row = db.execute("SELECT * FROM tasks WHERE uid = 'abc123'").fetchone()
    assert row is not None
    assert row["host_path"] == "journal/2026-06-11.md"
    assert row["status"] == "open"
    assert row["due"] == "2026-06-12"


def test_task_scan_soft_deletes_tasks_removed_from_file(db, vault_tmp):
    from mastisk.tasks.sync import scan_task_hosts

    host = vault_tmp / "projects" / "mastisk.md"
    host.parent.mkdir(parents=True)
    host.write_text("## Tasks\n- [ ] ship parser 🆔 gone1\n", encoding="utf-8")
    scan_task_hosts([host])

    host.write_text("## Tasks\n", encoding="utf-8")
    scan_task_hosts([host])

    row = db.execute("SELECT deleted_at FROM tasks WHERE uid = 'gone1'").fetchone()
    assert row is not None
    assert row["deleted_at"] is not None


def test_project_scan_mirrors_frontmatter_and_soft_deletes_missing_files(db, vault_tmp):
    from mastisk.projects.sync import scan_projects

    projects = vault_tmp / "projects"
    projects.mkdir()
    path = projects / "mastisk.md"
    path.write_text(
        "---\n"
        "name: Mastisk\n"
        "type: project\n"
        "domain: work\n"
        "status: active\n"
        "due: 2026-06-30\n"
        "---\n\n"
        "## Log\n\n## Tasks\n",
        encoding="utf-8",
    )

    scan_projects()
    row = db.execute("SELECT * FROM projects WHERE slug = 'mastisk'").fetchone()
    assert row["name"] == "Mastisk"
    assert row["domain"] == "work"
    assert row["status"] == "active"

    path.unlink()
    scan_projects()
    row = db.execute("SELECT deleted_at FROM projects WHERE slug = 'mastisk'").fetchone()
    assert row["deleted_at"] is not None
