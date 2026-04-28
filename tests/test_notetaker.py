"""Notetaker unit tests. Mocks claude_bridge.run_claude so no subprocess."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

# ─────────────────────────────── fixtures ───────────────────────────────


@pytest.fixture
def notetaker(db, vault_tmp):
    """A fresh Notetaker with vault paths + DB fixture active."""
    from mastisk.paths import ensure_dirs
    ensure_dirs()
    from mastisk.agents.notetaker import Notetaker
    return Notetaker()


@pytest.fixture
def fake_ollama():
    """Patch ``claude_bridge.run_claude`` inside the notetaker module with a canned response.

    Fixture name kept for historical churn-avoidance — the patched target is
    Claude now, but the call sites still reference ``fake_ollama``."""
    default = {
        "text": json.dumps({
            "classification": "idea",
            "summary": "a brief summary",
            "confidence": 0.85,
            "tags": ["ai", "mastisk"],
            "related_articles": [],
        }),
    }
    with patch(
        "mastisk.agents.notetaker.claude_bridge.run_claude",
        new_callable=AsyncMock,
    ) as mock:
        mock.return_value = default
        yield mock


# ─────────────────────────────── pure helpers ───────────────────────────────


def test_strip_frontmatter_noop_when_missing():
    from mastisk.agents.notetaker import strip_frontmatter
    assert strip_frontmatter("no frontmatter\nbody") == "no frontmatter\nbody"
    assert strip_frontmatter("") == ""


def test_strip_frontmatter_removes_block():
    from mastisk.agents.notetaker import strip_frontmatter
    assert strip_frontmatter("---\nk: v\n---\n\nbody") == "body"
    assert strip_frontmatter("---\nk: v\n---\nbody") == "body"
    # Multi-line body survives.
    assert strip_frontmatter("---\nk: v\n---\n\nline 1\nline 2") == "line 1\nline 2"


def test_write_frontmatter_preserves_body(tmp_path):
    from mastisk.agents.notetaker import write_frontmatter
    p = tmp_path / "note.md"
    write_frontmatter(p, "the body\nline 2", {"k": "v", "n": 1})
    text = p.read_text()
    assert text.startswith("---\n")
    assert "k: v" in text
    assert "n: 1" in text
    assert text.endswith("the body\nline 2")


def test_write_frontmatter_replaces_existing(tmp_path):
    from mastisk.agents.notetaker import write_frontmatter
    p = tmp_path / "note.md"
    # Pre-existing frontmatter should be stripped, not nested.
    write_frontmatter(p, "---\nold: true\n---\n\nactual body", {"new": True})
    text = p.read_text()
    # Only one frontmatter block.
    assert text.count("---\n") == 2  # opening + closing of the single block
    assert "new: true" in text
    assert "old: true" not in text
    assert text.endswith("actual body")


def test_extract_json_raw():
    from mastisk.agents.notetaker import _extract_json
    assert _extract_json('{"k": 1}') == {"k": 1}


def test_extract_json_fenced():
    from mastisk.agents.notetaker import _extract_json
    text = 'preamble\n```json\n{"k": 2}\n```\ntrailer'
    assert _extract_json(text) == {"k": 2}


def test_extract_json_naked_braces():
    from mastisk.agents.notetaker import _extract_json
    text = 'here is the answer: {"k": 3} — hope that helps'
    assert _extract_json(text) == {"k": 3}


def test_extract_json_raises_on_nothing():
    from mastisk.agents.notetaker import _extract_json
    with pytest.raises(ValueError):
        _extract_json("no json here at all")


# ─────────────────────────────── end-to-end classify ───────────────────────────────


def test_notetaker_classifies_a_note(notetaker, db, vault_tmp, fake_ollama):
    """Inbox → classified: file moves, frontmatter written, DB updated, feed row emitted."""
    from mastisk.agents.base import enqueue
    from mastisk.db.queries import insert_note
    from mastisk.paths import notes_inbox_dir, vault_dir

    inbox = notes_inbox_dir()
    p = inbox / "143522-test-thought.md"
    p.write_text("test thought body")

    rel = str(p.relative_to(vault_dir()))
    note_id = insert_note(
        db, slug="143522-test-thought", path=rel,
        body="test thought body", source="pwa",
        created_at=datetime(2026, 4, 21, 14, 35, 22),
    )
    enqueue("notetaker", "classify", {"note_id": note_id})

    asyncio.run(notetaker.run_once())

    row = db.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    assert row["classification"] == "idea"
    assert row["summary"] == "a brief summary"
    assert row["confidence"] == pytest.approx(0.85)
    assert json.loads(row["tags_json"]) == ["ai", "mastisk"]
    assert row["path"] == "_notes/2026-04-21/143522-test-thought.md"
    assert row["classified_at"] is not None

    new_path = vault_dir() / row["path"]
    assert new_path.exists()
    content = new_path.read_text()
    assert content.startswith("---\n")
    assert "classification: idea" in content
    assert "summary: a brief summary" in content
    # Original prose preserved.
    assert content.endswith("test thought body")
    # Old inbox file removed.
    assert not p.exists()

    # Feed row emitted with verb='classified'.
    feed = db.execute(
        "SELECT * FROM feed WHERE agent='notetaker' AND verb='classified'"
    ).fetchall()
    assert len(feed) == 1
    assert feed[0]["obj"] == "143522-test-thought"


def test_notetaker_links_existing_related_articles(notetaker, db, vault_tmp):
    """Only related_articles that exist in the `articles` table become note_links."""
    from mastisk.agents.base import enqueue
    from mastisk.db.queries import insert_note
    from mastisk.paths import notes_inbox_dir, vault_dir

    # Seed two articles; the fake response will reference one real + one bogus.
    db.execute(
        "INSERT INTO articles (id, kind, title, slug, body_md) "
        "VALUES ('real-article', 'Concept', 'Real Article', 'real-article', '')"
    )

    inbox = notes_inbox_dir()
    p = inbox / "143522-linked.md"
    p.write_text("about real-article and something else")
    rel = str(p.relative_to(vault_dir()))
    note_id = insert_note(
        db, slug="143522-linked", path=rel,
        body=p.read_text(), source="pwa",
        created_at=datetime(2026, 4, 21, 14, 35, 22),
    )
    enqueue("notetaker", "classify", {"note_id": note_id})

    canned = {
        "text": json.dumps({
            "classification": "insight",
            "summary": "thoughts on real-article",
            "confidence": 0.9,
            "tags": ["tag-a"],
            "related_articles": ["real-article", "hallucinated-slug"],
        }),
    }
    with patch(
        "mastisk.agents.notetaker.claude_bridge.run_claude",
        new_callable=AsyncMock,
        return_value=canned,
    ):
        asyncio.run(notetaker.run_once())

    # Link exists for the real article, skipped for the hallucinated one.
    links = db.execute(
        "SELECT article_id, rank FROM note_links WHERE note_id = ?",
        (note_id,),
    ).fetchall()
    assert len(links) == 1
    assert links[0]["article_id"] == "real-article"
    # rank >= 1 for classifier-created (rank 0 is reserved for direct-action).
    assert links[0]["rank"] >= 1


def test_notetaker_skips_already_classified_note(notetaker, db, vault_tmp, fake_ollama):
    """A note whose row already has classification set isn't re-processed."""
    from mastisk.agents.base import enqueue
    from mastisk.db.queries import insert_note
    from mastisk.paths import notes_inbox_dir, vault_dir

    inbox = notes_inbox_dir()
    p = inbox / "090000-prior.md"
    p.write_text("already done")
    rel = str(p.relative_to(vault_dir()))
    note_id = insert_note(
        db, slug="090000-prior", path=rel,
        body="already done", source="pwa",
        created_at=datetime(2026, 4, 21, 9, 0, 0),
    )
    # Mark it classified already.
    db.execute(
        "UPDATE notes SET classification='idea', summary='done', confidence=0.5, "
        "classified_at=? WHERE id=?",
        (datetime(2026, 4, 21, 9, 0, 10).isoformat(), note_id),
    )
    enqueue("notetaker", "classify", {"note_id": note_id})

    asyncio.run(notetaker.run_once())

    # Ollama was not called.
    assert fake_ollama.call_count == 0
    # File not moved (still in inbox).
    assert p.exists()


