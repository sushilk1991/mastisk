# Smart Settings deployment: 2026-11-02

Deployed source `a280819` after approval for one drained install/restart. The running daemon now serves the compact provider/model/thinking form and native model-discovery API.

## Install gate

- Pre-install gate: 1,223 Linux backend tests, 74 targeted macOS tests, 22 Chromium tests, TypeScript, production build; no new Ruff findings.
- Held the user-only safe-drain lease across rollback snapshots, installation, file/dependency checks and restart. Zero admitted runs remained at installation; no model call was force-cancelled.
- Preserved the daemon's actual Python 3.14 interpreter and all 57 non-Mastisk package versions. Verified all 173 installed package files against the tested wheel before restarting.
- Wheel SHA256: `cb16d3e82d0e575cbc0f9e8958881311a1eb6bfcaa88e1b1313c3e8186e07653`.

## Live checks

- Local and tailnet `/settings` and settings API return 200. All 14 served PWA files match the tested build on both endpoints.
- Live catalogs return 26 Ollama, eight Codex, 13 Claude and four Muse models. CLI paths are detected; DeepSeek 1017 exposes Off/Low/High/Max. These are observations at deployment time, not permanent model lists.
- A real settings save of the existing thinking value returned success; config bytes and mode 0600 remained unchanged. Existing provider order and DeepSeek/high selection were preserved without migration.
- Live Ask returned exactly `MASTISK_SETTINGS_READY` through Ollama. Its temporary verification conversation was deleted through the API.
- The new daemon reads its nonempty iCloud vault identity. Launchd plist and all Serve routes are unchanged; the other application on TLS port 443 remains healthy; proxy-marked API access is still blocked.

Desktop/light and mobile/dark frames were reviewed before deployment; the live assets match that tested build. Phone-side interaction was not observed. Authenticated Anthropic catalog access remains covered by contract/mocked tests rather than a real authenticated request.

Private rollback snapshots, log and verification manifest: `/tmp/mastisk-smart-settings-20261102.1O7wPz/deployment-5gzu22zq/`. No secrets or database snapshots were committed.
