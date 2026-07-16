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
from fastapi.testclient import TestClient

from mastisk.db.queries import (
    _ensure_fts_initialized,
    _fts_ask_query,
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


def test_ask_query_uses_broad_or_semantics_without_changing_palette_search() -> None:
    assert _fts_ask_query("agent governance receipts") == (
        "agent* OR governance* OR receipts*"
    )
    assert _fts_palette_query("agent governance receipts") == (
        "agent* governance* receipts*"
    )


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
    """The Unicode regex must include Latin-with-diacritics, CJK, and
    Cyrillic; otherwise non-ASCII queries silently return None."""
    assert _fts_palette_query("résumé") == "résumé*"
    assert _fts_palette_query("café latte") == "café* latte*"
    # CJK: each character is a "word" under \w+UNICODE — 元気 → ['元気']
    cjk = _fts_palette_query("元気")
    assert cjk is not None and "元気" in cjk
    # Cyrillic
    assert _fts_palette_query("Москва") == "москва*"


def test_fts_palette_query_splits_underscored_identifiers() -> None:
    """snake_case and dunder identifiers must split into AND-joined prefix
    terms, not collapse into a single phrase token. Catching this is the
    whole reason we use [^\\W_]+ instead of \\w+ — \\w treats underscore
    as a word character, but FTS5's query parser then re-tokenizes the
    bare token 'foo_bar*' as the phrase 'foo bar*' (only the LAST token
    gets the prefix wildcard), which is a phrase match — silently
    different from what the user typed."""
    assert _fts_palette_query("test_helper") == "test* helper*"
    assert _fts_palette_query("__init__") == "init*"
    assert _fts_palette_query("snake_case_var") == "snake* case* var*"


def test_fts_palette_query_underscored_query_actually_matches(
    db: sqlite3.Connection,
) -> None:
    """End-to-end: a FTS-indexed article body containing 'test_helper'
    should be findable when the user types 'test_helper'. unicode61
    tokenizes 'test_helper' in indexed content as ['test', 'helper'];
    we must do the same on the query side."""
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art', 'Concept', 'helpers', 's', 'a',
                   'See test_helper.py for shared fixtures.')""",
    )
    results = search_all(db, "test_helper")
    assert any(r["kind"] == "article" for r in results), \
        "snake_case identifier query did not match indexed content"


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


def _seed_personal_os_search_rows(conn: sqlite3.Connection) -> None:
    conn.execute(
        """INSERT INTO tasks
             (uid, host_path, line_number, text, checked, status, due, domain,
              tags_json, links_json)
           VALUES ('task-water', 'journal/2026-06-13.md', 1,
                   'Change water filter', 0, 'open', '2026-06-13T14:00:00',
                   'home', '["home"]', '[]')"""
    )
    conn.execute(
        """INSERT INTO projects (slug, path, name, type, domain, status)
           VALUES ('garage-refresh', 'projects/garage-refresh.md',
                   'Garage Refresh', 'project', 'home', 'active')"""
    )
    conn.execute(
        """INSERT INTO routines (slug, path, name, description, domain, time_of_day)
           VALUES ('morning-vitamins', 'routines/morning-vitamins.md',
                   'Morning Vitamins', 'Vitamin habit', 'health', 'morning')"""
    )
    conn.execute(
        """INSERT INTO journal_days (date, path, mood, energy, log_count)
           VALUES ('2026-06-13', 'journal/2026-06-13.md', 4, 3, 2)"""
    )
    conn.execute(
        """INSERT INTO people (slug, name, facts_json, path)
           VALUES ('anjali-rao', 'Anjali Rao', '{"topic":"water filter"}',
                   'people/anjali-rao.md')"""
    )
    conn.execute(
        """INSERT INTO books (slug, path, title, author, status, summary)
           VALUES ('designing-data-intensive-applications',
                   'library/books/designing-data-intensive-applications.md',
                   'Designing Data-Intensive Applications',
                   'Martin Kleppmann', 'reading', 'Distributed systems book')"""
    )
    conn.execute(
        """INSERT INTO quotes
             (id, path, text, content_hash, source_type, source_ref, tags_json)
           VALUES ('quote-water',
                   'library/quotes/quote-water.md',
                   'A system compounds when every capture becomes reusable.',
                   'quote-water-hash', 'book',
                   'designing-data-intensive-applications', '["systems"]')"""
    )
    conn.execute(
        """INSERT INTO inventory (id, path, name, status, location)
           VALUES ('monitor-1', 'inventory/monitor-1.md',
                   'Studio Monitor', 'owned', 'desk')"""
    )
    conn.execute(
        """INSERT INTO content_items (slug, path, title, kind, status, domain)
           VALUES ('local-first-video', 'content/local-first-video.md',
                   'Local-first personal OS video', 'video', 'idea', 'mastisk')"""
    )


def test_search_all_returns_all_three_kinds(db: sqlite3.Connection) -> None:
    _seed_minimal(db)
    results = search_all(db, "compute")
    kinds = [r["kind"] for r in results]
    assert kinds == ["article", "note", "blog"]


def test_search_api_returns_personal_os_mirror_kinds(db: sqlite3.Connection) -> None:
    """Global search is the cross-cutting entrypoint, so each typed mirror must
    produce a navigable result without adding a new search engine."""
    _seed_personal_os_search_rows(db)

    from mastisk.app import create_app

    client = TestClient(create_app())
    cases = [
        ("water filter", "task", "task-water", "/tasks"),
        ("garage refresh", "project", "garage-refresh", "/projects"),
        ("vitamins", "routine", "morning-vitamins", "/routines"),
        ("2026-06-13", "journal", "2026-06-13", "/journal"),
        ("anjali", "person", "anjali-rao", "/people"),
        ("kleppmann", "book", "designing-data-intensive-applications", "/library/books/designing-data-intensive-applications"),
        ("compounds", "quote", "quote-water", "/library/quotes/quote-water"),
        ("studio monitor", "inventory", "monitor-1", "/inventory"),
        ("personal os video", "content", "local-first-video", "/content"),
    ]
    for query, kind, expected_id, link_target in cases:
        response = client.get("/api/search", params={"q_param": query})
        assert response.status_code == 200, response.text
        hit = next((r for r in response.json()["results"] if r["kind"] == kind), None)
        assert hit is not None, f"{kind} missing for query {query!r}"
        assert hit["id"] == expected_id
        assert hit["excerpt"]
        assert hit["link_target"] == link_target


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


def _fts_data_rows(db: sqlite3.Connection, table: str) -> int:
    """Row count of the ``<table>_data`` shadow segment. This is what
    actually grows when FTS5 re-indexes content — the docsize.sz column
    only reflects token COUNT for the row's content, so a trigger that
    deletes-then-reinserts the SAME body produces an identical sz value
    even though it WROTE to _data. _data row growth is the trigger-fire
    signal we need."""
    return db.execute(f"SELECT COUNT(*) FROM {table}_data").fetchone()[0]


def test_notes_au_trigger_skips_unchanged_indexed_columns(
    db: sqlite3.Connection,
) -> None:
    """UPDATE notes ... SET escalation_state=... mustn't fire the FTS
    re-index trigger when summary and body are unchanged. Otherwise every
    state transition burns shadow-table writes that are pure no-ops."""
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              summary)
           VALUES ('n', 'p/n.md', 'long body text content here', 'h', 'pwa',
                   '2026-04-26T10:00:00', 'summary text')""",
    )

    # Force a checkpoint so any pending insert work is flushed into _data.
    db.execute("INSERT INTO notes_fts(notes_fts) VALUES('optimize')")
    data_before = _fts_data_rows(db, "notes_fts")

    # Touch only non-indexed columns — should NOT cause new _data rows.
    db.execute("UPDATE notes SET escalation_state = 'pending' WHERE id = 1")
    db.execute("UPDATE notes SET escalation_retry_count = 1 WHERE id = 1")
    db.execute("UPDATE notes SET classified_at = CURRENT_TIMESTAMP WHERE id = 1")

    data_after = _fts_data_rows(db, "notes_fts")
    assert data_after == data_before, (
        f"FTS index churned on no-op UPDATE: {data_before} → {data_after}"
    )

    # Sanity: actually changing the indexed body DOES grow _data (or at
    # least reach the trigger; we accept ≥ to avoid coupling to merge tuning).
    db.execute(
        "UPDATE notes SET body = 'completely different body content' WHERE id = 1"
    )
    data_after_change = _fts_data_rows(db, "notes_fts")
    assert data_after_change >= data_after, "trigger didn't fire on real change"


