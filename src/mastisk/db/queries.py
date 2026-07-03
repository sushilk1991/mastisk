"""Database access — small, direct, no ORM.

Sync (sqlite3) for most reads; async helpers used only where FastAPI routes really need to yield.
Mastisk is single-user; a single sqlite3 connection with WAL is fine.
"""
from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from mastisk.paths import db_path

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"
MAX_GENERATED_ARTICLE_TITLE_CHARS = 70


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def clamp_generated_title(
    title: str, *, max_chars: int = MAX_GENERATED_ARTICLE_TITLE_CHARS
) -> str:
    """Cap generated article titles at a word boundary before persistence."""
    clean = str(title or "").strip()
    if len(clean) <= max_chars:
        return clean
    suffix = "..."
    hard_limit = max_chars - len(suffix)
    clipped = clean[:hard_limit].rstrip()
    boundary = clipped.rfind(" ")
    if boundary >= max(24, hard_limit // 2):
        clipped = clipped[:boundary].rstrip()
    return clipped.rstrip(" ,;:-.") + suffix


def init_schema(conn: sqlite3.Connection | None = None) -> None:
    own = conn is None
    c = conn or connect()
    try:
        # Migrate legacy FTS triggers BEFORE the schema script runs. The CREATE
        # TRIGGER IF NOT EXISTS lines in schema.sql are a no-op when a trigger
        # of the same name already exists, so a shape-change to an existing
        # trigger (e.g. adding a WHEN clause) wouldn't reach upgraded users
        # without an explicit drop. Dropping happens here; the executescript
        # below recreates them with the current shape.
        _migrate_legacy_fts_triggers(c)
        c.executescript(_SCHEMA_PATH.read_text())
        _run_migrations(c)
    finally:
        if own:
            c.close()


_FTS_UPDATE_TRIGGERS: tuple[str, ...] = (
    "articles_au",
    "notes_au",
    "blog_posts_au",
)


def _migrate_legacy_fts_triggers(conn: sqlite3.Connection) -> None:
    """Drop any FTS UPDATE trigger whose installed DDL is missing the
    `WHEN old.col IS NOT new.col` guard.

    Without the guard the trigger fires on every UPDATE — including
    count-bump UPDATEs from links_ai/links_ad/article_sources_ai and
    state-transition UPDATEs from the escalator and blog writer — and
    each fire emits a delete+reinsert into the FTS shadow tables for
    content that didn't actually change. Adding the guard via the
    schema's CREATE TRIGGER IF NOT EXISTS doesn't reach existing DBs
    (the trigger already exists with the old shape), so we drop the
    legacy ones here and let executescript recreate them.

    Safe on fresh installs: the SELECT returns no row, the loop is a
    no-op, and the schema script then creates triggers with the current
    shape on first run.
    """
    for trigger in _FTS_UPDATE_TRIGGERS:
        row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone()
        if not row:
            continue
        sql = row["sql"] or ""
        # Match the keyword as a whole word so we don't false-positive on
        # something like "WHENever" inside a comment. Case-insensitive
        # because schema.sql could in theory be reformatted.
        import re
        if re.search(r"\bWHEN\b", sql, flags=re.IGNORECASE):
            continue
        conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")


def _add_column_if_missing(
    conn: sqlite3.Connection, table: str, column: str, decl: str
) -> None:
    """Idempotent ALTER TABLE ... ADD COLUMN. SQLite has no IF NOT EXISTS for
    ADD COLUMN, so we peek at the current column set first. Cheap — pragma
    returns at most a dozen rows."""
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _run_migrations(conn: sqlite3.Connection) -> None:
    """Idempotent column additions for pre-existing DBs. CREATE TABLE IF NOT
    EXISTS handles fresh installs; this handles upgrade-in-place."""
    _add_column_if_missing(conn, "articles", "hero_image_url", "TEXT")
    _ensure_editing_locks_schema(conn)
    _add_column_if_missing(conn, "sources", "hero_image_url", "TEXT")
    _add_column_if_missing(conn, "sources", "media_json", "TEXT")
    _add_column_if_missing(conn, "sources", "duration_sec", "INTEGER")
    _add_column_if_missing(conn, "sources", "feed_url", "TEXT")
    _add_column_if_missing(conn, "notes", "transcript_anchor_json", "TEXT")
    _add_column_if_missing(conn, "tasks", "needs_triage", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "tasks", "reminder_lead_minutes", "INTEGER")
    _add_column_if_missing(conn, "tasks", "no_reminder", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "tasks", "recurrence_materialized_key", "TEXT")
    _add_column_if_missing(conn, "tasks", "recurrence_unparsed", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "tasks", "staleness_days", "INTEGER")
    _add_column_if_missing(conn, "tasks", "slipping_muted_until", "TEXT")
    _add_column_if_missing(conn, "tasks", "slipping_muted", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "projects", "staleness_days", "INTEGER")
    _add_column_if_missing(conn, "projects", "slipping_muted_until", "TEXT")
    _add_column_if_missing(conn, "projects", "slipping_muted", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "reminders", "attempts", "INTEGER NOT NULL DEFAULT 0")
    _add_column_if_missing(conn, "reminders", "next_attempt_at", "DATETIME")
    _add_column_if_missing(conn, "reminders", "last_error", "TEXT")
    _add_column_if_missing(conn, "reminders", "title", "TEXT")
    _add_column_if_missing(conn, "reminders", "body", "TEXT")
    _add_column_if_missing(conn, "reminders", "url", "TEXT")
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_routine_missed_date
           ON reminders(kind, entity_id)
           WHERE kind = 'routine_missed' AND deleted_at IS NULL"""
    )
    _add_column_if_missing(conn, "people", "follow_up_at", "DATETIME")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS interactions (
             person_slug TEXT NOT NULL REFERENCES people(slug) ON DELETE CASCADE,
             ts          TEXT NOT NULL,
             text        TEXT NOT NULL,
             created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
             PRIMARY KEY(person_slug, ts, text)
           )"""
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_followup_entity
           ON reminders(kind, entity_id)
           WHERE kind = 'followup' AND deleted_at IS NULL"""
    )
    _ensure_library_schema(conn)
    _ensure_inventory_schema(conn)
    _ensure_agent_studio_schema(conn)
    _ensure_content_schema(conn)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS calendar_events (
             id          TEXT NOT NULL,
             calendar_id TEXT NOT NULL,
             summary     TEXT NOT NULL DEFAULT '',
             start       TEXT NOT NULL,
             end         TEXT NOT NULL,
             all_day     INTEGER NOT NULL DEFAULT 0,
             location    TEXT,
             status      TEXT,
             updated_at  TEXT,
             synced_at   DATETIME NOT NULL,
             PRIMARY KEY(calendar_id, id)
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_calendar_events_start
           ON calendar_events(start, end)"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_calendar_events_calendar
           ON calendar_events(calendar_id)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS calendar_state (
             id             INTEGER PRIMARY KEY CHECK(id = 1),
             status         TEXT NOT NULL,
             last_synced_at TEXT,
             error          TEXT,
             last_error     TEXT,
             last_error_at  TEXT,
             updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    _add_column_if_missing(conn, "calendar_state", "last_error", "TEXT")
    _add_column_if_missing(conn, "calendar_state", "last_error_at", "TEXT")
    _add_column_if_missing(
        conn, "articles", "source_note_id",
        "INTEGER REFERENCES notes(id) ON DELETE SET NULL",
    )
    _add_column_if_missing(conn, "repos", "source_type", "TEXT NOT NULL DEFAULT 'github'")
    _add_column_if_missing(conn, "repos", "local_path", "TEXT")
    # Backfill the FTS indexes for pre-existing DBs. The CREATE VIRTUAL TABLE in
    # schema.sql is idempotent, but the AFTER INSERT triggers only fire on rows
    # written *after* the index existed — so any notes/blog_posts that were
    # already in the table are invisible to FTS until we ask it to rebuild.
    _ensure_fts_initialized(conn, "notes_fts", "notes")
    _ensure_fts_initialized(conn, "blog_posts_fts", "blog_posts")
    _add_column_if_missing(conn, "blog_posts", "content_slug", "TEXT")


def _ensure_editing_locks_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute("PRAGMA table_info(editing_locks)").fetchall()
    columns = {row["name"] for row in rows}
    pk_columns = [
        row["name"] for row in sorted(rows, key=lambda row: row["pk"]) if row["pk"]
    ]
    if rows and (
        columns != {"path", "token", "locked_at", "heartbeat_at"}
        or pk_columns != ["path", "token"]
    ):
        # Editor locks are ephemeral advisory rows. Rebuilding avoids carrying
        # path-only locks that cannot prove tab ownership into the token schema.
        conn.execute("DROP TABLE IF EXISTS editing_locks")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS editing_locks (
             path         TEXT NOT NULL,
             token        TEXT NOT NULL,
             locked_at    DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
             heartbeat_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
             PRIMARY KEY(path, token)
           )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_editing_locks_path ON editing_locks(path)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_editing_locks_heartbeat ON editing_locks(heartbeat_at)"
    )


def _ensure_library_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS books (
             slug       TEXT PRIMARY KEY,
             path       TEXT UNIQUE NOT NULL,
             title      TEXT NOT NULL,
             author     TEXT,
             cover_url  TEXT,
             status     TEXT NOT NULL DEFAULT 'want',
             format     TEXT,
             started    TEXT,
             finished   TEXT,
             rating     INTEGER,
             isbn       TEXT,
             summary    TEXT,
             deleted_at DATETIME,
             created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
             updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    for column, decl in (
        ("cover_url", "TEXT"),
        ("format", "TEXT"),
        ("started", "TEXT"),
        ("finished", "TEXT"),
        ("rating", "INTEGER"),
        ("isbn", "TEXT"),
        ("summary", "TEXT"),
    ):
        _add_column_if_missing(conn, "books", column, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_status ON books(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_books_title ON books(title)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_books_active ON books(slug) WHERE deleted_at IS NULL"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS book_highlights (
             id           INTEGER PRIMARY KEY AUTOINCREMENT,
             book_slug    TEXT NOT NULL REFERENCES books(slug) ON DELETE CASCADE,
             position     INTEGER NOT NULL,
             text         TEXT NOT NULL,
             content_hash TEXT NOT NULL,
             quote_id     TEXT,
             deleted_at   DATETIME,
             created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
             updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
             UNIQUE(book_slug, content_hash)
           )"""
    )
    _add_column_if_missing(conn, "book_highlights", "deleted_at", "DATETIME")
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_book_highlights_book
           ON book_highlights(book_slug, position)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS quotes (
             id           TEXT PRIMARY KEY,
             path         TEXT UNIQUE NOT NULL,
             text         TEXT NOT NULL,
             content_hash TEXT NOT NULL,
             source_type  TEXT NOT NULL,
             source_ref   TEXT,
             tags_json    TEXT NOT NULL DEFAULT '[]',
             deleted_at   DATETIME,
             created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
             updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_quotes_source ON quotes(source_type, source_ref)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_quotes_active ON quotes(id) WHERE deleted_at IS NULL"
    )
    conn.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS idx_quotes_source_hash
           ON quotes(source_type, COALESCE(source_ref, ''), content_hash)
           WHERE deleted_at IS NULL"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS quote_thoughts (
             quote_id   TEXT NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
             ts         TEXT NOT NULL,
             text       TEXT NOT NULL,
             created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
             PRIMARY KEY(quote_id, ts, text)
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_quote_thoughts_quote_ts
           ON quote_thoughts(quote_id, ts)"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS kindle_import_review (
             id             INTEGER PRIMARY KEY AUTOINCREMENT,
             raw_hash       TEXT UNIQUE NOT NULL,
             raw_block      TEXT NOT NULL,
             reason         TEXT NOT NULL,
             parsed_title   TEXT,
             parsed_author  TEXT,
             parsed_content TEXT,
             status         TEXT NOT NULL DEFAULT 'open',
             quote_id       TEXT,
             created_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
             resolved_at    DATETIME
           )"""
    )
    conn.execute(
        """CREATE INDEX IF NOT EXISTS idx_kindle_import_review_status
           ON kindle_import_review(status, created_at)"""
    )


def _ensure_inventory_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS inventory (
             id         TEXT PRIMARY KEY,
             path       TEXT UNIQUE NOT NULL,
             name       TEXT NOT NULL,
             acquired   TEXT,
             value      REAL,
             status     TEXT NOT NULL DEFAULT 'owned',
             location   TEXT,
             photo      TEXT,
             deleted_at DATETIME,
             created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
             updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    for column, decl in (
        ("acquired", "TEXT"),
        ("value", "REAL"),
        ("status", "TEXT NOT NULL DEFAULT 'owned'"),
        ("location", "TEXT"),
        ("photo", "TEXT"),
        ("deleted_at", "DATETIME"),
    ):
        _add_column_if_missing(conn, "inventory", column, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_status ON inventory(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_inventory_location ON inventory(location)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inventory_active ON inventory(id) WHERE deleted_at IS NULL"
    )


def _ensure_agent_studio_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_profiles (
             agent_id            TEXT PRIMARY KEY,
             path                TEXT UNIQUE NOT NULL,
             enabled             INTEGER NOT NULL DEFAULT 1,
             model               TEXT,
             skills_json         TEXT NOT NULL DEFAULT '[]',
             prompt_override     TEXT,
             slot_overrides_json TEXT NOT NULL DEFAULT '{}',
             invalid             INTEGER NOT NULL DEFAULT 0,
             invalid_reason      TEXT,
             invalid_slots_json  TEXT NOT NULL DEFAULT '{}',
             deleted_at          DATETIME,
             created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
             updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    for column, decl in (
        ("model", "TEXT"),
        ("skills_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("prompt_override", "TEXT"),
        ("slot_overrides_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("invalid", "INTEGER NOT NULL DEFAULT 0"),
        ("invalid_reason", "TEXT"),
        ("invalid_slots_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("deleted_at", "DATETIME"),
    ):
        _add_column_if_missing(conn, "agent_profiles", column, decl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_profiles_active ON agent_profiles(agent_id) WHERE deleted_at IS NULL"
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_skills (
             slug        TEXT PRIMARY KEY,
             path        TEXT UNIQUE NOT NULL,
             name        TEXT NOT NULL,
             description TEXT,
             tags_json   TEXT NOT NULL DEFAULT '[]',
             body        TEXT NOT NULL DEFAULT '',
             invalid     INTEGER NOT NULL DEFAULT 0,
             invalid_reason TEXT,
             deleted_at  DATETIME,
             created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
             updated_at  DATETIME DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    for column, decl in (
        ("description", "TEXT"),
        ("tags_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("body", "TEXT NOT NULL DEFAULT ''"),
        ("invalid", "INTEGER NOT NULL DEFAULT 0"),
        ("invalid_reason", "TEXT"),
        ("deleted_at", "DATETIME"),
    ):
        _add_column_if_missing(conn, "agent_skills", column, decl)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_skills_active ON agent_skills(slug) WHERE deleted_at IS NULL"
    )


def _ensure_content_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS content_items (
             slug         TEXT PRIMARY KEY,
             path         TEXT UNIQUE NOT NULL,
             title        TEXT NOT NULL,
             kind         TEXT NOT NULL,
             status       TEXT NOT NULL DEFAULT 'idea',
             domain       TEXT,
             channel      TEXT,
             url          TEXT,
             publish_date TEXT,
             needs_triage INTEGER NOT NULL DEFAULT 0,
             deleted_at   DATETIME,
             created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
             updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
           )"""
    )
    for column, decl in (
        ("channel", "TEXT"),
        ("url", "TEXT"),
        ("publish_date", "TEXT"),
        ("needs_triage", "INTEGER NOT NULL DEFAULT 0"),
        ("deleted_at", "DATETIME"),
    ):
        _add_column_if_missing(conn, "content_items", column, decl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_content_items_kind ON content_items(kind)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_content_items_status ON content_items(status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_content_items_domain ON content_items(domain)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_content_items_active ON content_items(slug) WHERE deleted_at IS NULL"
    )


def _ensure_fts_initialized(
    conn: sqlite3.Connection, fts_table: str, content_table: str
) -> None:
    """If the FTS5 index is empty but the content table has rows, rebuild it.

    Why we can't just check ``SELECT 1 FROM <fts_table> LIMIT 1``: FTS5
    external-content tables project rows from the content table, so a SELECT
    returns one row per content row even when the FTS *index* is unbuilt.

    The semantic signal we need is "does the FTS index know about any docs?"
    — that lives in the ``<fts_table>_docsize`` shadow table, which gets one
    row per indexed document. Empty docsize ⇔ unbuilt index, regardless of
    SQLite version (other shadow tables like ``_data`` carry config rows
    whose count varies across SQLite builds).

    No-op when the content table is empty (fresh install, no rows to index)
    or when the index is already populated (steady state — triggers maintain
    it). Runs at most once per FTS table per daemon's lifetime, on the boot
    that first sees a content table with rows but no built index.
    """
    has_content = conn.execute(
        f"SELECT 1 FROM {content_table} LIMIT 1"
    ).fetchone() is not None
    if not has_content:
        return
    # docsize has one row per indexed doc; any row means the rebuild has run.
    indexed = conn.execute(
        f"SELECT 1 FROM {fts_table}_docsize LIMIT 1"
    ).fetchone() is not None
    if indexed:
        return
    # Identifiers are hardcoded above — interpolation is safe.
    conn.execute(f"INSERT INTO {fts_table}({fts_table}) VALUES ('rebuild')")


@contextmanager
def txn(conn: sqlite3.Connection):
    conn.execute("BEGIN")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


# ─────────────────────────────── Articles ───────────────────────────────

def list_articles(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict]:
    q = "SELECT * FROM articles"
    params: list[Any] = []
    if kind:
        q += " WHERE kind = ?"
        params.append(kind)
    q += " ORDER BY updated_at DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, params)]


def get_article(conn: sqlite3.Connection, article_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    d["aka"] = json.loads(d.pop("aka_json") or "[]")
    d["sections"] = [
        dict(r)
        for r in conn.execute(
            "SELECT idx, heading AS h, body, kind FROM article_sections WHERE article_id = ? ORDER BY idx",
            (article_id,),
        )
    ]
    d["related"] = [
        {"id": r["to_article"], "label": r["title"], "weight": r["weight"]}
        for r in conn.execute(
            """SELECT links.to_article, links.weight, articles.title
               FROM links JOIN articles ON articles.id = links.to_article
               WHERE links.from_article = ?
               ORDER BY links.weight DESC LIMIT 20""",
            (article_id,),
        )
    ]
    # Inbound links — other articles that reference this one. Snippet may be
    # empty when the row was inserted by the linter's graph-repair pass, which
    # doesn't have access to the quoted context.
    d["backlinkList"] = [
        {
            "id": r["from_article"],
            "title": r["title"],
            "snippet": r["snippet"] or "",
            "weight": r["weight"],
        }
        for r in conn.execute(
            """SELECT links.from_article, links.weight, links.snippet, articles.title
               FROM links JOIN articles ON articles.id = links.from_article
               WHERE links.to_article = ?
               ORDER BY links.weight DESC, articles.updated_at DESC LIMIT 20""",
            (article_id,),
        )
    ]
    d["sourceList"] = [
        dict(r)
        for r in conn.execute(
            """SELECT sources.kind, sources.title, sources.url, sources.published_at AS date
               FROM article_sources JOIN sources ON sources.id = article_sources.source_id
               WHERE article_sources.article_id = ?""",
            (article_id,),
        )
    ]
    # camelCase a couple for the frontend
    d["readingTime"] = f"{d.pop('reading_minutes', 3)} min"
    d["sources"] = d.pop("sources_count", 0)
    d["backlinks"] = d.pop("backlinks_count", 0)
    d["forwardlinks"] = d.pop("forwardlinks_count", 0)
    d["heroImageUrl"] = d.pop("hero_image_url", None)
    # Inline media: the Compiler-time copy lives on sources; for now we union
    # the media_json from all sources attached to this article.
    media: list[dict] = []
    for r in conn.execute(
        """SELECT s.media_json FROM article_sources a_s
           JOIN sources s ON s.id = a_s.source_id
           WHERE a_s.article_id = ?""",
        (article_id,),
    ):
        raw = r["media_json"]
        if not raw:
            continue
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            for m in parsed:
                if isinstance(m, dict) and m.get("src"):
                    media.append(m)
    d["media"] = media
    return d


def upsert_article(conn: sqlite3.Connection, art: dict) -> None:
    # hero_image_url uses COALESCE-on-update so a recompile doesn't wipe a
    # hero set by an earlier ingest pass. Pass an empty string (not None) to
    # explicitly clear it if that's ever needed.
    title = art["title"]
    if art.get("updated_by") in {"Compiler", "Synthesizer"}:
        title = clamp_generated_title(title)
    conn.execute(
        """INSERT INTO articles (id, kind, title, slug, aka_json, summary, body_md,
                                 confidence, reading_minutes, updated_by, vault_path,
                                 hero_image_url)
           VALUES (:id, :kind, :title, :slug, :aka_json, :summary, :body_md,
                   :confidence, :reading_minutes, :updated_by, :vault_path,
                   :hero_image_url)
           ON CONFLICT(id) DO UPDATE SET
             kind=excluded.kind, title=excluded.title, slug=excluded.slug,
             aka_json=excluded.aka_json, summary=excluded.summary, body_md=excluded.body_md,
             confidence=excluded.confidence, reading_minutes=excluded.reading_minutes,
             updated_by=excluded.updated_by, vault_path=excluded.vault_path,
             hero_image_url=COALESCE(excluded.hero_image_url, articles.hero_image_url),
             updated_at=CURRENT_TIMESTAMP""",
        {
            "id": art["id"],
            "kind": art["kind"],
            "title": title,
            "slug": art.get("slug", art["id"]),
            "aka_json": json.dumps(art.get("aka", [])),
            "summary": art.get("summary", ""),
            "body_md": art.get("body_md", ""),
            "confidence": art.get("confidence", 0.5),
            "reading_minutes": art.get("reading_minutes", 3),
            "updated_by": art.get("updated_by"),
            "vault_path": art.get("vault_path"),
            "hero_image_url": art.get("hero_image_url"),
        },
    )


def replace_sections(conn: sqlite3.Connection, article_id: str, sections: Iterable[dict]) -> None:
    conn.execute("DELETE FROM article_sections WHERE article_id = ?", (article_id,))
    for i, s in enumerate(sections):
        conn.execute(
            "INSERT INTO article_sections (article_id, idx, heading, body, kind) VALUES (?, ?, ?, ?, ?)",
            (article_id, i, s.get("h") or s.get("heading", ""), s.get("body", ""), s.get("kind", "section")),
        )


def set_related(conn: sqlite3.Connection, article_id: str, links: Iterable[dict]) -> None:
    """Replace outgoing links for an article.

    Silently drops link targets that don't exist yet — the Compiler often
    references sibling articles that haven't been written on this pass. A
    scheduled backfill reconciles these once the graph catches up.
    """
    conn.execute("DELETE FROM links WHERE from_article = ?", (article_id,))
    for r in links:
        target = r.get("id")
        if not target or target == article_id:
            continue
        exists = conn.execute("SELECT 1 FROM articles WHERE id = ?", (target,)).fetchone()
        if not exists:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO links (from_article, to_article, weight, snippet) VALUES (?, ?, ?, ?)",
            (article_id, target, r.get("weight", 0.5), r.get("snippet")),
        )


# ─────────────────────────────── Podcasts (article view of audio sources) ───────────────────────────────

def list_podcast_articles(
    conn: sqlite3.Connection,
    *,
    limit: int = 100,
) -> list[dict]:
    """List articles whose attached source is a podcast or YouTube video.

    Returns one row per (article, source) pair — typically 1:1 since the
    Listener writes a single source per ingest. Ordered by source.published_at
    DESC (when present) then articles.updated_at DESC. Used by the
    /api/podcasts list view.
    """
    rows = conn.execute(
        """SELECT
             a.id              AS article_id,
             a.title           AS article_title,
             a.summary         AS article_summary,
             a.kind            AS article_kind,
             a.confidence      AS article_confidence,
             a.updated_at      AS article_updated_at,
             a.hero_image_url  AS article_hero,
             s.id              AS source_id,
             s.kind            AS source_kind,
             s.title           AS source_title,
             s.url             AS source_url,
             s.author          AS source_author,
             s.published_at    AS source_published_at,
             s.duration_sec    AS source_duration_sec,
             s.feed_url        AS source_feed_url,
             s.hero_image_url  AS source_hero
           FROM article_sources a_s
           JOIN sources  s ON s.id = a_s.source_id
           JOIN articles a ON a.id = a_s.article_id
           WHERE s.kind IN ('podcast', 'youtube')
           ORDER BY COALESCE(s.published_at, a.updated_at) DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_podcast_view(
    conn: sqlite3.Connection, article_id: str
) -> dict | None:
    """Return the joined article + source + transcript payload for the
    podcast detail page. Returns None if the article doesn't exist or has
    no podcast/youtube source attached.

    The transcript text is read from sources.raw_path. When
    source_transcript_segments has rows for this source, they are returned
    too (Phase 2 — the segment-by-segment renderer). When absent, the caller
    falls back to displaying the raw text as a single block.
    """
    article = get_article(conn, article_id)
    if not article:
        return None
    src = conn.execute(
        """SELECT s.* FROM article_sources a_s
           JOIN sources s ON s.id = a_s.source_id
           WHERE a_s.article_id = ? AND s.kind IN ('podcast', 'youtube')
           ORDER BY s.fetched_at DESC LIMIT 1""",
        (article_id,),
    ).fetchone()
    if not src:
        return None
    src_d = dict(src)

    # Read transcript text from raw_path. The Listener writes a structured
    # markdown blob there (header + AI extraction + transcript at the bottom);
    # we strip the metadata header so the UI shows just the transcript proper.
    transcript_text = ""
    raw_path = src_d.get("raw_path")
    if raw_path:
        from pathlib import Path
        try:
            full = Path(raw_path).read_text(encoding="utf-8")
            # The Listener sentinel is "## Transcript\n" — everything after
            # is the verbatim whisper output. Falls back to the whole file
            # if the sentinel isn't present (older raw files predate the
            # current header format).
            marker = "## Transcript\n"
            idx = full.find(marker)
            transcript_text = full[idx + len(marker):].strip() if idx >= 0 else full.strip()
        except OSError:
            transcript_text = ""

    segments = [
        dict(r) for r in conn.execute(
            """SELECT idx, start_sec, end_sec, text
               FROM source_transcript_segments
               WHERE source_id = ? ORDER BY idx""",
            (src_d["id"],),
        )
    ]

    # Notes anchored to segments of this source (Phase 2). Joined to the
    # transcript_anchor_json column on notes; only returns notes whose anchor
    # points at this source.
    anchored_notes = [
        dict(r) for r in conn.execute(
            """SELECT id, body, classification, summary, created_at,
                      transcript_anchor_json
               FROM notes
               WHERE deleted_at IS NULL
                 AND transcript_anchor_json IS NOT NULL
                 AND json_extract(transcript_anchor_json, '$.source_id') = ?
               ORDER BY created_at DESC""",
            (src_d["id"],),
        )
    ]
    for n in anchored_notes:
        try:
            n["transcript_anchor"] = json.loads(n.pop("transcript_anchor_json") or "{}")
        except (TypeError, ValueError):
            n["transcript_anchor"] = {}

    return {
        "article": article,
        "source": {
            "id": src_d["id"],
            "kind": src_d["kind"],
            "title": src_d.get("title") or "",
            "url": src_d.get("url") or "",
            "author": src_d.get("author") or "",
            "published_at": src_d.get("published_at"),
            "duration_sec": src_d.get("duration_sec"),
            "feed_url": src_d.get("feed_url"),
            "hero_image_url": src_d.get("hero_image_url"),
        },
        "transcript_text": transcript_text,
        "segments": segments,
        "anchored_notes": anchored_notes,
    }


# ─────────────────────────────── Vault / sidebar ───────────────────────────────

def vault_tree(conn: sqlite3.Connection) -> list[dict]:
    """Return the sidebar vault tree — mirrors the shape the React shell expects."""
    def folder(label: str, kind: str, glyph: str, *, hot_ids: set[str] = frozenset()) -> dict:
        rows = [
            {
                "kind": "page",
                "id": r["id"],
                "label": r["title"],
                "glyph": glyph,
                "hot": r["id"] in hot_ids,
            }
            for r in conn.execute(
                "SELECT id, title FROM articles WHERE kind = ? ORDER BY updated_at DESC LIMIT 50",
                (kind,),
            )
        ]
        count = conn.execute("SELECT COUNT(*) AS n FROM articles WHERE kind = ?", (kind,)).fetchone()["n"]
        return {"kind": "folder", "label": label, "count": count, "children": rows}

    # "Hot" = >1 signal in last 3 days
    hot = {
        r["article_id"]
        for r in conn.execute(
            "SELECT article_id FROM signals WHERE ts >= datetime('now', '-3 days') GROUP BY article_id HAVING COUNT(*) > 1"
        )
    }

    def badge_if_nonzero(n: int) -> str | None:
        return str(n) if n > 0 else None

    digest_n = conn.execute(
        "SELECT COUNT(*) AS n FROM articles WHERE DATE(updated_at) = DATE('now')"
    ).fetchone()["n"]
    queue_n = conn.execute("SELECT COUNT(*) AS n FROM jobs WHERE status='queued'").fetchone()["n"]
    open_q_n = conn.execute(
        "SELECT COUNT(*) AS n FROM article_sections WHERE kind='open'"
    ).fetchone()["n"]
    any_feed = conn.execute("SELECT 1 FROM feed LIMIT 1").fetchone() is not None

    return [
        {"kind": "section", "label": "Today"},
        {"kind": "page", "id": "digest", "label": "Daily Digest", "glyph": "◐", "badge": badge_if_nonzero(digest_n)},
        {"kind": "page", "id": "queue", "label": "Reading queue", "glyph": "≡", "badge": badge_if_nonzero(queue_n)},
        {"kind": "page", "id": "open_questions", "label": "Open questions", "glyph": "?", "badge": badge_if_nonzero(open_q_n)},
        {"kind": "page", "id": "feed", "label": "Agent feed", "glyph": "◇", "badge": "live" if any_feed else None},
        {"kind": "section", "label": "Wiki"},
        folder("Concepts", "Concept", "▲", hot_ids=hot),
        folder("Entities", "Entity", "●"),
        folder("Sources", "Source", "◊"),
        folder("Synthesis", "Synthesis", "✦"),
        {"kind": "section", "label": "System"},
        {"kind": "page", "id": "graph", "label": "Graph view", "glyph": "✱"},
        {"kind": "page", "id": "agents", "label": "Agents", "glyph": "◯"},
        {"kind": "page", "id": "ingest", "label": "Import", "glyph": "↧"},
        {"kind": "page", "id": "lint", "label": "System health", "glyph": "✓"},
        {"kind": "page", "id": "settings", "label": "Settings", "glyph": "⚙"},
    ]


def user_info(conn: sqlite3.Connection) -> dict:
    """Pull a personalized label from identity.md + live counts for the sidebar pill."""
    import getpass
    import re

    from mastisk.paths import self_dir

    # Name: prefer first bullet under `## Role` in identity.md, else OS user, else "You".
    name = (getpass.getuser() or "you").capitalize()
    p = self_dir() / "identity.md"
    if p.exists():
        text = p.read_text()
        m = re.search(r"^##\s*Role\s*\n([^\n]*\n){0,8}", text, flags=re.M)
        if m:
            for line in m.group(0).splitlines()[1:]:
                line = line.strip()
                if not line or line.startswith("#") or line.startswith("("):
                    continue
                # "- Alice — engineer..." → "Alice"
                cleaned = re.sub(r"^[-*•\d.\s]+", "", line).strip()
                cleaned = re.split(r"[—\-–|,]", cleaned, maxsplit=1)[0].strip()
                cleaned = re.sub(r"\*\*", "", cleaned)
                if cleaned and len(cleaned) < 40:
                    name = cleaned
                    break

    pages   = conn.execute("SELECT COUNT(*) AS n FROM articles").fetchone()["n"]
    sources = conn.execute("SELECT COUNT(*) AS n FROM sources").fetchone()["n"]
    feeds   = conn.execute("SELECT COUNT(*) AS n FROM rss_feeds WHERE enabled=1").fetchone()["n"]

    initials = "".join(w[0] for w in name.split()[:2]).upper() or "—"

    return {
        "name": name,
        "initials": initials,
        "stats": {"pages": pages, "sources": sources, "feeds": feeds},
    }


def pinned_list(conn: sqlite3.Connection) -> list[dict]:
    return [
        {"id": r["id"], "label": r["title"]}
        for r in conn.execute(
            """SELECT articles.id, articles.title
               FROM pinned JOIN articles ON articles.id = pinned.article_id
               ORDER BY pinned.pinned_at DESC LIMIT 10"""
        )
    ]


# ─────────────────────────────── Feed / signals ───────────────────────────────

def append_feed(
    conn: sqlite3.Connection, *, agent: str, verb: str, obj: str,
    kind: str | None = None, touched_pages: int = 0, payload: dict | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO feed (agent, verb, obj, kind, touched_pages, payload_json) VALUES (?, ?, ?, ?, ?, ?)",
        (agent, verb, obj, kind, touched_pages, json.dumps(payload) if payload else None),
    )
    return cur.lastrowid or 0


def recent_feed(conn: sqlite3.Connection, *, limit: int = 50) -> list[dict]:
    rows = [dict(r) for r in conn.execute("SELECT * FROM feed ORDER BY ts DESC LIMIT ?", (limit,))]
    return [_feed_row_for_ui(r) for r in rows]


def _feed_row_for_ui(r: dict) -> dict:
    ts = datetime.fromisoformat(r["ts"]) if isinstance(r["ts"], str) else r["ts"]
    delta = datetime.utcnow() - ts
    if delta.total_seconds() < 60:
        t = f"{int(delta.total_seconds())}s"
    elif delta.total_seconds() < 3600:
        t = f"{int(delta.total_seconds() / 60)}m"
    elif delta.total_seconds() < 86400:
        t = f"{int(delta.total_seconds() / 3600)}h"
    else:
        t = f"{int(delta.total_seconds() / 86400)}d"
    return {"t": t, "agent": r["agent"], "verb": r["verb"], "obj": r["obj"], "touched": r["touched_pages"] or 0}


def add_signal(
    conn: sqlite3.Connection, *, article_id: str | None, kind: str, value: dict | None = None
) -> None:
    conn.execute(
        "INSERT INTO signals (article_id, kind, value_json) VALUES (?, ?, ?)",
        (article_id, kind, json.dumps(value) if value else None),
    )


# ─────────────────────────────── Linter findings ───────────────────────────────

def _finding_hash(*, kind: str, article_id: str | None, target: str | None) -> str:
    import hashlib
    key = f"{kind}|{article_id or ''}|{target or ''}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def record_finding(
    conn: sqlite3.Connection,
    *,
    kind: str,
    article_id: str | None,
    target: str | None = None,
) -> bool:
    """Upsert a Linter finding; return True iff this is the first time we've
    seen this (kind, article_id, target) tuple in an unresolved state.

    On a subsequent tick where the condition still holds, we bump ``last_seen``
    and return False so the caller can skip emitting another feed row.

    If a matching row was previously resolved (``resolved_at IS NOT NULL``),
    we treat reappearance as a fresh finding: clear ``resolved_at``, refresh
    ``first_seen`` and ``last_seen``, and return True so it emits again.
    """
    h = _finding_hash(kind=kind, article_id=article_id, target=target)
    existing = conn.execute(
        "SELECT hash, resolved_at FROM lint_findings WHERE hash = ?", (h,)
    ).fetchone()
    if existing is None:
        conn.execute(
            """INSERT INTO lint_findings (hash, kind, article_id, target)
               VALUES (?, ?, ?, ?)""",
            (h, kind, article_id, target),
        )
        return True
    if existing["resolved_at"] is not None:
        # Previously resolved, now reappearing — treat as new.
        conn.execute(
            """UPDATE lint_findings
               SET resolved_at = NULL,
                   first_seen = CURRENT_TIMESTAMP,
                   last_seen = CURRENT_TIMESTAMP
               WHERE hash = ?""",
            (h,),
        )
        return True
    # Still open: just bump last_seen.
    conn.execute(
        "UPDATE lint_findings SET last_seen = CURRENT_TIMESTAMP WHERE hash = ?",
        (h,),
    )
    return False


def resolve_finding(
    conn: sqlite3.Connection,
    *,
    kind: str | None = None,
    article_id: str | None = None,
    target: str | None = None,
    hash: str | None = None,
) -> int:
    """Mark a finding as resolved.

    Either pass ``hash`` directly, or the (kind, article_id, target) tuple
    and we'll derive it. Returns rowcount (0 if nothing matched or already
    resolved).
    """
    if hash is None:
        if kind is None:
            return 0
        hash = _finding_hash(kind=kind, article_id=article_id, target=target)
    cur = conn.execute(
        """UPDATE lint_findings
           SET resolved_at = CURRENT_TIMESTAMP
           WHERE hash = ? AND resolved_at IS NULL""",
        (hash,),
    )
    return cur.rowcount or 0


def resolve_findings_for_article(
    conn: sqlite3.Connection,
    *,
    kind: str,
    article_id: str,
    keep_targets: set[str] | None = None,
) -> int:
    """Resolve open findings of ``kind`` for ``article_id`` whose ``target`` is
    NOT in ``keep_targets``. Used to clear stale 'dangling' findings once a
    target either resolves or stops being referenced.
    """
    keep = keep_targets or set()
    rows = conn.execute(
        """SELECT hash, target FROM lint_findings
           WHERE kind = ? AND article_id = ? AND resolved_at IS NULL""",
        (kind, article_id),
    ).fetchall()
    n = 0
    for r in rows:
        if r["target"] in keep:
            continue
        conn.execute(
            "UPDATE lint_findings SET resolved_at = CURRENT_TIMESTAMP WHERE hash = ?",
            (r["hash"],),
        )
        n += 1
    return n


def pin_article(conn: sqlite3.Connection, article_id: str) -> None:
    conn.execute("INSERT OR IGNORE INTO pinned (article_id) VALUES (?)", (article_id,))


def unpin_article(conn: sqlite3.Connection, article_id: str) -> None:
    conn.execute("DELETE FROM pinned WHERE article_id = ?", (article_id,))


# ─────────────────────────────── Search ───────────────────────────────

def search_articles(conn: sqlite3.Connection, q: str, *, limit: int = 20) -> list[dict]:
    if not q.strip():
        return []
    # External-content FTS: join on rowid
    rows = conn.execute(
        """SELECT articles.id, articles.title, articles.kind, articles.summary,
                  snippet(articles_fts, 2, '<mark>', '</mark>', '…', 10) AS snippet
           FROM articles_fts JOIN articles ON articles.rowid = articles_fts.rowid
           WHERE articles_fts MATCH ? ORDER BY rank LIMIT ?""",
        (_fts_escape(q), limit),
    )
    return [dict(r) for r in rows]


_STOPWORDS = frozenset([
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does",
    "for", "from", "how", "i", "if", "in", "is", "it", "its", "of", "on",
    "or", "that", "the", "their", "them", "then", "there", "they", "this",
    "to", "was", "were", "what", "when", "where", "which", "who", "why",
    "will", "with", "you", "your", "me", "my", "our", "us", "me",
    "what's", "that's", "there's", "about", "know", "knows", "tell",
    "show", "please",
])


def _fts_escape(q: str) -> str:
    """Build an FTS5 MATCH expression with OR semantics, stopwords stripped.

    We want "What is test-time compute and why does it matter?" to match any article
    containing the meaningful terms — not require every word, including stopwords.
    """
    import re
    tokens = re.findall(r"[A-Za-z][A-Za-z0-9\-]+", q)
    terms = [t for t in tokens if t.lower() not in _STOPWORDS and len(t) > 1]
    if not terms:
        return f'"{q.strip()}"' if q.strip() else "NULL"
    # Quote each term, OR-join. FTS5 is case-insensitive.
    return " OR ".join(f'"{t}"' for t in terms)


def _fts_palette_query(q: str) -> str | None:
    """Build an FTS5 MATCH for the command palette: prefix match, AND-joined.

    Optimised for narrow-as-you-type behaviour. "test ag" should match an
    article whose title contains both "test*" and "ag*" (e.g. "test agent"),
    not "anything that mentions test OR agent" (that's the Ask flow).

    Hyphens are split into separate tokens so "test-time" still matches
    "test-time compute" even if the user typed "test time". Stopwords are
    dropped. Tokens are lowercased before emitting — this is what protects
    the MATCH parser from reserved-word collisions: FTS5 only treats the
    *all-uppercase* tokens AND/OR/NOT/NEAR as operators, so "NOT NULL"
    becomes "not* null*" which is parsed as two prefix search terms (and
    still matches the indexed "not"/"null" content because FTS5 is
    case-insensitive at index time).

    Returns None when the query has no usable terms — caller should skip
    the SELECT entirely in that case.
    """
    terms = _palette_terms(q)
    if not terms:
        return None
    # `term*` is FTS5 prefix match. AND is the implicit operator between bare
    # tokens, so "foo* bar*" means "starts-with-foo AND starts-with-bar".
    return " ".join(f"{t}*" for t in terms)


def _palette_terms(q: str) -> list[str]:
    import re

    # Tokenization: keep letters/digits/marks across all scripts (Latin with
    # diacritics, CJK, Cyrillic, etc.) and split on everything else INCLUDING
    # underscore. `[^\W_]+` is "word chars but not underscore" — it gives us
    # \w's Unicode coverage without `\w`'s gotcha that "test_helper" stays a
    # single token (which FTS5's query parser would then re-tokenize into a
    # phrase 'test helper', defeating the AND-joined prefix-match contract).
    # An ASCII-only regex would silently drop all non-ASCII queries; this
    # one aligns with what users actually mean for snake_case identifiers.
    tokens = re.findall(r"[^\W_]+", q, flags=re.UNICODE)
    terms: list[str] = []
    for t in tokens:
        lower = t.lower()
        if lower in _STOPWORDS:
            continue
        if len(t) < 2:
            continue
        terms.append(lower)
    return terms


_PALETTE_ARTICLE_CAP = 10
_PALETTE_NOTE_CAP = 6
_PALETTE_BLOG_CAP = 4
_PALETTE_MIRROR_CAP = 3


def _search_result(
    *,
    kind: str,
    id: object,
    title: object,
    subtitle: str,
    excerpt: object,
    link_target: str,
    score: float,
    slug: object | None = None,
) -> dict:
    text = str(title or "").strip() or "(untitled)"
    snippet = str(excerpt or "").strip()
    result = {
        "kind": kind,
        "id": str(id),
        "title": text,
        "subtitle": subtitle,
        "snippet": snippet,
        "excerpt": snippet,
        "link_target": link_target,
        "score": score,
    }
    if slug is not None:
        result["slug"] = str(slug)
    return result


def _like_pattern(term: str) -> str:
    escaped = (
        term.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )
    return f"%{escaped}%"


def _mirror_match_clause(
    columns: list[str],
    terms: list[str],
    *,
    any_term: bool,
) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for term in terms:
        pattern = _like_pattern(term)
        column_clauses = [f"COALESCE(CAST({col} AS TEXT), '') LIKE ? ESCAPE '\\'" for col in columns]
        clauses.append("(" + " OR ".join(column_clauses) + ")")
        params.extend([pattern] * len(columns))
    joiner = " OR " if any_term else " AND "
    return joiner.join(clauses), params


def _compact_parts(*values: object) -> str:
    return " · ".join(str(v).strip() for v in values if str(v or "").strip())


def _truncate(value: object, limit: int = 180) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _search_personal_os_mirrors(
    conn: sqlite3.Connection,
    q: str,
    *,
    per_kind: int = _PALETTE_MIRROR_CAP,
    any_term: bool = False,
) -> list[dict]:
    terms = _palette_terms(q)
    if not terms:
        return []

    rows: list[dict] = []

    def collect(
        *,
        table: str,
        columns: list[str],
        where: str,
        order_by: str,
        build: Any,
    ) -> None:
        clause, params = _mirror_match_clause(columns, terms, any_term=any_term)
        query = (
            f"SELECT * FROM {table} WHERE {where} AND ({clause}) "
            f"ORDER BY {order_by} LIMIT ?"
        )
        for index, row in enumerate(conn.execute(query, (*params, per_kind))):
            rows.append(build(dict(row), 100.0 + index))

    collect(
        table="tasks",
        columns=["text", "due", "scheduled", "priority", "domain", "project", "tags_json", "links_json"],
        where="deleted_at IS NULL",
        order_by="due IS NULL, due ASC, updated_at DESC",
        build=lambda row, score: _search_result(
            kind="task",
            id=row["uid"],
            title=row["text"],
            subtitle="Task",
            excerpt=_compact_parts(row.get("status"), row.get("due"), row.get("domain"), row.get("project")),
            link_target="/tasks",
            score=score,
        ),
    )
    collect(
        table="projects",
        columns=["slug", "name", "type", "domain", "status", "due"],
        where="deleted_at IS NULL",
        order_by="updated_at DESC",
        build=lambda row, score: _search_result(
            kind="project",
            id=row["slug"],
            slug=row["slug"],
            title=row["name"],
            subtitle="Project",
            excerpt=_compact_parts(row.get("type"), row.get("status"), row.get("domain"), row.get("due")),
            link_target="/projects",
            score=score,
        ),
    )
    collect(
        table="routines",
        columns=["slug", "name", "description", "domain", "time_of_day", "specific_time"],
        where="deleted_at IS NULL AND archived = 0",
        order_by="updated_at DESC",
        build=lambda row, score: _search_result(
            kind="routine",
            id=row["slug"],
            slug=row["slug"],
            title=row["name"],
            subtitle="Routine",
            excerpt=_compact_parts(row.get("time_of_day"), row.get("domain"), row.get("description")),
            link_target="/routines",
            score=score,
        ),
    )
    collect(
        table="journal_days",
        columns=["date", "path", "mood", "energy"],
        where="deleted_at IS NULL",
        order_by="date DESC",
        build=lambda row, score: _search_result(
            kind="journal",
            id=row["date"],
            slug=row["date"],
            title=f"Journal {row['date']}",
            subtitle="Journal day",
            excerpt=_compact_parts(row.get("path"), f"{row.get('log_count') or 0} log entries"),
            link_target="/journal",
            score=score,
        ),
    )
    collect(
        table="people",
        columns=["slug", "name", "facts_json", "birthday", "anniversary", "follow_up_at", "last_interaction_at"],
        where="deleted_at IS NULL",
        order_by="updated_at DESC",
        build=lambda row, score: _search_result(
            kind="person",
            id=row["slug"],
            slug=row["slug"],
            title=row["name"],
            subtitle="Person",
            excerpt=_compact_parts(row.get("last_interaction_at"), _truncate(row.get("facts_json"), 120)),
            link_target="/people",
            score=score,
        ),
    )
    collect(
        table="books",
        columns=["slug", "title", "author", "status", "format", "isbn", "summary"],
        where="deleted_at IS NULL",
        order_by="updated_at DESC",
        build=lambda row, score: _search_result(
            kind="book",
            id=row["slug"],
            slug=row["slug"],
            title=row["title"],
            subtitle="Book",
            excerpt=_compact_parts(row.get("author"), row.get("status"), _truncate(row.get("summary"), 120)),
            link_target=f"/library/books/{row['slug']}",
            score=score,
        ),
    )
    collect(
        table="quotes",
        columns=["id", "text", "source_type", "source_ref", "tags_json"],
        where="deleted_at IS NULL",
        order_by="updated_at DESC",
        build=lambda row, score: _search_result(
            kind="quote",
            id=row["id"],
            title=_truncate(row["text"], 90),
            subtitle="Quote",
            excerpt=_compact_parts(row.get("source_type"), row.get("source_ref"), row.get("tags_json")),
            link_target=f"/library/quotes/{row['id']}",
            score=score,
        ),
    )
    collect(
        table="inventory",
        columns=["id", "name", "status", "location", "photo"],
        where="deleted_at IS NULL",
        order_by="updated_at DESC",
        build=lambda row, score: _search_result(
            kind="inventory",
            id=row["id"],
            title=row["name"],
            subtitle="Inventory item",
            excerpt=_compact_parts(row.get("status"), row.get("location")),
            link_target="/inventory",
            score=score,
        ),
    )
    collect(
        table="content_items",
        columns=["slug", "title", "kind", "status", "domain", "channel", "url", "publish_date"],
        where="deleted_at IS NULL",
        order_by="updated_at DESC",
        build=lambda row, score: _search_result(
            kind="content",
            id=row["slug"],
            slug=row["slug"],
            title=row["title"],
            subtitle="Content item",
            excerpt=_compact_parts(row.get("kind"), row.get("status"), row.get("domain"), row.get("channel")),
            link_target="/content",
            score=score,
        ),
    )
    return rows


def search_personal_os_context(
    conn: sqlite3.Connection,
    q: str,
    *,
    per_kind: int = 2,
) -> list[dict]:
    """Compact Ask/RAG retrieval over personal-OS mirror tables.

    This intentionally reuses the LIKE-over-mirror machinery from palette
    search, but uses any-term matching because natural-language questions
    contain extra words beyond the entity names.
    """
    return _search_personal_os_mirrors(conn, q, per_kind=per_kind, any_term=True)


def search_all(
    conn: sqlite3.Connection, q: str, *, limit: int = 20
) -> list[dict]:
    """Unified palette search across wiki, notes, blog, and personal-OS mirrors.

    Each result is a dict with a stable shape the frontend can render
    uniformly::

        {kind, id, title, subtitle, snippet, excerpt, link_target, score}

    ``kind`` includes FTS-backed article/note/blog results and the typed
    personal-OS mirror kinds. ``id`` is always stringified for frontend
    routing.

    Why per-kind quotas instead of a global BM25 sort: BM25's IDF term
    punishes terms that appear in a high fraction of documents in the
    *corpus*. For a small notes corpus where 12/14 notes mention "agent",
    the IDF for "agent" collapses to ~0 — every matching note ties at
    score 0. A naive global merge then floods the result list with
    articles (where "agent" is rarer and so scores higher), and notes
    never appear regardless of how relevant they are to the user's mental
    model. Allotting fixed slots per kind means the user sees notes and
    blog hits even on terms that are common in their own writing.

    Returns rows in fixed order: FTS-backed articles first, then notes, then
    blogs, then typed mirrors. Each block is sorted internally by its native
    query semantics. Capped at ``limit`` total.

    Snippet markers: matched terms are wrapped with ASCII STX (``\\x02``)
    and ETX (``\\x03``) instead of HTML ``<mark>`` tags so they can never
    collide with literal ``<mark>`` text a user might have written in a
    note about HTML. The frontend renders them as React ``<mark>`` nodes.
    """
    expr = _fts_palette_query(q)
    if expr is None:
        return []

    rows: list[dict] = []

    # Articles. bm25 weights: title=10, summary=5, body=1. Tie-break by
    # updated_at so an edit to an older article surfaces above a stale one.
    # snippet column index -1 means "FTS5 picks the best matching column",
    # so a body hit shows body context and a title hit shows the title.
    for r in conn.execute(
        """SELECT articles.id AS id, articles.title AS title,
                  articles.kind AS subkind, articles.summary AS summary,
                  snippet(articles_fts, -1, char(2), char(3), '…', 10) AS snippet,
                  bm25(articles_fts, 10.0, 5.0, 1.0) AS score
           FROM articles_fts JOIN articles ON articles.rowid = articles_fts.rowid
           WHERE articles_fts MATCH ?
           ORDER BY score, articles.updated_at DESC
           LIMIT ?""",
        (expr, _PALETTE_ARTICLE_CAP),
    ):
        rows.append(_search_result(
            kind="article",
            id=r["id"],
            title=r["title"],
            subtitle=r["subkind"],
            excerpt=r["snippet"] or (r["summary"] or "")[:160],
            link_target=f"/a/{r['id']}",
            score=float(r["score"]),
        ))

    # Notes. notes_fts schema is (summary, body). Title for the result is
    # always the summary (else first line of body), so we point snippet at
    # column 1 (body) — keeps the snippet text distinct from the title and
    # always shows context the title doesn't already cover.
    for r in conn.execute(
        """SELECT notes.id AS id, notes.summary AS summary, notes.body AS body,
                  notes.classification AS classification,
                  snippet(notes_fts, 1, char(2), char(3), '…', 10) AS snippet,
                  bm25(notes_fts, 3.0, 1.0) AS score
           FROM notes_fts JOIN notes ON notes.id = notes_fts.rowid
           WHERE notes_fts MATCH ? AND notes.deleted_at IS NULL
           ORDER BY score, notes.created_at DESC
           LIMIT ?""",
        (expr, _PALETTE_NOTE_CAP),
    ):
        body = r["body"] or ""
        first_line = next(
            (ln.strip() for ln in body.splitlines() if ln.strip()), ""
        )
        title = (r["summary"] or first_line or "(empty note)").strip()
        if len(title) > 80:
            title = title[:77] + "…"
        kind_label = (r["classification"] or "Note").capitalize()
        rows.append(_search_result(
            kind="note",
            id=r["id"],
            title=title,
            subtitle=f"Note · {kind_label}",
            excerpt=r["snippet"] or "",
            link_target=f"/notes/{r['id']}",
            score=float(r["score"]),
        ))

    # Blog posts. Excludes tombstoned rows AND pending/failed drafts (a draft
    # has no body to read yet). bm25 weights: title=10, theme=3, body_preview=1.
    for r in conn.execute(
        """SELECT blog_posts.id AS id, blog_posts.title AS title,
                  blog_posts.theme AS theme, blog_posts.body_preview AS body_preview,
                  snippet(blog_posts_fts, -1, char(2), char(3), '…', 10) AS snippet,
                  bm25(blog_posts_fts, 10.0, 3.0, 1.0) AS score
           FROM blog_posts_fts JOIN blog_posts ON blog_posts.id = blog_posts_fts.rowid
           WHERE blog_posts_fts MATCH ?
             AND blog_posts.deleted_at IS NULL
             AND blog_posts.status = 'done'
           ORDER BY score, blog_posts.created_at DESC
           LIMIT ?""",
        (expr, _PALETTE_BLOG_CAP),
    ):
        title = (r["title"] or r["theme"] or "Untitled draft").strip()
        rows.append(_search_result(
            kind="blog",
            id=r["id"],
            title=title,
            subtitle="Blog post",
            excerpt=r["snippet"] or (r["body_preview"] or "")[:160],
            link_target=f"/blog/{r['id']}",
            score=float(r["score"]),
        ))

    rows.extend(_search_personal_os_mirrors(conn, q))

    return rows[:limit]


# ─────────────────────────────── Article artifacts ───────────────────────────────

def list_artifacts(conn: sqlite3.Connection, article_id: str) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, article_id, kind, title, description, spec_json,
                      created_by, created_at, updated_at
               FROM article_artifacts
               WHERE article_id = ?
               ORDER BY created_at ASC, id ASC""",
            (article_id,),
        )
    ]


