"""Stub-gate tests: wiki_suggestions accumulation, promotion, routes, vault mirror."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mastisk.db import queries as q


@pytest.fixture
def client(db):
    from mastisk.app import create_app
    return TestClient(create_app())


def _seed_article(db, article_id: str, title: str | None = None) -> None:
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, updated_by)
           VALUES (?, 'Concept', ?, ?, 'Compiler')""",
        (article_id, title or article_id, article_id),
    )


def test_first_reference_records_pending_suggestion(db):
    _seed_article(db, "referrer-one")
    minted = q.gate_stub_targets(
        db, from_article="referrer-one", refs={"new-topic": "New Topic"}, min_sources=2,
    )
    assert minted == {}
    assert db.execute("SELECT 1 FROM articles WHERE id='new-topic'").fetchone() is None
    row = db.execute("SELECT * FROM wiki_suggestions WHERE slug='new-topic'").fetchone()
    assert row["status"] == "pending"
    assert row["occurrences"] == 1
    assert row["title"] == "New Topic"


def test_second_distinct_referrer_mints_and_heals_links(db):
    _seed_article(db, "referrer-one")
    _seed_article(db, "referrer-two")
    q.gate_stub_targets(db, from_article="referrer-one", refs={"new-topic": "New Topic"}, min_sources=2)
    minted = q.gate_stub_targets(
        db, from_article="referrer-two", refs={"new-topic": "New Topic"}, min_sources=2,
    )
    assert minted == {"new-topic": "New Topic"}
    stub = db.execute("SELECT updated_by FROM articles WHERE id='new-topic'").fetchone()
    assert stub is not None and "stub" in stub["updated_by"].lower()
    row = db.execute("SELECT status FROM wiki_suggestions WHERE slug='new-topic'").fetchone()
    assert row["status"] == "promoted"
    # Both referrers got their dropped edges healed.
    links = {
        r["from_article"]
        for r in db.execute("SELECT from_article FROM links WHERE to_article='new-topic'")
    }
    assert links == {"referrer-one", "referrer-two"}


def test_same_referrer_twice_counts_once(db):
    _seed_article(db, "referrer-one")
    q.gate_stub_targets(db, from_article="referrer-one", refs={"new-topic": "New Topic"}, min_sources=2)
    minted = q.gate_stub_targets(
        db, from_article="referrer-one", refs={"new-topic": "New Topic"}, min_sources=2,
    )
    assert minted == {}
    row = db.execute("SELECT occurrences FROM wiki_suggestions WHERE slug='new-topic'").fetchone()
    assert row["occurrences"] == 1
    assert db.execute("SELECT 1 FROM articles WHERE id='new-topic'").fetchone() is None


def test_dismissed_keeps_counting_but_never_mints(db):
    _seed_article(db, "referrer-one")
    _seed_article(db, "referrer-two")
    _seed_article(db, "referrer-three")
    q.gate_stub_targets(db, from_article="referrer-one", refs={"noise": "Noise"}, min_sources=2)
    q.decide_wiki_suggestion(db, "noise", action="dismiss")
    for ref in ("referrer-two", "referrer-three"):
        minted = q.gate_stub_targets(db, from_article=ref, refs={"noise": "Noise"}, min_sources=2)
        assert minted == {}
    row = db.execute("SELECT occurrences, status FROM wiki_suggestions WHERE slug='noise'").fetchone()
    assert row["occurrences"] == 3
    assert row["status"] == "dismissed"
    assert db.execute("SELECT 1 FROM articles WHERE id='noise'").fetchone() is None


def test_min_sources_one_restores_legacy_behavior(db):
    _seed_article(db, "referrer-one")
    minted = q.gate_stub_targets(
        db, from_article="referrer-one", refs={"instant": "Instant"}, min_sources=1,
    )
    assert minted == {"instant": "Instant"}
    assert db.execute("SELECT 1 FROM articles WHERE id='instant'").fetchone() is not None
    # Legacy path records no suggestion row.
    assert db.execute("SELECT 1 FROM wiki_suggestions WHERE slug='instant'").fetchone() is None


def test_slug_shaped_title_upgrades_when_real_label_arrives(db):
    _seed_article(db, "referrer-one")
    _seed_article(db, "referrer-two")
    q.gate_stub_targets(db, from_article="referrer-one", refs={"kv-cache": "kv-cache"}, min_sources=3)
    q.gate_stub_targets(db, from_article="referrer-two", refs={"kv-cache": "KV cache"}, min_sources=3)
    row = db.execute("SELECT title FROM wiki_suggestions WHERE slug='kv-cache'").fetchone()
    assert row["title"] == "KV cache"


def test_manual_promote_mints_and_heals(db):
    _seed_article(db, "referrer-one")
    q.gate_stub_targets(db, from_article="referrer-one", refs={"topic": "Topic"}, min_sources=2)
    row = q.decide_wiki_suggestion(db, "topic", action="promote")
    assert row["status"] == "promoted"
    assert db.execute("SELECT 1 FROM articles WHERE id='topic'").fetchone() is not None
    links = db.execute("SELECT 1 FROM links WHERE from_article='referrer-one' AND to_article='topic'").fetchone()
    assert links is not None


def test_restore_returns_dismissed_to_pending(db):
    _seed_article(db, "referrer-one")
    q.gate_stub_targets(db, from_article="referrer-one", refs={"topic": "Topic"}, min_sources=2)
    q.decide_wiki_suggestion(db, "topic", action="dismiss")
    row = q.decide_wiki_suggestion(db, "topic", action="restore")
    assert row["status"] == "pending"
    assert row["decided_at"] is None


def test_suggestion_routes_roundtrip(client, db):
    _seed_article(db, "referrer-one")
    q.gate_stub_targets(db, from_article="referrer-one", refs={"topic": "Topic"}, min_sources=2)

    listed = client.get("/api/suggestions")
    assert listed.status_code == 200
    slugs = [s["slug"] for s in listed.json()["suggestions"]]
    assert "topic" in slugs

    promoted = client.post("/api/suggestions/topic/promote")
    assert promoted.status_code == 200
    assert promoted.json()["status"] == "promoted"
    assert client.get("/api/suggestions").json()["suggestions"] == []

    assert client.post("/api/suggestions/unknown/dismiss").status_code == 404
    assert client.get("/api/suggestions", params={"status": "bogus"}).status_code == 422


def test_vault_mirror_renders_shortlist_and_skips_unchanged(db, vault_tmp):
    from mastisk import wiki_suggestions

    _seed_article(db, "referrer-one")
    q.gate_stub_targets(db, from_article="referrer-one", refs={"topic": "Topic"}, min_sources=2)

    path = wiki_suggestions.render_vault_file()
    assert path is not None and path.exists()
    content = path.read_text()
    assert "**Topic** (`topic`)" in content
    assert "seen in 1 article" in content

    # Unchanged content → same mtime (write skipped).
    mtime = path.stat().st_mtime_ns
    wiki_suggestions.render_vault_file()
    assert path.stat().st_mtime_ns == mtime
