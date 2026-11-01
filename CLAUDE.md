# Mastisk

Mastisk is the user's local knowledge-wiki daemon, and this repo is its source (Python backend in `src/mastisk/`, Vite/React PWA in `frontend/`). It runs as the launchd agent `com.mastisk.agents`, serves `http://127.0.0.1:5555`, and mirrors a markdown vault to iCloud. Domain terms (articles, notes, escalation, Compiler, Synthesizer, signals) are defined in `CONTEXT.md`.

## Memory contract

Use the global `mastisk` skill and CLI. Recall with `mastisk ask "<complete task context>" --json` before work that prior knowledge could change. After verified work produces a durable, reusable learning, preserve it with `mastisk ingest`, including its scope, evidence, current date or version, and recheck conditions. Treat old knowledge as dated evidence: keep durable lessons, re-verify fast-moving claims. Ingest only verified learnings, never status, guesses, hidden reasoning or secrets.

## The live daemon

- All writes go through the API or CLI so triggers, signal cascades and FTS indexes fire. The SQLite DB (`~/Library/Application Support/Mastisk/mastisk.db`) and the vault are read-only for agents.
- Pre-flight: `curl -sf http://127.0.0.1:5555/api/sidebar > /dev/null && echo ok || echo "DAEMON DOWN"`. If it's down, tell the user and get approval before starting it.
- Restarts interrupt in-flight LLM calls, so each one needs the user's approval. Then use `mastisk restart`, which drains active runs; the procedure, including older daemons without drain, is in [README: Safe restarts](README.md#safe-restarts). `launchctl kickstart -k` and reinstalling package files during active runs stay off the table.
- Manual note promotion goes through `POST /api/notes/{id}/escalate`. When quoting vault content, cite the article id (`note-XXXXXX-slug`).

## Gate

Backend: `pytest` (target the touched area, e.g. `pytest tests/test_pele_artifacts.py`) and `ruff check`. Frontend: `npm --prefix frontend run typecheck` and `npm --prefix frontend test` (Playwright, needs Chromium). Run browser tests and builds through `boxrun`.

## Pointers

- Querying the running wiki (endpoints, request fields, recipes, which surface for which question, read-only SQL for pipeline state): `docs/agents/api.md`.
- Phone setup, remote-access failures or Tailscale Serve changes: [README: Phone setup](README.md#phone-setup) and its troubleshooting guardrails. Keep the TLS-terminated TCP setup and other apps' routes, and verify the page and API.
- Renderer or chart-artifact changes: [README: Charts and diagrams](README.md#charts-and-diagrams). Mermaid source goes through the shared Pele renderer; keep legacy chart compatibility and the app-owned SVG safety limits.
- Blog drafts and edits: apply the `/stop-slop` skill before delivering prose.
- Issues: GitHub Issues on `sushilk1991/mastisk` via `gh` (`docs/agents/issue-tracker.md`). Triage labels: `docs/agents/triage-labels.md`. Domain docs: `docs/agents/domain.md`.

## Maintaining this file

Keep only what almost every session in this repo needs. Point to the file or command that owns a fact instead of restating it, and prune or rewrite before appending.