def get_artifact(conn: sqlite3.Connection, artifact_id: int) -> dict | None:
    row = conn.execute(
        """SELECT id, article_id, kind, title, description, spec_json,
                  created_by, created_at, updated_at
           FROM article_artifacts WHERE id = ?""",
        (artifact_id,),
    ).fetchone()
    return dict(row) if row else None


def create_artifact(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    kind: str,
    title: str,
    description: str | None,
    spec_json: str,
    created_by: str | None,
) -> dict:
    """Insert an artifact and return the fresh row.

    ``spec_json`` here is already a JSON string — the route layer serializes.
    Re-queries by lastrowid so the caller sees DB-generated timestamps.
    """
    cur = conn.execute(
        """INSERT INTO article_artifacts
             (article_id, kind, title, description, spec_json, created_by)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (article_id, kind, title, description, spec_json, created_by),
    )
    new_id = cur.lastrowid or 0
    row = get_artifact(conn, new_id)
    if row is None:
        # Should be impossible right after a successful insert, but fail loud.
        raise RuntimeError(f"failed to read back artifact {new_id} after insert")
    return row


def update_artifact(
    conn: sqlite3.Connection,
    artifact_id: int,
    *,
    kind: str | None = None,
    title: str | None = None,
    description: str | None = None,
    spec_json: str | None = None,
) -> dict | None:
    """Patch an artifact — only non-None fields are updated. Always bumps
    ``updated_at``. Returns the fresh row, or None if no row matched.

    Note: ``description=None`` means "don't touch" (not "clear to NULL"),
    to keep the PATCH semantics sane. Clearing a description isn't a use
    case we need yet; add a sentinel if it ever becomes one.
    """
    sets: list[str] = []
    params: list[Any] = []
    if kind is not None:
        sets.append("kind = ?")
        params.append(kind)
    if title is not None:
        sets.append("title = ?")
        params.append(title)
    if description is not None:
        sets.append("description = ?")
        params.append(description)
    if spec_json is not None:
        sets.append("spec_json = ?")
        params.append(spec_json)
    if not sets:
        # Nothing to patch — return the current row (or None) so callers
        # can 404 on missing without a second query.
        return get_artifact(conn, artifact_id)
    sets.append("updated_at = CURRENT_TIMESTAMP")
    params.append(artifact_id)
    cur = conn.execute(
        f"UPDATE article_artifacts SET {', '.join(sets)} WHERE id = ?",
        params,
    )
    if cur.rowcount == 0:
        return None
    return get_artifact(conn, artifact_id)


def delete_artifact(conn: sqlite3.Connection, artifact_id: int) -> bool:
    cur = conn.execute("DELETE FROM article_artifacts WHERE id = ?", (artifact_id,))
    return (cur.rowcount or 0) > 0


# ─────────────────────────────── Synthesis feedback (UI side) ───────────────────────────────
#
# Helpers for the accept/discard affordance on Synthesis articles. The Synthesizer
# agent writes rows to ``synthesis_runs`` with ``user_accepted IS NULL`` (pending);
# the UI flips that to 1 (accepted) or 0 (rejected) and stores optional free-text
# feedback that a future prompt loop can replay as few-shot material.
#
# Kept separate from the agent-side helpers (list_graph_edges, record_synthesis_run,
# etc.) so the two tasks can land without touching the same lines.


def _row_to_synthesis_run(row: sqlite3.Row) -> dict:
    """Convert a synthesis_runs row into the API-facing dict.

    Parses ``source_article_ids`` from JSON text to a list. If the column is
    corrupt (shouldn't happen — writes always serialize a list), we surface an
    empty list rather than crashing the whole endpoint.
    """
    d = dict(row)
    raw = d.get("source_article_ids") or "[]"
    try:
        ids = json.loads(raw)
        if not isinstance(ids, list):
            ids = []
    except json.JSONDecodeError:
        ids = []
    d["source_article_ids"] = ids
    return d


def list_pending_synthesis_runs(
    conn: sqlite3.Connection, limit: int = 50
) -> list[dict]:
    """Runs awaiting a user decision, newest first. Drives the sidebar count."""
    rows = conn.execute(
        """SELECT id, cluster_hash, source_article_ids, draft_article_id,
                  eval_score, eval_rationale, user_accepted, user_feedback,
                  created_at, reviewed_at
           FROM synthesis_runs
           WHERE user_accepted IS NULL
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    return [_row_to_synthesis_run(r) for r in rows]


def get_synthesis_run_for_article(
    conn: sqlite3.Connection, article_id: str
) -> dict | None:
    """Most recent run whose ``draft_article_id`` matches, or None.

    Used by the ArticleView to decide whether to show the feedback strip.
    A single article can theoretically be the draft for multiple runs (re-runs
    of the same cluster), so we pick the newest by created_at.
    """
    row = conn.execute(
        """SELECT id, cluster_hash, source_article_ids, draft_article_id,
                  eval_score, eval_rationale, user_accepted, user_feedback,
                  created_at, reviewed_at
           FROM synthesis_runs
           WHERE draft_article_id = ?
           ORDER BY created_at DESC
           LIMIT 1""",
        (article_id,),
    ).fetchone()
    return _row_to_synthesis_run(row) if row else None


def mark_synthesis_accepted(conn: sqlite3.Connection, run_id: int) -> bool:
    """Accept a pending run. Returns True iff a pending row flipped.

    Guards on ``user_accepted IS NULL`` so a double-click or a race between
    two tabs can't overwrite a prior decision.
    """
    cur = conn.execute(
        """UPDATE synthesis_runs
           SET user_accepted = 1,
               reviewed_at = CURRENT_TIMESTAMP
           WHERE id = ? AND user_accepted IS NULL""",
        (run_id,),
    )
    return (cur.rowcount or 0) > 0


def mark_synthesis_rejected(
    conn: sqlite3.Connection, run_id: int, feedback: str | None
) -> bool:
    """Reject a pending run, optionally recording user feedback text.

    Empty/whitespace-only feedback is stored as NULL — the Synthesizer's
    future few-shot pass only wants substantive rejection reasons.
    """
    clean = feedback.strip() if isinstance(feedback, str) else None
    cur = conn.execute(
        """UPDATE synthesis_runs
           SET user_accepted = 0,
               user_feedback = ?,
               reviewed_at = CURRENT_TIMESTAMP
           WHERE id = ? AND user_accepted IS NULL""",
        (clean or None, run_id),
    )
    return (cur.rowcount or 0) > 0

# ─────────────────────────────── Synthesizer ───────────────────────────────

def list_graph_edges(conn: sqlite3.Connection) -> list[tuple[str, str, float]]:
    """Return every (from, to, weight) edge in ``links``.

    Used by the Synthesizer's Personalized PageRank pass to build an adjacency
    matrix. We return raw tuples (not dicts) because PPR only needs the three
    columns and tuple unpacking is faster in the inner loop.
    """
    return [
        (r["from_article"], r["to_article"], float(r["weight"] or 0.5))
        for r in conn.execute(
            "SELECT from_article, to_article, weight FROM links"
        )
    ]


def recent_seed_articles(
    conn: sqlite3.Connection, *, hours: int = 48, limit: int = 10
) -> list[dict]:
    """Articles updated within the last ``hours`` hours, most-recent first.

    Used as PPR seeds. Capped at ``limit`` — if more than 10 articles churned
    in the window, we only seed from the freshest 10 to keep the spread
    focused (otherwise the random walk diffuses uniformly and the top-K is
    meaningless). 'Synthesis' kind is excluded from seeds — we don't want to
    chain-synthesise off our own output.
    """
    return [
        dict(r)
        for r in conn.execute(
            """SELECT id, title, kind, updated_at
               FROM articles
               WHERE updated_at > datetime('now', ?)
                 AND kind != 'Synthesis'
               ORDER BY updated_at DESC LIMIT ?""",
            (f"-{int(hours)} hours", int(limit)),
        )
    ]


def record_synthesis_run(
    conn: sqlite3.Connection,
    *,
    cluster_hash: str,
    source_article_ids: list[str],
    draft_article_id: str | None,
    eval_score: float | None,
    eval_rationale: str | None,
    prompt_version: int = 1,
) -> int:
    """Insert a synthesis_runs row. Returns the new row id.

    user_accepted is left NULL — the accept/discard UI layer sets it later.
    We always record the row even on low scores so repeat suppression works
    (cluster_hash check skips re-synthesising the same membership) and so
    the user has a rejection trail to calibrate from.
    """
    cur = conn.execute(
        """INSERT INTO synthesis_runs
             (cluster_hash, source_article_ids, prompt_version,
              draft_article_id, eval_score, eval_rationale)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (
            cluster_hash,
            json.dumps(sorted(source_article_ids)),
            prompt_version,
            draft_article_id,
            eval_score,
            eval_rationale,
        ),
    )
    return cur.lastrowid or 0


def find_recent_synthesis_for_hash(
    conn: sqlite3.Connection, cluster_hash: str
) -> dict | None:
    """Return the most recent synthesis_runs row for a given cluster_hash.

    The Synthesizer checks this before drafting: if we already ran on this
    exact membership AND no member has been updated since, skip — nothing's
    changed to warrant a fresh synthesis.
    """
    row = conn.execute(
        """SELECT id, cluster_hash, source_article_ids, draft_article_id,
                  eval_score, eval_rationale, created_at
           FROM synthesis_runs
           WHERE cluster_hash = ?
           ORDER BY created_at DESC LIMIT 1""",
        (cluster_hash,),
    ).fetchone()
    return dict(row) if row else None


def accepted_synthesis_examples(
    conn: sqlite3.Connection, *, limit: int = 3
) -> list[dict]:
    """Most recent user-accepted synthesis drafts. Used as positive few-shot
    exemplars in the draft prompt. Joins into articles so we get the title
    + body the user actually kept.
    """
    return [
        dict(r)
        for r in conn.execute(
            """SELECT sr.id, sr.draft_article_id, a.title, a.summary, a.body_md,
                      sr.eval_score, sr.created_at
               FROM synthesis_runs sr
               JOIN articles a ON a.id = sr.draft_article_id
               WHERE sr.user_accepted = 1 AND sr.draft_article_id IS NOT NULL
               ORDER BY sr.reviewed_at DESC, sr.created_at DESC
               LIMIT ?""",
            (int(limit),),
        )
    ]


def rejected_synthesis_examples(
    conn: sqlite3.Connection, *, limit: int = 2
) -> list[dict]:
    """Most recent user-rejected synthesis drafts with feedback. Used as
    negative few-shot exemplars. Drafts may have been deleted post-rejection,
    so we LEFT JOIN and tolerate missing article rows.
    """
    return [
        dict(r)
        for r in conn.execute(
            """SELECT sr.id, sr.draft_article_id, a.title, a.summary, a.body_md,
                      sr.eval_score, sr.user_feedback, sr.created_at
               FROM synthesis_runs sr
               LEFT JOIN articles a ON a.id = sr.draft_article_id
               WHERE sr.user_accepted = 0
               ORDER BY sr.reviewed_at DESC, sr.created_at DESC
               LIMIT ?""",
            (int(limit),),
        )
    ]


# ─────────────────────────────── Stubs (graph hygiene) ───────────────────────────────

def ensure_stub_article(
    conn: sqlite3.Connection, *, id: str, title: str, kind: str = "Entity"
) -> bool:
    """Create a placeholder article for a wiki-link target that doesn't exist yet.

    The Compiler writes ``<span class="link" data-target="slug">`` references
    before the target concept has its own source. Without a matching ``articles``
    row, those links 404 and the graph stays empty. This inserts a minimal
    stub so forwardlinks/backlinks resolve; a real Compiler pass later will
    overwrite the body via ``upsert_article``.

    Returns True iff a new row was inserted (False if the id already existed —
    never overwrite a real article with a stub).
    """
    from slugify import slugify  # local import: only needed when creating stubs
    existing = conn.execute("SELECT 1 FROM articles WHERE id = ?", (id,)).fetchone()
    if existing:
        return False
    slug = (slugify(title)[:80] if title else id) or id
    conn.execute(
        """INSERT INTO articles (id, kind, title, slug, aka_json, summary, body_md,
                                 confidence, reading_minutes, updated_by, vault_path)
           VALUES (?, ?, ?, ?, '[]', ?, '', 0.0, 1, 'Compiler (stub)', NULL)""",
        (id, kind, title or id, slug,
         "Referenced by other articles — awaiting a dedicated source."),
    )
    return True


def ensure_note_stub_article(
    conn: sqlite3.Connection,
    *,
    id: str,
    title: str,
    kind: str,
    summary: str,
    body_md: str,
    source_note_id: int,
    slug: str | None = None,
) -> bool:
    """Insert an article stub originating from a user note.

    Unlike ``ensure_stub_article`` (which hardcodes ``updated_by='Compiler (stub)'``,
    ``kind='Entity'``, and a placeholder summary), this helper takes the
    Claude-derived ``body_md``, ``summary`` and ``kind`` so the Escalator can
    create a richer wiki-article stub off a user note.

    Returns True iff a new row was inserted; False if ``id`` already existed
    (never overwrites — same idempotency contract as ``ensure_stub_article``).
    Writes ``updated_by='escalator (stub)'`` and ``confidence=0.0`` so the stub
    is visually distinct until a full Compiler/Synthesizer pass rewrites it.
    """
    from slugify import slugify as _slugify  # local import: only needed at stub time

    existing = conn.execute("SELECT 1 FROM articles WHERE id = ?", (id,)).fetchone()
    if existing:
        return False
    final_slug = (slug or _slugify(title)[:80] or id)[:80]
    conn.execute(
        """INSERT INTO articles (id, kind, title, slug, aka_json, summary, body_md,
                                 confidence, reading_minutes, updated_by, vault_path,
                                 source_note_id)
           VALUES (?, ?, ?, ?, '[]', ?, ?, 0.0, 1, 'escalator (stub)', NULL, ?)""",
        (id, kind, title, final_slug, summary, body_md, source_note_id),
    )
    return True


# ─────────────────────────────── Notes ───────────────────────────────

def insert_note(
    conn: sqlite3.Connection,
    *,
    slug: str,
    path: str,
    body: str,
    source: str,
    created_at: datetime,
) -> int:
    """Insert a new note. On UNIQUE-slug collision, retry with -2, -3, ... up to -99.

    The caller has already picked a slug based on timestamp + first-line slugify;
    collisions are vanishingly rare (same-second capture). See spec §5.
    """
    import hashlib
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    base_slug = slug
    for attempt in range(1, 100):
        try_slug = base_slug if attempt == 1 else f"{base_slug}-{attempt}"
        try_path = path if attempt == 1 else path.replace(f"{base_slug}.md", f"{base_slug}-{attempt}.md")
        try:
            cur = conn.execute(
                """INSERT INTO notes (slug, path, body, body_sha256, source, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (try_slug, try_path, body, body_sha, source, created_at.isoformat()),
            )
            return cur.lastrowid or 0
        except sqlite3.IntegrityError as e:
            msg = str(e).lower()
            if "unique" in msg and ("slug" in msg or "path" in msg):
                continue
            raise
    raise RuntimeError(f"insert_note: exhausted slug collision retries for {base_slug!r}")


def get_note(conn: sqlite3.Connection, note_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM notes WHERE id = ?", (note_id,)).fetchone()
    return dict(row) if row else None


def list_notes(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    before_id: int | None = None,
    classification: str | None = None,
) -> list[dict]:
    """List notes, newest first. Excludes tombstoned rows."""
    q = "SELECT * FROM notes WHERE deleted_at IS NULL"
    params: list[Any] = []
    if before_id is not None:
        q += " AND id < ?"
        params.append(before_id)
    if classification is not None:
        q += " AND classification = ?"
        params.append(classification)
    q += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def soft_delete_note(conn: sqlite3.Connection, note_id: int) -> None:
    conn.execute(
        "UPDATE notes SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND deleted_at IS NULL",
        (note_id,),
    )


def insert_note_link(
    conn: sqlite3.Connection,
    *,
    note_id: int,
    article_id: str,
    rank: int = 0,
) -> None:
    """Idempotent insert — primary key is (note_id, article_id) so OR IGNORE no-ops on duplicates.

    ``rank=0`` is reserved for direct user-action links (e.g. "Add thoughts" on an
    open question). Later classifier-created links should use rank>=1.
    """
    conn.execute(
        """INSERT OR IGNORE INTO note_links (note_id, article_id, rank)
           VALUES (?, ?, ?)""",
        (note_id, article_id, rank),
    )


def list_notes_for_article(
    conn: sqlite3.Connection,
    *,
    article_id: str,
    limit: int = 50,
) -> list[dict]:
    """Notes linked to an article via note_links, newest first. Excludes tombstoned."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT n.*, nl.rank
               FROM notes n
               JOIN note_links nl ON nl.note_id = n.id
               WHERE nl.article_id = ? AND n.deleted_at IS NULL
               ORDER BY n.created_at DESC, n.id DESC
               LIMIT ?""",
            (article_id, limit),
        ).fetchall()
    ]


# ─────────────────────────────── Roundtables ───────────────────────────────

def create_roundtable(
    conn: sqlite3.Connection,
    *,
    input_type: str,
    input_ref: str,
    prompt: str,
) -> int:
    """Insert a roundtable row in status='pending'. Returns the new id."""
    cur = conn.execute(
        """INSERT INTO roundtables (input_type, input_ref, prompt, status)
           VALUES (?, ?, ?, 'pending')""",
        (input_type, input_ref, prompt),
    )
    return cur.lastrowid or 0


def get_roundtable(conn: sqlite3.Connection, rt_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM roundtables WHERE id = ?", (rt_id,)
    ).fetchone()
    return dict(row) if row else None


def list_roundtables(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    before_id: int | None = None,
) -> list[dict]:
    """List roundtables newest first, optional keyset pagination via before_id."""
    q = "SELECT * FROM roundtables"
    params: list[Any] = []
    if before_id is not None:
        q += " WHERE id < ?"
        params.append(before_id)
    q += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(q, params).fetchall()]


def list_roundtable_perspectives(
    conn: sqlite3.Connection, rt_id: int
) -> list[dict]:
    """Perspectives for a roundtable, ordered by backend for stable rendering."""
    return [
        dict(r) for r in conn.execute(
            "SELECT * FROM roundtable_perspectives WHERE roundtable_id = ? ORDER BY backend",
            (rt_id,),
        ).fetchall()
    ]


def insert_roundtable_perspective(
    conn: sqlite3.Connection,
    *,
    roundtable_id: int,
    backend: str,
    model: str | None,
    content: str | None,
    error: str | None,
    latency_ms: int | None,
    started_at: str | None,
    finished_at: str | None,
) -> int:
    """Record one backend's response (success or error). Always writes a row."""
    cur = conn.execute(
        """INSERT INTO roundtable_perspectives
             (roundtable_id, backend, model, content, error, latency_ms, started_at, finished_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (roundtable_id, backend, model, content, error, latency_ms, started_at, finished_at),
    )
    return cur.lastrowid or 0


def update_roundtable_status(
    conn: sqlite3.Connection,
    *,
    rt_id: int,
    status: str,
    synthesis: str | None = None,
    synthesis_model: str | None = None,
    error: str | None = None,
    finished: bool = False,
) -> None:
    """Patch a roundtable's status and (optionally) synthesis/error.

    When ``finished=True``, also stamps ``finished_at`` and writes the
    synthesis/error fields. The ``running`` transition uses the compact form.
    """
    if finished:
        conn.execute(
            """UPDATE roundtables
               SET status = ?, synthesis = ?, synthesis_model = ?, error = ?,
                   finished_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, synthesis, synthesis_model, error, rt_id),
        )
    else:
        conn.execute(
            "UPDATE roundtables SET status = ? WHERE id = ?",
            (status, rt_id),
        )


