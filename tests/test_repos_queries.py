"""Repo query tests: re-inserting a tracked repo must not destroy its history."""
from __future__ import annotations

from mastisk.db import queries as q

_META = dict(
    slug="a/b", owner="a", name="b", display_name="a/b",
    description="first", default_branch="main", is_private=0,
)


def test_insert_repo_twice_preserves_history_and_poll_state(db):
    q.insert_repo(db, **_META)
    snap_id = q.insert_repo_snapshot(db, repo_slug="a/b", polled_at="2026-01-01T00:00:00Z")
    q.insert_repo_idea_run(
        db, repo_slug="a/b", ideated_at="2026-01-01T00:05:00Z",
        snapshot_id=snap_id, note_ids_json="[]", model="m",
    )
    q.update_repo_context(db, slug="a/b", context_md="# ctx", polled_at="2026-01-01T00:00:00Z")
    q.mark_repo_ideated(db, slug="a/b", ideated_at="2026-01-01T00:05:00Z")
    added_at = q.get_repo(db, "a/b")["added_at"]

    q.insert_repo(db, **{**_META, "description": "second", "is_private": 1})

    repo = q.get_repo(db, "a/b")
    assert repo["description"] == "second"
    assert repo["is_private"] == 1
    assert repo["added_at"] == added_at
    assert repo["context_md"] == "# ctx"
    assert repo["last_polled_at"] == "2026-01-01T00:00:00Z"
    assert repo["last_ideated_at"] == "2026-01-01T00:05:00Z"
    assert db.execute("SELECT COUNT(*) FROM repo_snapshots WHERE repo_slug='a/b'").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM repo_idea_runs WHERE repo_slug='a/b'").fetchone()[0] == 1


def test_insert_repo_keeps_local_source_fields(db):
    db.execute(
        """INSERT INTO repos (slug, source_type, owner, name, local_path)
           VALUES ('a/b', 'local', 'a', 'b', '/tmp/b')"""
    )
    q.insert_repo(db, **_META)
    repo = q.get_repo(db, "a/b")
    assert repo["source_type"] == "local"
    assert repo["local_path"] == "/tmp/b"


def test_insert_repo_revives_soft_deleted_repo(db):
    q.insert_repo(db, **_META)
    q.soft_delete_repo(db, "a/b")
    q.insert_repo(db, **_META)
    assert q.get_repo(db, "a/b")["deleted_at"] is None
