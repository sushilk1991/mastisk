"""Blog writer API tests. See src/mastisk/routes/blog_route.py + spec §11."""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings
    reload_settings()
    from mastisk.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c


def _seed_note(db, body: str = "the note body", classified: bool = True):
    """Insert a minimal classified note so the candidate pool isn't empty."""
    classification_col = "classification"
    now = datetime.now().astimezone().isoformat()
    if classified:
        db.execute(
            f"""INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                                   classified_at, {classification_col}, summary, tags_json)
                VALUES ('s', '_notes/inbox/s.md', ?, 'h', 'pwa', ?,
                        ?, 'idea', 'a short summary', '[]')""",
            (body, now, now),
        )
    else:
        db.execute(
            """INSERT INTO notes (slug, path, body, body_sha256, source, created_at)
               VALUES ('s2', '_notes/inbox/s2.md', ?, 'h', 'pwa', ?)""",
            (body, now),
        )
    return db.execute("SELECT id FROM notes ORDER BY id DESC LIMIT 1").fetchone()["id"]


# ─────────────────────────────── create ───────────────────────────────


def test_post_blog_rejects_invalid_window_days(client, db):
    _seed_note(db)
    r = client.post("/api/blog-posts", json={"theme": "t", "window_days": 21})
    assert r.status_code == 422


def test_post_blog_rejects_empty_pool(client, db):
    # No notes/articles/roundtables in window → 422.
    r = client.post("/api/blog-posts", json={"theme": "", "window_days": 14})
    assert r.status_code == 422
    assert "no sources" in r.json()["detail"].lower()


def test_post_blog_returns_202_with_row_and_job(client, db):
    _seed_note(db)
    r = client.post("/api/blog-posts", json={"theme": "t", "window_days": 14})
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["status"] == "pending"
    row = db.execute(
        "SELECT * FROM blog_posts WHERE id=?", (body["id"],),
    ).fetchone()
    assert row["theme"] == "t"
    assert row["window_days"] == 14
    assert row["status"] == "pending"
    job = db.execute(
        "SELECT * FROM jobs WHERE agent='blog_writer' AND kind='draft'",
    ).fetchone()
    assert job is not None
    import json as _json
    assert _json.loads(job["payload_json"])["blog_post_id"] == body["id"]


def test_post_blog_rejects_theme_too_long(client, db):
    _seed_note(db)
    r = client.post(
        "/api/blog-posts", json={"theme": "x" * 501, "window_days": 14},
    )
    assert r.status_code == 422


# ─────────────────────────────── list / detail ───────────────────────────────


def test_list_blog_posts_returns_recent_first(client, db):
    from mastisk.db.queries import create_blog_post
    ids = [create_blog_post(db, theme=f"t{i}", window_days=14) for i in range(3)]
    r = client.get("/api/blog-posts")
    assert r.status_code == 200
    returned = [x["id"] for x in r.json()]
    assert returned == list(reversed(ids))


def test_list_blog_posts_limit_validates(client):
    r = client.get("/api/blog-posts?limit=999")
    assert r.status_code == 422


def test_get_blog_post_404_on_unknown(client):
    r = client.get("/api/blog-posts/99999")
    assert r.status_code == 404


def test_get_blog_post_404_on_deleted(client, db):
    from mastisk.db.queries import create_blog_post, soft_delete_blog_post
    bp_id = create_blog_post(db, theme="t", window_days=14)
    soft_delete_blog_post(db, bp_id)
    r = client.get(f"/api/blog-posts/{bp_id}")
    assert r.status_code == 404


