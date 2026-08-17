"""Guru agent behavior: syllabus jobs, the daily-lesson gate, timeline
density, validation of LLM output, and lesson delivery side effects
(questions, FSRS cards, vault mirror, push reminder).

All LLM calls are mocked at ``mastisk.bridges.intelligence.run_intelligence``
— these tests encode the *contract* around the model, not the model.
"""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mastisk.agents.guru import Guru
from mastisk.db.queries import connect
from mastisk.settings import get_settings


def _mk_goal(
    conn,
    *,
    topic: str = "Rust async internals",
    slug: str = "rust-async-internals",
    status: str = "active",
    timeline_days: int | None = None,
    target_date: str | None = None,
) -> int:
    cur = conn.execute(
        """INSERT INTO learning_goals (slug, topic, status, timeline_days, target_date)
           VALUES (?, ?, ?, ?, ?)""",
        (slug, topic, status, timeline_days, target_date),
    )
    return int(cur.lastrowid)


def _mk_items(conn, goal_id: int, slugs: list[str]) -> None:
    for pos, slug in enumerate(slugs):
        conn.execute(
            """INSERT INTO syllabus_items (goal_id, slug, title, definition, position)
               VALUES (?, ?, ?, ?, ?)""",
            (goal_id, slug, slug.replace("-", " ").title(), f"About {slug}.", pos),
        )


def _llm(payload: dict) -> AsyncMock:
    return AsyncMock(return_value=({"text": json.dumps(payload)}, "anthropic"))


def _syllabus_payload() -> dict:
    return {
        "concepts": [
            {"slug": "futures", "title": "Futures", "definition": "A value later.", "tier": 1},
            {"slug": "executors", "title": "Executors", "definition": "Who polls.", "tier": 2},
            {"slug": "wakers", "title": "Wakers", "definition": "Who re-polls.", "tier": 2},
            {"slug": "pinning", "title": "Pinning", "definition": "Why not move.", "tier": 3},
        ],
        "edges": [
            {"prereq": "futures", "dependent": "executors"},
            {"prereq": "executors", "dependent": "wakers"},
            {"prereq": "wakers", "dependent": "futures"},  # cycle — must be dropped
            {"prereq": "wakers", "dependent": "pinning"},
        ],
    }


def _lesson_payload(check_slugs: list[str], warmup_slugs: list[str] | None = None) -> dict:
    rubric = {
        "key_points": ["names the mechanism", "gives an example"],
        "common_mistakes": ["confuses polling with blocking"],
        "model_answer": "A future is polled by an executor until ready.",
    }
    return {
        "title": "Futures and friends",
        "sections": [
            {"kind": "section", "heading": "The idea", "body_md": "Plain words first."},
            {"kind": "diagram", "heading": "Mechanism", "body_md": "flowchart LR\n  A --> B"},
            {"kind": "callout", "heading": "The trap", "body_md": "Poll is not block."},
        ],
        "warmup_questions": [
            {"concept_slug": s, "question": f"Recall {s}?", "rubric": rubric}
            for s in (warmup_slugs or [])
        ],
        "check_questions": [
            {"concept_slug": s, "question": f"Explain {s} to a 12-year-old.", "rubric": rubric}
            for s in check_slugs
        ],
    }


# ───── syllabus job ─────


def test_syllabus_job_persists_dag_ordered_concepts_and_activates_goal(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn, status="pending")
    with patch(
        "mastisk.bridges.intelligence.run_intelligence", new=_llm(_syllabus_payload()),
    ):
        asyncio.run(Guru()._generate_syllabus(goal_id))

    with connect() as conn:
        goal = dict(conn.execute(
            "SELECT * FROM learning_goals WHERE id = ?", (goal_id,),
        ).fetchone())
        items = [dict(r) for r in conn.execute(
            "SELECT * FROM syllabus_items WHERE goal_id = ? ORDER BY position",
            (goal_id,),
        )]
    assert goal["status"] == "active"
    assert goal["syllabus_error"] is None
    slugs = [i["slug"] for i in items]
    assert slugs.index("futures") < slugs.index("executors") < slugs.index("wakers")
    # Cycle edge (wakers -> futures) dropped: futures has no prereqs.
    futures = next(i for i in items if i["slug"] == "futures")
    assert json.loads(futures["prereqs_json"]) == []
    pinning = next(i for i in items if i["slug"] == "pinning")
    assert json.loads(pinning["prereqs_json"]) == ["wakers"]


