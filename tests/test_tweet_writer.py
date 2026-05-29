from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch


def _seed_note(db, *, body: str, summary: str, days_ago: int = 0) -> int:
    ts = (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat()
    slug = f"tweet-note-{days_ago}"
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              classified_at, classification, summary, tags_json)
           VALUES (?, ?, ?, 'h', 'pwa', ?, ?, 'idea', ?, '[]')""",
        (slug, f"_notes/inbox/{slug}.md", body, ts, ts, summary),
    )
    return db.execute("SELECT id FROM notes WHERE slug=?", (slug,)).fetchone()["id"]


def _seed_thread(db, **kwargs) -> int:
    from mastisk.db.queries import create_tweet_thread

    return create_tweet_thread(
        db,
        theme=kwargs.get("theme", ""),
        url=kwargs.get("url"),
        window_days=kwargs.get("window_days", 7),
        include_web=kwargs.get("include_web", False),
        use_browser_context=kwargs.get("use_browser_context", False),
    )


def _enqueue(thread_id: int) -> None:
    from mastisk.agents.base import enqueue

    enqueue("tweet_writer", "draft", {"tweet_thread_id": thread_id})


def test_tweet_writer_generates_thread_from_recent_note(db, vault_tmp, data_tmp):
    from mastisk.agents.tweet_writer import TweetWriter

    _seed_note(
        db,
        body="I keep noticing that browser-capable agents change the UI testing loop.",
        summary="Browser-capable agents make UI testing more direct.",
    )
    thread_id = _seed_thread(db, theme="browser agents", include_web=False)
    _enqueue(thread_id)

    draft = {
        "title": "Browser agents",
        "angle": "Browser use turns QA into an observation loop.",
        "thread": [
            "The interesting part of browser agents is not that they can click buttons.",
            "It is that the loop becomes inspect, act, verify, then revise.",
            "That changes the product surface we should build for debugging.",
        ],
        "sources": [{"kind": "local", "title": "Browser-capable agents"}],
        "warnings": [],
    }

    async def fake_run_intelligence(*args, **kwargs):
        return {"text": json.dumps(draft)}, "claude"

    with patch(
        "mastisk.agents.tweet_writer.run_intelligence",
        new_callable=AsyncMock,
        side_effect=fake_run_intelligence,
    ):
        asyncio.run(TweetWriter().run_once())

    row = db.execute(
        "SELECT * FROM tweet_threads WHERE id=?", (thread_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["title"] == "Browser agents"
    assert row["model"] == "claude+claude-polish"
    assert json.loads(row["thread_json"])[0].startswith("The interesting")

    feed = db.execute(
        "SELECT * FROM feed WHERE agent='tweet_writer' AND verb='tweet-thread-done'",
    ).fetchall()
    assert len(feed) == 1


def test_tweet_writer_fails_on_overlong_tweet(db, vault_tmp, data_tmp):
    from mastisk.agents.tweet_writer import TweetWriter

    _seed_note(db, body="short", summary="short")
    thread_id = _seed_thread(db, include_web=False)
    _enqueue(thread_id)

    draft = {
        "title": "Bad",
        "angle": "Bad",
        "thread": ["x" * 281],
        "sources": [],
        "warnings": [],
    }

    async def fake_run_intelligence(*args, **kwargs):
        return {"text": json.dumps(draft)}, "claude"

    with patch(
        "mastisk.agents.tweet_writer.run_intelligence",
        new_callable=AsyncMock,
        side_effect=fake_run_intelligence,
    ):
        asyncio.run(TweetWriter().run_once())

    row = db.execute(
        "SELECT status, error FROM tweet_threads WHERE id=?", (thread_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert "over 240" in row["error"]


def test_tweet_writer_can_use_url_without_browser(db, vault_tmp, data_tmp):
    from mastisk.agents.tweet_writer import TweetWriter

    thread_id = _seed_thread(
        db,
        url="https://example.com/post",
        include_web=False,
        use_browser_context=False,
    )
    _enqueue(thread_id)

    draft = {
        "title": "URL thread",
        "angle": "A page can seed the observation.",
        "thread": ["A linked page can be enough context for a short thread."],
        "sources": [{"kind": "browser", "title": "Example"}],
        "warnings": [],
    }

    async def fake_context(*args, **kwargs):
        return {
            "kind": "url",
            "url": "https://example.com/post",
            "title": "Example",
            "text": "Example page text",
        }

    async def fake_run_intelligence(*args, **kwargs):
        return {"text": json.dumps(draft)}, "claude"

    with patch.object(
        TweetWriter,
        "_fetch_url_context",
        new_callable=AsyncMock,
        side_effect=fake_context,
    ), patch(
        "mastisk.agents.tweet_writer.run_intelligence",
        new_callable=AsyncMock,
        side_effect=fake_run_intelligence,
    ):
        asyncio.run(TweetWriter().run_once())

    row = db.execute(
        "SELECT status, thread_json FROM tweet_threads WHERE id=?", (thread_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert json.loads(row["thread_json"]) == draft["thread"]


def test_tweet_writer_falls_back_to_plain_fetch_when_browser_fails(db, vault_tmp, data_tmp):
    from mastisk.agents.tweet_writer import TweetWriter

    thread_id = _seed_thread(
        db,
        url="https://example.com/post",
        include_web=False,
        use_browser_context=True,
    )
    _enqueue(thread_id)

    draft = {
        "title": "Fallback",
        "angle": "Browser fallback still uses the URL.",
        "thread": ["If authenticated browser capture fails, the URL should still be read."],
        "sources": [{"kind": "web", "title": "Example"}],
        "warnings": [],
    }

    async def fake_context(*args, **kwargs):
        return {
            "kind": "url",
            "url": "https://example.com/post",
            "title": "Example",
            "text": "Example page text",
        }

    async def fake_run_intelligence(*args, **kwargs):
        return {"text": json.dumps(draft)}, "claude"

    with patch(
        "mastisk.agents.tweet_writer._browser_context",
        side_effect=RuntimeError("chrome unavailable"),
    ), patch.object(
        TweetWriter,
        "_fetch_url_context",
        new_callable=AsyncMock,
        side_effect=fake_context,
    ), patch(
        "mastisk.agents.tweet_writer.run_intelligence",
        new_callable=AsyncMock,
        side_effect=fake_run_intelligence,
    ):
        asyncio.run(TweetWriter().run_once())

    row = db.execute(
        "SELECT status, warnings_json FROM tweet_threads WHERE id=?", (thread_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert "plain URL fetch" in json.loads(row["warnings_json"])[0]


def test_tweet_writer_rejects_sloppy_analyst_patterns(db, vault_tmp, data_tmp):
    from mastisk.agents.tweet_writer import TweetWriter

    _seed_note(db, body="short", summary="short")
    thread_id = _seed_thread(db, include_web=False)
    _enqueue(thread_id)

    draft = {
        "title": "Slop",
        "angle": "Slop",
        "thread": [
            "The pattern is clear: the ecosystem is shifting in a crucial way.",
            "Read the constraint, not the number.",
        ],
        "sources": [],
        "warnings": [],
    }

    async def fake_run_intelligence(*args, **kwargs):
        return {"text": json.dumps(draft)}, "claude"

    with patch(
        "mastisk.agents.tweet_writer.run_intelligence",
        new_callable=AsyncMock,
        side_effect=fake_run_intelligence,
    ):
        asyncio.run(TweetWriter().run_once())

    row = db.execute(
        "SELECT status, error FROM tweet_threads WHERE id=?", (thread_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert "anti-slop" in row["error"]
