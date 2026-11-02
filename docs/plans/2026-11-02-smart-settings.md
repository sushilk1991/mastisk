# Smart Settings Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Put provider, discovered model, supported thinking, and readable fallback behavior together; detect installed CLIs without requiring paths.

**Architecture:** Settings calls a read-only provider-catalog API. A bounded metadata driver talks directly to native CLI protocols or provider HTTP APIs; it never starts an inference/session turn. Shared executable detection is used by both discovery and actual bridges. Existing config fields remain compatible, with technical controls behind Advanced.

**Tech Stack:** FastAPI/httpx/asyncio, React/TypeScript, pytest, Playwright through boxrun.

## Approved design

The user approved one AI model card: Provider → Model → Thinking, followed by a plain fallback sentence. Models come from native catalogs, not a hardcoded vendor list. CLI commands are detected; paths, API priority, connections, specialised model overrides, and budgets use readable labels under Advanced. Retain saved selections on discovery failures and offer Refresh.

No inference, installation, login, config migration, daemon restart, or deployment is authorized by discovery. Retain existing DeepSeek/high and other pins unless explicitly edited. Deployment needs fresh approval.

## Task 1: Shared executable detection

Files: create `src/mastisk/bridges/executables.py`, `tests/test_executables.py`; update Claude/Codex/Muse bridges and existing bridge tests.

1. Write tests for a minimal daemon PATH, common user install locations, explicit overrides, missing overrides, and exclusion of relative PATH entries.
2. Run `.venv/bin/pytest tests/test_executables.py -q`; observe the absent detection behavior.
3. Implement `_find_cli(command)` using executable checks and expanded trusted search locations. Never parse a shell command or silently replace an explicit path.
4. Reuse it in actual bridge calls; preserve subprocess sandbox settings and existing Claude wrapper compatibility.
5. Run detection/bridge tests and Ruff on the new module.

## Task 2: Read-only model discovery

Files: create `src/mastisk/bridges/model_catalog.py`, `tests/test_model_catalog.py`; update `src/mastisk/routes/settings_route.py`.

1. Write API tests for native account catalogs, pagination, explicit paths, absent CLIs, thinking metadata, timeout/child cleanup, stale-cache retention, cache invalidation, and credential redaction. Initial `/api/settings/models/codex` must fail with 404 before implementation.
2. Add `GET /api/settings/models/{provider}?model=...&refresh=...`. Restrict provider values and model length; use only configured executable paths and connection URLs.
3. Adapt the verified YoYo metadata handshake: Codex `initialize`/`account/read`/`model/list`; Claude tool-free SDK initialize; Muse ephemeral `serve` and `model/list` before any session. Muse's current native schema and real probe confirm `providerCatalog` and per-model effort variants.
4. Read Ollama tags plus the selected model's `/api/show` thinking metadata. Discover Anthropic API models only with a configured key. Do not infer thinking support when metadata is absent.
5. Bound time, output, pages, cache size, and child lifetime. Use throwaway working directories, no shell, no inference, no account identity in returned data, and suppress Muse launcher auto-updates. Cache briefly; refresh explicitly; return the last good catalog with a visible stale/error state on failure.
6. Expose passive CLI detection and actual routing metadata from Settings without starting probes on ordinary settings reads.
7. Run targeted tests and probe actual installed catalogs without inference.

## Task 3: Compact form and real payloads

Files: update `frontend/src/components/SettingsView.tsx`, `frontend/src/types.ts`, `frontend/src/api.ts`, `frontend/src/styles/mastisk.css`, `frontend/tests/settings.spec.ts`. Add Claude effort support to settings/bridge only with tests if its native picker advertises supported efforts.

1. Write browser tests before replacing the UI: selected-provider model dropdown, supported thinking, provider switching with retained pins, hidden technical controls, refresh failure preserving choices, honest automatic-priority copy, partial-save behavior, and mobile layout.
2. Run `boxrun -- 'npm --prefix frontend test -- --grep ...'` and confirm missing UI behavior fails.
3. Implement the single card with native labelled selects. Fetch only the selected provider; discard obsolete async responses. Keep a saved model visible even when absent from a catalog. Never silently change unrelated provider pins or CLI defaults.
4. On provider/model edits, send existing settings fields; choosing a provider disables implicit Anthropic priority. Show actual routing/override information without claiming every provider is ready merely because a binary exists.
5. Advanced contains friendly labels, optional executable overrides with detected hints, existing connection/key handling, specialised models, and budgets. Preserve existing GitHub actions and secret masking. Keep save/discard visible, keyboard-accessible, and touch-friendly.
6. Use only declared thinking levels; never show a functional control unsupported by its actual runtime adapter. A model change may reset only that provider's incompatible effort to its declared default; explain this behavior.
7. Run typecheck, browser tests, screenshots at desktop/mobile/light/dark through boxrun. Review frames before sharing.

## Task 4: Verification and handoff

Files: update `README.md`, `PROGRESS.md`, this plan as needed.

1. Run targeted backend tests, full backend on boxrun, frontend typecheck/tests/build on boxrun, and Ruff baseline comparison. No destructive cache cleanup on the full cloud disk.
2. Probe Ollama/Codex/Claude/Muse catalogs through the new API in an isolated temporary config using this checkout. Do not alter the live daemon's config or files.
3. Review the diff for secrets, unnecessary changes, stale responses, process leaks, and unsupported capability claims. Record observed results, not assumed readiness.
4. Commit the implementation and verification evidence. Do not restart/deploy without new user approval; report that the live Settings page still needs deployment.