# ─────────────────────────────── scan-path filters ───────────────────────────────


def test_notetaker_skips_unstable_file_on_first_tick(notetaker, vault_tmp):
    """First scan of a new file registers it in the cache but doesn't enqueue."""
    from mastisk.paths import notes_inbox_dir

    inbox = notes_inbox_dir()
    p = inbox / "090000-unstable.md"
    p.write_text("brand new")

    asyncio.run(notetaker.run_once())

    # Cache entry exists, but no note row was created (external-editor path
    # only runs after stability is confirmed).
    from mastisk.db.queries import connect
    with connect() as conn:
        rows = conn.execute("SELECT * FROM notes").fetchall()
    assert rows == []
    # The stability cache tracked the file.
    assert any("090000-unstable" in k for k in notetaker._stability_cache)


def test_notetaker_skips_icloud_placeholder(notetaker, vault_tmp):
    """A `.icloud` placeholder is ignored entirely (not even cached)."""
    from mastisk.paths import notes_inbox_dir

    inbox = notes_inbox_dir()
    # iCloud uses a leading dot + .icloud suffix: `.foo.md.icloud`.
    p = inbox / ".090000-pending.md.icloud"
    p.write_text("placeholder content")

    asyncio.run(notetaker.run_once())

    assert p.name.startswith(".") or p.name.endswith(".icloud")
    # Nothing in the stability cache for this file — we skip before touching it.
    assert not any(".icloud" in k for k in notetaker._stability_cache)


