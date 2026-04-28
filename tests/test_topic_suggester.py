"""TopicSuggester agent tests. Ollama is mocked so no network calls fire.

See src/mastisk/agents/topic_suggester.py.
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
    from mastisk.agents.topic_suggester import TopicSuggester
    return TopicSuggester()


# ───── cadence ─────


def test_run_once_skips_within_cadence(agent, db):
    """A 'daily' row 2h old should suppress a fresh generation."""
    db.execute(
        """INSERT INTO topic_suggestions (kind, title, hook, source_refs_json,
                                          created_at)
           VALUES ('daily', 'old-topic', 'hook',
                   '[]', datetime('now', '-2 hours'))"""
    )

    async def never_called(*args, **kwargs):  # pragma: no cover - asserts no call
        raise AssertionError("Ollama should not be called within cadence")

    with patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=never_called,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions",
    ).fetchone()["c"]
    assert n == 1


# ───── empty signal ─────


def test_run_once_no_user_signal_writes_zero_rows(agent, db):
    """No notes → no LLM call, no rows."""
    _seed_article(db, article_id="a1")  # world signal exists, user does not.

    async def never_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("Ollama should not be called when user signal is empty")

    with patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=never_called,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions",
    ).fetchone()["c"]
    assert n == 0


def test_run_once_no_world_signal_writes_zero_rows(agent, db):
    """Notes present but no in-window articles → no LLM call, no rows."""
    _seed_note(db, body="hello world about agents", summary="agents in code")

    async def never_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("Ollama should not be called when world signal is empty")

    with patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=never_called,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions",
    ).fetchone()["c"]
    assert n == 0


# ───── happy path ─────


def test_run_once_writes_suggestions_on_real_crossing(agent, db):
    """A note + Source article that share tokens should produce a suggestion."""
    note_id = _seed_note(
        db,
        body="we shipped a pod_available flag and it caught a regression",
        summary="pod_available flag caught regression",
    )
    _seed_article(
        db,
        article_id="art-witness",
        kind="Source",
        title="A regression caught by a flag",
        summary="pod_available flag caught regression in production",
        body_md="prose about production regression flags",
    )

    payload = {
        "topics": [
            {
                "title": "Why our pod_available flag is a witness",
                "hook": "We shipped a flag; the world just published why those matter.",
                "angle": "Argue: shipping witnesses beats writing postmortems.",
                "user_refs": [{"kind": "note", "ref": note_id}],
                "world_refs": [{"kind": "article", "ref": "art-witness"}],
            }
        ]
    }

    async def fake_ollama(prompt, model, **kwargs):
        return {"text": json.dumps(payload)}

    with patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    rows = db.execute(
        "SELECT * FROM topic_suggestions WHERE kind='daily'",
    ).fetchall()
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Why our pod_available flag is a witness"
    assert "shipped a flag" in row["hook"]
    assert row["angle"]
    refs = json.loads(row["source_refs_json"])
    assert {"kind": "note", "ref": note_id} in refs
    assert {"kind": "article", "ref": "art-witness"} in refs
    # Feed row was emitted.
    feed = db.execute(
        "SELECT * FROM feed WHERE agent='topic_suggester' AND verb='suggested'",
    ).fetchall()
    assert len(feed) == 1
    assert feed[0]["kind"] == "topic"


# ───── validation ─────


def test_validation_rejects_invalid_topic_shape(agent, db, caplog):
    """A 2-topic payload where the first cites a non-existent ref and the
    second is valid: malformed one is dropped, valid one is persisted, and
    a warning is logged for the bad sibling."""
    note_id = _seed_note(
        db,
        body="agents writing about agents",
        summary="agent prose summary",
    )
    _seed_article(
        db,
        article_id="art-real",
        kind="Source",
        summary="agent prose summary in the world",
        body_md="agents agents agents",
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

    async def fake_ollama(prompt, model, **kwargs):
        return {"text": json.dumps(payload)}

    with caplog.at_level("WARNING"), patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    rows = db.execute(
        "SELECT title FROM topic_suggestions",
    ).fetchall()
    titles = {r["title"] for r in rows}
    assert titles == {"Valid sibling"}
    assert any(
        "topic_suggester" in record.name and "unknown" in record.message
        for record in caplog.records
    )


def test_validation_rejects_non_string_title(agent, db, caplog):
    """A 2-topic payload where the first has a non-string title and the
    second is valid: malformed one is dropped, valid one is persisted, and
    a warning is logged."""
    note_id = _seed_note(
        db,
        body="pod_available flag prose",
        summary="pod_available flag prose summary",
    )
    _seed_article(
        db,
        article_id="art-2",
        kind="Source",
        summary="pod_available flag witnessing",
        body_md="flags flags flags",
    )

    payload = {
        "topics": [
            {
                "title": 123,  # not a string — this topic is dropped
                "hook": "ok",
                "angle": "ok",
                "user_refs": [{"kind": "note", "ref": note_id}],
                "world_refs": [{"kind": "article", "ref": "art-2"}],
            },
            {
                "title": "Valid sibling title",
                "hook": "ok",
                "angle": "ok",
                "user_refs": [{"kind": "note", "ref": note_id}],
                "world_refs": [{"kind": "article", "ref": "art-2"}],
            },
        ]
    }

    async def fake_ollama(prompt, model, **kwargs):
        return {"text": json.dumps(payload)}

    with caplog.at_level("WARNING"), patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    rows = db.execute(
        "SELECT title FROM topic_suggestions",
    ).fetchall()
    titles = {r["title"] for r in rows}
    assert titles == {"Valid sibling title"}
    assert any(
        "topic_suggester" in record.name
        and "title" in record.message
        and "not a string" in record.message
        for record in caplog.records
    )


# ───── crossing scoring sanity ─────


def test_zero_crossings_writes_nothing(agent, db):
    """Note and article with no shared tokens → no LLM call, no rows."""
    _seed_note(db, body="quantum entanglement", summary="entanglement")
    _seed_article(
        db,
        article_id="art-x",
        summary="completely unrelated about gardens",
        body_md="garden tomatoes potato basil",
    )

    async def never_called(*args, **kwargs):  # pragma: no cover
        raise AssertionError("LLM should not be called when no crossings exist")

    with patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=never_called,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions",
    ).fetchone()["c"]
    assert n == 0


def test_synthesis_articles_count_as_world_signal(agent, db):
    """Synthesis articles (the agent's own claims) should be in the world pool."""
    note_id = _seed_note(
        db,
        body="we are building a daily-suggester agent",
        summary="daily suggester architecture",
    )
    _seed_article(
        db,
        article_id="art-syn",
        kind="Synthesis",
        summary="daily suggester architecture observation",
        body_md="agent suggester crossings prose",
    )

    payload = {
        "topics": [
            {
                "title": "When my agent thinks like the world",
                "hook": "I'm building a suggester; the synthesis layer just picked the same theme.",
                "angle": "Examine the loop closing on itself.",
                "user_refs": [{"kind": "note", "ref": note_id}],
                "world_refs": [{"kind": "article", "ref": "art-syn"}],
            }
        ]
    }

    async def fake_ollama(prompt, model, **kwargs):
        return {"text": json.dumps(payload)}

    with patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    rows = db.execute(
        "SELECT title FROM topic_suggestions",
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["title"] == "When my agent thinks like the world"


# ───── new coverage: malformed JSON, idempotency, cross-kind, all-bad ─────


def test_malformed_json_response_writes_zero_rows(agent, db, caplog):
    """LLM returns garbage, not JSON. Agent logs a warning, writes zero
    rows, AND does NOT write a cadence-blocking row — so the next 10-min
    tick can retry."""
    _seed_note(
        db,
        body="we shipped a pod_available flag",
        summary="pod_available flag",
    )
    _seed_article(
        db,
        article_id="art-malformed",
        summary="pod_available flag witnessed regression",
        body_md="flag prose",
    )

    async def fake_ollama(prompt, model, **kwargs):
        return {"text": "not json at all"}

    with caplog.at_level("WARNING"), patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions",
    ).fetchone()["c"]
    assert n == 0
    assert any(
        "topic_suggester" in record.name and "LLM call failed" in record.message
        for record in caplog.records
    )


def test_persist_is_idempotent_within_day(agent, db, caplog):
    """Calling _persist twice in the same day with kind='daily' AND the same
    title must keep only one row — the second insert silently no-ops via
    INSERT OR IGNORE on the (kind, date, title) unique index and logs a
    warning. Defends against multi-process races without capping legit
    1-2-topic-per-day output."""
    topic = {
        "title": "Same-day topic",
        "hook": "hook line",
        "angle": "angle line",
        "user_refs": [{"kind": "note", "ref": 1}],
        "world_refs": [{"kind": "article", "ref": "art-id"}],
    }

    agent._persist([topic])
    with caplog.at_level("WARNING"):
        agent._persist([topic])

    rows = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions WHERE kind='daily'",
    ).fetchone()["c"]
    assert rows == 1
    assert any(
        "topic_suggester" in record.name and "ignored" in record.message
        for record in caplog.records
    )


