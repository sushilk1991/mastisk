"""Learning API flow: goal creation → syllabus → today's lesson →
answering with grading → FSRS rescheduling → mastery → override → streak.

Business rules under test:
- Creating a goal with a timeline sets target_date and queues a Guru job.
- The rubric is the answer key: hidden until graded, revealed after.
- Every graded answer applies exactly one FSRS review; three Good/Easy
  recalls master a concept; Again resets the counter.
- Override rewinds the FSRS card to its pre-grade state, then re-applies.
- Streak counts completed lessons.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db) -> TestClient:
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def _mk_goal_with_item(conn, *, slug: str = "futures") -> tuple[int, int]:
    cur = conn.execute(
        """INSERT INTO learning_goals (slug, topic, status)
           VALUES ('rust', 'Rust', 'active')""",
    )
    goal_id = int(cur.lastrowid)
    cur = conn.execute(
        """INSERT INTO syllabus_items (goal_id, slug, title, position)
           VALUES (?, ?, ?, 0)""",
        (goal_id, slug, slug.title()),
    )
    return goal_id, int(cur.lastrowid)


def _mk_lesson_with_question(
    conn, goal_id: int, item_id: int, *, lesson_date: str = "2026-08-16",
) -> tuple[int, int]:
    from mastisk.services.learning import new_card

    cur = conn.execute(
        """INSERT INTO lessons (goal_id, lesson_date, day_number, title, sections_json)
           VALUES (?, ?, 1, 'Lesson', '[{"kind":"section","heading":"","body_md":"x"}]')""",
        (goal_id, lesson_date),
    )
    lesson_id = int(cur.lastrowid)
    rubric = {"key_points": ["poll not block"], "common_mistakes": [],
              "model_answer": "Futures are polled."}
    cur = conn.execute(
        """INSERT INTO lesson_questions
             (lesson_id, syllabus_item_id, kind, position, question, rubric_json)
           VALUES (?, ?, 'check', 0, 'Explain futures.', ?)""",
        (lesson_id, item_id, json.dumps(rubric)),
    )
    conn.execute(
        """UPDATE syllabus_items
           SET status = 'taught', card_json = ?, due_at = ?, taught_lesson_id = ?
           WHERE id = ?""",
        (new_card(), datetime.now(UTC).isoformat(), lesson_id, item_id),
    )
    return lesson_id, int(cur.lastrowid)


def _add_question(conn, lesson_id: int, item_id: int, position: int) -> int:
    cur = conn.execute(
        """INSERT INTO lesson_questions
             (lesson_id, syllabus_item_id, kind, position, question, rubric_json)
           VALUES (?, ?, 'warmup', ?, 'Recall futures.',
                   '{"key_points": ["poll not block"]}')""",
        (lesson_id, item_id, position),
    )
    return int(cur.lastrowid)


# ───── goals ─────


def test_create_goal_with_timeline_sets_target_date_and_enqueues_syllabus(
    vault_tmp, data_tmp, db,
) -> None:
    client = _client(vault_tmp, data_tmp, db)
    resp = client.post("/api/learning/goals", json={
        "topic": "Options pricing", "timeline_days": 14, "level": "beginner",
    })
    assert resp.status_code == 202
    body = resp.json()
    assert body["slug"] == "options-pricing"
    assert body["target_date"] is not None
    from mastisk.db.queries import connect
    with connect() as conn:
        job = dict(conn.execute(
            "SELECT * FROM jobs WHERE agent = 'guru' AND kind = 'syllabus'",
        ).fetchone())
        goal = dict(conn.execute(
            "SELECT * FROM learning_goals WHERE id = ?", (body["id"],),
        ).fetchone())
    assert json.loads(job["payload_json"])["goal_id"] == body["id"]
    assert goal["timeline_days"] == 14
    assert goal["target_date"] == body["target_date"]


def test_duplicate_topic_gets_distinct_slug(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    first = client.post("/api/learning/goals", json={"topic": "Rust"}).json()
    second = client.post("/api/learning/goals", json={"topic": "Rust"}).json()
    assert first["slug"] == "rust"
    assert second["slug"] == "rust-2"


def test_blank_topic_rejected(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    assert client.post("/api/learning/goals", json={"topic": "  "}).status_code == 422


def test_goal_lifecycle_pause_resume_archive(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    with connect() as conn:
        goal_id, _ = _mk_goal_with_item(conn)
    assert client.post(f"/api/learning/goals/{goal_id}/pause").status_code == 204
    assert client.post(f"/api/learning/goals/{goal_id}/pause").status_code == 409
    assert client.post(f"/api/learning/goals/{goal_id}/resume").status_code == 204
    assert client.delete(f"/api/learning/goals/{goal_id}").status_code == 204
    assert client.get(f"/api/learning/goals/{goal_id}").status_code == 404
    assert client.get("/api/learning/goals").json()["goals"] == []


def test_goal_detail_includes_syllabus_and_progress(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        _mk_lesson_with_question(conn, goal_id, item_id)
    listed = client.get("/api/learning/goals").json()["goals"][0]
    assert listed["total_concepts"] == 1
    assert listed["taught_concepts"] == 1
    # The concept's check question is still unanswered inside the lesson, so
    # it is NOT double-counted as a due review.
    assert listed["due_reviews"] == 0
    detail = client.get(f"/api/learning/goals/{goal_id}").json()
    assert detail["total_concepts"] == 1
    assert detail["taught_concepts"] == 1
    assert detail["syllabus"][0]["slug"] == "futures"
    assert detail["lessons"][0]["day_number"] == 1
    # Once answered (no pending question) and past its due date, the concept
    # counts as a due review again.
    q_id = client.get(f"/api/learning/lessons/{detail['lessons'][0]['id']}").json()["questions"][0]["id"]
    client.post(f"/api/learning/questions/{q_id}/answer", json={"answer": "x", "self_rating": 2})
    with connect() as conn:
        conn.execute(
            "UPDATE syllabus_items SET due_at = ? WHERE id = ?",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(), item_id),
        )
    listed = client.get("/api/learning/goals").json()["goals"][0]
    assert listed["due_reviews"] == 1


# ───── today / lesson payload ─────


def test_today_returns_lesson_with_rubric_hidden_until_graded(
    vault_tmp, data_tmp, db,
) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    from mastisk.routes.learning_route import _local_now
    today = _local_now().date().isoformat()
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        _mk_lesson_with_question(conn, goal_id, item_id, lesson_date=today)
    body = client.get("/api/learning/today").json()
    assert body["lesson"] is not None
    q = body["lesson"]["questions"][0]
    assert q["answered"] is False
    assert "rubric" not in q          # the answer key must not leak pre-grade
    # The unanswered check question owns this concept's retrieval — it must
    # not ALSO show up as a due review.
    assert body["due_reviews"] == 0
    assert body["active_goals"] == 1


def test_generate_now_conflicts_when_lesson_exists(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    from mastisk.routes.learning_route import _local_now
    assert client.post("/api/learning/generate-now").status_code == 202
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        _mk_lesson_with_question(
            conn, goal_id, item_id, lesson_date=_local_now().date().isoformat(),
        )
    assert client.post("/api/learning/generate-now").status_code == 409


# ───── answering / grading ─────


def test_answer_with_llm_grading_applies_fsrs_and_reveals_rubric(
    vault_tmp, data_tmp, db,
) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        _lesson_id, q_id = _mk_lesson_with_question(conn, goal_id, item_id)

    grade = {"rating": 3, "feedback": "Right mechanism.", "evidence": [
        {"key_point": "poll not block", "met": True, "quote": "it polls"},
    ]}
    with patch(
        "mastisk.agents.guru.grade_answer", new=AsyncMock(return_value=grade),
    ) as mocked:
        resp = client.post(f"/api/learning/questions/{q_id}/answer", json={
            "answer": "it polls until ready", "confidence": 2,
        })
    assert resp.status_code == 200
    body = resp.json()
    assert body["rating"] == 3
    assert body["graded_by"] == "llm"
    assert body["rubric"]["key_points"] == ["poll not block"]
    # Same-session check question: FSRS applies but mastery doesn't advance
    # (successive relearning requires separate sessions).
    assert body["recall_successes"] == 0
    assert body["mastered"] is False
    mocked.assert_awaited_once()
    kwargs = mocked.await_args.kwargs
    assert kwargs["confidence"] == 2
    assert kwargs["rubric"]["key_points"] == ["poll not block"]

    with connect() as conn:
        item = dict(conn.execute(
            "SELECT * FROM syllabus_items WHERE id = ?", (item_id,),
        ).fetchone())
        logrow = dict(conn.execute("SELECT * FROM review_logs").fetchone())
    assert datetime.fromisoformat(item["due_at"]) > datetime.now(UTC)
    assert logrow["rating"] == 3
    assert logrow["source"] == "quiz"


def test_answer_falls_back_to_self_rating_without_llm(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        _lesson_id, q_id = _mk_lesson_with_question(conn, goal_id, item_id)
    with patch("mastisk.agents.guru.grade_answer", new=AsyncMock()) as grader:
        resp = client.post(f"/api/learning/questions/{q_id}/answer", json={
            "answer": "polled by executor", "self_rating": 4,
        })
    assert resp.status_code == 200
    assert resp.json()["graded_by"] == "self"
    assert resp.json()["rating"] == 4
    grader.assert_not_awaited()  # self-grading must never bill an LLM call


def test_answer_503_when_all_llm_tiers_fail(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        _lesson_id, q_id = _mk_lesson_with_question(conn, goal_id, item_id)
    with patch("mastisk.agents.guru.grade_answer", new=AsyncMock(return_value=None)):
        resp = client.post(f"/api/learning/questions/{q_id}/answer", json={
            "answer": "polled",
        })
    assert resp.status_code == 503
    # Nothing was persisted — the question is still answerable.
    resp = client.post(f"/api/learning/questions/{q_id}/answer", json={
        "answer": "polled", "self_rating": 3,
    })
    assert resp.status_code == 200


def test_double_answer_conflicts(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        _lesson_id, q_id = _mk_lesson_with_question(conn, goal_id, item_id)
    client.post(f"/api/learning/questions/{q_id}/answer",
                json={"answer": "x", "self_rating": 3})
    resp = client.post(f"/api/learning/questions/{q_id}/answer",
                       json={"answer": "x", "self_rating": 3})
    assert resp.status_code == 409


def test_three_good_recalls_master_the_concept_and_again_resets(
    vault_tmp, data_tmp, db,
) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, q1 = _mk_lesson_with_question(conn, goal_id, item_id)
        q2 = _add_question(conn, lesson_id, item_id, 1)
        q3 = _add_question(conn, lesson_id, item_id, 2)
        q4 = _add_question(conn, lesson_id, item_id, 3)

    def answer(q_id: int, rating: int) -> dict:
        return client.post(f"/api/learning/questions/{q_id}/answer", json={
            "answer": "recall", "self_rating": rating,
        }).json()

    assert answer(q1, 3)["recall_successes"] == 0  # same-session check: no credit
    assert answer(q2, 4)["recall_successes"] == 1  # warmup: counts
    assert answer(q3, 1)["recall_successes"] == 0  # Again resets the counter
    assert answer(q4, 3)["recall_successes"] == 1
    # One review log per graded answer, no double-counting.
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM review_logs").fetchone()["n"]
        item = dict(conn.execute(
            "SELECT * FROM syllabus_items WHERE id = ?", (item_id,),
        ).fetchone())
    assert n == 4
    assert item["status"] == "taught"

    with connect() as conn:
        q5 = _add_question(conn, lesson_id, item_id, 4)
        q6 = _add_question(conn, lesson_id, item_id, 5)
    assert answer(q5, 3)["recall_successes"] == 2
    final = answer(q6, 4)
    assert final["recall_successes"] == 3
    assert final["mastered"] is True
    with connect() as conn:
        item = dict(conn.execute(
            "SELECT * FROM syllabus_items WHERE id = ?", (item_id,),
        ).fetchone())
    assert item["status"] == "mastered"


def test_override_rewinds_card_state_before_reapplying(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, _check_q = _mk_lesson_with_question(conn, goal_id, item_id)
        q_id = _add_question(conn, lesson_id, item_id, 1)  # warmup: counts toward mastery
        before = dict(conn.execute(
            "SELECT card_json FROM syllabus_items WHERE id = ?", (item_id,),
        ).fetchone())

    first = client.post(f"/api/learning/questions/{q_id}/answer", json={
        "answer": "recall", "self_rating": 4,
    }).json()
    assert first["recall_successes"] == 1

    # Override Easy -> Hard. Discriminating assertion: a correct rewind gives
    # prev(0) kept by Hard = 0; a missing rewind would give current(1) kept
    # by Hard = 1.
    resp = client.post(f"/api/learning/questions/{q_id}/override", json={"rating": 2})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rating"] == 2
    assert body["recall_successes"] == 0

    with connect() as conn:
        item = dict(conn.execute(
            "SELECT * FROM syllabus_items WHERE id = ?", (item_id,),
        ).fetchone())
        sources = [r["source"] for r in conn.execute(
            "SELECT source FROM review_logs ORDER BY id",
        )]
        question = dict(conn.execute(
            "SELECT * FROM lesson_questions WHERE id = ?", (q_id,),
        ).fetchone())
    assert item["recall_successes"] == 0
    assert item["card_json"] != before["card_json"]
    assert sources == ["quiz", "override"]
    assert question["rating"] == 2


def test_override_before_grading_conflicts(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        _lesson_id, q_id = _mk_lesson_with_question(conn, goal_id, item_id)
    assert client.post(
        f"/api/learning/questions/{q_id}/override", json={"rating": 2},
    ).status_code == 409


# ───── completion / streak ─────


def test_completing_lessons_builds_streak(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    from mastisk.routes.learning_route import _local_now
    today = _local_now().date()
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_today, _ = _mk_lesson_with_question(
            conn, goal_id, item_id, lesson_date=today.isoformat(),
        )
        cur = conn.execute(
            """INSERT INTO lessons (goal_id, lesson_date, day_number, title)
               VALUES (?, ?, 2, 'Yesterday')""",
            (goal_id, (today - timedelta(days=1)).isoformat()),
        )
        lesson_yday = int(cur.lastrowid)
    assert client.get("/api/learning/today").json()["streak"] == 0
    assert client.post(f"/api/learning/lessons/{lesson_yday}/complete").status_code == 204
    assert client.post(f"/api/learning/lessons/{lesson_today}/complete").status_code == 204
    assert client.get("/api/learning/today").json()["streak"] == 2
    # Completing twice is idempotent, not an error.
    assert client.post(f"/api/learning/lessons/{lesson_today}/complete").status_code == 204


def test_agent_end_to_end_via_api(vault_tmp, data_tmp, db, monkeypatch) -> None:
    """Full loop: goal via API → Guru syllabus job → Guru daily lesson →
    today payload → answer → FSRS moved. The one test that exercises every
    seam together (LLM mocked)."""
    from mastisk.agents.guru import Guru
    from mastisk.db.queries import connect
    from mastisk.routes.learning_route import _local_now
    from mastisk.settings import get_settings

    client = _client(vault_tmp, data_tmp, db)
    monkeypatch.setattr(get_settings().learning, "lesson_time", "00:00")

    goal = client.post("/api/learning/goals", json={
        "topic": "Rust async", "timeline_days": 2,
    }).json()

    syllabus = {
        "concepts": [
            {"slug": "futures", "title": "Futures", "definition": "later", "tier": 1},
            {"slug": "wakers", "title": "Wakers", "definition": "re-poll", "tier": 2},
            {"slug": "pinning", "title": "Pinning", "definition": "no move", "tier": 3},
        ],
        "edges": [{"prereq": "futures", "dependent": "wakers"}],
    }
    rubric = {"key_points": ["polling"], "common_mistakes": [], "model_answer": "x"}
    lesson = {
        "title": "Day one",
        "sections": [{"kind": "section", "heading": "h", "body_md": "b"}],
        "warmup_questions": [],
        "check_questions": [
            {"concept_slug": "futures", "question": "Explain?", "rubric": rubric},
            {"concept_slug": "wakers", "question": "Explain?", "rubric": rubric},
        ],
    }

    responses = [
        ({"text": json.dumps(syllabus)}, "anthropic"),
        ({"text": json.dumps(lesson)}, "anthropic"),
    ]
    with patch(
        "mastisk.bridges.intelligence.run_intelligence",
        new=AsyncMock(side_effect=responses),
    ):
        asyncio.run(Guru().run_once())  # drains syllabus job + daily lesson

    today = client.get("/api/learning/today").json()
    assert today["lesson"] is not None
    # timeline_days=2 with 3 concepts → 2 concepts in today's lesson.
    assert today["lesson"]["concept_slugs"] == ["futures", "wakers"]
    questions = today["lesson"]["questions"]
    assert len(questions) == 2

    answered = client.post(
        f"/api/learning/questions/{questions[0]['id']}/answer",
        json={"answer": "poll until ready", "self_rating": 3},
    ).json()
    assert answered["rating"] == 3
    with connect() as conn:
        item = dict(conn.execute(
            "SELECT * FROM syllabus_items WHERE slug = 'futures'",
        ).fetchone())
        reminder = conn.execute(
            "SELECT * FROM reminders WHERE kind = 'lesson_due'",
        ).fetchone()
    assert datetime.fromisoformat(item["due_at"]) > datetime.now(UTC)
    assert reminder is not None
    assert reminder["entity_id"] == _local_now().date().isoformat()


def test_stale_grade_write_loses_the_race_and_applies_nothing(
    vault_tmp, data_tmp, db,
) -> None:
    """The 409 pre-check runs before a slow LLM call on another connection;
    the guarded UPDATE is what actually prevents a double grade. Simulate
    the race by re-applying with the stale pre-LLM question snapshot."""
    import pytest
    from fastapi import HTTPException

    from mastisk.db.queries import connect
    from mastisk.routes.learning_route import _apply_review

    _client(vault_tmp, data_tmp, db)
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        _lesson_id, q_id = _mk_lesson_with_question(conn, goal_id, item_id)
        question = dict(conn.execute(
            "SELECT * FROM lesson_questions WHERE id = ?", (q_id,),
        ).fetchone())
        item = dict(conn.execute(
            "SELECT * FROM syllabus_items WHERE id = ?", (item_id,),
        ).fetchone())

    grade = {"rating": 3, "feedback": "", "evidence": [], "source": "self"}
    _apply_review(item=item, question=question, rating=3, answer="a",
                  confidence=None, grade=grade, source="quiz")
    # Second writer raced with the same stale snapshot: must 409 and leave
    # exactly one review applied.
    with pytest.raises(HTTPException) as exc:
        _apply_review(item=item, question=question, rating=4, answer="b",
                      confidence=None, grade=grade, source="quiz")
    assert exc.value.status_code == 409
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM review_logs").fetchone()["n"]
        row = dict(conn.execute(
            "SELECT rating FROM lesson_questions WHERE id = ?", (q_id,),
        ).fetchone())
    assert n == 1
    assert row["rating"] == 3


def test_syllabus_completion_does_not_resurrect_paused_goal(
    vault_tmp, data_tmp, db,
) -> None:
    from mastisk.agents.guru import Guru

    client = _client(vault_tmp, data_tmp, db)
    goal = client.post("/api/learning/goals", json={"topic": "Rust"}).json()
    assert client.post(f"/api/learning/goals/{goal['id']}/pause").status_code == 204

    syllabus = {
        "concepts": [
            {"slug": "a", "title": "A", "definition": "x", "tier": 1},
            {"slug": "b", "title": "B", "definition": "x", "tier": 1},
            {"slug": "c", "title": "C", "definition": "x", "tier": 2},
        ],
        "edges": [],
    }
    with patch(
        "mastisk.bridges.intelligence.run_intelligence",
        new=AsyncMock(return_value=({"text": json.dumps(syllabus)}, "anthropic")),
    ):
        asyncio.run(Guru()._generate_syllabus(goal["id"]))

    detail = client.get(f"/api/learning/goals/{goal['id']}").json()
    assert detail["status"] == "paused"          # the pause survives the job
    assert detail["total_concepts"] == 3         # syllabus still landed


# ───── lesson chat ─────


def test_lesson_chat_roundtrip_persists_and_renders(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect

    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, _qid = _mk_lesson_with_question(conn, goal_id, item_id)

    assert client.get(f"/api/learning/lessons/{lesson_id}/chat").json() == {
        "messages": [],
    }

    with patch(
        "mastisk.bridges.intelligence.run_intelligence",
        new=AsyncMock(return_value=({"text": "A future is **lazy**."}, "anthropic")),
    ):
        r = client.post(
            f"/api/learning/lessons/{lesson_id}/chat",
            json={"message": "Why is this lazy?\n\nfn main() { async {} }  "},
        )
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    # Newlines survive (learners paste code); only the edges are trimmed.
    assert msgs[0]["content"] == "Why is this lazy?\n\nfn main() { async {} }"
    # Assistant markdown arrives rendered server-side; the user turn does not.
    assert "<strong>lazy</strong>" in msgs[1]["content_html"]
    assert "content_html" not in msgs[0]

    stored = client.get(f"/api/learning/lessons/{lesson_id}/chat").json()["messages"]
    assert [m["role"] for m in stored] == ["user", "assistant"]


def test_lesson_chat_prompt_gates_rubric_on_grading(vault_tmp, data_tmp, db) -> None:
    """The rubric/model answer must never enter the chat prompt while the
    question is ungraded (it's the answer key), and must appear once graded."""
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect

    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, qid = _mk_lesson_with_question(conn, goal_id, item_id)
        conn.execute(
            """UPDATE lessons SET sections_json =
                 '[{"kind":"section","heading":"Polling","body_md":"An executor drives futures by polling."}]'
               WHERE id = ?""",
            (lesson_id,),
        )

    fake = AsyncMock(return_value=({"text": "hint"}, "anthropic"))
    with patch("mastisk.bridges.intelligence.run_intelligence", new=fake):
        client.post(f"/api/learning/lessons/{lesson_id}/chat", json={"message": "help"})
    prompt = fake.call_args.args[0]
    # The rules section always mentions the marker; assert on the quiz-state
    # line itself so the check discriminates.
    assert "NOT YET ANSWERED \u2014 do not reveal" in prompt
    assert "Futures are polled." not in prompt  # model answer withheld
    assert "Explain futures." in prompt         # question itself is fine
    assert "An executor drives futures by polling." in prompt  # section body

    with connect() as conn:
        conn.execute(
            "UPDATE lesson_questions SET rating = 3, answer_text = 'polled' WHERE id = ?",
            (qid,),
        )
    fake.reset_mock()
    with patch("mastisk.bridges.intelligence.run_intelligence", new=fake):
        client.post(f"/api/learning/lessons/{lesson_id}/chat", json={"message": "what did I miss?"})
    prompt = fake.call_args.args[0]
    assert "NOT YET ANSWERED \u2014 do not reveal" not in prompt
    assert "Futures are polled." in prompt
    assert "Graded 3/4" in prompt


def test_lesson_chat_503_persists_nothing(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect

    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, _qid = _mk_lesson_with_question(conn, goal_id, item_id)

    with patch(
        "mastisk.bridges.intelligence.run_intelligence",
        new=AsyncMock(side_effect=RuntimeError("all providers down")),
    ):
        r = client.post(
            f"/api/learning/lessons/{lesson_id}/chat", json={"message": "hi"},
        )
    assert r.status_code == 503
    # A failed turn leaves no transcript rows, so a retry can't duplicate.
    assert client.get(f"/api/learning/lessons/{lesson_id}/chat").json() == {
        "messages": [],
    }


def test_lesson_chat_unknown_lesson_404(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    assert client.get("/api/learning/lessons/999/chat").status_code == 404
    assert client.post(
        "/api/learning/lessons/999/chat", json={"message": "hi"},
    ).status_code == 404


# ───── visualize + rich section rendering ─────


def test_lesson_payload_renders_image_and_mnemonic_sections(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect

    sections = [
        {"kind": "section", "heading": "Idea", "body_md": "Plain **words**."},
        {"kind": "image", "heading": "The loop",
         "body_md": "Flat vector. No other text.",
         "image_url": "/api/attachments/abc.png"},
        {"kind": "mnemonic", "heading": "PEW", "body_md": "**P**oll first."},
    ]
    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, _q = _mk_lesson_with_question(conn, goal_id, item_id)
        conn.execute(
            "UPDATE lessons SET sections_json = ? WHERE id = ?",
            (json.dumps(sections), lesson_id),
        )
    r = client.get(f"/api/learning/lessons/{lesson_id}")
    assert r.status_code == 200
    got = r.json()["sections"]
    image = next(s for s in got if s["kind"] == "image")
    # The client renders image_url; the imagegen prompt never becomes HTML.
    assert image["image_url"] == "/api/attachments/abc.png"
    assert image["body_html"] == ""
    mnemonic = next(s for s in got if s["kind"] == "mnemonic")
    assert "<strong>P</strong>" in mnemonic["body_html"]


def test_visualize_endpoint_queues_guru_job(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect

    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, _q = _mk_lesson_with_question(conn, goal_id, item_id)
    with patch("mastisk.bridges.imagegen_bridge.available", return_value=True):
        r = client.post(f"/api/learning/lessons/{lesson_id}/visualize")
    assert r.status_code == 202
    with connect() as conn:
        job = dict(conn.execute(
            "SELECT * FROM jobs WHERE agent = 'guru' AND kind = 'visual'",
        ).fetchone())
    assert json.loads(job["payload_json"]) == {"lesson_id": lesson_id}


def test_visualize_endpoint_guards(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect

    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, _q = _mk_lesson_with_question(conn, goal_id, item_id)

    # yoyo missing → 503 with an explanation, no job queued.
    with patch("mastisk.bridges.imagegen_bridge.available", return_value=False):
        r = client.post(f"/api/learning/lessons/{lesson_id}/visualize")
    assert r.status_code == 503

    # unknown lesson → 404.
    with patch("mastisk.bridges.imagegen_bridge.available", return_value=True):
        assert client.post("/api/learning/lessons/99999/visualize").status_code == 404

    # already at the per-lesson image cap → 409.
    full = [
        {"kind": "image", "heading": "a", "body_md": "p", "image_url": "/api/attachments/1.png"},
        {"kind": "image", "heading": "b", "body_md": "p", "image_url": "/api/attachments/2.png"},
    ]
    with connect() as conn:
        conn.execute(
            "UPDATE lessons SET sections_json = ? WHERE id = ?",
            (json.dumps(full), lesson_id),
        )
    with patch("mastisk.bridges.imagegen_bridge.available", return_value=True):
        r = client.post(f"/api/learning/lessons/{lesson_id}/visualize")
    assert r.status_code == 409
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind = 'visual'",
        ).fetchone()["n"]
    assert n == 0  # every guarded path refused to queue


def test_visualize_endpoint_dedups_inflight_jobs(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect

    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, _q = _mk_lesson_with_question(conn, goal_id, item_id)
    with patch("mastisk.bridges.imagegen_bridge.available", return_value=True):
        first = client.post(f"/api/learning/lessons/{lesson_id}/visualize")
        second = client.post(f"/api/learning/lessons/{lesson_id}/visualize")
    assert first.status_code == 202
    assert second.status_code == 202
    assert second.json()["job_id"] == first.json()["job_id"]
    assert second.json().get("duplicate") is True
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE kind = 'visual'",
        ).fetchone()["n"]
    assert n == 1  # a double-click never pays for a second render


def test_visualize_endpoint_409_when_images_disabled(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect
    from mastisk.settings import get_settings

    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, _q = _mk_lesson_with_question(conn, goal_id, item_id)
    get_settings().learning.lesson_images = "off"
    try:
        with patch("mastisk.bridges.imagegen_bridge.available", return_value=True):
            r = client.post(f"/api/learning/lessons/{lesson_id}/visualize")
    finally:
        get_settings().learning.lesson_images = "auto"
    assert r.status_code == 409
    assert "disabled" in r.json()["detail"]


def test_lesson_payload_exposes_can_visualize_and_blanks_prompts(vault_tmp, data_tmp, db) -> None:
    client = _client(vault_tmp, data_tmp, db)
    from mastisk.db.queries import connect

    with connect() as conn:
        goal_id, item_id = _mk_goal_with_item(conn)
        lesson_id, _q = _mk_lesson_with_question(conn, goal_id, item_id)
        conn.execute(
            "UPDATE lessons SET sections_json = ? WHERE id = ?",
            (json.dumps([
                {"kind": "section", "heading": "h", "body_md": "text"},
                {"kind": "image", "heading": "fig", "body_md": "SECRET PROMPT",
                 "image_url": "/api/attachments/x.png"},
            ]), lesson_id),
        )
    with patch("mastisk.bridges.imagegen_bridge.available", return_value=True):
        r = client.get(f"/api/learning/lessons/{lesson_id}")
    body = r.json()
    assert body["can_visualize"] is True  # 1 image < cap of 2
    image = next(s for s in body["sections"] if s["kind"] == "image")
    assert "SECRET PROMPT" not in json.dumps(body)  # prompt never ships
    assert image["image_url"] == "/api/attachments/x.png"
    with patch("mastisk.bridges.imagegen_bridge.available", return_value=False):
        r2 = client.get(f"/api/learning/lessons/{lesson_id}")
    assert r2.json()["can_visualize"] is False
