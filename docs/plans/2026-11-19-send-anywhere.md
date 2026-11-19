# Send to Mastisk from any machine

Date: 2026-11-19. Branch: `send-anywhere` (local only, not pushed, no pull request).

## Outcome

The code is done and its tests pass. Nothing has been deployed: the running daemon was not
touched, so it does not report `public_url` yet, and the extension has not been loaded in a
real Chrome. The rollout steps and the owner's decisions are at the end.

- The daemon accepts an optional `[server] public_url` and returns it from `GET /api/health`.
- The extension picks its server on its own: saved URL, then `http://localhost:5555`, then a
  public address synced from another of the owner's browsers.
- Every link the extension hands out uses the public address when the daemon reports one.
- `extension/scripts/package.sh` builds a zip for an unlisted Chrome Web Store listing.

## What changed

Daemon

| File | Change |
|---|---|
| `src/mastisk/settings.py` | `ServerSettings.public_url: str \| None = None`, with a validator: trims, drops a trailing slash, empty means unset, rejects anything that is not an `http(s)` URL or has a query or fragment. |
| `src/mastisk/app.py` | `/api/health` returns `{"ok": true, "public_url": <value or null>}`. |
| `tests/test_health_public_url.py` | New. Health with the setting unset, omitted and set; the host allowlist is unchanged by it; normalisation; rejected values; loading from `config.toml`. |
| `tests/test_extension_remote_host.py` | New. The extension's routes as they arrive on the public host: tailnet `Host`, a `chrome-extension://` origin, no proxy headers. |

Nothing else in the daemon changed. `public_url` does not affect what the daemon listens on,
CORS, or `local_hostnames`.

Extension (version 1.1.0 → 1.2.0, no new permissions)

| File | Change |
|---|---|
| `extension/lib/server.js` | New. Pure functions: URL normalisation, candidate order, health parsing, `resolveServer`, link base, article URL, host-permission pattern. Loaded by the worker, the pages and the Node tests. |
| `extension/background.js` | Resolves the server through `lib/server.js`, caches the choice for 60 s in `storage.session`, writes a reported public URL to `storage.sync` when it changes, retries once on a refused connection, pins each queued clip to the server that accepted it, builds notification links from the public URL. |
| `extension/sidepanel.{js,html,css}` | Cited sources link to the public address, are real `href`s, and each has a **Copy link** button. |
| `extension/popup.{js,html}` | Shows which server answered, an **Open wiki** link, and an **Allow access to \<host\>** button when Chrome is withholding the host. |
| `extension/options.{js,html}` | Explains the order and shows the synced public address. |
| `extension/scripts/package.sh` | New. Allowlisted zip in `extension/dist/` (already ignored by git). |
| `extension/tests/*.js` | New. 20 tests of the URL rules, 15 of the worker against a fake `chrome` and network. |
| `tests/test_extension_server_resolution.py`, `tests/test_extension_package.py` | New. Run the Node tests and the package script under `pytest`. |
| `tests/test_extension_context_menus.py`, `tests/test_extension_media_routing.py` | The two existing worker harnesses now provide `importScripts`, `chrome.permissions` and `/api/health`. Their assertions are unchanged. |

Docs: `README.md` (Phone setup → "Tell clients the public address"), `extension/README.md`,
`docs/agents/api.md` (the health field).

## How it behaves

**Server choice.** Candidates are probed together and read in order, so an unreachable address
costs one 4-second timeout in total. The first that answers `/api/health` with `ok: true` wins:

1. the Server URL saved in Settings;
2. `http://localhost:5555`;
3. the `publicUrl` key in `chrome.storage.sync`.

**Remembering the public address.** When the winning daemon reports `public_url`, the worker
writes it to `storage.sync` under its own key, only if it differs from what is stored. It is
kept apart from `settings.serverUrl` on purpose: overwriting the saved URL would send the
daemon's own machine through the tailnet. A daemon that reports none does not clear it.

**Links.** Link base = the answering daemon's `public_url`, else that daemon's address. Used by
the *Indexed* and *Already in your wiki* notifications, side-panel citations and the popup's
**Open wiki**. A queued clip stores its server and link base when it is accepted; status
polling and the final link use those, not whatever resolves minutes later.

**Copy link.** Added to the side panel's Sources list. Not added to notifications: a service
worker has no clipboard access. The popup had no article links before this change and still
has none (see open decision 5).

**Failure handling.** A refused connection drops the cached choice, resolves again and retries
once on a different server. A timeout is not retried, because the POST may have landed.

## Host permission (item 4)

Neither option was needed. `extension/manifest.json` already declares
`host_permissions: ["<all_urls>"]`, and commit `0f12048` removed the optional host patterns
because Chrome warned they were redundant under it. `<all_urls>` is required for "Send link",
which scripts a background tab on an arbitrary site. So a tailnet HTTPS address is reachable
with no prompt on every machine, and adding `https://*.ts.net/*` would change nothing.

I kept `<all_urls>` and added one thing: if the owner narrows site access in
`chrome://extensions`, the worker detects the withheld host with `chrome.permissions.contains`
and the popup offers a one-click grant. The cost of `<all_urls>` is a slower Web Store review
(open decision 3).

