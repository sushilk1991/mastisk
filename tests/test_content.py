from __future__ import annotations

import json

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def test_scan_content_round_trips_file_and_content_tasks(db, vault_tmp):
    from mastisk.content.sync import scan_content
    from mastisk.tasks.sync import scan_tasks

    path = vault_tmp / "content" / "local-first-video.md"
    path.parent.mkdir(parents=True)
    path.write_text(
        "---\n"
        "title: Local-first personal OS\n"
        "kind: video\n"
        "status: outline\n"
        "domain: mastisk\n"
        "channel: YouTube\n"
        "url: https://example.com/video\n"
        "publish_date: 2026-07-01\n"
        "---\n\n"
        "## Outline\n\n"
        "- Hook: the file is the product.\n\n"
        "## Tasks\n\n"
        "- [ ] Record the first cut #video 🆔 contenttask1\n",
        encoding="utf-8",
    )

    assert scan_content() == {"upserted": 1}
    scan_tasks()

    row = db.execute(
        "SELECT * FROM content_items WHERE slug = 'local-first-video'"
    ).fetchone()
    assert row["title"] == "Local-first personal OS"
    assert row["kind"] == "video"
    assert row["status"] == "outline"
    assert row["domain"] == "mastisk"
    assert row["channel"] == "YouTube"
    assert row["url"] == "https://example.com/video"
    assert row["publish_date"] == "2026-07-01"
    assert row["path"] == "content/local-first-video.md"

    task = db.execute(
        "SELECT host_path, text, domain, project FROM tasks WHERE uid = 'contenttask1'"
    ).fetchone()
    assert dict(task) == {
        "host_path": "content/local-first-video.md",
        "text": "Record the first cut",
        "domain": "mastisk",
        "project": None,
    }


def test_content_routes_create_filter_detail_patch_and_spawn_blog_draft(
    db,
    vault_tmp,
    data_tmp,
):
    template = vault_tmp / "templates" / "checklists" / "publish.md"
    template.parent.mkdir(parents=True)
    template.write_text(
        "- [ ] Draft outline #content\n"
        "- [ ] Schedule publish date #content\n",
        encoding="utf-8",
    )

    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/content",
            json={
                "title": "Local-first personal OS",
                "kind": "article",
                "status": "idea",
                "domain": "mastisk",
                "channel": "Blog",
                "outline": "## Outline\n\n- The markdown file is canonical.",
                "checklist_template": "publish",
            },
        )
        assert created.status_code == 201, created.text
        slug = created.json()["slug"]

        listed = client.get("/api/content?kind=article&status=idea&domain=mastisk")
        assert listed.status_code == 200, listed.text
        assert [item["slug"] for item in listed.json()["items"]] == [slug]
        assert listed.json()["kanban"]["idea"][0]["slug"] == slug

        detail = client.get(f"/api/content/{slug}")
        assert detail.status_code == 200, detail.text
        body = detail.json()
        assert body["body"].startswith("## Outline")
        assert "The markdown file is canonical" in body["body"]
        assert [task["text"] for task in body["tasks"]] == [
            "Draft outline",
            "Schedule publish date",
        ]

        patched = client.patch(
            f"/api/content/{slug}",
            json={
                "status": "editing",
                "url": "https://example.com/local-first",
                "publish_date": "2026-07-01",
                "channel": "Personal site",
            },
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == "editing"
        assert patched.json()["url"] == "https://example.com/local-first"

        drafted = client.post(f"/api/content/{slug}/draft")
        assert drafted.status_code == 202, drafted.text
        bp_id = drafted.json()["blog_post_id"]

        retried = client.post(f"/api/content/{slug}/draft")
        assert retried.status_code == 202, retried.text
        assert retried.json()["blog_post_id"] == bp_id
        assert retried.json()["reused"] is True

    blog = db.execute("SELECT * FROM blog_posts WHERE id = ?", (bp_id,)).fetchone()
    assert blog["status"] == "pending"
    assert blog["theme"].startswith("Local-first personal OS")
    assert "The markdown file is canonical" in blog["theme"]
    assert db.execute("SELECT COUNT(*) AS n FROM blog_posts").fetchone()["n"] == 1
    job = db.execute(
        "SELECT payload_json FROM jobs WHERE agent='blog_writer' AND kind='draft'"
    ).fetchone()
    payload = json.loads(job["payload_json"])
    assert payload["blog_post_id"] == bp_id
    assert payload["content_slug"] == slug
    assert payload["content_source"]["slug"] == slug
    assert "The markdown file is canonical" in payload["content_source"]["body"]


def test_content_draft_rejects_video_and_podcast(db, vault_tmp, data_tmp):
    from mastisk.content.sync import create_content_file

    item = create_content_file(
        title="Interview with Mira",
        kind="podcast",
        outline="## Outline\n\n- Ask about local-first writing.",
    )

    with _client(vault_tmp, data_tmp, db) as client:
        drafted = client.post(f"/api/content/{item['slug']}/draft")

    assert drafted.status_code == 422, drafted.text
    assert "article or newsletter" in drafted.json()["detail"]


def test_content_triage_accept_clears_file_marker(db, vault_tmp, data_tmp):
    from mastisk.content.sync import create_content_file

    item = create_content_file(
        title="Local-first personal OS",
        kind="article",
        outline="## Outline\n\n- Files are the API.",
        needs_triage=True,
    )

    with _client(vault_tmp, data_tmp, db) as client:
        listed = client.get("/api/triage")
        assert listed.status_code == 200, listed.text
        assert [row["id"] for row in listed.json()] == [f"content:{item['slug']}"]

        accepted = client.post(
            f"/api/triage/content:{item['slug']}/reclassify",
            json={"type": "content"},
        )

    assert accepted.status_code == 200, accepted.text
    file_text = (vault_tmp / item["path"]).read_text(encoding="utf-8")
    assert "needs_triage:" not in file_text


def test_content_triage_reclassifies_to_note_preserving_outline(
    db,
    vault_tmp,
    data_tmp,
):
    from mastisk.content.sync import create_content_file

    item = create_content_file(
        title="Local-first personal OS",
        kind="article",
        outline="## Outline\n\n- Files are the API.",
        needs_triage=True,
    )

    with _client(vault_tmp, data_tmp, db) as client:
        r = client.post(
            f"/api/triage/content:{item['slug']}/reclassify",
            json={"type": "note"},
        )

    assert r.status_code == 200, r.text
    note = db.execute(
        "SELECT body FROM notes WHERE deleted_at IS NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert note is not None
    assert "Local-first personal OS" in note["body"]
    assert "Files are the API" in note["body"]
