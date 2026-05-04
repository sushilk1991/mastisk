"""Classify incoming URLs + resolve RSS episodes to direct audio."""
from __future__ import annotations

import logging
import re
from datetime import datetime
from urllib.parse import urljoin, urlparse

import feedparser
import httpx

log = logging.getLogger("mastisk.podcasts")

_AUDIO_EXTS = (".mp3", ".m4a", ".ogg", ".opus", ".wav", ".aac", ".flac")


class UnsupportedPlatformError(Exception):
    """Raised for platforms whose audio we can't download (Spotify etc)."""


async def classify(url: str) -> str:
    """Return 'youtube' | 'rss' | 'direct_audio' | 'spotify' | 'twitter' |
    'article' | 'unknown'.

    'article' = generic HTML page where main text is extractable. 'twitter' =
    x.com / twitter.com URLs (handled via the article path with limitations
    documented in Listener._ingest_article — JS-only timelines won't yield
    much, single-tweet pages are best-effort)."""
    if not url:
        return "unknown"
    u = url.strip()
    parsed = urlparse(u)
    host = (parsed.hostname or "").lower()
    path_lower = (parsed.path or "").lower()

    if host.endswith("youtube.com") or host == "youtu.be" or host.endswith("youtube-nocookie.com"):
        return "youtube"
    if host.endswith("spotify.com"):
        return "spotify"
    if host.endswith("twitter.com") or host == "x.com" or host.endswith(".x.com"):
        return "twitter"
    if any(path_lower.endswith(ext) for ext in _AUDIO_EXTS):
        return "direct_audio"

    # Network sniff for RSS. One HEAD request — cheap.
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as c:
            resp = await c.head(u)
        ctype = (resp.headers.get("content-type") or "").lower()
    except Exception as e:
        log.info("podcasts.classify: HEAD failed for %s: %s", u, e)
        return "unknown"
    if "application/rss+xml" in ctype or "application/atom+xml" in ctype:
        return "rss"
    if "text/xml" in ctype or "application/xml" in ctype:
        return await _sniff_xml_for_rss(u)
    # HTML and JSON pages aren't feeds — but HTML with a body is an "article"
    # candidate. Caller's classify_and_resolve will try RSS auto-discovery
    # FIRST; the 'article' fallback only fires when no feed link is advertised.
    if "text/html" in ctype:
        return "article"
    # Some feeds return text/html from HEAD; do a small GET sniff as a last chance.
    if "application/json" not in ctype:
        return await _sniff_xml_for_rss(u)
    return "unknown"


async def _sniff_xml_for_rss(url: str) -> str:
    """GET the first ~4KB and look for <rss or <feed roots."""
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=8.0) as c:
            resp = await c.get(url, headers={"Range": "bytes=0-4095"})
    except Exception:
        return "unknown"
    head = (resp.text or "")[:4096].lower()
    if "<rss" in head or "<feed" in head:
        return "rss"
    return "unknown"


# Matches <link rel="alternate" type="application/rss+xml" href="..."> and
# attribute-order variants. Captures the href. Intentionally permissive: real
# podcast site HTML uses single quotes, attribute reordering, and self-closing
# slashes interchangeably. Regex is fine here — we only need the href, not a
# full DOM, and we only run it on documents that already failed every cheaper
# classifier check.
_RSS_LINK_RE = re.compile(
    r"""<link\b[^>]*?\brel=["']alternate["'][^>]*?\btype=["']application/(?:rss|atom)\+xml["'][^>]*?\bhref=["']([^"']+)["']"""
    r"""|<link\b[^>]*?\btype=["']application/(?:rss|atom)\+xml["'][^>]*?\brel=["']alternate["'][^>]*?\bhref=["']([^"']+)["']"""
    r"""|<link\b[^>]*?\bhref=["']([^"']+)["'][^>]*?\btype=["']application/(?:rss|atom)\+xml["']""",
    re.IGNORECASE | re.DOTALL,
)


