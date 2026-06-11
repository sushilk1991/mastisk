"""Integration tests for /api/notes routes."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(vault_tmp, data_tmp, db):
    """Build the FastAPI app with tmp paths in effect. `db` runs first so schema is applied."""
    from mastisk.settings import reload_settings
    reload_settings()
    from mastisk.app import create_app
    app = create_app()
    with TestClient(app) as c:
        yield c


def test_post_notes_creates_file_and_row(client, vault_tmp):
    r = client.post("/api/notes", json={"text": "first thought", "source": "pwa"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["slug"].endswith("first-thought") or "first" in body["slug"]
    file_path = vault_tmp / body["path"]
    assert file_path.exists()
    assert "first thought" in file_path.read_text()


def test_persist_note_capture_keeps_both_files_on_slug_collision(db, vault_tmp):
    from mastisk.routes.notes import persist_note_capture

    ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)

    first = persist_note_capture(
        body="first body",
        source="cli",
        slug_text="same title",
        ts=ts,
    )
    second = persist_note_capture(
        body="second body",
        source="cli",
        slug_text="same title",
        ts=ts,
    )

    first_path = vault_tmp / first["path"]
    second_path = vault_tmp / second["path"]
    assert first_path.exists()
    assert second_path.exists()
    assert first_path.read_text(encoding="utf-8") == "first body"
    assert second_path.read_text(encoding="utf-8") == "second body"


def test_persist_note_capture_write_failure_preserves_existing_manual_file(
    db, vault_tmp, monkeypatch
):
    from mastisk.paths import notes_inbox_dir
    from mastisk.routes import notes
    from mastisk.routes.notes import persist_note_capture

    ts = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    existing = notes_inbox_dir() / "030405-same-title.md"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_text("manual file", encoding="utf-8")

    def fail_write(target, content):
        raise OSError("simulated write failure")

    monkeypatch.setattr(notes, "atomic_write", fail_write)

    with pytest.raises(OSError):
        persist_note_capture(
            body="new body",
            source="cli",
            slug_text="same title",
            ts=ts,
        )

    assert existing.read_text(encoding="utf-8") == "manual file"
    rows = db.execute("SELECT * FROM notes").fetchall()
    assert rows == []


def test_post_notes_rejects_empty_text(client):
    r = client.post("/api/notes", json={"text": "   ", "source": "pwa"})
    assert r.status_code == 422


def test_post_notes_uses_cli_source(client):
    r = client.post("/api/notes", json={"text": "from cli", "source": "cli"})
    assert r.status_code == 201
    note_id = r.json()["id"]
    from mastisk.db.queries import connect, get_note
    with connect() as conn:
        row = get_note(conn, note_id)
        assert row["source"] == "cli"


def test_list_notes_returns_most_recent_first(client):
    for i in range(3):
        client.post("/api/notes", json={"text": f"thought {i}"})
    r = client.get("/api/notes")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 3
    assert rows[0]["id"] > rows[1]["id"] > rows[2]["id"]


def test_list_notes_limit_param(client):
    for i in range(5):
        client.post("/api/notes", json={"text": f"thought {i}"})
    r = client.get("/api/notes?limit=2")
    assert r.status_code == 200
    assert len(r.json()) == 2


def test_get_note_detail(client):
    post = client.post("/api/notes", json={"text": "detailed thought"}).json()
    r = client.get(f"/api/notes/{post['id']}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == post["id"]
    assert body["body"] == "detailed thought"
    assert body["classification"] is None
    assert body["source"] == "pwa"


def test_get_note_404_on_unknown(client):
    r = client.get("/api/notes/99999")
    assert r.status_code == 404


def test_delete_note_tombstones_and_removes_file(client, vault_tmp):
    post = client.post("/api/notes", json={"text": "to delete"}).json()
    file_path = vault_tmp / post["path"]
    assert file_path.exists()

    r = client.delete(f"/api/notes/{post['id']}")
    assert r.status_code == 204
    assert not file_path.exists()
    listing = client.get("/api/notes").json()
    assert all(n["id"] != post["id"] for n in listing)


def test_delete_idempotent(client):
    post = client.post("/api/notes", json={"text": "once"}).json()
    assert client.delete(f"/api/notes/{post['id']}").status_code == 204
    r = client.delete(f"/api/notes/{post['id']}")
    assert r.status_code == 404


def test_get_note_file_returns_markdown(client):
    post = client.post("/api/notes", json={"text": "# heading\n\nbody here"}).json()
    r = client.get(f"/api/notes/{post['id']}/file")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/markdown")
    assert "# heading" in r.text


# ─────────────────────────────── Phase 1.5: context-linked capture ───────────────────────────────

def test_post_notes_with_context_creates_link_and_preamble(client, vault_tmp, db):
    # Seed an article. articles.body_md is NOT NULL (no default), so we pass it explicitly.
    db.execute(
        "INSERT INTO articles (id, kind, title, slug, body_md) VALUES (?, 'Concept', ?, ?, '')",
        ("test-article", "Test Article", "test-article"),
    )
    r = client.post("/api/notes", json={
        "text": "my thought on this",
        "source": "pwa",
        "context": {
            "article_id": "test-article",
            "section_heading": "Open questions",
            "question_html": "What is the <em>real</em> tension here?",
        },
    })
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["linked_article_id"] == "test-article"
    # note_links row created (rank=0 for direct user-action links)
    row = db.execute(
        "SELECT rank FROM note_links WHERE note_id = ? AND article_id = ?",
        (body["id"], "test-article"),
    ).fetchone()
    assert row is not None
    assert row["rank"] == 0
    # Body preamble present on disk + in DB
    file_text = (vault_tmp / body["path"]).read_text()
    assert "> From article: Test Article" in file_text
    assert "> Section: Open questions" in file_text
    assert "> Question: What is the real tension here?" in file_text
    assert "my thought on this" in file_text


def test_post_notes_with_unknown_article_id_returns_404(client):
    # Using 404 (not 422) to match REST semantics — the request is well-formed
    # but references a resource that doesn't exist.
    r = client.post("/api/notes", json={
        "text": "doesn't matter",
        "context": {"article_id": "nonexistent-article-slug"},
    })
    assert r.status_code == 404


def test_post_notes_without_context_has_no_link_and_no_preamble(client, vault_tmp):
    r = client.post("/api/notes", json={"text": "no context thought"})
    assert r.status_code == 201
    body = r.json()
    assert body["linked_article_id"] is None
    file_text = (vault_tmp / body["path"]).read_text()
    assert "> From article" not in file_text


def test_list_notes_for_article(client, db):
    db.execute(
        "INSERT INTO articles (id, kind, title, slug, body_md) VALUES (?, 'Concept', ?, ?, '')",
        ("art-a", "Art A", "art-a"),
    )
    db.execute(
        "INSERT INTO articles (id, kind, title, slug, body_md) VALUES (?, 'Concept', ?, ?, '')",
        ("art-b", "Art B", "art-b"),
    )
    r1 = client.post("/api/notes", json={"text": "thought 1", "context": {"article_id": "art-a"}})
    r2 = client.post("/api/notes", json={"text": "thought 2", "context": {"article_id": "art-a"}})
    r3 = client.post("/api/notes", json={"text": "thought 3", "context": {"article_id": "art-b"}})
    assert r1.status_code == r2.status_code == r3.status_code == 201

    r = client.get("/api/articles/art-a/notes")
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 2
    ids_expected = {r1.json()["id"], r2.json()["id"]}
    ids_returned = {n["id"] for n in rows}
    assert ids_returned == ids_expected


# ─────────────────────────────── Phase 2: related articles on detail ───────────────────────────────

def test_get_note_detail_includes_related_articles(client, db):
    # Seed two articles
    db.execute(
        "INSERT INTO articles (id, kind, title, slug, body_md) VALUES ('alpha', 'Concept', 'Alpha', 'alpha', '')"
    )
    db.execute(
        "INSERT INTO articles (id, kind, title, slug, body_md) VALUES ('beta', 'Concept', 'Beta', 'beta', '')"
    )
    post = client.post("/api/notes", json={"text": "links test"}).json()
    # Manually insert note_links (as Notetaker would, post-classification)
    db.execute("INSERT INTO note_links (note_id, article_id, rank) VALUES (?, 'alpha', 0)", (post["id"],))
    db.execute("INSERT INTO note_links (note_id, article_id, rank) VALUES (?, 'beta', 1)", (post["id"],))

    r = client.get(f"/api/notes/{post['id']}")
    assert r.status_code == 200
    body = r.json()
    assert "related_articles" in body
    assert len(body["related_articles"]) == 2
    # Ordered by rank ASC
    assert body["related_articles"][0]["article_id"] == "alpha"
    assert body["related_articles"][0]["title"] == "Alpha"
    assert body["related_articles"][1]["article_id"] == "beta"