# ─────────────────────────────── GitHub repos ───────────────────────────────

def insert_repo(
    conn: sqlite3.Connection,
    *,
    slug: str,
    owner: str,
    name: str,
    display_name: str | None,
    description: str | None,
    default_branch: str | None,
    is_private: int,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO repos
           (slug, owner, name, display_name, description, default_branch, is_private, added_at, deleted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT added_at FROM repos WHERE slug = ?), CURRENT_TIMESTAMP), NULL)""",
        (slug, owner, name, display_name, description, default_branch, is_private, slug),
    )


def get_repo(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute("SELECT * FROM repos WHERE slug = ?", (slug,)).fetchone()
    return dict(row) if row else None


def list_repos(conn: sqlite3.Connection, *, include_deleted: bool = False) -> list[dict]:
    q = "SELECT * FROM repos"
    if not include_deleted:
        q += " WHERE deleted_at IS NULL"
    q += " ORDER BY added_at DESC"
    return [dict(r) for r in conn.execute(q).fetchall()]


def soft_delete_repo(conn: sqlite3.Connection, slug: str) -> None:
    conn.execute(
        "UPDATE repos SET deleted_at = CURRENT_TIMESTAMP WHERE slug = ? AND deleted_at IS NULL",
        (slug,),
    )


def insert_repo_snapshot(
    conn: sqlite3.Connection,
    *,
    repo_slug: str,
    polled_at: str,
    latest_commit_sha: str | None = None,
    latest_commit_at: str | None = None,
    open_issues_count: int | None = None,
    open_prs_count: int | None = None,
    stars_count: int | None = None,
    forks_count: int | None = None,
    commits_json: str | None = None,
    issues_json: str | None = None,
    prs_json: str | None = None,
    readme_hash: str | None = None,
    readme_excerpt: str | None = None,
    error: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO repo_snapshots
           (repo_slug, polled_at, latest_commit_sha, latest_commit_at,
            open_issues_count, open_prs_count, stars_count, forks_count,
            commits_json, issues_json, prs_json, readme_hash, readme_excerpt, error)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (repo_slug, polled_at, latest_commit_sha, latest_commit_at,
         open_issues_count, open_prs_count, stars_count, forks_count,
         commits_json, issues_json, prs_json, readme_hash, readme_excerpt, error),
    )
    return cur.lastrowid or 0


def latest_repo_snapshot(conn: sqlite3.Connection, slug: str) -> dict | None:
    row = conn.execute(
        "SELECT * FROM repo_snapshots WHERE repo_slug = ? ORDER BY polled_at DESC, id DESC LIMIT 1",
        (slug,),
    ).fetchone()
    return dict(row) if row else None


def update_repo_context(
    conn: sqlite3.Connection,
    *,
    slug: str,
    context_md: str | None,
    polled_at: str,
) -> None:
    conn.execute(
        "UPDATE repos SET context_md = ?, last_polled_at = ? WHERE slug = ?",
        (context_md, polled_at, slug),
    )


def mark_repo_polled(conn: sqlite3.Connection, *, slug: str, polled_at: str) -> None:
    conn.execute("UPDATE repos SET last_polled_at = ? WHERE slug = ?", (polled_at, slug))


def insert_repo_idea_run(
    conn: sqlite3.Connection,
    *,
    repo_slug: str,
    ideated_at: str,
    snapshot_id: int | None,
    note_ids_json: str,
    model: str,
    error: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO repo_idea_runs (repo_slug, ideated_at, snapshot_id, note_ids_json, model, error)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (repo_slug, ideated_at, snapshot_id, note_ids_json, model, error),
    )
    return cur.lastrowid or 0


def mark_repo_ideated(conn: sqlite3.Connection, *, slug: str, ideated_at: str) -> None:
    conn.execute(
        "UPDATE repos SET last_ideated_at = ? WHERE slug = ?",
        (ideated_at, slug),
    )


def list_ideas_for_repo(
    conn: sqlite3.Connection, slug: str, *, limit: int = 50
) -> list[dict]:
    """Return notes produced by the GithubIdeator for this repo.

    Joins ``repo_idea_runs.note_ids_json`` (a JSON array of note ids) against
    the ``notes`` table via ``json_each``. Excludes tombstoned notes; newest
    first. Same join pattern used by the blog-writer's recent-ideas gather.
    """
    return [dict(r) for r in conn.execute(
        """SELECT n.id, n.slug, n.summary, n.classification, n.confidence,
                  n.created_at, n.classified_at
           FROM repo_idea_runs r
           JOIN json_each(r.note_ids_json) je
           JOIN notes n ON n.id = CAST(je.value AS INTEGER)
           WHERE r.repo_slug = ?
             AND json_valid(r.note_ids_json)
             AND n.deleted_at IS NULL
           ORDER BY n.created_at DESC, n.id DESC
           LIMIT ?""",
        (slug, limit),
    )]


def list_recent_idea_runs(
    conn: sqlite3.Connection, slug: str, *, limit: int = 5
) -> list[dict]:
    """Return recent ideation runs for this repo for UI visibility.

    Each row includes ``ideas_count`` (array length of ``note_ids_json``) so
    the UI can show ``3 ideas`` etc. without a second query per run.
    """
    return [dict(r) for r in conn.execute(
        """SELECT id, ideated_at, model, error,
                  CASE WHEN json_valid(note_ids_json)
                       THEN json_array_length(note_ids_json) ELSE 0 END AS ideas_count
           FROM repo_idea_runs
           WHERE repo_slug = ?
           ORDER BY ideated_at DESC
           LIMIT ?""",
        (slug, limit),
    )]


# ─────────────────────────────── Blog posts ───────────────────────────────
# See docs/superpowers/specs/2026-04-22-blog-writer-design.md §§10-11.


def create_blog_post(
    conn: sqlite3.Connection,
    *,
    theme: str,
    window_days: int,
    content_slug: str | None = None,
) -> int:
    """Insert a blog_posts row in status='pending' and return its id.

    slug, path, title, model and word_count are all null at creation — they're
    populated only when the BlogWriter agent finishes a successful draft. The
    spec's storage model treats the vault file as the source of truth for
    body_md; this row is the index.
    """
    cur = conn.execute(
        """INSERT INTO blog_posts (theme, window_days, content_slug, status)
           VALUES (?, ?, ?, 'pending')""",
        (theme, window_days, content_slug),
    )
    return cur.lastrowid or 0


def get_blog_post(conn: sqlite3.Connection, bp_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM blog_posts WHERE id = ?", (bp_id,)
    ).fetchone()
    return dict(row) if row else None


def list_blog_posts(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    before_id: int | None = None,
) -> list[dict]:
    """List blog posts newest first, excluding tombstoned rows.

    before_id enables keyset pagination for long lists (same shape as
    list_roundtables).
    """
    qry = "SELECT * FROM blog_posts WHERE deleted_at IS NULL"
    params: list[Any] = []
    if before_id is not None:
        qry += " AND id < ?"
        params.append(before_id)
    qry += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(qry, params).fetchall()]


def update_blog_post_status(
    conn: sqlite3.Connection,
    *,
    bp_id: int,
    status: str,
    error: str | None = None,
    finished: bool = False,
) -> None:
    """Patch a blog post's status (and optionally error).

    ``finished=True`` stamps finished_at. Used for running→failed and the
    (rare) mid-flight failed transition. Success transitions go through
    update_blog_post_done so we can write slug/path/title atomically.
    """
    if finished:
        conn.execute(
            """UPDATE blog_posts
               SET status = ?, error = ?, finished_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, error, bp_id),
        )
    else:
        conn.execute(
            "UPDATE blog_posts SET status = ?, error = ? WHERE id = ?",
            (status, error, bp_id),
        )


