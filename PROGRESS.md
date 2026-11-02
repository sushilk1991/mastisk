# Smart Settings

## Done
- Implemented the approved AI model card: Provider → Model → supported Thinking, followed by readable fallback behavior.
- Model dropdowns read native Codex/Claude/Muse catalogs or provider APIs. Discovery sends no prompts, starts no model turn, and performs no login/install.
- Shared CLI detection respects absolute PATH order, supplements minimal service paths, honors explicit overrides, and rejects relative PATH entries. Normal Settings hides paths and internal config keys behind Advanced.
- Provider browsing preserves saved fallback order. Saves send only edited fields. Model changes adjust only incompatible thinking for that provider; discovery errors preserve saved selections and cached lists.
- Added Claude effort forwarding, preserving explicit task-model overrides. Preserved the legacy resolver contract used by optional image generation. Fixed mobile footer overlap and ensured 44px control targets.

## Verified
- Linux full backend: 1,223 passed. macOS touched-area tests: 74 passed. New modules pass Ruff; full-repo findings remain at the existing 144, with no new touched-file findings.
- Frontend through boxrun: 22 Chromium tests, TypeScript, and production build passed. Reviewed desktop/light (1280×900) and mobile/dark (390×844) frames, both labelled with base text 16px.
- Actual new API probes returned 26 Ollama, eight Codex, 13 Claude, and four Muse models. The pinned DeepSeek tag advertises Off/Low/High/Max. Production config bytes were unchanged by these read-only probes.
- Tests cover metadata-only requests, pagination, credential redaction, cache refresh/invalidation, timeout/cancellation cleanup, queued-request deadlines, stale responses, missing saved models, partial saves, fallback preservation, keyboard navigation, and mobile layout.
- Live daemon/package/config were not replaced or restarted for this redesign. Current production deployment remains `77eb686`; its DeepSeek/high selection and existing fallbacks remain in place.

## Pending
- Obtain new approval before a drained deployment/restart. The live Settings page still serves the previous UI.
- Install the tested source/PWA together, preserving the current Python interpreter and installed dependency versions. Recheck actual page/assets, model dropdown API, save behavior, and local/tailnet health afterward.
- Authenticated Anthropic catalog listing was checked against the official API contract and mocked pagination, not a real authenticated API request. Existing configured Anthropic access was not changed.

## Artifacts and cloud state
- Private artifacts: `/tmp/mastisk-smart-settings-20261102.1O7wPz/`. Includes reviewed screenshots, sanitized native catalog replies, verification JSON, and tested PWA under `pwa/pwa/`.
- Cloud full disks required using the existing `/tmp/boxrun-mastisk-20261101` checkout. The original cloud checkout was preserved as `/data/workspace/mastisk-6d982900.before-smart-settings-20261102`; restore its ordinary path before stopping.
- Backend scratch was under `/var/tmp/mastisk-smart-pytest-20261102`; frontend/browser scratch used `/tmp` after the root disk filled. No caches were deleted. Serialize boxrun invocations: concurrent pull/run commands raced its SSH config temp file.

## Prior delivery
- Earlier calendar, critic-gate and provider work is deployed at `77eb686`; see `docs/audits/2026-11-02-deployment.md` and the content-quality audit.
- That deployment verified all 31 October calendar dates, actual Ollama Ask routing, critic filtering, matching UI assets, private/unrelated config preservation, and local/tailnet endpoints.
- Prior rollback evidence: `/tmp/mastisk-deploy-20261102.UOUYGa/`. Extra merged worktrees were archived under `~/Library/Application Support/Mastisk/worktree-archives/20261101-200717/`; only main remains registered.
