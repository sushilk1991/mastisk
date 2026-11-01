"""Behavioral coverage for critic-gated publication and ordinary read surfaces."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from mastisk.agents.synthesizer import Synthesizer
from mastisk.db import queries as q
from mastisk.routes import ask, digest_route
from mastisk.services import digest_ranker

_PASS_SCORE = 4.0
_FAIL_SCORE = 2.0
_TITLE = "Evidence transport needs bounded retries"
_DRAFT_ID = "checked-synthesis"


@pytest.fixture
def client(db):
    """Exercise actual HTTP routes without starting unrelated scheduled agents."""
    app = FastAPI()
    app.include_router(digest_route.router, prefix="/api")
    with TestClient(app) as instance:
        yield instance


def _seed(db, article_id, kind="Source", day="2026-10-31"):
    """Create readable evidence so tests exercise ranking rather than the stub filter."""
    db.execute(
        """INSERT INTO articles (id, slug, kind, title, summary, body_md, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (article_id, article_id, kind, f"Evidence {article_id}", "Evidence transport",
         f"{article_id} evidence transport " * 100, f"{day} 12:00:00"),
    )


def _record_run(db, article_id, score):
    """Use the real run ledger, including same-second rows used for tie-breaking."""
    return q.record_synthesis_run(
        db, cluster_hash=article_id, source_article_ids=[], draft_article_id=article_id,
        eval_score=score, eval_rationale="Recorded critic result",
    )


def _draft():
    """Return a valid model payload; the gate must decide on quality, not JSON shape."""
    return {
        "id": _DRAFT_ID, "title": _TITLE, "summary": "Bound retries with evidence.",
        "confidence": 0.8,
        "sections": [{"h": "The thread", "body": "Evidence transport needs bounded retries. " * 60}],
        "related": [],
    }


def _model_reply(payload):
    return {"text": f"```json\n{json.dumps(payload)}\n```"}, "test"


@pytest.mark.asyncio
@pytest.mark.parametrize("score", [_FAIL_SCORE, None])
async def test_withhold_failed_draft(db, vault_tmp, monkeypatch, score):
    """A failed critic must leave audit evidence, not a readable wiki page."""
    from mastisk.bridges import intelligence

    members = ["source-one", "source-two", "source-three"]
    for article_id in members:
        _seed(db, article_id)
    critic = {"text": f"Unsupported connection.\nSCORE: {score}"}
    replies = [_model_reply(_draft()), (critic, "test")]
    if score is not None:
        replies.extend([_model_reply(_draft()), (critic, "test")])
    bridge = AsyncMock(side_effect=replies)
    monkeypatch.setattr(intelligence, "run_intelligence", bridge)
    agent = Synthesizer()

    result = await agent._synthesize_once(cluster_override=members)

    assert result is None
    assert db.execute("SELECT 1 FROM articles WHERE id = ?", (_DRAFT_ID,)).fetchone() is None
    assert not list((vault_tmp / "synthesis").glob("*.md"))
    run = dict(db.execute("SELECT * FROM synthesis_runs").fetchone())
    assert run["draft_article_id"] is None
    assert run["eval_score"] == score
    assert "Unsupported connection" in run["eval_rationale"]
    assert sorted(json.loads(run["source_article_ids"])) == sorted(members)
    assert db.execute("SELECT verb FROM feed WHERE agent='synthesizer'").fetchone()[0] == "withheld"

    # Recording a withheld attempt prevents spending again on an unchanged cluster.
    calls = bridge.await_count
    assert await agent._synthesize_once(cluster_override=members) is None
    assert bridge.await_count == calls


@pytest.mark.asyncio
async def test_publish_passing_draft(db, vault_tmp, monkeypatch):
    """A passing draft still reaches the wiki, run ledger, and vault."""
    from mastisk.bridges import intelligence

    members = ["source-one", "source-two", "source-three"]
    for article_id in members:
        _seed(db, article_id)
    bridge = AsyncMock(side_effect=[
        _model_reply(_draft()), ({"text": "Grounded thread.\nSCORE: 4"}, "test"),
    ])
    monkeypatch.setattr(intelligence, "run_intelligence", bridge)

    assert await Synthesizer()._synthesize_once(cluster_override=members) == _DRAFT_ID
    article = q.get_article(db, _DRAFT_ID)
    assert article["body_md"]
    assert list((vault_tmp / "synthesis").glob("*.md"))
    assert q.get_synthesis_run_for_article(db, _DRAFT_ID)["eval_score"] == _PASS_SCORE
    assert bridge.await_count == 2


@pytest.mark.asyncio
async def test_one_revision_only(db, monkeypatch):
    """Critic feedback gets one repair attempt; the repaired result is regraded."""
    from mastisk.bridges import intelligence

    bridge = AsyncMock(side_effect=[
        _model_reply(_draft()), ({"text": "Remove unsupported causal claim.\nSCORE: 2"}, "test"),
        _model_reply(_draft()), ({"text": "Now grounded.\nSCORE: 4"}, "test"),
    ])
    monkeypatch.setattr(intelligence, "run_intelligence", bridge)
    members = ["source-one", "source-two", "source-three"]
    for article_id in members:
        _seed(db, article_id)

    assert await Synthesizer()._synthesize_once(cluster_override=members) == _DRAFT_ID
    run = q.get_synthesis_run_for_article(db, _DRAFT_ID)
    assert run["eval_score"] == _PASS_SCORE
    assert "Now grounded" in run["eval_rationale"]
    assert bridge.await_count == 4
    revision_prompt = bridge.await_args_list[2].args[0]
    assert "Remove unsupported causal claim" in revision_prompt
    assert _TITLE in revision_prompt