def update_blog_post_done(
    conn: sqlite3.Connection,
    *,
    bp_id: int,
    slug: str,
    path: str,
    title: str,
    tags_json: str,
    model: str,
    word_count: int,
    body_preview: str,
) -> int:
    """Flip a blog post to status='done' and write all the draft-derived fields.

    Called once per successful agent run. slug/path/title become non-null here
    (they were null during pending/running).

    Returns the number of rows affected. The ``WHERE deleted_at IS NULL`` clause
    closes a DELETE-mid-write race: if the user hit DELETE between the post-
    write re-check and this UPDATE, 0 rows are affected and the caller MUST
    unlink the draft file it just wrote.
    """
    cur = conn.execute(
        """UPDATE blog_posts
           SET status = 'done', slug = ?, path = ?, title = ?, tags_json = ?,
               model = ?, word_count = ?, body_preview = ?,
               error = NULL, finished_at = CURRENT_TIMESTAMP
           WHERE id = ? AND deleted_at IS NULL""",
        (slug, path, title, tags_json, model, word_count, body_preview, bp_id),
    )
    return cur.rowcount or 0


def soft_delete_blog_post(conn: sqlite3.Connection, bp_id: int) -> None:
    """Soft-delete the blog post by setting deleted_at.

    Sources remain (spec §7 — soft-delete is orthogonal to status and does not
    cascade). The vault file unlink is handled by the route layer — this only
    touches DB.
    """
    conn.execute(
        """UPDATE blog_posts SET deleted_at = CURRENT_TIMESTAMP
           WHERE id = ? AND deleted_at IS NULL""",
        (bp_id,),
    )


