# Mastisk Clipper

A Chrome extension (Manifest V3) that clips pages, selections, and links into your local
[Mastisk](https://localhost:5555) personal knowledge wiki, and gives you a side panel to chat
with the whole wiki plus the page you're on.

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
  list links straight to the underlying wiki articles.

## Install (load unpacked)

1. Make sure the Mastisk daemon is running (default `http://localhost:5555`).
2. Open `chrome://extensions`.
3. Toggle **Developer mode** (top right).
4. Click **Load unpacked** and select this `extension/` folder.
5. Pin the extension. Click it for the popup, or right-click any page to clip.

## Configuration

Open **Settings** (from the popup) to change the server URL — for example a Tailnet address instead
of `localhost`. Use **Test connection** to confirm the daemon is reachable. When you point at a
non-local host, Chrome will ask you to grant permission for that origin.

## Backend

The extension talks to the Mastisk daemon over these endpoints:

- `GET /api/health` — connection test.
- `POST /api/ingest/web` — submit a clip; returns a `source_id` and queued status.
- `GET /api/ingest/web/{source_id}` — poll indexing status.
- `POST /api/listen` — queue detected YouTube/video/podcast media for transcription.
- `POST /api/ask` — RAG question answering across the wiki (+ the current page).

All requests are made from the background service worker, which holds the host permissions.

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
- `popup.{html,js}` — quick actions + connection status.
- `options.{html,js}` — server URL configuration.
- `lib/defuddle.js` — vendored Defuddle 0.19.2 full browser bundle
  (`defuddle/dist/index.full.js`).
- `lib/markdown.js` — markdown → HTML renderer for chat answers.
