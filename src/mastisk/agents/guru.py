"""Guru — daily learning tutor.

The user declares a learning goal (``mastisk learn "topic" --days 14``).
Guru generates a prerequisite-ordered syllabus of atomic concepts (an LLM
proposes concepts + edges; ``services.learning`` enforces the DAG in code),
then teaches one lesson per local day across all active goals:

- **Density scales with the timeline**: a 30-concept goal due in 10 days
  packs ~3 concepts per lesson; an open-ended goal gets the default
  single-concept Feynman lesson.
- **Retrieval first**: every lesson opens with warmup questions on
  previously-taught concepts whose FSRS review is due — across all goals,
  so multi-goal users get interleaving for free.
- **Feynman anatomy**: plain-language concept, analogy plus where it
  breaks, worked example, Mermaid diagram, misconception callout, then
  check questions whose grading rubric is authored *with* the question.
- Delivery reuses the reminder engine (``kind='lesson_due'``) so the
  daily push arrives over the configured notify backend.

Job-driven for syllabus generation and on-demand lessons; timer-driven
(self-gating on lesson_time + one-lesson-per-day) for the daily loop.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import ClassVar
from zoneinfo import ZoneInfo

from slugify import slugify

from mastisk.agents.base import Agent
from mastisk.agents.registry import resolve_prompt
from mastisk.bridges import intelligence
from mastisk.db.queries import connect, txn
from mastisk.paths import vault_dir
from mastisk.services import learning as learn_svc
from mastisk.settings import get_settings
from mastisk.vault_io import write_vault_text

log = logging.getLogger("mastisk.guru")


SYLLABUS_PROMPT_TEMPLATE = """You are Guru, a personal tutor planning a syllabus. Decompose the topic into atomic concepts — each teachable in one focused sitting — plus DIRECT prerequisite edges between them. Return STRICT JSON only.

Topic: {topic}
Learner context: {learner_context}
{timeline_line}

About the learner:
{identity_preamble}

Return JSON matching this schema exactly:
{{"concepts": [
  {{"slug": "<kebab-case-id>",
   "title": "<short concept name>",
   "definition": "<one plain-language sentence>",
   "tier": <1 fundamentals | 2 core | 3 advanced>}}
],
 "edges": [
  {{"prereq": "<slug>", "dependent": "<slug>"}}
]}}

Rules:
- Between {min_concepts} and {max_concepts} concepts. Atomic: "gradient descent", never "machine learning".
- Order the concepts list as a sensible learning narrative; put a motivating early payoff in the first few.
- Edges are DIRECT prerequisites only (A must be understood immediately before B makes sense) — no transitive edges, no cycles. Slugs must come from your own concepts list.
- Definitions in 12-year-old register; jargon only if the definition itself explains it.
- Strict JSON, no prose around it.
"""


LESSON_PROMPT_TEMPLATE = """You are Guru, a personal tutor who teaches like Feynman: plain language, one load-bearing analogy (and where it breaks), a concrete worked example, and the common trap. Return STRICT JSON only.