def reset_blog_post_for_regenerate(conn: sqlite3.Connection, bp_id: int) -> None:
    """Reset a blog_posts row to pending for a regenerate run.

    CASCADE clears blog_post_sources when the caller deletes via the
    ``delete_blog_post_sources`` helper (which hits the same rows).

    We INTENTIONALLY keep ``path`` and ``slug`` on the row. The BlogWriter
    agent reads them at ``_handle`` start to unlink the prior draft file
    before writing a fresh one — otherwise the previous file would orphan
    and ``_unique_draft_path`` would slide to a ``-2.md`` variant. The
    successful agent run later overwrites slug/path via ``update_blog_post_done``.
    title/tags/model/word_count/body_preview ARE cleared — callers use them
    to detect "row has been redrafted".
    """
    conn.execute(
        """UPDATE blog_posts
           SET status = 'pending', error = NULL, finished_at = NULL,
               title = NULL, tags_json = '[]',
               model = NULL, word_count = NULL, body_preview = NULL
           WHERE id = ?""",
        (bp_id,),
    )


def insert_blog_post_source(
    conn: sqlite3.Connection,
    *,
    blog_post_id: int,
    kind: str,
    ref: str,
    rank: int,
    used: bool,
    origin: str | None = None,
) -> int:
    """Record one source considered for a blog draft. See spec §10."""
    cur = conn.execute(
        """INSERT INTO blog_post_sources (blog_post_id, kind, ref, rank, used, origin)
           VALUES (?, ?, ?, ?, ?, ?)""",
        (blog_post_id, kind, ref, rank, 1 if used else 0, origin),
    )
    return cur.lastrowid or 0


