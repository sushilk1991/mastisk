from __future__ import annotations

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


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


def test_tweet_writer_enforces_practical_top_ten_theme_contract():
    from mastisk.agents.tweet_writer import _analyze_thread_intent, _validate_draft

    intent = _analyze_thread_intent(
        "How to take maximum advantage of Codex: top 10 hacks I have figured out "
        "while coding using agents for the past 6 months.",
    )
    draft = {
        "title": "Codex agent hacks",
        "angle": "A practical list of Codex habits that make agent coding less mushy.",
        "thread": [
            "10 Codex agent hacks I keep using after six months of coding with agents.",
            "1. Start with the failure mode. Ask Codex to name what would make the task wrong before it writes code.",
            "2. Keep the diff small. I split UI, backend, and prompt changes so the agent has one thing to hold.",
            "3. Run tests before polish. A pretty explanation is useless if the route or typecheck is broken.",
            "4. Save context as a skill when I repeat myself. The next run should inherit the scar tissue.",
            "5. Ask for file and line evidence. It stops vague agent confidence from turning into bad code.",
            "6. Use browser checks for UI. Screenshots catch the issues a green unit test will never see.",
            "7. Commit only the intended change. Agent speed is dangerous if unrelated dirt rides along.",
            "8. Make it explain the mechanism. If Codex cannot say why a fix works, I do not trust the patch.",
            "9. Restart local services after install changes. Otherwise I end up judging stale code.",
            "10. Keep one unresolved question. The best agent sessions end with the next sharp test, not a victory lap.",
        ],
        "sources": [],
        "warnings": [],
    }

    _, _, tweets, _, _ = _validate_draft(draft, intent=intent)
    assert len(tweets) == 11


def test_tweet_writer_rejects_topic_drift_from_top_ten_hacks_theme():
    from mastisk.agents.tweet_writer import _analyze_thread_intent, _validate_draft

    intent = _analyze_thread_intent(
        "How to take maximum advantage of Codex: top 10 hacks I have figured out "
        "while coding using agents for the past 6 months.",
    )
    draft = {
        "title": "Capability converged",
        "angle": "Recent AI model capability changed the cost curve.",
        "thread": [
            "Quandri found their MCP tool definitions ate 10.5% of the context window.",
            "Opus 4.8 landed incremental, and an open 11B-active model got close to old Opus.",
            "If frontier coding ability gets cheap, harness cost becomes the real fight.",
            "Tool schemas are an easy place to leak tokens and never notice.",
            "The question is whether CLI wrapping saves lifetime tokens or only upfront ones.",
        ],
        "sources": [],
        "warnings": [],
    }

    with pytest.raises(RuntimeError, match="exactly 11"):
        _validate_draft(draft, intent=intent)


def test_tweet_writer_sanitizes_browser_traceback_warning():
    from mastisk.agents.tweet_writer import _sanitize_warning

    warning = "browser/url context unavailable: Traceback (most recent call last):\n/Users/me/x.py"
    assert _sanitize_warning(warning) == (
        "Browser capture unavailable; generated without browser context."
    )


def test_tweet_writer_ignores_mastisk_tweet_page_as_browser_context():
    from mastisk.agents.tweet_writer import _is_mastisk_app_context

    assert _is_mastisk_app_context({"url": "http://localhost:5555/tweets/3"})
    assert not _is_mastisk_app_context({"url": "https://x.com/someone/status/1"})


def test_tweet_writer_prompt_includes_current_date_and_x_context(vault_tmp, data_tmp):
    from mastisk.agents.tweet_writer import (
        TweetWriter,
        _analyze_thread_intent,
        _current_date_context,
    )

    prompt = TweetWriter()._render_prompt(
        theme="codex agent hacks",
        intent=_analyze_thread_intent("codex agent hacks"),
        previous_tweets=[],
        feedback=[],
        local_sources=[],
        web_context=[{
            "kind": "browser",
            "title": "X: Codex agents are trending",
            "url": "https://x.com/someone/status/1",
            "excerpt": "Codex agents are trending in developer workflows.",
        }],
        browser_context=None,
    )

    assert "Current date:" in prompt
    assert datetime.now().astimezone().date().isoformat() in prompt
    assert "[browser] X: Codex agents are trending" in prompt
    fixed_date = _current_date_context(datetime(2026, 5, 30, tzinfo=UTC))
    assert fixed_date.startswith("Saturday, May 30, 2026 (")
    assert "ISO 2026-05-30" in fixed_date