Goal topic: {topic}
Learner context: {learner_context}
Today you must teach these NEW concepts (all of them — density is paced to the learner's timeline):
{concepts_block}

Previously-taught concepts due for retrieval warmup (write ONE recall question each; do NOT re-teach them):
{warmup_block}

About the learner:
{identity_preamble}

Return JSON matching this schema exactly:
{{"title": "<lesson title, <=80 chars>",
 "sections": [
  {{"kind": "section", "heading": "<heading>", "body_md": "<markdown>"}},
  {{"kind": "diagram", "heading": "<diagram caption>", "body_md": "<mermaid source, no fences>"}},
  {{"kind": "callout", "heading": "The trap", "body_md": "<the most common misconception and why smart people fall for it>"}}
 ],
 "warmup_questions": [
  {{"concept_slug": "<slug from the warmup list>", "question": "<free-recall question>",
   "rubric": {{"key_points": ["<point>", "..."], "common_mistakes": ["<mistake>", "..."], "model_answer": "<2-4 sentence ideal answer>"}}}}
 ],
 "check_questions": [
  {{"concept_slug": "<slug from the NEW concepts>", "question": "<free-recall or application question>",
   "rubric": {{"key_points": ["<point>", "..."], "common_mistakes": ["<mistake>", "..."], "model_answer": "<2-4 sentence ideal answer>"}}}}
 ]}}

Rules:
- Teach EVERY new concept: plain-language explanation first, then an analogy that maps structure (say explicitly where the analogy breaks), then a worked example with real numbers or runnable code.
- Exactly one "diagram" section: valid Mermaid (flowchart or sequenceDiagram) that shows the mechanism, not decoration. No markdown fences inside body_md.
- One "callout" section naming the most common misconception.
- One warmup question per listed warmup concept (free recall — no hints in the question). One check question per NEW concept.
- Rubrics are the grading contract: 2-4 key_points a correct answer must contain, in plain language.
- Questions must never contain their own answers.
- Strict JSON, no prose around it.
"""


GRADE_PROMPT_TEMPLATE = """You are grading a learner's free-text answer against a rubric authored with the question. Be evidence-based: quote the learner's own words for every key point you score. Return STRICT JSON only.

Question: {question}

Rubric (the grading contract):
{rubric_json}

Learner's answer:
{answer}
{confidence_line}

Return JSON matching this schema exactly:
{{"rating": <1 Again (missed the core idea) | 2 Hard (core idea present but effortful/incomplete) | 3 Good (correct, minor gaps) | 4 Easy (complete and fluent)>,
 "feedback": "<2-4 sentences: what was right, what was missing, and the corrective explanation. If the learner was confident but wrong, make the correction vivid.>",
 "evidence": [
  {{"key_point": "<rubric key point>", "met": <true|false>, "quote": "<verbatim span from the learner's answer, or null if absent>"}}
 ]}}

Rules:
- Score ONLY against the rubric's key points; wording differences are fine, missing ideas are not.
- A hedged-but-correct answer is still correct.
- Every evidence entry needs a verbatim quote when met=true.
- Strict JSON, no prose around it.
"""


CHAT_PROMPT_TEMPLATE = """You are Guru, the learner's personal tutor, chatting inside today's lesson. Teach like Feynman: plain language, one concrete example or analogy when it helps, and honesty about where analogies break.

About the learner:
{identity_preamble}

Lesson context (goal: {goal_topic}{level_line}, day {day_number}):
{lesson_context}

Quiz state:
{quiz_state}

Rules:
- Answer the learner's actual question first; keep replies short (a few sentences to a couple of short paragraphs) unless they ask for depth.
- For questions marked NOT YET ANSWERED, never reveal the answer, the rubric, or the model answer — coach Socratically instead: point at the relevant idea, then narrow the gap with a hint.
- For graded questions you may discuss the rubric and what was missed.
- Ground explanations in this lesson's content; if the question goes beyond it, say so and answer briefly from general knowledge.
- Plain markdown only (paragraphs, lists, `code`, **bold**); no headings, no diagrams, no tables.
- You cannot change anything (grades, schedule, goals) from this chat; if asked, say how to do it in the app instead.

Conversation so far:
{history}

Learner's message:
{message}
"""


# Machine schemas for the anthropic tier's tool-forced JSON. A real schema
# matters: with a bare {"type": "object"} the model tends to nest its answer
# under an invented wrapper key (observed live: {"parameter": {...}}).
_RUBRIC_SCHEMA = {
    "type": "object",
    "properties": {
        "key_points": {"type": "array", "items": {"type": "string"}},
        "common_mistakes": {"type": "array", "items": {"type": "string"}},
        "model_answer": {"type": "string"},
    },
    "required": ["key_points", "model_answer"],
}

_QUESTION_SCHEMA = {
    "type": "object",
    "properties": {
        "concept_slug": {"type": "string"},
        "question": {"type": "string"},
        "rubric": _RUBRIC_SCHEMA,
    },
    "required": ["concept_slug", "question", "rubric"],
}

SYLLABUS_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "concepts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "slug": {"type": "string"},
                    "title": {"type": "string"},
                    "definition": {"type": "string"},
                    "tier": {"type": "integer", "minimum": 1, "maximum": 3},
                },
                "required": ["slug", "title", "definition", "tier"],
            },
        },
        "edges": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "prereq": {"type": "string"},
                    "dependent": {"type": "string"},
                },
                "required": ["prereq", "dependent"],
            },
        },
    },
    "required": ["concepts", "edges"],
}

LESSON_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["section", "diagram", "callout"]},
                    "heading": {"type": "string"},
                    "body_md": {"type": "string"},
                },
                "required": ["kind", "heading", "body_md"],
            },
        },
        "warmup_questions": {"type": "array", "items": _QUESTION_SCHEMA},
        "check_questions": {"type": "array", "items": _QUESTION_SCHEMA},
    },
    "required": ["title", "sections", "warmup_questions", "check_questions"],
}

GRADE_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "rating": {"type": "integer", "minimum": 1, "maximum": 4},
        "feedback": {"type": "string"},
        "evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key_point": {"type": "string"},
                    "met": {"type": "boolean"},
                    "quote": {"type": ["string", "null"]},
                },
                "required": ["key_point", "met"],
            },
        },
    },
    "required": ["rating", "feedback", "evidence"],
}


class Guru(Agent):
    """Daily learning tutor. See module docstring."""

    name: ClassVar[str] = "guru"
    tick_seconds: ClassVar[int] = 600
    max_jobs_per_tick: ClassVar[int] = 3

    async def run_once(self) -> None:
        """Drain queued jobs (syllabus / on-demand lessons), then check the
        daily-lesson gate."""
        if self.disabled_tick():
            return
        for _ in range(self.max_jobs_per_tick):
            job = self._pick_job()
            if not job:
                break
            log.info("guru: picking job %s (%s)", job["id"], job["kind"])
            self._mark_running(job["id"])
            try:
                await self._handle(job)
                self._mark_done(job["id"])
            except Exception as e:
                log.exception("guru: job %s failed", job["id"])
                self._mark_failed(job["id"], str(e))
        try:
            await self._maybe_generate_daily()
        except Exception:
            log.exception("guru: daily lesson tick failed")

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job.get("payload_json") or "{}")
        if job["kind"] == "syllabus":
            await self._generate_syllabus(int(payload["goal_id"]))
        elif job["kind"] == "lesson":
            await self._generate_lesson(_local_today_iso())
        else:
            raise ValueError(f"guru: unknown job kind {job['kind']!r}")

    # ───── syllabus ─────

    async def _generate_syllabus(self, goal_id: int) -> None:
        with connect() as conn:
            goal = conn.execute(
                "SELECT * FROM learning_goals WHERE id = ? AND archived_at IS NULL",
                (goal_id,),
            ).fetchone()
        if goal is None:
            log.warning("guru: syllabus job for missing goal %s", goal_id)
            return
        goal = dict(goal)

        min_c, max_c = learn_svc.target_concept_count(goal.get("timeline_days"))
        timeline_line = (
            f"Timeline: the learner wants this done in {goal['timeline_days']} days "
            f"(target {goal['target_date']}). Choose concept granularity so the "
            "syllabus is completable in that window."
            if goal.get("timeline_days")
            else "Timeline: open-ended; optimize for depth over speed."
        )
        prompt = resolve_prompt("guru", "syllabus", SYLLABUS_PROMPT_TEMPLATE).format(
            topic=goal["topic"],
            learner_context=_learner_context(goal),
            timeline_line=timeline_line,
            identity_preamble=_identity_preamble(),
            min_concepts=min_c,
            max_concepts=max_c,
        )
        settings = get_settings().learning
        try:
            result, provider = await intelligence.run_intelligence(
                prompt, timeout_s=settings.timeout_s, json_object=True,
                json_schema=SYLLABUS_JSON_SCHEMA,
            )
            parsed = _parse_json(result)
        except Exception as e:
            log.warning("guru: syllabus LLM call failed for goal %s (%s)", goal_id, e)
            self._mark_goal_error(goal_id, f"syllabus generation failed: {e}")
            raise

        concepts = _validate_concepts(parsed.get("concepts"))
        if len(concepts) < 3:
            msg = f"syllabus produced only {len(concepts)} usable concepts"
            log.warning("guru: %s for goal %s", msg, goal_id)
            self._mark_goal_error(goal_id, msg)
            raise ValueError(msg)
        raw_edges = _validate_edges(parsed.get("edges"))
        edges = learn_svc.validate_dag(concepts, raw_edges)
        ordered = learn_svc.topological_order(concepts, edges)
        by_slug = {c["slug"]: c for c in concepts}
        prereqs: dict[str, list[str]] = {c["slug"]: [] for c in concepts}
        for prereq, dependent in edges:
            prereqs[dependent].append(prereq)

        with connect() as conn:
            conn.execute(
                "DELETE FROM syllabus_items WHERE goal_id = ? AND status = 'pending'",
                (goal_id,),
            )
            existing = {
                r["slug"]
                for r in conn.execute(
                    "SELECT slug FROM syllabus_items WHERE goal_id = ?", (goal_id,),
                )
            }
            # Regeneration keeps taught rows; new rows number after them so
            # the goal-detail ordering stays taught-first.
            position = conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS n FROM syllabus_items WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()["n"]
            for slug in ordered:
                if slug in existing:
                    continue  # regeneration keeps already-taught rows and their cards
                c = by_slug[slug]
                conn.execute(
                    """INSERT INTO syllabus_items
                         (goal_id, slug, title, definition, tier, position, prereqs_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (goal_id, slug, c["title"], c["definition"], c["tier"],
                     position, json.dumps(prereqs[slug])),
                )
                position += 1
            # A paused goal keeps its pause — a background job finishing
            # must never resurrect it.
            conn.execute(
                """UPDATE learning_goals SET status = 'active'
                   WHERE id = ? AND status = 'pending'""",
                (goal_id,),
            )
            conn.execute(
                """UPDATE learning_goals
                   SET syllabus_error = NULL, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (goal_id,),
            )
        self.emit_feed(
            verb="planned",
            obj=goal["topic"],
            kind="learning",
            touched=len(ordered),
            payload={"goal_id": goal_id, "concepts": len(ordered), "provider": provider},
        )
        log.info("guru: syllabus for goal %s — %d concepts, %d edges",
                 goal_id, len(ordered), len(edges))

    def _mark_goal_error(self, goal_id: int, message: str) -> None:
        with connect() as conn:
            conn.execute(
                """UPDATE learning_goals
                   SET syllabus_error = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (message[:500], goal_id),
            )

    # ───── daily lesson ─────

    async def _maybe_generate_daily(self) -> None:
        """Self-gate: after the configured local lesson time, generate the
        day's lesson if none exists yet. Interval ticks make this robust to
        sleep/wake — the first tick after lesson_time catches up."""
        settings = get_settings()
        try:
            hour_s, minute_s = settings.learning.lesson_time.split(":", 1)
            lesson_hour, lesson_minute = int(hour_s), int(minute_s)
            tz = ZoneInfo(settings.capture.default_timezone)
        except (ValueError, KeyError) as e:
            log.warning("guru: invalid learning config (%s); skipping daily tick", e)
            return
        local_now = datetime.now(UTC).astimezone(tz)
        if (local_now.hour, local_now.minute) < (lesson_hour, lesson_minute):
            return
        # Backoff after repeated generation failures (LLM down, invalid
        # output): without this the tick would burn a full-length LLM call
        # every 10 minutes until midnight.
        retry_after = getattr(self, "_lesson_retry_after", None)
        if retry_after is not None and datetime.now(UTC) < retry_after:
            return
        today = local_now.date().isoformat()
        with connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM lessons WHERE lesson_date = ? LIMIT 1", (today,),
            ).fetchone()
        if exists:
            return
        await self._generate_lesson(today)

    async def _generate_lesson(self, lesson_date: str) -> None:
        """Generate the lesson for ``lesson_date``: rotation-pick a goal,
        pace density from its timeline, pull due warmups across all goals,
        one LLM call, validate, persist, mirror to vault, push reminder."""
        settings = get_settings().learning
        with connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM lessons WHERE lesson_date = ? LIMIT 1", (lesson_date,),
            ).fetchone()
            if exists:
                return
            goal = self._pick_goal(conn)
            if goal is None:
                log.info("guru: no active goal with pending concepts; skipping lesson")
                return
            pending = [
                dict(r)
                for r in conn.execute(
                    """SELECT * FROM syllabus_items
                       WHERE goal_id = ? AND status = 'pending'
                       ORDER BY position ASC""",
                    (goal["id"],),
                )
            ]
            active_goals = conn.execute(
                """SELECT COUNT(DISTINCT g.id) AS n
                   FROM learning_goals g
                   JOIN syllabus_items s ON s.goal_id = g.id AND s.status = 'pending'
                   WHERE g.status = 'active' AND g.archived_at IS NULL""",
            ).fetchone()["n"]
            due_items = [
                dict(r)
                for r in conn.execute(
                    """SELECT s.*, g.topic AS goal_topic
                       FROM syllabus_items s
                       JOIN learning_goals g ON g.id = s.goal_id
                       WHERE s.status = 'taught'
                         AND s.card_json IS NOT NULL
                         AND s.due_at IS NOT NULL AND s.due_at <= ?
                         AND g.status = 'active' AND g.archived_at IS NULL
                         AND NOT EXISTS (
                           SELECT 1 FROM lesson_questions lq
                           WHERE lq.syllabus_item_id = s.id AND lq.rating IS NULL
                         )
                       ORDER BY s.due_at ASC
                       LIMIT ?""",
                    (datetime.now(UTC).isoformat(), settings.max_warmups_per_lesson),
                )
            ]
        # Questions are linked back by slug, and slugs are only unique per
        # goal — keep one item per slug (the most overdue wins) so a warmup
        # is never attributed to the wrong goal's card.
        seen_slugs: set[str] = set()
        due_items = [
            d for d in due_items
            if d["slug"] not in seen_slugs and not seen_slugs.add(d["slug"])
        ]

        density = learn_svc.concepts_per_lesson(
            remaining_concepts=len(pending),
            target_date=goal.get("target_date"),
            today=datetime.now(UTC).astimezone(
                ZoneInfo(get_settings().capture.default_timezone)
            ).date(),
            active_goals=max(1, int(active_goals)),
            max_per_lesson=settings.max_concepts_per_lesson,
        )
        new_concepts = pending[:density]
        if not new_concepts and not due_items:
            log.info("guru: nothing to teach or review; skipping lesson")
            return

        parsed = await self._llm_lesson(goal, new_concepts, due_items)
        if parsed is None:
            self._note_lesson_failure()
            return
        lesson = _validate_lesson(
            parsed,
            new_slugs={c["slug"] for c in new_concepts},
            warmup_items=due_items,
        )
        if lesson is None:
            log.warning("guru: lesson response failed validation; skipping")
            self._note_lesson_failure()
            return
        self._lesson_retry_after = None

        self._persist_lesson(goal, lesson, new_concepts, due_items, lesson_date)

    def _note_lesson_failure(self) -> None:
        """Hold daily-lesson retries for an hour after a failed generation."""
        from datetime import timedelta

        self._lesson_retry_after = datetime.now(UTC) + timedelta(hours=1)
        log.warning("guru: lesson generation failed; next retry after %s",
                    self._lesson_retry_after.isoformat())

    def _pick_goal(self, conn) -> dict | None:
        """Rotation: among active goals with pending concepts, pick the one
        taught least recently (never-taught goals first, oldest first)."""
        row = conn.execute(
            """SELECT g.*, MAX(l.day_number) AS last_day,
                      MAX(l.lesson_date) AS last_lesson_date
               FROM learning_goals g
               JOIN syllabus_items s ON s.goal_id = g.id AND s.status = 'pending'
               LEFT JOIN lessons l ON l.goal_id = g.id
               WHERE g.status = 'active' AND g.archived_at IS NULL
               GROUP BY g.id
               ORDER BY (last_lesson_date IS NOT NULL), last_lesson_date ASC,
                        g.created_at ASC
               LIMIT 1""",
        ).fetchone()
        return dict(row) if row else None

    async def _llm_lesson(
        self, goal: dict, new_concepts: list[dict], due_items: list[dict],
    ) -> dict | None:
        concepts_block = "\n".join(
            f"- slug={c['slug']} title={c['title']}\n  definition: {c['definition'] or ''}"
            for c in new_concepts
        ) or "(none today — this is a pure review day; teach nothing new)"
        warmup_block = "\n".join(
            f"- slug={d['slug']} title={d['title']} (from goal: {d.get('goal_topic', goal['topic'])})"
            for d in due_items
        ) or "(none due)"
        prompt = resolve_prompt("guru", "lesson", LESSON_PROMPT_TEMPLATE).format(
            topic=goal["topic"],
            learner_context=_learner_context(goal),
            concepts_block=concepts_block,
            warmup_block=warmup_block,
            identity_preamble=_identity_preamble(),
        )
        try:
            result, _provider = await intelligence.run_intelligence(
                prompt, timeout_s=get_settings().learning.timeout_s, json_object=True,
                json_schema=LESSON_JSON_SCHEMA,
            )
            return _parse_json(result)
        except Exception as e:
            log.warning("guru: lesson LLM call failed (%s)", e)
            return None

    def _persist_lesson(
        self,
        goal: dict,
        lesson: dict,
        new_concepts: list[dict],
        due_items: list[dict],
        lesson_date: str,
    ) -> None:
        item_by_slug = {c["slug"]: c for c in new_concepts}
        warmup_by_slug = {d["slug"]: d for d in due_items}
        with connect() as conn, txn(conn):
            day_number = conn.execute(
                "SELECT COALESCE(MAX(day_number), 0) + 1 AS n FROM lessons WHERE goal_id = ?",
                (goal["id"],),
            ).fetchone()["n"]
            cur = conn.execute(
                """INSERT OR IGNORE INTO lessons
                     (goal_id, lesson_date, day_number, title, sections_json,
                      concept_slugs_json)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (goal["id"], lesson_date, day_number, lesson["title"],
                 json.dumps(lesson["sections"]),
                 json.dumps([c["slug"] for c in new_concepts])),
            )
            if cur.rowcount == 0:
                log.warning("guru: lesson for %s already exists (race); skipping",
                            lesson_date)
                return
            lesson_id = int(cur.lastrowid)

            position = 0
            for wq in lesson["warmup_questions"]:
                item = warmup_by_slug[wq["concept_slug"]]
                conn.execute(
                    """INSERT INTO lesson_questions
                         (lesson_id, syllabus_item_id, kind, position, question,
                          rubric_json)
                       VALUES (?, ?, 'warmup', ?, ?, ?)""",
                    (lesson_id, item["id"], position, wq["question"],
                     json.dumps(wq["rubric"])),
                )
                position += 1
            for cq in lesson["check_questions"]:
                item = item_by_slug[cq["concept_slug"]]
                conn.execute(
                    """INSERT INTO lesson_questions
                         (lesson_id, syllabus_item_id, kind, position, question,
                          rubric_json)
                       VALUES (?, ?, 'check', ?, ?, ?)""",
                    (lesson_id, item["id"], position, cq["question"],
                     json.dumps(cq["rubric"])),
                )
                position += 1

            now_iso = datetime.now(UTC).isoformat()
            for c in new_concepts:
                conn.execute(
                    """UPDATE syllabus_items
                       SET status = 'taught', card_json = ?, due_at = ?,
                           taught_lesson_id = ?
                       WHERE id = ?""",
                    (learn_svc.new_card(), now_iso, lesson_id, c["id"]),
                )

        vault_path = self._write_vault_mirror(goal, lesson, lesson_date, day_number)
        if vault_path:
            with connect() as conn:
                conn.execute(
                    "UPDATE lessons SET vault_path = ? WHERE id = ?",
                    (vault_path, lesson_id),
                )

        self._create_lesson_reminder(goal, lesson, lesson_date, day_number,
                                     reviews_due=len(lesson["warmup_questions"]))
        self.emit_feed(
            verb="taught",
            obj=lesson["title"],
            kind="learning",
            touched=len(new_concepts),
            payload={
                "goal_id": goal["id"],
                "lesson_date": lesson_date,
                "day_number": day_number,
                "concepts": len(new_concepts),
                "warmups": len(lesson["warmup_questions"]),
            },
        )
        log.info("guru: lesson day %s for goal %s (%d concepts, %d warmups)",
                 day_number, goal["id"], len(new_concepts),
                 len(lesson["warmup_questions"]))

    def _write_vault_mirror(
        self, goal: dict, lesson: dict, lesson_date: str, day_number: int,
    ) -> str | None:
        try:
            folder = vault_dir() / "learning"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"{lesson_date}-{goal['slug']}-day{day_number}.md"
            lines = [
                "---",
                f"goal: {goal['topic']}",
                f"day: {day_number}",
                f"date: {lesson_date}",
                "---",
                "",
                f"# {lesson['title']}",
                "",
            ]
            for section in lesson["sections"]:
                heading = section.get("heading") or ""
                body = section.get("body_md") or ""
                if heading:
                    lines.append(f"## {heading}")
                if section.get("kind") == "diagram":
                    lines.append("```mermaid")
                    lines.append(body)
                    lines.append("```")
                else:
                    lines.append(body)
                lines.append("")
            write_vault_text(path, "\n".join(lines))
            return str(path)
        except Exception:
            log.exception("guru: vault mirror write failed")
            return None

    def _create_lesson_reminder(
        self, goal: dict, lesson: dict, lesson_date: str, day_number: int,
        *, reviews_due: int,
    ) -> None:
        body = f"{goal['topic']} — day {day_number}"
        if reviews_due:
            body += f" · {reviews_due} review{'s' if reviews_due != 1 else ''} due"
        with connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO reminders
                     (entity_type, entity_id, fire_at, kind, status, title, body, url)
                   VALUES ('lesson', ?, ?, 'lesson_due', 'pending', ?, ?, '/learning')""",
                (lesson_date, datetime.now(UTC).isoformat(),
                 f"Today's lesson: {lesson['title']}", body),
            )


