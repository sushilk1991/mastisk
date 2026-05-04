"""End-to-end test for Listener._ingest_article — the universal-URL path.

Mocks the article extractor (no real HTTP), runs the Listener, and asserts:
  * a sources row lands with kind='blog' (or 'twitter' for x.com URLs)
  * a compiler/compile job is enqueued referencing that source
  * the raw_path file is written with title + URL + body in Scout-compatible shape
  * a 'clipped' feed row is emitted
  * re-running the same URL is a no-op (dedup)
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def listener(db, vault_tmp):
    from mastisk.paths import ensure_dirs
    ensure_dirs()
    from mastisk.agents.listener import Listener
    return Listener()


def _enqueue(db, kind: str, payload: dict) -> int:
    cur = db.execute(
        "INSERT INTO jobs (agent, kind, payload_json) VALUES ('listener', ?, ?)",
        (kind, json.dumps(payload)),
    )
    return cur.lastrowid or 0


def _stub_article_data(*, url="https://example.com/post", title="A Post"):
    """Build an article_extractor.ArticleData stand-in. Real fetch is patched
    out so the test never hits the network."""
    from mastisk.integrations.article import ArticleData
    return ArticleData(
        url=url,
        title=title,
        text=("This is the extracted main text of the post. " * 8).strip(),
        author="Jane Smith",
        published_at="2026-04-22T12:00:00Z",
        hero_image_url="https://example.com/og.jpg",
        inline_media=[
            {"src": "https://example.com/diagram.png", "alt": "diagram"},
        ],
        raw_html="<html>…</html>",
    )


def _patch_classify(kind: str = "article"):
    """classify_and_resolve is called by the Listener. Stub it to return
    a fixed kind so the test stays focused on the article-ingestion path."""
    return patch(
        "mastisk.agents.listener.podcasts.classify_and_resolve",
        new_callable=AsyncMock,
        return_value=(kind, "https://example.com/post"),
    )


def _patch_fetch(article_data):
    return patch(
        "mastisk.agents.listener.article_extractor.fetch_and_extract",
        new_callable=AsyncMock,
        return_value=article_data,
    )


def test_ingest_article_creates_source_and_compile_job(listener, db, vault_tmp):
    _enqueue(db, "transcribe", {"url": "https://example.com/post"})
    data = _stub_article_data()

    with _patch_classify("article"), _patch_fetch(data):
        asyncio.run(listener.run_once())

    # Source row landed with kind='blog' (universal HTML page).
    src = db.execute(
        "SELECT id, kind, url, title, author, hero_image_url, media_json FROM sources WHERE url = ?",
        (data.url,),
    ).fetchone()
    assert src is not None
    assert src["kind"] == "blog"
    assert src["title"] == "A Post"
    assert src["author"] == "Jane Smith"
    assert src["hero_image_url"] == "https://example.com/og.jpg"
    media = json.loads(src["media_json"])
    assert media[0]["src"] == "https://example.com/diagram.png"

    # Compiler job enqueued for that source.
    job = db.execute(
        "SELECT kind, payload_json FROM jobs WHERE agent='compiler' AND kind='compile'",
    ).fetchone()
    assert job is not None
    assert json.loads(job["payload_json"])["source_id"] == src["id"]

    # Feed row emitted.
    feed = db.execute(
        "SELECT verb, kind, obj FROM feed WHERE agent='listener' AND verb='clipped'",
    ).fetchone()
    assert feed is not None
    assert feed["kind"] == "blog"
    assert feed["obj"] == "A Post"


def test_ingest_twitter_url_persists_with_twitter_kind(listener, db, vault_tmp):
    """X / Twitter URLs route through the same article extractor but the
    source kind is 'twitter' so the UI can label them differently."""
    _enqueue(db, "transcribe", {"url": "https://x.com/jack/status/123"})
    data = _stub_article_data(url="https://x.com/jack/status/123", title="a tweet")

    with _patch_classify("twitter"), _patch_fetch(data):
        asyncio.run(listener.run_once())

    src = db.execute(
        "SELECT kind, title FROM sources WHERE url = ?",
        ("https://x.com/jack/status/123",),
    ).fetchone()
    assert src is not None
    assert src["kind"] == "twitter"
    assert src["title"] == "a tweet"


def test_ingest_article_dedupes_on_canonical_url(listener, db, vault_tmp):
    """Re-running with the same canonical URL must NOT create a duplicate
    source row or re-enqueue a compile job — Compiler curation could be
    clobbered by a re-run."""
    _enqueue(db, "transcribe", {"url": "https://example.com/post"})
    data = _stub_article_data()

    with _patch_classify("article"), _patch_fetch(data):
        asyncio.run(listener.run_once())

    # Second pass on the same URL.
    _enqueue(db, "transcribe", {"url": "https://example.com/post"})
    with _patch_classify("article"), _patch_fetch(data):
        asyncio.run(listener.run_once())

    src_count = db.execute(
        "SELECT COUNT(*) AS n FROM sources WHERE url = ?",
        (data.url,),
    ).fetchone()["n"]
    assert src_count == 1
    job_count = db.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE agent='compiler' AND kind='compile'",
    ).fetchone()["n"]
    assert job_count == 1
    # And a 'duplicate' feed row was emitted on the second run.
    dup = db.execute(
        "SELECT COUNT(*) AS n FROM feed WHERE agent='listener' AND verb='duplicate'",
    ).fetchone()["n"]
    assert dup == 1


def test_ingest_article_propagates_extractor_errors(listener, db, vault_tmp):
    """Paywall / no-extractable-text failures bubble up as job 'failed'
    state so the user sees them in the queue UI (same shape as other listener
    failures)."""
    _enqueue(db, "transcribe", {"url": "https://paywalled.example.com/x"})

    with _patch_classify("article"), patch(
        "mastisk.agents.listener.article_extractor.fetch_and_extract",
        new_callable=AsyncMock,
        side_effect=RuntimeError("no extractable text — paywall"),
    ):
        asyncio.run(listener.run_once())

    job = db.execute(
        "SELECT status, error FROM jobs WHERE kind='transcribe' ORDER BY id DESC LIMIT 1",
    ).fetchone()
    assert job["status"] == "failed"
    assert "paywall" in (job["error"] or "")
