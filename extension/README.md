# Mastisk Clipper

A Chrome extension (Manifest V3) that clips pages, selections, and links into your
Mastisk personal knowledge wiki, and gives you a side panel to chat with the whole wiki plus
the page you're on. It works from any of your computers against one Mastisk daemon: the one
on this machine, or your main instance over your tailnet.

No build step — load the folder as-is.

## Features

- **Right-click → Send to Mastisk**
  - *Send page to Mastisk* — routes primary video/podcast pages to the Listener;
    ordinary pages are extracted as Markdown with Defuddle and indexed by the Compiler.
  - *Send selection to Mastisk* — clips the highlighted text.
  - *Send link to Mastisk* — routes recognizable media URLs directly; otherwise opens the
    link in a background tab, detects media or extracts text, indexes it, then closes the tab.
- **Indexing feedback** — a desktop notification when the clip is sent, the toolbar badge shows `…`
  while indexing, and you get an *Indexed: <title>* notification when it lands. Click it to open the
  compiled wiki article.
- **Side panel chat** — ask questions across your wiki. By default the current page is included so
  Mastisk can draw parallels between what you're reading and what you've saved. The cited-sources
  list links straight to the underlying wiki articles, each with a **Copy link** button.
- **Links that open anywhere** — when the daemon reports a public address, every link the
  extension hands you uses it, so a link copied on your laptop opens on your phone.

## Which server it uses

The extension tries these in order and uses the first that answers `GET /api/health`:

1. The **Server URL** saved in Settings, if you saved one.
2. `http://localhost:5555`, a daemon on this computer.
3. The **public address** a Mastisk daemon reported earlier.