## Verification

Run locally on 2026-11-19 (Node v24.11.0). The UI render ran on the `build` box.

```
$ uv run ruff check src/mastisk/settings.py src/mastisk/app.py tests/test_health_public_url.py \
    tests/test_extension_server_resolution.py tests/test_extension_package.py \
    tests/test_extension_context_menus.py tests/test_extension_media_routing.py
All checks passed!

$ uv run ruff check            # whole repository
Found 52 errors.
```

The 52 are not from this branch: a clean export of `main` at `7945391` gives the same
`Found 52 errors.`, and none are in the files touched here.

```
$ uv run pytest tests/test_health_public_url.py -v
test_health_reports_null_public_url_when_unset PASSED
test_health_reports_null_public_url_when_server_table_omits_it PASSED
test_health_reports_configured_public_url PASSED
test_public_url_does_not_extend_the_host_allowlist PASSED
test_public_url_is_normalised[...] PASSED            (7 cases)
test_public_url_rejects_values_a_browser_could_not_open[...] PASSED   (5 cases)
test_config_toml_public_url_loads_into_settings PASSED
============================== 17 passed in 0.75s ==============================

$ uv run pytest tests/test_extension_remote_host.py -v
test_health_and_web_clip_work_on_the_public_host PASSED
test_listen_and_ask_are_reachable_on_the_public_host PASSED
test_the_same_requests_are_hidden_when_they_carry_proxy_headers PASSED
============================== 3 passed in 0.68s ===============================

$ uv run pytest tests/test_health_public_url.py tests/test_extension_remote_host.py \
    tests/test_settings_env_isolation.py tests/test_capture_settings.py \
    tests/test_provider_settings.py tests/test_settings_github.py tests/test_tunnel_scope.py \
    tests/test_phase15_editor_backend.py tests/test_integration_health.py tests/test_ingest_web.py \
    tests/test_restart_control.py tests/test_extension_context_menus.py \
    tests/test_extension_media_routing.py tests/test_extension_server_resolution.py \
    tests/test_extension_package.py -q
105 passed in 6.47s

$ node --test "extension/tests/*.test.js"
ℹ tests 35
ℹ pass 35
ℹ fail 0
```

`test_extension_remote_host.py` covers the daemon's side of the other-machine path, which the
extension's fake network cannot: with the public host in `local_hostnames`, requests carrying
that `Host` and an extension origin reach `/api/health`, `/api/ingest/web` (202, then its
status), `/api/listen` and `/api/ask` (422 on an empty body, so the route was reached and no
work ran). No auth or host check turns them away.

Nothing else reads the `/api/health` body: `mastisk restart` checks `/api/sidebar`, and there
is no other caller in `src/`, `frontend/src/`, `install.sh` or `skills/`.

The worker tests were checked by breaking the code on purpose. Each of these five edits made
a test fail: polling the wrong server, building the *Indexed* link without the public URL,
writing to `storage.sync` on every probe, retrying after a timeout, caching a resolution that
predates a saved-URL change.

`package.sh` output: `mastisk-clipper-1.2.0.zip`, 17 files, `manifest.json` at the root, no
README, tests or scripts.

The popup, options page and side panel were rendered once in headless Chromium on the `build`
box with a stub `chrome` object, in light and dark, and the screenshots inspected. One defect
found and fixed (the Settings link lost its right alignment). This checks layout only. In the
dark side-panel capture the connection dot was grey, not green; the light capture of the same
code was green. I did not find the cause. The dot has a 0.2 s colour transition, so the capture
may have caught it mid-change.

The full backend `pytest` suite and the frontend gate were not run; the frontend is untouched.

## Two facts checked against Chrome's documentation

**Does `chrome.storage.sync` sync between two machines for an unpacked extension loaded from
different paths?** No, as far as the sources go. The answer rests on two links of different
strength:

