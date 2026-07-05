"""Twitter/X status extraction for Listener imports.

X status pages expose a mix of JSON-LD, logged-out SSR markup, oEmbed data,
and JavaScript-rendered DOM depending on the post. This module keeps the
Listener's Twitter path separate from the generic article extractor so we can
preserve tweet/card structure instead of storing one concatenated text blob.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import subprocess
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urljoin, urlparse
from urllib.request import urlopen

import httpx

from mastisk.integrations import article as article_extractor
from mastisk.integrations.article import ArticleData

log = logging.getLogger("mastisk.twitter")


USER_AGENT = "Mastisk/0.1 (personal knowledge wiki; Twitter ingestion)"
HTTP_TIMEOUT = httpx.Timeout(connect=8.0, read=25.0, write=15.0, pool=5.0)
_JSON_LD_RE = re.compile(
    r"<script\b[^>]*\btype\s*=\s*['\"]application/ld\+json['\"][^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL,
)
_STATUS_RE = re.compile(r"/status(?:es)?/(\d+)")
_URL_ONLY_RE = re.compile(r"^(?:https?://\S+\s*)+$", re.IGNORECASE)
_CARD_LABELS = {"article", "video", "image", "post", "x"}


async def fetch_and_extract(url: str) -> ArticleData:
    """Extract an X/Twitter URL into structured ArticleData.

    Order matters:
      1. Static status HTML, because logged-out SSR often has JSON-LD and cards.
      2. Browser harness, only if its Chrome DevTools endpoint is already live.
      3. Syndication/oEmbed metadata.
      4. Generic article extraction as the last compatibility fallback.
    """
    errors: list[str] = []

    try:
        data = await _fetch_static_status(url)
        if data and _useful_twitter_data(data):
            return data
    except Exception as e:
        errors.append(f"static: {e}")
        log.info("twitter static extraction failed for %s: %s", url, e)

    try:
        data = await asyncio.to_thread(_fetch_browser_status, url)
        if data and _useful_twitter_data(data):
            return data
    except Exception as e:
        errors.append(f"browser: {e}")
        log.info("twitter browser extraction failed for %s: %s", url, e)

    try:
        data = await _fetch_oembed_status(url)
        if data and _useful_twitter_data(data):
            return data
    except Exception as e:
        errors.append(f"oembed: {e}")
        log.info("twitter oEmbed extraction failed for %s: %s", url, e)

    try:
        data = await article_extractor.fetch_and_extract(url)
        if _useful_generic_data(data):
            return _normalize_generic_data(data)
    except Exception as e:
        errors.append(f"generic: {e}")
        log.info("twitter generic extraction failed for %s: %s", url, e)

    reason = "; ".join(errors) if errors else "no extractor returned content"
    raise RuntimeError(f"twitter fetch failed: {reason}")


def needs_refresh(existing_raw_text: str | None, new_data: ArticleData) -> bool:
    """Return true when an existing Twitter source should be overwritten.

    Generic extraction used to collapse Twitter pages into a noisy single line.
    Re-importing the same URL should refresh that weak raw file instead of being
    skipped by source-id dedupe.
    """
    old = _body_from_raw_file(existing_raw_text or "")
    old_score = _text_score(old)
    new_score = _text_score(new_data.text)
    if not old:
        return True
    if _structured_marker(new_data.text) and not _structured_marker(old):
        return new_score >= max(8, old_score)
    return new_score >= old_score + 80


async def _fetch_static_status(url: str) -> ArticleData | None:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(url, headers={"User-Agent": USER_AGENT})
    if resp.status_code >= 400:
        raise RuntimeError(f"fetch returned HTTP {resp.status_code}")
    canonical = str(resp.url) or url
    return _article_data_from_static_html(canonical, resp.text or "")


def _article_data_from_static_html(url: str, html_text: str) -> ArticleData | None:
    post = _find_social_post(html_text)
    if not post:
        return None

    cards = _extract_cards(html_text, url)
    for shared_url in _shared_content_urls(post):
        if not any(card.get("url") == shared_url for card in cards):
            cards.append({"url": shared_url, "title": "", "description": "", "images": []})

    canonical = str(post.get("url") or url)
    author = _author_from_post(post)
    tweet_text = _clean_multiline(
        str(post.get("articleBody") or post.get("text") or post.get("headline") or "")
    )
    published_at = str(post.get("datePublished") or post.get("dateCreated") or "") or None
    metrics = _metrics_from_post(post)
    title = _title_from_parts(author=author, tweet_text=tweet_text, cards=cards, fallback=url)
    body = _render_text(
        canonical_url=canonical,
        author=author,
        tweet_text=tweet_text,
        published_at=published_at,
        cards=cards,
        metrics=metrics,
        linked_article=None,
    )
    media = _media_from_cards(cards)
    hero = media[0]["src"] if media else _post_image(post)

    return ArticleData(
        url=canonical,
        title=title,
        text=body,
        author=author.get("display") or None,
        published_at=published_at,
        hero_image_url=hero,
        inline_media=media,
        raw_html=html_text,
    )


async def _fetch_oembed_status(url: str) -> ArticleData | None:
    endpoint = f"https://publish.x.com/oembed?url={quote(url, safe='')}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, follow_redirects=True) as client:
        resp = await client.get(endpoint, headers={"User-Agent": USER_AGENT})
    if resp.status_code >= 400:
        raise RuntimeError(f"oEmbed returned HTTP {resp.status_code}")
    payload = resp.json()
    if not isinstance(payload, dict):
        return None
    author_name = _clean_piece(str(payload.get("author_name") or ""))
    author_url = str(payload.get("author_url") or "")
    handle = ""
    if author_url:
        path = urlparse(author_url).path.strip("/")
        handle = f"@{path}" if path else ""
    tweet_text = _tweet_text_from_oembed_html(str(payload.get("html") or ""))
    author = {"display": author_name, "handle": handle, "url": author_url}
    body = _render_text(
        canonical_url=url,
        author=author,
        tweet_text=tweet_text,
        published_at=None,
        cards=[],
        metrics=[],
        linked_article=None,
    )
    return ArticleData(
        url=url,
        title=_title_from_parts(author=author, tweet_text=tweet_text, cards=[], fallback=url),
        text=body,
        author=author_name or None,
        raw_html=str(payload.get("html") or ""),
    )


def _fetch_browser_status(url: str) -> ArticleData | None:
    cdp_url = _browser_harness_cdp_url()
    if not cdp_url:
        return None

    status_id = _status_id(url)
    script = f"""