@pytest.mark.asyncio
async def test_failed_revision_is_bounded(db, monkeypatch):
    """An unfixable draft must not start an unbounded self-review loop."""
    from mastisk.bridges import intelligence

    bridge = AsyncMock(side_effect=[
        _model_reply(_draft()), ({"text": "Unsupported.\nSCORE: 2"}, "test"),
        _model_reply(_draft()), ({"text": "Still unsupported.\nSCORE: 2"}, "test"),
    ])
    monkeypatch.setattr(intelligence, "run_intelligence", bridge)

    _data, score, _rationale = await Synthesizer()._draft_critic_loop([], [], [])

    assert score == _FAIL_SCORE
    assert bridge.await_count == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["critic", "revision"])
async def test_model_failure_withholds(db, monkeypatch, failure_stage):
    """Unavailable critics or failed repairs must not publish an earlier failed draft."""
    from mastisk.bridges import intelligence

    members = ["source-one", "source-two", "source-three"]
    for article_id in members:
        _seed(db, article_id)
    replies = [_model_reply(_draft())]
    if failure_stage == "revision":
        replies.append(({"text": "Unsupported.\nSCORE: 2"}, "test"))
    replies.append(TimeoutError("Model unavailable"))
    bridge = AsyncMock(side_effect=replies)
    monkeypatch.setattr(intelligence, "run_intelligence", bridge)

    assert await Synthesizer()._synthesize_once(cluster_override=members) is None
    assert q.get_article(db, _DRAFT_ID) is None
    run = db.execute("SELECT draft_article_id, eval_score FROM synthesis_runs").fetchone()
    assert run["draft_article_id"] is None
    expected = _FAIL_SCORE if failure_stage == "revision" else None
    assert run["eval_score"] == expected
    assert bridge.await_count == len(replies)


@pytest.mark.parametrize("text", [
    "SCORE: 6", "SCORE: 100", "SCORE: 0", "SCORE: 5.5",
    "SCORE: 4 trailing", "SCORE: nan", "SCORE: inf",
])
def test_invalid_score_fails_closed(text):
    """Malformed critic output must never be clamped into a passing score."""
    assert Synthesizer._parse_score(text) is None


def test_final_score_controls_gate():
    """Earlier score mentions must not override the critic's final verdict."""
    assert Synthesizer._parse_score("SCORE: 5\nCorrection:\nSCORE: 2") == _FAIL_SCORE


@pytest.mark.parametrize("score, verdict", [
    (_FAIL_SCORE, "pending"), (3.9, "pending"), (6.0, "pending"),
    (None, "pending"), (_PASS_SCORE, "reject"),
    (_PASS_SCORE, "newer-failure"), (None, "missing-run"),
])
def test_hidden_on_read_surfaces(client, db, score, verdict):
    """Legacy weak/rejected drafts cannot bypass the new write gate through reads."""
    _seed(db, "withheld-proof", "Synthesis", "2026-10-30")
    _seed(db, "source-proof")
    _seed(db, "future-withheld", "Synthesis", "2026-11-01")
    if verdict != "missing-run":
        run_id = _record_run(db, "withheld-proof", score)
        if verdict == "reject":
            q.mark_synthesis_rejected(db, run_id, "Unsupported")
        if verdict == "newer-failure":
            _record_run(db, "withheld-proof", _FAIL_SCORE)

    assert digest_ranker.rank_for_digest(db, "2026-10-30") == []
    assert client.get("/api/digest/calendar?year=2026&month=10").json()["active_dates"] == ["2026-10-31"]
    digest = client.get("/api/digest?date=2026-10-31").json()
    assert digest["prev_date"] is None
    assert digest["next_date"] is None
    assert digest["threads"][0]["article_id"] == "source-proof"
    assert "withheld-proof" not in {r["id"] for r in q.search_all(db, "Evidence", limit=100)}
    assert q.search_articles(db, "withheld-proof") == []
    sources = ask._wiki_sources(db, ask.AskRequest(question="Evidence", article_id="withheld-proof"))
    assert "withheld-proof" not in {r["id"] for r in sources}
    assert "withheld-proof" not in sources[0]["content"]
    # Explicit review stays possible through the article API/query layer.
    assert q.get_article(db, "withheld-proof")["body_md"]


@pytest.mark.parametrize("verdict", ["critic-pass", "human-accept"])
def test_visible_after_approval(client, db, verdict):
    """Keep passing drafts and explicit human decisions available on every read path."""
    _seed(db, "approved-proof", "Synthesis")
    score = _PASS_SCORE if verdict == "critic-pass" else _FAIL_SCORE
    run_id = _record_run(db, "approved-proof", score)
    if verdict == "human-accept":
        assert q.mark_synthesis_accepted(db, run_id)

    assert [r.article_id for r in digest_ranker.rank_for_digest(db, "2026-10-31")] == ["approved-proof"]
    assert client.get("/api/digest/calendar?year=2026&month=10").json()["active_dates"] == ["2026-10-31"]
    assert "approved-proof" in {r["id"] for r in q.search_all(db, "Evidence", limit=100)}
    sources = ask._wiki_sources(db, ask.AskRequest(question="Evidence", article_id="approved-proof"))
    assert "approved-proof" in {r["id"] for r in sources}