def test_syllabus_with_too_few_usable_concepts_marks_goal_error(db, vault_tmp) -> None:
    bad = {"concepts": [{"slug": "only-one", "title": "Only", "definition": "x", "tier": 1}],
           "edges": []}
    with connect() as conn:
        goal_id = _mk_goal(conn, status="pending")
    with patch("mastisk.bridges.intelligence.run_intelligence", new=_llm(bad)):
        try:
            asyncio.run(Guru()._generate_syllabus(goal_id))
        except ValueError:
            pass
        else:  # pragma: no cover
            raise AssertionError("expected ValueError for unusable syllabus")
    with connect() as conn:
        goal = dict(conn.execute(
            "SELECT * FROM learning_goals WHERE id = ?", (goal_id,),
        ).fetchone())
    assert goal["status"] == "pending"
    assert "concepts" in (goal["syllabus_error"] or "")


def test_syllabus_regeneration_keeps_taught_rows(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn, status="active")
        _mk_items(conn, goal_id, ["futures", "old-pending"])
        conn.execute(
            "UPDATE syllabus_items SET status = 'taught', card_json = '{}' "
            "WHERE goal_id = ? AND slug = 'futures'",
            (goal_id,),
        )
    with patch(
        "mastisk.bridges.intelligence.run_intelligence", new=_llm(_syllabus_payload()),
    ):
        asyncio.run(Guru()._generate_syllabus(goal_id))
    with connect() as conn:
        rows = {r["slug"]: dict(r) for r in conn.execute(
            "SELECT * FROM syllabus_items WHERE goal_id = ?", (goal_id,),
        )}
    assert rows["futures"]["status"] == "taught"  # kept, card preserved
    assert "old-pending" not in rows              # stale pending row replaced
    assert {"executors", "wakers", "pinning"} <= set(rows)


# ───── daily gate ─────


def test_daily_gate_waits_for_lesson_time(db, vault_tmp, monkeypatch) -> None:
    monkeypatch.setattr(get_settings().learning, "lesson_time", "23:59")
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures"])
    called = AsyncMock()
    guru = Guru()
    monkeypatch.setattr(guru, "_generate_lesson", called)
    asyncio.run(guru._maybe_generate_daily())
    called.assert_not_awaited()


def test_daily_gate_generates_once_after_lesson_time(db, vault_tmp, monkeypatch) -> None:
    monkeypatch.setattr(get_settings().learning, "lesson_time", "00:00")
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures"])
    with patch(
        "mastisk.bridges.intelligence.run_intelligence",
        new=_llm(_lesson_payload(["futures"])),
    ):
        asyncio.run(Guru()._maybe_generate_daily())
        # Second tick the same day must be a no-op (one lesson per day).
        asyncio.run(Guru()._maybe_generate_daily())
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM lessons").fetchone()["n"]
    assert n == 1


# ───── lesson generation ─────


def test_lesson_density_follows_timeline(db, vault_tmp) -> None:
    """A 6-concept goal due in 2 days must pack 3 concepts into today's
    lesson — the user's timeline feedback, end to end."""
    from zoneinfo import ZoneInfo
    local_today = datetime.now(UTC).astimezone(
        ZoneInfo(get_settings().capture.default_timezone)
    ).date()
    target = (local_today + timedelta(days=1)).isoformat()
    with connect() as conn:
        goal_id = _mk_goal(conn, timeline_days=2, target_date=target)
        _mk_items(conn, goal_id, [f"c{i}" for i in range(6)])
    payload = _lesson_payload([f"c{i}" for i in range(3)])
    with patch("mastisk.bridges.intelligence.run_intelligence", new=_llm(payload)):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))
    with connect() as conn:
        lesson = dict(conn.execute("SELECT * FROM lessons").fetchone())
        taught = conn.execute(
            "SELECT COUNT(*) AS n FROM syllabus_items WHERE status = 'taught'",
        ).fetchone()["n"]
    assert json.loads(lesson["concept_slugs_json"]) == ["c0", "c1", "c2"]
    assert taught == 3


def test_open_ended_goal_teaches_single_concept(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures", "executors"])
    with patch(
        "mastisk.bridges.intelligence.run_intelligence",
        new=_llm(_lesson_payload(["futures"])),
    ):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))
    with connect() as conn:
        lesson = dict(conn.execute("SELECT * FROM lessons").fetchone())
    assert json.loads(lesson["concept_slugs_json"]) == ["futures"]


