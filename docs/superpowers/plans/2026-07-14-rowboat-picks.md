# Rowboat picks — memory quality + proactive surfaces + prose agents

Branch: `feature/rowboat-picks`. Source of the ideas: code exploration of
rowboatlabs/rowboat (`apps/x/packages/core/src`). Six features, built in order.
Baseline before any change: 872 passed, 2 pre-existing failures
(`test_e2e_smoke.py::test_e2e_watch_capture_task_due_reminder_and_filters`,
`test_routines.py::test_routine_routes_create_toggle_progress_and_archive_file_first`
— both date-sensitive, hardcode June 2026). Gate: zero NEW failures.

## 1. Dated facts + supersession (foundation)

Convention: `- (YYYY-MM-DD) fact text`, superseded in place with
`(previously <old> as of <date>)`. Never silently drop an old value.

- `src/mastisk/memory_conventions.py` — NEW: `DATED_FACT_RE`,
  `parse_dated_facts(md_section) -> list[DatedFact]`,
  `format_dated_fact(date, text)`, `supersede_fact(old_line, new_text, today)`,
  plus `DATED_FACTS_PROMPT` — a short prompt block stating the convention,
  importable by any agent prompt.
- Compiler: article schema gains a `key_facts` list (date + text); rendered as
  `## Key facts` section with dated bullets. Prompt block added to `SCHEMA_MD`.
- Escalator research prompt: same block.
- People: `append_interaction` unchanged (already dated); `facts` dict rendering
  in `dump_person_file` untouched (free-form).
- PWA: article view renders `## Key facts` bullets with the date visually
  distinguished (`.fact-date` span) — done in the markdown renderer.

## 2. Write-time pollution gates + suggested-topics queue

Rowboat's lesson: memory quality comes from refusing to write; new topics never
auto-mint canonical notes — they land on a curated shortlist.

- DB: `suggested_topics` table (id, slug, title, rationale, kind
  concept|entity, source_ids_json, occurrences, status
  pending|promoted|dismissed, created_at, decided_at) in schema.sql.
- Vault mirror: `vault/_suggestions/suggested-topics.md` — one file, shortlist
  section rendered from DB (write-through, not file-first: suggestions are
  machine-derived, unlike people/projects).
- Compiler `_extract_link_refs`/stub path: a wikilink target that does NOT
  resolve to an existing article no longer calls `ensure_stub_article`
  directly. Deterministic gate in code (Rowboat pattern — critical gate lives
  outside the LLM): mint the stub only if the same target was referenced from
  ≥2 distinct sources (`stub_gate_min_sources`, default 2, config
  `[compiler]`); otherwise upsert into `suggested_topics` (occurrences++).
  Second source arriving later → gate passes → stub minted + suggestion row
  marked promoted.
- Topic Suggester: unchanged (it already writes its own table) but its output
  merges into the same PWA queue view.
