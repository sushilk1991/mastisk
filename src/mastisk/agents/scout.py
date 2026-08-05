"""Scout — polls RSS feeds, clips relevant items, enqueues Compiler jobs.

Interest-gating: a new item's title + summary is compared against `vault/_self/interests.md`
via Ollama embeddings. Items with cosine sim < 0.25 to any interest line AND matching a
dislikes keyword are skipped (and emit a "Scout filtered: …" feed entry).
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime

import feedparser
import httpx
import trafilatura

from mastisk.agents.base import Agent, enqueue
from mastisk.db.queries import connect
from mastisk.integrations.article import (
    extract_inline_images as _extract_inline_images,
)
from mastisk.integrations.article import (
    first_img_in_html as _first_img_in_html,
)
from mastisk.paths import raw_dir, self_dir
from mastisk.vault_io import read_vault_text

log = logging.getLogger("mastisk.scout")

# Relevance thresholds (tuned conservatively; easy to edit in one place)
SIM_THRESHOLD = 0.25

USER_AGENT = "Mastisk/0.1 (personal knowledge wiki; RSS reader)"
HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=25.0, write=15.0, pool=5.0)


class Scout(Agent):
    name = "scout"
    tick_seconds = 600  # 10 min

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job["payload_json"] or "{}")
        feed_url = payload.get("url")
        if not feed_url:
            # Default path: pick one enabled feed that hasn't been fetched in an hour
            with connect() as conn:
                row = conn.execute(
                    """SELECT url FROM rss_feeds WHERE enabled=1
                       AND (last_fetched IS NULL OR last_fetched < datetime('now', '-1 hour'))
                       ORDER BY last_fetched ASC LIMIT 1"""
                ).fetchone()
                if not row:
                    return
                feed_url = row["url"]

        await self._fetch_feed(feed_url)

    async def run_once(self) -> None:
        """Scout is different from the default pick-one-job loop: it tick-polls the DB for feeds."""
        if self.disabled_tick():
            return
        # If there are queued scout jobs, process one; otherwise auto-poll a feed
        job = self._pick_job()
        if job:
            return await super().run_once()
        # Auto-poll
        with connect() as conn:
            row = conn.execute(
                """SELECT url FROM rss_feeds WHERE enabled=1
                   AND (last_fetched IS NULL OR last_fetched < datetime('now', '-1 hour'))
                   ORDER BY last_fetched ASC LIMIT 1"""
            ).fetchone()
        if row:
            try:
                await self._fetch_feed(row["url"])
            except Exception:
                log.exception("scout auto-poll failed")

    async def _fetch_feed(self, feed_url: str) -> None:
        """Fetch a feed with HTTP conditional-GET — we only re-parse if changed."""
        log.info("scout fetching %s", feed_url)

        # Load cached conditional headers
        with connect() as conn:
            row = conn.execute(
                "SELECT last_etag, last_modified FROM rss_feeds WHERE url=?", (feed_url,)
            ).fetchone()
        headers = {"User-Agent": USER_AGENT}
        if row:
            if row["last_etag"]:
                headers["If-None-Match"] = row["last_etag"]
            if row["last_modified"]:
                headers["If-Modified-Since"] = row["last_modified"]

        try:
            async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
                resp = await client.get(feed_url, headers=headers)
        except Exception as e:
            log.warning("scout: network error on %s: %s", feed_url, e)
            with connect() as conn:
                conn.execute(
                    "UPDATE rss_feeds SET last_fetched=CURRENT_TIMESTAMP WHERE url=?",
                    (feed_url,),
                )
            self.emit_feed(verb="failed", obj=feed_url[:80], kind="rss", payload={"error": str(e)[:200]})
            return

        # Always record that we tried
        new_etag = resp.headers.get("etag")
        new_last_mod = resp.headers.get("last-modified")
        with connect() as conn:
            conn.execute(
                """UPDATE rss_feeds
                     SET last_fetched=CURRENT_TIMESTAMP,
                         last_etag=COALESCE(?, last_etag),
                         last_modified=COALESCE(?, last_modified)
                   WHERE url=?""",
                (new_etag, new_last_mod, feed_url),
            )

        if resp.status_code == 304:
            log.info("scout: %s unchanged (304)", feed_url)
            self.emit_feed(verb="checked", obj=feed_url[:80], kind="rss", payload={"status": "not_modified"})
            return
        if resp.status_code >= 400:
            log.warning("scout: %s returned %s", feed_url, resp.status_code)
            self.emit_feed(verb="failed", obj=feed_url[:80], kind="rss", payload={"status": resp.status_code})
            return

        parsed = feedparser.parse(resp.content)
        if parsed.bozo and not parsed.entries:
            log.warning("scout: malformed feed %s: %s", feed_url, parsed.bozo_exception)
            return

        interests = self._load_interests()
        dislikes = self._load_dislikes()
        new_count = 0

        with connect() as conn:
            for entry in parsed.entries[:20]:
                link = entry.get("link", "")
                if not link:
                    continue
                title = entry.get("title", "")
                summary = entry.get("summary", "") or entry.get("description", "")

                # De-dupe by URL
                exists = conn.execute("SELECT id FROM sources WHERE url=?", (link,)).fetchone()
                if exists:
                    continue

                if self._is_disliked(title, summary, dislikes):
                    self.emit_feed(verb="filtered", obj=title[:80], kind="rss", payload={"why": "dislikes match"})
                    continue

                relevant = await self._score_relevance(f"{title}\n{summary}", interests)
                if not relevant:
                    self.emit_feed(verb="filtered", obj=title[:80], kind="rss", payload={"why": "below threshold"})
                    continue

                # Fetch + extract clean text (async so slow sites don't block the tick)
                body = summary
                page_html: str | None = None
                try:
                    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as c:
                        page = await c.get(link, headers={"User-Agent": USER_AGENT})
                        if page.status_code < 400:
                            page_html = page.text
                            extracted = trafilatura.extract(
                                page.text,
                                include_comments=False,
                                include_tables=False,
                            )
                            if extracted:
                                body = extracted
                except Exception as e:
                    log.info("scout extract failed for %s: %s", link, e)

                src_id = hashlib.sha256(link.encode()).hexdigest()[:16]
                raw_path = raw_dir() / f"{src_id}.txt"
                raw_path.parent.mkdir(parents=True, exist_ok=True)
                raw_path.write_text(f"# {title}\n\n{link}\n\n{body}")

                published = entry.get("published_parsed")
                published_iso = datetime(*published[:6]).isoformat(sep=" ") if published else None

                # Hero: entry-level thumbnail beats anything we scrape from the
                # page HTML. Falls back to the first <img> in the summary or body.
                hero = _pick_entry_thumbnail(entry) \
                    or _first_img_in_html(summary) \
                    or (_first_img_in_html(page_html) if page_html else None)
                # Inline media: up to a handful of additional images from the
                # extracted page body. Always relative-resolved against the page
                # URL so downstream renderers don't have to re-parse.
                inline_media = _extract_inline_images(page_html, link, limit=6) if page_html else []
                # De-dupe hero out of the inline list so it isn't shown twice.
                if hero:
                    inline_media = [m for m in inline_media if m.get("src") != hero]

                conn.execute(
                    """INSERT INTO sources
                       (id, kind, url, title, published_at, raw_path, author,
                        hero_image_url, media_json)
                       VALUES (?, 'blog', ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        src_id,
                        link,
                        title,
                        published_iso,
                        str(raw_path),
                        entry.get("author"),
                        hero,
                        json.dumps(inline_media) if inline_media else None,
                    ),
                )
                enqueue("compiler", kind="compile", payload={"source_id": src_id})
                self.emit_feed(verb="clipped", obj=title[:80], kind="blog", payload={"source_id": src_id})
                new_count += 1

        log.info("scout: %s new sources from %s", new_count, feed_url)

    # ───── interest/dislike handling ─────

    def _load_interests(self) -> list[str]:
        p = self_dir() / "interests.md"
        if not p.exists():
            return []
        return [re.sub(r"^[-*•\d.\s]+", "", line).strip() for line in read_vault_text(p).splitlines()
                if line.strip() and not line.lstrip().startswith("#")]

    def _load_dislikes(self) -> list[str]:
        p = self_dir() / "dislikes.md"
        words: list[str] = []
        if p.exists():
            for line in read_vault_text(p).splitlines():
                if line.strip() and not line.lstrip().startswith("#"):
                    cleaned = re.sub(r"^[-*•\d.\s]+", "", line).strip().lower()
                    if cleaned:
                        words.append(cleaned)
        words.extend(self._load_avoid_rules())
        return words

    def _load_avoid_rules(self) -> list[str]:
        """Distilled `avoid: X` preference rules from learnings.md.

        The Gardener's feedback-distillation pass phrases pure content
        filters exactly as `avoid: <keyword>` so the Scout can apply them
        mechanically alongside the user-authored dislikes. Dated bullets
        ("- (2026-07-14) avoid: crypto") parse the same way."""
        p = self_dir() / "learnings.md"
        if not p.exists():
            return []
        words: list[str] = []
        for line in read_vault_text(p).splitlines():
            cleaned = re.sub(r"^[-*•\d.\s]+", "", line).strip()
            cleaned = re.sub(r"^\(\d{4}-\d{2}-\d{2}\)\s*", "", cleaned)
            if cleaned.lower().startswith("avoid:"):
                term = cleaned[len("avoid:"):].strip().lower()
                if term:
                    words.append(term)
        return words

    def _is_disliked(self, title: str, summary: str, dislikes: list[str]) -> bool:
        hay = f"{title} {summary}".lower()
        return any(kw and kw in hay for kw in dislikes)

    async def _score_relevance(self, text: str, interests: list[str]) -> bool:
        """True if text is relevant to any interest. If no interests file, accept everything."""
        if not interests:
            return True
        try:
            from mastisk.bridges import ollama_bridge
            vectors = await ollama_bridge.embed([text] + interests)
            if not vectors or len(vectors) < 2:
                return True  # fail-open
            item_vec = vectors[0]
            max_sim = max(ollama_bridge.cosine(item_vec, v) for v in vectors[1:])
            return max_sim >= SIM_THRESHOLD
        except Exception as e:
            log.info("scout embed failed, fail-open: %s", e)
            return True


# ───── image-extraction helpers ─────


def _pick_entry_thumbnail(entry: dict) -> str | None:
    """RSS entry-level thumbnail. Most feeds use ``media_thumbnail`` (mRSS)
    or ``media_content``; some stuff an image into the entry directly."""
    for mt in entry.get("media_thumbnail") or []:
        href = mt.get("url") if isinstance(mt, dict) else None
        if href:
            return href
    for mc in entry.get("media_content") or []:
        if not isinstance(mc, dict):
            continue
        if (mc.get("medium") or "").lower() == "image" and mc.get("url"):
            return mc["url"]
        # Some feeds omit medium; trust the extension.
        url = mc.get("url") or ""
        if url.lower().split("?", 1)[0].endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            return url
    image = entry.get("image")
    if isinstance(image, dict):
        href = image.get("href") or image.get("url")
        if href:
            return href
    return None


# Image extraction helpers (_first_img_in_html, _extract_inline_images) live
# in mastisk.integrations.article — Listener and Scout share them so a generic
# URL paste and an RSS-clipped item get identical hero/inline-media handling.
