"""Escalator unit tests. Mocks claude_bridge.run_claude and ollama_bridge.run_ollama."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest


# ─────────────────────────────── fixtures / helpers ───────────────────────────────


CLAUDE_JSON_OK = {
    "text": (
        "```json\n"
        + json.dumps({
            "title": "compounding knowledge",
            "kind": "Concept",
            "framing_paragraph": "Compounding knowledge systems build substrate over time.",
            "research_questions": [
                "What loops compound fastest?",
                "Where do most systems stall?",
                "How to measure compounding velocity?",
            ],
        })
        + "\n```"
    ),
    "raw": {"result": "dummy"},
}


@pytest.fixture
def escalator(db, vault_tmp):
    """A fresh Escalator with vault paths + DB fixture active."""
    from mastisk.paths import ensure_dirs
    ensure_dirs()
    from mastisk.agents.escalator import Escalator
    return Escalator()


def _seed_note(
    db,
    *,
    note_id: int | None = None,
    classification: str = "idea",
    confidence: float = 0.85,
    summary: str = "an idea about compounding knowledge systems",
    body: str | None = None,
    slug: str | None = None,
    created_at: datetime | None = None,
    escalation_state: str = "none",
    escalation_retry_count: int = 0,
    escalation_trigger: str | None = None,
    escalation_next_attempt_at: str | None = None,
    write_file: bool = True,
) -> int:
    """Insert a classified note + its on-disk file. Returns the note id."""
    from mastisk.paths import notes_dir, vault_dir
    ts = created_at or datetime(2026, 4, 21, 14, 35, 22).astimezone()
    if slug is None:
        slug = f"143522-seed-{id(body) if body else note_id or 'n'}"
    date_dir = ts.strftime("%Y-%m-%d")
    rel_path = f"_notes/{date_dir}/{slug}.md"
    effective_body = body if body is not None else (
        "This is a note about compounding knowledge systems that should be long "
        "enough to pass the length rule check. It needs at least eighty characters."
    )
    classified_at = (ts + timedelta(seconds=23)).isoformat()

    cur = db.execute(
        """INSERT INTO notes (
             slug, path, body, body_sha256, source, created_at,
             classified_at, classification, summary, confidence, tags_json,
             escalation_state, escalation_retry_count, escalation_trigger,
             escalation_next_attempt_at
           )
           VALUES (?, ?, ?, ?, 'pwa', ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?)""",
        (
            slug, rel_path, effective_body, "sha-" + slug,
            ts.isoformat(), classified_at, classification, summary,
            confidence, escalation_state, escalation_retry_count,
            escalation_trigger, escalation_next_attempt_at,
        ),
    )
    new_id = cur.lastrowid or 0

    if write_file:
        from mastisk.agents.notetaker import write_frontmatter
        abs_path = vault_dir() / rel_path
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        write_frontmatter(
            abs_path, effective_body,
            {
                "created_at": ts.isoformat(),
                "classified_at": classified_at,
                "classification": classification,
                "summary": summary,
                "confidence": confidence,
                "tags": [],
                "related_articles": [],
                "escalation_state": escalation_state,
            },
        )
    return new_id


def _enqueue_evaluate(note_id: int, *, trigger: str = "auto") -> None:
    from mastisk.agents.base import enqueue
    enqueue("escalator", "evaluate", {"note_id": note_id, "trigger": trigger})


def _patch_claude(return_value=CLAUDE_JSON_OK, side_effect=None):
    """Patch claude_bridge.run_claude inside the escalator module."""
    return patch(
        "mastisk.agents.escalator.claude_bridge.run_claude",
        new_callable=AsyncMock,
        return_value=return_value if side_effect is None else None,
        side_effect=side_effect,
    )


def _patch_ollama(return_value=None, side_effect=None):
    """Patch ollama_bridge.run_ollama inside the escalator module."""
    return patch(
        "mastisk.agents.escalator.ollama_bridge.run_ollama",
        new_callable=AsyncMock,
        return_value=return_value if side_effect is None else None,
        side_effect=side_effect,
    )


def _patch_codex(return_value=None, side_effect=None):
    """Patch codex_bridge.run_codex inside the escalator module.

    Codex sits between Claude and Ollama in the fallback chain. Tests that
    exercised Claude→Ollama directly need to neutralise Codex (default:
    raise) so Ollama is reached.
    """
    if side_effect is None and return_value is None:
        side_effect = RuntimeError("codex unavailable in test")
    return patch(
        "mastisk.agents.escalator.codex_bridge.run_codex",
        new_callable=AsyncMock,
        return_value=return_value if side_effect is None else None,
        side_effect=side_effect,
    )


# ─────────────────────────────── rule-pass / happy path ───────────────────────────────


def test_evaluate_rule_pass_creates_stub(escalator, db, vault_tmp):
    note_id = _seed_note(db)
    _enqueue_evaluate(note_id)
    with _patch_claude() as mock_claude:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 1

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "auto_done"
    assert row["escalation_trigger"] == "auto"
    assert row["escalation_article_id"] is not None
    stub_id = row["escalation_article_id"]

    stub = db.execute("SELECT * FROM articles WHERE id=?", (stub_id,)).fetchone()
    assert stub is not None
    assert stub["updated_by"] == "escalator (stub)"
    assert stub["confidence"] == 0.0
    assert stub["kind"] == "Concept"
    assert stub["source_note_id"] == note_id
    assert "Research questions" in stub["body_md"]
    assert "compounding" in stub["title"].lower()

    # note_escalations row present.
    rows = db.execute(
        "SELECT * FROM note_escalations WHERE note_id=?", (note_id,)
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["result"] == "stub_created"
    assert rows[0]["trigger"] == "auto"
    assert rows[0]["model"] == "claude"

    # Feed row emitted.
    feed = db.execute(
        "SELECT * FROM feed WHERE agent='escalator' AND verb='auto-escalated'"
    ).fetchall()
    assert len(feed) == 1
    assert feed[0]["obj"] == str(note_id)


# ─────────────────────────────── rule failures ───────────────────────────────


def test_evaluate_rule_fails_classification(escalator, db, vault_tmp):
    note_id = _seed_note(db, classification="rant")
    _enqueue_evaluate(note_id)
    with _patch_claude() as mock_claude:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 0

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    # classification fail → 'none' state (still eligible for manual).
    assert row["escalation_state"] == "none"
    assert row["escalation_article_id"] is None
    # escalations row with 'failed' result.
    esc = db.execute(
        "SELECT * FROM note_escalations WHERE note_id=?", (note_id,)
    ).fetchall()
    assert len(esc) == 1
    assert esc[0]["result"] == "failed"


def test_evaluate_rule_fails_confidence(escalator, db, vault_tmp):
    note_id = _seed_note(db, confidence=0.5)
    _enqueue_evaluate(note_id)
    with _patch_claude() as mock_claude:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 0
    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "none"


def test_evaluate_rule_fails_length(escalator, db, vault_tmp):
    note_id = _seed_note(db, body="too short")
    _enqueue_evaluate(note_id)
    with _patch_claude() as mock_claude:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 0
    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "none"


def test_evaluate_rule_fails_daily_cap(escalator, db, vault_tmp):
    """Seed 20 prior stub_created rows for today; the 21st note is skipped_cap."""
    from mastisk.settings import get_settings
    cap = get_settings().notes.auto_escalate_cap
    assert cap == 20

    now = datetime.now().astimezone()
    for i in range(cap):
        prior_id = _seed_note(
            db,
            slug=f"prior-{i:03d}",
            summary=f"unique summary about topic {i} alpha beta gamma",
            body="body body body body body body body body body body body body",
            write_file=False,
        )
        db.execute(
            """INSERT INTO note_escalations
                 (note_id, triggered_at, trigger, result, stub_article_id, model)
               VALUES (?, ?, 'auto', 'stub_created', NULL, 'claude')""",
            (prior_id, now.isoformat()),
        )

    note_id = _seed_note(
        db,
        slug="143522-overflow",
        summary="totally fresh topic zebra xylophone yak unrelated",
    )
    _enqueue_evaluate(note_id)
    with _patch_claude() as mock_claude:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 0

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "skipped_cap"


def test_evaluate_rule_fails_dedup(escalator, db, vault_tmp):
    """A prior successful escalation within dedup_hours with overlapping summary → skipped_dup."""
    # Prior note + successful escalation with similar summary.
    prior_id = _seed_note(
        db, slug="prior-dup",
        summary="compounding knowledge systems build substrate over time alpha beta",
        write_file=False,
    )
    now = datetime.now().astimezone()
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, body_md, summary, source_note_id,
                                 updated_by, confidence)
           VALUES ('stub-prior', 'Concept', 'Prior Stub', 'stub-prior', 'x',
                   'compounding knowledge systems build substrate over time alpha beta',
                   ?, 'escalator (stub)', 0.0)""",
        (prior_id,),
    )
    db.execute(
        """INSERT INTO note_escalations
             (note_id, triggered_at, trigger, result, stub_article_id, model)
           VALUES (?, ?, 'auto', 'stub_created', 'stub-prior', 'claude')""",
        (prior_id, now.isoformat()),
    )

    note_id = _seed_note(
        db, slug="143522-dupish",
        summary="compounding knowledge systems build substrate alpha beta delta",
    )
    _enqueue_evaluate(note_id)
    with _patch_claude() as mock_claude:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 0

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "skipped_dup"


