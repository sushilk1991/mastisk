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

## Pending
- Install and activate the calendar and critic-gate changes only after explicit approval for a drained deployment/restart. The installed package is separate from this checkout; pushing to main does not deploy it.
- After approved deployment, verify live calendar interaction and a real synthesis run's publication/withheld outcome.
- Provenance-preserving promotion, long-history dedup, mixed digest ranking, and extraction retries remain recommendations, not implemented features.

## Constraints
- All changes were authorized for direct commits and push to main; no release tag or production restart is authorized.
- Production database and vault remain read-only to agents; writes use API/CLI.
- Git history and origin/main identify delivered commits. Do not infer live deployment from a successful push.
