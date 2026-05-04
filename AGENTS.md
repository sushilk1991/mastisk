# Agent instructions — sushil's machine

## Mastisk: the user's personal knowledge wiki

Mastisk is a local daemon that maintains the user's compounding knowledge
wiki — notes, articles, syntheses, blog drafts. It runs as a launchd agent
on this Mac (`com.mastisk.agents`), exposes an HTTP API at
`http://localhost:8080`, and mirrors content to a markdown vault on iCloud.

**You can read it. Prefer the API over scraping the vault directly so the
user's signals, links, and graph stay coherent.**

### Pre-flight check

```bash
curl -sf http://localhost:8080/api/sidebar > /dev/null && echo ok || echo "DAEMON DOWN"
```

If down, surface that to the user — do not try to restart it yourself
(restart needs `uv tool install --reinstall ~/Code/mastisk` then
`launchctl kickstart -k gui/$UID/com.mastisk.agents`, both of which the user
should approve).

### Mental model

- **Notes** = raw quick captures (a sentence or paragraph). Mastisk
  classifies them, deduplicates, and may promote the worthy ones.
- **Articles** = structured wiki pages, four kinds: `Concept`, `Entity`,
  `Source`, `Synthesis`. Each has sections, related links, citations,
  and a confidence score.
- **The corpus compounds** — every article links to others via wiki-link
  spans; the link graph drives clustering, ranking, and synthesis.

### Glossary (mastisk lingo)

| Term | Meaning |
|------|---------|
| **Article** | Wiki page. Kinds: `Concept`, `Entity`, `Source`, `Synthesis`. Has sections, related, sources, media, confidence. |
| **Note** | Raw user capture. Has `classification`, `confidence`, `escalation_state`. Stored at `vault/_notes/YYYY-MM-DD/HHMMSS-slug.md`. |
| **Stub article** | Placeholder. Two flavors: `escalator (stub)` (note awaiting enrichment) and `Compiler (stub)` (auto-created from an unknown wiki link target). Both have `confidence=0`. |
| **Escalation** | Note → article promotion pipeline. States: `none → pending → retrying → auto_done | manual_done | failed`, plus `skipped_cap` / `skipped_dup`. |
| **Compiler** | Agent that turns a raw source or escalator-stub into a structured wiki article. Sets `updated_by='Compiler'`. |
| **Synthesizer** | Agent that picks a *cluster* of related articles via Personalized PageRank (HippoRAG2-style) and weaves them into a `Synthesis` article. Tick = 30 min. |
| **Roundtable** | Multi-LLM debate over an `input_ref` (usually an article). Produces N perspectives + a synthesis. |
| **Notetaker** | Agent that classifies new notes, assigns confidence, links to related articles. |
| **Escalator** | Agent that promotes worthy notes into stub articles, then enqueues `compiler/enrich_stub` jobs. Tick = 60s. |
| **Scout** | Agent that pulls sources from RSS/web feeds. |
| **Listener** | Agent that ingests user signals (opened, time_read, pin, dismiss, asked). |
| **Topic Suggester / Blog Writer / Artifact Agent / Vault Integrity** | Other specialist agents in the daemon. |
| **Digest** | Today's curated reading list, ranked by signals + recency. |
| **Feed** | Activity stream of agent actions. |
| **Open question** | An analytical loose end inside an article (a section with `kind='open'`). |
| **Wiki link** | Inside article HTML bodies: `<span class="link" data-target="slug">label</span>`. |
| **Confidence** | 0.0–1.0. Stubs are 0; Compiler-written articles typically 0.5–0.8. |
| **Signals** | User behavior events: `opened, time_read, pin, dismiss, asked`. |
| **PPR / HippoRAG2** | Personalized PageRank used by the Synthesizer. |
| **\_self / identity** | The user's identity files at `vault/_self/{identity,interests,dislikes,style,learnings}.md`. Loaded by every generative agent — read these first to understand the user's voice before writing on their behalf. |

### Where data lives

- **HTTP API** — `http://localhost:8080/api/*` (preferred)
- **Vault on iCloud** — `~/Library/Mobile Documents/com~apple~CloudDocs/Mastisk/vault/`
  - `_self/` — identity files (read first for personalization)
  - `_notes/YYYY-MM-DD/` — raw notes
  - `concepts/`, `entities/`, `sources/`, `synthesis/` — articles by kind
  - `blog/` — blog drafts
- **SQLite** — `~/Library/Application Support/Mastisk/mastisk.db` (read-only safe; **never write directly** — go through the API so triggers fire)
- **Daemon logs** — `~/Library/Application Support/Mastisk/logs/mastisk.log`
- **Source code** — `~/Code/mastisk/` (Python backend, Vite/React frontend)

### API endpoints (verified against source)

**Articles**
- `GET  /api/articles` — list
- `GET  /api/articles/{id}` — full article (`title, kind, summary, body_md, sections, related, sourceList, media, confidence, source_note_id, vault_path, updated_by, ...`)
- `GET  /api/articles/{id}/preview` — lightweight preview
- `POST /api/articles/{id}/pin` — pin to sidebar
- `GET  /api/sidebar` — pinned + recent

**Search & Ask**
- `GET  /api/search?q_param=QUERY&limit=20` — unified FTS over articles/notes/blog. **Param name is `q_param`, not `q`.**
- `POST /api/ask` — RAG-backed Q&A over the wiki, grounded in identity. Body: `{question, selection?, article_id?}` → `{answer, cites, hits}`. **Best surface for "what does my wiki say about X".**

