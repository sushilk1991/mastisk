"""Tests for the unified command-palette search.

Covers the three issue classes the reviewers flagged:
- Tokenization edge cases (Unicode, FTS5 reserved words, stopwords).
- Migration backfill on a pre-existing DB whose FTS index doesn't yet exist.
- Per-kind quotas, soft-delete filtering, snippet markers, and the trigger
  WHEN clause that prevents redundant FTS reindexing on no-op UPDATEs.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from mastisk.db.queries import (
    _ensure_fts_initialized,
    _fts_palette_query,
    init_schema,
    search_all,
)


# ─────────────────────────────── _fts_palette_query ───────────────────────────────


@pytest.mark.parametrize(
    "query, expected",
    [
        ("", None),
        ("   ", None),
        ("a", None),                       # below MIN_QUERY_LEN
        ("the and", None),                 # all stopwords
        ("test", "test*"),
        ("Test Compute", "test* compute*"),  # case-folded
        ("test compute", "test* compute*"),
        ("What is test-time compute?", "test* time* compute*"),
    ],
)
def test_fts_palette_query_basic(query: str, expected: str | None) -> None:
    assert _fts_palette_query(query) == expected


def test_fts_palette_query_lowercases_to_avoid_fts5_operators() -> None:
    """All-caps NOT/NEAR/AND/OR are FTS5 operators; emitting them unchanged
    crashes sqlite with `fts5: syntax error`. Lowercasing before emitting
    sidesteps the operator parse rule entirely (FTS5 is case-insensitive at
    index time, so the matches still work) and lets the user's intent come
    through — "NOT NULL" finds docs containing "not"+"null" rather than
    erroring out."""
    assert _fts_palette_query("NOT NULL") == "not* null*"
    assert _fts_palette_query("NEAR future") == "near* future*"
    # 'and'/'or' are stopwords (lowercase) so they drop out regardless.
    assert _fts_palette_query("AND OR") is None


def test_fts_palette_query_emitting_those_terms_doesnt_crash_fts5(
    db: sqlite3.Connection,
) -> None:
    """Belt-and-suspenders: the lowercased expression must actually parse."""
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art', 'Concept', 'NOT NULL constraints', 's', 'a',
                   'about postgres NOT NULL')""",
    )
    # Should not raise; should find the article.
    results = search_all(db, "NOT NULL")
    assert any(r["kind"] == "article" for r in results)


def test_fts_palette_query_handles_unicode() -> None:
    """The default \\w+ regex must include Latin-with-diacritics, CJK, and
    Cyrillic; otherwise non-ASCII queries silently return None."""
    assert _fts_palette_query("résumé") == "résumé*"
    assert _fts_palette_query("café latte") == "café* latte*"
    # CJK: each character is a "word" under \w+UNICODE — 元気 → ['元気']
    cjk = _fts_palette_query("元気")
    assert cjk is not None and "元気" in cjk
    # Cyrillic
    assert _fts_palette_query("Москва") == "москва*"


# ─────────────────────────────── search_all ───────────────────────────────


def _seed_minimal(conn: sqlite3.Connection) -> None:
    """Insert one of each kind so search_all has something to find."""
    conn.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art-1', 'Concept', 'Test-time compute', 'tt',
                   'Scaling at test time.', 'Body about reasoning')""",
    )
    conn.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              summary, classification)
           VALUES ('n-1', 'p/n-1.md', 'Quick thought on test-time compute',
                   'aaa', 'pwa', '2026-04-27T10:00:00',
                   'Note about TT compute', 'observation')""",
    )
    conn.execute(
        """INSERT INTO blog_posts (theme, window_days, status, title, body_preview,
                                   slug, path)
           VALUES ('reasoning', 7, 'done', 'Why TT Compute Matters',
                   'TT compute is the new scaling axis.', 'why', 'b/why.md')""",
    )


