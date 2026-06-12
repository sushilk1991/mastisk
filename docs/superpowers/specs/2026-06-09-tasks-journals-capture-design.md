# Personal OS — Tasks, Journals, Routines, Library & Multi-Directional Capture — Design Spec

**Status:** Draft for review (2026-06-09) · **v2** (expanded to full life-management scope after analysis of the Jerad Hill "manage my entire life in Claude Code" reference build)
**Author:** Mastisk team (researched + drafted with Claude Opus 4.8)
**Scope:** Turns Mastisk from a knowledge-wiki engine into a complete personal operating system — capture, tasks, projects, routines, journal, reminders/notifications, a personal CRM, a reading/quotes library, inventory, and a content pipeline — by extending the existing notes capture spine rather than building a parallel app. Sister spec: `docs/superpowers/specs/2026-04-21-notes-subsystem-design.md` (the spine this builds on). See `project_mastisk_vision` memory.

> **Architectural stance (read first).** This is a **local-first** build: vault markdown files are the source of truth, SQLite is a derived index, the daemon runs on the user's Mac. The reference video uses a cloud Node+Supabase app, which gets always-on push/calendar/reachability "for free." We deliberately keep local-first (per the user's requirement) and pay for it with one honest constraint: **time-based features (reminders, calendar sync, scheduled pushes) only fire while the daemon is running.** §11 addresses this directly. Every feature below is designed to degrade gracefully when the Mac is asleep, not to pretend it's a 24/7 server.

---

## 1. Context & Goals

Mastisk already has an inbound capture spine: a thought lands in `vault/_notes/inbox/`, the **Notetaker** classifies it (Claude, with confidence), the **Escalator** promotes high-value notes into wiki stubs, the **Compiler/Synthesizer** make articles. Classification already emits `todo` (`notetaker.py:44`). Local audio transcription exists (`mlx-whisper`, `listener.py`).

This subsystem widens that spine into a full personal OS. The same pipeline routes a freeform capture (often a transcribed voice note) into the **right typed destination**, and we add the typed objects a life-management tool needs: tasks, projects/areas/retainers, routines/streaks, journal, reminders, people, books, quotes, inventory, and content items. The wiki article becomes **one consumption view among many** — the "multi-directional" requirement.

**Core principle: one capture spine, many typed destinations, all local-first markdown.** No second app, no competing database. Every capture path (PWA, CLI, vault-drop, Apple Watch, phone) feeds a single ingress that classifies intent and files a typed markdown object, mirrored to SQLite for query. Every typed object is a markdown file editable in Obsidian/Files.

**Goals (v2):**

1. **Frictionless capture** from the wrist (Apple Watch), phone, desktop, and any editor — voice or text — auto-routed by AI.
2. **Active, not passive.** Tasks and routines fire **reminders + push notifications**; a daily summary lands each morning; nothing logged "goes there to die."
3. **Typed objects, still plain markdown.** Tasks (inline), projects/areas/retainers, routines (with streaks), journal (daily), people (CRM), books + quotes (reading library), inventory, content items — all files, all mirrored.
4. **A smart dashboard.** A "Today" view (top-3 focus, due tasks, calendar, routines), a **"Slipping"** neglect detector, daily **"Resurfacing"** inspiration, and an AI **"Needs review"** queue.
5. **Calendar awareness.** Read-only Google Calendar merged into Today.
6. **Multi-directional consumption + RAG.** One atom surfaces across task list, project page, journal timeline, wiki graph, search, and the existing "Ask" chat.
7. **Compounding preserved.** Nothing terminal: a quote links a book, a task references a project, a note escalates to an article, a journal day stitches the lot.

**Success criteria:** see each subsystem section; the headline ones —
- Watch: "schedule a task for home to change the water filter at 2pm tomorrow, remind me" → a task with `area=home`, `due=tomorrow 14:00`, a reminder set, visible in Today within 60s and pushed at the reminder time (daemon running).
- Routines: a "morning vitamins" routine shows in Today's morning group, toggling it advances the streak and updates the progress graph.
- Library: syncing Kindle highlights creates a book with its highlights as linked quotes, each able to carry threaded thoughts.

---

## 2. Non-Goals (v1 of this expanded scope)