# ───── grading (called synchronously from the route) ─────


async def grade_answer(
    *, question: str, rubric: dict, answer: str, confidence: int | None,
) -> dict | None:
    """One LLM grading call. Returns ``{rating, feedback, evidence}`` or
    None when every provider fails (the route falls back to self-grading)."""
    confidence_line = (
        f"\nLearner's stated confidence before seeing any feedback: {confidence}/4."
        if confidence is not None
        else ""
    )
    prompt = resolve_prompt("guru", "grade", GRADE_PROMPT_TEMPLATE).format(
        question=question,
        rubric_json=json.dumps(rubric, ensure_ascii=False, indent=2),
        answer=answer,
        confidence_line=confidence_line,
    )
    try:
        result, _provider = await intelligence.run_intelligence(
            prompt, timeout_s=get_settings().learning.grade_timeout_s, json_object=True,
            json_schema=GRADE_JSON_SCHEMA,
        )
        parsed = _parse_json(result)
    except Exception as e:
        log.warning("guru: grading LLM call failed (%s)", e)
        return None
    rating = parsed.get("rating")
    if rating not in (1, 2, 3, 4):
        log.warning("guru: grader returned invalid rating %r", rating)
        return None
    feedback = parsed.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        feedback = ""
    evidence = parsed.get("evidence")
    if not isinstance(evidence, list):
        evidence = []
    return {"rating": int(rating), "feedback": feedback.strip(), "evidence": evidence}