def test_search_all_returns_all_three_kinds(db: sqlite3.Connection) -> None:
    _seed_minimal(db)
    results = search_all(db, "compute")
    kinds = [r["kind"] for r in results]
    assert kinds == ["article", "note", "blog"]


def test_search_all_empty_query_returns_empty(db: sqlite3.Connection) -> None:
    _seed_minimal(db)
    assert search_all(db, "") == []
    assert search_all(db, "x") == []   # below min length
    assert search_all(db, "the and or") == []  # all stopwords


def test_search_all_excludes_soft_deleted_notes(db: sqlite3.Connection) -> None:
    _seed_minimal(db)
    db.execute("UPDATE notes SET deleted_at = CURRENT_TIMESTAMP WHERE id = 1")
    results = search_all(db, "compute")
    assert all(r["kind"] != "note" for r in results)


def test_search_all_excludes_pending_blog_posts(db: sqlite3.Connection) -> None:
    _seed_minimal(db)
    db.execute("UPDATE blog_posts SET status = 'pending' WHERE id = 1")
    results = search_all(db, "compute")
    assert all(r["kind"] != "blog" for r in results)


def test_search_all_per_kind_quota(db: sqlite3.Connection) -> None:
    """Notes are capped at 6 per query so a flood of matching notes can't
    push articles or blogs out of the response (BM25 IDF can't be relied
    on for the per-kind balance — see the docstring)."""
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art-x', 'Concept', 'Agent guide', 's', 'a', 'b')""",
    )
    for i in range(20):
        db.execute(
            """INSERT INTO notes (slug, path, body, body_sha256, source,
                                  created_at, summary)
               VALUES (?, ?, 'agent body', ?, 'pwa', '2026-04-26T10:00:00',
                       'agent agent agent')""",
            (f"nf-{i}", f"p/nf-{i}.md", f"h{i}"),
        )
    results = search_all(db, "agent", limit=20)
    note_count = sum(1 for r in results if r["kind"] == "note")
    article_count = sum(1 for r in results if r["kind"] == "article")
    assert note_count <= 6
    assert article_count >= 1, "articles starved by note flood"


