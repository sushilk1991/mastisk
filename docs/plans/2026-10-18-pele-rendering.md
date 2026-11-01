# Pele Rendering Implementation Plan

**Goal:** Render diagrams and newly generated chart artifacts locally with Pele, preserving existing Chart.js artifacts and stored Mermaid source.

**Approved design:** Replace the shared Mermaid.js renderer used by articles, podcasts, and Guru. Use Mermaid-based specs for new charts. Keep legacy chart configs readable. Apply existing theme tokens, adapt to container width, and disable diagram-triggered links/images. No content goes to pele.run.

**Architecture:** `MermaidBlock` remains the shared React interface. It lazy-loads Pele and mounts sanitized SVG with bounded source/output/edge limits, resize handling, zoom, and plain-text source/error fallback. `ArtifactCard` dispatches explicitly marked Mermaid chart specs to that component; legacy specs still go to Chart.js. ArtifactAgent generates and validates the new spec format without a database migration.

**Primary sources checked:** https://pele.run/api, /security, /theming, /compatibility; npm package metadata. Pele 1.6.0 supports Mermaid diagrams and XY/pie charts. Its mount API accepts a sanitizer, handles resize/zoom, and exposes destruction for cleanup. Security docs require app-owned options and safe error display. Existing app layout and colors stay unchanged.

## 1. Renderer and runtime tests

- Add exact Pele and DOMPurify dependencies; remove Mermaid.js after migration.
- Add Playwright/Chromium tests against actual components in an isolated fixture, without live API or vault access. Run browser tests on the cloud box.
- Write failing tests for real Pele flowchart/XY/pie SVG output, script/link/image rejection, plain-text parse fallback, source updates, and mount cleanup.
- Rewrite `frontend/src/components/MermaidBlock.tsx` behind its existing interface; add scoped Pele CSS tokens to `frontend/src/styles/mastisk.css`.
- Run frontend tests until green. No JavaScript execution from diagrams; no author-supplied render options.

## 2. New chart format and compatibility

- Add failing backend tests for prompt examples and validation of `{renderer: "pele", source: "…Mermaid…"}` chart specs. Keep legacy Chart.js validation behavior.
- Add component tests proving new-format charts use Pele and old charts still use Chart.js.
- Update `src/mastisk/agents/artifact_agent.py` and `frontend/src/components/ArtifactCard.tsx` minimally. Keep existing kinds, database schema, and source text.
- Reject malformed new-format charts instead of misrouting them to Chart.js. Preserve comparison/timeline/stat formats.

## 3. Verification, docs, and rollout

- Run frontend tests, build, and changed backend tests plus compiler/Guru/static diagram regressions.
- Compare typecheck against the existing baseline rather than changing unrelated code.
- Attempt cloud build/browser tests via boxrun; report unavailable infrastructure rather than hiding skipped checks. Inspect mobile/light/dark layouts and fallback in a browser when available.
- Document renderer, spec format, compatibility, security, and local-only behavior in README; point agents at it.
- Deploy only through the approved drain-before-install/restart path, preserving the daemon's Python interpreter and existing Tailscale Serve routes.

## Session state

User approved this design and implementation. Prior safe-restart/Tailscale changes remain uncommitted; preserve them. Production safe restart is deployed on Python 3.14, with page/API reachable at https://sushils-macbook-pro.tailec4b87.ts.net:8443/. Initial maintenance rollout showed that an unpinned uv install can select Python 3.15 and lose iCloud access; retain the running daemon's interpreter. Do not restart/reinstall outside the safe drain protocol.

## Verified implementation and deployment

- Pele 1.6.0 and DOMPurify 3.4.17 are exact runtime dependencies. Mermaid.js is removed; Chart.js remains for legacy charts. Test tooling is Playwright 1.64.1, not Vitest/jsdom; an initial Vitest candidate was removed after npm audit flagged new critical dependencies. Current audit has 14 findings in existing build tooling (no critical findings), versus 16 before removing Mermaid.
- Cloud job `20261018-200735-mastisk` passed 13 Chromium tests, TypeScript checks, frontend build, and the complete 1,146-test Python suite. The suite has one existing Starlette/httpx deprecation warning. Build warns about large chunks and old Browserslist metadata.
- Browser coverage includes flowcharts, XY/pie charts, legacy canvas charts, invalid specs, source fallback/recovery, script-looking labels, blocked external image/link requests, source limits, mobile reflow, theme changes without SVG replacement, keyboard/touch zoom, and observer cleanup. Reviewed mobile light/dark screenshots with device/viewport/theme/text-size annotations; no page overflow.
- Full-suite verification exposed a capture-triage test requesting a closed TestClient after app shutdown. Moved that assertion inside its existing client context; production restart code is unchanged.
- Live artifact-agent profile has no prompt override or slot overrides; the new default chart prompt will apply after deployment.
- README and CLAUDE document the renderer, new spec format, compatibility, and safety limits.

Cloud build was pulled into the checkout's ignored `src/mastisk/pwa` directory after validation. The old regenerable build was moved into the artifact backup, not deleted. Prepared wheel: `/tmp/mastisk-pele-build.fGQP71/wheel/mastisk-0.1.0-py3-none-any.whl`; frozen dependency constraints and `deployment.json` are in `/tmp/mastisk-pele-build.fGQP71/`. `/tmp/mastisk-pele-build-current` points there. The wheel contains the Pele/sanitizer assets and new generation prompt; both renderer chunks are in the PWA precache. Production remains unchanged and its local API returned 200.

The user approved deployment. `_drain_session` waited 53.65 seconds for one active run to finish, then held admission closed while the prepared wheel was installed with the live daemon's Python 3.14 interpreter and frozen constraints. `_restart_idle` verified the replacement daemon's health. No active run was force-cancelled and no existing wiki artifacts were regenerated.

Post-deployment checks passed: new daemon identity and launchd PID agree; installed source matches the checkout; new Pele chart validation/prompt are present; configuration, launchd plist, and every Tailscale Serve route match pre-deployment fingerprints. Local API, tailnet page/API, and Mobius root return HTTP 200. TLS responses for Pele, DOMPurify, app JavaScript, and CSS exactly match installed build assets; renderer chunks are precached. Proxy-marked API requests still return 404. One immediate control read timed out after startup; a subsequent status read and all verification checks succeeded without another restart.

Next user action: reload Mastisk at https://sushils-macbook-pro.tailec4b87.ts.net:8443/ . Diagrams use Pele; newly generated charts use Pele; existing Chart.js charts remain unchanged. Phone-side browser verification is still the user's check. No commits or pushes made.
