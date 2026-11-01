# Digest calendar fix and content audit

## Done
- Changed `src/mastisk/routes/digest_route.py` to list all dates with eligible articles, independent of saved digest candidates. Digest generation remains on demand.
- Replaced the saved-only calendar test and added threshold, month-boundary, date-movement, first-open, read-only-calendar, and validation coverage in `tests/test_dashboard_intelligence.py`.
- Wrote `docs/audits/2026-11-01-content-quality.md`: full-corpus structural checks, 23 article/20 note full-text samples, four fetched primary sources, and ranked improvements with acceptance checks.
- Applied the no-ai-slop writing pass to the audit report.

## Verified
- Before fix: four calendar tests failed on missing eligible dates.
- After fix: 46 dashboard intelligence and digest ranker tests pass (last run 2026-11-01).
- Corrected route returns HTTP 200 and all 31 October dates against a read-only production DB connection in an isolated FastAPI TestClient.
- Production route Ruff check and git diff whitespace check pass. Four import-order findings in untouched test blocks match HEAD exactly; no new lint findings.
- All 23 sampled articles and 20 sampled notes returned nonempty HTTP 200 API records. Live API confirms a persisted synthesis scored 1/5 with no human acceptance.
- Installed Compiler, Synthesizer, Scout, Escalator, GithubIdeator, and digest ranker match the inspected checkout.
- Scout's current extraction recipe reproduces the datasette release failure: navigation only, missing the dependency explanation present in the primary source.

## Pending
- User approval for a drained deployment/restart of ONLY the calendar route. The installed package is separate from this checkout; restarting alone will not activate the fix. Do not run a full dirty-checkout update that would include unrelated changes.
- After approved deployment, verify live `/api/digest/calendar?year=2026&month=10` returns all 31 dates and check the calendar interaction. Current live endpoint still marks only Oct 8, 18, 19.
- Quality improvements are recommendations only. No corpus cleanup, publication-policy changes, or regeneration was performed.

## Delivery requested
- Commit and push all pending source, tests, skills, and documentation to main.
- Remove merged worktrees; preserve untracked logs in a private archive first.
- Then implement the critic gate in a separate main commit: minimum score 4/5, one bounded revision, and exclusion of withheld/rejected syntheses from normal answers and digests.

## Shipping checks
- Full local backend suite: 1,154 passed.
- TypeScript checks and shell syntax pass.
- Ruff: 145 findings, exactly matching the pre-existing baseline; no new findings.
- Cloud browser/build checks are blocked by a full 20 GB disk. The Go build cache occupies 7.3 GB; permission to clear only that cache is pending.

## Constraints
- Preserve existing code outside the requested changes; all pending files are now authorized for commit/push.
- Exclude local `.know` runtime indexes and locks from version control.
- Production database and vault are read-only; any writes use API/CLI.
- No daemon restart or package replacement without explicit approval.
