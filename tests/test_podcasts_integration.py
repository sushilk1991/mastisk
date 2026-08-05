"""Tests for podcasts.classify_and_resolve — RSS auto-discovery in HTML pages."""

from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest

# A trimmed-down approximation of what real podcast SPA shells (Vercel/Next.js,
# Megaphone, Substack) put in their <head>. Includes attribute-order variations
# and an extra <link> that should NOT match (favicon).
HTML_WITH_FEED = """<!DOCTYPE html><html><head>
  <meta charSet="utf-8"/>
  <link rel="icon" href="/favicon.ico"/>
  <link rel="preload" href="/fonts/x.woff2" as="font"/>
  <link rel="alternate" type="application/rss+xml" title="Founders Podcast" href="https://feeds.megaphone.fm/DSLLC6297708582"/>
  <title>Episodes | Founders Podcast</title>
</head><body><div id="root"></div></body></html>"""

HTML_WITH_RELATIVE_FEED = """<!DOCTYPE html><html><head>
  <link rel="alternate" type="application/atom+xml" href="/feed.xml"/>
</head><body></body></html>"""

HTML_NO_FEED = """<!DOCTYPE html><html><head>
  <title>A normal page</title>
</head><body><p>nothing podcast-y here</p></body></html>"""


def _mock_httpx_response(text: str, status: int = 200, headers: dict | None = None):
    """Build a fake httpx.Response without going through real HTTP."""
    return httpx.Response(
        status_code=status,
        text=text,
        headers=headers or {"content-type": "text/html"},
    )


def _patch_async_client(get_response, head_response=None):
    """Patch httpx.AsyncClient so .head() and .get() both return our canned shape.

    The classify path calls .head() first, then .get() for the discovery step,
    so both need realistic stubs. Returns the patcher (use as context manager).
    """
    head_resp = head_response or httpx.Response(
        status_code=200, headers={"content-type": "text/html"},
    )

    class _StubClient:
        def __init__(self, *_, **__): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def head(self, *_, **__): return head_resp
        async def get(self, *_, **__): return get_response

    return patch("mastisk.integrations.podcasts.httpx.AsyncClient", _StubClient)


@pytest.mark.asyncio
async def test_classify_and_resolve_discovers_feed_in_html_page():
    """The whole point of Phase 1: a podcast show page (HTML, no RSS content
    type) advertises its feed via <link rel="alternate"> — we should follow it
    and return ('rss', feed_url) instead of ('unknown', url)."""
    from mastisk.integrations import podcasts

    page_url = "https://www.founderspodcast.com/episodes"
    with _patch_async_client(_mock_httpx_response(HTML_WITH_FEED)):
        kind, resolved = await podcasts.classify_and_resolve(page_url)
    assert kind == "rss"
    assert resolved == "https://feeds.megaphone.fm/DSLLC6297708582"


@pytest.mark.asyncio
async def test_classify_and_resolve_handles_relative_feed_href():
    """Atom feeds and feeds with relative hrefs (rare but valid) should resolve
    against the page URL so downstream httpx calls don't choke."""
    from mastisk.integrations import podcasts

    page_url = "https://example.com/blog/podcast/"
    with _patch_async_client(_mock_httpx_response(HTML_WITH_RELATIVE_FEED)):
        kind, resolved = await podcasts.classify_and_resolve(page_url)
    assert kind == "rss"
    assert resolved == "https://example.com/feed.xml"


@pytest.mark.asyncio
async def test_classify_and_resolve_falls_back_to_article_for_html_with_no_feed():
    """Phase 2 behavior: HTML page without a feed link is now an 'article'
    candidate (handled by trafilatura in the Listener), not 'unknown'.

    Pre-Phase-2 this used to return 'unknown' and the Listener would
    surface "can't ingest — unknown type". Now the same URL routes to the
    universal article extractor, which is exactly the broader-ingestion UX
    the user asked for."""
    from mastisk.integrations import podcasts

    page_url = "https://example.com/just-a-page"
    with _patch_async_client(_mock_httpx_response(HTML_NO_FEED)):
        kind, resolved = await podcasts.classify_and_resolve(page_url)
    assert kind == "article"
    assert resolved == page_url


@pytest.mark.asyncio
async def test_classify_and_resolve_passes_through_known_kinds():
    """When classify already recognises the URL (youtube/spotify/audio),
    classify_and_resolve must NOT alter the URL — it just labels."""
    from mastisk.integrations import podcasts

    # YouTube is detected purely from hostname; no network needed.
    yt_url = "https://www.youtube.com/watch?v=abc"
    with _patch_async_client(_mock_httpx_response("")):  # never called
        kind, resolved = await podcasts.classify_and_resolve(yt_url)
    assert kind == "youtube"
    assert resolved == yt_url

    # Direct audio detected from extension.
    audio_url = "https://example.com/episode42.mp3"
    with _patch_async_client(_mock_httpx_response("")):
        kind, resolved = await podcasts.classify_and_resolve(audio_url)
    assert kind == "direct_audio"
    assert resolved == audio_url


@pytest.mark.asyncio
async def test_rss_sniff_stops_after_first_four_kilobytes():
    from mastisk.integrations import podcasts

    observed: dict[str, object] = {}

    class _StreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def aiter_bytes(self, *, chunk_size: int):
            observed["chunk_size"] = chunk_size
            yield b'<?xml version="1.0"?><rss>' + (b"x" * 5000)
            observed["read_second_chunk"] = True
            yield b"should-not-be-read"

    class _StreamingClient:
        def __init__(self, *_, **__):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        def stream(self, method, url, *, headers):
            observed.update(method=method, url=url, headers=headers)
            return _StreamResponse()

    with patch("mastisk.integrations.podcasts.httpx.AsyncClient", _StreamingClient):
        kind = await podcasts._sniff_xml_for_rss("https://cdn.example.com/large.mp4")

    assert kind == "rss"
    assert observed["chunk_size"] == 4096
    assert observed["headers"] == {"Range": "bytes=0-4095"}
    assert "read_second_chunk" not in observed
