---
name: mastisk
description: Talk to the user's Mastisk personal knowledge wiki — search articles, ask RAG-backed questions, capture notes, look up open questions, and read the user's identity files. Use whenever the user references "the wiki", "my notes", "my articles", an article id like `note-XXXXXX-...`, or asks something like "what does my wiki say about X".
user-invokable: true
---

# Mastisk integration

Mastisk is the user's local personal-knowledge daemon. It runs as a launchd
agent on this Mac, exposes an HTTP API on `http://localhost:5555`, and mirrors
its content to a markdown vault on iCloud. **You can read it; prefer the API
over scraping the vault directly so the user's signals/links stay coherent.**

## Pre-flight

Confirm the daemon is up before any other call:

```bash
curl -sf http://localhost:5555/api/sidebar > /dev/null && echo "ok" || echo "DAEMON DOWN"
```

If down, surface that to the user — don't try to restart it yourself
(the daemon is a `uv tool install`-managed launchd agent at
`com.mastisk.agents`; restart needs `uv tool install --reinstall` of the
mastisk source repo at `~/Code/mastisk` followed by `launchctl kickstart -k
gui/$UID/com.mastisk.agents`).

## Mental model

- **Notes** are raw quick captures (a sentence, a paragraph). The user
  brain-dumps; mastisk classifies, deduplicates, and may promote the worthy
  ones into wiki articles.
- **Articles** are wiki pages — the durable, structured form. Four kinds:
  **Concept**, **Entity**, **Source**, **Synthesis**. Each has sections,
  related links, citations, and a confidence score.
- **The corpus compounds.** Every article links to others via wiki-link
  spans; the graph drives clustering, ranking, and synthesis. Don't treat
  individual articles as standalone — they're nodes.

## Glossary (mastisk lingo)

| Term | Meaning |
|------|---------|
| **Article** | A wiki page. Kinds: `Concept`, `Entity`, `Source`, `Synthesis`. Has `sections`, `related`, `sources`, `media`, `confidence`. |
| **Note** | Raw user capture. Has `classification` (idea/rant/question/etc), `confidence`, an `escalation_state`. Stored at `vault/_notes/YYYY-MM-DD/HHMMSS-slug.md`. |
| **Stub article** | Placeholder, two flavors: `escalator (stub)` (created when a note is promoted, before enrichment) and `Compiler (stub)` (auto-created when an article references an unknown wiki slug). Both have `confidence=0` and no real content yet. |
| **Escalation** | Note → article promotion pipeline. States: `none → pending → retrying → auto_done | manual_done | failed`, plus `skipped_cap` / `skipped_dup` for rule-failed cases. |
| **Compiler** | Agent. Turns a raw source or escalator-stub into a structured wiki article via Claude. Sets `updated_by='Compiler'`. |
| **Synthesizer** | Agent. Picks a *cluster* of related articles via Personalized PageRank (HippoRAG2-style) and weaves them into a `Synthesis` article. Cluster size 3-8, seeded from articles updated in the last 48h. Tick = 30 min. |
| **Roundtable** | Multi-LLM debate. The user picks an article (`input_ref`), N perspectives are generated, then a synthesis. UI surface at `/r/<roundtable_id>`. |
| **Notetaker** | Agent. Classifies new notes (idea/rant/etc), assigns confidence, links to related articles. |
| **Escalator** | Agent. Decides which notes are worth promoting to article stubs. Tick = 60s. Auto-rule gates on classification + confidence + length + daily cap + dedup. After creating a stub it enqueues a `compiler/enrich_stub` job. |
| **Scout** | Agent. Pulls sources from RSS/web feeds; enqueues compile jobs. |
| **Listener** | Agent. Ingests user signals (opened, time_read, pin, dismiss, asked) used for ranking. |
| **Topic Suggester** | Agent. Surfaces topic ideas from the corpus. |
| **GitHub Ideator / Poller** | Agents that watch tracked GitHub repos and generate issue ideas. |
| **Blog Writer** | Agent. Drafts blog posts in the user's voice from recent notes/articles. |
| **Artifact Agent** | Generates inline visual artifacts (charts, stats, timelines) for articles. |
| **Vault Integrity** | Agent. Reconciles DB ↔ on-disk vault drift. |
| **Digest** | Today's curated reading list. Ranking uses signals (opened/time_read/pin) + recency. |
| **Feed** | Activity stream of agent actions. SSE stream at `/api/feed/stream`. |
| **Open question** | Analytical loose end inside an article (a section with `kind='open'`). The Open Questions view is the global research backlog. |
| **Wiki link** | Inside article HTML bodies, cross-references look like `<span class="link" data-target="slug">label</span>`. The Compiler resolves these and auto-stubs unknown slugs. |
| **Confidence** | 0.0–1.0. The model's calibration of how solid a page is. Stubs are 0; Compiler-written articles typically 0.5-0.8. |
| **Signals** | User behavior events (`opened`, `time_read`, `pin`, `dismiss`, `asked`). Drive digest ranking. |
| **PPR / HippoRAG2** | Personalized PageRank, used by the Synthesizer for cluster selection. Damping 0.85, 20 iterations. |
| **\_self / identity** | The user's identity files at `vault/_self/{identity,interests,dislikes,style,learnings}.md`. Loaded by every generative agent as system context — read these *first* to understand the user before responding on their behalf. |