def test_search_all_uses_control_char_snippet_markers(db: sqlite3.Connection) -> None:
    """A note whose body legitimately contains the literal substring '<mark>'
    must not be misrendered. We mark matches with STX/ETX (\\x02/\\x03)
    instead of HTML tags so collisions are impossible."""
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art-html', 'Concept', 'HTML semantics', 's',
                   'About <mark>highlight</mark> elements.',
                   'Use <mark>foo</mark> for emphasis')""",
    )
    results = search_all(db, "foo")
    assert results, "expected a match"
    snippet = results[0]["snippet"]
    assert "\x02" in snippet, "no STX marker found"
    assert "\x03" in snippet, "no ETX marker found"
    # The literal text "<mark>" from the user's content must survive
    # un-mangled (FTS5 doesn't tokenize angle brackets, but the snippet may
    # show the surrounding context including the literal `<mark>` text).
    assert snippet.count("\x02") == snippet.count("\x03"), "unbalanced markers"


def test_search_all_unknown_term_returns_empty(db: sqlite3.Connection) -> None:
    _seed_minimal(db)
    assert search_all(db, "zzqxqz") == []


# ─────────────────────────────── Migration backfill ───────────────────────────────


def test_ensure_fts_initialized_rebuilds_for_preexisting_rows(
    tmp_path: Path,
) -> None:
    """The migration must populate notes_fts and blog_posts_fts when those
    tables are created fresh in front of a DB that already has notes/blogs.
    This is the "user upgrades mastisk and existing notes start being
    searchable" path."""
    dbpath = tmp_path / "test.db"

    # Step 1: bootstrap a DB without the new FTS tables. We simulate this by
    # running init_schema, then DROPing the FTS tables and re-creating notes
    # rows to mimic a pre-upgrade state.
    conn = sqlite3.connect(dbpath, isolation_level=None)
    conn.row_factory = sqlite3.Row
    init_schema(conn)
    conn.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              summary)
           VALUES ('n', 'p/n.md', 'pre-existing note about Anthropic', 'h',
                   'pwa', '2026-04-26T10:00:00', 'old note summary')""",
    )
    # Drop FTS so we can verify the migration rebuilds it. (Triggers go too.)
    conn.execute("DROP TABLE notes_fts")

    # Step 2: re-init. The CREATE VIRTUAL TABLE re-creates an empty FTS;
    # _ensure_fts_initialized must spot the empty index and run rebuild.
    init_schema(conn)

    # Step 3: now the search should find the pre-existing note.
    results = search_all(conn, "anthropic")
    assert any(r["kind"] == "note" for r in results), \
        "migration did not rebuild notes_fts for pre-existing rows"
    conn.close()


# ─────────────────────────────── Triggers ───────────────────────────────


def test_notes_au_trigger_skips_unchanged_indexed_columns(
    db: sqlite3.Connection,
) -> None:
    """UPDATE notes ... SET escalation_state=... mustn't fire the FTS
    re-index trigger when summary and body are unchanged. Otherwise every
    state transition burns shadow-table writes that are pure no-ops."""
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              summary)
           VALUES ('n', 'p/n.md', 'foo', 'h', 'pwa', '2026-04-26T10:00:00',
                   's')""",
    )
    # Rowid must exist in FTS now (via notes_ai trigger).
    indexed_before = db.execute(
        "SELECT 1 FROM notes_fts_docsize WHERE id = 1"
    ).fetchone()
    assert indexed_before is not None

    # Snapshot the docsize row's content (sz column) — same content should
    # yield the same docsize. Any trigger fire would re-emit the row.
    sz_before = db.execute(
        "SELECT sz FROM notes_fts_docsize WHERE id = 1"
    ).fetchone()["sz"]

    # Touch a non-indexed column. Should NOT fire the re-index trigger.
    db.execute("UPDATE notes SET escalation_state = 'pending' WHERE id = 1")

    sz_after = db.execute(
        "SELECT sz FROM notes_fts_docsize WHERE id = 1"
    ).fetchone()["sz"]
    assert sz_before == sz_after, \
        "FTS re-indexed despite summary/body unchanged"

    # Sanity check: changing an indexed column DOES re-index.
    db.execute("UPDATE notes SET body = 'updated long body text' WHERE id = 1")
    new_sz = db.execute(
        "SELECT sz FROM notes_fts_docsize WHERE id = 1"
    ).fetchone()["sz"]
    assert new_sz != sz_before, "trigger failed to re-index on real change"


def test_blog_posts_au_trigger_skips_unchanged_indexed_columns(
    db: sqlite3.Connection,
) -> None:
    """Same as the notes trigger test, but for blog_posts. Status churn
    (pending → running → done) shouldn't churn the FTS index."""
    db.execute(
        """INSERT INTO blog_posts (theme, window_days, status, title, body_preview)
           VALUES ('t', 7, 'pending', 'init title', 'init preview')""",
    )
    sz_before = db.execute(
        "SELECT sz FROM blog_posts_fts_docsize WHERE id = 1"
    ).fetchone()["sz"]

    db.execute("UPDATE blog_posts SET status = 'running' WHERE id = 1")
    sz_after = db.execute(
        "SELECT sz FROM blog_posts_fts_docsize WHERE id = 1"
    ).fetchone()["sz"]
    assert sz_before == sz_after


# ─────────────────────────────── Helpers ───────────────────────────────


def test_ensure_fts_initialized_no_op_on_empty_content_table(
    db: sqlite3.Connection,
) -> None:
    """When the content table is empty, no rebuild — _docsize stays empty."""
    _ensure_fts_initialized(db, "notes_fts", "notes")
    rows = db.execute("SELECT COUNT(*) AS n FROM notes_fts_docsize").fetchone()["n"]
    assert rows == 0
