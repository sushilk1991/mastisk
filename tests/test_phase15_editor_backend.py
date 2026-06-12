from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.app import create_app

    return TestClient(create_app())


def test_default_cors_blocks_drive_by_browser_origins_but_same_origin_still_works(
    vault_tmp, data_tmp, db
):
    with _client(vault_tmp, data_tmp, db) as client:
        same_origin = client.get("/api/health")
        cross_origin = client.get(
            "/api/health",
            headers={"Origin": "https://evil.example"},
        )

    assert same_origin.status_code == 200
    assert cross_origin.status_code == 200
    assert "access-control-allow-origin" not in cross_origin.headers


def test_attachment_upload_dedupes_and_returns_markdown(vault_tmp, data_tmp, db):
    with _client(vault_tmp, data_tmp, db) as client:
        first = client.post(
            "/api/attachments",
            files={"file": ("photo.png", b"same-bytes", "image/png")},
        )
        second = client.post(
            "/api/attachments",
            files={"file": ("renamed.png", b"same-bytes", "image/png")},
        )
        fetched = client.get(f"/api/attachments/{first.json()['path'].rsplit('/', 1)[-1]}")

    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert fetched.status_code == 200, fetched.text
    assert fetched.content == b"same-bytes"
    assert first.json()["path"] == second.json()["path"]
    assert first.json()["path"].startswith("attachments/")
    assert first.json()["path"].endswith(".png")
    assert first.json()["markdown"] == f"![photo]({first.json()['path']})"
    assert len(list((vault_tmp / "attachments").glob("*.png"))) == 1