def test_lesson_persists_questions_cards_vault_and_reminder(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures", "executors"])
        # A previously-taught concept whose review is due → warmup.
        conn.execute(
            """UPDATE syllabus_items
               SET status = 'taught', card_json = '{"due": "x"}', due_at = ?
               WHERE slug = 'executors'""",
            ((datetime.now(UTC) - timedelta(hours=1)).isoformat(),),
        )
    payload = _lesson_payload(["futures"], warmup_slugs=["executors"])
    with patch("mastisk.bridges.intelligence.run_intelligence", new=_llm(payload)):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))

    with connect() as conn:
        lesson = dict(conn.execute("SELECT * FROM lessons").fetchone())
        questions = [dict(r) for r in conn.execute(
            "SELECT * FROM lesson_questions ORDER BY position",
        )]
        futures = dict(conn.execute(
            "SELECT * FROM syllabus_items WHERE slug = 'futures'",
        ).fetchone())
        reminder = dict(conn.execute(
            "SELECT * FROM reminders WHERE kind = 'lesson_due'",
        ).fetchone())

    assert lesson["day_number"] == 1
    kinds = [q["kind"] for q in questions]
    assert kinds == ["warmup", "check"]
    assert all(json.loads(q["rubric_json"])["key_points"] for q in questions)
    # New concept got taught + a fresh FSRS card due immediately.
    assert futures["status"] == "taught"
    assert futures["card_json"]
    assert futures["taught_lesson_id"] == lesson["id"]
    # Push reminder queued for the notify backend.
    assert reminder["entity_id"] == "2026-08-16"
    assert reminder["url"] == "/learning"
    # Vault mirror written with a mermaid fence for the diagram section.
    path = Path(lesson["vault_path"])
    assert path.exists()
    content = path.read_text()
    assert "```mermaid" in content
    assert "# Futures and friends" in content


def test_lesson_rejected_when_llm_omits_check_questions(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures"])
    payload = _lesson_payload([])  # no check questions for a new concept
    with patch("mastisk.bridges.intelligence.run_intelligence", new=_llm(payload)):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM lessons").fetchone()["n"]
    assert n == 0


def test_lesson_drops_questions_for_unknown_slugs(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures"])
    payload = _lesson_payload(["futures"])
    payload["check_questions"].append({
        "concept_slug": "hallucinated", "question": "?", "rubric": {"key_points": ["x"]},
    })
    with patch("mastisk.bridges.intelligence.run_intelligence", new=_llm(payload)):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM lesson_questions").fetchone()["n"]
    assert n == 1


def test_rotation_alternates_between_active_goals(db, vault_tmp) -> None:
    with connect() as conn:
        g1 = _mk_goal(conn, topic="Rust", slug="rust")
        g2 = _mk_goal(conn, topic="Options pricing", slug="options")
        _mk_items(conn, g1, ["r1", "r2"])
        _mk_items(conn, g2, ["o1", "o2"])

    def payload_for(slug: str) -> dict:
        return _lesson_payload([slug])

    with patch(
        "mastisk.bridges.intelligence.run_intelligence",
        new=_llm(payload_for("r1")),
    ):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))
    with patch(
        "mastisk.bridges.intelligence.run_intelligence",
        new=_llm(payload_for("o1")),
    ):
        asyncio.run(Guru()._generate_lesson("2026-08-17"))

    with connect() as conn:
        lessons = [dict(r) for r in conn.execute(
            "SELECT * FROM lessons ORDER BY lesson_date",
        )]
    assert lessons[0]["goal_id"] == g1
    assert lessons[1]["goal_id"] == g2  # least-recently-taught goal next


def test_paused_goals_are_never_taught(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn, status="paused")
        _mk_items(conn, goal_id, ["futures"])
    llm = _llm(_lesson_payload(["futures"]))
    with patch("mastisk.bridges.intelligence.run_intelligence", new=llm):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))
    llm.assert_not_awaited()
    with connect() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM lessons").fetchone()["n"]
    assert n == 0


# ───── images + mnemonics ─────


def _payload_with_visuals(check_slugs: list[str]) -> dict:
    payload = _lesson_payload(check_slugs)
    payload["sections"].insert(2, {
        "kind": "image", "heading": "The polling loop",
        "body_md": "Flat vector, plain white background. Two boxes labeled "
                   "'FUTURE' and 'EXECUTOR' joined by a loop arrow. No other text.",
    })
    payload["sections"].append({
        "kind": "mnemonic", "heading": "PEW",
        "body_md": "**P**oll, **E**xecutor, **W**aker — the loop in firing order.",
    })
    return payload