- **Raw audio push from the Apple Watch.** Not possible natively on watchOS (§8). Wrist path = dictated *text*. Phone-audio→local-Whisper is a later optional path.
- **Two-way calendar.** Google Calendar is **read-only** (display/merge). We do not create/edit Google events; the user keeps Google as the calendar of record.
- **Real-time collaboration / CRDT sync.** Solo user, Mac + iPhone; iCloud + atomic writes + FSEvents suffices (§11). CRDT explicitly rejected.
- **A native app.** PWA + Shortcut is the mobile path.
- **A 24/7 hosted server.** Local-first by design. If always-on push becomes a hard requirement, §11 sketches a minimal relay — but it is not v1.
- **Automated bookkeeping/invoicing** off time-tracking. We record hours; we don't generate invoices.

(Calendar read-sync and a recurring-task engine were non-goals in v1 of this spec; v2 promotes both into scope.)

---

## 3. Architecture

```
Capture surfaces          ingress + router            typed destinations (vault, md = truth)        engines (scheduler)         consumers
────────────────          ────────────────            ──────────────────────────────────────        ──────────────────         ─────────
Apple Watch ─(Dictate)─┐                              ┌ task   → journal/ or projects/<slug>.md      ┌ reminder_tick (fire      ┌ Today dashboard
  Cloudflare Tunnel    │  POST /api/capture           │         + tasks mirror                       │   due reminders → push)   │  (top-3, due,
iPhone PWA ─(Tailscale)┤  (bearer token)              ├ journal→ journal/YYYY-MM-DD.md                ├ daily_summary (AM push)  │   calendar, routines,
Desktop ⌘-capture ─────┤   capture_router:            ├ note   → _notes/inbox/ (existing pipeline)   ├ routine_rollover         │   slipping, resurface,
CLI / vault-drop ──────┤    1 command detect          ├ project_update → projects/<slug>.md          ├ retainer_rollover (mo.)  │   needs-review)
                       │    2 intent classify         ├ routine_done → routines/<slug>.md            ├ calendar_sync (poll GCal)│ Tasks / Projects /
Document upload ───────┤    3 extract fields          ├ person/interaction → people/<slug>.md        ├ slipping_scan            │  Routines / Journal /
Kindle / journal photo─┘    4 confidence gate         ├ quote/book → library/...                     └ needs_review_scan (AI)   │  Library / People /
                            5 reminder defaults        ├ inventory → inventory/...                                               │  Content / Inventory
                                                       └ content → content/<slug>.md                  notifier: Pushover/ntfy    │ Search · Ask (RAG)
```

**Storage model (unchanged — Pattern 3 hybrid):** markdown canonical, SQLite derived. Every new entity type follows the `notes` pattern: file is truth, row reproducible.

**Domains/areas are user-defined**, not a fixed enum (the reference build has "Field Notes / Hill Media Group / YouTube channels / photography" as top-level domains). `domain` (a.k.a. area) is the highest grouping; projects, tasks, and routines hang off it. Stored in config + a `domains` table.

**The router is additive.** Note/inbox use the existing spine untouched; new types are new destinations on the same `jobs` queue. New scheduler engines are added with the established `try/except import + sched.add_job` pattern.

---

## 4. Entity Catalog

| entity | vault location | mirror table(s) | one-line purpose |
|---|---|---|---|
| note | `_notes/…` (existing) | `notes` | freeform knowledge; escalates to wiki |
| task | inline in host file | `tasks` | an action; due/priority/recurrence/reminders |
| project / area / retainer | `projects/<slug>.md` | `projects`, `milestones`, `time_entries` | unit of work; areas ongoing, retainers monthly-recurring |
| routine | `routines/<slug>.md` | `routines`, `routine_completions` | habit/streak; morning/afternoon/evening |
| journal day | `journal/YYYY-MM-DD.md` | `journal_days` | daily log; tasks + captures + reflections + media |
| reminder | (field on host entity) | `reminders` | a scheduled push tied to a task/routine/follow-up |
| domain | config + file optional | `domains` | top-level life area |
| person | `people/<slug>.md` | `people`, `interactions` | personal CRM: facts + interaction log |
| book | `library/books/<slug>.md` | `books`, `book_highlights` | reading tracker + Kindle highlights |
| quote | `library/quotes/<id>.md` | `quotes`, `quote_thoughts` | saved quote w/ threaded thoughts |
| inventory item | `inventory/<id>.md` | `inventory` | possession w/ photo, value, status |
| content item | `content/<slug>.md` | `content_items` | video/article/podcast w/ status pipeline |
| calendar event | (cache only) | `calendar_events` | read-only Google Calendar mirror |
| daily focus | (derived) | `daily_focus` | the top-3 tasks for a date |