The public address comes from the daemon's config (`[server] public_url`, see
[Phone setup](../README.md#phone-setup)), for example
`https://<machine>.<tailnet>.ts.net:8443`. When a daemon that answers reports one, the
extension stores it in `chrome.storage.sync`. A computer with no daemon of its own then finds
your main instance at step 3 with nothing typed in. The popup shows which server answered.

The choice is cached for a minute. If the connection is refused (the laptop left the tailnet,
a daemon stopped), the extension looks again and retries once on another server. It does not
retry after a timeout, because the clip may already have been accepted.

A public address is taken on trust from whichever daemon answers at step 1 or 2 and is then
used by your other browsers. Only run the extension against daemons you control.

### Links

Links use the public address when the daemon that took the request reports one, otherwise the
address of that daemon. This covers the *Indexed* and *Already in your wiki* notifications,
the cited sources in the side panel, and **Open wiki** in the popup. A clip's link is fixed
when the daemon accepts it, so it still points at the right instance if you change networks
while it is indexing.

**Copy link** is in the side panel's Sources list. Cited links are also real links, so
right-click → *Copy link address* works. Notifications have no copy button: a service worker
has no clipboard access of its own.

### Host permission

The manifest already asks for `<all_urls>` at install. It needs that to read a link you
right-clicked, which it opens in a background tab. A tailnet address is therefore reachable
with no prompt and no trip to Settings, on every machine. Declaring `https://*.ts.net/*` as
well would change nothing, and Chrome drops optional host patterns that `<all_urls>` already
covers.

If you narrow the extension's site access in `chrome://extensions`, the popup shows
**Allow access to <host>** when the server it needs is blocked. One click grants it.

## Install

### From the Chrome Web Store (one step)

On macOS and Windows the store is the only one-step install Chrome offers. Publish the
extension as **Unlisted**: it gets no store listing and anyone with the link can install it.
Unlisted items go through the same review as public ones, and `<all_urls>` can make that
review take longer.

```bash
extension/scripts/package.sh        # writes extension/dist/mastisk-clipper-<version>.zip
```

The zip holds only what the extension runs, with `manifest.json` at its root, and without a
manifest `key` if you added one for development. Upload it in the
[Developer Dashboard](https://chrome.google.com/webstore/devconsole) (one-time registration
fee), set **Visibility → Unlisted**, and submit for review. After that, installing on a new
computer is opening the store link and clicking **Add to Chrome**.

### Load unpacked (development)

1. Open `chrome://extensions` and turn on **Developer mode**.
2. Click **Load unpacked** and select this `extension/` folder.
3. Pin the extension. Click it for the popup, or right-click any page to clip.

### What Chrome allows on macOS outside the store

Checked against Chrome's documentation on 2026-11-19.

| Route | Allowed on macOS? |
|---|---|
| Chrome Web Store link (public, unlisted or private) | Yes. |
| **Load unpacked** with Developer mode | Yes, by hand on each computer. |
| Local `.crx` file | No. "As of Chrome 44, no external installs are allowed from a path to a local CRX file on Mac OS." |
| `External Extensions/<id>.json` preferences file | Only for store items: on macOS the `external_update_url` "must point to the Chrome Web Store". Chrome asks you to confirm before enabling it. |
| `ExtensionInstallForcelist` policy with a self-hosted update URL | Only on a managed Mac: "managed via MDM, joined to a domain via MCX or enrolled in Chrome Enterprise Core". |

Sources: [Alternative installation methods](https://developer.chrome.com/docs/extensions/how-to/distribute/install-extensions),
[ExtensionInstallForcelist policy definition](https://github.com/chromium/chromium/blob/main/components/policy/resources/templates/policy_definitions/Extensions/ExtensionInstallForcelist.yaml),
[Chrome Web Store distribution](https://developer.chrome.com/docs/webstore/cws-dashboard-distribution).

### Sync between computers needs the same extension id

`chrome.storage.sync` data is kept per extension id. Chrome's documentation does not spell
that out; it is how developers on the chromium-extensions list describe it (links below). A
store install has the same id everywhere, so the saved server URL and the public address
follow you to every Chrome you are signed in to with sync on.

An unpacked copy gets its id from the folder it was loaded from: Chromium hashes the path
([`GenerateIdForPath`](https://github.com/chromium/chromium/blob/main/components/crx_file/id_util.cc)).
Loaded from different paths on two computers, it is two different extensions and nothing
syncs between them. The same path on both, or a manifest
[`key`](https://developer.chrome.com/docs/extensions/reference/manifest/key), gives one id.
Chrome's [storage documentation](https://developer.chrome.com/docs/extensions/reference/api/storage)
does not say whether unpacked extensions sync. Developers on the chromium-extensions list
report that they do once the ids match
([1](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/MmPE0Zl1KoQ),
[2](https://groups.google.com/a/chromium.org/g/chromium-extensions/c/qp_087h_vrU)); this
repository has not tested it. Without sync, type the address into **Settings** once per
computer.

## Configuration

Open **Settings** (from the popup) to set the server URL, for example a tailnet address. Use
**Test connection** to confirm the daemon is reachable. Settings also shows the public address
currently synced.

## Backend

The extension talks to the Mastisk daemon over these endpoints:

- `GET /api/health` — which server answers, and its `public_url` (null when not configured).
- `POST /api/ingest/web` — submit a clip; returns a `source_id` and queued status.
- `GET /api/ingest/web/{source_id}` — poll indexing status.
- `POST /api/listen` — queue detected YouTube/video/podcast media for transcription.
- `POST /api/ask` — RAG question answering across the wiki (+ the current page).

All requests are made from the background service worker, which holds the host permissions.

## Tests

```bash
node --test "extension/tests/*.test.js"
```

`server.test.js` covers the URL rules in `lib/server.js`. `background.test.js` runs the service
worker against a fake `chrome` and a fake network. `pytest` runs both through
`tests/test_extension_server_resolution.py`. Nothing here drives a real browser.

## Regenerating icons

```bash
uv run --with pillow python scripts/generate-icons.py
```

## Files

- `manifest.json` — MV3 manifest.
- `background.js` — context menus, extraction, ingest + polling, notifications, badge, message routing.
- `media-content.js` — lightweight primary-video/podcast detection.
- `chat-content.js` — Defuddle Markdown extraction for textual page/chat context.
- `sidepanel.{html,css,js}` — the chat UI.
- `popup.{html,js}` — quick actions, connection status, which server answered.
- `options.{html,js}` — server URL configuration.
- `lib/server.js` — which server to use and how links are built (shared by the worker and the pages).
- `scripts/package.sh` — builds the Chrome Web Store zip.
- `tests/` — Node tests for the server choice and links.
- `lib/defuddle.js` — vendored Defuddle 0.19.2 full browser bundle
  (`defuddle/dist/index.full.js`).
- `lib/markdown.js` — markdown → HTML renderer for chat answers.
