"""BlogWriter agent tests. Both LLM bridges are mocked so no real network /
subprocess calls happen. See src/mastisk/agents/blog_writer.py + spec §§6-13.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


def _seed_note(
    db, *, body: str, summary: str = "a summary",
    classification: str = "idea", days_ago: int = 1, slug: str | None = None,
) -> int:
    """Insert a classified note within the window."""
    now = datetime.now().astimezone()
    ts = (now - timedelta(days=days_ago)).isoformat()
    slug_to_use = slug or f"n-{days_ago}-{abs(hash(body)) % 100000}"
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              classified_at, classification, summary, tags_json)
           VALUES (?, ?, ?, 'h', 'pwa', ?, ?, ?, ?, '[]')""",
        (slug_to_use, f"_notes/inbox/{slug_to_use}.md", body, ts, ts,
         classification, summary),
    )
    return db.execute(
        "SELECT id FROM notes WHERE slug=?", (slug_to_use,),
    ).fetchone()["id"]


def _seed_article(db, *, article_id: str = "a1", title: str = "An article") -> None:
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, body_md, summary)
           VALUES (?, 'Concept', ?, ?, 'article body prose here', 'the summary')""",
        (article_id, title, article_id),
    )


def _seed_roundtable(db, *, prompt: str = "what is X?", synthesis: str = "the synthesis") -> int:
    cur = db.execute(
        """INSERT INTO roundtables (input_type, input_ref, prompt, status,
                                    synthesis, synthesis_model, finished_at)
           VALUES ('prompt', '', ?, 'done', ?, 'claude', CURRENT_TIMESTAMP)""",
        (prompt, synthesis),
    )
    return cur.lastrowid or 0


def _seed_blog_post(db, *, theme: str = "", window_days: int = 14) -> int:
    from mastisk.db.queries import create_blog_post
    return create_blog_post(db, theme=theme, window_days=window_days)


def _enqueue(bp_id: int) -> None:
    from mastisk.agents.base import enqueue
    enqueue("blog_writer", "draft", {"blog_post_id": bp_id})


_FAKE_DRAFT = {
    "title": "The shape of the draft",
    "tags": ["test-time-compute", "agents"],
    "body_md": (
        "This is the opening sentence [source 1].\n\n"
        "And a second paragraph [source 2, source 3].\n\n"
        "What if I'm still thinking about this?\n"
    ),
}


@pytest.fixture
def agent(db, vault_tmp, data_tmp):
    from mastisk.paths import ensure_dirs
    ensure_dirs()
    from mastisk.agents.blog_writer import BlogWriter
    return BlogWriter()


# ─────────────────────────────── happy path ───────────────────────────────


def test_happy_path_writes_file_and_rows(agent, db, vault_tmp):
    _seed_note(db, body="note one body", summary="note one summary")
    _seed_article(db)
    _seed_roundtable(db)
    bp_id = _seed_blog_post(db, theme="")
    _enqueue(bp_id)

    async def fake_claude(prompt, **kwargs):
        return {"text": json.dumps(_FAKE_DRAFT)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    row = db.execute(
        "SELECT * FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["title"] == "The shape of the draft"
    assert row["model"] == "claude"
    assert row["word_count"] and row["word_count"] > 0
    assert row["path"].startswith("blog/drafts/")
    file_path = vault_tmp / row["path"]
    assert file_path.exists()
    text = file_path.read_text()
    assert "---" in text  # frontmatter marker
    assert "[source 1]" in text
    assert "The shape of the draft" in text or f"blog_post_id: {bp_id}" in text

    # Sources rows — exactly one per ranked candidate. 3 seeded items.
    sources = db.execute(
        "SELECT * FROM blog_post_sources WHERE blog_post_id=? ORDER BY rank",
        (bp_id,),
    ).fetchall()
    assert len(sources) == 3
    used = [s for s in sources if s["used"] == 1]
    # Our fake draft cites [source 1], [source 2, source 3] → 3 used.
    assert len(used) == 3

    # Feed row.
    feed = db.execute(
        "SELECT * FROM feed WHERE agent='blog_writer' AND verb='blog-done'",
    ).fetchall()
    assert len(feed) == 1


def test_happy_path_no_theme_sorts_by_recency(agent, db, vault_tmp):
    """Without a theme, the top-ranked candidate should be the most recent."""
    _seed_note(db, body="old note", summary="old", days_ago=10, slug="old")
    new_id = _seed_note(db, body="new note", summary="new", days_ago=0, slug="new")
    bp_id = _seed_blog_post(db, theme="")
    _enqueue(bp_id)

    # Fake draft cites only [source 1].
    draft = {"title": "t", "tags": [], "body_md": "x [source 1]"}

    async def fake_claude(prompt, **kwargs):
        return {"text": json.dumps(draft)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    used_row = db.execute(
        "SELECT kind, ref FROM blog_post_sources WHERE blog_post_id=? AND rank=1",
        (bp_id,),
    ).fetchone()
    assert used_row["kind"] == "note"
    assert int(used_row["ref"]) == new_id


# ─────────────────────────────── empty pool ───────────────────────────────


def test_no_sources_in_window_fails(agent, db):
    # No notes/articles/roundtables in window.
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    async def _never_called(*a, **kw):
        raise AssertionError("LLM should not be called when pool is empty")

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=_never_called,
    ):
        asyncio.run(agent.run_once())

    row = db.execute(
        "SELECT status, error FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert "no sources in window" in row["error"]


# ─────────────────────────────── Claude → Ollama fallback ───────────────────────────────


def test_claude_failure_falls_back_to_ollama(agent, db, vault_tmp):
    _seed_note(db, body="n", summary="s")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    async def fake_claude(*a, **kw):
        raise RuntimeError("claude quota")

    async def fake_ollama(prompt, model, **kw):
        return {"text": json.dumps(_FAKE_DRAFT)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ), patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    row = db.execute(
        "SELECT status, model FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["model"] == "ollama"


def test_both_models_fail(agent, db):
    _seed_note(db, body="n", summary="s")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    async def always_fail(*a, **kw):
        raise RuntimeError("down")

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=always_fail,
    ), patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=always_fail,
    ):
        asyncio.run(agent.run_once())

    row = db.execute(
        "SELECT status, error FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["status"] == "failed"
    assert "LLM failed" in row["error"]


# ─────────────────────────────── deletion race ───────────────────────────────


def test_delete_mid_draft_cleans_up_file(agent, db, vault_tmp):
    """If deleted_at is set between the pre-write check and now, we unlink
    the file and do NOT mark the row done."""
    _seed_note(db, body="n", summary="s")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    # fake_claude is called once. Before it returns, we arrange for the row
    # to be deleted. Use a call-count side-effect to check before flipping.
    call_count = {"n": 0}

    async def fake_claude(*a, **kw):
        call_count["n"] += 1
        # Mid-draft: the user hits DELETE before the agent writes the file.
        db.execute(
            "UPDATE blog_posts SET deleted_at=CURRENT_TIMESTAMP WHERE id=?",
            (bp_id,),
        )
        return {"text": json.dumps(_FAKE_DRAFT)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    row = db.execute(
        "SELECT status, deleted_at FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    # Status should NOT be 'done' — the pre-write check short-circuited.
    assert row["deleted_at"] is not None
    assert row["status"] != "done"


# ─────────────────────────────── citation parsing ───────────────────────────────


def test_postprocess_out_of_range_citation_stripped(agent, db):
    """`[source 99]` where only 3 sources exist → bracket stripped, warning logged."""
    from mastisk.agents.blog_writer import BlogWriter
    body = "hello [source 1] world [source 99] end [source 2]"
    cleaned, cited = BlogWriter._postprocess_citations(body, source_count=3)
    assert "[source 99]" not in cleaned
    assert "[source 1]" in cleaned
    assert "[source 2]" in cleaned
    assert cited == {1, 2}


def test_postprocess_multi_citation(agent, db):
    from mastisk.agents.blog_writer import BlogWriter
    body = "claim [source 2, source 5] more text"
    cleaned, cited = BlogWriter._postprocess_citations(body, source_count=5)
    assert "[source 2, source 5]" in cleaned
    assert cited == {2, 5}


# ─────────────────────────────── theme-mode rerank ───────────────────────────────


def test_theme_rerank_validation_failure_falls_back_to_keyword(agent, db, vault_tmp):
    """A garbage rerank response should fall back to keyword/recency order and
    still produce a draft."""
    _seed_note(db, body="n about X", summary="X summary", slug="a")
    _seed_note(db, body="n about Y", summary="Y summary", days_ago=3, slug="b")
    bp_id = _seed_blog_post(db, theme="X")
    _enqueue(bp_id)

    async def bad_rerank(prompt, model, **kw):
        return {"text": "this is not JSON"}

    async def fake_claude(*a, **kw):
        # Return a draft citing [source 1] — whatever that turns out to be.
        return {"text": json.dumps({"title": "t", "tags": [], "body_md": "x [source 1]"})}

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=bad_rerank,
    ), patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    # Draft still landed.
    row = db.execute(
        "SELECT status FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["status"] == "done"


def test_theme_rerank_success_reorders_sources(agent, db, vault_tmp):
    """A valid rerank with scored entries should put the second note first."""
    # Seed in order: n_a (most recent) then n_b. The theme "alpha" is too
    # specific to overlap with either body, so the keyword pre-rank produces
    # a recency-tiebreak order [a, b] (indices 0, 1). The rerank then flips
    # them by giving index 1 the higher score.
    a_id = _seed_note(db, body="body about alpha thread", summary="alpha-summary",
                      days_ago=0, slug="a")
    b_id = _seed_note(db, body="body about beta thread", summary="beta-summary",
                      days_ago=1, slug="b")
    bp_id = _seed_blog_post(db, theme="zeta-theme-keyword")
    _enqueue(bp_id)

    # Rerank puts index 1 (note_b) first with the higher score. After the 1.6
    # note multiplier both clear the 0.4 threshold, so both survive but b is
    # ranked above a in the final order.
    async def good_rerank(prompt, model, **kw):
        return {"text": '{"scored": [{"i": 1, "score": 0.8}, {"i": 0, "score": 0.7}]}'}

    async def fake_claude(*a, **kw):
        return {"text": json.dumps({"title": "t", "tags": [], "body_md": "x [source 1]"})}

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=good_rerank,
    ), patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    row = db.execute(
        "SELECT status FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["status"] == "done"
    # The rerank put index 1 (note_b) first → blog_post_sources rank=1 must
    # be note_b. If the boost or threshold filter drops the wrong row, this
    # check would catch it.
    rank1 = db.execute(
        "SELECT kind, ref FROM blog_post_sources WHERE blog_post_id=? AND rank=1",
        (bp_id,),
    ).fetchone()
    assert rank1["kind"] == "note"
    assert int(rank1["ref"]) == b_id


# ─────────────────────────────── terminal-status guard ───────────────────────────────


def test_terminal_status_is_skipped(agent, db):
    bp_id = _seed_blog_post(db)
    db.execute(
        "UPDATE blog_posts SET status='done' WHERE id=?", (bp_id,),
    )
    _enqueue(bp_id)

    async def _never(*a, **kw):
        raise AssertionError("bridge should not be called on terminal status")

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=_never,
    ):
        asyncio.run(agent.run_once())

    row = db.execute("SELECT status FROM blog_posts WHERE id=?", (bp_id,)).fetchone()
    assert row["status"] == "done"  # still done — untouched


def test_deleted_before_start_is_skipped(agent, db):
    """deleted_at set before the agent picked up → skip silently."""
    bp_id = _seed_blog_post(db)
    db.execute(
        "UPDATE blog_posts SET deleted_at=CURRENT_TIMESTAMP WHERE id=?",
        (bp_id,),
    )
    _enqueue(bp_id)

    async def _never(*a, **kw):
        raise AssertionError("bridge should not be called on deleted row")

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=_never,
    ):
        asyncio.run(agent.run_once())

    row = db.execute(
        "SELECT status FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    # Status untouched — we never even flipped to running.
    assert row["status"] == "pending"


# ─────────────────────────────── repo_ideator tagging ───────────────────────────────


def test_repo_ideator_origin_tag_on_sources(agent, db, vault_tmp):
    """A note whose id is in an in-window repo_idea_runs.note_ids_json → its
    blog_post_sources row should carry origin='repo_ideator'."""
    note_id = _seed_note(db, body="from repo", summary="repo note")
    # Link it to a repo_idea_runs in-window.
    db.execute("INSERT INTO repos (slug, owner, name) VALUES ('a/b', 'a', 'b')")
    db.execute(
        """INSERT INTO repo_idea_runs (repo_slug, ideated_at, note_ids_json, model)
           VALUES ('a/b', datetime('now','-1 days'), ?, 'claude')""",
        (json.dumps([note_id]),),
    )
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    async def fake_claude(*a, **kw):
        return {"text": json.dumps({"title": "t", "tags": [], "body_md": "x [source 1]"})}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    row = db.execute(
        "SELECT origin FROM blog_post_sources WHERE blog_post_id=? AND rank=1",
        (bp_id,),
    ).fetchone()
    assert row["origin"] == "repo_ideator"


# ─────────────────────────────── slug derivation ───────────────────────────────


def test_derive_blog_slug_uses_created_at_date():
    from mastisk.agents.blog_writer import _derive_blog_slug
    slug = _derive_blog_slug("The Title Here", bp_id=5, created_at="2026-04-22T10:00:00")
    assert slug == "2026-04-22-the-title-here"


def test_derive_blog_slug_falls_back_when_title_empty():
    from mastisk.agents.blog_writer import _derive_blog_slug
    slug = _derive_blog_slug("", bp_id=7, created_at="2026-04-22T10:00:00")
    assert slug == "2026-04-22-draft-7"


# ─────────────────────────────── regenerate behavior ───────────────────────────────


def test_regenerate_cascade_delete_and_redraft(agent, db, vault_tmp):
    """After a regenerate (sources deleted, row reset), a fresh run writes a
    fresh set of sources rows — not duplicates on top of the old ones."""
    _seed_note(db, body="n1", summary="s1", slug="n1")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    async def fake_claude(*a, **kw):
        return {"text": json.dumps({"title": "t", "tags": [], "body_md": "x [source 1]"})}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    n1 = db.execute(
        "SELECT COUNT(*) AS c FROM blog_post_sources WHERE blog_post_id=?",
        (bp_id,),
    ).fetchone()["c"]
    assert n1 == 1

    # Simulate a regenerate by clearing + resetting.
    from mastisk.db.queries import (
        delete_blog_post_sources,
        reset_blog_post_for_regenerate,
    )
    delete_blog_post_sources(db, bp_id)
    reset_blog_post_for_regenerate(db, bp_id)
    _enqueue(bp_id)

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    n2 = db.execute(
        "SELECT COUNT(*) AS c FROM blog_post_sources WHERE blog_post_id=?",
        (bp_id,),
    ).fetchone()["c"]
    # One note → one source row again (no duplicates from the prior run).
    assert n2 == 1


# ───── regenerate orphan cleanup (Fix 1) ─────


def test_regenerate_unlinks_stale_draft_file(agent, db, vault_tmp):
    """A second run on a reset row should unlink the prior draft file before
    writing the new one, not slide to a ``-2.md`` variant.

    Flow: first run writes ``<slug>.md`` and sets path/slug on the row.
    reset_blog_post_for_regenerate INTENTIONALLY preserves path/slug so the
    agent can see the stale draft and unlink it before calling
    _unique_draft_path.
    """
    _seed_note(db, body="n1", summary="s1", slug="n1")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    async def fake_claude(*a, **kw):
        return {"text": json.dumps(_FAKE_DRAFT)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    row = db.execute(
        "SELECT path FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    first_path = vault_tmp / row["path"]
    assert first_path.exists()

    # Regenerate: clear sources + reset the row (mirrors what the route does).
    from mastisk.db.queries import (
        delete_blog_post_sources,
        reset_blog_post_for_regenerate,
    )
    delete_blog_post_sources(db, bp_id)
    reset_blog_post_for_regenerate(db, bp_id)
    _enqueue(bp_id)

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    # The new draft file uses the same base slug — prior file was unlinked
    # so no ``-2`` suffix was needed. Final path matches first_path.
    row2 = db.execute(
        "SELECT path FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    final_path = vault_tmp / row2["path"]
    assert final_path == first_path
    assert final_path.exists()
    # No ``-2.md`` leftover — we unlinked, not renamed.
    neighbor = vault_tmp / row2["path"].replace(".md", "-2.md")
    assert not neighbor.exists()


# ───── post-write DELETE race (Fix 2) ─────


def test_post_write_delete_race_unlinks_orphan(agent, db, vault_tmp):
    """If DELETE fires between the post-write re-check and the status=done
    UPDATE, update_blog_post_done returns 0 rows affected; the agent MUST
    unlink the file it just wrote so we don't leak an orphan.
    """
    _seed_note(db, body="n", summary="s")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    async def fake_claude(*a, **kw):
        return {"text": json.dumps(_FAKE_DRAFT)}

    from mastisk.db import queries as q_mod
    real_update = q_mod.update_blog_post_done
    state: dict = {}

    def racy_update(conn, *, bp_id, slug, path, title, tags_json, model,
                    word_count, body_preview):
        # Race: DELETE lands just before the UPDATE commits.
        conn.execute(
            "UPDATE blog_posts SET deleted_at=CURRENT_TIMESTAMP WHERE id=?",
            (bp_id,),
        )
        state["file_path_rel"] = path
        return real_update(
            conn, bp_id=bp_id, slug=slug, path=path, title=title,
            tags_json=tags_json, model=model, word_count=word_count,
            body_preview=body_preview,
        )

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ), patch(
        "mastisk.agents.blog_writer.q.update_blog_post_done",
        side_effect=racy_update,
    ):
        asyncio.run(agent.run_once())

    # Row was tombstoned, status didn't flip to done, file was cleaned up.
    row = db.execute(
        "SELECT status, deleted_at, path FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["deleted_at"] is not None
    assert row["status"] != "done"
    # The file path we would have written is no longer on disk.
    assert state.get("file_path_rel")
    orphan = vault_tmp / state["file_path_rel"]
    assert not orphan.exists()


# ───── Claude non-JSON / bad-shape retry (Fix 4 + Fix 5) ─────


def test_claude_non_json_retries_once_then_falls_back(agent, db, vault_tmp):
    """Claude returns junk on the first call → one retry with suffix → still
    bad → Ollama fallback."""
    _seed_note(db, body="n", summary="s")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    claude_prompts: list[str] = []

    async def fake_claude(*, prompt, **kw):
        claude_prompts.append(prompt)
        return {"text": "not json at all"}

    async def fake_ollama(prompt, model, **kw):
        return {"text": json.dumps(_FAKE_DRAFT)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ), patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    # Claude was called twice: original + retry with suffix.
    assert len(claude_prompts) == 2
    assert "not valid JSON" in claude_prompts[1]
    row = db.execute(
        "SELECT status, model FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["model"] == "ollama"


def test_claude_returns_list_json_retries_then_ollama(agent, db, vault_tmp):
    """A JSON list at top level is valid JSON but not the expected shape —
    should trigger retry then fall to Ollama."""
    _seed_note(db, body="n", summary="s")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    attempts: list[int] = []

    async def fake_claude(**kw):
        attempts.append(1)
        return {"text": "[1, 2, 3]"}

    async def fake_ollama(prompt, model, **kw):
        return {"text": json.dumps(_FAKE_DRAFT)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ), patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    assert len(attempts) == 2  # one retry
    row = db.execute(
        "SELECT model FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["model"] == "ollama"


@pytest.mark.parametrize("bad_body", [None, "", 123])
def test_claude_bad_body_md_retries_then_ollama(agent, db, vault_tmp, bad_body):
    """body_md null / empty string / non-string → retry then Ollama."""
    _seed_note(db, body="n", summary="s")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    attempts: list[int] = []
    payload = {"title": "t", "tags": [], "body_md": bad_body}

    async def fake_claude(**kw):
        attempts.append(1)
        return {"text": json.dumps(payload)}

    async def fake_ollama(prompt, model, **kw):
        return {"text": json.dumps(_FAKE_DRAFT)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ), patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    # Retry fires once, then Ollama takes over.
    assert len(attempts) == 2
    row = db.execute(
        "SELECT status, model FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["status"] == "done"
    assert row["model"] == "ollama"


def test_claude_transport_error_skips_retry(agent, db, vault_tmp):
    """Transport errors (timeouts, non-zero exits) fall straight to Ollama
    without the JSON-shape retry."""
    _seed_note(db, body="n", summary="s")
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    claude_calls: list[int] = []

    async def fake_claude(**kw):
        claude_calls.append(1)
        raise RuntimeError("subprocess died")

    async def fake_ollama(prompt, model, **kw):
        return {"text": json.dumps(_FAKE_DRAFT)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ), patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    # Only one Claude attempt — transport error breaks the loop.
    assert len(claude_calls) == 1


# ───── empty rerank (Fix 6) ─────


def test_empty_rerank_falls_back_to_keyword(agent, db, vault_tmp):
    """``{"scored": []}`` is a valid-JSON but useless response — the caller
    should fall back to keyword ordering rather than draft with zero sources."""
    # Note b (older) has the matching keyword in its summary; note a (more
    # recent) does not. Without rerank, the keyword-overlap pass should put
    # b above a despite a being newer, since keyword score outranks ts.
    _seed_note(db, body="unrelated text", summary="unrelated topic",
               days_ago=0, slug="a")
    b_id = _seed_note(db, body="body about throughline keyword",
                      summary="throughline summary", days_ago=1, slug="b")
    bp_id = _seed_blog_post(db, theme="throughline")
    _enqueue(bp_id)

    async def empty_rerank(prompt, model, **kw):
        return {"text": '{"scored": []}'}

    async def fake_claude(**kw):
        return {"text": json.dumps({
            "title": "t", "tags": [], "body_md": "x [source 1]",
        })}

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=empty_rerank,
    ), patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    # Draft still landed with at least one source.
    row = db.execute(
        "SELECT status FROM blog_posts WHERE id=?", (bp_id,),
    ).fetchone()
    assert row["status"] == "done"
    # Empty rerank → fall back to keyword-pass order. Note b has the
    # matching keyword, so it lands at rank 1 even though a is more recent.
    rank1 = db.execute(
        "SELECT kind, ref FROM blog_post_sources WHERE blog_post_id=? AND rank=1",
        (bp_id,),
    ).fetchone()
    assert rank1["kind"] == "note"
    assert int(rank1["ref"]) == b_id


# ───── Ollama prompt re-truncation (test gap) ─────


def test_ollama_fallback_prompt_within_char_limit(agent, db, vault_tmp):
    """On Claude failure, the prompt passed to run_ollama must be ≤
    ollama_prompt_char_limit.
    """
    # Seed a few sources with large bodies so the Claude prompt would blow
    # past the tighter Ollama budget.
    big = "x " * 5000
    _seed_note(db, body=big, summary="big summary 1", slug="big1")
    _seed_note(db, body=big, summary="big summary 2", slug="big2", days_ago=1)
    _seed_note(db, body=big, summary="big summary 3", slug="big3", days_ago=2)
    bp_id = _seed_blog_post(db)
    _enqueue(bp_id)

    captured: dict[str, str] = {}

    async def fake_claude(**kw):
        raise RuntimeError("down")

    async def fake_ollama(prompt, model, **kw):
        captured["prompt"] = prompt
        return {"text": json.dumps(_FAKE_DRAFT)}

    with patch(
        "mastisk.agents.blog_writer.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ), patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    from mastisk.settings import get_settings
    limit = get_settings().blog.ollama_prompt_char_limit
    assert "prompt" in captured
    assert len(captured["prompt"]) <= limit


# ───── per-source halving + tail-drop (test gap) ─────


def test_render_prompt_halves_cap_before_dropping_items(agent):
    """When an oversized source would blow the budget, halve per_source_cap
    down to the floor before dropping items from the tail."""
    from mastisk.agents.blog_writer import BlogWriter

    # 3 items; each body long enough that at the default cap the prompt
    # exceeds the budget. Floor = min_per_source_chars.
    big = "x" * 10000
    ranked = [
        {"kind": "note", "ref": i, "slug": f"s{i}", "title": None,
         "summary": "", "body": big, "ts": "2026-04-22"}
        for i in range(3)
    ]
    w = BlogWriter()
    # Tiny budget that forces halving but still fits all 3 at the floor.
    small_budget = 3000
    out = w._render_prompt(
        theme="", ranked=ranked,
        char_budget=small_budget, per_source_cap=10000,
    )
    # Prompt fits within the budget.
    assert len(out) <= small_budget
    # All three sources are represented (no tail-drop needed at this budget).
    assert out.count("### Source") == 3


def test_render_prompt_drops_tail_when_floor_still_oversized(agent):
    """Once we're at the floor and still over budget, start dropping items
    from the tail."""
    from mastisk.agents.blog_writer import BlogWriter
    from mastisk.settings import get_settings
    floor = get_settings().blog.min_per_source_chars

    big = "x" * 5000
    ranked = [
        {"kind": "note", "ref": i, "slug": f"s{i}", "title": None,
         "summary": "", "body": big, "ts": "2026-04-22"}
        for i in range(10)
    ]
    w = BlogWriter()
    # Budget too tight for 10 items even at the floor.
    tight_budget = floor * 5
    out = w._render_prompt(
        theme="", ranked=ranked,
        char_budget=tight_budget, per_source_cap=10000,
    )
    # Fewer than 10 sources survived — some were tail-dropped.
    assert out.count("### Source") < 10


# ───── regenerate txn atomicity (Fix 3) ─────


def test_regenerate_rolls_back_on_reset_failure(db, vault_tmp):
    """If reset_blog_post_for_regenerate raises mid-txn, the
    delete_blog_post_sources should be rolled back too — atomicity."""
    from fastapi.testclient import TestClient

    from mastisk.app import create_app
    from mastisk.db.queries import (
        create_blog_post,
        insert_blog_post_source,
        update_blog_post_done,
    )
    from mastisk.settings import reload_settings
    reload_settings()

    bp_id = create_blog_post(db, theme="t", window_days=14)
    update_blog_post_done(
        db, bp_id=bp_id, slug="s", path="p", title="T", tags_json="[]",
        model="claude", word_count=10, body_preview="pre",
    )
    insert_blog_post_source(
        db, blog_post_id=bp_id, kind="note", ref="1", rank=1, used=True,
    )
    n_before = db.execute(
        "SELECT COUNT(*) AS c FROM blog_post_sources WHERE blog_post_id=?",
        (bp_id,),
    ).fetchone()["c"]
    assert n_before == 1

    def boom(conn, bp_id):
        raise RuntimeError("simulated crash")

    app = create_app()
    # raise_server_exceptions=False so a mid-handler exception surfaces as
    # a 500 rather than re-raising out of the client call.
    with TestClient(app, raise_server_exceptions=False) as client, patch(
        "mastisk.routes.blog_route.q.reset_blog_post_for_regenerate",
        side_effect=boom,
    ):
        r = client.post(f"/api/blog-posts/{bp_id}/regenerate")
        assert r.status_code == 500

    # Sources should NOT have been deleted — the txn rolled back.
    n_after = db.execute(
        "SELECT COUNT(*) AS c FROM blog_post_sources WHERE blog_post_id=?",
        (bp_id,),
    ).fetchone()["c"]
    assert n_after == 1


# ───── rerank scoring + personal-evidence boost ─────


def _candidate(
    *, kind: str, ref: int, summary: str = "", body: str = "",
    ts: str = "2026-04-22T00:00:00", origin: str | None = None,
) -> dict:
    """Bare-minimum candidate dict for unit-testing _rank/_llm_rerank."""
    c: dict = {
        "kind": kind, "ref": ref, "slug": f"s{ref}", "title": None,
        "summary": summary, "body": body, "ts": ts, "origin": origin,
    }
    return c


def test_rerank_filters_below_relevance_threshold():
    """Candidates whose boosted score falls below min_relevance_score get
    dropped from the final ranking."""
    from mastisk.agents.blog_writer import BlogWriter

    # Two articles → multiplier 1.0, so raw score == boosted score.
    keep = _candidate(kind="article", ref=1, summary="theme stuff")
    drop = _candidate(kind="article", ref=2, summary="other")
    candidates = [keep, drop]

    async def fake_rerank(self, cands, *, theme):
        # Return both, but with one score above and one below the threshold.
        return [(keep, 0.5), (drop, 0.2)]

    with patch.object(BlogWriter, "_llm_rerank", new=fake_rerank):
        ranked = asyncio.run(BlogWriter()._rank(candidates, theme="theme"))

    refs = [c["ref"] for c in ranked]
    assert refs == [1]


def test_personal_evidence_boost_reorders():
    """A note with raw 0.5 (boosted 0.8) outranks an article with raw 0.7
    (boosted 0.7)."""
    from mastisk.agents.blog_writer import BlogWriter

    note = _candidate(kind="note", ref=1, summary="my note")
    article = _candidate(kind="article", ref=2, summary="external article")
    # Pre-rank pass receives both. Mocked rerank returns both.
    candidates = [note, article]

    async def fake_rerank(self, cands, *, theme):
        return [(note, 0.5), (article, 0.7)]

    with patch.object(BlogWriter, "_llm_rerank", new=fake_rerank):
        ranked = asyncio.run(BlogWriter()._rank(candidates, theme="theme"))

    refs = [c["ref"] for c in ranked]
    # Note boosted 0.5*1.6=0.8 beats article 0.7*1.0=0.7.
    assert refs == [1, 2]


def test_repo_ideator_origin_is_boosted():
    """A repo_ideator-tagged note gets the SAME 1.6x multiplier as a plain
    note — both are personal evidence — so origin doesn't subtly tilt the
    score either direction.

    Real differential: same raw score for both notes, plus an article with a
    higher raw score that would beat the unboosted notes but loses to the
    boosted ones. Both notes must land above the article, AND their relative
    order must follow ts DESC tiebreak (proving the boost is identical, not
    subtly different by origin)."""
    from mastisk.agents.blog_writer import BlogWriter

    plain_note = _candidate(
        kind="note", ref=1, summary="plain user note",
        ts="2026-04-22T00:00:00",  # newer
    )
    repo_note = _candidate(
        kind="note", ref=2, summary="repo-derived", origin="repo_ideator",
        ts="2026-04-21T00:00:00",  # older
    )
    # Article raw 0.7 would beat unboosted notes (0.6) but loses to boosted
    # (0.6 * 1.6 = 0.96).
    article = _candidate(
        kind="article", ref=3, summary="article", ts="2026-04-23T00:00:00",
    )
    candidates = [plain_note, repo_note, article]

    async def fake_rerank(self, cands, *, theme):
        # Same raw score for both notes; article higher raw score.
        return [(plain_note, 0.6), (repo_note, 0.6), (article, 0.7)]

    with patch.object(BlogWriter, "_llm_rerank", new=fake_rerank):
        ranked = asyncio.run(BlogWriter()._rank(candidates, theme="theme"))

    refs = [c["ref"] for c in ranked]
    # Both notes (boosted to 0.96) must beat article (0.7).
    assert refs[0] in (1, 2)
    assert refs[1] in (1, 2)
    assert refs[2] == 3
    # The boost is identical for both notes — relative order is ts DESC, so
    # plain_note (newer) comes before repo_note (older). If the boost were
    # subtly different by origin, this would flip.
    assert refs == [1, 2, 3]


def test_rerank_validation_rejects_missing_scored_key():
    """A response that omits the 'scored' key should fail validation and
    return None."""
    from mastisk.agents.blog_writer import BlogWriter

    async def bad_rerank(prompt, model, **kw):
        return {"text": '{"foo": 1}'}

    candidates = [_candidate(kind="note", ref=1, summary="s")]

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=bad_rerank,
    ):
        result = asyncio.run(
            BlogWriter()._llm_rerank(candidates, theme="theme"),
        )
    assert result is None


def test_rerank_validation_rejects_non_numeric_score():
    """A response with a non-numeric score (string 'high') should fail
    validation and return None."""
    from mastisk.agents.blog_writer import BlogWriter

    async def bad_rerank(prompt, model, **kw):
        return {"text": '{"scored": [{"i": 0, "score": "high"}]}'}

    candidates = [_candidate(kind="note", ref=1, summary="s")]

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=bad_rerank,
    ):
        result = asyncio.run(
            BlogWriter()._llm_rerank(candidates, theme="theme"),
        )
    assert result is None


def test_rerank_failure_falls_back_unboosted():
    """When _llm_rerank returns None, _rank returns the keyword-order
    pre_ranked list AS-IS — no boost, no threshold filter applied."""
    from mastisk.agents.blog_writer import BlogWriter

    # Articles only; if the boost/threshold path were taken these would all
    # be at multiplier 1.0 and none would have a "score" attached. Build the
    # input so we can assert the fallback returns the raw candidates.
    a = _candidate(kind="article", ref=1, summary="theme aligned",
                   ts="2026-04-22T00:00:00")
    b = _candidate(kind="article", ref=2, summary="theme aligned too",
                   ts="2026-04-21T00:00:00")
    candidates = [a, b]

    async def fail_rerank(self, cands, *, theme):
        return None

    with patch.object(BlogWriter, "_llm_rerank", new=fail_rerank):
        ranked = asyncio.run(BlogWriter()._rank(candidates, theme="theme"))

    # Same objects, in the keyword/recency order produced by the pre-rank
    # pass (both have identical keyword scores, so they're sorted by ts DESC).
    assert ranked == [a, b]
    # The candidate dicts themselves are untouched (no score field added).
    for c in ranked:
        assert "score" not in c


def test_rerank_all_low_scores_falls_back_to_pre_ranked():
    """When every reranked entry's boosted score is below min_relevance_score,
    survivors is empty and _rank must fall back to the keyword pre-rank rather
    than return [] (which would draft from no sources)."""
    from mastisk.agents.blog_writer import BlogWriter

    a = _candidate(kind="article", ref=1, summary="theme one",
                   ts="2026-04-22T00:00:00")
    b = _candidate(kind="article", ref=2, summary="theme two",
                   ts="2026-04-21T00:00:00")
    c = _candidate(kind="article", ref=3, summary="theme three",
                   ts="2026-04-20T00:00:00")
    candidates = [a, b, c]

    async def low_score_rerank(self, cands, *, theme):
        # Articles → multiplier 1.0, so boosted == raw. All below 0.4.
        return [(a, 0.05), (b, 0.2), (c, 0.3)]

    with patch.object(BlogWriter, "_llm_rerank", new=low_score_rerank):
        ranked = asyncio.run(BlogWriter()._rank(candidates, theme="theme one"))

    # Fallback returns pre_ranked AS-IS — non-empty, in keyword/recency order.
    assert len(ranked) == 3
    # All three input candidates have the same keyword overlap with "theme one"
    # (token "theme" matches each), so they tiebreak by ts DESC: a, b, c.
    refs = [r["ref"] for r in ranked]
    assert refs == [1, 2, 3]


def test_rerank_rejects_nan_score():
    """NaN is not a finite float — validator rejects it.

    Python's json.loads accepts the bare ``NaN`` literal by default (cpython
    extension), so a malicious or buggy LLM response can slip a non-finite
    value past the basic isinstance check.
    """
    from mastisk.agents.blog_writer import BlogWriter

    async def nan_rerank(prompt, model, **kw):
        return {"text": '{"scored": [{"i": 0, "score": NaN}]}'}

    candidates = [_candidate(kind="note", ref=1, summary="s")]

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=nan_rerank,
    ):
        result = asyncio.run(
            BlogWriter()._llm_rerank(candidates, theme="theme"),
        )
    assert result is None


def test_rerank_rejects_infinity_score():
    """Infinity is not a finite float — validator rejects it."""
    from mastisk.agents.blog_writer import BlogWriter

    async def inf_rerank(prompt, model, **kw):
        return {"text": '{"scored": [{"i": 0, "score": Infinity}]}'}

    candidates = [_candidate(kind="note", ref=1, summary="s")]

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=inf_rerank,
    ):
        result = asyncio.run(
            BlogWriter()._llm_rerank(candidates, theme="theme"),
        )
    assert result is None


def test_rerank_rejects_negative_score():
    """Score below 0 violates the [0,1] contract — validator rejects it."""
    from mastisk.agents.blog_writer import BlogWriter

    async def neg_rerank(prompt, model, **kw):
        return {"text": '{"scored": [{"i": 0, "score": -0.5}]}'}

    candidates = [_candidate(kind="note", ref=1, summary="s")]

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=neg_rerank,
    ):
        result = asyncio.run(
            BlogWriter()._llm_rerank(candidates, theme="theme"),
        )
    assert result is None


def test_rerank_rejects_score_above_one():
    """Score above 1 violates the [0,1] contract — validator rejects it."""
    from mastisk.agents.blog_writer import BlogWriter

    async def high_rerank(prompt, model, **kw):
        return {"text": '{"scored": [{"i": 0, "score": 1.5}]}'}

    candidates = [_candidate(kind="note", ref=1, summary="s")]

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=high_rerank,
    ):
        result = asyncio.run(
            BlogWriter()._llm_rerank(candidates, theme="theme"),
        )
    assert result is None


def test_rerank_skips_duplicate_indices():
    """When the LLM returns the same i twice, first occurrence wins; the
    duplicate is silently dropped (not a validation failure)."""
    from mastisk.agents.blog_writer import BlogWriter

    async def dup_rerank(prompt, model, **kw):
        return {"text": '{"scored": [{"i": 0, "score": 0.8}, {"i": 0, "score": 0.2}]}'}

    candidates = [_candidate(kind="note", ref=1, summary="s")]

    with patch(
        "mastisk.agents.blog_writer.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=dup_rerank,
    ):
        result = asyncio.run(
            BlogWriter()._llm_rerank(candidates, theme="theme"),
        )
    assert result is not None
    assert len(result) == 1
    cand, score = result[0]
    assert cand is candidates[0]
    assert score == 0.8


def test_threshold_boundary_inclusive():
    """A note with raw 0.25 → boosted 0.4 (1.6×) exactly equals the default
    threshold and must be KEPT. The comparison is `>=`; pin this so a future
    drift to `>` would surface here.

    Worth noting: 0.25 * 1.6 == 0.4 in IEEE 754 (0.25 has an exact binary
    representation). If either constant changes, recompute the product.
    """
    from mastisk.agents.blog_writer import BlogWriter

    note = _candidate(kind="note", ref=1, summary="boundary note")
    candidates = [note]

    async def boundary_rerank(self, cands, *, theme):
        return [(note, 0.25)]

    with patch.object(BlogWriter, "_llm_rerank", new=boundary_rerank):
        ranked = asyncio.run(BlogWriter()._rank(candidates, theme="theme"))

    refs = [c["ref"] for c in ranked]
    assert refs == [1]