Domains/areas/tags/wikilinks cross-cut everything, which is what makes the same atom surface in many views.

---

## 5. Capture → Route → Store

Intent types: `task · note · journal · project_update · routine_done · person · quote · inventory · content · inbox`.

| intent | destination | example |
|---|---|---|
| `task` | inline `- [ ]` in today's journal or matched project | "follow up with Anjali Thursday, remind me" |
| `note` | `_notes/inbox/` → existing pipeline | "test-time compute trades cycles for accuracy" |
| `journal` | append `journal/YYYY-MM-DD.md` `## Log` | "felt scattered today" |
| `project_update` | append `projects/<slug>.md` `## Log` | "shipped the capture endpoint" |
| `routine_done` | mark a routine complete for today | "did my vitamins" |
| `person` | `people/<slug>.md` fact or interaction | "Sam's daughter started college" |
| `quote` | `library/quotes/<id>.md` | "save this quote from the podcast: …" |
| `inventory` | `inventory/<id>.md` | "add my new monitor to inventory" |
| `content` | `content/<slug>.md` | "new video idea: local-first PKM" |
| `inbox` | `_notes/inbox/` + `needs_triage` | anything below the confidence floor |

**Command override** (higher precision than classification): "remind me to…", "log/journal that…", "add to the <X> project…", "save a quote…", "add <thing> to inventory…", "new video idea…", "did my <routine>". When matched, intent is fixed; only field extraction runs.

**Reminder default rule** (from the reference build): when the router files a `task` with a due time and the user didn't say "no reminder," it **auto-creates a reminder** per `[reminders] default_lead_minutes` — unless the text says "no reminder/don't remind." This is the "active, not passive" behavior. Configurable; default on.

**AI cleanup:** the router returns `body` as cleaned text (filler/ums removed, rewritten to a clear line) — same single structured call, the `body` field.

---

## 6. Intent Router