def test_attachment_upload_enforces_type_size_and_safe_serving(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    (data_tmp / "config.toml").write_text("[attachments]\nmax_mb = 0\n", encoding="utf-8")
    reload_settings()
    try:
        with _client(vault_tmp, data_tmp, db) as client:
            oversized = client.post(
                "/api/attachments",
                files={"file": ("photo.png", b"x", "image/png")},
            )
            bad_type = client.post(
                "/api/attachments",
                files={"file": ("note.txt", b"hello", "text/plain")},
            )
            traversal = client.get("/api/attachments/../secret.png")
    finally:
        (data_tmp / "config.toml").unlink(missing_ok=True)
        reload_settings()

    assert oversized.status_code == 413, oversized.text
    assert bad_type.status_code == 415, bad_type.text
    assert traversal.status_code in {404, 422}


def test_editing_locks_are_owned_by_session_token_and_refcounted(
    vault_tmp, data_tmp, db
):
    from mastisk.editing import is_user_editing

    path = "journal/2026-06-11.md"
    with _client(vault_tmp, data_tmp, db) as client:
        first_lock = client.post("/api/editing/lock", json={"path": path})
        second_lock = client.post("/api/editing/lock", json={"path": path})
        assert first_lock.status_code == 200, first_lock.text
        assert second_lock.status_code == 200, second_lock.text
        first_token = first_lock.json()["token"]
        second_token = second_lock.json()["token"]
        assert first_token != second_token

        heartbeat = client.put(
            "/api/editing/heartbeat",
            json={"path": path, "token": second_token},
        )
        missing_token = client.put("/api/editing/heartbeat", json={"path": path})
        assert heartbeat.status_code == 200, heartbeat.text
        assert missing_token.status_code == 422, missing_token.text
        assert is_user_editing(path)

        first_unlock = client.post(
            "/api/editing/unlock",
            json={"path": path, "token": first_token},
        )
        assert first_unlock.status_code == 200, first_unlock.text
        assert is_user_editing(path)

        second_unlock = client.post(
            "/api/editing/unlock",
            json={"path": path, "token": second_token},
        )

    assert second_unlock.status_code == 200, second_unlock.text
    assert not is_user_editing(path)

    with _client(vault_tmp, data_tmp, db) as client:
        locked = client.post("/api/editing/lock", json={"path": path})
        token = locked.json()["token"]
    stale = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    db.execute(
        "UPDATE editing_locks SET heartbeat_at = ? WHERE path = ? AND token = ?",
        (stale, path, token),
    )

    assert not is_user_editing(path)


def test_locked_inbox_note_is_not_enqueued_for_notetaker(db, vault_tmp, data_tmp):
    from mastisk.agents.notetaker import Notetaker
    from mastisk.editing import lock_path

    inbox_note = vault_tmp / "_notes" / "inbox" / "editing.md"
    inbox_note.parent.mkdir(parents=True)
    inbox_note.write_text("draft while editor is open", encoding="utf-8")
    lock_path("_notes/inbox/editing.md")

    asyncio.run(Notetaker()._enqueue_if_needed(inbox_note, inbox_note.stat()))

    assert db.execute("SELECT COUNT(*) AS n FROM notes").fetchone()["n"] == 0
    assert db.execute("SELECT COUNT(*) AS n FROM jobs WHERE agent = 'notetaker'").fetchone()["n"] == 0


def test_locked_task_scan_does_not_stamp_uid_until_lock_expires(db, vault_tmp, data_tmp):
    from mastisk.editing import lock_path
    from mastisk.tasks.sync import scan_task_hosts

    host = vault_tmp / "journal" / "2026-06-11.md"
    host.parent.mkdir(parents=True)
    host.write_text("## Tasks\n- [ ] edit me\n", encoding="utf-8")
    lock_path("journal/2026-06-11.md")

    locked_result = scan_task_hosts([host], uid_factory=lambda: "abc123")

    assert locked_result == {"upserted": 0, "assigned": 0}
    assert "🆔" not in host.read_text(encoding="utf-8")

    stale = (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    db.execute(
        "UPDATE editing_locks SET heartbeat_at = ? WHERE path = 'journal/2026-06-11.md'",
        (stale,),
    )

    unlocked_result = scan_task_hosts([host], uid_factory=lambda: "abc123")

    assert unlocked_result == {"upserted": 1, "assigned": 1}
    assert "🆔 abc123" in host.read_text(encoding="utf-8")


def test_vault_file_write_is_atomic_and_rescans_without_rewriting_frontmatter(
    db, vault_tmp, data_tmp
):
    path = vault_tmp / "journal" / "2026-06-11.md"
    path.parent.mkdir(parents=True)
    original = "---\nmood: 4\nenergy: 3\n---\n\n## Tasks\n\n## Log\n"
    updated = "---\nmood: 4\nenergy: 3\n---\n\n## Tasks\n\n## Log\n- 09:00 Saved in editor\n"
    path.write_text(original, encoding="utf-8")

    with _client(vault_tmp, data_tmp, db) as client:
        loaded = client.get("/api/vault/file?path=journal/2026-06-11.md").json()
        response = client.put(
            "/api/vault/file",
            json={
                "path": "journal/2026-06-11.md",
                "content": updated,
                "base_sha256": loaded["content_sha256"],
            },
        )

    assert response.status_code == 200, response.text
    assert path.read_text(encoding="utf-8") == updated
    row = db.execute(
        "SELECT mood, energy, log_count FROM journal_days WHERE date = '2026-06-11'",
    ).fetchone()
    assert dict(row) == {"mood": 4, "energy": 3, "log_count": 1}


def test_vault_file_write_rejects_stale_editor_save_after_user_append(
    db, vault_tmp, data_tmp
):
    from mastisk.journal import append_log

    path = vault_tmp / "journal" / "2026-06-11.md"
    path.parent.mkdir(parents=True)
    original = "---\nmood: 4\n---\n\n## Tasks\n\n## Log\n\n## Reflections\n"
    path.write_text(original, encoding="utf-8")

    with _client(vault_tmp, data_tmp, db) as client:
        loaded = client.get("/api/vault/file?path=journal/2026-06-11.md").json()

        append_log(
            "2026-06-11",
            "captured while editor was open",
            datetime.fromisoformat("2026-06-11T09:00:00+00:00"),
        )

        response = client.put(
            "/api/vault/file",
            json={
                "path": "journal/2026-06-11.md",
                "content": loaded["content"] + "\nlocal editor change\n",
                "base_sha256": loaded["content_sha256"],
            },
        )

    assert response.status_code == 409, response.text
    assert "captured while editor was open" in path.read_text(encoding="utf-8")
    assert "local editor change" not in path.read_text(encoding="utf-8")


def test_vault_file_write_reports_success_when_rescan_fails(
    db, vault_tmp, data_tmp, monkeypatch, caplog
):
    from mastisk.routes import vault_route

    path = vault_tmp / "journal" / "2026-06-11.md"
    path.parent.mkdir(parents=True)
    original = "---\nmood: 4\n---\n\n## Tasks\n\n## Log\n"
    updated = "---\nmood: 4\n---\n\n## Tasks\n\n## Log\n- 09:00 saved\n"
    path.write_text(original, encoding="utf-8")

    def fail_rescan(*_args, **_kwargs):
        raise RuntimeError("mirror refresh failed")

    monkeypatch.setattr(vault_route, "rescan_vault_markdown_path", fail_rescan)

    with caplog.at_level("ERROR", logger="mastisk.routes.vault_route"):
        with _client(vault_tmp, data_tmp, db) as client:
            loaded = client.get("/api/vault/file?path=journal/2026-06-11.md").json()
            response = client.put(
                "/api/vault/file",
                json={
                    "path": "journal/2026-06-11.md",
                    "content": updated,
                    "base_sha256": loaded["content_sha256"],
                },
            )

    assert response.status_code == 200, response.text
    assert response.json()["ok"] is True
    assert response.json()["rescan_failed"] is True
    assert response.json()["content_sha256"] != loaded["content_sha256"]
    assert path.read_text(encoding="utf-8") == updated
    assert "rescan failed after successful vault write" in caplog.text


def test_editor_save_rescans_tasks_even_before_unlock(db, vault_tmp, data_tmp):
    from mastisk.editing import lock_path

    journal = vault_tmp / "journal" / "2026-06-11.md"
    journal.parent.mkdir(parents=True)
    journal.write_text(
        "---\nmood: 4\n---\n\n## Tasks\n\n## Log\n\n## Reflections\n",
        encoding="utf-8",
    )
    project = vault_tmp / "projects" / "mastisk.md"
    project.parent.mkdir(parents=True)
    project.write_text(
        "---\nname: Mastisk\nstatus: active\n---\n\n## Tasks\n\n",
        encoding="utf-8",
    )
    content = vault_tmp / "content" / "local-first-video.md"
    content.parent.mkdir(parents=True)
    content.write_text(
        "---\n"
        "title: Local-first personal OS\n"
        "kind: video\n"
        "status: outline\n"
        "---\n\n"
        "## Outline\n\n"
        "## Tasks\n\n",
        encoding="utf-8",
    )

    lock_path("journal/2026-06-11.md")
    lock_path("projects/mastisk.md")
    lock_path("content/local-first-video.md")

    with _client(vault_tmp, data_tmp, db) as client:
        journal_loaded = client.get("/api/vault/file?path=journal/2026-06-11.md").json()
        project_loaded = client.get("/api/vault/file?path=projects/mastisk.md").json()
        content_loaded = client.get(
            "/api/vault/file?path=content/local-first-video.md"
        ).json()

        journal_save = client.put(
            "/api/vault/file",
            json={
                "path": "journal/2026-06-11.md",
                "content": journal_loaded["content"].replace(
                    "## Tasks\n\n",
                    "## Tasks\n- [ ] journal editor task\n\n",
                ),
                "base_sha256": journal_loaded["content_sha256"],
            },
        )
        project_save = client.put(
            "/api/vault/file",
            json={
                "path": "projects/mastisk.md",
                "content": project_loaded["content"].replace(
                    "## Tasks\n\n",
                    "## Tasks\n- [ ] project editor task\n\n",
                ),
                "base_sha256": project_loaded["content_sha256"],
            },
        )
        content_save = client.put(
            "/api/vault/file",
            json={
                "path": "content/local-first-video.md",
                "content": content_loaded["content"].replace(
                    "## Tasks\n\n",
                    "## Tasks\n- [ ] content editor task\n\n",
                ),
                "base_sha256": content_loaded["content_sha256"],
            },
        )

    assert journal_save.status_code == 200, journal_save.text
    assert project_save.status_code == 200, project_save.text
    assert content_save.status_code == 200, content_save.text
    assert "🆔" in journal.read_text(encoding="utf-8")
    assert "🆔" in project.read_text(encoding="utf-8")
    assert "🆔" in content.read_text(encoding="utf-8")
    rows = db.execute(
        "SELECT host_path, text FROM tasks ORDER BY host_path, text",
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        ("content/local-first-video.md", "content editor task"),
        ("journal/2026-06-11.md", "journal editor task"),
        ("projects/mastisk.md", "project editor task"),
    ]


def test_vault_file_write_accepts_frontmatter_close_without_trailing_newline(
    db, vault_tmp, data_tmp
):
    path = vault_tmp / "_notes" / "inbox" / "frontmatter.md"
    path.parent.mkdir(parents=True)
    original = "---\ntitle: Frontmatter\n---"
    path.write_text(original, encoding="utf-8")

    with _client(vault_tmp, data_tmp, db) as client:
        loaded = client.get("/api/vault/file?path=_notes/inbox/frontmatter.md").json()
        response = client.put(
            "/api/vault/file",
            json={
                "path": "_notes/inbox/frontmatter.md",
                "content": f"{loaded['content']}\n\nBody",
                "base_sha256": loaded["content_sha256"],
            },
        )

    assert response.status_code == 200, response.text
    assert path.read_text(encoding="utf-8").endswith("\n\nBody")
