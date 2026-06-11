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


def test_task_scan_reassigns_duplicate_uid_in_later_host(db, vault_tmp):
    from mastisk.tasks.sync import scan_task_hosts

    journal = vault_tmp / "journal" / "2026-06-11.md"
    project = vault_tmp / "projects" / "mastisk.md"
    journal.parent.mkdir(parents=True)
    project.parent.mkdir(parents=True)
    journal.write_text("## Tasks\n- [ ] journal task 🆔 dup1\n", encoding="utf-8")
    project.write_text("## Tasks\n- [ ] project task 🆔 dup1\n", encoding="utf-8")

    scan_task_hosts([journal, project], uid_factory=lambda: "fresh2")

    rows = db.execute(
        "SELECT uid, host_path, text FROM tasks WHERE deleted_at IS NULL ORDER BY host_path"
    ).fetchall()
    assert [(row["uid"], row["text"]) for row in rows] == [
        ("dup1", "journal task"),
        ("fresh2", "project task"),
    ]
    assert "🆔 dup1" in journal.read_text(encoding="utf-8")
    assert "🆔 fresh2" in project.read_text(encoding="utf-8")


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


def test_append_task_sanitizes_injected_uid_before_mirror_write(db, vault_tmp):
    from mastisk.tasks.sync import append_task_to_host, scan_task_hosts

    host = vault_tmp / "journal" / "2026-06-11.md"
    host.parent.mkdir(parents=True)
    host.write_text("## Tasks\n- [ ] original row 🆔 existing1\n", encoding="utf-8")
    scan_task_hosts([host])

    row = append_task_to_host(
        host,
        text="new task tries to steal 🆔 existing1",
        uid="fresh1",
    )

    assert row["uid"] == "fresh1"
    original = db.execute(
        "SELECT text, host_path FROM tasks WHERE uid = 'existing1'",
    ).fetchone()
    assert original["text"] == "original row"
    new_row = db.execute("SELECT text FROM tasks WHERE uid = 'fresh1'").fetchone()
    assert new_row["text"] == "new task tries to steal existing1"


def test_append_task_collapses_newlines_and_marker_glyphs_to_one_line(db, vault_tmp):
    from mastisk.tasks.parser import parse_markdown_tasks
    from mastisk.tasks.sync import append_task_to_host

    host = vault_tmp / "journal" / "2026-06-11.md"

    row = append_task_to_host(
        host,
        text="first line\n- [ ] injected 📅 2026-06-30",
        tags=["safe\n🆔 bad"],
        links=["Some Page\n⏳ bad"],
        uid="clean1",
    )

    file_text = host.read_text(encoding="utf-8")
    task_lines = [line for line in file_text.splitlines() if line.startswith("- [ ]")]
    assert task_lines == [
        "- [ ] first line - [ ] injected 2026-06-30 #safe-bad [[Some Page bad]] 🆔 clean1"
    ]
    parsed = parse_markdown_tasks(file_text)
    assert len(parsed) == 1
    assert parsed[0]["uid"] == row["uid"] == "clean1"
    assert parsed[0]["text"] == "first line - [ ] injected 2026-06-30"


def test_task_append_and_scan_acquire_host_file_lock(db, vault_tmp, monkeypatch):
    from mastisk.tasks import sync as task_sync

    host = vault_tmp / "journal" / "2026-06-11.md"
    acquisitions: list[str] = []

    class RecordingLock:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            acquisitions.append(str(self.path))
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_lock(path):
        return RecordingLock(path)

    monkeypatch.setattr(task_sync, "host_file_lock", fake_lock, raising=False)

    task_sync.append_task_to_host(host, text="locked append", uid="lock1")
    assert str(host) in acquisitions

    acquisitions.clear()
    task_sync.scan_task_hosts([host])
    assert acquisitions == [str(host)]