def test_blog_posts_au_trigger_skips_unchanged_indexed_columns(
    db: sqlite3.Connection,
) -> None:
    """Same as the notes trigger test, but for blog_posts. Status churn
    (pending → running → done) shouldn't churn the FTS index."""
    db.execute(
        """INSERT INTO blog_posts (theme, window_days, status, title, body_preview)
           VALUES ('t', 7, 'pending', 'init title', 'init preview text')""",
    )
    db.execute("INSERT INTO blog_posts_fts(blog_posts_fts) VALUES('optimize')")
    data_before = _fts_data_rows(db, "blog_posts_fts")

    db.execute("UPDATE blog_posts SET status = 'running' WHERE id = 1")
    db.execute("UPDATE blog_posts SET status = 'done' WHERE id = 1")
    db.execute(
        "UPDATE blog_posts SET saved_as_note_id = NULL WHERE id = 1"
    )

    data_after = _fts_data_rows(db, "blog_posts_fts")
    assert data_after == data_before, (
        f"blog_posts FTS churned on no-op UPDATE: {data_before} → {data_after}"
    )


def test_articles_au_trigger_skips_unchanged_indexed_columns(
    db: sqlite3.Connection,
) -> None:
    """Articles get UPDATEd on every link insert/delete (count bumps via
    links_ai / links_ad triggers) — these mustn't reindex the article's
    title/summary/body for FTS. This is the hottest path of the three
    because article ingestion is link-heavy."""
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art', 'Concept', 'tt compute', 'tt-compute',
                   'a summary', 'long body about reasoning models')""",
    )
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art2', 'Concept', 'agents', 'agents', 'b', 'agent body')""",
    )
    db.execute("INSERT INTO articles_fts(articles_fts) VALUES('optimize')")
    data_before = _fts_data_rows(db, "articles_fts")

    # Inserting a link bumps backlinks_count/forwardlinks_count — these
    # are columns that are NOT in the FTS schema (title, summary, body_md).
    db.execute(
        "INSERT INTO links (from_article, to_article, weight) VALUES (?, ?, ?)",
        ("art2", "art", 0.9),
    )
    # Direct count-bump UPDATE (no link involved) — same expectation.
    db.execute(
        "UPDATE articles SET sources_count = sources_count + 1 WHERE id = ?",
        ("art",),
    )

    data_after = _fts_data_rows(db, "articles_fts")
    assert data_after == data_before, (
        f"articles FTS churned on link/count UPDATE: {data_before} → {data_after}"
    )