def test_persist_keeps_two_distinct_topics_in_one_batch(agent, db):
    """Two genuinely-different topics in a single batch (different titles)
    BOTH persist. This pins the design intent: 1-2 topics per day, not 1.
    The unique index keys on (kind, date, title), not (kind, date) alone."""
    topics = [
        {
            "title": "First topic",
            "hook": "First hook",
            "angle": "First angle",
            "user_refs": [{"kind": "note", "ref": 1}],
            "world_refs": [{"kind": "article", "ref": "a1"}],
        },
        {
            "title": "Second topic",
            "hook": "Second hook",
            "angle": "Second angle",
            "user_refs": [{"kind": "note", "ref": 2}],
            "world_refs": [{"kind": "article", "ref": "a2"}],
        },
    ]
    agent._persist(topics)
    rows = db.execute(
        "SELECT title FROM topic_suggestions WHERE kind='daily' ORDER BY id",
    ).fetchall()
    assert [r["title"] for r in rows] == ["First topic", "Second topic"]


def test_validator_rejects_cross_kind_confusion(agent, db, caplog):
    """A topic that claims kind='note' but uses an article id (a string ref
    that exists in the world pool) must be rejected. (kind, ref) tuple
    membership catches the cross-kind confusion that a flat id-set wouldn't."""
    note_id = _seed_note(
        db,
        body="we shipped a pod_available flag witnessed in production",
        summary="pod_available flag witnessed",
    )
    _seed_article(
        db,
        article_id="art-cross",
        summary="pod_available flag witnessed regression",
        body_md="flag prose witnessed",
    )

    payload = {
        "topics": [
            {
                "title": "Cross-kind confusion",
                "hook": "Claims a note ref that's actually an article id.",
                "angle": "n/a",
                # Article id reused as a "note" kind — must be rejected.
                "user_refs": [{"kind": "note", "ref": "art-cross"}],
                "world_refs": [{"kind": "article", "ref": "art-cross"}],
            },
            {
                "title": "Honest sibling",
                "hook": "Real refs both ways.",
                "angle": "ok",
                "user_refs": [{"kind": "note", "ref": note_id}],
                "world_refs": [{"kind": "article", "ref": "art-cross"}],
            },
        ]
    }

    async def fake_ollama(prompt, model, **kwargs):
        return {"text": json.dumps(payload)}

    with caplog.at_level("WARNING"), patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    rows = db.execute(
        "SELECT title FROM topic_suggestions",
    ).fetchall()
    titles = {r["title"] for r in rows}
    assert titles == {"Honest sibling"}


