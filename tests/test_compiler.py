"""Compiler unit tests. Covers the enrich_stub path (escalator stub → full article)."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest

COMPILER_ENRICH_JSON = {
    "text": (
        "```json\n"
        + json.dumps({
            "skip": False,
            "id": "WILL-BE-OVERWRITTEN",
            "kind": "Concept",
            "title": "Compounding Knowledge Systems",
            "aka": ["compound knowledge"],
            "summary": "Systems that build substrate over time so each iteration runs farther than the last.",
            "confidence": 0.7,
            "reading_minutes": 4,
            "sections": [
                {"h": "TL;DR", "kind": "callout",
                 "body": "<p>Compounding knowledge is about <em>persistent substrate</em>.</p>"},
                {"h": "Mechanism",
                 "body": '<p>Connects to <span class="link" data-target="agent-orchestration">agent orchestration</span>.</p>'},
            ],
            "related": [],
        })
        + "\n```"
    ),
    "raw": {"result": "dummy"},
}


@pytest.fixture
def compiler(db, vault_tmp):
    from mastisk.paths import ensure_dirs
    ensure_dirs()
    from mastisk.agents.compiler import Compiler
    return Compiler()


def _seed_stub(db, *, stub_id: str, note_id: int, title: str = "Compounding") -> None:
    """Mirror what escalator.ensure_note_stub_article writes."""
    # Insert a minimal note row (the enrich path reads body).
    db.execute(
        """INSERT INTO notes (id, slug, path, body, body_sha256, source, created_at,
                              classification, summary, confidence, tags_json,
                              escalation_state, escalation_retry_count)
           VALUES (?, ?, ?, ?, ?, 'pwa', '2026-04-21T14:35:22+00:00',
                   'idea', 'idea about compounding', 0.85, '[]', 'auto_done', 0)""",
        (note_id, f"seed-{note_id}", f"_notes/seed-{note_id}.md",
         "Compounding knowledge systems build substrate over time. " * 3,
         f"sha-{note_id}"),
    )
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, aka_json, summary, body_md,
                                 confidence, reading_minutes, updated_by, vault_path,
                                 source_note_id)
           VALUES (?, 'Concept', ?, ?, '[]', 'placeholder summary', 'placeholder body',
                   0.0, 1, 'escalator (stub)', NULL, ?)""",
        (stub_id, title, stub_id, note_id),
    )


def _enqueue_enrich(db, *, article_id: str, note_id: int) -> int:
    cur = db.execute(
        "INSERT INTO jobs (agent, kind, payload_json) VALUES ('compiler', 'enrich_stub', ?)",
        (json.dumps({"article_id": article_id, "note_id": note_id}),),
    )
    return cur.lastrowid


def _patch_intelligence(return_value=COMPILER_ENRICH_JSON):
    return patch(
        "mastisk.agents.compiler.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=(return_value, "claude"),
    )


def test_schema_guidance_requires_short_single_clause_titles():
    """Compiler titles must stay digest-sized before the persist guard runs."""
    from mastisk.agents.compiler import SCHEMA_MD
    from mastisk.db.queries import MAX_GENERATED_ARTICLE_TITLE_CHARS

    assert "single clause" in SCHEMA_MD
    assert f"{MAX_GENERATED_ARTICLE_TITLE_CHARS} characters" in SCHEMA_MD
    assert "No subtitles" in SCHEMA_MD
    assert "no em-dash appendages" in SCHEMA_MD


def test_schema_guidance_covers_depth_and_diagrams():
    """The rewritten schema must ask for depth and Mermaid diagram sections."""
    from mastisk.agents.compiler import SCHEMA_MD

    assert '"kind": "diagram"' in SCHEMA_MD
    assert "Mermaid" in SCHEMA_MD
    assert "700" in SCHEMA_MD  # depth target present