import json
import time

target_url = {json.dumps(url)}
status_id = {json.dumps(status_id)}

def extract_page():
    return js(\"\"\"(() => {{
      const normalize = (value) => (value || '')
        .replaceAll(String.fromCharCode(13), '\\n')
        .replace(/[ \\t]+/g, ' ')
        .replace(/\\n{{3,}}/g, '\\n\\n')
        .trim();
      const statusId = {json.dumps(status_id)};
      const article = (statusId && document.querySelector(`article[data-tweet-id="${{statusId}}"]`))
        || document.querySelector('article');
      const scope = article || document;
      const nameNode = scope.querySelector('a[href^="https://x.com/"] div[dir="auto"]')
        || scope.querySelector('a[href^="/"] div[dir="auto"]');
      const handleNode = Array.from(scope.querySelectorAll('a[href]'))
        .map((a) => normalize(a.innerText || ''))
        .find((text) => text.startsWith('@'));
      const tweetTexts = Array.from(scope.querySelectorAll('[data-testid="tweetText"], div[dir="auto"]'))
        .map((el) => normalize(el.innerText || ''))
        .filter(Boolean);
      const cards = Array.from(scope.querySelectorAll('a[href]'))
        .map((a) => {{
          const href = a.href || '';
          const text = normalize(a.innerText || '');
          const images = Array.from(a.querySelectorAll('img[src]'))
            .map((img) => ({{src: img.src || '', alt: img.alt || ''}}))
            .filter((img) => img.src);
          return {{url: href, text, images}};
        }})
        .filter((card) => card.url.includes('/i/article/') || (card.images.length && card.text.length > 40));
      const timeNode = scope.querySelector('time[datetime]');
      return {{
        url: location.href,
        title: document.title || '',
        author_name: normalize(nameNode?.innerText || ''),
        author_handle: normalize(handleNode || ''),
        published_at: timeNode?.getAttribute('datetime') || '',
        tweet_texts: tweetTexts,
        cards,
        media: Array.from(scope.querySelectorAll('img[src]'))
          .map((img) => ({{src: img.src || '', alt: img.alt || ''}}))
          .filter((img) => img.src),
        captured_at: new Date().toISOString()
      }};
    }})()\"\"\")