async def chat_reply(
    *, goal: dict, day_number: int, lesson_context: str, quiz_state: str,
    history: list[dict], message: str,
) -> str | None:
    """One free-text tutoring turn over a lesson. Returns markdown or None
    when every provider fails (the route surfaces a 503)."""
    level = str(goal.get("level") or "").strip()
    history_text = "\n".join(
        f"{m['role']}: {m['content']}" for m in history
    ) or "(new conversation)"
    prompt = resolve_prompt("guru", "chat", CHAT_PROMPT_TEMPLATE).format(
        identity_preamble=_identity_preamble(),
        goal_topic=goal.get("topic") or "",
        level_line=f", learner level: {level}" if level else "",
        day_number=day_number,
        lesson_context=lesson_context,
        quiz_state=quiz_state,
        history=history_text,
        message=message,
    )
    try:
        result, _provider = await intelligence.run_intelligence(
            prompt, timeout_s=get_settings().learning.chat_timeout_s,
        )
    except Exception as e:
        log.warning("guru: chat LLM call failed (%s)", e)
        return None
    text = str(result.get("text") or "").strip()
    return text or None


# ───── module helpers ─────


def goal_slug(topic: str) -> str:
    return slugify(topic)[:64] or "goal"


def _slug64(text: object) -> str:
    """Truncate-then-reslugify so a 64-char cut never leaves a trailing '-'
    (a bare [:64] is not slugify-idempotent, and question slugs are matched
    by re-slugifying the model's echo)."""
    return slugify(slugify(str(text or ""))[:64])


