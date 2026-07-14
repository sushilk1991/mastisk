"""Gardener tests: weave candidate selection, weave persistence, reflection."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

from mastisk.agents.gardener import Gardener


WEAVE_JSON = {
    "text": "```json\n" + json.dumps({
        "skip": False,
        "id": "WILL-BE-FORCED",
        "kind": "Concept",
        "title": "Agent Orchestration",
        "aka": [],
        "summary": "How agents coordinate.",
        "confidence": 0.9,
        "reading_minutes": 3,
        "sections": [
            {"h": "What it is", "body": "<p>Coordination of agents.</p>"},
            {"h": "Key facts", "body": "<ul><li>(2026-07-14) Referenced by 3 articles</li></ul>"},
        ],
        "related": [],
    }) + "\n```",
}


@pytest.fixture
def gardener(db):
    return Gardener()


def _seed_article(db, article_id, *, kind="Concept", body="x" * 500, confidence=0.7):
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, body_md, confidence, updated_by)
           VALUES (?, ?, ?, ?, ?, ?, 'Compiler')""",
        (article_id, kind, article_id.replace("-", " ").title(), article_id, body, confidence),
    )


def _seed_stub_with_backlinks(db, stub_id, referrer_ids, *, mention=True):
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, body_md, confidence, updated_by)
           VALUES (?, 'Entity', ?, ?, '', 0.0, 'Compiler (stub)')""",
        (stub_id, stub_id.replace("-", " ").title(), stub_id),
    )
    for ref in referrer_ids:
        _seed_article(db, ref)
        body = (
            f'<p>Discusses <span class="link" data-target="{stub_id}">the stub</span> at length.</p>'
            if mention else "<p>No mention.</p>"
        )
        db.execute(
            "INSERT INTO article_sections (article_id, idx, heading, body) VALUES (?, 0, 'Body', ?)",
            (ref, body),
        )
        db.execute(
            "INSERT INTO links (from_article, to_article, weight) VALUES (?, ?, 0.5)",
            (ref, stub_id),
        )


def test_weave_candidates_selects_referenced_stubs_only(gardener, db):
    _seed_stub_with_backlinks(db, "hot-stub", ["r1", "r2", "r3"])
    _seed_stub_with_backlinks(db, "cold-stub", ["r4"])  # below min_backlinks
    _seed_article(db, "real-article")  # healthy page, not a candidate

    cands = gardener._weave_candidates(limit=10)
    assert [c["id"] for c in cands] == ["hot-stub"]
    assert cands[0]["backlinks"] == 3


def test_weave_candidates_respects_cooldown(gardener, db):
    _seed_stub_with_backlinks(db, "hot-stub", ["r1", "r2", "r3"])
    db.execute("UPDATE articles SET curated_at = datetime('now', '-1 day') WHERE id='hot-stub'")
    assert gardener._weave_candidates(limit=10) == []
    db.execute("UPDATE articles SET curated_at = datetime('now', '-8 days') WHERE id='hot-stub'")
    assert [c["id"] for c in gardener._weave_candidates(limit=10)] == ["hot-stub"]


def test_weave_context_extracts_only_mentioning_sections(gardener, db):
    _seed_stub_with_backlinks(db, "hot-stub", ["r1", "r2", "r3"])
    db.execute(
        "INSERT INTO article_sections (article_id, idx, heading, body) VALUES ('r1', 1, 'Other', '<p>unrelated</p>')",
    )
    ctx = gardener._weave_context("hot-stub")
    assert "From `r1`" in ctx
    assert "Discusses" in ctx
    assert "unrelated" not in ctx


def test_weave_pass_persists_article_and_stamps_curated(gardener, db, vault_tmp):
    _seed_stub_with_backlinks(db, "hot-stub", ["r1", "r2", "r3"])

    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock, return_value=(WEAVE_JSON, "claude"),
    ) as mock_int:
        asyncio.run(gardener._weave_pass())

    assert mock_int.call_count == 1
    row = db.execute("SELECT * FROM articles WHERE id='hot-stub'").fetchone()
    assert row["updated_by"] == "Gardener"
    assert row["title"] == "Agent Orchestration"
    assert row["kind"] == "Entity"  # candidate kind wins over model kind
    assert row["confidence"] == 0.6  # capped for secondhand synthesis
    assert row["curated_at"] is not None
    feed = db.execute("SELECT * FROM feed WHERE agent='gardener' AND verb='wove'").fetchall()
    assert len(feed) == 1


def test_weave_pass_respects_daily_cap(gardener, db, vault_tmp):
    from mastisk.db import queries as q
    _seed_stub_with_backlinks(db, "hot-stub", ["r1", "r2", "r3"])
    for _ in range(4):  # default weave_daily_cap
        q.append_feed(db, agent="gardener", verb="wove", obj="x", kind="concept")

    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as mock_int:
        asyncio.run(gardener._weave_pass())
    assert mock_int.call_count == 0


def test_weave_without_mentions_stamps_cooldown_without_llm(gardener, db, vault_tmp):
    _seed_stub_with_backlinks(db, "hot-stub", ["r1", "r2", "r3"], mention=False)

    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as mock_int:
        asyncio.run(gardener._weave_pass())
    assert mock_int.call_count == 0
    row = db.execute("SELECT curated_at FROM articles WHERE id='hot-stub'").fetchone()
    assert row["curated_at"] is not None


def test_reflect_appends_dated_learnings(gardener, db, vault_tmp):
    from mastisk.db import queries as q
    # Enough activity to clear the thin-signal gate.
    for i in range(4):
        q.append_feed(db, agent="compiler", verb="wrote", obj=f"Article {i}", kind="concept")

    reply = {"text": json.dumps({"learnings": ["Keeps returning to agent memory — core interest."]})}
    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock, return_value=(reply, "claude"),
    ):
        asyncio.run(gardener._reflect_pass())

    learnings = (vault_tmp / "_self" / "learnings.md").read_text()
    assert "Keeps returning to agent memory — core interest." in learnings
    assert "- (20" in learnings  # dated bullet
    feed = db.execute("SELECT * FROM feed WHERE agent='gardener' AND verb='reflected'").fetchall()
    assert len(feed) == 1


def test_reflect_gated_by_cadence(gardener, db, vault_tmp):
    from mastisk.db import queries as q
    q.append_feed(db, agent="gardener", verb="reflected", obj="1 learning", kind="reflection")
    for i in range(4):
        q.append_feed(db, agent="compiler", verb="wrote", obj=f"Article {i}", kind="concept")

    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as mock_int:
        asyncio.run(gardener._reflect_pass())
    assert mock_int.call_count == 0


def test_reflect_skips_on_thin_activity_without_burning_cadence(gardener, db, vault_tmp):
    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as mock_int:
        asyncio.run(gardener._reflect_pass())
    assert mock_int.call_count == 0
    feed = db.execute("SELECT * FROM feed WHERE agent='gardener'").fetchall()
    assert feed == []


def test_gardener_registered_in_agent_catalog():
    from mastisk.agents.registry import agent_definition
    spec = agent_definition("gardener")
    assert spec is not None
    slot_ids = [s.slot_id for s in spec.slots]
    assert slot_ids == ["weave", "reflect"]