- Routes: `routes/suggestions.py` — list/promote/dismiss. Promote = ensure
  stub article + enqueue compiler enrich job (same as escalator's path).
- PWA: "Suggestions" sidebar entry + queue view (promote/dismiss buttons,
  occurrence count, rationale, source links).
- Config: `[compiler] stub_gate_min_sources = 2`; escape hatch 1 = old
  behavior.

## 3. Gardener agent (consolidation + reflection; the M2 slot)

- `agents/gardener.py` — NEW Agent, timer-driven (copy topic_suggester
  pattern): `tick_seconds=3600`, daily cadence gate via its own run log table.
- Selection (deterministic, in code): articles + entity stubs where
  `length(body) > threshold OR section count > N`, plus notes-heavy entity
  pages; qualify: modified since `curated_at`, ≥8 accumulated
  updates/backlinks, 7-day per-page cooldown; max 8 pages/run, most-bloated
  first. `curated_at` column on articles (migration) + frontmatter stamp in
  vault mirror.
- Prompt `GARDENER_PROMPT` (Studio slot, primary): adapted Rowboat quality
  contract — no new facts; no deleted substance; keep title/slug; ~150-line
  target; last-60-days verbatim, older collapsed month-by-month; promotion of
  cross-source patterns into dated Key facts; unsupported inferences
  downgraded to dated observations; stale future-tense → past tense with
  absolute dates; open items >45d → Dormant; contradictions
  newest-wins-with-history; preserve every [[wikilink]]; supersession
  convention from memory_conventions.
- Learnings promotion: gardener may emit `learnings` list in its JSON reply →
  appended (dated) to `vault/_self/learnings.md` (the documented M2
  behavior).
- Output: full replacement article JSON (reuse compiler's
  `_normalize_article_data` + `upsert_article` + vault mirror), `curated_at`
  stamped, feed event emitted. Budget: `[gardener] daily_page_cap = 8`,
  enabled flag; registry spec so Studio can edit the prompt.

## 4. Meeting prep (calendar × People)

- `google_calendar.py::_normalize_event`: persist `attendees` (email,
  displayName, responseStatus, self) — new `attendees_json` column on
  `calendar_events` (migration) + `_upsert_calendar_event` + `_event_response`.
- People: first-class `email` frontmatter field (parse + dump + column +
  migration + `find_person_by_email(email)` — exact, casefolded; fallback scan
  of `facts.email` for legacy files).
- `agents/meeting_prep.py` — scheduled interval job (15 min): events starting
  within `lead_hours` (default 6) and not yet prepped (`meeting_prep_state`
  table keyed by event id + start) and with ≥1 non-self attendee. Deterministic
  assembly (NO LLM): resolve attendees by email → People notes (full body),
  org grouping by email domain (skip user's own domains, config), prior
  interactions, open follow-ups. Brief via `run_intelligence` with Rowboat's
  contract ("Use ONLY the context provided… 3-5 bullets… if thin, say so").
  Brief best-effort: prep note still written if LLM unavailable.
- Output: `vault/_notes/meetings/prep/<slug>-<date>.md` + notes-table row so
  it shows in PWA; Today view gets a prep card (brief + attendee links) via
  `GET /api/calendar/today` extension (`prep` field per event).
- Config: `[calendar] prep_enabled=true, prep_lead_hours=6, own_domains=[]`.

## 5. Feedback distillation

- DB: `feedback_corrections` (id, subject_kind article|scout_item, subject_id,
  verdict up|down, reason, created_at, distilled_at NULL).
- Routes: `routes/feedback.py` POST verdict (+optional reason), GET recent.
- PWA: thumbs up/down on article view (+ optional one-line reason popover);
  down on a scout-sourced article asks "why" inline.
- Distiller: part of gardener's daily tick (cheap): if ≥N undistilled
  corrections (default 6), one `run_intelligence` call distills them into
  short imperative rules appended (dated) to `vault/_self/learnings.md`
  under `## Preference rules`; corrections stamped `distilled_at`. Rules ride
  into every prompt free via `load_identity()`. Scout addition: literal
  "not interested in X"-style rules file check — Scout loads
  `## Preference rules` lines as additional dislike patterns when prefixed
  `avoid:` (deterministic, no embedding change).
- Config: `[feedback] distill_every = 6`.

## 6. Prose-defined background tasks

- Vault: `vault/_agents/tasks/<slug>/` with `task.yaml` (name, instructions,
  active, triggers {cron, windows[{start,end}]}, model?, created_at +
  runtime-managed last_attempt_at/last_run_at/last_run_summary/
  last_run_error) and `index.md` (agent-owned). Runs log:
  `bg_task_runs` DB table (id, slug, trigger, started_at, finished_at,
  summary, error) — not jsonl files (SQLite is the index; keeps PWA simple).
  File-first sync module `bgtasks/sync.py` following people/sync.py pattern
  (scan/parse/dump/create/patch; task.yaml is YAML not frontmatter-markdown —
  parse with yaml.safe_load directly).
- Scheduler: `bg_task_tick` interval job (60s): for each active task compute
  due (cron via croniter-style check against last_run_at — implement minimal
  5-field cron matcher in code, no new dep unless croniter already available;
  windows = fire once/day inside band), honor 5-min backoff from
  last_attempt_at on failure. Sequential execution, max 1 in flight.
- Runner `bgtasks/runner.py`: builds headless prompt (adapted Rowboat
  instructions: no user present; OUTPUT vs ACTION from verbs; journal
  format; no fabrication — skip run and say why; 1-2 sentence data-point
  summary), gives the task's `index.md` current content + instructions +
  trigger info; calls `run_intelligence` expecting JSON
  {mode, new_index_md?, journal_line?, summary}; runner applies the write
  (atomic_write + scan) — the LLM never writes files directly (Mastisk
  idiom, and it can't clobber task.yaml by construction). Guard: runner
  refuses to run when instructions mention managing background tasks
  themselves. Feed events + optional notify when instructions say notify.
- Budget: `[bgtasks] daily_run_cap = 24` global.
- Routes `routes/bgtasks.py`: CRUD + run-now + runs list + toggle active.
- PWA: "Agents" sidebar entry → task list (name, active toggle, last run
  summary/error, next-due hint) + detail (instructions editor, index.md
  rendered, runs table, Run now).

## 7. UI polish pass

Load /make-interfaces-feel-better + /frontend-design + /ui-ux-pro-max; apply to
suggestion queue, Today prep cards, feedback affordances, bg-tasks manager.
Both themes, PWA-responsive, existing OKLCH token idiom.

## 8. Verification

Full pytest (gate: no new failures vs baseline) + `npm run build` + frontend
static tests + end-to-end: run daemon against tmp home, exercise each feature
with fake LLM patched; then real dev-instance smoke via browser. Dual review by
2 independent subagents; iterate until clean.