New module `src/mastisk/capture/router.py` + an agent hand-off mirroring Notetaker. One structured-output Claude call (reusing `claude_bridge.extract_json_block` + `Agent.load_identity()`, with the user's domains/projects/people-names injected so the model can match existing entities).

```python
class Capture(BaseModel):
    type: Literal["task","note","journal","project_update","routine_done",
                  "person","quote","inventory","content","inbox"]
    confidence: float
    title: str | None
    body: str                       # cleaned text
    domain: str | None              # user-defined area
    project: str | None             # existing project slug
    person: str | None              # existing person slug (for person/interaction)
    due: str | None                 # ISO 8601 (resolved server-side, §6.1)
    scheduled: str | None
    priority: Literal["high","medium","low"] | None
    recurrence: str | None          # natural language, verbatim
    reminder_lead_minutes: int | None  # explicit "remind me N before"
    no_reminder: bool               # user said don't remind
    review_at: str | None           # "revisit this later" → needs-review queue
    tags: list[str]
    related: list[str]              # → wikilinks
```

**Confidence gate:** `≥0.85` file directly; `0.5–0.85` file + `needs_triage` (one-tap reclassify in the dashboard); `<0.5` → inbox raw. Command-detected captures skip the gate.

**§6.1 Relative-date resolution is code, not LLM** (Rule 5). The model returns its best ISO guess; a deterministic normalizer reconciles "Thursday/tomorrow/next week" against the request `ts` (server tz from `[capture] default_timezone`). `ts` is sent by the Watch shortcut from day one.

---

## 7. Reminders & Push Notifications (NEW)

The single biggest "make it active" feature. Two parts: **reminders** (scheduled triggers) and a **notifier** (the push channel).

**Reminders model.** A `reminders` row references a host entity (task, routine, or person follow-up): `{id, entity_type, entity_id, fire_at, lead_minutes, kind, status}`. Kinds: `task_due`, `routine_missed`, `followup`, `daily_summary`, `custom`. Created by the router's default rule, by explicit "remind me," by routine config, or by a person follow-up.

**Notifier.** A `src/mastisk/notify/` module with pluggable backends:
- **Pushover** (reference build's choice): one HTTPS POST (`token`, `user`, `message`, optional `priority`, `url`). App on phone/watch; reliable; ~free.
- **ntfy.sh** (recommended local-first alt): POST to a topic URL; self-hostable; no account.
- **APNs / native PWA Web Push** (later; more setup, fully self-owned).
Backend + creds in config; the module is a thin `send(title, body, url=None)` over the chosen backend.

**Engines (scheduler jobs):**
- `reminder_tick` (≈60s): fire `reminders` with `fire_at <= now AND status='pending'` → `notifier.send` → mark `sent`. Idempotent; survives restart (state in DB).
- `daily_summary` (configurable AM time): compose today's top-3 + due tasks + routines + slipping count → one push.
- `routine_missed`: when a routine's time-of-day window passes uncompleted, optionally nudge.

**Honest reliability constraint (§11):** these fire only while the daemon runs. A reminder whose `fire_at` passed while the Mac slept fires on next wake (late, but not lost) — and we mark it `late` so the user knows. If on-time delivery while-asleep is required, the only real fixes are (a) keep the Mac awake / use a small always-on box, or (b) a minimal cloud relay that holds the reminder queue. Documented, not pretended away.

---

## 8. Apple Watch Capture Path

**Honest constraint (verified, 2026):** raw audio push from the Watch is a watchOS dead end (Apple discourages mic actions; `SFSpeechRecognizer` absent on watchOS; no Shortcuts path to capture+POST an audio file). The viable wrist path — and the user's chosen priority — is **on-watch Dictate Text → POST text**. Transcription is Apple's cloud dictation, not local Whisper: a deliberate, accepted tradeoff.

**Shortcut:** Dictate Text → `DictatedText` → Get Contents of URL: `POST https://capture.<domain>/api/capture`, header `Authorization: Bearer <token>`, JSON `{text, source:"watch", ts}`; optional Show Notification echoing the response `type`/`destination` ("filed as task → home"). Trigger via watch-face **complication** (tap→confirm→speak, ~5s) or Siri.

**Networking gotcha:** the Watch is **not** a Tailscale client → it can't reach the tailnet host. Use **Cloudflare Tunnel** scoped to `/api/capture` (Watch enforces ATS → real TLS required; Cloudflare provides it). iPhone path keeps Tailscale.

**Security (defense in depth):** (1) app-level bearer token on `/api/capture*`, constant-time compare, token in `config.toml` 0600 — the load-bearing control; (2) network-level: tunnel fronts only the capture surface (Cloudflare Access policy or path-scoped ingress). Mandated in the Phase-1 plan.

**Fallbacks (designed, later):** iPhone audio → `/api/capture/audio` → `mlx-whisper` (local-first, longer-form); Voice-Memo relay (last resort, 10–90s latency).

---

## 9. Subsystems

### 9.1 Tasks
Inline Obsidian-Tasks syntax (`- [ ] text 📅due ⏳sched 🔁recur ⏫prio #tag [[link]] 🆔uid`) in a host file (journal/project/note); `tasks` mirror for query. **Fields beyond v1:** `remind_offsets`, `is_top3`/daily focus, `last_activity_at` (for Slipping), `review_at` (Needs-review), `recurrence` now backed by a real engine (§9.4). Toggle in dashboard rewrites the host line; edit in Obsidian flips the row on rescan. Default host for wrist-captured tasks = today's journal `## Tasks`, else the matched project.

### 9.2 Projects / Areas / Retainers
`projects/<slug>.md`, `type ∈ {project, area, retainer}`, `domain`, `status ∈ {active, someday, paused, done}`, `due`. New depth (from the reference build):
- **Milestones** — `## Milestones` checklist + `milestones` table → % complete.
- **Checklists + templates** — reusable checklist templates in `templates/checklists/<name>.md`, applied on project creation (the "new website" checklist: domain access, hosting, install, …).
- **Time tracking** — `## Activity` log entries with durations; `time_entries` table sums to hours per project.
- **Retainers** — `type=retainer` holds `recurring_items` (tasks/checklists) that `retainer_rollover` re-materializes at month start, **carrying overdue** items forward.

### 9.3 Routines & Streaks (NEW)
`routines/<slug>.md`: frontmatter `{name, description, domain, time_of_day ∈ morning|afternoon|evening|anytime, specific_time?, notify?, streak_type ∈ ongoing|fixed, target_days?}` + a `## Completions` date log. Mirror: `routines` + `routine_completions`. Dashboard shows routines grouped by time-of-day; toggling writes today's completion (file + row). **Streaks** derived from completion dates; **fixed challenges** (e.g., "run 5K daily for 30 days starting June 1") track target vs done; **archive** completed streaks; **progress bar graph** = completions over time. Missed-window items can nudge via §7.

### 9.4 Recurrence Engine (NEW — promoted from deferred)
On task completion (or a nightly `recurrence_tick`), a task with `recurrence` materializes its next instance (next due per the rule, status reset). Natural-language rules parsed deterministically (`every Monday`, `every 2 weeks`, `monthly`). Retainer recurring items use the same engine, month-scoped.

### 9.5 Journal
`journal/YYYY-MM-DD.md`, append model, optional `mood/energy` frontmatter, sections `## Tasks / ## Log / ## Reflections`, **photos + short video clips** via attachments (§10 editor phase). Assembly point: dashboard renders the day file + a live query of tasks/routines/events for that date. **Handwritten-journal OCR (later):** `POST /api/ingest/journal-photo` → vision model (Claude API image input) extracts insights → appends to the day under `## Log` with a `source: handwritten` marker.

### 9.6 People / Personal CRM (NEW)
`people/<slug>.md`: structured `facts` (birthday, anniversary, relationships, kids, interests) + freeform notes + a `## Interactions` timestamped log. Mirror: `people` + `interactions`. Birthdays/anniversaries/follow-ups create reminders (§7). Captures like "Sam's daughter started college" route to a `person` interaction. Surfaces in search, Ask, and a People view.

### 9.7 Library: Books & Quotes (NEW)
- **Books** `library/books/<slug>.md`: `{title, author, cover_url, status ∈ want|reading|finished|abandoned, format, started, finished, rating, isbn, summary}` + `## Highlights`. Cover/metadata via Open Library / Google Books lookup (online, optional). **Kindle import:** parse `My Clippings.txt` or the Kindle Notebook export → create `book_highlights`, each linked as a `quote`.
- **Quotes** `library/quotes/<id>.md`: `{text, source_type ∈ book|article|podcast|conversation, source_ref, tags}` + threaded `## Thoughts` (append-only, timestamped, multiple over time). Mirror: `quotes` + `quote_thoughts`. A book highlight *is* a quote with `source_type=book` linked to the book.

### 9.8 Inventory (NEW)
`inventory/<id>.md`: `{name, photo, acquired, value, status ∈ owned|sold|discarded, location, notes}`. Mirror `inventory`. For insurance (export a list) and decluttering (find unused). Photo via attachments.

### 9.9 Content Pipeline (NEW — creator workflow)
`content/<slug>.md`: `{kind ∈ video|article|podcast|newsletter, status ∈ idea|outline|editing|waiting|published|done, channel/domain, url, publish_date}` + markdown outline body + linked tasks + a process checklist (template). Views: a list **and a kanban by status**. Leverages adjacency to the existing blog/tweet writers (a content item can spawn a draft via those agents).

### 9.10 Dashboard Intelligence (NEW)
- **Today / Top-3:** star up to 3 tasks as the day's focus (`daily_focus` table, per date). Today shows top-3 + due tasks (sorted by due) + calendar + routines by time-of-day.
- **Slipping:** projects/tasks/areas with `last_activity_at` older than a per-entity staleness window surface in a "Slipping" rail. `slipping_scan` computes it; flexible across all domains (the reference build's key pain solved).
- **Resurfacing:** one favorited quote/note per day, deterministic by date (`hash(date) % count`), rotating daily inspiration.
- **Needs review:** the router/Notetaker flags captures/notes carrying an embedded action item or `review_at`; `needs_review_scan` surfaces them in a review queue so logged items don't "go there to die."

### 9.11 Calendar (NEW — read-only)
Google Calendar via the Calendar API (OAuth2, read scope). `calendar_sync` agent polls (background, configurable) → caches into `calendar_events`; Today merges events with tasks/routines. **Read-only**: source of record stays Google. OAuth token stored encrypted in the data dir; `[calendar]` config holds client creds + sync interval. Settings view shows last-synced + a force-sync + connection health.

### 9.12 Cross-cutting (existing, reused)
Global **search** (`/api/search`) already spans articles/notes; extend to new types. **Ask/RAG** (`/api/ask`) chats over all data — already wired. **Import** of legacy notes/quotes uses the document-ingestion path. **Integration health** endpoint lists connected services (calendar/push/ollama/claude) with status for the Settings view. **Desktop quick-capture** = a global hotkey (Raycast/Alfred/shortcut) hitting `/api/capture` — a client convenience, no backend change.

---

## 10. Data Model (new tables — `CREATE TABLE IF NOT EXISTS`, file-canonical)

Beyond the `notes`/`note_links`/`note_escalations` (existing) and `tasks`/`projects` (v1 §8), v2 adds: `domains`, `milestones`, `time_entries`, `routines`, `routine_completions`, `reminders`, `daily_focus`, `people`, `interactions`, `books`, `book_highlights`, `quotes`, `quote_thoughts`, `inventory`, `content_items`, `calendar_events`. Each mirrors a markdown file (or, for high-write logs like `routine_completions`/`time_entries`/`calendar_events`, is DB-primary with a markdown projection). Full DDL lives in each subsystem's phase plan; shapes follow the §8/v1 `tasks` table conventions (vault-relative `path` or host pointer, `*_json` for arrays, soft-delete `deleted_at`, indexes on the query columns — `due`, `status`, `domain`, `fire_at`, `last_activity_at`). Reuse the `jobs` table for all new agent hand-offs; reuse the existing feed/SSE for "filed/created/reminded" toasts.

---

## 11. Local-First Reliability (the one honest hard part)

Time-based engines (§7 reminders, §9.11 calendar, §9.4 recurrence, daily summary) depend on the daemon running. Design rules:
- **Durable queues, not in-memory timers.** Every reminder/rollover is a DB row with `fire_at`/`status`; on startup the engines scan for anything due-and-pending and fire (marking `late` if past). Nothing is lost to a restart or sleep — only delayed.
- **Idempotent ticks.** `coalesce=True, max_instances=1` (existing convention); a reminder fires exactly once via a status transition under a row guard.
- **Mac-asleep is the known gap — DECIDED: accept "fires on wake."** A reminder whose `fire_at` passed while the Mac slept fires on next wake, marked `late`; nothing is lost, only delayed. We stay purely local-first: **no always-on relay, no forced-awake requirement** in v1. The daily-summary and reminder pushes are best-effort while the daemon is live. We will **not** silently imply 24/7 delivery — the UI shows `late` so the user always knows. (If on-time-while-asleep ever becomes a hard need, a minimal relay is a separate future project, explicitly out of scope now.)
- **Sync:** iCloud + atomic writes + FSEvents (Khoj-style incremental re-index). **No CRDT** — over-engineering for solo Mac+iPhone; it would add a competing source of truth. Syncthing is the upgrade if iCloud annoys.
- **Editing invariant:** the rich editor (later) changes notes from agent-written to user-editable; an open editor session marks the file `user_editing` so agents skip re-classification until it closes (decided in the editor phase).

---

## 12. Client / Editor & Attachments (design — later phases)

**Editor** must preserve markdown-on-disk fidelity (frontmatter must never corrupt) → **CodeMirror 6 live-preview** (Obsidian/SilverBullet approach; perfect round-trip), not TipTap/BlockNote (JSON-native, lossy). Milkdown is the fallback if block-UI polish outweighs fidelity. **Attachments** (Obsidian pattern): paste/drop → `POST /api/attachments` → `vault/attachments/<hash>.<ext>` → insert relative link; covers journal photos/video, inventory/people/book-cover images. **App shell:** FastAPI already serves the React PWA — add views there, install PWA on iPhone; **no Tauri/Electron**. Reference patterns to steal: SilverBullet (sync+CM6), Memos (journal stream), Khoj (file-watcher re-index).

**Views to add:** Today, Tasks, Projects (incl. milestones/time/retainers), Routines (with streak graph), Journal timeline, People, Library (Books/Quotes), Inventory, Content (list + kanban), Inbox-triage, Needs-review, Settings (integrations health). Reuse SSE for live updates; ⌘-capture + global search + Ask already patterned.

---

## 13. Document & Media Ingestion (design — later phase)

`POST /api/ingest/document` → **MarkItDown** (Office/clean PDF) / **Docling** (messy/scanned, tables), both offline → LLM extract `{summary, tags, entities, source_type}` → store raw under `vault/sources/` → existing Escalator/Compiler decide article-worthiness. Bulk legacy-note/quote import rides this path. Kindle highlight import (§9.7) and handwritten-journal OCR (§9.5) are sibling ingestors. Summarize-and-link > full-text-dump for a wiki that compounds.

---

## 14. Error Handling & Edge Cases (additions to v1 §14)

| case | handling |
|---|---|
| Reminder due while Mac asleep | Fires on next wake, marked `late`; never dropped. User informed of the constraint. |
| Push backend down (Pushover/ntfy) | Retry w/ backoff (Escalator pattern); after N, mark reminder `notify_failed`, surface in-app. Never silently swallow (Rule 12). |
| Calendar OAuth expired | Settings health shows `disconnected`; Today renders tasks/routines without events; loud, not blank. |
| Recurrence rule unparseable | Keep the verbatim string, don't materialize, flag the task `recurrence_unparsed` for the user to fix. |
| Kindle clippings format variance | Parse best-effort; unparsed highlights land in an import-review list rather than failing the whole import. |
| Person/project named but absent | High confidence → auto-create stub; low → inbox w/ "create?" prompt. |
| Top-3 already has 3 | Starring a 4th prompts to swap; never silently exceeds 3. |
| Slipping false positives | Per-entity staleness window + a "snooze/mute slipping" toggle. |

Plus all v1 cases (token auth, blank text, slug collision, iCloud placeholders, low-confidence→inbox).

---

## 15. Testing Strategy

Per existing pattern (real LLMs where cheap, `tmp_path`, `pytest-asyncio`, TestClient). Key new suites: router (each intent + command + gate + date resolution + reminder-default rule), tasks parse/toggle/recurrence-materialize, routines streak math + rollover, reminders fire-once + late-on-wake + push backend retry, retainer month-rollover carrying overdue, calendar merge, Kindle import parse, people interaction routing, top-3 cap, slipping window. E2E smokes per subsystem (e.g., watch capture → task → reminder pushed).

---

## 16. Compounding Properties (constraint, each must hold)

- ✅ Tasks→projects→articles; quotes→books; highlights→quotes→thoughts; interactions→people; captures→provenance. No dead ends.
- ✅ Journal day stitches tasks+routines+notes+events; a consumer of the graph, not a silo.
- ✅ Router is additive; note/inbox spine untouched; new types are new destinations on one queue.
- ✅ Documents/Kindle/photos enter the same wiki pipeline — no parallel store.
- ✅ Every field is queryable via the mirror; no write-only metadata; dashboard is a consumer of the same API the Watch/CLI hit.

---

## 17. Implementation Phases

Each phase = its own TDD plan under `docs/superpowers/plans/`, ~6–15 tasks, shippable, dual-subagent reviewed. Sequencing front-loads the wrist→server loop, then "active" reminders, then the typed objects, then depth, then breadth.

1. **Phase 1 — Capture ingress + Watch path.** `POST /api/capture` (bearer auth), Cloudflare Tunnel, Watch shortcut; capture → note via existing pipeline. *(Written: `2026-06-09-capture-phase-1-ingress.md`.)*
2. **Phase 2 — Intent router.** `Capture` schema, command detection, confidence gate, date resolution, reminder-default rule. Routes note/inbox now; typed branches as entities land.
3. **Phase 3 — Tasks + projects/areas.** Inline parser+writer, `tasks`/`projects` tables, `/api/tasks*` `/api/projects*`, domains.
4. **Phase 4 — Reminders + push notifications.** `reminders` table, `notifier` (Pushover/ntfy), `reminder_tick` + `daily_summary`, durable-queue reliability (§11). *The "active, not passive" milestone.*
5. **Phase 5 — Routines + streaks** (+ recurrence engine §9.4).
6. **Phase 6 — Journal** (daily files, append API, media via attachments).
7. **Phase 7 — Dashboard.** Today (top-3, due, calendar slot, routines), Tasks/Projects/Routines/Journal views, Inbox-triage, Needs-review; SSE live.
8. **Phase 8 — Dashboard intelligence.** Slipping, Resurfacing, Needs-review scan, Today top-3 starring.
9. **Phase 9 — Calendar (read-only).** Google OAuth, `calendar_sync`, Today merge, Settings health.
10. **Phase 10 — Richer projects.** Milestones, checklist templates, time tracking, retainers + month rollover.
11. **Phase 11 — People / CRM.**
12. **Phase 12 — Library: Books + Quotes** (+ Kindle import).
13. **Phase 13 — Inventory.**
14. **Phase 14 — Content pipeline** (list + kanban; blog/tweet adjacency).
15. **Phase 15 — Rich editor + attachments** (CodeMirror 6, editing-invariant lock) — can land earlier if journal media is needed sooner.
16. **Phase 16 — Document/media ingestion** (MarkItDown/Docling, handwritten-journal OCR, iPhone audio→Whisper).

(Phases 4–16 are independently valuable; reorder to taste. 4 and 7 are the highest-leverage after the spine.)

## 18. Open Questions / Known Unknowns

- ~~Push reliability vs local-first~~ — **DECIDED (2026-06-09): accept "fires on wake," pure local-first, no relay** (§11).
- **Push backend** — Pushover (paid app, dead simple) vs ntfy (free/self-host) vs PWA Web Push (most owned, most setup). Decide in Phase 4.
- **Google Calendar OAuth token storage** — exact secure-storage mechanism in the data dir; verify Calendar API scopes against current Google docs before coding (Rule 5).
- **Kindle highlight source format** — `My Clippings.txt` vs Notebook export; parse variance is real (§14).
- **Book cover/metadata provider** — Open Library vs Google Books; both online (breaks pure-offline for that lookup only).
- **`tasks.uid` durability in Obsidian** — does the `🆔` marker survive real editing habits?
- **Domains as files vs config-only** — do top-level domains need their own markdown pages or just rows?
- **Tunnel path-scoping mechanism** — Cloudflare Access vs path-scoped ingress (Phase-1 plan decides; bearer token is the backstop).
- **Editing-invariant lock** — exact note-edit vs re-classify mechanism (editor phase).

These don't block Phase 1. Flagged so per-phase plans slot them as decision points.

## Implementation Status (2026-06-12)

- Phases 1-16 are implemented in the local-first shape described here: capture ingress/router, typed tasks/projects/domains, reminders and push backends, routines/recurrence, journal, dashboard intelligence, read-only calendar, richer projects, people, library, inventory, content, editor/attachments, and ingestion.
- Documented deviations: calendar OAuth tokens are stored in the data dir with `0600` permissions, relying on the user's local account and FileVault rather than an extra app-level encrypted-at-rest wrapper; resurfacing is gated to escalated or linked notes until a favorites concept exists; journal-photo OCR is a loud-unavailable seam because the installed non-interactive vision path is not verified, while document ingestion and audio capture use optional `markitdown`/`docling` and `mlx-whisper` extras; milestone toggles use position plus expected text to avoid stale UI writes; task reminder fields (`reminder_lead_minutes`, `no_reminder`, `review_at`) are documented DB-primary exceptions to file-truth.
- Resolved §18 questions: push backend choice is deferred to `[notify].backend` (`pushover` or `ntfy`, with `none` as the default); tunnel scope is documented as capture-only with the bearer token as the load-bearing control; `tasks.uid` durability is handled by scan reconciliation and UID stamping.
