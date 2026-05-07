"""Unit tests for digest ranking + scoring.

Uses the shared `db` fixture (from tests/conftest.py) which applies the real
schema + migrations to a tmp SQLite, so triggers and indices match production.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import date, timedelta

import pytest

from mastisk.routes.digest_route import _latest_feedback
from mastisk.services import digest_ranker, user_profile


@pytest.fixture()
def conn(db):
    """Alias the shared `db` fixture so existing test code reads naturally."""
    return db


def _insert_article(
    conn: sqlite3.Connection, *,
    id: str, title: str, kind: str = "Concept",
    body: str | None = None, summary: str = "",
    sources: int = 0, backlinks: int = 0, forwardlinks: int = 0,
    updated_at: str | None = None,
):
    body_md = body if body is not None else ("x" * 2000)  # long enough to pass MIN_BODY_CHARS by default
    conn.execute(
        """INSERT INTO articles
               (id, kind, title, slug, summary, body_md,
                sources_count, backlinks_count, forwardlinks_count, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, COALESCE(?, CURRENT_TIMESTAMP))""",
        (id, kind, title, id.lower(), summary, body_md, sources, backlinks, forwardlinks, updated_at),
    )


class TestQualityScore:
    def test_kind_weighting(self):
        today = date.today().isoformat()
        base = {
            "body_md": "x" * 2000, "sources_count": 2,
            "backlinks_count": 3, "forwardlinks_count": 2,
            "updated_at": today,
        }
        syn = digest_ranker.quality_score({"kind": "Synthesis", **base}, today)
        con = digest_ranker.quality_score({"kind": "Concept", **base}, today)
        ent = digest_ranker.quality_score({"kind": "Entity", **base}, today)
        src = digest_ranker.quality_score({"kind": "Source", **base}, today)
        # Synthesis should dominate, Source should be floor.
        assert syn > con > ent > src
        assert syn >= 2 * src

    def test_short_body_gets_floor(self):
        today = date.today().isoformat()
        long_art = {
            "kind": "Synthesis", "body_md": "x" * 2000, "sources_count": 2,
            "backlinks_count": 2, "forwardlinks_count": 2, "updated_at": today,
        }
        short_art = {**long_art, "body_md": "too short"}
        assert digest_ranker.quality_score(long_art, today) > 0.5
        assert digest_ranker.quality_score(short_art, today) <= 0.1

    def test_more_sources_wins(self):
        today = date.today().isoformat()
        base = {
            "kind": "Concept", "body_md": "x" * 2000,
            "backlinks_count": 1, "forwardlinks_count": 1, "updated_at": today,
        }
        one = digest_ranker.quality_score({**base, "sources_count": 1}, today)
        four = digest_ranker.quality_score({**base, "sources_count": 4}, today)
        assert four > one


class TestUserProfile:
    def test_empty_profile_yields_neutral_interest(self, conn):
        prof = user_profile.build_profile(conn)
        assert prof.is_empty()
        bow = Counter({"anything": 1})
        assert user_profile.interest_score(bow, prof) == 0.5

    def test_liked_article_shifts_interest_up(self, conn):
        _insert_article(conn, id="A1", title="Agent orchestration patterns", body="Durable workflows agent orchestration " * 60)
        _insert_article(conn, id="A2", title="Crime blotter local", body="Crime local news report " * 60)
        # User liked A1.
        conn.execute("INSERT INTO signals (article_id, kind) VALUES ('A1', 'liked')")
        prof = user_profile.build_profile(conn)
        assert not prof.is_empty()
        bow_related = user_profile.article_bow(
            conn.execute("SELECT title, summary, body_md FROM articles WHERE id = 'A1'").fetchone()
        )
        bow_unrelated = user_profile.article_bow(
            conn.execute("SELECT title, summary, body_md FROM articles WHERE id = 'A2'").fetchone()
        )
        s_related = user_profile.interest_score(bow_related, prof)
        s_unrelated = user_profile.interest_score(bow_unrelated, prof)
        assert s_related > s_unrelated
        assert s_related > 0.5  # positive lift
        assert s_unrelated <= 0.55  # not much lift for unrelated

    def test_disliked_suppresses(self, conn):
        _insert_article(conn, id="A1", title="Agent orchestration patterns", body="Durable workflows agent orchestration " * 60)
        # User disliked it.
        conn.execute("INSERT INTO signals (article_id, kind) VALUES ('A1', 'disliked')")
        prof = user_profile.build_profile(conn)
        bow = user_profile.article_bow(
            conn.execute("SELECT title, summary, body_md FROM articles WHERE id = 'A1'").fetchone()
        )
        # Overlapping terms with the negative bag should push below 0.5.
        assert user_profile.interest_score(bow, prof) < 0.5


class TestRankAndPersist:
    def test_top_n_selected_and_persisted(self, conn):
        today = date.today().isoformat()
        # 8 articles all touched today, varying kinds + sources.
        # Each needs distinct content so the diversity filter doesn't merge them.
        topics = [
            "agent orchestration workflow", "kernel scheduling preemption",
            "database migration safety", "browser rendering pipeline",
            "network protocol design", "compiler optimization passes",
            "memory allocation patterns", "distributed consensus theory",
        ]
        for i, (kind, src) in enumerate(
            [("Synthesis", 4), ("Concept", 3), ("Concept", 2), ("Entity", 2),
             ("Source", 1), ("Source", 1), ("Entity", 1), ("Concept", 0)]
        ):
            _insert_article(
                conn, id=f"A{i}", title=f"{topics[i].title()} overview",
                kind=kind, body=f"{topics[i]} " * 400,
                sources=src, backlinks=src, forwardlinks=src,
                updated_at=f"{today} 10:00:00",
            )

        top = digest_ranker.rank_for_digest(conn, today, top_n=5)
        assert len(top) == 5
        assert top[0].kind == "Synthesis"  # highest kind weight wins at top

        # All 8 should be in digest_candidates; 5 selected.
        rows = conn.execute(
            "SELECT selected, rank FROM digest_candidates WHERE digest_date = ? ORDER BY rank",
            (today,),
        ).fetchall()
        assert len(rows) == 8
        selected = [r for r in rows if r["selected"]]
        assert len(selected) == 5
        ranks = sorted(r["rank"] for r in selected)
        assert ranks == [1, 2, 3, 4, 5]

    def test_rerun_overwrites_scores(self, conn):
        today = date.today().isoformat()
        _insert_article(conn, id="A1", title="T1", kind="Concept", sources=1, updated_at=f"{today} 10:00:00")
        digest_ranker.rank_for_digest(conn, today, top_n=5)
        row1 = conn.execute("SELECT final_score FROM digest_candidates WHERE article_id='A1'").fetchone()
        # Simulate the article accruing backlinks, then re-score.
        conn.execute("UPDATE articles SET backlinks_count = 10, forwardlinks_count = 10 WHERE id = 'A1'")
        digest_ranker.rank_for_digest(conn, today, top_n=5)
        row2 = conn.execute("SELECT final_score FROM digest_candidates WHERE article_id='A1'").fetchone()
        assert row2["final_score"] > row1["final_score"]

    def test_thin_body_not_selected_even_when_alone(self, conn):
        today = date.today().isoformat()
        _insert_article(conn, id="A1", title="Stubby", kind="Concept", body="short", updated_at=f"{today} 10:00:00")
        top = digest_ranker.rank_for_digest(conn, today, top_n=5)
        assert top == []
        # But it IS in the audit log with its low score.
        row = conn.execute("SELECT selected FROM digest_candidates WHERE article_id='A1'").fetchone()
        assert row["selected"] == 0


class TestFeedbackShiftsDigest:
    """End-to-end: a liked article nudges the next digest toward similar ones."""
    def test_liked_yesterday_boosts_similar_today(self, conn):
        today = date.today().isoformat()
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        # Yesterday: I liked an "agent orchestration" article.
        _insert_article(
            conn, id="Y1", title="Temporal workflow orchestration",
            body="agent orchestration temporal durable workflows " * 80,
            kind="Concept", updated_at=f"{yesterday} 10:00:00",
        )
        conn.execute("INSERT INTO signals (article_id, kind) VALUES ('Y1', 'liked')")

        # Today: two candidates with identical quality, one similar, one not.
        _insert_article(
            conn, id="T1", title="Saga pattern for distributed agent workflows",
            body="agent orchestration saga workflow distributed " * 80,
            kind="Concept", sources=2, backlinks=2, forwardlinks=2,
            updated_at=f"{today} 10:00:00",
        )
        _insert_article(
            conn, id="T2", title="Local cricket tournament results",
            body="cricket batsman tournament local innings " * 80,
            kind="Concept", sources=2, backlinks=2, forwardlinks=2,
            updated_at=f"{today} 10:00:00",
        )

        top = digest_ranker.rank_for_digest(conn, today, top_n=2)
        assert [c.article_id for c in top] == ["T1", "T2"]
        # Interest score should lift T1 above T2 in final score.
        t1 = next(c for c in top if c.article_id == "T1")
        t2 = next(c for c in top if c.article_id == "T2")
        assert t1.interest > t2.interest
        assert t1.final > t2.final


class TestFeedbackReadback:
    """_latest_feedback correctly reads vote + clear markers, regardless of order."""

    def _like(self, conn, aid, d):
        conn.execute(
            """INSERT INTO signals (article_id, kind, value_json)
               VALUES (?, 'liked', json_object('digest_date', ?))""",
            (aid, d),
        )

    def _dislike(self, conn, aid, d):
        conn.execute(
            """INSERT INTO signals (article_id, kind, value_json)
               VALUES (?, 'disliked', json_object('digest_date', ?))""",
            (aid, d),
        )

    def _clear(self, conn, aid, d):
        conn.execute(
            """INSERT INTO signals (article_id, kind, value_json)
               VALUES (?, 'edited', json_object('from', 'feedback', 'digest_date', ?, 'verdict', 'clear'))""",
            (aid, d),
        )

    def test_no_signals_returns_none(self, conn):
        _insert_article(conn, id="A1", title="t")
        assert _latest_feedback(conn, "A1", "2026-04-24") is None

    def test_liked_then_clear_reads_as_none(self, conn):
        """The bug fix: a toggle-off clear marker must override the prior like."""
        _insert_article(conn, id="A1", title="t")
        self._like(conn, "A1", "2026-04-24")
        self._clear(conn, "A1", "2026-04-24")
        assert _latest_feedback(conn, "A1", "2026-04-24") is None

    def test_clear_then_like_reads_as_yes(self, conn):
        """User changes their mind back — latest wins."""
        _insert_article(conn, id="A1", title="t")
        self._clear(conn, "A1", "2026-04-24")
        self._like(conn, "A1", "2026-04-24")
        assert _latest_feedback(conn, "A1", "2026-04-24") == "yes"

    def test_feedback_scoped_to_digest_date(self, conn):
        """A like on yesterday's digest shouldn't leak into today's read."""
        _insert_article(conn, id="A1", title="t")
        self._like(conn, "A1", "2026-04-23")
        assert _latest_feedback(conn, "A1", "2026-04-24") is None
        assert _latest_feedback(conn, "A1", "2026-04-23") == "yes"

    def test_flip_yes_to_no(self, conn):
        _insert_article(conn, id="A1", title="t")
        self._like(conn, "A1", "2026-04-24")
        self._dislike(conn, "A1", "2026-04-24")
        assert _latest_feedback(conn, "A1", "2026-04-24") == "no"
