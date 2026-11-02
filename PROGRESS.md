# Mastisk delivery and deployment

## Done
- Main commits: `45f80c8` collected pending renderer/restart/calendar work; `c0aaffe` added the critic gate; `77eb686` added provider controls and Muse support.
- Calendar dates depend on eligible articles, not prior digest requests.
- Synthesis publication requires a valid 4/5 score, with at most one revision. Low/missing/invalid grades are withheld. Existing low/unscored/rejected syntheses are filtered from ordinary Ask/search/digests; explicit human acceptance and direct article review remain supported.
- Settings now exposes provider priority, Ollama thinking, and Claude/Codex/Muse CLI/model controls. Nested settings saves are validated, atomic, and private.
- Deployed `77eb686` under the user's approved safe drain. Installed 58 packages with all 57 non-Mastisk dependency versions and the daemon's existing Python 3.14 preserved. Replacement daemon identity and health were verified.
- Applied the user's runtime choice through the live API: `deepseek-v4.1-flash:1017-cloud`, `think: high`, provider order Ollama → Codex → Claude → Muse, automatic Anthropic prepending disabled. Cheap and embedding models were unchanged.
- Removed both merged extra worktrees. Complete private archives are under `~/Library/Application Support/Mastisk/worktree-archives/20261101-200717/`. Main is the only registered worktree.

## Verified
- Full macOS and Linux backend suites: 1,197 passed each. Frontend through boxrun: 15 Chromium tests, TypeScript, and production build passed.
- Full Ruff: 144 existing findings versus the 145 baseline; no new findings. New modules pass lint; legacy findings remain in existing files.
- Real probes passed for DeepSeek high thinking, Codex read-only execution, and Muse tool-free execution. Ollama metadata confirms the pinned tag supports `high`.
- Installed source matches all 153 committed Python modules. Local and TLS-served PWA assets exactly match the tested build.
- Live local and tailnet page/API return 200. Both calendar endpoints return all 31 October dates. New Codex/Muse controls and the selected thinking/model settings are returned by the live settings API.
- Live Ask returned `MASTISK_PROVIDER_READY` using provider `ollama`; its temporary verification conversation was removed through the API.
- Live search excludes the known 1/5 synthesis while direct review still works. The October 29 digest returned 83 items; all eight selected syntheses met their actual critic/review policy.
- Unrelated configuration, mode 0600, launchd plist, all Tailscale Serve routes, other-app HTTPS availability, and forwarded-header blocking were preserved.

## Deployment evidence
- Private artifact directory: `/tmp/mastisk-deploy-20261102.UOUYGa/` contains the tested wheel, source/build fingerprints, frozen dependencies, prior package/config/database snapshots, deployment log, and verification manifest.
- Wheel SHA256: `915e444a4fab42266e8dbbf78dce0e303b626f5237408eb004e2ebdb12a9af58`.
- Cloud data disk was full; tests used temporary storage. Prior test artifacts were preserved under `/var/tmp/mastisk-pytest-preserved-20261102/`, and the original cloud checkout was restored. No caches were deleted.

## Pending
- Observe the next post-restart synthesis publication/withheld outcome; no new synthesis-run row appeared during verification. No content was generated solely to force this check.
- Phone-side browser interaction remains a user check. Server responses, real counts, and served UI assets were verified from the Mac.
- Provenance-preserving note promotion, long-history dedup, mixed digest ranking, and extraction retries remain recommendations, not implemented features.

## Constraints
- Deployment approval was used for this single drained install/restart. Another restart still needs approval.
- Production database/vault writes remain API/CLI-only. No release tag was requested.
