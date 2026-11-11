"""Linter orphan-finding tests. The advisory LLM pass is patched out."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


@pytest.fixture
def linter(db, vault_tmp):
    from mastisk.agents.linter import Linter
    return Linter()


def _seed_article(db, article_id: str, *, age_minutes: int = 0) -> None:
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, body_md, summary, updated_at)
           VALUES (?, 'Concept', ?, ?, 'body prose', 'summary',
                   datetime('now', ?))""",
        (article_id, article_id, article_id, f"-{age_minutes} minutes"),
    )


def _scan(linter) -> None:
    with patch.object(type(linter), "_llm_advise", new_callable=AsyncMock):
        asyncio.run(linter._scan())


def _orphan_rows(db) -> list:
    return db.execute(
        "SELECT article_id, resolved_at FROM lint_findings WHERE kind = 'orphan'"
    ).fetchall()


def test_orphan_findings_stay_open_while_article_is_still_an_orphan(linter, db):
    """More orphans than the per-tick cap: flagging is capped per tick, but a
    still-orphaned article is never resolved just because newer orphans exist."""
    total = linter.max_new_orphans_per_tick + 2
    for i in range(total):
        _seed_article(db, f"orphan-{i:02d}", age_minutes=i + 1)

    _scan(linter)
    rows = _orphan_rows(db)
    assert len(rows) == linter.max_new_orphans_per_tick
    assert all(r["resolved_at"] is None for r in rows)

    # A newer orphan arrives; the old LIMIT 10 window would rotate an already
    # flagged article out and resolve it.
    _seed_article(db, "orphan-newest")
    _scan(linter)
    rows = _orphan_rows(db)
    assert all(r["resolved_at"] is None for r in rows)
    assert len(rows) == total + 1


def test_orphan_finding_resolves_once_article_is_linked(linter, db):
    _seed_article(db, "a")
    _seed_article(db, "b")
    _scan(linter)
    assert {r["article_id"] for r in _orphan_rows(db)} == {"a", "b"}

    db.execute("INSERT INTO links (from_article, to_article) VALUES ('a', 'b')")
    _scan(linter)
    assert all(r["resolved_at"] is not None for r in _orphan_rows(db))