# ─────────────────────────────── manual trigger ───────────────────────────────


def test_manual_trigger_bypasses_rule(escalator, db, vault_tmp):
    """A rant-classified note would fail auto, but manual trigger escalates anyway."""
    note_id = _seed_note(db, classification="rant", confidence=0.2, body="short")
    _enqueue_evaluate(note_id, trigger="manual")
    with _patch_claude() as mock_claude:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 1

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "manual_done"
    assert row["escalation_trigger"] == "manual"
    assert row["escalation_article_id"] is not None

    feed = db.execute(
        "SELECT * FROM feed WHERE agent='escalator' AND verb='escalated'"
    ).fetchall()
    assert len(feed) == 1


# ─────────────────────────────── retry path ───────────────────────────────


def test_claude_error_triggers_retry(escalator, db, vault_tmp):
    """First Claude failure → state='retrying', next_attempt_at set."""
    from mastisk.bridges.claude_bridge import ClaudeError

    note_id = _seed_note(db)
    _enqueue_evaluate(note_id)
    with _patch_claude(side_effect=ClaudeError("boom")) as mock_claude:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 1

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "retrying"
    assert row["escalation_retry_count"] == 1
    assert row["escalation_next_attempt_at"] is not None
    # No stub_created escalations.
    rows = db.execute(
        "SELECT * FROM note_escalations WHERE note_id=?", (note_id,)
    ).fetchall()
    # We don't insert a failed row on retry paths (only on skip/exhausted-fail).
    assert all(r["result"] != "stub_created" for r in rows)