## Where data lives

- **HTTP API** — `http://localhost:5555/api/*` (preferred; see endpoint catalog below)
- **Vault on iCloud** — `~/Library/Mobile Documents/com~apple~CloudDocs/Mastisk/vault/`
  - `_self/` — identity files (read first for personalization)
  - `_notes/YYYY-MM-DD/` — raw notes
  - `concepts/`, `entities/`, `sources/`, `synthesis/` — articles by kind
  - `blog/` — blog drafts
- **SQLite** — `~/Library/Application Support/Mastisk/mastisk.db` (read-only safe; **never write** — go through the API so triggers + signals fire)
- **Daemon logs** — `~/Library/Application Support/Mastisk/logs/mastisk.log`
- **Source code** — `~/Code/mastisk/` (Python backend, React/Vite frontend)

## API endpoint catalog (verified)

### Articles
- `GET  /api/articles` — list, paginated
- `GET  /api/articles/{id}` — full article: `{title, kind, summary, body_md, sections, related, sourceList, media, confidence, source_note_id, vault_path, updated_by, ...}`
- `GET  /api/articles/{id}/preview` — lightweight preview (used for hover cards)
- `POST /api/articles/{id}/pin` — pin to sidebar
- `GET  /api/articles/{id}/artifacts` — inline visualizations
- `POST /api/articles/{id}/artifacts/regenerate` — regenerate them
- `GET  /api/sidebar` — pinned + recent articles

### Search & Ask
- `GET  /api/search?q_param=QUERY&limit=20` — unified FTS across articles, notes, blog. **Param is `q_param` not `q`.** Returns `{results: [{kind, id, title, subtitle, snippet, score}, ...]}`
- `GET  /api/search/{query}` — same but path-style
- `POST /api/ask` — RAG-backed Q&A. Body: `{question, selection?, article_id?}`. Returns `{answer, cites, hits}`. **Use this when the user asks "what does my wiki think about X" — it grounds in their corpus.**

### Notes
- `POST /api/notes` — capture. Body: `{text, source: "pwa"|"cli", context?: {article_id, section_heading?, question_html?}}`. Field is `text` not `body`.
- `GET  /api/notes?limit=50&before=ID&classification=KIND` — list
- `GET  /api/notes/{id}` — note detail
- `GET  /api/notes/{id}/file` — raw markdown
- `POST /api/notes/{id}/escalate` — manually trigger Escalator (bypasses auto-rule)
- `DELETE /api/notes/{id}` — soft delete

