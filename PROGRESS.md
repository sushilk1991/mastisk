# Digest calendar, critic gate, and main delivery

## Done
- Collected all pending renderer, safe-restart, calendar, skill, test, and documentation changes in main commit `45f80c8`.
- Calendar dates now depend on eligible articles, not previously saved digest selections.
- Implemented a 4/5 synthesis publication gate with one bounded revision. Missing/invalid scores and failed repairs withhold publication.
- Withheld attempts retain cluster IDs and available critic evidence without creating an article or vault page.
- Existing low/unscored/rejected syntheses are filtered from ordinary Ask context, search, and digest ranking/calendar/navigation. Explicit human acceptance overrides the score; direct existing-article access remains available for review.
- Wrote the content audit and implementation plan under `docs/audits/` and `docs/plans/`.
- Removed both extra worktrees after verifying their HEADs were merged into origin/main and their tracked files were clean. Complete private archives, including untracked logs, are under `~/Library/Application Support/Mastisk/worktree-archives/20261101-200717/`.
- Excluded `.know` runtime indexes and locks from version control.

## Verified
- Before critic implementation: 15 focused regressions failed on missing publication/read gates.
- Full macOS backend suite: 1,178 passed.
- Full Linux backend suite through boxrun: 1,178 passed, with a Starlette/httpx deprecation warning.
- Frontend through boxrun: 13 Chromium tests, TypeScript checks, and production build passed. Build reports existing large-chunk/Browserslist warnings and dependency-audit findings.
- Changed critic-gate Python files pass Ruff. Full-repo Ruff remains at the same 145 baseline findings, with no new findings.
- Corrected route returns HTTP 200 and all 31 October dates against the production DB in read-only mode.
- Read-only production checks confirm the existing 1/5 synthesis is excluded from corrected search and digest candidates; no production data was changed.
- The live daemon's health endpoint still returns HTTP 200.
- Both extra worktrees are gone; the main checkout is the only registered worktree.

## Cloud test workaround
- The 20 GB data disk is full. No caches were deleted.
- Tests used `/tmp/boxrun-mastisk-20261101` and temporary npm/uv caches. The original cloud checkout was preserved and restored after the test run; temporary outputs remain under `/tmp`.

## Provider work (2026-11-02)
- User approved the combined deployment after completion, then selected DeepSeek Flash 1017 cloud as default with thinking on/high and requested Codex/Muse controls in Settings.
- Added provider/model/effort settings, isolated Muse execution, configurable Codex paths, shared Ask routing, and atomic nested settings persistence. Package defaults remain compatible; this installation will receive its selected order through the live API.
- Provider API regressions: 10 failed before implementation; focused backend suite now passes 74 tests. Settings browser regressions: two failed before implementation and now pass.
- Real probes passed for DeepSeek `:1017-cloud` with high thinking, Codex read-only exec, and Muse tool-free exec. Ollama `/api/show` confirms supported thinking values include `high`.
- Combined validation passes: 1,197 backend tests on macOS and Linux; 15 Chromium tests, TypeScript, and frontend production build through boxrun. Full Ruff has 144 pre-existing findings, down from 145, with no new findings.
- Full validation exposed an existing host/configured-timezone mismatch in a meeting-prep test. The fixture now queries its seeded event's configured calendar date; it passed red/green verification.
- Cloud temporary storage filled during a test run. Mastisk's own prior test artifacts were preserved under `/var/tmp/mastisk-pytest-preserved-20261102/`, then the full Linux suite passed using a fresh dedicated test directory. No caches were deleted.
- The cloud checkout currently points to the preserved temporary test directory; restore the original cloud checkout after pulling tested build assets.

## Pending
- Deployment is explicitly approved. Finish the provider changes, validate, commit/push to main, then drain/install/restart once. Preserve the daemon's Python 3.14 and frozen dependencies.
- Verify live provider settings/default response, calendar dates, critic filtering, new UI assets, and TLS endpoints. No reinstall/restart has occurred yet.
- Provenance-preserving promotion, long-history dedup, mixed digest ranking, and extraction retries remain recommendations, not implemented features.

## Constraints
- Direct commits/push to main and one combined drained deployment/restart are authorized. No release tag is requested.
- Production database and vault remain read-only to agents; writes use API/CLI.
- Git history and origin/main identify delivered commits. Do not infer live deployment from a successful push.