def test_claude_exhausted_falls_back_to_ollama(escalator, db, vault_tmp):
    """After retry budget exhausted, Ollama fallback is invoked and succeeds."""
    from mastisk.bridges.claude_bridge import ClaudeError

    # Seed note already at retry_count = claude_retry_count (=2 by default).
    # One more Claude attempt on this handler call should push new_count=3
    # which exhausts the budget and triggers Ollama.
    note_id = _seed_note(
        db,
        escalation_state="retrying",
        escalation_retry_count=2,
        escalation_trigger="auto",
        escalation_next_attempt_at=(
            datetime.now().astimezone() - timedelta(minutes=1)
        ).isoformat(),
    )
    _enqueue_evaluate(note_id)

    ollama_resp = {
        "text": json.dumps({
            "title": "local fallback",
            "kind": "Concept",
            "framing_paragraph": "Local Ollama built this stub after Claude exhausted.",
            "research_questions": ["q1?", "q2?", "q3?"],
        }),
    }
    with _patch_claude(side_effect=ClaudeError("still broken")) as mock_claude, \
         _patch_codex() as mock_codex, \
         _patch_ollama(return_value=ollama_resp) as mock_ollama:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 1
    assert mock_codex.call_count == 1
    assert mock_ollama.call_count == 1

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "auto_done"
    assert row["escalation_article_id"] is not None

    esc = db.execute(
        "SELECT * FROM note_escalations WHERE note_id=? ORDER BY id DESC LIMIT 1",
        (note_id,),
    ).fetchone()
    assert esc["result"] == "stub_created"
    assert esc["model"] == "ollama"