### Vault & identity
- `GET  /api/vault/info` — `{vault_path, icloud, self_files}`
- `GET  /api/vault/self/{name}` — `name ∈ {identity, interests, dislikes, style, learnings}`. Returns `{name, content}`.
- `PUT  /api/vault/self/{name}` — update an identity file. Body: `{content}`.

### Surfaces
- `GET  /api/digest` — today's curated reading list
- `GET  /api/digest/calendar` — what existed on each day
- `POST /api/digest/feedback` — record user verdict on a digest item
- `GET  /api/feed?limit=50` — recent agent activity
- `GET  /api/feed/stream` — SSE live feed
- `GET  /api/open-questions` — research backlog (analytical loose ends)
- `GET  /api/synthesis/pending` — Synthesizer drafts awaiting accept/reject

### Other
- `GET  /api/repos` — tracked GitHub repos; `/repos/ideas/{slug}` for ideator output
- `POST /api/roundtables` — kick off a roundtable (body: `{input_ref, perspectives, ...}`)
- `GET  /api/roundtables` — list
- `POST /api/signals` — record a signal manually (rare; UI emits these automatically)

## Recipes

```bash
# 1. Quick search
curl -s "http://localhost:5555/api/search?q_param=autopilot+reliability&limit=10" | jq

# 2. Ask the wiki a question (RAG over articles, grounded in identity)
curl -s -X POST http://localhost:5555/api/ask \
  -H 'Content-Type: application/json' \
  -d '{"question": "What does my wiki say about agent skill composition?"}' | jq

# 3. Get full article content
curl -s "http://localhost:5555/api/articles/note-000044-skill-composing-autopilot-loop" | jq '{title, kind, confidence, sections: (.sections | length), updated_by}'

# 4. Capture a note
curl -s -X POST http://localhost:5555/api/notes \
  -H 'Content-Type: application/json' \
  -d '{"text": "Idea: route synthesizer jobs through a priority queue so user-pinned articles synth first.", "source": "cli"}' | jq

# 5. Today's digest (what mastisk thinks the user should read)
curl -s http://localhost:5555/api/digest | jq '.items[] | {title, score, why}'

# 6. Open research questions
curl -s http://localhost:5555/api/open-questions | jq '.[:5]'

# 7. Read user identity (drives personalization tone)
curl -s http://localhost:5555/api/vault/self/identity | jq -r .content
curl -s http://localhost:5555/api/vault/self/style    | jq -r .content

# 8. Recent agent activity
curl -s "http://localhost:5555/api/feed?limit=20" | jq '.[] | "\(.created_at) [\(.agent)] \(.verb) \(.obj)"'

# 9. Force-escalate a note (bypass auto-rule)
curl -s -X POST http://localhost:5555/api/notes/44/escalate
```

## Decision table — which surface to use

| User intent | Best surface |
|-------------|--------------|
| "What does my wiki say about X?" | `POST /api/ask` (RAG, cited) |
| "Find articles about X" | `GET /api/search?q_param=X` |
| "Show me article Y in full" | `GET /api/articles/Y` |
| "What's on my reading queue?" | `GET /api/digest` |
| "What open questions does my corpus have?" | `GET /api/open-questions` |
| "What did mastisk do today?" | `GET /api/feed` |
| "Who is the user / what's their voice?" | `GET /api/vault/self/identity` then `style` |
| "Capture this thought for me" | `POST /api/notes` |
| "Why is article Y still a stub?" | SQLite: jobs/note_escalations tables |
| "Inspect job queue / pipeline state" | SQLite (read-only) |

## SQLite reference (read-only)

Path: `~/Library/Application Support/Mastisk/mastisk.db`. Use `sqlite3` via Bash.
**Never write directly** — all writes must go through the API so signals,
links, and vault sync stay coherent.