def test_sections_to_md_fences_diagrams():
    from mastisk.agents.compiler import _sections_to_md

    md = _sections_to_md([
        {"h": "Intro", "body": "<p>Hello <em>world</em></p>"},
        {"h": "Flow", "kind": "diagram", "body": "flowchart TD\n  A --> B"},
    ])
    assert "```mermaid\nflowchart TD\n  A --> B\n```" in md
    assert "Hello *world*" in md


def test_run_for_json_retries_once_on_malformed_response(compiler, db, vault_tmp):
    from unittest.mock import patch as _patch

    responses = iter([
        ({"text": "sorry, no json here"}, "codex"),
        (COMPILER_ENRICH_JSON, "codex"),
    ])

    async def fake_run(*a, **kw):
        return next(responses)

    with _patch("mastisk.agents.compiler.intelligence.run_intelligence", new=fake_run):
        data, provider = asyncio.run(compiler._run_for_json("p", label="test"))
    assert provider == "codex"
    assert data and data["title"] == "Compounding Knowledge Systems"


def test_normalize_article_data_drops_malformed_entries():
    """Prod failure 2026-07-12: a bare string in sections crashed the compile."""
    from mastisk.agents.compiler import _normalize_article_data

    data = {
        "sections": [
            {"h": "Ok", "body": "<p>fine</p>"},
            "a stray string section",
            {"h": "No body", "body": None},
        ],
        "related": [{"id": "x", "weight": 0.5}, "stray", {"label": "no id"}],
        "aka": ["alias", 42],
    }
    _normalize_article_data(data)
    assert data["sections"] == [{"h": "Ok", "body": "<p>fine</p>"}]
    assert data["related"] == [{"id": "x", "weight": 0.5}]
    assert data["aka"] == ["alias"]


def test_strip_html_preserves_paragraph_boundaries():
    from mastisk.agents.compiler import _strip_html

    md = _strip_html("<p>One ends here.</p><p>Two starts here.</p>")
    assert md == "One ends here.\n\nTwo starts here."


def test_maybe_generate_hero_skips_when_article_already_has_hero(compiler, db, vault_tmp):
    from unittest.mock import patch as _patch

    db.execute(
        """INSERT INTO articles (id, kind, title, slug, hero_image_url, updated_by)
           VALUES ('has-hero', 'Concept', 'Has Hero', 'has-hero',
                   '/api/attachments/old.png', 'Compiler')""",
    )
    gen = AsyncMock(return_value="/api/attachments/new.png")
    with (
        _patch("mastisk.agents.compiler.imagegen_bridge.available", return_value=True),
        _patch("mastisk.agents.compiler.imagegen_bridge.generate_hero", new=gen),
    ):
        url = asyncio.run(compiler._maybe_generate_hero(
            {"id": "has-hero", "title": "Has Hero", "summary": "s"},
        ))
    assert url is None  # recompile must not regenerate or spend a cap slot
    assert gen.call_count == 0


def test_maybe_generate_hero_off_when_yoyo_missing(compiler, db, vault_tmp, data_tmp):
    from unittest.mock import patch as _patch

    with _patch(
        "mastisk.agents.compiler.imagegen_bridge.available", return_value=False,
    ):
        url = asyncio.run(compiler._maybe_generate_hero({"id": "x", "title": "X"}))
    assert url is None


def test_maybe_generate_hero_respects_daily_cap(compiler, db, vault_tmp, data_tmp):
    from unittest.mock import patch as _patch

    from mastisk.settings import reload_settings

    (data_tmp / "config.toml").write_text(
        "[compiler]\nhero_images_daily_cap = 1\n", encoding="utf-8",
    )
    reload_settings()

    gen = AsyncMock(return_value="/api/attachments/abc123.png")
    with (
        _patch("mastisk.agents.compiler.imagegen_bridge.available", return_value=True),
        _patch("mastisk.agents.compiler.imagegen_bridge.generate_hero", new=gen),
    ):
        first = asyncio.run(compiler._maybe_generate_hero(
            {"id": "a", "title": "A", "summary": "s"},
        ))
        second = asyncio.run(compiler._maybe_generate_hero(
            {"id": "b", "title": "B", "summary": "s"},
        ))
    assert first == "/api/attachments/abc123.png"
    assert second is None  # cap of 1 reached via the 'illustrated' feed row
    assert gen.call_count == 1