def test_validator_returns_empty_when_all_topics_invalid(agent, db):
    """When every topic in the LLM payload is malformed, the validator
    returns an empty list and the caller writes zero rows — without
    crashing."""
    _seed_note(
        db,
        body="agents writing about agents",
        summary="agent prose summary",
    )
    _seed_article(
        db,
        article_id="art-allbad",
        kind="Source",
        summary="agent prose summary in the world",
        body_md="agents agents agents",
    )

    payload = {
        "topics": [
            {
                "title": 42,  # not a string
                "hook": "ok",
                "angle": "ok",
                "user_refs": [],
                "world_refs": [{"kind": "article", "ref": "art-allbad"}],
            },
            {
                "title": "OK title",
                "hook": "ok",
                "angle": "ok",
                # Unknown ref — rejected by tuple-membership check.
                "user_refs": [{"kind": "note", "ref": 999999}],
                "world_refs": [{"kind": "article", "ref": "art-allbad"}],
            },
        ]
    }

    async def fake_ollama(prompt, model, **kwargs):
        return {"text": json.dumps(payload)}

    with patch(
        "mastisk.agents.topic_suggester.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=fake_ollama,
    ):
        asyncio.run(agent.run_once())

    n = db.execute(
        "SELECT COUNT(*) AS c FROM topic_suggestions",
    ).fetchone()["c"]
    assert n == 0
