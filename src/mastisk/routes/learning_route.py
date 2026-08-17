"""Learning (Guru) API — goals, daily lessons, and quiz grading.

Write-side flow: ``POST /api/learning/goals`` creates a goal and enqueues a
Guru syllabus job (202 — syllabus generation is an LLM call). The daily
lesson generates on Guru's timer; ``POST /api/learning/generate-now``
enqueues an on-demand lesson job for impatient days.

Read-side: ``GET /api/learning/today`` is the one-round-trip payload the
Learning view (and the /guru skill) renders — today's lesson with its
questions, the due-review count, streak, and per-goal progress.

Grading: ``POST /api/learning/questions/{id}/answer`` grades the free-text
answer against the rubric authored with the question (evidence-quoting LLM
call), applies the FSRS review to the concept's card, and returns the
grade + rubric. Rubric and model answer are hidden until a question is
graded so the UI can't leak answers. When every LLM tier is down the
client may retry with ``self_rating`` (Anki-style self-grading).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mastisk.agents import guru
from mastisk.agents.base import enqueue
from mastisk.db.queries import connect, txn
from mastisk.routines.streaks import current_streak
from mastisk.services import learning as learn_svc
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.learning_route")

router = APIRouter(prefix="/api/learning", tags=["learning"])


class GoalIn(BaseModel):
    topic: str = Field(min_length=2, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    level: str | None = Field(default=None, max_length=200)
    timeline_days: int | None = Field(default=None, ge=1, le=365)
    minutes_per_day: int | None = Field(default=None, ge=5, le=240)


class AnswerIn(BaseModel):
    answer: str = Field(min_length=1, max_length=8000)
    confidence: int | None = Field(default=None, ge=1, le=4)
    # Anki-style fallback when the grading LLM is unavailable.
    self_rating: int | None = Field(default=None, ge=1, le=4)


class OverrideIn(BaseModel):
    rating: int = Field(ge=1, le=4)


class ChatIn(BaseModel):
    message: str = Field(min_length=1, max_length=4000)


# ───── goals ─────


@router.post("/goals", status_code=202)
async def create_goal_endpoint(body: GoalIn) -> dict:
    topic = body.topic.strip()
    if not topic:
        raise HTTPException(status_code=422, detail="topic must not be blank")
    target_date = None
    if body.timeline_days:
        # "--days 2" means two lesson days: today and tomorrow. Day 1 is
        # today, so the target lands timeline_days - 1 from now.
        target_date = (
            _local_now().date() + timedelta(days=body.timeline_days - 1)
        ).isoformat()
    base_slug = guru.goal_slug(topic)
    with connect() as conn:
        slug = base_slug
        n = 2
        while conn.execute(
            "SELECT 1 FROM learning_goals WHERE slug = ?", (slug,),
        ).fetchone():
            slug = f"{base_slug}-{n}"
            n += 1
        cur = conn.execute(
            """INSERT INTO learning_goals
                 (slug, topic, description, level, timeline_days, target_date,
                  minutes_per_day, status)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (slug, topic, body.description, body.level, body.timeline_days,
             target_date, body.minutes_per_day),
        )
        goal_id = int(cur.lastrowid)
    enqueue("guru", "syllabus", {"goal_id": goal_id})
    return {"id": goal_id, "slug": slug, "status": "pending",
            "target_date": target_date}


@router.get("/goals")
async def list_goals_endpoint() -> dict:
    with connect() as conn:
        rows = conn.execute(
            """SELECT g.*,
                      COUNT(s.id) AS total_concepts,
                      SUM(CASE WHEN s.status != 'pending' THEN 1 ELSE 0 END) AS taught_concepts,
                      SUM(CASE WHEN s.status = 'mastered' THEN 1 ELSE 0 END) AS mastered_concepts,
                      SUM(CASE WHEN s.status = 'taught' AND s.due_at IS NOT NULL
                               AND s.due_at <= ?
                               AND NOT EXISTS (
                                 SELECT 1 FROM lesson_questions lq
                                 WHERE lq.syllabus_item_id = s.id AND lq.rating IS NULL
                               ) THEN 1 ELSE 0 END) AS due_reviews
               FROM learning_goals g
               LEFT JOIN syllabus_items s ON s.goal_id = g.id
               WHERE g.archived_at IS NULL
               GROUP BY g.id
               ORDER BY g.created_at DESC""",
            (datetime.now(UTC).isoformat(),),
        ).fetchall()
        goals = [_goal_row(dict(r)) for r in rows]
    return {"goals": goals, "streak": _streak()}


