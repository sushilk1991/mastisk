"""OpinionGapMiner agent tests. Claude is mocked so no subprocess calls fire.

See src/mastisk/agents/opinion_gap_miner.py.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest


# ───── seed helpers ─────


def _seed_note(
    db,
    *,
    body: str,
    summary: str = "a summary",
    classification: str = "idea",
    hours_ago: int = 4,
    slug: str | None = None,
) -> int:
    """Insert a classified note in the given hour-window."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=hours_ago)).isoformat(sep=" ", timespec="seconds")
    slug = slug or f"n-{abs(hash(body)) % 100000}"
    db.execute(
        """INSERT INTO notes (slug, path, body, body_sha256, source, created_at,
                              classified_at, classification, summary, tags_json)
           VALUES (?, ?, ?, 'h', 'pwa', ?, ?, ?, ?, '[]')""",
        (
            slug,
            f"_notes/inbox/{slug}.md",
            body,
            ts,
            ts,
            classification,
            summary,
        ),
    )
    return db.execute(
        "SELECT id FROM notes WHERE slug = ?", (slug,),
    ).fetchone()["id"]


def _seed_article(
    db,
    *,
    article_id: str,
    kind: str = "Source",
    title: str = "An article",
    summary: str = "the summary",
    body_md: str = "article body prose",
    hours_ago: int = 4,
) -> None:
    """Insert an article with explicit updated_at."""
    now = datetime.now(timezone.utc)
    ts = (now - timedelta(hours=hours_ago)).isoformat(sep=" ", timespec="seconds")
    db.execute(
        """INSERT INTO articles
             (id, kind, title, slug, body_md, summary, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (article_id, kind, title, article_id, body_md, summary, ts, ts),
    )


@pytest.fixture
def agent(db, vault_tmp, data_tmp):
    from mastisk.paths import ensure_dirs
    ensure_dirs()
    from mastisk.agents.opinion_gap_miner import OpinionGapMiner
    return OpinionGapMiner()


# ───── cadence ─────


def test_run_once_skips_within_cadence(agent, db):
    """An 'opinion' row inside the 7-day cadence window should suppress a
    fresh generation. Use 24h-old to be well within the 168h window."""
    db.execute(
        """INSERT INTO topic_suggestions (kind, title, hook, source_refs_json,
                                          created_at)
           VALUES ('opinion', 'old-opinion-topic', 'hook',
                   '[]', datetime('now', '-24 hours'))"""
    )

    async def never_called(*args, **kwargs):  # pragma: no cover - asserts no call
        raise AssertionError("Claude should not be called within cadence")

    with patch(
        "mastisk.agents.opinion_gap_miner.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=never_called,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions WHERE kind='opinion'",
    ).fetchone()["c"]
    assert n == 1


def test_cadence_guard_ignores_daily_kind(agent, db):
    """A 'daily' row from topic_suggester must NOT block opinion_gap_miner.
    The cadence guard reads kind='opinion' only — daily rows are a different
    agent's territory."""
    # A very recent 'daily' row should not block opinion-gap generation.
    db.execute(
        """INSERT INTO topic_suggestions (kind, title, hook, source_refs_json,
                                          created_at)
           VALUES ('daily', 'recent daily topic', 'hook',
                   '[]', datetime('now', '-1 hours'))"""
    )
    note_id = _seed_note(
        db,
        body="i think the witnessing approach beats postmortems",
        summary="i think witnessing beats postmortems for production debugging",
    )
    _seed_article(
        db,
        article_id="art-witness",
        kind="Source",
        title="Why postmortems are essential",
        summary="postmortems are essential for production debugging",
        body_md="prose about production debugging postmortems",
    )

    payload = {
        "topics": [
            {
                "title": "Why witnessing beats postmortems",
                "hook": "I think witnessing > postmortems. The article argues postmortems are essential. We disagree.",
                "angle": "Argue: shipping witnesses beats writing postmortems.",
                "user_refs": [{"kind": "note", "ref": note_id}],
                "world_refs": [{"kind": "article", "ref": "art-witness"}],
            }
        ]
    }

    async def fake_claude(prompt, **kwargs):
        return {"text": json.dumps(payload)}

    with patch(
        "mastisk.agents.opinion_gap_miner.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions WHERE kind='opinion'",
    ).fetchone()["c"]
    assert n == 1


# ───── empty signal ─────


def test_run_once_no_assertive_user_signal_writes_zero(agent, db):
    """Notes exist in window, but none contain assertion markers → no LLM
    call, no rows."""
    _seed_note(
        db,
        body="just a neutral observation about agents",
        summary="agents observation",
    )
    _seed_article(db, article_id="a1")  # world signal exists, user does not assert.

    async def never_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("Claude should not be called when no assertions exist")

    with patch(
        "mastisk.agents.opinion_gap_miner.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=never_called,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions",
    ).fetchone()["c"]
    assert n == 0


def test_run_once_no_world_signal_writes_zero(agent, db):
    """An assertive note exists but no in-window articles → no LLM call,
    no rows."""
    _seed_note(
        db,
        body="i think witnessing > postmortems",
        summary="i think witnessing beats postmortems",
    )

    async def never_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("Claude should not be called when world signal is empty")

    with patch(
        "mastisk.agents.opinion_gap_miner.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=never_called,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions",
    ).fetchone()["c"]
    assert n == 0


# ───── assertion-marker filter ─────


def test_assertion_marker_matching_is_case_insensitive(agent, db):
    """Notes whose body contains 'I think' / 'I THINK' / 'i think' all
    qualify; a note with no marker is filtered out. Pull list directly to
    keep the assertion-detector under unit-style scrutiny."""
    asserted_lower = _seed_note(
        db,
        body="i think this is the right call",
        summary="lower-case assertion summary",
        slug="lower",
    )
    asserted_upper = _seed_note(
        db,
        body="I THINK we shipped the right thing",
        summary="upper-case assertion summary",
        slug="upper",
    )
    asserted_mixed = _seed_note(
        db,
        body="I think the loop has converged",
        summary="mixed-case assertion summary",
        slug="mixed",
    )
    _seed_note(
        db,
        body="just a neutral observation today",
        summary="neutral observation",
        slug="neutral",
    )

    items = agent._pull_user_assertions()
    refs = {i["ref"] for i in items}
    assert asserted_lower in refs
    assert asserted_upper in refs
    assert asserted_mixed in refs
    # All three assertion notes qualified; the neutral one did not.
    assert len(items) == 3


def test_assertion_marker_in_summary_qualifies(agent, db):
    """If the *summary* (Claude-derived) carries the marker but body does
    not, the note still qualifies. Catches the case where the user's
    classifier output highlights the stake but the source body is terse."""
    note_id = _seed_note(
        db,
        body="terse text",
        summary="my take is that pod_available is right",
        slug="summary-marker",
    )
    items = agent._pull_user_assertions()
    refs = {i["ref"] for i in items}
    assert note_id in refs


def test_third_person_action_verbs_are_not_assertions(agent, db):
    """Single-word past-tense verbs (shipped, replaced, killed, etc.) only
    qualify when anchored to first-person ('I shipped', 'we replaced').
    A note summarising someone else's action — 'Anthropic shipped Claude
    Code' — must NOT trigger the assertion filter, otherwise news-summary
    notes get treated as the user's stake."""
    _seed_note(
        db,
        body="Anthropic shipped Claude Code last month and the team replaced the cache.",
        summary="news roundup: Anthropic shipped Claude Code; team replaced cache",
        slug="third-person-shipped",
    )
    items = agent._pull_user_assertions()
    assert items == [], (
        "third-person 'shipped'/'replaced' must not pass the assertion filter"
    )


def test_first_person_action_verbs_qualify(agent, db):
    """Anchored first-person commits ('I shipped X', 'we replaced Y') are
    the user's own stake — these MUST pass the assertion filter."""
    note_id = _seed_note(
        db,
        body="I shipped the pod_available flag this week and we replaced the cache.",
        summary="shipped pod_available; we replaced cache",
        slug="first-person-shipped",
    )
    items = agent._pull_user_assertions()
    refs = {i["ref"] for i in items}
    assert note_id in refs


def test_assertion_marker_word_boundary_avoids_substring_collisions(agent, db):
    """Phrasal markers must respect word boundaries: 'in fact' must NOT
    fire on 'in factories', and 'actually' must NOT fire on 'actuallya'.
    Without word boundaries the filter would leak into prefix/suffix
    collisions and produce noise weekly."""
    _seed_note(
        db,
        body="In factories the build broke, actuallya quirky note.",
        summary="factory build broke actuallya note",
        slug="word-boundary-collisions",
    )
    items = agent._pull_user_assertions()
    assert items == [], (
        "word-boundary regex must reject 'in factories' / 'actuallya' substring matches"
    )


def test_assertion_marker_beyond_body_window_does_not_qualify(agent, db):
    """The body scan caps at the first 1k chars. A marker placed at char
    1500 must NOT qualify the note — pins the documented scan window."""
    padding = "x " * 800   # ~1600 chars of filler before the marker
    _seed_note(
        db,
        body=f"{padding}i think this is the real take",
        summary="terse summary with no markers",
        slug="marker-out-of-window",
    )
    items = agent._pull_user_assertions()
    assert items == []


# ───── happy path ─────


def test_run_once_writes_opinion_topics_on_real_conflict(agent, db):
    """An assertive note + a topical Source article should produce a
    persisted opinion-gap topic with kind='opinion' and correct refs."""
    note_id = _seed_note(
        db,
        body="i think shipping witnesses beats writing postmortems for production",
        summary="i think witnesses beat postmortems for production debugging",
    )
    _seed_article(
        db,
        article_id="art-postmortems",
        kind="Source",
        title="Why postmortems are essential for production",
        summary="postmortems are essential for production debugging witnesses",
        body_md="prose about production debugging postmortems",
    )

    payload = {
        "topics": [
            {
                "title": "Why witnessing beats postmortems for production",
                "hook": "I argued shipping witnesses beats postmortems. The article claims postmortems are essential. We disagree on which scales.",
                "angle": "Counter: witnesses give live signal, postmortems give dead signal.",
                "user_refs": [{"kind": "note", "ref": note_id}],
                "world_refs": [{"kind": "article", "ref": "art-postmortems"}],
            }
        ]
    }

    async def fake_claude(prompt, **kwargs):
        return {"text": json.dumps(payload)}

    with patch(
        "mastisk.agents.opinion_gap_miner.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    rows = db.execute(
        "SELECT * FROM topic_suggestions WHERE kind='opinion'",
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Why witnessing beats postmortems for production"
    assert "witnesses" in row["hook"]
    assert row["angle"]
    refs = json.loads(row["source_refs_json"])
    assert {"kind": "note", "ref": note_id} in refs
    assert {"kind": "article", "ref": "art-postmortems"} in refs
    # Feed row was emitted with verb='surfaced' kind='opinion-gap'.
    feed = db.execute(
        "SELECT * FROM feed WHERE agent='opinion_gap_miner'",
    ).fetchall()
    assert len(feed) == 1
    assert feed[0]["verb"] == "surfaced"
    assert feed[0]["kind"] == "opinion-gap"


# ───── validation ─────


def test_validator_skips_malformed_persists_survivors(agent, db, caplog):
    """A 2-topic payload with one bad → 1 valid row written, 1 warning
    logged for the bad sibling."""
    note_id = _seed_note(
        db,
        body="i think witnesses are better than postmortems for production",
        summary="i think witnesses beat postmortems",
    )
    _seed_article(
        db,
        article_id="art-real",
        kind="Source",
        summary="postmortems are essential for production witnesses",
        body_md="postmortems witnesses production prose",
    )

    payload = {
        "topics": [
            {
                "title": "Made-up ref",
                "hook": "Cites a note that doesn't exist.",
                "angle": "n/a",
                "user_refs": [{"kind": "note", "ref": 999999}],
                "world_refs": [{"kind": "article", "ref": "art-real"}],
            },
            {
                "title": "Valid sibling",
                "hook": "Real refs both ways.",
                "angle": "ok",
                "user_refs": [{"kind": "note", "ref": note_id}],
                "world_refs": [{"kind": "article", "ref": "art-real"}],
            },
        ]
    }

    async def fake_claude(prompt, **kwargs):
        return {"text": json.dumps(payload)}

    with caplog.at_level("WARNING"), patch(
        "mastisk.agents.opinion_gap_miner.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    rows = db.execute(
        "SELECT title FROM topic_suggestions WHERE kind='opinion'",
    ).fetchall()
    titles = {r["title"] for r in rows}
    assert titles == {"Valid sibling"}
    assert any(
        "opinion_gap_miner" in record.name and "unknown" in record.message
        for record in caplog.records
    )


def test_zero_topics_response_does_not_block_next_week(agent, db):
    """LLM returns {"topics":[]} → no rows written, including no cadence-
    blocking row. The cadence guard reads written 'opinion' rows only;
    a zero-topics week leaves nothing behind, so next week's tick fires
    normally."""
    _seed_note(
        db,
        body="i think there is no real conflict here in this seed",
        summary="i think there is no conflict",
    )
    _seed_article(
        db,
        article_id="art-zero",
        kind="Source",
        summary="conflict prose witnessed",
        body_md="some prose conflict witnessed",
    )

    async def fake_claude(prompt, **kwargs):
        return {"text": json.dumps({"topics": []})}

    with patch(
        "mastisk.agents.opinion_gap_miner.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=fake_claude,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions WHERE kind='opinion'",
    ).fetchone()["c"]
    assert n == 0
    # No row at all means the cadence guard finds nothing on next tick.
    n_any = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions",
    ).fetchone()["c"]
    assert n_any == 0


# ───── source-only world filter ─────


def test_only_source_kind_articles_pulled(agent, db):
    """Synthesis articles are the user's own cross-source claims, not
    external takes. Only 'Source' articles should make it into the world
    pool. Pin the design decision: feed Synthesis + Source articles, only
    Source ends up in pairs."""
    _seed_article(
        db,
        article_id="art-source",
        kind="Source",
        summary="the source claim summary",
        body_md="source body prose",
    )
    _seed_article(
        db,
        article_id="art-synthesis",
        kind="Synthesis",
        summary="the synthesis claim summary",
        body_md="synthesis body prose",
    )

    items = agent._pull_world_claims()
    refs = {i["ref"] for i in items}
    assert "art-source" in refs
    assert "art-synthesis" not in refs
    assert len(items) == 1
