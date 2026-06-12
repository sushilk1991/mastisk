-- Mastisk schema. Idempotent via IF NOT EXISTS.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS articles (
  id               TEXT PRIMARY KEY,
  kind             TEXT NOT NULL,              -- Concept | Entity | Source | Synthesis
  title            TEXT NOT NULL,
  slug             TEXT NOT NULL,
  aka_json         TEXT DEFAULT '[]',
  summary          TEXT,
  body_md          TEXT NOT NULL DEFAULT '',
  confidence       REAL DEFAULT 0.5,
  reading_minutes  INTEGER DEFAULT 3,
  sources_count    INTEGER DEFAULT 0,
  backlinks_count  INTEGER DEFAULT 0,
  forwardlinks_count INTEGER DEFAULT 0,
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_by       TEXT,
  vault_path       TEXT,
  hero_image_url   TEXT                       -- optional hero picked from the source at ingest time
);

CREATE INDEX IF NOT EXISTS idx_articles_kind ON articles(kind);
CREATE INDEX IF NOT EXISTS idx_articles_updated ON articles(updated_at DESC);

-- External-content FTS5: mirror of `articles`. Rowid in FTS = rowid in articles;
-- we query by joining on rowid rather than carrying id as a column.
CREATE VIRTUAL TABLE IF NOT EXISTS articles_fts USING fts5(
  title, summary, body_md,
  content='articles', content_rowid='rowid'
);

CREATE TABLE IF NOT EXISTS article_sections (
  article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  idx        INTEGER NOT NULL,
  heading    TEXT NOT NULL,
  body       TEXT NOT NULL,
  kind       TEXT DEFAULT 'section',          -- section | callout | open
  PRIMARY KEY (article_id, idx)
);

