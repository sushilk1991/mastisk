# Deployment verification: 2026-11-02

Deployed source revision `77eb686` after explicit user approval. This includes the digest calendar fix, 4/5 critic gate, provider settings, and Muse adapter.

## Gate and install

- 1,197 backend tests passed on macOS and Linux. Fifteen Chromium tests, TypeScript, and the production frontend build passed through boxrun.
- All 153 packaged Python modules and PWA asset bytes were checked against the committed source/tested build before installation.
- The safe drain reached zero admitted work before replacing package files. The restart driver rechecked the idle lease and verified a replacement daemon identity and HTTP health.
- Python 3.14 and all 57 non-Mastisk dependency versions were preserved. No active work was force-cancelled.
- Wheel SHA256: `915e444a4fab42266e8dbbf78dce0e303b626f5237408eb004e2ebdb12a9af58`.

## Live results

- Default shared provider: Ollama, using `deepseek-v4.1-flash:1017-cloud` with `think: "high"`. Fallbacks: Codex, Claude, Muse. Automatic Anthropic prepending is disabled for this installation.
- Live Ask returned the requested verification marker with provider `ollama`. The test conversation was deleted through its API afterward.
- Local and tailnet calendar APIs return 31 October dates. New model/thinking and Codex/Muse settings are present in the live API.
- Live search excludes `valid-outputs-outrun-evidence-trails`, whose recorded score is 1/5. Direct article access remains available for review. The October 29 digest returned 83 items; its eight syntheses met the actual critic/review policy.
- Both local and TLS-served PWA assets match the tested build. Local/tailnet page and API return 200; the other app on TLS port 443 remains reachable; proxy-marked API requests remain blocked with 404.

## Preserved and not yet checked

Unrelated configuration values, private config mode, launchd plist, all Serve routes, cheap-model selection, and embeddings were preserved. Configuration changes went through the live settings API.

No new synthesis-run row appeared during post-restart verification, so a new scheduled publication/withheld event remains unobserved. Phone-side browser interaction was not checked. These limits do not change the verified live filtering and model-routing results.

Private rollback snapshots, logs, and fingerprints are under `/tmp/mastisk-deploy-20261102.UOUYGa/`; secrets and database snapshots were not committed.
