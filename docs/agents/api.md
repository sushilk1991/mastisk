# Querying the running wiki

Prefer the HTTP API at `http://127.0.0.1:5555` over reading the vault, so signals, links and the graph stay coherent. Endpoints were verified against source; when one disagrees with `src/mastisk/`, the source wins.

## Which surface

| Question | Surface |
|----------|---------|
| "What does my wiki say about X?" | `POST /api/ask` (then `GET /api/articles/{id}` for each cited source) |
| "Find articles about X" | `GET /api/search?q_param=X` |
| "Show me article Y" | `GET /api/articles/Y` |
| "What's on my reading queue?" | `GET /api/digest` |
| "Open research questions?" | `GET /api/open-questions` |
| "What did mastisk do today?" | `GET /api/feed` |
| "Who is the user?" / match their voice | `GET /api/vault/self/identity`, then `style` |
| "Capture this thought" | `POST /api/notes` with `{text, source: "cli"}` |
| Job or escalation pipeline state | SQLite, read-only (below) |

## Endpoints

- **Articles:** `GET /api/articles`, `GET /api/articles/{id}` (`title, kind, summary, body_md, sections, related, sourceList, media, confidence, source_note_id, vault_path, updated_by, ...`), `GET /api/articles/{id}/preview`, `POST /api/articles/{id}/pin`, `GET /api/sidebar`.
- **Search:** `GET /api/search?q_param=QUERY&limit=20`, unified FTS over articles, notes and blog. The parameter is `q_param`.
- **Ask:** `POST /api/ask` with `{question, intelligent?, mode?, messages?, selection?, article_id?}`; returns `{answer, sources, retrieved_sources, coverage, research_status, conversation_id}`. Agent CLI calls go through `mastisk ask`, which sets `intelligent=true`.
- **Notes:** `POST /api/notes` with `{text, source: "pwa"|"cli", context?}` (the field is `text`). `GET /api/notes?limit=50&before=ID&classification=KIND`, `GET /api/notes/{id}`, `GET /api/notes/{id}/file`, `POST /api/notes/{id}/escalate`, `DELETE /api/notes/{id}` (soft delete).
- **Ingestion:** `POST /api/ingest/document` (multipart, through the converter and Compiler). `POST /api/listen` with `{url, exact_url: true}` so an advertised RSS feed doesn't replace the article.
- **Vault:** `GET /api/vault/info`, `GET /api/vault/self/{name}` and `PUT` with `{content}`, for `name` in `identity, interests, dislikes, style, learnings`.
- **Surfaces:** `GET /api/digest`, `GET /api/feed?limit=50`, `GET /api/feed/stream` (SSE), `GET /api/open-questions`, `GET /api/synthesis/pending`, `POST /api/roundtables`, `GET /api/repos`.

```bash
curl -s "http://127.0.0.1:5555/api/search?q_param=autopilot+reliability&limit=10" | jq
curl -s -X POST http://127.0.0.1:5555/api/ask -H 'Content-Type: application/json' \
  -d '{"question": "What does my wiki say about agent skill composition?"}' | jq
curl -s http://127.0.0.1:5555/api/digest | jq '.items[] | {title, score}'
```

## SQLite (read-only)

`DB="$HOME/Library/Application Support/Mastisk/mastisk.db"`. Tables: `articles`, `article_sections`, `links`, `notes`, `note_escalations`, `note_links`, `sources`, `article_sources`, `jobs`, `feed`, `signals`, `roundtables`, `roundtable_perspectives`, `synthesis_runs`, `blog_posts`.

```bash
# Stubs awaiting enrichment
sqlite3 "$DB" "SELECT id, title, source_note_id FROM articles WHERE updated_by='escalator (stub)' LIMIT 20;"
# Job queue health
sqlite3 "$DB" "SELECT agent, kind, status, COUNT(*) FROM jobs GROUP BY agent, kind, status ORDER BY agent, kind;"
# Recent escalations
sqlite3 "$DB" "SELECT triggered_at, trigger, result, error FROM note_escalations ORDER BY id DESC LIMIT 20;"
```

**Why is article Z still a stub?** `GET /api/articles/Z` and check `updated_by`. For `escalator (stub)`, look for its `enrich_stub` job in `jobs`; if it failed, read the `error` column and the daemon log.