def list_blog_post_sources(
    conn: sqlite3.Connection, blog_post_id: int
) -> list[dict]:
    """All sources for a blog post, ordered by rank (1-indexed)."""
    return [
        dict(r)
        for r in conn.execute(
            """SELECT * FROM blog_post_sources
               WHERE blog_post_id = ? ORDER BY rank ASC""",
            (blog_post_id,),
        ).fetchall()
    ]


def delete_blog_post_sources(
    conn: sqlite3.Connection, blog_post_id: int
) -> int:
    """Remove all blog_post_sources rows for a post. Used before regenerate."""
    cur = conn.execute(
        "DELETE FROM blog_post_sources WHERE blog_post_id = ?",
        (blog_post_id,),
    )
    return cur.rowcount or 0


# ─────────────────────────────── Tweet threads ───────────────────────────────


def create_tweet_thread(
    conn: sqlite3.Connection,
    *,
    theme: str,
    url: str | None,
    window_days: int,
    include_web: bool,
    use_browser_context: bool,
) -> int:
    cur = conn.execute(
        """INSERT INTO tweet_threads
           (theme, url, window_days, include_web, use_browser_context, status)
           VALUES (?, ?, ?, ?, ?, 'pending')""",
        (
            theme,
            url,
            window_days,
            1 if include_web else 0,
            1 if use_browser_context else 0,
        ),
    )
    return cur.lastrowid or 0