def test_lesson_image_section_materializes_and_mnemonic_persists(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures"])
    gen = AsyncMock(return_value="/api/attachments/deadbeef.png")
    with patch("mastisk.bridges.intelligence.run_intelligence",
               new=_llm(_payload_with_visuals(["futures"]))), \
         patch("mastisk.bridges.imagegen_bridge.available", return_value=True), \
         patch("mastisk.bridges.imagegen_bridge.generate_hero", new=gen):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))

    with connect() as conn:
        lesson = dict(conn.execute("SELECT * FROM lessons").fetchone())
    sections = json.loads(lesson["sections_json"])
    kinds = [s["kind"] for s in sections]
    assert "mnemonic" in kinds
    image = next(s for s in sections if s["kind"] == "image")
    assert image["image_url"] == "/api/attachments/deadbeef.png"
    gen.assert_awaited_once()
    # The imagegen prompt was passed through verbatim.
    assert "No other text" in gen.await_args.args[0]
    # Vault mirror embeds the figure, never the raw imagegen prompt.
    content = Path(lesson["vault_path"]).read_text()
    assert "![The polling loop](/api/attachments/deadbeef.png)" in content
    assert "Flat vector" not in content
    assert "PEW" in content


def test_lesson_image_dropped_when_imagegen_unavailable(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures"])
    with patch("mastisk.bridges.intelligence.run_intelligence",
               new=_llm(_payload_with_visuals(["futures"]))), \
         patch("mastisk.bridges.imagegen_bridge.available", return_value=False):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))
    with connect() as conn:
        lesson = dict(conn.execute("SELECT * FROM lessons").fetchone())
    kinds = [s["kind"] for s in json.loads(lesson["sections_json"])]
    assert "image" not in kinds          # dropped, never a broken figure
    assert "mnemonic" in kinds           # the rest of the lesson is intact
    assert "section" in kinds


def test_lesson_image_dropped_when_generation_fails(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures"])
    gen = AsyncMock(side_effect=RuntimeError("yoyo exploded"))
    with patch("mastisk.bridges.intelligence.run_intelligence",
               new=_llm(_payload_with_visuals(["futures"]))), \
         patch("mastisk.bridges.imagegen_bridge.available", return_value=True), \
         patch("mastisk.bridges.imagegen_bridge.generate_hero", new=gen):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))
    with connect() as conn:
        lesson = dict(conn.execute("SELECT * FROM lessons").fetchone())
    kinds = [s["kind"] for s in json.loads(lesson["sections_json"])]
    assert "image" not in kinds
    assert "section" in kinds  # the lesson itself still shipped


# ───── on-demand visual job ─────


def _mk_lesson(conn, goal_id: int, *, sections: list[dict] | None = None) -> int:
    cur = conn.execute(
        """INSERT INTO lessons (goal_id, lesson_date, day_number, title,
                                sections_json, concept_slugs_json)
           VALUES (?, '2026-08-16', 1, 'Futures and friends', ?, '[]')""",
        (goal_id, json.dumps(sections if sections is not None else [
            {"kind": "section", "heading": "The idea", "body_md": "Plain words."},
        ])),
    )
    return int(cur.lastrowid)


def test_visual_job_appends_generated_figure(db, vault_tmp) -> None:
    with connect() as conn:
        goal_id = _mk_goal(conn)
        lesson_id = _mk_lesson(conn, goal_id)
    llm = _llm({"caption": "How polling works",
                "image_prompt": "Flat vector, white background. No other text."})
    gen = AsyncMock(return_value="/api/attachments/cafe.png")
    with patch("mastisk.bridges.intelligence.run_intelligence", new=llm), \
         patch("mastisk.bridges.imagegen_bridge.available", return_value=True), \
         patch("mastisk.bridges.imagegen_bridge.generate_hero", new=gen):
        asyncio.run(Guru()._generate_visual(lesson_id))

    with connect() as conn:
        lesson = dict(conn.execute(
            "SELECT * FROM lessons WHERE id = ?", (lesson_id,),
        ).fetchone())
    sections = json.loads(lesson["sections_json"])
    assert sections[-1]["kind"] == "image"
    assert sections[-1]["heading"] == "How polling works"
    assert sections[-1]["image_url"] == "/api/attachments/cafe.png"
    # The lesson prompt saw the lesson content, not an imagegen prompt.
    prompt_sent = llm.await_args.args[0]
    assert "Plain words." in prompt_sent


