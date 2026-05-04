"""Generic article extraction — fetch any HTML URL, pull main text + images.

Powers two ingestion paths:
  * Scout's RSS-driven clipping (one item per feed entry)
  * Listener's universal /api/listen URL (any web page the user pastes)

Wraps trafilatura for the main-text extraction and reuses Scout's regex-based
image picking. Intentionally narrow: we don't try to render JS, follow paywalls,
or solve hostile anti-scraping. The Compiler's prompt is robust to short or
truncated bodies, so a partial extraction still produces a usable article stub.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
import trafilatura

log = logging.getLogger("mastisk.article")


USER_AGENT = "Mastisk/0.1 (personal knowledge wiki; URL ingestion)"
HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=25.0, write=15.0, pool=5.0)

# Floor below which extraction is treated as a failure. ~200 chars is short
# enough to admit tweet-length status pages and short news ledes, but long
# enough to reject paywall stubs ("Sign in to read") and pure error pages.
_MIN_USEFUL_TEXT_CHARS = 200


@dataclass
class ArticleData:
    url: str                             # canonical URL after redirects
    title: str                           # best-effort title (HTML <title> or extracted)
    text: str                            # main body text
    author: str | None = None            # if discoverable from meta tags
    published_at: str | None = None      # ISO-ish string if discoverable
    hero_image_url: str | None = None    # og:image / first body img
    inline_media: list[dict] = field(default_factory=list)  # [{src, alt}]
    raw_html: str = ""                   # so callers can re-parse if needed


_IMG_RE = re.compile(
    r'<img\b[^>]*?\bsrc\s*=\s*(?:"([^"]+)"|\'([^\']+)\'|([^\s>]+))[^>]*>',
    re.IGNORECASE,
)
_IMG_ALT_RE = re.compile(r'\balt\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.IGNORECASE)
_TITLE_RE = re.compile(r"<title[^>]*>([^<]+)</title>", re.IGNORECASE)
_OG_IMAGE_RE = re.compile(
    r'<meta\b[^>]*?\bproperty\s*=\s*["\']og:image["\'][^>]*?\bcontent\s*=\s*["\']([^"\']+)["\']'
    r"|"
    r'<meta\b[^>]*?\bcontent\s*=\s*["\']([^"\']+)["\'][^>]*?\bproperty\s*=\s*["\']og:image["\']',
    re.IGNORECASE,
)
_AUTHOR_RE = re.compile(
    r'<meta\b[^>]*?\bname\s*=\s*["\']author["\'][^>]*?\bcontent\s*=\s*["\']([^"\']+)["\']'
    r"|"
    r'<meta\b[^>]*?\bcontent\s*=\s*["\']([^"\']+)["\'][^>]*?\bname\s*=\s*["\']author["\']',
    re.IGNORECASE,
)
_PUB_RE = re.compile(
    r'<meta\b[^>]*?\bproperty\s*=\s*["\']article:published_time["\'][^>]*?\bcontent\s*=\s*["\']([^"\']+)["\']'
    r"|"
    r'<meta\b[^>]*?\bcontent\s*=\s*["\']([^"\']+)["\'][^>]*?\bproperty\s*=\s*["\']article:published_time["\']',
    re.IGNORECASE,
)


async def fetch_and_extract(url: str, *, max_inline_images: int = 6) -> ArticleData:
    """Fetch ``url`` and return a populated ArticleData.

    Raises ``RuntimeError`` for clean failure modes (HTTP error, no extractable
    text). Network exceptions are wrapped in RuntimeError too — callers don't
    need to catch httpx-specific types.
    """
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as c:
            resp = await c.get(url, headers={"User-Agent": USER_AGENT})
    except Exception as e:
        raise RuntimeError(f"fetch failed: {e}") from e
    if resp.status_code >= 400:
        raise RuntimeError(f"fetch returned HTTP {resp.status_code}")

    canonical = str(resp.url) or url
    html = resp.text or ""

    text = (trafilatura.extract(html, include_comments=False, include_tables=False) or "").strip()
    if len(text) < _MIN_USEFUL_TEXT_CHARS:
        # trafilatura returned nothing or a stub (paywall message, "Sign in to
        # read", etc). Fall back to a crude "everything between <body> minus
        # tags" extraction. If THAT also undershoots the floor, give up and
        # raise — there's nothing for the Compiler to chew on.
        body_text = _crude_body_text(html)
        if len(body_text) < _MIN_USEFUL_TEXT_CHARS:
            raise RuntimeError(
                "no extractable text — the page may require JavaScript or be paywalled"
            )
        text = body_text

    title = _extract_title(html) or canonical
    return ArticleData(
        url=canonical,
        title=title.strip()[:280],
        text=text,
        author=_extract_meta(_AUTHOR_RE, html),
        published_at=_extract_meta(_PUB_RE, html),
        hero_image_url=_extract_meta(_OG_IMAGE_RE, html) or first_img_in_html(html, base_url=canonical),
        inline_media=extract_inline_images(html, canonical, limit=max_inline_images),
        raw_html=html,
    )


# ─────────────────── Helpers reused by Scout (lifted from scout.py) ───────────────────


def first_img_in_html(html: str | None, *, base_url: str | None = None) -> str | None:
    """Return the first ``<img src>`` in an HTML fragment, optionally
    resolved against ``base_url`` so relative paths become absolute."""
    if not html:
        return None
    m = _IMG_RE.search(html)
    if not m:
        return None
    src = m.group(1) or m.group(2) or m.group(3) or None
    if not src or src.startswith("data:"):
        return None
    if base_url:
        return urljoin(base_url, src)
    return src


def extract_inline_images(html: str | None, base_url: str, *, limit: int) -> list[dict]:
    """Collect up to ``limit`` distinct ``<img>`` URLs.

    Skips data URIs and dedupes by absolute URL. Returns dicts matching the
    frontend Article.media shape: ``{src, alt}``. Order follows document order.
    """
    if not html:
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for m in _IMG_RE.finditer(html):
        src = m.group(1) or m.group(2) or m.group(3)
        if not src or src.startswith("data:"):
            continue
        absolute = urljoin(base_url, src)
        if absolute in seen:
            continue
        seen.add(absolute)
        alt_match = _IMG_ALT_RE.search(m.group(0))
        alt = (alt_match.group(1) or alt_match.group(2)) if alt_match else ""
        out.append({"src": absolute, "alt": alt})
        if len(out) >= limit:
            break
    return out


def _extract_title(html: str) -> str | None:
    if not html:
        return None
    # Prefer <title>; fall back to the first <h1>. Trimming HTML entities is
    # the caller's job — these snippets land in DB columns where the renderer
    # decodes them.
    m = _TITLE_RE.search(html)
    if m:
        return m.group(1)
    h1 = re.search(r"<h1[^>]*>([^<]+)</h1>", html, re.IGNORECASE)
    if h1:
        return h1.group(1)
    return None


def _extract_meta(pattern: re.Pattern[str], html: str) -> str | None:
    if not html:
        return None
    m = pattern.search(html)
    if not m:
        return None
    return next((g for g in m.groups() if g), None)


def _crude_body_text(html: str) -> str:
    """Last-resort text extraction when trafilatura returns nothing.

    Strips script/style blocks first (otherwise they leak code into the
    "body"), then drops every remaining tag. Keeps line breaks so the
    Compiler can still see paragraph structure.
    """
    if not html:
        return ""
    cleaned = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.IGNORECASE | re.DOTALL)
    cleaned = re.sub(r"<br\s*/?>", "\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"</p>", "\n\n", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()