**Notes**
- `POST /api/notes` — capture. Body: `{text, source: "pwa"|"cli", context?}`. Field is `text`, not `body`.
- `GET  /api/notes?limit=50&before=ID&classification=KIND`
- `GET  /api/notes/{id}` — detail
- `GET  /api/notes/{id}/file` — raw markdown
- `POST /api/notes/{id}/escalate` — manual promotion (bypass auto-rule)
- `DELETE /api/notes/{id}` — soft delete

**Vault & identity**
- `GET  /api/vault/info` — `{vault_path, icloud, self_files}`
- `GET  /api/vault/self/{name}` — `name ∈ {identity, interests, dislikes, style, learnings}`
- `PUT  /api/vault/self/{name}` — update (body: `{content}`)

**Surfaces**
- `GET  /api/digest` — today's curated reading
- `GET  /api/feed?limit=50` — agent activity
- `GET  /api/feed/stream` — SSE live feed
- `GET  /api/open-questions` — research backlog
- `GET  /api/synthesis/pending` — Synthesizer drafts awaiting accept/reject
- `POST /api/roundtables` — kick off a roundtable
- `GET  /api/repos` — tracked GitHub repos

### Recipes

```bash
# Quick search
curl -s "http://localhost:8080/api/search?q_param=autopilot+reliability&limit=10" | jq

# Ask the wiki (RAG, cited)
curl -s -X POST http://localhost:8080/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "What does my wiki say about agent skill composition?"}' | jq

# Get full article
curl -s "http://localhost:8080/api/articles/note-000044-skill-composing-autopilot-loop" | jq

# Capture a note
curl -s -X POST http://localhost:8080/api/notes \
  -H 'Content-Type: application/json' \
  -d '{"text": "Idea: ...", "source": "cli"}' | jq

# Today's digest
curl -s http://localhost:8080/api/digest | jq '.items[] | {title, score}'

# Open research questions
curl -s http://localhost:8080/api/open-questions | jq '.[:5]'

# User identity (drives personalization tone)
curl -s http://localhost:8080/api/vault/self/identity | jq -r .content
curl -s http://localhost:8080/api/vault/self/style    | jq -r .content
```

### Decision table — which surface

| User intent | Best surface |
|-------------|--------------|
| "What does my wiki say about X?" | `POST /api/ask` |
| "Find articles about X" | `GET /api/search?q_param=X` |
| "Show me article Y" | `GET /api/articles/Y` |
| "What's on my reading queue?" | `GET /api/digest` |
| "Open research questions?" | `GET /api/open-questions` |
| "What did mastisk do today?" | `GET /api/feed` |
| "Who is the user?" | `GET /api/vault/self/identity` then `style` |
| "Capture this thought" | `POST /api/notes` |
| "Inspect job/escalation pipeline state" | SQLite (read-only) |

### SQLite reference (read-only)

DB: `~/Library/Application Support/Mastisk/mastisk.db`. Use `sqlite3` via shell.
**Never write directly** — bypasses triggers, signal cascades, and FTS indexes.

Useful tables: `articles`, `article_sections`, `links`, `notes`,
`note_escalations`, `note_links`, `sources`, `article_sources`, `jobs`,
`feed`, `signals`, `roundtables`, `roundtable_perspectives`, `synthesis_runs`,
`blog_posts`.

```bash
DB="$HOME/Library/Application Support/Mastisk/mastisk.db"

# Stuck stubs awaiting enrichment
sqlite3 "$DB" "SELECT id, title, source_note_id FROM articles
               WHERE updated_by='escalator (stub)' LIMIT 20;"

# Job queue health
sqlite3 "$DB" "SELECT agent, kind, status, COUNT(*) FROM jobs
               GROUP BY agent, kind, status ORDER BY agent, kind;"

# Recent escalation outcomes
sqlite3 "$DB" "SELECT triggered_at, trigger, result, error
               FROM note_escalations ORDER BY id DESC LIMIT 20;"
```

### Don'ts

- **Don't write to the SQLite DB directly.** Use the API.
- **Don't restart the daemon without asking** — it interrupts in-flight LLM calls.
- **Don't bypass the Escalator auto-rule** by faking notes. Use `POST /api/notes/{id}/escalate` for manual promotion.
- **Don't paste vault content verbatim without citing the article id** (`note-XXXXXX-slug`).

### Workflow examples

**"What's on my mind about X?"**
1. `POST /api/ask` with `{question: "..."}` → cited answer.
2. Follow each `cites[]` entry with `GET /api/articles/{id}` for depth.

**"Find related material before I write Y."**
1. `GET /api/search?q_param=Y`
2. `GET /api/open-questions` — what loose ends exist?
3. `GET /api/vault/self/style` — match the voice.

**"Capture a thought from this conversation."**
1. `POST /api/notes` with `{text, source: "cli"}`.
2. Notetaker classifies (60s tick), Escalator may promote (60s tick),
   Compiler enriches the stub (5-min tick). End-to-end ≈ 5–15 min.

**"Why is article Z stuck as a stub?"**
1. `GET /api/articles/Z` — check `updated_by`.
2. If `escalator (stub)`: query `jobs` for an `enrich_stub` job.
3. If failed, read the `error` column and the daemon log.