CREATE TABLE IF NOT EXISTS article_embeddings (
  article_id TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  dim        INTEGER NOT NULL,
  vec        BLOB NOT NULL,                   -- float32 little-endian packed
  computed_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS sources (
  id            TEXT PRIMARY KEY,
  kind          TEXT,                          -- blog | podcast | youtube | paper | rss | twitter
  url           TEXT UNIQUE,
  title         TEXT,
  published_at  DATETIME,
  fetched_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
  raw_path      TEXT,                          -- ./data/raw/<hash>.{txt,html,vtt}
  author        TEXT,
  hero_image_url TEXT,                         -- optional thumbnail / cover art captured at ingest
  media_json    TEXT,                          -- inline media captured at ingest (JSON array of {src, alt, caption})
  duration_sec  INTEGER,                       -- audio/video runtime, used by the podcast view
  feed_url      TEXT                           -- for podcast episodes: the show's RSS URL (so we can group episodes by show)
);
CREATE INDEX IF NOT EXISTS idx_sources_kind ON sources(kind);

-- Whisper segments for time-anchored transcript reading. Populated by the
-- Listener after a successful whisper.transcribe() call when segments are
-- available (mlx-whisper returns segments alongside the joined text).
-- Cascade-deletes when the source is deleted.
CREATE TABLE IF NOT EXISTS source_transcript_segments (
  source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  idx        INTEGER NOT NULL,                 -- 0-based ordinal in playback order
  start_sec  REAL NOT NULL,
  end_sec    REAL NOT NULL,
  text       TEXT NOT NULL,
  PRIMARY KEY (source_id, idx)
);
CREATE INDEX IF NOT EXISTS idx_segments_source ON source_transcript_segments(source_id);

CREATE TABLE IF NOT EXISTS article_sources (
  article_id TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  source_id  TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  PRIMARY KEY (article_id, source_id)
);

CREATE TABLE IF NOT EXISTS links (
  from_article TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  to_article   TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  weight       REAL DEFAULT 0.5,
  snippet      TEXT,                           -- the line where the link appeared (for backlinks rail)
  PRIMARY KEY (from_article, to_article)
);
CREATE INDEX IF NOT EXISTS idx_links_to ON links(to_article);

CREATE TABLE IF NOT EXISTS feed (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts DATETIME DEFAULT CURRENT_TIMESTAMP,
  agent TEXT NOT NULL,
  verb TEXT NOT NULL,
  obj  TEXT NOT NULL,
  kind TEXT,
  touched_pages INTEGER DEFAULT 0,
  payload_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_feed_ts ON feed(ts DESC);

CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL,
  kind  TEXT NOT NULL,
  payload_json TEXT,
  status TEXT NOT NULL DEFAULT 'queued',      -- queued | running | done | failed
  attempts INTEGER DEFAULT 0,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  started_at DATETIME,
  finished_at DATETIME,
  error TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_pending ON jobs(agent, status, created_at);

CREATE TABLE IF NOT EXISTS rss_feeds (
  url TEXT PRIMARY KEY,
  title TEXT,
  last_fetched DATETIME,
  last_etag TEXT,
  last_modified TEXT,
  enabled INTEGER DEFAULT 1,
  added_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts DATETIME DEFAULT CURRENT_TIMESTAMP,
  article_id TEXT REFERENCES articles(id) ON DELETE CASCADE,
  kind TEXT NOT NULL,                         -- opened | time_read | pinned | unpinned | deleted | edited | asked | skipped
  value_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_signals_article ON signals(article_id);
CREATE INDEX IF NOT EXISTS idx_signals_ts ON signals(ts DESC);

CREATE TABLE IF NOT EXISTS pinned (
  article_id TEXT PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
  pinned_at  DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS agent_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  agent TEXT NOT NULL,
  started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  finished_at DATETIME,
  jobs_processed INTEGER DEFAULT 0,
  error TEXT
);

-- Per-article artifacts — charts, comparison cards, timelines, stat panels, etc.
-- Rendered in the article's right rail. spec_json is the declarative spec the
-- frontend consumes (Chart.js config for kind='chart', structured JSON for the
-- others). The generator (artifact-agent) and humans can both write these.
CREATE TABLE IF NOT EXISTS article_artifacts (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  article_id   TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  kind         TEXT NOT NULL,      -- 'chart' | 'comparison' | 'timeline' | 'stat'
  title        TEXT NOT NULL,
  description  TEXT,               -- 1-2 sentence narrative that goes next to the viz
  spec_json    TEXT NOT NULL,      -- Chart.js config OR declarative spec for other kinds
  created_by   TEXT,               -- 'compiler' | 'artifact-agent' | 'user'
  created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_artifacts_article ON article_artifacts(article_id);

-- Linter finding dedup: each structural finding gets a stable hash so the
-- Linter only emits a feed row the first time it sees a condition. Bumping
-- last_seen on subsequent hits lets us age out stale findings without feed
-- spam. resolved_at is set when the condition clears (e.g. an orphan gets
-- a backlink) so we can re-flag if it reappears.
CREATE TABLE IF NOT EXISTS lint_findings (
  hash TEXT PRIMARY KEY,
  kind TEXT NOT NULL,              -- 'orphan' | 'empty' | 'dangling' | etc.
  article_id TEXT,
  target TEXT,                     -- for 'dangling', the missing target slug
  first_seen DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_seen  DATETIME DEFAULT CURRENT_TIMESTAMP,
  resolved_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_lint_findings_open ON lint_findings(kind) WHERE resolved_at IS NULL;

-- Synthesizer bookkeeping. One row per Draft→Critic pass. cluster_hash is a
-- stable identifier for "these N article ids, in sorted order", so we can
-- skip re-synthesising a cluster whose membership hasn't changed. Scores
-- and rationale come from the Critic model; user_accepted / user_feedback
-- are set later by the accept-or-discard UI layer.
CREATE TABLE IF NOT EXISTS synthesis_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_hash TEXT NOT NULL,
  source_article_ids TEXT NOT NULL,      -- json array
  prompt_version INTEGER NOT NULL DEFAULT 1,
  draft_article_id TEXT REFERENCES articles(id) ON DELETE SET NULL,
  eval_score REAL,                        -- 1.0-5.0
  eval_rationale TEXT,
  user_accepted INTEGER,                  -- null = pending, 1 = accepted, 0 = rejected
  user_feedback TEXT,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  reviewed_at DATETIME
);
CREATE INDEX IF NOT EXISTS idx_synthesis_runs_hash ON synthesis_runs(cluster_hash);
CREATE INDEX IF NOT EXISTS idx_synthesis_runs_pending ON synthesis_runs(user_accepted) WHERE user_accepted IS NULL;

-- Triggers: keep external-content FTS in sync
CREATE TRIGGER IF NOT EXISTS articles_ai AFTER INSERT ON articles BEGIN
  INSERT INTO articles_fts(rowid, title, summary, body_md)
    VALUES (new.rowid, new.title, new.summary, new.body_md);
END;
CREATE TRIGGER IF NOT EXISTS articles_ad AFTER DELETE ON articles BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, title, summary, body_md)
    VALUES ('delete', old.rowid, old.title, old.summary, old.body_md);
END;
-- Same WHEN-guard rationale as notes_au and blog_posts_au further down: every
-- link insert/delete fires links_ai/links_ad triggers that bump
-- backlinks_count and forwardlinks_count on the linked article, which
-- otherwise would re-index the article's full title+summary+body for FTS
-- even though none of those columns changed. Article ingestion is link-heavy,
-- so this is the hottest of the three tables for spurious reindex traffic.
CREATE TRIGGER IF NOT EXISTS articles_au AFTER UPDATE ON articles
WHEN old.title IS NOT new.title
  OR old.summary IS NOT new.summary
  OR old.body_md IS NOT new.body_md
BEGIN
  INSERT INTO articles_fts(articles_fts, rowid, title, summary, body_md)
    VALUES ('delete', old.rowid, old.title, old.summary, old.body_md);
  INSERT INTO articles_fts(rowid, title, summary, body_md)
    VALUES (new.rowid, new.title, new.summary, new.body_md);
END;

-- Trigger: keep *_count columns fresh
CREATE TRIGGER IF NOT EXISTS links_ai AFTER INSERT ON links BEGIN
  UPDATE articles SET backlinks_count = backlinks_count + 1 WHERE id = new.to_article;
  UPDATE articles SET forwardlinks_count = forwardlinks_count + 1 WHERE id = new.from_article;
END;
CREATE TRIGGER IF NOT EXISTS links_ad AFTER DELETE ON links BEGIN
  UPDATE articles SET backlinks_count = MAX(0, backlinks_count - 1) WHERE id = old.to_article;
  UPDATE articles SET forwardlinks_count = MAX(0, forwardlinks_count - 1) WHERE id = old.from_article;
END;
CREATE TRIGGER IF NOT EXISTS article_sources_ai AFTER INSERT ON article_sources BEGIN
  UPDATE articles SET sources_count = sources_count + 1 WHERE id = new.article_id;
END;
CREATE TRIGGER IF NOT EXISTS article_sources_ad AFTER DELETE ON article_sources BEGIN
  UPDATE articles SET sources_count = MAX(0, sources_count - 1) WHERE id = old.article_id;
END;

-- ─────────────────────────────── Notes ───────────────────────────────
-- User-authored content. File in vault/_notes/ is the source of truth;
-- this row is a derived index. See docs/superpowers/specs/2026-04-21-notes-subsystem-design.md

CREATE TABLE IF NOT EXISTS notes (
  id                         INTEGER PRIMARY KEY AUTOINCREMENT,
  slug                       TEXT UNIQUE NOT NULL,
  path                       TEXT UNIQUE NOT NULL,
  body                       TEXT NOT NULL,
  body_sha256                TEXT NOT NULL,
  source                     TEXT NOT NULL,          -- 'pwa' | 'cli' | 'file'
  created_at                 DATETIME NOT NULL,
  classified_at              DATETIME,
  classification             TEXT,
  summary                    TEXT,
  confidence                 REAL,
  tags_json                  TEXT DEFAULT '[]',
  escalation_state           TEXT NOT NULL DEFAULT 'none',
  escalation_trigger         TEXT,
  escalation_article_id      TEXT REFERENCES articles(id) ON DELETE SET NULL,
  escalation_retry_count     INTEGER NOT NULL DEFAULT 0,
  escalation_next_attempt_at DATETIME,
  deleted_at                 DATETIME,
  -- Optional anchor when a note was captured against a specific transcript
  -- segment. JSON shape: {"source_id": str, "segment_idx": int, "start_sec": float}.
  -- Null for notes that aren't tied to a podcast/youtube transcript moment.
  transcript_anchor_json     TEXT
);

CREATE INDEX IF NOT EXISTS idx_notes_created_at         ON notes(created_at);
CREATE INDEX IF NOT EXISTS idx_notes_classified_at      ON notes(classified_at);
CREATE INDEX IF NOT EXISTS idx_notes_escalation_pending ON notes(escalation_state, escalation_next_attempt_at)
  WHERE escalation_state IN ('pending', 'retrying');
CREATE INDEX IF NOT EXISTS idx_notes_deleted_at         ON notes(deleted_at);

-- External-content FTS5 over user notes. Same shape as articles_fts: rowid in
-- FTS = id in notes (notes uses INTEGER PK so id == rowid). Search columns are
-- summary (Claude-derived classifier output) and body (raw user text).
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(
  summary, body,
  content='notes', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid, summary, body)
    VALUES (new.id, new.summary, new.body);
END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, summary, body)
    VALUES ('delete', old.id, old.summary, old.body);
END;
-- Only re-index when an indexed column actually changed. Notes get UPDATEd on
-- every escalation_state transition (none → pending → auto_done/manual_done →
-- retrying) and on every cascade SET NULL when a linked article is deleted —
-- none of which touch summary or body. Without this WHEN clause every state
-- transition would emit a redundant delete+reinsert into the FTS index.
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes
WHEN old.summary IS NOT new.summary OR old.body IS NOT new.body
BEGIN
  INSERT INTO notes_fts(notes_fts, rowid, summary, body)
    VALUES ('delete', old.id, old.summary, old.body);
  INSERT INTO notes_fts(rowid, summary, body)
    VALUES (new.id, new.summary, new.body);
END;

CREATE TABLE IF NOT EXISTS note_links (
  note_id    INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  article_id TEXT    NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  rank       INTEGER NOT NULL,
  PRIMARY KEY (note_id, article_id)
);

CREATE INDEX IF NOT EXISTS idx_note_links_article ON note_links(article_id);

CREATE TABLE IF NOT EXISTS note_escalations (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  note_id         INTEGER NOT NULL REFERENCES notes(id) ON DELETE CASCADE,
  triggered_at    DATETIME NOT NULL,
  trigger         TEXT NOT NULL,
  result          TEXT NOT NULL,
  stub_article_id TEXT REFERENCES articles(id) ON DELETE SET NULL,
  error           TEXT,
  model           TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_note_escalations_note         ON note_escalations(note_id);
CREATE INDEX IF NOT EXISTS idx_note_escalations_triggered_at ON note_escalations(triggered_at);

-- ─────────────────────────────── Personal OS Phase 3 ───────────────────────────────
-- Domains are user-defined strings. Projects are markdown files under projects/.
-- Tasks are inline markdown checkboxes in host files; this table is only a mirror.

CREATE TABLE IF NOT EXISTS domains (
  slug       TEXT PRIMARY KEY,
  name       TEXT NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  deleted_at DATETIME
);

CREATE INDEX IF NOT EXISTS idx_domains_active ON domains(slug) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS projects (
  slug             TEXT PRIMARY KEY,
  path             TEXT UNIQUE NOT NULL,
  name             TEXT NOT NULL,
  type             TEXT NOT NULL DEFAULT 'project',
  domain           TEXT,
  status           TEXT NOT NULL DEFAULT 'active',
  due              TEXT,
  last_activity_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  -- File-canonical project frontmatter mirrored by scan_projects.
  staleness_days   INTEGER,
  slipping_muted_until TEXT,
  slipping_muted   INTEGER NOT NULL DEFAULT 0,
  deleted_at       DATETIME,
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_projects_status ON projects(status);
CREATE INDEX IF NOT EXISTS idx_projects_domain ON projects(domain);
CREATE INDEX IF NOT EXISTS idx_projects_active ON projects(slug) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS tasks (
  uid              TEXT PRIMARY KEY,
  host_path        TEXT NOT NULL,
  line_number      INTEGER NOT NULL,
  text             TEXT NOT NULL,
  checked          INTEGER NOT NULL DEFAULT 0,
  status           TEXT NOT NULL,
  due              TEXT,
  scheduled        TEXT,
  priority         TEXT,
  domain           TEXT,
  project          TEXT,
  recurrence       TEXT,
  tags_json        TEXT NOT NULL DEFAULT '[]',
  links_json       TEXT NOT NULL DEFAULT '[]',
  needs_triage     INTEGER NOT NULL DEFAULT 0,
  last_activity_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  -- DB-primary task operator/capture controls are preserved across file scans;
  -- the markdown task line remains canonical for text/status/date/tag fields.
  staleness_days   INTEGER,
  slipping_muted_until TEXT,
  slipping_muted   INTEGER NOT NULL DEFAULT 0,
  reminder_lead_minutes INTEGER,
  no_reminder      INTEGER NOT NULL DEFAULT 0,
  review_at        TEXT,
  recurrence_materialized_key TEXT,
  recurrence_unparsed INTEGER NOT NULL DEFAULT 0,
  deleted_at       DATETIME,
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_tasks_due ON tasks(due);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_domain ON tasks(domain);
CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project);

-- ─────────────────────────────── Personal OS Phase 10 ───────────────────────────────
-- Project depth mirrors. Project markdown remains canonical.

CREATE TABLE IF NOT EXISTS milestones (
  project_slug TEXT NOT NULL,
  position     INTEGER NOT NULL,
  text         TEXT NOT NULL,
  done         INTEGER NOT NULL DEFAULT 0,
  deleted_at   DATETIME,
  created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(project_slug, position)
);

CREATE INDEX IF NOT EXISTS idx_milestones_project
  ON milestones(project_slug)
  WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS time_entries (
  project_slug TEXT NOT NULL,
  position     INTEGER NOT NULL,
  date         TEXT NOT NULL,
  hours        REAL NOT NULL,
  text         TEXT NOT NULL,
  deleted_at   DATETIME,
  created_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(project_slug, position)
);

CREATE INDEX IF NOT EXISTS idx_time_entries_project_date
  ON time_entries(project_slug, date)
  WHERE deleted_at IS NULL;

-- ─────────────────────────────── Personal OS Phase 4 ───────────────────────────────
-- Reminders are operational state, not markdown-canonical knowledge.

CREATE TABLE IF NOT EXISTS reminders (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type     TEXT,
  entity_id       TEXT,
  fire_at         DATETIME NOT NULL,
  lead_minutes    INTEGER,
  kind            TEXT NOT NULL,                       -- task_due | followup | daily_summary | custom
  status          TEXT NOT NULL DEFAULT 'pending',     -- pending | firing | sent | late | notify_failed | cancelled
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at DATETIME,
  last_error      TEXT,
  title           TEXT,
  body            TEXT,
  url             TEXT,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  fired_at        DATETIME,
  deleted_at      DATETIME
);

CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(status, fire_at);
CREATE INDEX IF NOT EXISTS idx_reminders_entity ON reminders(entity_type, entity_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_daily_summary_date
  ON reminders(kind, entity_id)
  WHERE kind = 'daily_summary' AND deleted_at IS NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_routine_missed_date
  ON reminders(kind, entity_id)
  WHERE kind = 'routine_missed' AND deleted_at IS NULL;

-- ─────────────────────────────── Personal OS Phase 5 ───────────────────────────────
-- Routine files remain markdown-canonical. Completions are high-write rows, but
-- every API mutation projects the completion list back into ## Completions.

CREATE TABLE IF NOT EXISTS routines (
  slug             TEXT PRIMARY KEY,
  path             TEXT NOT NULL,
  name             TEXT NOT NULL,
  description      TEXT,
  domain           TEXT,
  time_of_day      TEXT NOT NULL,
  specific_time    TEXT,
  notify           INTEGER NOT NULL DEFAULT 0,
  streak_type      TEXT NOT NULL DEFAULT 'ongoing',
  target_days      INTEGER,
  start_date       TEXT,
  archived         INTEGER NOT NULL DEFAULT 0,
  deleted_at       DATETIME,
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS routine_completions (
  routine_id       TEXT NOT NULL REFERENCES routines(slug) ON DELETE CASCADE,
  date             TEXT NOT NULL,
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(routine_id, date)
);

CREATE INDEX IF NOT EXISTS idx_routines_time ON routines(time_of_day);
CREATE INDEX IF NOT EXISTS idx_routines_archived ON routines(archived);
CREATE INDEX IF NOT EXISTS idx_routine_completions_date ON routine_completions(routine_id, date);

-- ─────────────────────────────── Personal OS Phase 11 ───────────────────────────────
-- Person files are markdown-canonical. Interactions are parsed from the
-- person's ## Interactions section.

CREATE TABLE IF NOT EXISTS people (
  slug                TEXT PRIMARY KEY,
  name                TEXT NOT NULL,
  birthday            TEXT,
  anniversary         TEXT,
  facts_json          TEXT NOT NULL DEFAULT '{}',
  follow_up_at        DATETIME,
  path                TEXT NOT NULL,
  deleted_at          DATETIME,
  last_interaction_at TEXT,
  created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at          DATETIME DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS interactions (
  person_slug TEXT NOT NULL REFERENCES people(slug) ON DELETE CASCADE,
  ts          TEXT NOT NULL,
  text        TEXT NOT NULL,
  created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(person_slug, ts, text)
);

CREATE INDEX IF NOT EXISTS idx_people_name ON people(name);
CREATE INDEX IF NOT EXISTS idx_people_active ON people(deleted_at, name);
CREATE INDEX IF NOT EXISTS idx_people_birthday ON people(birthday) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_interactions_person_ts ON interactions(person_slug, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reminders_followup_entity
  ON reminders(kind, entity_id)
  WHERE kind = 'followup' AND deleted_at IS NULL;

-- ─────────────────────────────── Personal OS Phase 12 ───────────────────────────────
-- Book and quote files are markdown-canonical. Highlights are parsed from each
-- book's ## Highlights section; every book highlight is also a linked quote.

CREATE TABLE IF NOT EXISTS books (
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
);

CREATE INDEX IF NOT EXISTS idx_books_status ON books(status);
CREATE INDEX IF NOT EXISTS idx_books_title ON books(title);
CREATE INDEX IF NOT EXISTS idx_books_active ON books(slug) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS book_highlights (
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
);

CREATE INDEX IF NOT EXISTS idx_book_highlights_book
  ON book_highlights(book_slug, position);

CREATE TABLE IF NOT EXISTS quotes (
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
);

CREATE INDEX IF NOT EXISTS idx_quotes_source ON quotes(source_type, source_ref);
CREATE INDEX IF NOT EXISTS idx_quotes_active ON quotes(id) WHERE deleted_at IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_quotes_source_hash
  ON quotes(source_type, COALESCE(source_ref, ''), content_hash)
  WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS quote_thoughts (
  quote_id   TEXT NOT NULL REFERENCES quotes(id) ON DELETE CASCADE,
  ts         TEXT NOT NULL,
  text       TEXT NOT NULL,
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(quote_id, ts, text)
);

CREATE INDEX IF NOT EXISTS idx_quote_thoughts_quote_ts
  ON quote_thoughts(quote_id, ts);

CREATE TABLE IF NOT EXISTS kindle_import_review (
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
);

CREATE INDEX IF NOT EXISTS idx_kindle_import_review_status
  ON kindle_import_review(status, created_at);

-- ─────────────────────────────── Personal OS Phase 13 ───────────────────────────────
-- Inventory item files are markdown-canonical. Photos are stored as optional
-- vault-relative paths; attachment upload lands in a later phase.

CREATE TABLE IF NOT EXISTS inventory (
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
);

CREATE INDEX IF NOT EXISTS idx_inventory_status ON inventory(status);
CREATE INDEX IF NOT EXISTS idx_inventory_location ON inventory(location);
CREATE INDEX IF NOT EXISTS idx_inventory_active ON inventory(id) WHERE deleted_at IS NULL;

-- ─────────────────────────────── Personal OS Phase 14 ───────────────────────────────
-- Content files are markdown-canonical creator workflow items. The mirror
-- powers list/kanban/filter views and draft spawning.

CREATE TABLE IF NOT EXISTS content_items (
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
);

CREATE INDEX IF NOT EXISTS idx_content_items_kind ON content_items(kind);
CREATE INDEX IF NOT EXISTS idx_content_items_status ON content_items(status);
CREATE INDEX IF NOT EXISTS idx_content_items_domain ON content_items(domain);
CREATE INDEX IF NOT EXISTS idx_content_items_active ON content_items(slug) WHERE deleted_at IS NULL;

-- ─────────────────────────────── Personal OS Phase 6 ───────────────────────────────
-- Journal day files are markdown-canonical. This table mirrors journal/*.md for
-- timeline and dashboard queries.

CREATE TABLE IF NOT EXISTS journal_days (
  date             TEXT PRIMARY KEY,
  path             TEXT UNIQUE NOT NULL,
  mood             INTEGER,
  energy           INTEGER,
  log_count        INTEGER NOT NULL DEFAULT 0,
  has_reflections  INTEGER NOT NULL DEFAULT 0,
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  updated_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  deleted_at       DATETIME
);

CREATE INDEX IF NOT EXISTS idx_journal_days_updated ON journal_days(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_journal_days_active ON journal_days(date) WHERE deleted_at IS NULL;

-- ─────────────────────────────── Personal OS Phase 8 ───────────────────────────────
-- Dashboard intelligence is DB-side derived/operator state. It never creates
-- markdown obligations; task/project/journal files remain canonical.

CREATE TABLE IF NOT EXISTS daily_focus (
  date       TEXT NOT NULL,
  task_uid   TEXT NOT NULL,
  position   INTEGER NOT NULL CHECK(position BETWEEN 1 AND 3),
  created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(date, position),
  UNIQUE(date, task_uid)
);

CREATE INDEX IF NOT EXISTS idx_daily_focus_date ON daily_focus(date, position);

CREATE TABLE IF NOT EXISTS slipping (
  entity_type TEXT NOT NULL,
  entity_id   TEXT NOT NULL,
  stale_since TEXT NOT NULL,
  computed_at DATETIME NOT NULL,
  PRIMARY KEY(entity_type, entity_id)
);

CREATE TABLE IF NOT EXISTS needs_review (
  id           INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type  TEXT NOT NULL,
  entity_id    TEXT NOT NULL,
  reason       TEXT NOT NULL,
  surfaced_at  DATETIME NOT NULL,
  dismissed_at DATETIME,
  UNIQUE(entity_type, entity_id, reason)
);

CREATE INDEX IF NOT EXISTS idx_needs_review_open
  ON needs_review(surfaced_at DESC)
  WHERE dismissed_at IS NULL;

-- ─────────────────────────────── Personal OS Phase 9 ───────────────────────────────
-- Google Calendar is read-only. These rows are cache only; Google remains the
-- source of record and Mastisk never calls mutating Calendar endpoints.

CREATE TABLE IF NOT EXISTS calendar_events (
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
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_start
  ON calendar_events(start, end);
CREATE INDEX IF NOT EXISTS idx_calendar_events_calendar
  ON calendar_events(calendar_id);

CREATE TABLE IF NOT EXISTS calendar_state (
  id             INTEGER PRIMARY KEY CHECK(id = 1),
  status         TEXT NOT NULL,
  last_synced_at TEXT,
  error          TEXT,
  last_error     TEXT,
  last_error_at  TEXT,
  updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- ─────────────────────────────── Roundtables ───────────────────────────────
-- A roundtable is one fan-out of a prompt to multiple LLMs + one synthesis.
-- Fully DB-stored (no filesystem artifact), because perspectives are transient
-- research output, not canonical user content.
-- See docs/superpowers/specs/2026-04-22-multi-llm-roundtable-design.md §5

CREATE TABLE IF NOT EXISTS roundtables (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  input_type       TEXT NOT NULL,       -- 'note' | 'article' | 'prompt'
  input_ref        TEXT NOT NULL,       -- stringified note_id | article_id | '' for free prompt
  prompt           TEXT NOT NULL,       -- the final prompt used
  status           TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
  synthesis        TEXT,
  synthesis_model  TEXT,
  error            TEXT,
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  finished_at      DATETIME,
  saved_as_note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_roundtables_created ON roundtables(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_roundtables_status  ON roundtables(status) WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_roundtables_input   ON roundtables(input_type, input_ref);

CREATE TABLE IF NOT EXISTS roundtable_perspectives (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  roundtable_id INTEGER NOT NULL REFERENCES roundtables(id) ON DELETE CASCADE,
  backend       TEXT NOT NULL,         -- 'claude' | 'codex' | 'gemini' | 'ollama'
  model         TEXT,
  content       TEXT,
  error         TEXT,
  latency_ms    INTEGER,
  started_at    DATETIME,
  finished_at   DATETIME
);

CREATE INDEX IF NOT EXISTS idx_roundtable_perspectives_rt ON roundtable_perspectives(roundtable_id);

-- ─────────────────────────────── Digest ranking ───────────────────────────────
-- Every article considered for a given daily digest is logged here with its
-- quality_score, interest_score, final_score, and whether it was selected.
-- This lets the digest UI explain "why did X show up today?" and lets us
-- audit the ranker's picks against user feedback (signals kind='liked'|'disliked')
-- after a week of use.

CREATE TABLE IF NOT EXISTS digest_candidates (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  digest_date     TEXT NOT NULL,                 -- ISO YYYY-MM-DD
  article_id      TEXT NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
  quality_score   REAL NOT NULL,
  interest_score  REAL NOT NULL,
  final_score     REAL NOT NULL,
  selected        INTEGER NOT NULL DEFAULT 0,     -- 1 if it made the digest
  rank            INTEGER,                        -- 1-indexed within that digest, NULL if not selected
  computed_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(digest_date, article_id)
);
CREATE INDEX IF NOT EXISTS idx_digest_candidates_date ON digest_candidates(digest_date DESC);
CREATE INDEX IF NOT EXISTS idx_digest_candidates_article ON digest_candidates(article_id);

-- ─────────────────────────────── GitHub ───────────────────────────────

CREATE TABLE IF NOT EXISTS repos (
  slug            TEXT PRIMARY KEY,
  source_type     TEXT NOT NULL DEFAULT 'github',   -- 'github' | 'local'
  owner           TEXT NOT NULL,
  name            TEXT NOT NULL,
  display_name    TEXT,
  description     TEXT,
  default_branch  TEXT,
  is_private      INTEGER NOT NULL DEFAULT 0,
  local_path      TEXT,                              -- for source_type='local'
  added_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
  last_polled_at  DATETIME,
  last_ideated_at DATETIME,
  context_md      TEXT,
  deleted_at      DATETIME
);

CREATE INDEX IF NOT EXISTS idx_repos_added ON repos(added_at DESC);
CREATE INDEX IF NOT EXISTS idx_repos_not_deleted ON repos(slug) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS repo_snapshots (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_slug         TEXT NOT NULL REFERENCES repos(slug) ON DELETE CASCADE,
  polled_at         DATETIME NOT NULL,
  latest_commit_sha TEXT,
  latest_commit_at  DATETIME,
  open_issues_count INTEGER,
  open_prs_count    INTEGER,
  stars_count       INTEGER,
  forks_count       INTEGER,
  commits_json      TEXT,
  issues_json       TEXT,
  prs_json          TEXT,
  readme_hash       TEXT,
  readme_excerpt    TEXT,
  error             TEXT
);

CREATE INDEX IF NOT EXISTS idx_repo_snapshots_repo ON repo_snapshots(repo_slug, polled_at DESC);

CREATE TABLE IF NOT EXISTS repo_idea_runs (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  repo_slug     TEXT NOT NULL REFERENCES repos(slug) ON DELETE CASCADE,
  ideated_at    DATETIME NOT NULL,
  snapshot_id   INTEGER REFERENCES repo_snapshots(id) ON DELETE SET NULL,
  note_ids_json TEXT,
  model         TEXT,
  error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_repo_idea_runs_repo ON repo_idea_runs(repo_slug, ideated_at DESC);

-- ─────────────────────────────── Blog posts ───────────────────────────────
-- User-triggered long-form drafts assembled from recent synthesis.
-- File in vault/blog/drafts/ is the source of truth for body_md; this row is
-- a derived index. See docs/superpowers/specs/2026-04-22-blog-writer-design.md

CREATE TABLE IF NOT EXISTS blog_posts (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  slug            TEXT UNIQUE,                   -- filename without .md; null until done (path is also null pre-done)
  path            TEXT UNIQUE,                   -- relative to vault root; null until done
  title           TEXT,                          -- null until status='done'
  theme           TEXT NOT NULL DEFAULT '',      -- '' when no theme was given
  window_days     INTEGER NOT NULL,              -- 7 | 14 | 30 | 90
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
  model           TEXT,                          -- 'claude' | 'ollama' — populated at done
  tags_json       TEXT DEFAULT '[]',             -- JSON array of tags from Claude's output
  word_count      INTEGER,                       -- populated at done (len(body_md.split()))
  body_preview    TEXT,                          -- first 400 chars of the draft, for list views
  error           TEXT,                          -- populated iff status='failed'
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  finished_at     DATETIME,
  saved_as_note_id INTEGER REFERENCES notes(id) ON DELETE SET NULL,
  deleted_at      DATETIME                       -- tombstone; file also unlinked
);

CREATE INDEX IF NOT EXISTS idx_blog_posts_created ON blog_posts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_blog_posts_status  ON blog_posts(status)
  WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_blog_posts_not_deleted ON blog_posts(id) WHERE deleted_at IS NULL;

-- External-content FTS5 over blog posts. body_md lives in the vault file (not
-- the row), so we index the columns we have: title, theme, and body_preview
-- (first ~400 chars). Good enough for the command palette's narrow-as-you-type
-- experience; full-body search would require streaming files at query time.
CREATE VIRTUAL TABLE IF NOT EXISTS blog_posts_fts USING fts5(
  title, theme, body_preview,
  content='blog_posts', content_rowid='id'
);

CREATE TRIGGER IF NOT EXISTS blog_posts_ai AFTER INSERT ON blog_posts BEGIN
  INSERT INTO blog_posts_fts(rowid, title, theme, body_preview)
    VALUES (new.id, new.title, new.theme, new.body_preview);
END;
CREATE TRIGGER IF NOT EXISTS blog_posts_ad AFTER DELETE ON blog_posts BEGIN
  INSERT INTO blog_posts_fts(blog_posts_fts, rowid, title, theme, body_preview)
    VALUES ('delete', old.id, old.title, old.theme, old.body_preview);
END;
-- Same rationale as notes_au: blog_posts get UPDATEd on status transitions
-- (pending → running → done|failed) and saved_as_note_id mutations that
-- don't touch any indexed column. WHEN gates the re-index to real changes.
CREATE TRIGGER IF NOT EXISTS blog_posts_au AFTER UPDATE ON blog_posts
WHEN old.title IS NOT new.title
  OR old.theme IS NOT new.theme
  OR old.body_preview IS NOT new.body_preview
BEGIN
  INSERT INTO blog_posts_fts(blog_posts_fts, rowid, title, theme, body_preview)
    VALUES ('delete', old.id, old.title, old.theme, old.body_preview);
  INSERT INTO blog_posts_fts(rowid, title, theme, body_preview)
    VALUES (new.id, new.title, new.theme, new.body_preview);
END;

-- Citation ledger: one row per (blog_post, source item) considered by the agent.
-- used=1 means cited in the draft; used=0 means offered but Claude didn't pick it.
-- Keeping used=0 rows lets us debug ranking + train better selection later.
CREATE TABLE IF NOT EXISTS blog_post_sources (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  blog_post_id  INTEGER NOT NULL REFERENCES blog_posts(id) ON DELETE CASCADE,
  kind          TEXT NOT NULL,                   -- 'note' | 'article' | 'roundtable' | 'content'
  ref           TEXT NOT NULL,                   -- stringified (note_id | article_id | roundtable_id)
  rank          INTEGER NOT NULL,                -- N in `[source N]` — 1-indexed, matches the Sources block
  used          INTEGER NOT NULL DEFAULT 0,      -- 1 if cited in body, 0 if offered-but-unused
  origin        TEXT                             -- 'repo_ideator' if the note came from GithubIdeator; else null
);

CREATE INDEX IF NOT EXISTS idx_blog_post_sources_post ON blog_post_sources(blog_post_id);
CREATE INDEX IF NOT EXISTS idx_blog_post_sources_ref ON blog_post_sources(kind, ref);

-- ─────────────────────────────── Tweet threads ───────────────────────────────
-- User-triggered short-form thread drafts assembled from recent local work plus
-- optional live web/browser context. Unlike blog_posts, the full thread is small
-- enough to live directly on the row as JSON.

CREATE TABLE IF NOT EXISTS tweet_threads (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  title           TEXT,
  angle           TEXT,
  theme           TEXT NOT NULL DEFAULT '',
  url             TEXT,
  window_days     INTEGER NOT NULL,
  include_web     INTEGER NOT NULL DEFAULT 1,
  use_browser_context INTEGER NOT NULL DEFAULT 0,
  status          TEXT NOT NULL DEFAULT 'pending',  -- pending | running | done | failed
  model           TEXT,
  thread_json     TEXT DEFAULT '[]',                -- JSON array of tweet strings
  sources_json    TEXT DEFAULT '[]',                -- local/web/browser evidence used
  warnings_json   TEXT DEFAULT '[]',
  error           TEXT,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  finished_at     DATETIME,
  deleted_at      DATETIME
);

CREATE INDEX IF NOT EXISTS idx_tweet_threads_created ON tweet_threads(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweet_threads_status ON tweet_threads(status)
  WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_tweet_threads_not_deleted ON tweet_threads(id) WHERE deleted_at IS NULL;

CREATE TABLE IF NOT EXISTS tweet_thread_feedback (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  tweet_thread_id      INTEGER NOT NULL REFERENCES tweet_threads(id) ON DELETE CASCADE,
  target_tweet_index   INTEGER,                 -- null = whole thread, 0-based tweet index otherwise
  body                TEXT NOT NULL,
  created_at          DATETIME DEFAULT CURRENT_TIMESTAMP,
  applied_at          DATETIME
);

CREATE INDEX IF NOT EXISTS idx_tweet_thread_feedback_thread
  ON tweet_thread_feedback(tweet_thread_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_tweet_thread_feedback_pending
  ON tweet_thread_feedback(tweet_thread_id, created_at)
  WHERE applied_at IS NULL;

-- ─────────────────────────────── Topic suggestions ───────────────────────────────
-- Topic suggestions surfaced by topic_suggester (kind='daily') and
-- opinion_gap_miner (kind='opinion'). The agent runs on a cadence and
-- writes 1-2 suggestion rows per run. The UI reads non-dismissed,
-- non-used rows and offers them as one-click blog drafts.
CREATE TABLE IF NOT EXISTS topic_suggestions (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  kind             TEXT NOT NULL,                          -- 'daily' | 'opinion'
  title            TEXT NOT NULL,                          -- short noun-phrase, fills theme field
  hook             TEXT NOT NULL,                          -- 1-2 sentence framing the user reads first
  angle            TEXT,                                   -- optional: the angle/argument the writer should take
  source_refs_json TEXT NOT NULL DEFAULT '[]',             -- list of {kind: 'note'|'article'|'roundtable', ref: int|str}
  created_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  dismissed_at     DATETIME,                               -- user-clicked-dismiss, null if active
  used_blog_id     INTEGER REFERENCES blog_posts(id) ON DELETE SET NULL  -- set when user drafts a post from this
);

CREATE INDEX IF NOT EXISTS idx_topic_suggestions_active
  ON topic_suggestions(created_at DESC)
  WHERE dismissed_at IS NULL AND used_blog_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_topic_suggestions_kind_created
  ON topic_suggestions(kind, created_at DESC);

-- Cadence backstop: collapses exact-duplicate (kind, day, title) rows.
-- The cadence guard in run_once already prevents the agent from running
-- twice in 22h within one process; this defends against multi-process
-- races (user runs `mastisk` CLI alongside the daemon) by deduping by
-- title. Two genuinely-different topics in the same batch get distinct
-- titles and both insert; an LLM hallucinating the same title twice or
-- a racing process landing the same title gets collapsed.
CREATE UNIQUE INDEX IF NOT EXISTS idx_topic_suggestions_dedup
  ON topic_suggestions(kind, date(created_at), title);