Useful tables:

| Table | Purpose |
|-------|---------|
| `articles` | One row per wiki page. Key columns: `id, kind, title, summary, body_md, confidence, updated_by, source_note_id, vault_path, hero_image_url`. |
| `article_sections` | Typed sections per article (`heading, body, kind, idx`). `kind='open'` = open question. |
| `links` | Wiki-link graph edges (`from, to, weight`). |
| `notes` | Raw captures. `classification, confidence, escalation_state, escalation_article_id`. |
| `note_escalations` | Audit log of every escalation attempt (`result, trigger, model, error`). |
| `note_links` | Notes ↔ articles (rank=0 means user-direct, >0 means classifier-derived). |
| `sources` | Origin URLs/paths feeding articles. |
| `article_sources` | Article ↔ source many-to-many. |
| `jobs` | Agent job queue. `agent, kind, status, payload_json, error, attempts`. |
| `feed` | Agent activity log (verbs like `wrote`, `enriched`, `synthesized`, `auto-escalated`). |
| `signals` | User behavior events. |
| `roundtables`, `roundtable_perspectives` | Roundtable runs and their perspectives. |
| `synthesis_runs` | Synthesizer audit log (cluster_hash, eval_score). |
| `blog_posts` | Blog drafts. |

Useful one-shots:

```bash
DB="$HOME/Library/Application Support/Mastisk/mastisk.db"

# Stuck stubs (escalator placeholders not yet enriched)
sqlite3 "$DB" "SELECT id, title, source_note_id FROM articles
               WHERE updated_by='escalator (stub)' LIMIT 20;"

# Job queue health
sqlite3 "$DB" "SELECT agent, kind, status, COUNT(*) FROM jobs
               GROUP BY agent, kind, status ORDER BY agent, kind;"

# Recent escalation outcomes
sqlite3 "$DB" "SELECT triggered_at, trigger, result, error
               FROM note_escalations ORDER BY id DESC LIMIT 20;"

# Articles touched by a specific agent today
sqlite3 "$DB" "SELECT verb, obj FROM feed
               WHERE agent='compiler' AND created_at > date('now')
               ORDER BY id DESC;"
```

## Don'ts

- **Don't write to the SQLite DB directly.** Use the API. Direct writes skip
  triggers (FTS index, link reconciliation, signal cascades).
- **Don't restart the daemon without asking.** Restarting interrupts in-flight
  Compiler/Synthesizer LLM calls. Escalator/Compiler ticks resume themselves
  fine, but ask first.
- **Don't bypass the Escalator's auto-rule** by inserting fake notes. If the
  user wants something promoted manually, use `POST /api/notes/{id}/escalate`.
- **Don't paste vault content verbatim into chat without attribution.** Cite
  the article id (`note-XXXXXX-slug`) so the user can navigate to it.

## Workflow examples

**"What's on my mind about X?"**
1. `POST /api/ask` with `{question: "..."}` — get a cited answer.
2. If you need more depth, follow each `cites[]` entry with `GET /api/articles/{id}`.

**"Find related material before I write about Y."**
1. `GET /api/search?q_param=Y` — list candidates.
2. `GET /api/open-questions` — what loose ends does the corpus already have?
3. `GET /api/vault/self/style` — match the user's voice.

**"Capture a thought from this conversation."**
1. `POST /api/notes` with `{text: "...", source: "cli"}`.
2. The Notetaker classifies on its next 60s tick; if the auto-rule passes the
   Escalator promotes it; the Compiler then enriches the stub. Total latency
   ≈ 5–15 min before it's a real article.

**"Why is article Z stuck as a stub?"**
1. `GET /api/articles/Z` — check `updated_by`.
2. If `escalator (stub)`: query `jobs` for an `enrich_stub` job for that id.
3. If queued/running: just wait. If failed: read the `error` column and the
   daemon log.