def test_visual_job_respects_image_cap(db, vault_tmp) -> None:
    figs = [
        {"kind": "section", "heading": "x", "body_md": "y"},
        {"kind": "image", "heading": "a", "body_md": "p", "image_url": "/api/attachments/1.png"},
        {"kind": "image", "heading": "b", "body_md": "p", "image_url": "/api/attachments/2.png"},
    ]
    with connect() as conn:
        goal_id = _mk_goal(conn)
        lesson_id = _mk_lesson(conn, goal_id, sections=figs)
    llm = _llm({"caption": "c", "image_prompt": "p"})
    with patch("mastisk.bridges.intelligence.run_intelligence", new=llm), \
         patch("mastisk.bridges.imagegen_bridge.available", return_value=True):
        asyncio.run(Guru()._generate_visual(lesson_id))
    llm.assert_not_awaited()  # cap reached before any LLM spend
    with connect() as conn:
        lesson = dict(conn.execute(
            "SELECT * FROM lessons WHERE id = ?", (lesson_id,),
        ).fetchone())
    assert len(json.loads(lesson["sections_json"])) == 3


def test_validate_lesson_rejects_all_image_lessons_and_caps_images() -> None:
    from mastisk.agents.guru import _validate_lesson

    # All-image lesson → no readable content → rejected.
    only_images = {
        "title": "T",
        "sections": [
            {"kind": "image", "heading": "a", "body_md": "p1"},
        ],
        "warmup_questions": [], "check_questions": [],
    }
    assert _validate_lesson(only_images, new_slugs=set(), warmup_items=[]) is None

    # A chatty model emitting several image sections keeps only the first.
    many = {
        "title": "T",
        "sections": [
            {"kind": "section", "heading": "h", "body_md": "text"},
            {"kind": "image", "heading": "a", "body_md": "p1"},
            {"kind": "image", "heading": "b", "body_md": "p2"},
            {"kind": "image", "heading": "c", "body_md": "p3"},
        ],
        "warmup_questions": [], "check_questions": [],
    }
    out = _validate_lesson(many, new_slugs=set(), warmup_items=[])
    assert [s["kind"] for s in out["sections"]].count("image") == 1
    assert next(s for s in out["sections"] if s["kind"] == "image")["heading"] == "a"


def test_lesson_persists_before_image_render_and_appends_figure(db, vault_tmp) -> None:
    """The text lesson (and its reminder) must exist before the slow image
    render; the figure is appended afterwards."""
    with connect() as conn:
        goal_id = _mk_goal(conn)
        _mk_items(conn, goal_id, ["futures"])

    seen_at_render: dict = {}

    async def slow_gen(prompt, *, timeout_s):
        with connect() as conn:
            row = conn.execute("SELECT id, vault_path FROM lessons").fetchone()
        seen_at_render["lesson_exists"] = row is not None
        return "/api/attachments/late.png"

    with patch("mastisk.bridges.intelligence.run_intelligence",
               new=_llm(_payload_with_visuals(["futures"]))), \
         patch("mastisk.bridges.imagegen_bridge.available", return_value=True), \
         patch("mastisk.bridges.imagegen_bridge.generate_hero", new=slow_gen):
        asyncio.run(Guru()._generate_lesson("2026-08-16"))

    assert seen_at_render["lesson_exists"] is True
    with connect() as conn:
        lesson = dict(conn.execute("SELECT * FROM lessons").fetchone())
    sections = json.loads(lesson["sections_json"])
    assert sections[-1]["kind"] == "image"
    assert sections[-1]["image_url"] == "/api/attachments/late.png"


def test_visual_job_appends_to_existing_mirror_preserving_edits(db, vault_tmp) -> None:
    from mastisk.paths import vault_dir

    folder = vault_dir() / "learning"
    folder.mkdir(parents=True, exist_ok=True)
    mirror = folder / "2026-08-16-rust-async-internals-day1.md"
    mirror.write_text("# My lesson\n\nMY PERSONAL NOTES — do not lose.\n")
    with connect() as conn:
        goal_id = _mk_goal(conn)
        lesson_id = _mk_lesson(conn, goal_id)
        conn.execute("UPDATE lessons SET vault_path = ? WHERE id = ?",
                     (str(mirror), lesson_id))
    llm = _llm({"caption": "The loop", "image_prompt": "p"})
    gen = AsyncMock(return_value="/api/attachments/keep.png")
    with patch("mastisk.bridges.intelligence.run_intelligence", new=llm), \
         patch("mastisk.bridges.imagegen_bridge.available", return_value=True), \
         patch("mastisk.bridges.imagegen_bridge.generate_hero", new=gen):
        asyncio.run(Guru()._generate_visual(lesson_id))
    content = mirror.read_text()
    assert "MY PERSONAL NOTES" in content              # user edits survive
    assert "![The loop](/api/attachments/keep.png)" in content
