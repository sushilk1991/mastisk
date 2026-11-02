# Provider settings and approved deployment

## Goal

Make the user's shared Mastisk intelligence chain prefer Ollama's `deepseek-v4.1-flash:1017-cloud` with `think: "high"`. Add real Codex/Muse controls to Settings, then deploy the calendar fix, critic gate, and provider changes together under one approved drain/restart.

## Boundaries

- Configure this installation through the settings API; preserve other installations' existing defaults and explicit task-specific routes.
- Shared chain and Ask follow the configured order. Keep the local cheap model and embedding model unchanged.
- Preferred order: Ollama, Codex, Claude, Muse. Disable automatic Anthropic prepending for this explicit choice.
- Add a stateless Muse adapter with tools disabled, isolated working directory, bounded runtime, and JSONL terminal-result validation. No workspace trust, approval bypass, or unrestricted shell.
- Expose Codex/Muse executable paths, model pins, reasoning effort, primary provider, and Ollama thinking in the existing Settings layout.
- Validate and atomically merge settings without losing nested tables, secrets, or unrelated fields. Persist mode 0600.

## Primary evidence checked

On 2026-11-02, Ollama 0.40.13 `/api/show` for the pinned tag reports thinking values `[false, "low", "high", "max"]`, default `"high"`. Official Ollama thinking docs specify the request's `think` field; reasoning and final content are separate. Muse Code 1.4.14 `exec --help` supports an empty preset, tool-disabling flags, JSONL, model/effort pins, and bounded model steps. Its echo provider verified the `run.terminal.completed` event shape.

## Implementation

1. Add failing API/bridge tests for nested persistence, invalid provider orders, DeepSeek thinking, configured Codex paths, and Muse execution/result failures.
2. Add a failing browser test using the real Settings component with mocked HTTP, including accessible labels and save/reload behavior.
3. Extend settings contracts and typed frontend fields; wire runtime provider controls and remove Ask's hardcoded default override.
4. Implement isolated Muse execution and wire it into the shared chain. Preserve current Codex read-only sandbox behavior.
5. Run focused tests, full backend tests, baseline-aware lint, TypeScript, browser tests, and build through boxrun.
6. Commit/push to main as requested. Prepare a wheel from committed source and tested assets with frozen installed dependencies.
7. Under the approved safe drain, install using the daemon's current Python, restart, apply runtime settings via the live API, and verify actual responses, calendar dates, critic filtering, settings UI, TLS endpoints, and unchanged routes/config outside the approved keys.

## Deployment status

No reinstall or restart has occurred. The approval covers the combined deployment after all validation passes. Keep the existing Python 3.14 interpreter; prior Python 3.15 deployment lost iCloud permissions.