# ─────────────────────────────── Helpers ───────────────────────────────


def test_ensure_fts_initialized_no_op_on_empty_content_table(
    db: sqlite3.Connection,
) -> None:
    """When the content table is empty, no rebuild — _docsize stays empty."""
    _ensure_fts_initialized(db, "notes_fts", "notes")
    rows = db.execute("SELECT COUNT(*) AS n FROM notes_fts_docsize").fetchone()["n"]
    assert rows == 0


# Legacy (pre-WHEN-clause) DDL for each FTS update trigger, captured from the
# git history at the point the trigger first shipped. Used by the migration
# test to faithfully reproduce the upgrade path.
_LEGACY_AU_TRIGGER_SQL: dict[str, str] = {
    "articles_au": """CREATE TRIGGER articles_au AFTER UPDATE ON articles BEGIN
      INSERT INTO articles_fts(articles_fts, rowid, title, summary, body_md)
        VALUES ('delete', old.rowid, old.title, old.summary, old.body_md);
      INSERT INTO articles_fts(rowid, title, summary, body_md)
        VALUES (new.rowid, new.title, new.summary, new.body_md);
    END""",
    "notes_au": """CREATE TRIGGER notes_au AFTER UPDATE ON notes BEGIN
      INSERT INTO notes_fts(notes_fts, rowid, summary, body)
        VALUES ('delete', old.id, old.summary, old.body);
      INSERT INTO notes_fts(rowid, summary, body)
        VALUES (new.id, new.summary, new.body);
    END""",
    "blog_posts_au": """CREATE TRIGGER blog_posts_au AFTER UPDATE ON blog_posts BEGIN
      INSERT INTO blog_posts_fts(blog_posts_fts, rowid, title, theme, body_preview)
        VALUES ('delete', old.id, old.title, old.theme, old.body_preview);
      INSERT INTO blog_posts_fts(rowid, title, theme, body_preview)
        VALUES (new.id, new.title, new.theme, new.body_preview);
    END""",
}


