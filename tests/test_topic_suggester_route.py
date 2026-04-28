"""Topic-suggester route tests. See src/mastisk/routes/topic_suggester_route.py."""
from __future__ import annotations

import json
from datetime import datetime, timezone

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


def _insert_active(
    db,
    *,
    title: str,
    hook: str = "h",
    refs: list | None = None,
    days_ago: int = 0,
) -> int:
    """Insert an active 'daily' topic suggestion. ``days_ago`` lets a test
    spread multiple rows across calendar days to side-step the
    ``idx_topic_suggestions_one_per_day`` unique index."""
    refs_json = json.dumps(refs or [])
    if days_ago > 0:
        cur = db.execute(
            """INSERT INTO topic_suggestions
               (kind, title, hook, source_refs_json, created_at)
               VALUES ('daily', ?, ?, ?, datetime('now', ?))""",
            (title, hook, refs_json, f"-{int(days_ago)} days"),
        )
    else:
        cur = db.execute(
            """INSERT INTO topic_suggestions (kind, title, hook, source_refs_json)
               VALUES ('daily', ?, ?, ?)""",
            (title, hook, refs_json),
        )
    return cur.lastrowid or 0


def _seed_note_for_route(db, *, summary: str = "summary") -> int:
    now = datetime.now(timezone.utc).isoformat(sep=" ", timespec="seconds")
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              classified_at, classification, summary, tags_json)
           VALUES ('rt-n', '_notes/inbox/rt-n.md', 'body line', 'h', 'pwa', ?, ?,
                   'idea', ?, '[]')""",
        (now, now, summary),
    )
    return db.execute("SELECT id FROM notes WHERE slug='rt-n'").fetchone()["id"]


def _seed_article_for_route(db, *, article_id: str = "a-rt", title: str = "World article") -> None:
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, body_md, summary)
           VALUES (?, 'Source', ?, ?, 'body', 'world summary')""",
        (article_id, title, article_id),
    )


# ───── GET ─────


def test_get_returns_only_active_rows(client, db):
    """3 rows: dismissed, used, active. GET returns just the active one.
    Spread across days so the per-day unique index is happy."""
    sid_dismissed = _insert_active(db, title="dismissed-topic", days_ago=2)
    db.execute(
        "UPDATE topic_suggestions SET dismissed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (sid_dismissed,),
    )
    sid_used = _insert_active(db, title="used-topic", days_ago=1)
    db.execute(
        """INSERT INTO blog_posts (theme, window_days, status)
           VALUES ('used-topic', 14, 'pending')"""
    )
    bp_id = db.execute(
        "SELECT id FROM blog_posts WHERE theme='used-topic'",
    ).fetchone()["id"]
    db.execute(
        "UPDATE topic_suggestions SET used_blog_id = ? WHERE id = ?",
        (bp_id, sid_used),
    )
    sid_active = _insert_active(db, title="active-topic")

    r = client.get("/api/topic-suggestions")
    assert r.status_code == 200
    body = r.json()
    ids = [s["id"] for s in body["suggestions"]]
    assert ids == [sid_active]
    assert body["suggestions"][0]["title"] == "active-topic"
    assert body["suggestions"][0]["kind"] == "daily"


def test_get_resolves_source_refs_to_labels(client, db):
    """Refs round-trip through the label resolver."""
    note_id = _seed_note_for_route(db, summary="The note summary")
    _seed_article_for_route(db, article_id="art-1", title="World article title")
    sid = _insert_active(
        db,
        title="topic-with-refs",
        refs=[
            {"kind": "note", "ref": note_id},
            {"kind": "article", "ref": "art-1"},
        ],
    )

    r = client.get("/api/topic-suggestions")
    assert r.status_code == 200
    payload = r.json()
    assert len(payload["suggestions"]) == 1
    s = payload["suggestions"][0]
    assert s["id"] == sid
    refs_by_kind = {ref["kind"]: ref for ref in s["source_refs"]}
    assert refs_by_kind["note"]["label"] == "The note summary"
    assert refs_by_kind["note"]["deleted"] is False
    assert refs_by_kind["article"]["label"] == "World article title"
    assert refs_by_kind["article"]["deleted"] is False


