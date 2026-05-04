"""Tests for integrations.article — shared HTML article extractor."""
from __future__ import annotations

from unittest.mock import patch

import httpx
import pytest


SAMPLE_HTML = """<!DOCTYPE html>
<html><head>
  <title>The State of Agentic Coding in 2026</title>
  <meta name="author" content="Jane Smith"/>
  <meta property="article:published_time" content="2026-04-22T12:00:00Z"/>
  <meta property="og:image" content="https://example.com/og.jpg"/>
</head>
<body>
  <header><img src="/site-logo.png" alt="logo"/></header>
  <article>
    <h1>The State of Agentic Coding in 2026</h1>
    <p>Most autonomous coding agents in 2026 still hit a verifier wall. The
       interesting work has shifted from scaffolding agents to scaffolding
       reviewers. <em>Karpathy</em> framed this clearly in his Stanford talk:
       the leverage has moved off the model and into the harness around it.</p>
    <p>Three patterns are emerging across teams I've talked to: progressive
       autonomy gates, mixture-of-reviewers escalation, and durable
       artifact-driven development. Each one trades latency for review
       quality, but the key insight is that human-in-the-loop checkpoints
       are not a fallback — they're the product surface area where trust
       gets compounded over weeks.</p>
    <p>If you're building an agent today, the question to ask is not "how
       do I make it more autonomous" but "what's the smallest verifier that
       lets me ship its work without anxiety." That reframing changes
       basically every architectural decision downstream.</p>
    <img src="/diagram.png" alt="diagram of agent loop"/>
  </article>
</body></html>"""

PAYWALL_HTML = """<!DOCTYPE html>
<html><head><title>Subscribers only</title></head>
<body><div id="paywall">Sign in to read.</div></body></html>"""


def _patch_get(html: str, status: int = 200, final_url: str = "https://example.com/post"):
    """Patch httpx.AsyncClient.get to return a canned response.

    Always attaches a request so accessing ``resp.url`` (used by the extractor
    for the canonical URL after redirects) doesn't blow up.
    """
    resp = httpx.Response(
        status_code=status,
        text=html,
        headers={"content-type": "text/html"},
        request=httpx.Request("GET", final_url),
    )

    class _Stub:
        def __init__(self, *_, **__): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_): return False
        async def get(self, *_, **__): return resp

    return patch("mastisk.integrations.article.httpx.AsyncClient", _Stub)


@pytest.mark.asyncio
async def test_fetch_and_extract_pulls_title_text_meta_and_images():
    from mastisk.integrations import article

    with _patch_get(SAMPLE_HTML, final_url="https://example.com/post"):
        data = await article.fetch_and_extract("https://example.com/post")

    assert data.title == "The State of Agentic Coding in 2026"
    assert "verifier wall" in data.text
    assert "smallest verifier" in data.text
    assert data.author == "Jane Smith"
    assert data.published_at == "2026-04-22T12:00:00Z"
    # og:image wins as hero.
    assert data.hero_image_url == "https://example.com/og.jpg"
    # Inline media should include the diagram (relative URL resolved against
    # the canonical page URL).
    srcs = [m["src"] for m in data.inline_media]
    assert "https://example.com/diagram.png" in srcs


@pytest.mark.asyncio
async def test_fetch_and_extract_raises_on_paywall_with_no_text():
    from mastisk.integrations import article

    with _patch_get(PAYWALL_HTML):
        with pytest.raises(RuntimeError, match="no extractable text"):
            await article.fetch_and_extract("https://example.com/locked")


@pytest.mark.asyncio
async def test_fetch_and_extract_raises_on_http_error():
    from mastisk.integrations import article

    with _patch_get("Not found", status=404):
        with pytest.raises(RuntimeError, match="HTTP 404"):
            await article.fetch_and_extract("https://example.com/missing")


def test_extract_inline_images_resolves_relative_dedupes_and_caps():
    from mastisk.integrations import article

    html = """
      <img src="/a.png" alt="a"/>
      <img src="https://other.com/b.png"/>
      <img src="/a.png"/>
      <img src="data:image/png;base64,xxx"/>
      <img src="/c.png" alt="c"/>
      <img src="/d.png"/>
    """
    out = article.extract_inline_images(html, "https://example.com/page", limit=3)
    srcs = [m["src"] for m in out]
    assert srcs == [
        "https://example.com/a.png",
        "https://other.com/b.png",
        "https://example.com/c.png",
    ]
    # First match's alt is preserved.
    assert out[0]["alt"] == "a"


def test_first_img_in_html_skips_data_uris():
    from mastisk.integrations import article

    assert article.first_img_in_html('<img src="data:image/png;base64,abc"/>') is None
    assert article.first_img_in_html('<img src="/x.png"/>', base_url="https://h.com") == "https://h.com/x.png"
    assert article.first_img_in_html(None) is None
    assert article.first_img_in_html("") is None