def test_enrich_stub_overwrites_placeholder_in_place(compiler, db, vault_tmp):
    stub_id = "note-000044-compounding"
    note_id = 44
    _seed_stub(db, stub_id=stub_id, note_id=note_id)
    _enqueue_enrich(db, article_id=stub_id, note_id=note_id)

    with _patch_intelligence() as mock_int:
        asyncio.run(compiler.run_once())
    assert mock_int.call_count == 1

    # Stub upgraded in place — same id, but updated_by/confidence/sections/etc filled.
    row = db.execute("SELECT * FROM articles WHERE id=?", (stub_id,)).fetchone()
    assert row is not None
    assert row["updated_by"] == "Compiler"
    assert row["confidence"] == 0.7
    assert row["title"] == "Compounding Knowledge Systems"
    assert row["summary"].startswith("Systems that build substrate")
    # source_note_id back-reference preserved by upsert (it doesn't touch that column).
    assert row["source_note_id"] == note_id
    assert row["vault_path"] is not None

    # Sections persisted.
    sections = db.execute(
        "SELECT heading FROM article_sections WHERE article_id=? ORDER BY idx", (stub_id,),
    ).fetchall()
    assert [s["heading"] for s in sections] == ["TL;DR", "Mechanism"]

    # Body-referenced target is gated now (stub_gate_min_sources=2 default):
    # first reference records a pending suggestion instead of minting a stub.
    target = db.execute(
        "SELECT id FROM articles WHERE id='agent-orchestration'",
    ).fetchone()
    assert target is None
    suggestion = db.execute(
        "SELECT slug, occurrences, status FROM wiki_suggestions WHERE slug='agent-orchestration'",
    ).fetchone()
    assert suggestion is not None
    assert suggestion["status"] == "pending"
    assert suggestion["occurrences"] == 1

    # Feed row emitted.
    feed = db.execute(
        "SELECT * FROM feed WHERE agent='compiler' AND verb='enriched'",
    ).fetchall()
    assert len(feed) == 1


def test_enrich_stub_forces_id_even_if_model_picks_different_slug(compiler, db, vault_tmp):
    stub_id = "note-000099-original-id"
    note_id = 99
    _seed_stub(db, stub_id=stub_id, note_id=note_id)
    _enqueue_enrich(db, article_id=stub_id, note_id=note_id)

    # Model returns a different "id" — handler must force it back to stub_id.
    with _patch_intelligence() as _:
        asyncio.run(compiler.run_once())

    # Original stub id still exists and is enriched; no rogue article was created.
    row = db.execute("SELECT updated_by FROM articles WHERE id=?", (stub_id,)).fetchone()
    assert row["updated_by"] == "Compiler"
    rogue = db.execute(
        "SELECT id FROM articles WHERE id='WILL-BE-OVERWRITTEN'",
    ).fetchone()
    assert rogue is None


def test_enrich_stub_skipped_when_article_missing(compiler, db, vault_tmp):
    """If the stub was deleted between enqueue and run, log + bail (no crash)."""
    _enqueue_enrich(db, article_id="nonexistent-stub", note_id=1)

    with _patch_intelligence() as mock_int:
        asyncio.run(compiler.run_once())
    # Should bail before calling the LLM.
    assert mock_int.call_count == 0

    # Job marked done so it doesn't loop forever.
    job = db.execute("SELECT status FROM jobs WHERE kind='enrich_stub'").fetchone()
    assert job["status"] == "done"