def test_tweet_writer_searches_x_with_browser_harness():
    from mastisk.agents.tweet_writer import _x_browser_search_context

    calls: list[list[str]] = []

    def fake_run(args, **kwargs):
        calls.append(args)
        payload = {
            "captured_at": "2026-05-30T10:00:00Z",
            "url": "https://x.com/search?q=codex%20agent%20hacks",
            "tweets": [
                {
                    "url": "https://x.com/dev/status/123",
                    "text": "Codex agent workflows are moving from prompt tricks to verified diffs.",
                },
            ],
        }
        return subprocess.CompletedProcess(
            args,
            0,
            stdout=f"browser started\n{json.dumps(payload)}\n",
            stderr="",
        )

    with patch("mastisk.agents.tweet_writer.subprocess.run", side_effect=fake_run):
        rows = _x_browser_search_context("codex agent hacks")

    assert calls
    assert calls[0][:2] == ["browser-harness", "-c"]
    assert "https://x.com/search" in calls[0][2]
    assert "q=codex%20agent%20hacks" in calls[0][2]
    assert rows == [{
        "kind": "browser",
        "title": "X: Codex agent workflows are moving from prompt tricks to verified diffs.",
        "url": "https://x.com/dev/status/123",
        "snippet": (
            "Live X search for codex agent hacks captured at 2026-05-30T10:00:00Z"
        ),
        "excerpt": "Codex agent workflows are moving from prompt tricks to verified diffs.",
    }]


def test_tweet_writer_keeps_x_browser_search_when_public_web_fails():
    from mastisk.agents.tweet_writer import TweetWriter, _analyze_thread_intent

    writer = TweetWriter()
    x_context = [{
        "kind": "browser",
        "title": "X: Codex agent workflows",
        "url": "https://x.com/dev/status/123",
        "snippet": "Live X search",
        "excerpt": "Codex agent workflows are current on X.",
    }]

    with patch.object(
        writer,
        "_fetch_public_web_results",
        new_callable=AsyncMock,
        side_effect=RuntimeError("duckduckgo down"),
    ), patch.object(
        writer,
        "_gather_x_browser_context",
        new_callable=AsyncMock,
        return_value=x_context,
    ) as x_search:
        rows = asyncio.run(writer._gather_web_context(
            theme="codex agent hacks",
            intent=_analyze_thread_intent("codex agent hacks"),
            local_sources=[],
            browser_context=None,
        ))

    assert rows == x_context
    x_search.assert_awaited_once()


def test_tweet_writer_incorporates_pending_feedback(db, vault_tmp, data_tmp):
    from mastisk.agents.tweet_writer import TweetWriter
    from mastisk.db.queries import (
        create_tweet_thread_feedback,
        update_tweet_thread_done,
    )

    _seed_note(
        db,
        body="I use Codex for small verified changes, not giant trust-me patches.",
        summary="Codex works best with small verified changes.",
    )
    thread_id = _seed_thread(db, theme="codex agent workflow", include_web=False)
    update_tweet_thread_done(
        db,
        thread_id=thread_id,
        title="Old",
        angle="Old",
        model="claude",
        thread_json=json.dumps([
            "Codex is useful for agent coding.",
            "Let it write a lot and review later.",
        ]),
        sources_json="[]",
        warnings_json="[]",
    )
    db.execute("UPDATE tweet_threads SET status='pending' WHERE id=?", (thread_id,))
    create_tweet_thread_feedback(
        db,
        thread_id=thread_id,
        target_tweet_index=1,
        body="make this less trusting and more about small verified diffs",
    )
    _enqueue(thread_id)

    draft = {
        "title": "Codex verified diffs",
        "angle": "Small verified changes beat big trust-me patches.",
        "thread": [
            "Codex works best when I keep the patch small enough to verify.",
            "The move is not 'write a lot and review later.' I ask for one diff I can run, read, and click.",
            "That makes agent speed useful without turning review into archaeology.",
        ],
        "sources": [{"kind": "local", "title": "Codex verified changes"}],
        "warnings": [],
    }
    prompts: list[str] = []

    async def fake_run_intelligence(prompt: str, *args, **kwargs):
        prompts.append(prompt)
        return {"text": json.dumps(draft)}, "claude"

    with patch(
        "mastisk.agents.tweet_writer.run_intelligence",
        new_callable=AsyncMock,
        side_effect=fake_run_intelligence,
    ):
        asyncio.run(TweetWriter().run_once())

    row = db.execute(
        "SELECT status, thread_json FROM tweet_threads WHERE id=?", (thread_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert "small enough to verify" in json.loads(row["thread_json"])[0]
    feedback = db.execute(
        "SELECT applied_at FROM tweet_thread_feedback WHERE tweet_thread_id=?", (thread_id,),
    ).fetchone()
    assert feedback["applied_at"] is not None
    assert "make this less trusting" in prompts[0]
    assert "Previous tweet 2" in prompts[0]