def _local_today_iso() -> str:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(UTC).astimezone(tz).date().isoformat()


def _learner_context(goal: dict) -> str:
    parts = []
    if goal.get("level"):
        parts.append(f"starting level: {goal['level']}")
    if goal.get("description"):
        parts.append(str(goal["description"]))
    if goal.get("minutes_per_day"):
        parts.append(f"about {goal['minutes_per_day']} minutes per day")
    return "; ".join(parts) or "(no extra context given)"


def _identity_preamble() -> str:
    raw = Agent.load_identity()
    stripped = raw.removeprefix("# About the user\n").strip()
    return stripped or "(no identity captured yet)"


def _parse_json(result: object) -> dict:
    """Parse the intelligence result into a dict.

    The anthropic tier's tool-forced mode returns the parsed object under
    ``json`` — prefer that. Text tiers get fence-tolerant parsing, plus a
    single-wrapper-key unwrap for models that nest the payload under an
    invented key (observed live: {"parameter": {...}})."""
    if isinstance(result, dict) and isinstance(result.get("json"), dict):
        return _unwrap_single_key(result["json"])
    text = result.get("text", "") if isinstance(result, dict) else str(result)
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
    parsed = json.loads(text.strip())
    if not isinstance(parsed, dict):
        raise ValueError(f"expected a JSON object, got {type(parsed).__name__}")
    return _unwrap_single_key(parsed)