@router.get("/goals/{goal_id}")
async def get_goal_endpoint(goal_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT g.*,
                      COUNT(s.id) AS total_concepts,
                      SUM(CASE WHEN s.status != 'pending' THEN 1 ELSE 0 END) AS taught_concepts,
                      SUM(CASE WHEN s.status = 'mastered' THEN 1 ELSE 0 END) AS mastered_concepts,
                      SUM(CASE WHEN s.status = 'taught' AND s.due_at IS NOT NULL
                               AND s.due_at <= ?
                               AND NOT EXISTS (
                                 SELECT 1 FROM lesson_questions lq
                                 WHERE lq.syllabus_item_id = s.id AND lq.rating IS NULL
                               ) THEN 1 ELSE 0 END) AS due_reviews
               FROM learning_goals g
               LEFT JOIN syllabus_items s ON s.goal_id = g.id
               WHERE g.id = ? AND g.archived_at IS NULL
               GROUP BY g.id""",
            (datetime.now(UTC).isoformat(), goal_id),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="goal not found")
        items = conn.execute(
            """SELECT id, slug, title, definition, tier, position, prereqs_json,
                      status, due_at, recall_successes, taught_lesson_id
               FROM syllabus_items WHERE goal_id = ? ORDER BY position ASC""",
            (goal_id,),
        ).fetchall()
        lessons = conn.execute(
            """SELECT id, lesson_date, day_number, title, status
               FROM lessons WHERE goal_id = ? ORDER BY day_number DESC LIMIT 30""",
            (goal_id,),
        ).fetchall()
    goal = _goal_row(dict(row))
    goal["syllabus"] = [_syllabus_item_row(dict(i)) for i in items]
    goal["lessons"] = [dict(lesson) for lesson in lessons]
    return goal


@router.post("/goals/{goal_id}/pause", status_code=204)
async def pause_goal_endpoint(goal_id: int) -> None:
    _set_goal_status(goal_id, from_statuses=("active", "pending"), to_status="paused")


@router.post("/goals/{goal_id}/resume", status_code=204)
async def resume_goal_endpoint(goal_id: int) -> None:
    _set_goal_status(goal_id, from_statuses=("paused",), to_status="active")


@router.delete("/goals/{goal_id}", status_code=204)
async def archive_goal_endpoint(goal_id: int) -> None:
    with connect() as conn:
        cur = conn.execute(
            """UPDATE learning_goals
               SET status = 'archived', archived_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE id = ? AND archived_at IS NULL""",
            (goal_id,),
        )
        if cur.rowcount != 1:
            raise HTTPException(status_code=404, detail="goal not found")


@router.post("/goals/{goal_id}/regenerate-syllabus", status_code=202)
async def regenerate_syllabus_endpoint(goal_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM learning_goals WHERE id = ? AND archived_at IS NULL",
            (goal_id,),
        ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="goal not found")
    job_id = enqueue("guru", "syllabus", {"goal_id": goal_id})
    return {"job_id": job_id, "status": "queued"}


# ───── today / lessons ─────


@router.get("/today")
async def today_endpoint() -> dict:
    today = _local_now().date().isoformat()
    with connect() as conn:
        lesson_row = conn.execute(
            """SELECT l.*, g.topic AS goal_topic, g.slug AS goal_slug
               FROM lessons l JOIN learning_goals g ON g.id = l.goal_id
               WHERE l.lesson_date = ?""",
            (today,),
        ).fetchone()
        lesson = _lesson_payload(conn, dict(lesson_row)) if lesson_row else None
        due_reviews = conn.execute(
            """SELECT COUNT(*) AS n FROM syllabus_items s
               JOIN learning_goals g ON g.id = s.goal_id
               WHERE s.status = 'taught' AND s.due_at IS NOT NULL
                 AND s.due_at <= ?
                 AND g.status = 'active' AND g.archived_at IS NULL
                 AND NOT EXISTS (
                   SELECT 1 FROM lesson_questions lq
                   WHERE lq.syllabus_item_id = s.id AND lq.rating IS NULL
                 )""",
            (datetime.now(UTC).isoformat(),),
        ).fetchone()["n"]
        active_goals = conn.execute(
            """SELECT COUNT(*) AS n FROM learning_goals
               WHERE status IN ('active', 'pending') AND archived_at IS NULL""",
        ).fetchone()["n"]
    return {
        "date": today,
        "lesson": lesson,
        "due_reviews": int(due_reviews),
        "active_goals": int(active_goals),
        "streak": _streak(),
    }


@router.get("/lessons/{lesson_id}")
async def get_lesson_endpoint(lesson_id: int) -> dict:
    with connect() as conn:
        row = conn.execute(
            """SELECT l.*, g.topic AS goal_topic, g.slug AS goal_slug
               FROM lessons l JOIN learning_goals g ON g.id = l.goal_id
               WHERE l.id = ?""",
            (lesson_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        return _lesson_payload(conn, dict(row))


@router.post("/lessons/{lesson_id}/complete", status_code=204)
async def complete_lesson_endpoint(lesson_id: int) -> None:
    with connect() as conn:
        cur = conn.execute(
            """UPDATE lessons
               SET status = 'completed', completed_at = CURRENT_TIMESTAMP
               WHERE id = ? AND status != 'completed'""",
            (lesson_id,),
        )
        if cur.rowcount != 1:
            exists = conn.execute(
                "SELECT 1 FROM lessons WHERE id = ?", (lesson_id,),
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="lesson not found")


@router.post("/generate-now", status_code=202)
async def generate_now_endpoint() -> dict:
    today = _local_now().date().isoformat()
    with connect() as conn:
        exists = conn.execute(
            "SELECT id FROM lessons WHERE lesson_date = ?", (today,),
        ).fetchone()
    if exists:
        raise HTTPException(status_code=409, detail="today's lesson already exists")
    job_id = enqueue("guru", "lesson", {})
    return {"job_id": job_id, "status": "queued"}


@router.post("/lessons/{lesson_id}/visualize", status_code=202)
async def visualize_lesson_endpoint(lesson_id: int) -> dict:
    """Enqueue a Guru job that authors + renders one explanatory figure for
    the lesson (LLM writes the image prompt, yoyo renders it)."""
    from mastisk.bridges import imagegen_bridge

    settings = get_settings().learning
    if settings.lesson_images != "auto" or settings.max_images_per_lesson <= 0:
        raise HTTPException(
            status_code=409, detail="lesson images are disabled in settings",
        )
    if not imagegen_bridge.available():
        raise HTTPException(
            status_code=503,
            detail="image generation is unavailable (yoyo CLI not found)",
        )
    with connect() as conn:
        row = conn.execute(
            "SELECT sections_json FROM lessons WHERE id = ?", (lesson_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        # In-flight dedup: a visual job takes minutes, so double-clicks and
        # impatient re-requests must reuse the pending job instead of paying
        # for a second render (same bug class as the listener dedup fix).
        pending = conn.execute(
            """SELECT id, payload_json FROM jobs
               WHERE agent = 'guru' AND kind = 'visual'
                 AND status IN ('queued', 'running')""",
        ).fetchall()
    for p in pending:
        if _loads_dict(p["payload_json"]).get("lesson_id") == lesson_id:
            return {"job_id": p["id"], "status": "queued", "duplicate": True}
    sections = _loads_list(row["sections_json"])
    images = sum(
        1 for s in sections if isinstance(s, dict) and s.get("kind") == "image"
    )
    if images >= settings.max_images_per_lesson:
        raise HTTPException(
            status_code=409, detail="this lesson already has its visuals",
        )
    job_id = enqueue("guru", "visual", {"lesson_id": lesson_id})
    return {"job_id": job_id, "status": "queued"}


# ───── lesson chat ─────


_CHAT_HISTORY_MESSAGES = 12
_CHAT_CONTEXT_CHARS = 16_000


@router.get("/lessons/{lesson_id}/chat")
async def get_lesson_chat_endpoint(lesson_id: int) -> dict:
    with connect() as conn:
        if conn.execute(
            "SELECT 1 FROM lessons WHERE id = ?", (lesson_id,),
        ).fetchone() is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        rows = conn.execute(
            """SELECT id, role, content, created_at FROM lesson_chat_messages
               WHERE lesson_id = ? ORDER BY id ASC""",
            (lesson_id,),
        ).fetchall()
    return {"messages": [_chat_message_row(dict(r)) for r in rows]}


@router.post("/lessons/{lesson_id}/chat")
async def lesson_chat_endpoint(lesson_id: int, body: ChatIn) -> dict:
    # Preserve newlines — learners paste code — but trim the edges.
    message = body.message.strip()
    if not message:
        raise HTTPException(status_code=422, detail="message must not be blank")
    with connect() as conn:
        row = conn.execute(
            """SELECT l.*, g.topic AS goal_topic, g.level AS goal_level
               FROM lessons l JOIN learning_goals g ON g.id = l.goal_id
               WHERE l.id = ?""",
            (lesson_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="lesson not found")
        lesson = dict(row)
        history = [
            dict(r)
            for r in conn.execute(
                """SELECT role, content FROM lesson_chat_messages
                   WHERE lesson_id = ? ORDER BY id DESC LIMIT ?""",
                (lesson_id, _CHAT_HISTORY_MESSAGES),
            )
        ][::-1]
        questions = [
            dict(q)
            for q in conn.execute(
                """SELECT lq.*, s.title AS concept_title
                   FROM lesson_questions lq
                   JOIN syllabus_items s ON s.id = lq.syllabus_item_id
                   WHERE lq.lesson_id = ? ORDER BY lq.position ASC""",
                (lesson_id,),
            )
        ]

    reply = await guru.chat_reply(
        goal={"topic": lesson.get("goal_topic"), "level": lesson.get("goal_level")},
        day_number=int(lesson.get("day_number") or 1),
        lesson_context=_chat_lesson_context(lesson),
        quiz_state=_chat_quiz_state(questions),
        history=history,
        message=message,
    )
    if reply is None:
        raise HTTPException(
            status_code=503,
            detail="Guru is unavailable (all LLM providers failed); try again shortly",
        )

    # Persist the turn only after a successful reply so a failed call can be
    # retried without duplicate user messages in the transcript. The lesson
    # may have been deleted during the (slow) LLM call — FK violation → 404.
    import sqlite3

    try:
        stored = _persist_chat_turn(lesson_id, message, reply)
    except sqlite3.IntegrityError:
        raise HTTPException(status_code=404, detail="lesson not found") from None
    return {"messages": [_chat_message_row(dict(r)) for r in stored]}


def _persist_chat_turn(lesson_id: int, message: str, reply: str) -> list:
    with connect() as conn, txn(conn):
        cur = conn.execute(
            """INSERT INTO lesson_chat_messages (lesson_id, role, content)
               VALUES (?, 'user', ?)""",
            (lesson_id, message),
        )
        user_id = int(cur.lastrowid)
        cur = conn.execute(
            """INSERT INTO lesson_chat_messages (lesson_id, role, content)
               VALUES (?, 'assistant', ?)""",
            (lesson_id, reply),
        )
        assistant_id = int(cur.lastrowid)
        return conn.execute(
            """SELECT id, role, content, created_at FROM lesson_chat_messages
               WHERE id IN (?, ?) ORDER BY id ASC""",
            (user_id, assistant_id),
        ).fetchall()


def _chat_lesson_context(lesson: dict) -> str:
    parts = [f"Lesson title: {lesson.get('title') or ''}"]
    for s in _loads_list(lesson.get("sections_json")):
        if not isinstance(s, dict):
            continue
        if s.get("kind") == "image":
            # body_md is an imagegen prompt, not lesson content.
            caption = str(s.get("heading") or "").strip()
            if caption:
                parts.append(f"(The lesson includes a figure: {caption})")
            continue
        heading = str(s.get("heading") or "").strip()
        body = str(s.get("body_md") or "").strip()
        if heading:
            parts.append(f"## {heading}\n{body}")
        elif body:
            parts.append(body)
    return "\n\n".join(parts)[:_CHAT_CONTEXT_CHARS]


def _chat_quiz_state(questions: list[dict]) -> str:
    """Per-question state for the tutor prompt. Rubrics and model answers are
    the answer key — they enter the prompt only after the question is graded,
    so a chat turn can never leak an ungraded answer."""
    lines = []
    for q in questions:
        label = f"[{q['kind']}] {q['question']}"
        if q["rating"] is None:
            lines.append(f"- {label}\n  NOT YET ANSWERED — do not reveal the answer.")
            continue
        rubric = _loads_dict(q.get("rubric_json"))
        raw_points = rubric.get("key_points")
        key_points = "; ".join(
            str(p) for p in (raw_points if isinstance(raw_points, list) else [])
        )
        lines.append(
            f"- {label}\n  Graded {q['rating']}/4."
            f" Learner answered: {str(q.get('answer_text') or '')[:400]}\n"
            f"  Rubric key points: {key_points[:800]}\n"
            f"  Model answer: {str(rubric.get('model_answer') or '')[:800]}"
        )
    return "\n".join(lines) or "(no quiz questions in this lesson)"


_chat_md = None


def _chat_message_row(m: dict) -> dict:
    global _chat_md
    out = {
        "id": m["id"],
        "role": m["role"],
        "content": m["content"],
        "created_at": m.get("created_at"),
    }
    if m["role"] == "assistant":
        if _chat_md is None:
            from markdown_it import MarkdownIt

            _chat_md = MarkdownIt()
        out["content_html"] = _chat_md.render(m["content"])
    return out


# ───── answering / grading ─────


@router.post("/questions/{question_id}/answer")
async def answer_question_endpoint(question_id: int, body: AnswerIn) -> dict:
    with connect() as conn:
        q_row = conn.execute(
            "SELECT * FROM lesson_questions WHERE id = ?", (question_id,),
        ).fetchone()
        if q_row is None:
            raise HTTPException(status_code=404, detail="question not found")
        question = dict(q_row)
        if question["rating"] is not None:
            raise HTTPException(status_code=409, detail="question already graded")
        item_row = conn.execute(
            "SELECT * FROM syllabus_items WHERE id = ?",
            (question["syllabus_item_id"],),
        ).fetchone()
        if item_row is None:
            raise HTTPException(status_code=404, detail="concept not found")
        item = dict(item_row)

    rubric = _loads_dict(question["rubric_json"])
    if body.self_rating is not None:
        grade = {"rating": body.self_rating, "feedback": "", "evidence": [],
                 "source": "self"}
    else:
        grade = await guru.grade_answer(
            question=question["question"],
            rubric=rubric,
            answer=body.answer,
            confidence=body.confidence,
        )
        if grade is None:
            raise HTTPException(
                status_code=503,
                detail="grading is unavailable (all LLM providers failed); "
                       "retry with self_rating to self-grade",
            )
        grade["source"] = "llm"

    rating = int(grade["rating"])
    result = _apply_review(
        item=item, question=question, rating=rating,
        answer=body.answer, confidence=body.confidence, grade=grade,
        source="quiz",
    )
    return {
        "rating": rating,
        "feedback": grade.get("feedback") or "",
        "evidence": grade.get("evidence") or [],
        "rubric": rubric,
        "graded_by": grade["source"],
        **result,
    }


@router.post("/questions/{question_id}/override")
async def override_grade_endpoint(question_id: int, body: OverrideIn) -> dict:
    with connect() as conn:
        q_row = conn.execute(
            "SELECT * FROM lesson_questions WHERE id = ?", (question_id,),
        ).fetchone()
        if q_row is None:
            raise HTTPException(status_code=404, detail="question not found")
        question = dict(q_row)
        if question["rating"] is None:
            raise HTTPException(status_code=409, detail="question not graded yet")
        item_row = conn.execute(
            "SELECT * FROM syllabus_items WHERE id = ?",
            (question["syllabus_item_id"],),
        ).fetchone()
        if item_row is None:
            raise HTTPException(status_code=404, detail="concept not found")
        item = dict(item_row)

    stored = _loads_dict(question["grade_json"])
    prev_card = stored.get("prev_card_json")
    prev_successes = stored.get("prev_recall_successes")
    if not prev_card or prev_successes is None:
        raise HTTPException(
            status_code=409, detail="original card state missing; cannot override",
        )
    # Rewind the FSRS state to before the original grade, then re-apply.
    item["card_json"] = prev_card
    item["recall_successes"] = int(prev_successes)
    grade = {**stored, "rating": body.rating, "source": "override"}
    result = _apply_review(
        item=item, question=question, rating=body.rating,
        answer=question["answer_text"] or "", confidence=question["confidence"],
        grade=grade, source="override",
    )
    return {"rating": body.rating, "graded_by": "override", **result}


# ───── helpers ─────


def _apply_review(
    *, item: dict, question: dict, rating: int, answer: str,
    confidence: int | None, grade: dict, source: str,
) -> dict:
    """Apply one graded retrieval to the concept's FSRS card and persist
    question + item + review log atomically. Returns the scheduling
    outcome the client renders."""
    settings = get_settings().learning
    card_json = item["card_json"] or learn_svc.new_card()
    prev_successes = int(item["recall_successes"] or 0)
    new_card_json, due_at = learn_svc.review_card(
        card_json, rating, desired_retention=settings.desired_retention,
    )
    same_session_check = (
        question.get("kind") == "check"
        and question.get("lesson_id") == item.get("taught_lesson_id")
    )
    if rating >= learn_svc.RATING_GOOD:
        # The check question inside the lesson that first taught the concept
        # is same-session retrieval: it schedules the card but doesn't count
        # toward mastery (successive relearning = separate sessions).
        successes = prev_successes if same_session_check else prev_successes + 1
    elif rating == learn_svc.RATING_AGAIN:
        successes = 0
    else:  # Hard: recalled with effort — neither a lapse nor a clean success
        successes = prev_successes
    mastered = successes >= learn_svc.MASTERY_RECALLS
    status = "mastered" if mastered else "taught"

    grade_stored = {
        **grade,
        "prev_card_json": card_json,
        "prev_recall_successes": prev_successes,
    }
    # Guarded write: the pre-grade 409 check ran before a (slow) LLM call on
    # a different connection, so a concurrent answer may have landed since.
    # For a fresh grade the row must still be ungraded; for an override it
    # must still be graded. rowcount 0 → lose the race → 409, and none of
    # the FSRS/log writes happen (the whole block is one transaction).
    rating_guard = "rating IS NOT NULL" if source == "override" else "rating IS NULL"
    with connect() as conn, txn(conn):
        cur = conn.execute(
            f"""UPDATE lesson_questions
               SET answer_text = ?, confidence = ?, rating = ?, grade_json = ?,
                   graded_at = CURRENT_TIMESTAMP
               WHERE id = ? AND {rating_guard}""",
            (answer, confidence, rating,
             json.dumps(grade_stored, ensure_ascii=False), question["id"]),
        )
        if cur.rowcount != 1:
            raise HTTPException(
                status_code=409,
                detail="question was graded concurrently; reload the lesson",
            )
        conn.execute(
            """UPDATE syllabus_items
               SET card_json = ?, due_at = ?, recall_successes = ?, status = ?
               WHERE id = ?""",
            (new_card_json, due_at, successes, status, item["id"]),
        )
        conn.execute(
            """INSERT INTO review_logs (syllabus_item_id, question_id, rating, source)
               VALUES (?, ?, ?, ?)""",
            (item["id"], question["id"], rating, source),
        )
    return {
        "due_at": due_at,
        "recall_successes": successes,
        "mastered": mastered,
        "concept_slug": item["slug"],
        "concept_title": item["title"],
    }


def _render_sections(raw_sections: list) -> list[dict]:
    """Markdown → HTML server-side (frontend has no markdown renderer;
    article sections arrive as HTML for the same reason). Diagram sections
    keep raw Mermaid source for MermaidBlock."""
    from markdown_it import MarkdownIt

    md = MarkdownIt()
    out = []
    for s in raw_sections:
        if not isinstance(s, dict):
            continue
        body_md = str(s.get("body_md") or "")
        section = {
            "kind": s.get("kind") or "section",
            "heading": s.get("heading") or "",
            "body_md": body_md,
        }
        if section["kind"] == "image":
            # body_md holds the imagegen prompt — blank it so no client-side
            # markdown fallback ever shows the learner a prompt.
            section["image_url"] = s.get("image_url")
            section["body_md"] = ""
            section["body_html"] = ""
        elif section["kind"] == "diagram":
            section["body_html"] = body_md
        else:
            section["body_html"] = md.render(body_md)
        out.append(section)
    return out


def _can_visualize(sections: list[dict]) -> bool:
    """Server-computed gate for the Visualize button, so the client never
    hardcodes the image cap or probes for the yoyo CLI."""
    from mastisk.bridges import imagegen_bridge

    settings = get_settings().learning
    images = sum(
        1 for s in sections if isinstance(s, dict) and s.get("kind") == "image"
    )
    return (
        settings.lesson_images == "auto"
        and images < settings.max_images_per_lesson
        and imagegen_bridge.available()
    )


def _lesson_payload(conn: Any, row: dict) -> dict:
    questions = [
        _question_row(dict(q))
        for q in conn.execute(
            """SELECT lq.*, s.slug AS concept_slug, s.title AS concept_title
               FROM lesson_questions lq
               JOIN syllabus_items s ON s.id = lq.syllabus_item_id
               WHERE lq.lesson_id = ?
               ORDER BY lq.position ASC""",
            (row["id"],),
        )
    ]
    return {
        "id": row["id"],
        "goal_id": row["goal_id"],
        "goal_topic": row.get("goal_topic"),
        "goal_slug": row.get("goal_slug"),
        "lesson_date": row["lesson_date"],
        "day_number": row["day_number"],
        "title": row["title"],
        "status": row["status"],
        "sections": _render_sections(_loads_list(row.get("sections_json"))),
        "can_visualize": _can_visualize(_loads_list(row.get("sections_json"))),
        "concept_slugs": _loads_list(row.get("concept_slugs_json")),
        "questions": questions,
        "created_at": row.get("created_at"),
        "completed_at": row.get("completed_at"),
    }


def _question_row(q: dict) -> dict:
    graded = q["rating"] is not None
    out = {
        "id": q["id"],
        "kind": q["kind"],
        "position": q["position"],
        "question": q["question"],
        "concept_slug": q.get("concept_slug"),
        "concept_title": q.get("concept_title"),
        "answered": graded,
        "rating": q["rating"],
        "confidence": q["confidence"],
        "answer_text": q["answer_text"] if graded else None,
    }
    if graded:
        # The rubric is the answer key — only reveal it after grading.
        out["rubric"] = _loads_dict(q.get("rubric_json"))
        grade = _loads_dict(q.get("grade_json"))
        grade.pop("prev_card_json", None)
        grade.pop("prev_recall_successes", None)
        out["grade"] = grade
    return out


def _goal_row(g: dict) -> dict:
    total = int(g.get("total_concepts") or 0)
    taught = int(g.get("taught_concepts") or 0)
    return {
        "id": g["id"],
        "slug": g["slug"],
        "topic": g["topic"],
        "description": g.get("description"),
        "level": g.get("level"),
        "timeline_days": g.get("timeline_days"),
        "target_date": g.get("target_date"),
        "minutes_per_day": g.get("minutes_per_day"),
        "status": g["status"],
        "syllabus_error": g.get("syllabus_error"),
        "total_concepts": total,
        "taught_concepts": taught,
        "mastered_concepts": int(g.get("mastered_concepts") or 0),
        "due_reviews": int(g.get("due_reviews") or 0),
        "created_at": g.get("created_at"),
    }


def _syllabus_item_row(i: dict) -> dict:
    return {
        "id": i["id"],
        "slug": i["slug"],
        "title": i["title"],
        "definition": i.get("definition"),
        "tier": i["tier"],
        "position": i["position"],
        "prereqs": _loads_list(i.get("prereqs_json")),
        "status": i["status"],
        "due_at": i.get("due_at"),
        "recall_successes": i.get("recall_successes"),
    }


def _set_goal_status(
    goal_id: int, *, from_statuses: tuple[str, ...], to_status: str,
) -> None:
    placeholders = ", ".join("?" for _ in from_statuses)
    with connect() as conn:
        cur = conn.execute(
            f"""UPDATE learning_goals
                SET status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND archived_at IS NULL
                  AND status IN ({placeholders})""",
            (to_status, goal_id, *from_statuses),
        )
        if cur.rowcount != 1:
            exists = conn.execute(
                "SELECT 1 FROM learning_goals WHERE id = ? AND archived_at IS NULL",
                (goal_id,),
            ).fetchone()
            if exists is None:
                raise HTTPException(status_code=404, detail="goal not found")
            raise HTTPException(
                status_code=409, detail=f"goal cannot move to {to_status}",
            )


def _streak() -> int:
    with connect() as conn:
        dates = [
            r["lesson_date"]
            for r in conn.execute(
                "SELECT DISTINCT lesson_date FROM lessons WHERE status = 'completed'",
            )
        ]
    return current_streak(dates, today=_local_now().date().isoformat())


def _local_now() -> datetime:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(UTC).astimezone(tz)


def _loads_dict(raw: object) -> dict:
    try:
        v = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return v if isinstance(v, dict) else {}


def _loads_list(raw: object) -> list:
    try:
        v = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return v if isinstance(v, list) else []
