# Synthesis Critic Gate Implementation Plan

**Goal:** Prevent syntheses below 4/5 or without a valid critic score from reaching ordinary answers and digests.

**Architecture:** Draft, critique, optionally revise once, then publish only on a valid score of 4–5. Withheld attempts keep their cluster IDs, score, and rationale in `synthesis_runs`, with no article or vault page. A shared database predicate applies the latest critic/review result to existing syntheses. Explicit human acceptance overrides the numeric score; rejection always withholds that run. Source articles remain unaffected. No production migration or cleanup is needed.

**Tech stack:** Python, SQLite, FastAPI, pytest.

## 1. Regression tests

Create `tests/test_synthesis_gate.py` with real temporary SQLite and vault fixtures. Exercise the generation entry point with deterministic bridge responses, not live models. Assert that:

- A 4/5 draft publishes; low or missing scores do not create an article or vault file.
- A low score permits only one revision; a passing revision publishes, a failing revision does not.
- Invalid scores fail closed; the final score line controls the result.
- Failed attempts retain critic evidence and suppress repeated generation for unchanged clusters.
- Low, missing, rejected, and stale-success syntheses are absent from digest ranking/calendar/navigation and normal Ask retrieval.
- Human-accepted syntheses and ordinary Source articles remain available.

Run the focused tests and observe the missing-gate failures before implementation.

## 2. Publication and read policy

Create private score constants/helpers in `src/mastisk/synthesis_policy.py`. Modify `src/mastisk/agents/synthesizer.py` to enforce the threshold before persistence, add one critic-directed revision, and reject malformed/out-of-range scores. Bump the prompt version.

Add private visibility helpers to `src/mastisk/db/queries.py`, based on the latest synthesis run ordered by timestamp and ID. Use them in article search, `src/mastisk/routes/ask.py`, `src/mastisk/services/digest_ranker.py`, and `src/mastisk/routes/digest_route.py`. Do not delete existing articles or alter production data.

Run focused tests, then the full backend suite. Existing ranker fixtures that create a Synthesis must now record a passing critic result.

## 3. Verification and delivery

Document the threshold, bounded revision, manual acceptance override, and withheld-run evidence in README. Update PROGRESS.md. Verify behavior against the live database through read-only connections; do not start the modified daemon or migrate production as a test.

Run Python tests, compare Ruff with baseline, and run browser/build checks through boxrun after cloud disk capacity is restored. Commit directly to main as requested, push normally, and verify origin/main. Remove merged worktrees only after preserving their untracked agent logs. Deployment/restart remains separately approval-gated.