def get_tweet_thread(conn: sqlite3.Connection, thread_id: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM tweet_threads WHERE id = ?", (thread_id,),
    ).fetchone()
    return dict(row) if row else None


def list_tweet_threads(
    conn: sqlite3.Connection,
    *,
    limit: int = 50,
    before_id: int | None = None,
) -> list[dict]:
    qry = "SELECT * FROM tweet_threads WHERE deleted_at IS NULL"
    params: list[Any] = []
    if before_id is not None:
        qry += " AND id < ?"
        params.append(before_id)
    qry += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(qry, params).fetchall()]


def update_tweet_thread_status(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    status: str,
    error: str | None = None,
    finished: bool = False,
) -> None:
    if finished:
        conn.execute(
            """UPDATE tweet_threads
               SET status = ?, error = ?, finished_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (status, error, thread_id),
        )
    else:
        conn.execute(
            "UPDATE tweet_threads SET status = ?, error = ? WHERE id = ?",
            (status, error, thread_id),
        )


def update_tweet_thread_done(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    title: str,
    angle: str,
    model: str,
    thread_json: str,
    sources_json: str,
    warnings_json: str,
) -> int:
    cur = conn.execute(
        """UPDATE tweet_threads
           SET status = 'done', title = ?, angle = ?, model = ?,
               thread_json = ?, sources_json = ?, warnings_json = ?,
               error = NULL, finished_at = CURRENT_TIMESTAMP
           WHERE id = ? AND deleted_at IS NULL""",
        (
            title,
            angle,
            model,
            thread_json,
            sources_json,
            warnings_json,
            thread_id,
        ),
    )
    return cur.rowcount or 0


def reset_tweet_thread_for_regenerate(conn: sqlite3.Connection, thread_id: int) -> None:
    conn.execute(
        """UPDATE tweet_threads
           SET status = 'pending', model = NULL,
               error = NULL, finished_at = NULL
           WHERE id = ?""",
        (thread_id,),
    )


def soft_delete_tweet_thread(conn: sqlite3.Connection, thread_id: int) -> None:
    conn.execute(
        """UPDATE tweet_threads SET deleted_at = CURRENT_TIMESTAMP
           WHERE id = ? AND deleted_at IS NULL""",
        (thread_id,),
    )


def create_tweet_thread_feedback(
    conn: sqlite3.Connection,
    *,
    thread_id: int,
    target_tweet_index: int | None,
    body: str,
) -> int:
    cur = conn.execute(
        """INSERT INTO tweet_thread_feedback
           (tweet_thread_id, target_tweet_index, body)
           VALUES (?, ?, ?)""",
        (thread_id, target_tweet_index, body),
    )
    return cur.lastrowid or 0


def list_tweet_thread_feedback(
    conn: sqlite3.Connection,
    thread_id: int,
    *,
    pending_only: bool = False,
) -> list[dict]:
    qry = """SELECT * FROM tweet_thread_feedback
             WHERE tweet_thread_id = ?"""
    params: list[Any] = [thread_id]
    if pending_only:
        qry += " AND applied_at IS NULL"
    qry += " ORDER BY created_at ASC, id ASC"
    return [dict(r) for r in conn.execute(qry, params).fetchall()]


def mark_tweet_thread_feedback_applied(conn: sqlite3.Connection, thread_id: int) -> int:
    cur = conn.execute(
        """UPDATE tweet_thread_feedback
           SET applied_at = CURRENT_TIMESTAMP
           WHERE tweet_thread_id = ? AND applied_at IS NULL""",
        (thread_id,),
    )
    return cur.rowcount or 0


def get_recent_blog_post_source_refs(
    conn: sqlite3.Connection, lookback: int
) -> set[tuple[str, str]]:
    """Union of ``(kind, ref)`` from blog_post_sources joined to the most
    recent ``lookback`` non-deleted blog_posts (ordered by created_at DESC).

    ``ref`` is stringified so candidates with int refs (notes) and string refs
    (article slugs) compare uniformly against the schema's TEXT column.
    """
    if lookback <= 0:
        return set()
    rows = conn.execute(
        """SELECT s.kind AS kind, s.ref AS ref
           FROM blog_post_sources s
           JOIN (
             SELECT id FROM blog_posts
             WHERE deleted_at IS NULL
             ORDER BY created_at DESC, id DESC
             LIMIT ?
           ) p ON p.id = s.blog_post_id""",
        (lookback,),
    ).fetchall()
    return {(r["kind"], str(r["ref"])) for r in rows}


def reclaim_running_blog_posts(
    conn: sqlite3.Connection, *, stale_minutes: int = 60
) -> int:
    """Flip orphaned running blog_posts rows to failed on daemon boot.

    Mirrors scheduler's _reclaim_orphaned_running for the jobs queue. A blog
    post stuck in 'running' past stale_minutes almost certainly lost its
    daemon to a crash / restart — the agent's _handle wouldn't get another
    chance because the jobs row may have been reclaimed to 'queued' but our
    own in-loop status guard would skip 'running' on second pickup anyway.

    Returns the number of rows reclaimed.
    """
    cur = conn.execute(
        f"""UPDATE blog_posts
            SET status = 'failed', error = 'daemon restart during draft',
                finished_at = CURRENT_TIMESTAMP
            WHERE status = 'running'
              AND created_at < datetime('now', '-{int(stale_minutes)} minutes')""",
    )
    return cur.rowcount or 0


def reclaim_running_tweet_threads(
    conn: sqlite3.Connection, *, stale_minutes: int = 60
) -> int:
    cur = conn.execute(
        f"""UPDATE tweet_threads
            SET status = 'failed', error = 'daemon restart during draft',
                finished_at = CURRENT_TIMESTAMP
            WHERE status = 'running'
              AND created_at < datetime('now', '-{int(stale_minutes)} minutes')""",
    )
    return cur.rowcount or 0