def test_claude_exhausted_codex_serves_succeeds(escalator, db, vault_tmp):
    """After Claude is exhausted and Codex succeeds, Ollama is never called.
    Pins the Codex tier between Claude and Ollama in the fallback chain."""
    from mastisk.bridges.claude_bridge import ClaudeError

    note_id = _seed_note(
        db,
        escalation_state="retrying",
        escalation_retry_count=2,
        escalation_trigger="auto",
        escalation_next_attempt_at=(
            datetime.now().astimezone() - timedelta(minutes=1)
        ).isoformat(),
    )
    _enqueue_evaluate(note_id)

    codex_resp = {
        "text": (
            "```json\n"
            + json.dumps({
                "title": "codex stub",
                "kind": "Concept",
                "framing_paragraph": "Codex built this stub after Claude exhausted.",
                "research_questions": ["q1?", "q2?", "q3?"],
            })
            + "\n```"
        ),
        "raw": "raw",
    }

    with _patch_claude(side_effect=ClaudeError("still broken")) as mock_claude, \
         _patch_codex(return_value=codex_resp) as mock_codex, \
         _patch_ollama(side_effect=AssertionError("ollama must not be called")) as mock_ollama:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 1
    assert mock_codex.call_count == 1
    assert mock_ollama.call_count == 0

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "auto_done"
    assert row["escalation_article_id"] is not None

    esc = db.execute(
        "SELECT * FROM note_escalations WHERE note_id=? ORDER BY id DESC LIMIT 1",
        (note_id,),
    ).fetchone()
    assert esc["result"] == "stub_created"
    assert esc["model"] == "codex"


def test_ollama_also_fails_state_is_failed(escalator, db, vault_tmp):
    """Both Claude (exhausted) and Ollama fail → state='failed'."""
    from mastisk.bridges.claude_bridge import ClaudeError

    note_id = _seed_note(
        db,
        escalation_state="retrying",
        escalation_retry_count=2,
        escalation_trigger="auto",
        escalation_next_attempt_at=(
            datetime.now().astimezone() - timedelta(minutes=1)
        ).isoformat(),
    )
    _enqueue_evaluate(note_id)

    with _patch_claude(side_effect=ClaudeError("claude down")), \
         _patch_codex(), \
         _patch_ollama(side_effect=RuntimeError("ollama dead")):
        asyncio.run(escalator.run_once())

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "failed"
    # escalations row with result='failed' and model='ollama'.
    esc = db.execute(
        "SELECT * FROM note_escalations WHERE note_id=? ORDER BY id DESC LIMIT 1",
        (note_id,),
    ).fetchone()
    assert esc["result"] == "failed"
    assert esc["model"] == "ollama"
    assert "ollama dead" in (esc["error"] or "")


# ─────────────────────────────── manual-escalate endpoint ───────────────────────────────


def test_post_escalate_endpoint(db, vault_tmp, data_tmp):
    """POST /api/notes/{id}/escalate enqueues a manual-trigger evaluate job."""
    from fastapi.testclient import TestClient

    from mastisk.settings import reload_settings
    reload_settings()
    from mastisk.app import create_app

    note_id = _seed_note(db, classification="rant", confidence=0.1)

    app = create_app()
    with TestClient(app) as client:
        r = client.post(f"/api/notes/{note_id}/escalate")
    assert r.status_code == 202
    body = r.json()
    assert body["note_id"] == note_id
    assert body["escalation_state"] == "pending"

    jobs = db.execute(
        """SELECT * FROM jobs WHERE agent='escalator' AND kind='evaluate'
           ORDER BY id DESC LIMIT 1"""
    ).fetchall()
    assert len(jobs) == 1
    payload = json.loads(jobs[0]["payload_json"])
    assert payload["note_id"] == note_id
    assert payload["trigger"] == "manual"