async def _discover_rss_link(url: str) -> str | None:
    """Look for an advertised RSS feed inside an HTML page's <head>.

    Most modern podcast sites (Megaphone, Substack, Apple-mirrored sites,
    Founders Podcast on Vercel/Next.js, etc.) embed
    ``<link rel="alternate" type="application/rss+xml" href="...">`` so feed
    readers can auto-discover. We do the same: GET the first ~32KB of the page,
    scan for that link, return the href (resolved against the page URL so
    relative paths work). Returns None if no link found or the fetch fails.

    Bounded to 32KB because the relevant <link> always lives in <head>; pulling
    the whole bundle on a SPA podcast site would be wasteful.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=10.0) as c:
            # Range request first; some servers reject ranges with 416, in
            # which case fall back to a regular GET.
            resp = await c.get(url, headers={
                "Range": "bytes=0-32767",
                "User-Agent": "Mastisk/0.1 (+rss-discovery)",
            })
            if resp.status_code == 416:
                resp = await c.get(url, headers={"User-Agent": "Mastisk/0.1 (+rss-discovery)"})
    except Exception as e:
        log.info("podcasts.discover_rss: fetch failed for %s: %s", url, e)
        return None
    if resp.status_code >= 400:
        return None
    body = resp.text or ""
    m = _RSS_LINK_RE.search(body[:32768])
    if not m:
        return None
    href = next((g for g in m.groups() if g), None)
    if not href:
        return None
    return urljoin(url, href.strip())


async def classify_and_resolve(url: str) -> tuple[str, str]:
    """Classify + auto-discover RSS feeds embedded in HTML pages.

    Returns ``(kind, resolved_url)``. ``resolved_url`` differs from the input
    when the input is a podcast show page (HTML) that advertises its feed via
    ``<link rel="alternate">`` — in that case we surface ``("rss", feed_url)``
    so callers can fetch the feed without orchestrating discovery themselves.

    Resolution priority for HTML pages:
      1. If the page advertises an RSS/Atom feed → surface as 'rss' with the
         feed URL. (A podcast show page with a feed gets routed to whisper,
         not the article extractor — audio is more useful than the page text.)
      2. Otherwise → surface as the original 'article' kind so the caller
         routes to the trafilatura article extractor.

    For non-HTML kinds (youtube, rss, direct_audio, spotify, twitter), this is
    a thin pass-through with the original url.
    """
    kind = await classify(url)
    if kind == "article":
        # Try feed discovery first — if a podcast page also functions as a blog
        # we prefer the feed (audio-bearing) over the article text.
        discovered = await _discover_rss_link(url)
        if discovered:
            log.info("podcasts.classify_and_resolve: discovered feed %s in %s", discovered, url)
            return "rss", discovered
        return "article", url
    if kind != "unknown":
        return kind, url
    # Last-ditch: even with no useful content-type, the page might be an RSS
    # feed served as text/html. Falls back to article on miss.
    discovered = await _discover_rss_link(url)
    if discovered:
        log.info("podcasts.classify_and_resolve: discovered feed %s in %s", discovered, url)
        return "rss", discovered
    return "unknown", url


async def resolve_rss_episode(feed_url: str, max_episodes: int = 1) -> list[dict]:
    """Parse a podcast RSS feed, return the latest N episodes with audio URLs.

    Each dict: {title, audio_url, published_at, author, description, duration}.
    """
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as c:
            resp = await c.get(feed_url, headers={"User-Agent": "Mastisk/0.1"})
    except Exception as e:
        raise RuntimeError(f"rss fetch failed: {e}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"rss fetch returned {resp.status_code}")

    parsed = feedparser.parse(resp.content)
    if parsed.bozo and not parsed.entries:
        raise RuntimeError(f"malformed rss feed: {parsed.bozo_exception}")

    feed_image = _pick_feed_image(parsed.feed)
    out: list[dict] = []
    for entry in parsed.entries[: max(1, max_episodes)]:
        audio_url = _pick_audio_enclosure(entry)
        if not audio_url:
            continue
        # Resolve relative hrefs (some feeds emit `/episodes/ep1.mp3`) against
        # the feed URL so downstream httpx calls don't choke on UnsupportedProtocol.
        audio_url = urljoin(feed_url, audio_url)
        pub = entry.get("published_parsed") or entry.get("updated_parsed")
        pub_iso = datetime(*pub[:6]).isoformat(sep=" ") if pub else None
        image = _pick_entry_image(entry) or feed_image
        out.append({
            "title": entry.get("title") or "",
            "audio_url": audio_url,
            "published_at": pub_iso,
            "author": entry.get("author") or parsed.feed.get("author") or "",
            "description": entry.get("summary") or entry.get("description") or "",
            "duration": entry.get("itunes_duration") or "",
            "image": image,
        })
    return out


def _pick_feed_image(feed: dict) -> str | None:
    """Channel-level cover art. Most podcasts put it in ``image.href`` or
    ``itunes_image.href``."""
    if not feed:
        return None
    img = feed.get("image")
    if isinstance(img, dict):
        href = img.get("href") or img.get("url")
        if href:
            return href
    elif isinstance(img, str) and img:
        return img
    it = feed.get("itunes_image")
    if isinstance(it, dict):
        href = it.get("href")
        if href:
            return href
    return None


def _pick_entry_image(entry: dict) -> str | None:
    """Per-episode cover art, if the feed overrides the channel default."""
    it = entry.get("itunes_image")
    if isinstance(it, dict):
        href = it.get("href")
        if href:
            return href
    # Some feeds use media:thumbnail at the entry level.
    for mt in entry.get("media_thumbnail") or []:
        href = mt.get("url") if isinstance(mt, dict) else None
        if href:
            return href
    return None


def _pick_audio_enclosure(entry: dict) -> str | None:
    """Return the first audio enclosure URL in an RSS entry."""
    for enc in entry.get("enclosures") or []:
        t = (enc.get("type") or "").lower()
        href = enc.get("href") or enc.get("url")
        if href and t.startswith("audio"):
            return href
    # Fallback — some feeds skip the type attr. Strip query string before ext
    # check so `https://cdn.example/ep1.mp3?token=abc` still resolves.
    for enc in entry.get("enclosures") or []:
        href = enc.get("href") or enc.get("url")
        if not href:
            continue
        path = urlparse(href).path.lower()
        if any(path.endswith(ext) for ext in _AUDIO_EXTS):
            return href
    return None