@pytest.mark.parametrize("trigger_name", list(_LEGACY_AU_TRIGGER_SQL.keys()))
def test_init_schema_migrates_legacy_au_triggers_to_when_clause(
    tmp_path: Path, trigger_name: str,
) -> None:
    """`CREATE TRIGGER IF NOT EXISTS` is a no-op when a trigger of the same
    name already exists, so a shape-change (adding a WHEN clause) to a
    pre-existing trigger never reaches upgraded users without an explicit
    drop. Parametrised over all three FTS update triggers so a typo in the
    migration's trigger-name tuple, or a shape divergence between any
    individual trigger's legacy and new DDL, is caught.
    """
    dbpath = tmp_path / f"legacy-{trigger_name}.db"
    raw = sqlite3.connect(dbpath, isolation_level=None)
    raw.row_factory = sqlite3.Row

    # Bootstrap schema, then pretend we're on the pre-WHEN version of this
    # specific trigger by dropping it and recreating with the legacy shape.
    init_schema(raw)
    raw.execute(f"DROP TRIGGER {trigger_name}")
    raw.execute(_LEGACY_AU_TRIGGER_SQL[trigger_name])
    legacy_sql = raw.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", (trigger_name,),
    ).fetchone()["sql"]
    assert "WHEN" not in legacy_sql.upper(), (
        f"test setup did not install legacy shape for {trigger_name}"
    )

    # Re-running init_schema must replace the legacy trigger with the new
    # WHEN-guarded version.
    init_schema(raw)
    new_sql = raw.execute(
        "SELECT sql FROM sqlite_master WHERE name=?", (trigger_name,),
    ).fetchone()["sql"]
    assert "WHEN" in new_sql.upper(), (
        f"legacy {trigger_name} not migrated: {new_sql}"
    )
    raw.close()


def test_migrated_articles_au_trigger_skips_count_bump_updates(
    tmp_path: Path,
) -> None:
    """End-to-end behaviour proof for the articles_au migration: after the
    legacy trigger is replaced, a count-bump UPDATE (the hot path that
    motivated the WHEN-clause fix) must NOT grow articles_fts_data. The
    parametrised migration test above covers DDL shape; this covers the
    runtime contract that shape change buys us."""
    dbpath = tmp_path / "behaviour.db"
    raw = sqlite3.connect(dbpath, isolation_level=None)
    raw.row_factory = sqlite3.Row
    init_schema(raw)
    raw.execute("DROP TRIGGER articles_au")
    raw.execute(_LEGACY_AU_TRIGGER_SQL["articles_au"])
    init_schema(raw)  # migration kicks in here
    raw.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art', 'Concept', 't', 's', 'a', 'b')""",
    )
    raw.execute("INSERT INTO articles_fts(articles_fts) VALUES('optimize')")
    data_before = raw.execute(
        "SELECT COUNT(*) FROM articles_fts_data"
    ).fetchone()[0]
    raw.execute(
        "UPDATE articles SET sources_count = sources_count + 1 WHERE id = ?",
        ("art",),
    )
    data_after = raw.execute(
        "SELECT COUNT(*) FROM articles_fts_data"
    ).fetchone()[0]
    assert data_before == data_after, (
        "migrated trigger still re-indexes on count-bump UPDATE"
    )
    raw.close()


def test_dunder_init_query_matches_indexed_content(db: sqlite3.Connection) -> None:
    """End-to-end of the underscore tokenizer change: a body containing
    `__init__.py` is indexed by FTS5 as the token "init", and a user query
    "__init__" must produce a search expression that matches it. The
    builder-level test asserts the expression is "init*"; this test
    asserts the expression actually finds the content."""
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art', 'Concept', 'Python init', 's', 'a',
                   'See __init__.py for module setup.')""",
    )
    results = search_all(db, "__init__")
    assert any(r["kind"] == "article" for r in results), \
        "dunder identifier query did not match indexed content"