_EXPECTED_TOP_KEYS = {"concepts", "edges", "title", "sections", "rating"}


def _unwrap_single_key(parsed: dict) -> dict:
    """Unwrap {"anything": {...real payload...}} when the outer dict has one
    key, none of the expected payload keys, and a dict value."""
    if (
        len(parsed) == 1
        and not (_EXPECTED_TOP_KEYS & set(parsed))
        and isinstance(next(iter(parsed.values())), dict)
    ):
        return next(iter(parsed.values()))
    return parsed


def _validate_concepts(raw: object) -> list[dict]:
    """Per-concept validation; bad siblings are dropped with a warning
    (same drop-bad-siblings policy as topic_suggester)."""
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    seen: set[str] = set()
    for idx, c in enumerate(raw[:60]):
        if not isinstance(c, dict):
            log.warning("guru: concept[%d] is not a dict", idx)
            continue
        slug = _slug64(c.get("slug") or c.get("title") or "")
        title = str(c.get("title") or "").strip()
        if not slug or not title or len(title) > 200 or slug in seen:
            log.warning("guru: concept[%d] missing/duplicate slug or title", idx)
            continue
        definition = str(c.get("definition") or "").strip()[:500]
        try:
            tier = int(c.get("tier") or 1)
        except (TypeError, ValueError):
            tier = 1
        tier = min(3, max(1, tier))
        seen.add(slug)
        out.append({"slug": slug, "title": title[:200], "definition": definition,
                    "tier": tier})
    return out