- An unpacked extension's id is a hash of its folder path, so different paths give different
  ids. Source: Chromium, `GenerateIdForPath` in
  [`components/crx_file/id_util.cc`](https://github.com/chromium/chromium/blob/main/components/crx_file/id_util.cc)
  (SHA-256 of the path). Primary source, read on 2026-11-19.
- Sync data is kept per extension id, so different ids share nothing. Source: the two
  chromium-extensions threads below, where a developer says sync needs the same id on every
  device.
  **Chrome's documentation does not state this**; the
  [storage API reference](https://developer.chrome.com/docs/extensions/reference/api/storage)
  says only that data "syncs with every Chrome browser that the user is logged into".
- Whether an unpacked copy syncs once the ids match (same path on both machines, or a manifest
  [`key`](https://developer.chrome.com/docs/extensions/reference/manifest/key)) is **not
  stated in Chrome's documentation**. Developers on the chromium-extensions list report that
  it does ([2022 thread](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/MmPE0Zl1KoQ),
  [2022–2024 thread](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/qp_087h_vrU)).
  Treat that as community-reported and untested here.

So "works on the other machine with nothing typed" is only assured for a store install.

**What does Chrome permit on macOS outside the store?** The quoted sentences were checked
word for word against the fetched pages on 2026-11-19.

- Local `.crx`: not allowed. "As of Chrome 44, no external installs are allowed from a path to
  a local CRX file on Mac OS."
- `External Extensions/<id>.json`: store items only. "On Windows and macOS, the update_URL
  must point to the Chrome Web Store." Chrome asks the user to confirm.
  ([Alternative installation methods](https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions))
- Force-install by policy from a self-hosted URL: only when the Mac "is managed via MDM,
  joined to a domain via MCX or enrolled in Chrome Enterprise Core"
  ([`ExtensionInstallForcelist` policy definition](https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Extensions/ExtensionInstallForcelist.yaml);
  the rendered policy page did not load for me, so this is the Chromium source for it).
- Load unpacked with Developer mode: allowed, by hand on each machine.
- Unlisted store item: "does not create a listing" and "allows anyone to install your item if
  they know its Chrome Web Store URL"; "All visibility settings ... go through the same review
  process" ([distribution](https://developer.chrome.com/docs/webstore/cws-dashboard-distribution)).
  Registration needs a one-time fee ([register](https://developer.chrome.com/docs/webstore/register)).

The one-step install on macOS is therefore the store link. `package.sh` prepares the upload.

## Untested

All of this needs a real Chrome and, for most of it, the upgraded daemon:

- Loading the extension unpacked and clipping a page, a selection and a link.
- Reaching the tailnet HTTPS address from the service worker on a second machine.
- `chrome.storage.sync` carrying `publicUrl` between two machines, for a store install or an
  unpacked one.
- The **Allow access** button: `chrome.permissions.request` re-granting a host that was
  withheld from a required `<all_urls>` permission.
- **Copy link** writing to the clipboard in the real side panel.
- Chrome's Local Network Access checks. A reply on the chromium-extensions list says
  extensions with host permissions are not affected; not confirmed here.
- Clicking a notification on macOS.
- The side panel's connection dot in dark mode (grey in one headless capture, see above).
- Uploading the zip to the Developer Dashboard and passing review.
- The running daemon: it was not restarted, so `curl http://127.0.0.1:5555/api/health` still
  returns only `{"ok": true}`. The extension treats that as "no public address".
- Opening a public link from the phone.

## Rollout (not done; each step is the owner's)

1. Upgrade the daemon to this change by the procedure in
   [README: Safe restarts](../../README.md#safe-restarts), which drains active runs before any
   package file is replaced. Do this before step 2: `[server]` rejects unknown keys, so a
   daemon without this change will not start if `public_url` is already in `config.toml`.
2. Add `public_url = "https://<machine>.<tailnet>.ts.net:8443"` under `[server]` in
   `config.toml` and restart once more. Check `curl -s http://127.0.0.1:5555/api/health`.
3. Reload the unpacked extension on the main machine and open the popup once. That writes the
   public address to `storage.sync`.
4. On the other machines, install the extension (see decision 2) and open the popup.

## Open decisions for the owner

1. **The real tailnet and machine name are already in this public repository.** On `main`
   they appear in `README.md` (2 lines, Phone setup), `tests/test_tunnel_scope.py` (2),
   `docs/plans/2026-10-18-pele-rendering.md` (2) and `docs/plans/2026-10-18-safe-restart.md`
   (1). This branch adds none and leaves those lines alone. Replacing them with placeholders
   is a small change; removing them from git history means a rewrite and a force-push.
2. **How to install on the other machines.** Unlisted store listing: one click per machine and
   sync is assured, but it needs a developer account, the fee and a review. Or load unpacked
   everywhere from the same absolute path (or with a manifest `key`): no review, but sync is
   only community-reported, and without it you type the address once per machine.
3. **Keep `<all_urls>` for the store.** Chrome's docs say reviews "may take longer" with it.
   Narrowing it means reworking "Send link" around an optional-permission prompt.
4. **Add a manifest `key`.** After a first dashboard upload, its public key can go in
   `manifest.json` so an unpacked copy shares the store item's id and synced data.
   `package.sh` strips `key` from the zip either way.
5. **A recent-clip link in the popup.** The popup has no article link today. A "last indexed"
   row with **Copy link** would give a reliable place to copy a clip's link, since macOS often
   suppresses notifications. Not built: it is a new feature, not a link fix.
6. **Trust in `public_url`.** The extension adopts whatever the daemon at the saved URL or on
   localhost reports, and other machines then send clips there. Restricting adopted values to
   `https://*.ts.net` would narrow that at the cost of other address styles.
7. **A machine with its own daemon.** There, localhost wins over the synced public address.
   To force the main instance, save its URL in Settings on that machine.
8. **Strict validation.** A malformed `public_url` stops the daemon from starting, like any
   other bad config value. The alternative is to log and ignore it.

## Not done on purpose

- No `mastisk ingest` of these learnings: the task forbids writes to the database.
- No daemon restart, no Tailscale Serve change, no edit to the live `config.toml`.
- No push and no pull request.