new_tab(target_url)
wait_for_load()
time.sleep(3)
data = extract_page()

article_url = ''
for card in data.get('cards') or []:
    if '/i/article/' in str(card.get('url') or ''):
        article_url = str(card.get('url') or '')
        break

if article_url:
    new_tab(article_url)
    wait_for_load()
    time.sleep(4)
    linked = js(\"\"\"(() => {{
      const normalize = (value) => (value || '')
        .replaceAll(String.fromCharCode(13), '\\n')
        .replace(/[ \\t]+/g, ' ')
        .replace(/\\n{{3,}}/g, '\\n\\n')
        .trim();
      const main = document.querySelector('main') || document.body;
      return {{
        url: location.href,
        title: document.title || '',
        text: normalize(main?.innerText || '').slice(0, 12000),
        captured_at: new Date().toISOString()
      }};
    }})()\"\"\")
    data['linked_article'] = linked

print(json.dumps(data))
"""
    timeout = _browser_timeout_seconds()
    env = os.environ.copy()
    env["BU_CDP_URL"] = cdp_url
    proc = subprocess.run(
        ["browser-harness", "-c", script],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(msg[:300] or f"browser-harness exited {proc.returncode}")
    payload = _last_json_line(proc.stdout)
    if not payload:
        raise RuntimeError("browser-harness returned no JSON payload")
    return _article_data_from_browser_payload(url, payload)


def _article_data_from_browser_payload(url: str, payload: dict[str, Any]) -> ArticleData | None:
    tweet_texts = [
        _clean_multiline(str(item))
        for item in payload.get("tweet_texts") or []
        if _clean_multiline(str(item))
    ]
    tweet_text = _best_tweet_text(tweet_texts)
    cards = _cards_from_browser_payload(payload)
    linked_article = _linked_article_from_payload(payload.get("linked_article"))
    author = {
        "display": _clean_piece(str(payload.get("author_name") or "")),
        "handle": _clean_piece(str(payload.get("author_handle") or "")),
        "url": "",
    }
    published_at = _clean_piece(str(payload.get("published_at") or "")) or None
    canonical = str(payload.get("url") or url)
    body = _render_text(
        canonical_url=canonical,
        author=author,
        tweet_text=tweet_text,
        published_at=published_at,
        cards=cards,
        metrics=[],
        linked_article=linked_article,
    )
    if not _text_score(body):
        return None
    media = _media_from_cards(cards) or _media_from_browser_payload(payload)
    return ArticleData(
        url=canonical,
        title=_title_from_parts(author=author, tweet_text=tweet_text, cards=cards, fallback=url),
        text=body,
        author=author.get("display") or None,
        published_at=published_at,
        hero_image_url=media[0]["src"] if media else None,
        inline_media=media,
        raw_html=json.dumps(payload, ensure_ascii=False),
    )


class _LinkCollector(HTMLParser):
    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.base_url = base_url
        self.links: list[dict[str, Any]] = []
        self._stack: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {key: value or "" for key, value in attrs}
        if tag == "a" and attr.get("href"):
            self._stack.append({
                "url": urljoin(self.base_url, html.unescape(attr["href"])),
                "texts": [],
                "labels": [],
                "images": [],
            })
        if not self._stack:
            return
        label = attr.get("aria-label")
        if label:
            self._stack[-1]["labels"].append(label)
        if tag == "img" and attr.get("src"):
            self._stack[-1]["images"].append({
                "src": urljoin(self.base_url, html.unescape(attr["src"])),
                "alt": _clean_piece(attr.get("alt") or ""),
            })

    def handle_data(self, data: str) -> None:
        if self._stack and data.strip():
            self._stack[-1]["texts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._stack:
            self.links.append(self._stack.pop())


def _extract_cards(html_text: str, base_url: str) -> list[dict[str, Any]]:
    parser = _LinkCollector(base_url)
    parser.feed(html_text or "")
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in parser.links:
        href = str(link.get("url") or "")
        if not href or href in seen:
            continue
        path = urlparse(href).path
        pieces = _dedupe_texts([*link.get("labels", []), *link.get("texts", [])])
        non_labels = [piece for piece in pieces if piece.lower() not in _CARD_LABELS]
        has_article_path = "/i/article/" in path
        has_rich_card_shape = bool(link.get("images")) and sum(len(p) for p in non_labels) > 40
        if not has_article_path and not has_rich_card_shape:
            continue
        seen.add(href)
        cards.append({
            "url": href,
            "title": non_labels[0] if non_labels else "",
            "description": non_labels[1] if len(non_labels) > 1 else "",
            "kind": pieces[0] if pieces and pieces[0].lower() in _CARD_LABELS else "",
            "images": link.get("images") or [],
        })
    return cards[:4]


def _find_social_post(html_text: str) -> dict[str, Any] | None:
    for obj in _json_ld_objects(html_text):
        found = _find_social_post_in_obj(obj)
        if found:
            return found
    return None


def _json_ld_objects(html_text: str) -> list[Any]:
    out: list[Any] = []
    for raw in _JSON_LD_RE.findall(html_text or ""):
        text = html.unescape(raw).strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        out.append(parsed)
    return out


def _find_social_post_in_obj(obj: Any) -> dict[str, Any] | None:
    if isinstance(obj, dict):
        obj_type = obj.get("@type")
        types = obj_type if isinstance(obj_type, list) else [obj_type]
        if "SocialMediaPosting" in types:
            return obj
        graph = obj.get("@graph")
        if isinstance(graph, list):
            for item in graph:
                found = _find_social_post_in_obj(item)
                if found:
                    return found
    elif isinstance(obj, list):
        for item in obj:
            found = _find_social_post_in_obj(item)
            if found:
                return found
    return None


def _author_from_post(post: dict[str, Any]) -> dict[str, str]:
    author = post.get("author") if isinstance(post.get("author"), dict) else {}
    name = _clean_piece(str(author.get("name") or ""))
    handle = _clean_piece(str(author.get("alternateName") or ""))
    url = str(author.get("url") or "")
    return {"display": name, "handle": handle, "url": url}


def _shared_content_urls(post: dict[str, Any]) -> list[str]:
    raw = post.get("sharedContent")
    items = raw if isinstance(raw, list) else [raw]
    urls: list[str] = []
    for item in items:
        if isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]).replace("http://x.com/", "https://x.com/"))
    return urls


def _metrics_from_post(post: dict[str, Any]) -> list[tuple[str, int]]:
    raw = post.get("interactionStatistic")
    rows = raw if isinstance(raw, list) else []
    metrics: list[tuple[str, int]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = _clean_piece(str(row.get("name") or ""))
        if not name:
            interaction = str(row.get("interactionType") or "")
            name = interaction.rsplit("/", 1)[-1].replace("Action", "") or "Interactions"
        try:
            count = int(row.get("userInteractionCount") or 0)
        except (TypeError, ValueError):
            continue
        metrics.append((name, count))
    return metrics


def _post_image(post: dict[str, Any]) -> str | None:
    image = post.get("image")
    if isinstance(image, str):
        return image
    if isinstance(image, list):
        return next((str(item) for item in image if item), None)
    return None


def _render_text(
    *,
    canonical_url: str,
    author: dict[str, str],
    tweet_text: str,
    published_at: str | None,
    cards: list[dict[str, Any]],
    metrics: list[tuple[str, int]],
    linked_article: dict[str, str] | None,
) -> str:
    lines: list[str] = []
    author_line = _format_author(author)
    lines.append(f"X post by {author_line}" if author_line else "X post")
    lines.append(f"URL: {canonical_url}")
    if published_at:
        lines.append(f"Published: {published_at}")
    if tweet_text:
        lines.extend(["", "Tweet:", tweet_text])
    for card in cards:
        card_lines = _render_card(card)
        if card_lines:
            lines.extend(["", "Shared link:", *card_lines])
    if linked_article and linked_article.get("text"):
        lines.extend([
            "",
            "Linked X article:",
            f"Title: {linked_article.get('title') or linked_article.get('url') or ''}",
            f"URL: {linked_article.get('url') or ''}",
            "",
            str(linked_article["text"]),
        ])
    if metrics:
        lines.append("")
        lines.append("Engagement:")
        for name, count in metrics:
            lines.append(f"- {name}: {count}")
    return "\n".join(line for line in lines if line is not None).strip()


def _render_card(card: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    title = _clean_piece(str(card.get("title") or ""))
    description = _clean_multiline(str(card.get("description") or ""))
    url = str(card.get("url") or "")
    kind = _clean_piece(str(card.get("kind") or ""))
    if kind:
        lines.append(f"Kind: {kind}")
    if title:
        lines.append(f"Title: {title}")
    if description:
        lines.append(f"Summary: {description}")
    if url:
        lines.append(f"URL: {url}")
    return lines


def _cards_from_browser_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    cards: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in payload.get("cards") or []:
        if not isinstance(raw, dict):
            continue
        url = str(raw.get("url") or "")
        if not url or url in seen:
            continue
        seen.add(url)
        pieces = _dedupe_texts(str(raw.get("text") or "").splitlines())
        non_labels = [piece for piece in pieces if piece.lower() not in _CARD_LABELS]
        cards.append({
            "url": url,
            "title": non_labels[0] if non_labels else "",
            "description": non_labels[1] if len(non_labels) > 1 else "",
            "kind": pieces[0] if pieces and pieces[0].lower() in _CARD_LABELS else "",
            "images": raw.get("images") if isinstance(raw.get("images"), list) else [],
        })
    return cards[:4]


def _linked_article_from_payload(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, dict):
        return None
    text = _clean_multiline(str(raw.get("text") or ""))
    if not text or "JavaScript is not available" in text:
        return None
    return {
        "url": str(raw.get("url") or ""),
        "title": _clean_piece(str(raw.get("title") or "")),
        "text": text,
    }


def _media_from_cards(cards: list[dict[str, Any]]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for card in cards:
        for img in card.get("images") or []:
            if not isinstance(img, dict):
                continue
            src = str(img.get("src") or "")
            if not src or src in seen or "profile_images" in src:
                continue
            seen.add(src)
            out.append({"src": src, "alt": _clean_piece(str(img.get("alt") or ""))})
    return out[:6]


def _media_from_browser_payload(payload: dict[str, Any]) -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    for img in payload.get("media") or []:
        if not isinstance(img, dict):
            continue
        src = str(img.get("src") or "")
        if not src or src in seen or "profile_images" in src or "emoji" in src:
            continue
        seen.add(src)
        out.append({"src": src, "alt": _clean_piece(str(img.get("alt") or ""))})
    return out[:6]


def _tweet_text_from_oembed_html(raw_html: str) -> str:
    m = re.search(r"<p\b[^>]*>(.*?)</p>", raw_html, flags=re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    text = re.sub(r"<br\s*/?>", "\n", m.group(1), flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    return _clean_multiline(html.unescape(text))


def _normalize_generic_data(data: ArticleData) -> ArticleData:
    text = _clean_multiline(data.text)
    if _looks_like_old_collapsed_x_text(text):
        text = _split_old_collapsed_x_text(text)
    return ArticleData(
        url=data.url,
        title=html.unescape(data.title),
        text=text,
        author=data.author,
        published_at=data.published_at,
        hero_image_url=data.hero_image_url,
        inline_media=data.inline_media,
        raw_html=data.raw_html,
    )


def _looks_like_old_collapsed_x_text(text: str) -> bool:
    return "@" in text and "Views" in text and "Read " in text and " replies" in text


def _split_old_collapsed_x_text(text: str) -> str:
    text = _clean_multiline(text)
    text = re.sub(r"([a-z0-9])(@[A-Za-z0-9_]+)", r"\1 \2", text)
    text = re.sub(r"(Article|Video)([A-Z])", r"\1\n\2", text)
    text = re.sub(r"(\.\.\.)(\d{1,2}:\d{2}\s[AP]M)", r"\1\n\2", text)
    text = re.sub(r"(\d+(?:\.\d+)?[KMB]?)(Views)", r"\1 \2", text)
    text = re.sub(r"(Read \d+ replies)", r"\n\1", text)
    return text.strip()


def _useful_twitter_data(data: ArticleData) -> bool:
    if _structured_marker(data.text):
        return _text_score(data.text) > 0
    return _useful_generic_data(data)


def _useful_generic_data(data: ArticleData) -> bool:
    text = _clean_multiline(data.text)
    if not text:
        return False
    lowered = text.lower()
    if "javascript is not available" in lowered or "please enable javascript" in lowered:
        return False
    if "sign in" in lowered and "x.com" in lowered and len(text) < 500:
        return False
    if _URL_ONLY_RE.match(text):
        return False
    return _text_score(text) > 0


def _structured_marker(text: str) -> bool:
    return text.startswith("X post") and ("\nTweet:" in text or "\nShared link:" in text)


def _text_score(text: str) -> int:
    clean = re.sub(r"https?://\S+", " ", text or "")
    clean = re.sub(r"@\w+", " ", clean)
    return len(re.findall(r"[A-Za-z0-9]{2,}", clean))


def _title_from_parts(
    *,
    author: dict[str, str],
    tweet_text: str,
    cards: list[dict[str, Any]],
    fallback: str,
) -> str:
    author_name = author.get("display") or author.get("handle") or "X"
    card_title = next((_clean_piece(str(card.get("title") or "")) for card in cards if card.get("title")), "")
    tweet_hint = _clean_piece(re.sub(r"https?://\S+", "", tweet_text or ""))
    hint = card_title or tweet_hint or fallback
    return f"{author_name} on X: {hint}"[:280]


def _format_author(author: dict[str, str]) -> str:
    name = author.get("display") or ""
    handle = author.get("handle") or ""
    if name and handle:
        return f"{name} ({handle})"
    return name or handle


def _best_tweet_text(texts: list[str]) -> str:
    if not texts:
        return ""
    non_ui = [
        text for text in texts
        if text.lower() not in {"post", "log in", "sign up"} and not text.startswith("@")
    ]
    return max(non_ui or texts, key=len)


def _clean_piece(text: str) -> str:
    text = html.unescape(str(text or "")).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def _clean_multiline(text: str) -> str:
    text = html.unescape(str(text or "")).replace("\xa0", " ")
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _dedupe_texts(items: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        clean = _clean_piece(item)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        out.append(clean)
    return out


def _body_from_raw_file(raw_text: str) -> str:
    parts = str(raw_text or "").split("\n\n", 2)
    return parts[2] if len(parts) >= 3 else str(raw_text or "")


def _last_json_line(stdout: str) -> dict[str, Any] | None:
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _status_id(url: str) -> str:
    m = _STATUS_RE.search(url)
    return m.group(1) if m else ""


def _browser_harness_cdp_url() -> str | None:
    if not shutil.which("browser-harness"):
        return None
    candidates: list[str] = []
    if configured := os.environ.get("BU_CDP_URL"):
        candidates.append(configured)
    candidates.extend(["http://127.0.0.1:9333", "http://127.0.0.1:9222"])
    for raw in candidates:
        cdp_url = raw.rstrip("/")
        if _cdp_endpoint_ready(cdp_url):
            return cdp_url
    return None


def _cdp_endpoint_ready(cdp_url: str) -> bool:
    try:
        with urlopen(f"{cdp_url}/json/version", timeout=0.5) as resp:
            return resp.status < 400
    except Exception:
        return False


def _browser_timeout_seconds() -> float:
    try:
        from mastisk.settings import get_settings
        return float(get_settings().tweet.browser_context_timeout_seconds)
    except Exception:
        return 25.0