def test_notetaker_skips_conflict_copy(notetaker, vault_tmp):
    """Files with 'conflicted' in the name are ignored (iCloud conflict-copy pattern)."""
    from mastisk.paths import notes_inbox_dir

    inbox = notes_inbox_dir()
    p = inbox / "090000-foo (conflicted copy 2026-04-22).md"
    p.write_text("whatever")

    asyncio.run(notetaker.run_once())

    # Not cached — it was skipped before the stability check.
    assert not any("conflicted" in k.lower() for k in notetaker._stability_cache)


def test_notetaker_skips_dotfile(notetaker, vault_tmp):
    """Hidden files (e.g. our own atomic-write temps) are skipped."""
    from mastisk.paths import notes_inbox_dir

    inbox = notes_inbox_dir()
    p = inbox / ".143522-tempfile.md.tmp"
    p.write_text("atomic-write artifact")

    asyncio.run(notetaker.run_once())

    # Tempfile not picked up. No note row created.
    from mastisk.db.queries import connect
    with connect() as conn:
        rows = conn.execute("SELECT COUNT(*) AS n FROM notes").fetchone()
    assert rows["n"] == 0


# ─────────────────────────────── daily digest ───────────────────────────────


def test_write_daily_digest_empty(db, vault_tmp):
    """No notes for the date → digest file written with an empty state."""
    from mastisk.agents.notetaker import write_daily_digest
    p = write_daily_digest(db, "2026-04-21")
    assert p.exists()
    text = p.read_text()
    assert text.startswith("---")
    assert "date: '2026-04-21'" in text or 'date: "2026-04-21"' in text
    assert "note_count: 0" in text
    assert "no classified notes yet" in text


def test_write_daily_digest_renders_classified_notes(db, vault_tmp):
    """Seed two classified notes on the same date; digest lists both in time order."""
    from datetime import datetime
    import json as _json
    from mastisk.agents.notetaker import write_daily_digest

    # Two notes on 2026-04-21
    for (hh, mm, slug, summary, cls, tags) in [
        ("09", "15", "091500-foo", "first thought of the day", "insight", ["morning"]),
        ("14", "02", "140200-bar", "afternoon reflection", "idea", ["ai", "compound"]),
    ]:
        ts = datetime(2026, 4, 21, int(hh), int(mm)).astimezone().isoformat()
        db.execute(
            """INSERT INTO notes (slug, path, body, body_sha256, source,
                                   created_at, classified_at, classification, summary, tags_json)
               VALUES (?, ?, ?, 'sha', 'pwa', ?, ?, ?, ?, ?)""",
            (slug, f"_notes/2026-04-21/{slug}.md", f"body of {slug}",
             ts, ts, cls, summary, _json.dumps(tags)),
        )

    # And an unclassified note on the same date — should be excluded
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at)
           VALUES ('120000-unclassified', '_notes/inbox/120000-unclassified.md',
                   'raw', 'sha2', 'pwa', ?)""",
        (datetime(2026, 4, 21, 12, 0).astimezone().isoformat(),),
    )

    p = write_daily_digest(db, "2026-04-21")
    text = p.read_text()
    # Both classified notes render
    assert "09:15" in text
    assert "14:02" in text
    assert "first thought of the day" in text
    assert "afternoon reflection" in text
    assert "insight" in text
    assert "idea" in text
    assert "#morning" in text
    assert "#ai" in text
    # Unclassified excluded
    assert "120000-unclassified" not in text
    # note_count is 2
    assert "note_count: 2" in text


def test_daily_digest_excludes_deleted_notes(db, vault_tmp):
    """Deleted notes don't show in the digest."""
    from datetime import datetime
    from mastisk.agents.notetaker import write_daily_digest
    ts = datetime(2026, 4, 21, 10, 0).astimezone().isoformat()
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source,
                               created_at, classified_at, classification, summary, deleted_at)
           VALUES ('100000-gone', '_notes/2026-04-21/100000-gone.md', 'b', 'sha', 'pwa',
                   ?, ?, 'rant', 'grumble', ?)""",
        (ts, ts, ts),
    )
    p = write_daily_digest(db, "2026-04-21")
    text = p.read_text()
    assert "100000-gone" not in text
    assert "note_count: 0" in text
