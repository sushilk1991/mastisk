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
    assert slot_ids == ["weave", "reflect", "distill"]


# ───── feedback distillation ─────

def _seed_verdicts(db, n, *, kind="disliked", reason=None):
    from mastisk.db import queries as q
    for i in range(n):
        _seed_article(db, f"fb-article-{kind}-{i}")
        q.add_signal(
            db, article_id=f"fb-article-{kind}-{i}", kind=kind,
            value={"reason": reason} if reason else None,
        )


def test_distill_waits_for_threshold(gardener, db, vault_tmp):
    _seed_verdicts(db, 3)
    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as mock_int:
        asyncio.run(gardener._distill_pass())
    assert mock_int.call_count == 0


def test_distill_appends_rules_and_advances_watermark(gardener, db, vault_tmp):
    _seed_verdicts(db, 6, kind="disliked", reason="crypto spam")

    reply = {"text": json.dumps({"rules": ["avoid: crypto price speculation"]})}
    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock, return_value=(reply, "claude"),
    ) as mock_int:
        asyncio.run(gardener._distill_pass())

    assert mock_int.call_count == 1
    prompt = mock_int.call_args[0][0]
    assert "crypto spam" in prompt  # reasons reach the distiller

    learnings = (vault_tmp / "_self" / "learnings.md").read_text()
    assert "## Preference rules" in learnings
    assert "avoid: crypto price speculation" in learnings

    # Watermark advanced: a second pass with no new signals is a no-op.
    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as mock_again:
        asyncio.run(gardener._distill_pass())
    assert mock_again.call_count == 0


def test_distill_zero_rules_still_advances_watermark(gardener, db, vault_tmp):
    _seed_verdicts(db, 6, kind="liked")
    reply = {"text": json.dumps({"rules": []})}
    with patch(
        "mastisk.agents.gardener.intelligence.run_intelligence",
        new_callable=AsyncMock, return_value=(reply, "claude"),
    ):
        asyncio.run(gardener._distill_pass())
    feed = db.execute("SELECT * FROM feed WHERE agent='gardener' AND verb='distilled'").fetchall()
    assert len(feed) == 1
    assert not (vault_tmp / "_self" / "learnings.md").exists()  # nothing appended


def test_scout_picks_up_avoid_rules_from_learnings(db, vault_tmp):
    from mastisk.agents.scout import Scout
    self_dir = vault_tmp / "_self"
    self_dir.mkdir(parents=True, exist_ok=True)
    (self_dir / "dislikes.md").write_text("- sports\n")
    (self_dir / "learnings.md").write_text(
        "# Learnings\n\n- (2026-07-10) Keeps returning to agent memory.\n\n"
        "## Preference rules\n\n"
        "- (2026-07-14) avoid: crypto price speculation\n"
        "- (2026-07-14) Skip funding news unless technical.\n"
    )
    dislikes = Scout()._load_dislikes()
    assert "sports" in dislikes
    assert "crypto price speculation" in dislikes
    # Non-avoid rules stay out of the mechanical filter.
    assert not any("funding" in d for d in dislikes)


def test_signals_verdict_route(db):
    from fastapi.testclient import TestClient
    from mastisk.app import create_app
    from mastisk.db import queries as q
    client = TestClient(create_app())
    _seed_article(db, "voted-article")
    assert client.get("/api/signals/verdict", params={"article_id": "voted-article"}).json() == {"verdict": None}
    q.add_signal(db, article_id="voted-article", kind="liked", value=None)
    q.add_signal(db, article_id="voted-article", kind="disliked", value={"reason": "meh"})
    assert client.get("/api/signals/verdict", params={"article_id": "voted-article"}).json() == {"verdict": "disliked"}
