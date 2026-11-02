# Smart Settings

## Done
- Source `a280819` is deployed after the approved drained install/restart. It implements the AI model card: Provider → Model → supported Thinking, followed by readable fallback behavior.
- Model dropdowns read native Codex/Claude/Muse catalogs or provider APIs. Discovery sends no prompts, starts no model turn, and performs no login/install.
- Shared CLI detection respects absolute PATH order, supplements minimal service paths, honors explicit overrides, and rejects relative PATH entries. Normal Settings hides paths and internal config keys behind Advanced.
- Provider browsing preserves saved fallback order. Saves send only edited fields. Model changes adjust only incompatible thinking for that provider; discovery errors preserve saved selections and cached lists.
- Added Claude effort forwarding, preserving explicit task-model overrides. Preserved the legacy resolver contract used by optional image generation. Fixed mobile footer overlap and ensured 44px control targets.

## Verified
- Linux full backend: 1,223 passed. macOS touched-area tests: 74 passed. New modules pass Ruff; full-repo findings remain at the existing 144, with no new touched-file findings.
- Frontend through boxrun: 22 Chromium tests, TypeScript, and production build passed. Reviewed desktop/light (1280×900) and mobile/dark (390×844) frames, both labelled with base text 16px.
- Live daemon API probes returned 26 Ollama, eight Codex, 13 Claude, and four Muse models. DeepSeek still uses high thinking and advertises Off/Low/High/Max. Live Ask returned `MASTISK_SETTINGS_READY` through Ollama; the temporary conversation was removed through its API.
- Tests cover metadata-only requests, pagination, credential redaction, cache refresh/invalidation, timeout/cancellation cleanup, queued-request deadlines, stale responses, missing saved models, partial saves, fallback preservation, keyboard navigation, and mobile layout.
- All 173 installed package files were verified. Local and tailnet `/settings`, settings API, and all 14 served PWA files match the tested build. A real no-op settings save succeeded with byte-identical config. Python 3.14, all 57 dependency versions, config mode 0600, launchd plist and every Tailscale Serve route were preserved. iCloud identity reads and the other app on TLS port 443 remain healthy; proxy-marked API access remains blocked.

## Pending
- Phone-side interaction has not been observed. Reload `/settings` to load the new UI; server assets, native model lists and settings-save roundtrip are verified.
- This approval was used for one drained install/restart. Future restarts need fresh approval.
- Authenticated Anthropic catalog listing was checked against the official API contract and mocked pagination, not a real authenticated API request. Existing configured Anthropic access was not changed.

## Artifacts and cloud state
- Private artifacts: `/tmp/mastisk-smart-settings-20261102.1O7wPz/`. Includes reviewed screenshots, sanitized native catalog replies, verification JSON, tested PWA under `pwa/pwa/`, and `wheel/mastisk-0.1.0-py3-none-any.whl`. All 155 packaged Python modules and 14 PWA files match the committed source/tested assets.
- Installed wheel SHA256: `cb16d3e82d0e575cbc0f9e8958881311a1eb6bfcaa88e1b1313c3e8186e07653`. Source revision: `a2808193d338a8c9e45f79fb41e9cb0228171094`. Private install log, before/after identities, rollback wheel/package/database/config snapshots and live verification are under `deployment-5gzu22zq/` in the artifact directory.
- Cloud full disks required using the existing `/tmp/boxrun-mastisk-20261101` checkout. The original cloud checkout is restored at `/data/workspace/mastisk-6d982900`; the tested temporary checkout remains preserved under `/tmp`.
- Backend scratch was under `/var/tmp/mastisk-smart-pytest-20261102`; frontend/browser scratch used `/tmp` after the root disk filled. No caches were deleted. Serialize boxrun invocations: concurrent pull/run commands raced its SSH config temp file.

## Prior delivery
- Earlier calendar, critic-gate and provider work was deployed at `77eb686` and remains included; see `docs/audits/2026-11-02-deployment.md` and the content-quality audit.
- That deployment verified all 31 October calendar dates, actual Ollama Ask routing, critic filtering, matching UI assets, private/unrelated config preservation, and local/tailnet endpoints.
- Prior rollback evidence: `/tmp/mastisk-deploy-20261102.UOUYGa/`. Extra merged worktrees were archived under `~/Library/Application Support/Mastisk/worktree-archives/20261101-200717/`; only main remains registered.