def _validate_edges(raw: object) -> list[tuple[str, str]]:
    if not isinstance(raw, list):
        return []
    out: list[tuple[str, str]] = []
    for e in raw[:300]:
        if not isinstance(e, dict):
            continue
        prereq = _slug64(e.get("prereq"))
        dependent = _slug64(e.get("dependent"))
        if prereq and dependent:
            out.append((prereq, dependent))
    return out


_SECTION_KINDS = {"section", "diagram", "callout"}


def _validate_lesson(
    parsed: dict, *, new_slugs: set[str], warmup_items: list[dict],
) -> dict | None:
    """Validate the lesson JSON. Sections and questions are cleaned
    per-item; a lesson with no usable sections or a check-question set
    that misses taught concepts entirely is rejected."""
    title = str(parsed.get("title") or "").strip()
    if not title:
        return None
    title = title[:200]

    sections: list[dict] = []
    for s in parsed.get("sections") or []:
        if not isinstance(s, dict):
            continue
        kind = str(s.get("kind") or "section")
        if kind not in _SECTION_KINDS:
            kind = "section"
        body = str(s.get("body_md") or "").strip()
        if not body:
            continue
        sections.append({
            "kind": kind,
            "heading": str(s.get("heading") or "").strip()[:200],
            "body_md": body,
        })
    if not sections:
        return None

    warmup_slugs = {d["slug"] for d in warmup_items}
    warmups = _clean_questions(parsed.get("warmup_questions"), allowed=warmup_slugs)
    checks = _clean_questions(parsed.get("check_questions"), allowed=new_slugs)
    if new_slugs and not checks:
        # New concepts without a single usable check question means no
        # retrieval event to seed FSRS — reject and let the next tick retry.
        return None
    return {
        "title": title,
        "sections": sections,
        "warmup_questions": warmups,
        "check_questions": checks,
    }


def _clean_questions(raw: object, *, allowed: set[str]) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    used: set[str] = set()
    for q in raw:
        if not isinstance(q, dict):
            continue
        slug = _slug64(q.get("concept_slug"))
        question = str(q.get("question") or "").strip()
        if slug not in allowed or slug in used or not question:
            continue
        rubric_raw = q.get("rubric")
        rubric = rubric_raw if isinstance(rubric_raw, dict) else {}
        key_points = [
            str(p).strip() for p in (rubric.get("key_points") or [])
            if str(p).strip()
        ][:6]
        mistakes = [
            str(p).strip() for p in (rubric.get("common_mistakes") or [])
            if str(p).strip()
        ][:6]
        model_answer = str(rubric.get("model_answer") or "").strip()[:2000]
        if not key_points:
            continue
        used.add(slug)
        out.append({
            "concept_slug": slug,
            "question": question[:1000],
            "rubric": {
                "key_points": key_points,
                "common_mistakes": mistakes,
                "model_answer": model_answer,
            },
        })
    return out