def test_post_escalate_endpoint_404_on_unknown(db, vault_tmp, data_tmp):
    """POST /api/notes/99999/escalate returns 404 when the note doesn't exist."""
    from fastapi.testclient import TestClient

    from mastisk.settings import reload_settings
    reload_settings()
    from mastisk.app import create_app

    app = create_app()
    with TestClient(app) as client:
        r = client.post("/api/notes/99999/escalate")
    assert r.status_code == 404


# ─────────────────────────── manual re-escalation on terminal state ───────────────────────────


def test_manual_escalate_on_already_done_creates_second_stub(escalator, db, vault_tmp):
    """A note already in ``auto_done`` with a stub can be manually re-escalated (§9.3 step 2).

    Manual trigger bypasses the terminal-state guard in ``_evaluate``. The second
    stub collides on base id (same note_id + same title slug) and falls through
    to the ``-2`` suffix via ``ensure_note_stub_article``'s collision handler.
    """
    # Seed an already-escalated note: auto_done, prior stub article, prior
    # note_escalations row (so dedup would skip a fresh auto-trigger).
    note_id = _seed_note(db, escalation_state="none")  # fresh note_id

    # Hand-craft the existing stub at the id the escalator would compute for
    # title="compounding knowledge" → slug "compounding-knowledge".
    prior_stub_id = f"note-{note_id:06d}-compounding-knowledge"
    db.execute(
        """INSERT INTO articles (id, kind, title, slug, aka_json, summary, body_md,
                                  confidence, reading_minutes, updated_by, vault_path,
                                  source_note_id)
           VALUES (?, 'Concept', 'compounding knowledge', ?, '[]',
                   'prior stub summary', 'prior body', 0.0, 1, 'escalator (stub)', NULL, ?)""",
        (prior_stub_id, prior_stub_id, note_id),
    )
    # Move the note to auto_done referencing that stub.
    now_iso = datetime.now().astimezone().isoformat()
    db.execute(
        """UPDATE notes SET escalation_state='auto_done', escalation_trigger='auto',
                             escalation_article_id=?
           WHERE id=?""",
        (prior_stub_id, note_id),
    )
    db.execute(
        """INSERT INTO note_escalations
             (note_id, triggered_at, trigger, result, stub_article_id, model)
           VALUES (?, ?, 'auto', 'stub_created', ?, 'claude')""",
        (note_id, now_iso, prior_stub_id),
    )

    # Manual escalate — should bypass the terminal-state guard and run Claude.
    _enqueue_evaluate(note_id, trigger="manual")
    with _patch_claude() as mock_claude:
        asyncio.run(escalator.run_once())
    assert mock_claude.call_count == 1

    row = db.execute("SELECT * FROM notes WHERE id=?", (note_id,)).fetchone()
    assert row["escalation_state"] == "manual_done"
    # The note's escalation_article_id now points to the NEW stub (suffix -2).
    assert row["escalation_article_id"] == f"{prior_stub_id}-2"

    # Both stubs exist.
    stubs = db.execute(
        "SELECT id FROM articles WHERE source_note_id=? ORDER BY id",
        (note_id,),
    ).fetchall()
    assert [s["id"] for s in stubs] == [prior_stub_id, f"{prior_stub_id}-2"]

    # Two note_escalations rows (prior auto + new manual).
    escs = db.execute(
        "SELECT trigger, result, stub_article_id FROM note_escalations "
        "WHERE note_id=? ORDER BY id",
        (note_id,),
    ).fetchall()
    assert len(escs) == 2
    assert escs[1]["trigger"] == "manual"
    assert escs[1]["result"] == "stub_created"
    assert escs[1]["stub_article_id"] == f"{prior_stub_id}-2"