def test_get_marks_missing_refs_deleted(client, db):
    """Refs to non-existent rows show as deleted."""
    _insert_active(
        db,
        title="dangling",
        refs=[
            {"kind": "note", "ref": 999999},
            {"kind": "article", "ref": "missing-art"},
        ],
    )
    r = client.get("/api/topic-suggestions")
    assert r.status_code == 200
    refs = r.json()["suggestions"][0]["source_refs"]
    for ref in refs:
        assert ref["deleted"] is True


def test_get_labels_deleted_source_refs(client, db):
    """Topic referencing a missing note id still surfaces — the ref is
    flagged ``deleted: True`` and labeled ``(note deleted)`` so the UI
    can grey it out without losing the topic itself."""
    _insert_active(
        db,
        title="references a tombstone",
        refs=[{"kind": "note", "ref": 9999}],
    )
    r = client.get("/api/topic-suggestions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["suggestions"]) == 1
    s = body["suggestions"][0]
    assert s["title"] == "references a tombstone"
    assert len(s["source_refs"]) == 1
    ref = s["source_refs"][0]
    assert ref["kind"] == "note"
    assert ref["ref"] == 9999
    assert ref["deleted"] is True
    assert ref["label"] == "(note deleted)"


# ───── dismiss ─────


def test_dismiss_marks_row(client, db):
    """POST dismiss → row leaves the active list."""
    sid = _insert_active(db, title="to-dismiss")
    r = client.post(f"/api/topic-suggestions/{sid}/dismiss")
    assert r.status_code == 204

    listing = client.get("/api/topic-suggestions").json()
    assert listing["suggestions"] == []

    row = db.execute(
        "SELECT dismissed_at FROM topic_suggestions WHERE id = ?", (sid,),
    ).fetchone()
    assert row["dismissed_at"] is not None


def test_dismiss_404_on_missing(client):
    r = client.post("/api/topic-suggestions/99999/dismiss")
    assert r.status_code == 404


def test_dismiss_idempotent(client, db):
    """Dismissing an already-dismissed row is a no-op (still 204)."""
    sid = _insert_active(db, title="already-gone")
    db.execute(
        "UPDATE topic_suggestions SET dismissed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (sid,),
    )
    r = client.post(f"/api/topic-suggestions/{sid}/dismiss")
    assert r.status_code == 204


# ───── blog draft → topic link ─────


def test_blog_create_links_to_matching_topic(client, db):
    """Drafting a blog with theme matching an active topic links them."""
    _seed_note_for_route(db)
    sid = _insert_active(db, title="X")

    r = client.post("/api/blog-posts", json={"theme": "X", "window_days": 14})
    assert r.status_code == 202, r.text
    bp_id = r.json()["id"]

    row = db.execute(
        "SELECT used_blog_id FROM topic_suggestions WHERE id = ?", (sid,),
    ).fetchone()
    assert row["used_blog_id"] == bp_id

    # And the suggestion is no longer in the active list.
    listing = client.get("/api/topic-suggestions").json()
    assert listing["suggestions"] == []


def test_blog_create_link_is_case_insensitive(client, db):
    """Theme casing differences don't break the link."""
    _seed_note_for_route(db)
    sid = _insert_active(db, title="Witnesses Beat Postmortems")

    r = client.post(
        "/api/blog-posts",
        json={"theme": "witnesses beat postmortems", "window_days": 14},
    )
    assert r.status_code == 202
    bp_id = r.json()["id"]

    row = db.execute(
        "SELECT used_blog_id FROM topic_suggestions WHERE id = ?", (sid,),
    ).fetchone()
    assert row["used_blog_id"] == bp_id


def test_blog_create_no_match_leaves_topics_active(client, db):
    """A theme that doesn't match any topic leaves the active list alone."""
    _seed_note_for_route(db)
    sid = _insert_active(db, title="Different topic")

    r = client.post(
        "/api/blog-posts", json={"theme": "Unrelated theme", "window_days": 14},
    )
    assert r.status_code == 202

    row = db.execute(
        "SELECT used_blog_id FROM topic_suggestions WHERE id = ?", (sid,),
    ).fetchone()
    assert row["used_blog_id"] is None