def test_get_blog_post_detail_resolves_sources(client, db, vault_tmp):
    """A done row with a vault file + sources list should come back fully
    resolved, with each source's `deleted` flag correctly populated."""
    from mastisk.db.queries import (
        create_blog_post,
        insert_blog_post_source,
        update_blog_post_done,
    )
    # Seed: one live note + one deleted note + one live article.
    now = datetime.now().astimezone().isoformat()
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              classified_at, classification, summary)
           VALUES ('live', '_notes/live.md', 'b', 'h', 'pwa', ?, ?, 'idea',
                   'live note summary')""",
        (now, now),
    )
    live_note_id = db.execute("SELECT id FROM notes WHERE slug='live'").fetchone()["id"]
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              classified_at, classification, summary, deleted_at)
           VALUES ('gone', '_notes/gone.md', 'b', 'h', 'pwa', ?, ?, 'idea',
                   's', CURRENT_TIMESTAMP)""",
        (now, now),
    )
    gone_note_id = db.execute("SELECT id FROM notes WHERE slug='gone'").fetchone()["id"]
    db.execute(
        "INSERT INTO articles (id, kind, title, slug, body_md, summary) VALUES ('a1','Concept','Art One','a-one','body','sum')"
    )

    bp_id = create_blog_post(db, theme="t", window_days=14)

    # Write a vault file with frontmatter + body.
    blog_drafts = vault_tmp / "blog" / "drafts"
    blog_drafts.mkdir(parents=True, exist_ok=True)
    slug = "2026-04-22-the-draft"
    rel = f"blog/drafts/{slug}.md"
    (vault_tmp / rel).write_text(
        "---\ntitle: \"The draft\"\n---\n\nThe actual body here [source 1].\n"
    )
    update_blog_post_done(
        db, bp_id=bp_id, slug=slug, path=rel, title="The draft",
        tags_json='["a"]', model="claude", word_count=4,
        body_preview="preview",
    )
    insert_blog_post_source(
        db, blog_post_id=bp_id, kind="note", ref=str(live_note_id),
        rank=1, used=True,
    )
    insert_blog_post_source(
        db, blog_post_id=bp_id, kind="note", ref=str(gone_note_id),
        rank=2, used=False,
    )
    insert_blog_post_source(
        db, blog_post_id=bp_id, kind="article", ref="a1",
        rank=3, used=True,
    )

    r = client.get(f"/api/blog-posts/{bp_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["title"] == "The draft"
    assert body["status"] == "done"
    assert body["tags"] == ["a"]
    assert "The actual body here" in body["body_md"]
    assert body["error"] is None

    sources = body["sources"]
    assert [s["n"] for s in sources] == [1, 2, 3]
    assert sources[0]["used"] is True
    assert sources[0]["resolved"]["deleted"] is False
    assert sources[0]["resolved"]["title"]  # non-empty
    assert sources[1]["resolved"]["deleted"] is True
    assert "deleted" in sources[1]["resolved"]["title"].lower()
    assert sources[2]["resolved"]["deleted"] is False
    assert sources[2]["resolved"]["title"] == "Art One"


def test_get_blog_post_detail_missing_file(client, db, vault_tmp):
    """Row says path=X but file is gone → 200 with body_md=null and
    error='file missing'. List-view body_preview stays independent."""
    from mastisk.db.queries import create_blog_post, update_blog_post_done

    bp_id = create_blog_post(db, theme="t", window_days=14)
    update_blog_post_done(
        db, bp_id=bp_id, slug="s", path="blog/drafts/missing.md",
        title="T", tags_json="[]", model="claude", word_count=10,
        body_preview="the preview",
    )
    r = client.get(f"/api/blog-posts/{bp_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["body_md"] is None
    assert body["error"] == "file missing"
    assert body["body_preview"] == "the preview"


# ─────────────────────────────── regenerate ───────────────────────────────


def test_regenerate_409_when_in_flight_job(client, db):
    """A stale blog_writer job (queued) for same bp_id → 409 even if row is done."""
    from mastisk.agents.base import enqueue
    from mastisk.db.queries import create_blog_post, update_blog_post_done
    bp_id = create_blog_post(db, theme="t", window_days=14)
    update_blog_post_done(
        db, bp_id=bp_id, slug="s", path="p", title="T", tags_json="[]",
        model="claude", word_count=10, body_preview="pre",
    )
    enqueue("blog_writer", "draft", {"blog_post_id": bp_id})
    r = client.post(f"/api/blog-posts/{bp_id}/regenerate")
    assert r.status_code == 409


def test_regenerate_409_when_content_spawned_job_in_flight(client, db):
    """Content-spawned job payloads carry extra seed fields but still guard by bp_id."""
    from mastisk.agents.base import enqueue
    from mastisk.db.queries import create_blog_post, update_blog_post_done

    bp_id = create_blog_post(db, theme="t", window_days=14, content_slug="content-seed")
    update_blog_post_done(
        db, bp_id=bp_id, slug="s", path="p", title="T", tags_json="[]",
        model="claude", word_count=10, body_preview="pre",
    )
    enqueue(
        "blog_writer",
        "draft",
        {
            "blog_post_id": bp_id,
            "content_slug": "content-seed",
            "content_source": {"slug": "content-seed", "title": "Seed", "body": "Outline"},
        },
    )

    r = client.post(f"/api/blog-posts/{bp_id}/regenerate")

    assert r.status_code == 409


def test_regenerate_409_when_pending(client, db):
    from mastisk.db.queries import create_blog_post

    bp_id = create_blog_post(db, theme="t", window_days=14)  # status=pending
    r = client.post(f"/api/blog-posts/{bp_id}/regenerate")
    assert r.status_code == 409


def test_regenerate_resets_row_and_clears_sources(client, db):
    """After a successful regenerate: status=pending, sources cleared, new
    jobs row enqueued. Theme + window_days preserved."""
    from mastisk.db.queries import (
        create_blog_post,
        insert_blog_post_source,
        list_blog_post_sources,
        update_blog_post_done,
    )
    bp_id = create_blog_post(db, theme="keep me", window_days=30)
    update_blog_post_done(
        db, bp_id=bp_id, slug="s", path="p", title="T", tags_json="[]",
        model="claude", word_count=10, body_preview="pre",
    )
    insert_blog_post_source(
        db, blog_post_id=bp_id, kind="note", ref="1", rank=1, used=True,
    )
    r = client.post(f"/api/blog-posts/{bp_id}/regenerate")
    assert r.status_code == 202
    row = db.execute("SELECT * FROM blog_posts WHERE id=?", (bp_id,)).fetchone()
    assert row["status"] == "pending"
    assert row["title"] is None
    # path/slug are preserved — the BlogWriter agent unlinks the prior draft
    # file on the regenerate run before writing a fresh one.
    assert row["path"] == "p"
    assert row["slug"] == "s"
    assert row["word_count"] is None
    assert row["theme"] == "keep me"
    assert row["window_days"] == 30
    assert list_blog_post_sources(db, bp_id) == []
    jobs = db.execute(
        "SELECT payload_json FROM jobs WHERE agent='blog_writer'",
    ).fetchall()
    import json as _json
    assert any(
        _json.loads(j["payload_json"])["blog_post_id"] == bp_id for j in jobs
    )


def test_regenerate_content_spawned_post_reenqueues_seed(client, db, vault_tmp):
    """Regenerating a content-spawned post must not depend on recent sources."""
    from mastisk.content.sync import create_content_file
    from mastisk.db.queries import update_blog_post_done

    item = create_content_file(
        title="Local-first personal OS",
        kind="article",
        outline="## Outline\n\n- Files are the API.",
    )

    drafted = client.post(f"/api/content/{item['slug']}/draft")
    assert drafted.status_code == 202, drafted.text
    bp_id = drafted.json()["blog_post_id"]

    row = db.execute("SELECT content_slug FROM blog_posts WHERE id=?", (bp_id,)).fetchone()
    assert row["content_slug"] == item["slug"]

    update_blog_post_done(
        db, bp_id=bp_id, slug="s", path="p", title="T", tags_json="[]",
        model="claude", word_count=10, body_preview="pre",
    )
    db.execute(
        """UPDATE jobs
           SET status='done', finished_at=CURRENT_TIMESTAMP
           WHERE agent='blog_writer'
             AND kind='draft'
             AND json_extract(payload_json, '$.blog_post_id') = ?""",
        (bp_id,),
    )

    regenerated = client.post(f"/api/blog-posts/{bp_id}/regenerate")
    assert regenerated.status_code == 202, regenerated.text

    jobs = db.execute(
        """SELECT payload_json FROM jobs
           WHERE agent='blog_writer' AND kind='draft'
           ORDER BY id DESC"""
    ).fetchall()
    import json as _json

    payload = _json.loads(jobs[0]["payload_json"])
    assert payload["blog_post_id"] == bp_id
    assert payload["content_slug"] == item["slug"]
    assert payload["content_source"]["slug"] == item["slug"]
    assert "Files are the API" in payload["content_source"]["body"]


def test_regenerate_missing_content_seed_leaves_completed_post_unchanged(
    client, db, vault_tmp
):
    from mastisk.content.sync import archive_content, create_content_file
    from mastisk.db.queries import insert_blog_post_source, update_blog_post_done

    item = create_content_file(
        title="Local-first personal OS",
        kind="article",
        outline="## Outline\n\n- Files are the API.",
    )
    drafted = client.post(f"/api/content/{item['slug']}/draft")
    assert drafted.status_code == 202, drafted.text
    bp_id = drafted.json()["blog_post_id"]
    db.execute(
        """UPDATE jobs
           SET status='done', finished_at=CURRENT_TIMESTAMP
           WHERE agent='blog_writer'
             AND kind='draft'
             AND json_extract(payload_json, '$.blog_post_id') = ?""",
        (bp_id,),
    )
    update_blog_post_done(
        db, bp_id=bp_id, slug="s", path="p", title="Finished", tags_json="[]",
        model="claude", word_count=10, body_preview="pre",
    )
    insert_blog_post_source(
        db, blog_post_id=bp_id, kind="content", ref=item["slug"], rank=1, used=True,
    )
    before_row = dict(db.execute("SELECT * FROM blog_posts WHERE id=?", (bp_id,)).fetchone())
    before_sources = [
        dict(row)
        for row in db.execute(
            "SELECT kind, ref, rank, used FROM blog_post_sources WHERE blog_post_id=?",
            (bp_id,),
        ).fetchall()
    ]
    assert archive_content(item["slug"]) is not None

    regenerated = client.post(f"/api/blog-posts/{bp_id}/regenerate")

    assert regenerated.status_code == 409, regenerated.text
    after_row = dict(db.execute("SELECT * FROM blog_posts WHERE id=?", (bp_id,)).fetchone())
    after_sources = [
        dict(row)
        for row in db.execute(
            "SELECT kind, ref, rank, used FROM blog_post_sources WHERE blog_post_id=?",
            (bp_id,),
        ).fetchall()
    ]
    assert after_row == before_row
    assert after_sources == before_sources
    queued = db.execute(
        """SELECT COUNT(*) AS n FROM jobs
           WHERE agent='blog_writer'
             AND kind='draft'
             AND status IN ('queued', 'running')
             AND json_extract(payload_json, '$.blog_post_id') = ?""",
        (bp_id,),
    ).fetchone()
    assert queued["n"] == 0


# ─────────────────────────────── save-as-note ───────────────────────────────


def test_save_as_note_creates_note_and_idempotent(client, db, vault_tmp):
    from mastisk.db.queries import create_blog_post, update_blog_post_done
    bp_id = create_blog_post(db, theme="t", window_days=14)
    update_blog_post_done(
        db, bp_id=bp_id, slug="s", path="blog/drafts/s.md",
        title="The Draft", tags_json="[]", model="claude", word_count=10,
        body_preview="hello world preview",
    )
    r = client.post(f"/api/blog-posts/{bp_id}/save-as-note", json={})
    assert r.status_code == 201
    body = r.json()
    assert body["reused"] is False
    note_id = body["note_id"]
    row = db.execute(
        "SELECT saved_as_note_id FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["saved_as_note_id"] == note_id
    note = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert note is not None
    assert f"From blog draft #{bp_id}" in note["body"]

    # Idempotent second call.
    r2 = client.post(f"/api/blog-posts/{bp_id}/save-as-note", json={})
    assert r2.status_code == 201
    assert r2.json()["note_id"] == note_id
    assert r2.json()["reused"] is True


def test_save_as_note_409_when_not_done(client, db):
    from mastisk.db.queries import create_blog_post
    bp_id = create_blog_post(db, theme="t", window_days=14)  # pending
    r = client.post(f"/api/blog-posts/{bp_id}/save-as-note", json={})
    assert r.status_code == 409


# ─────────────────────────────── delete ───────────────────────────────


def test_delete_returns_204_and_sets_tombstone(client, db, vault_tmp):
    from mastisk.db.queries import create_blog_post, update_blog_post_done
    # Seed a real file so we also confirm the unlink.
    blog_drafts = vault_tmp / "blog" / "drafts"
    blog_drafts.mkdir(parents=True, exist_ok=True)
    f = blog_drafts / "x.md"
    f.write_text("---\ntitle: x\n---\n\nbody\n")
    bp_id = create_blog_post(db, theme="t", window_days=14)
    update_blog_post_done(
        db, bp_id=bp_id, slug="x", path="blog/drafts/x.md",
        title="X", tags_json="[]", model="claude", word_count=1,
        body_preview="p",
    )
    r = client.delete(f"/api/blog-posts/{bp_id}")
    assert r.status_code == 204
    row = db.execute("SELECT deleted_at FROM blog_posts WHERE id=?", (bp_id,)).fetchone()
    assert row["deleted_at"] is not None
    assert not f.exists()


def test_delete_idempotent_second_call_404(client, db):
    from mastisk.db.queries import create_blog_post
    bp_id = create_blog_post(db, theme="t", window_days=14)
    r1 = client.delete(f"/api/blog-posts/{bp_id}")
    assert r1.status_code == 204
    r2 = client.delete(f"/api/blog-posts/{bp_id}")
    assert r2.status_code == 404
