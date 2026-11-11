"""Roundtable agent tests. All four bridges are mocked so no real LLM calls
happen. See src/mastisk/agents/roundtable.py + spec §§7, 8, 11, 12."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest

# ─────────────────────────────── helpers ───────────────────────────────


def _seed_roundtable(
    db,
    *,
    input_type: str = "prompt",
    input_ref: str = "",
    prompt: str = "What's the deal with compounding?",
    status: str = "pending",
) -> int:
    """Insert a roundtable row and return its id."""
    cur = db.execute(
        "INSERT INTO roundtables (input_type, input_ref, prompt, status) VALUES (?, ?, ?, ?)",
        (input_type, input_ref, prompt, status),
    )
    return cur.lastrowid or 0


def _enqueue_fan_out(rt_id: int) -> int:
    """Enqueue the fan_out job that _handle consumes."""
    from mastisk.agents.base import enqueue

    return enqueue("roundtable", "fan_out", {"roundtable_id": rt_id})


def _patch_all_bridges_ok():
    """Patch every bridge to return a canned successful perspective."""

    async def _fake_claude(prompt, *args, **kwargs):
        return {"text": "claude's take on the question"}

    async def _fake_codex(prompt, *, model=None, timeout=120.0):
        return {"text": "codex's take on the question"}

    async def _fake_gemini(prompt, *, model=None, timeout=120.0):
        return {"text": "gemini's take on the question"}

    async def _fake_ollama(prompt, model, **kwargs):
        return {"text": "ollama's take on the question"}

    return (
        patch(
            "mastisk.agents.roundtable.claude_bridge.run_claude",
            new_callable=AsyncMock, side_effect=_fake_claude,
        ),
        patch(
            "mastisk.agents.roundtable.codex_bridge.run_codex",
            new_callable=AsyncMock, side_effect=_fake_codex,
        ),
        patch(
            "mastisk.agents.roundtable.gemini_bridge.run_gemini",
            new_callable=AsyncMock, side_effect=_fake_gemini,
        ),
        patch(
            "mastisk.agents.roundtable.ollama_bridge.run_ollama",
            new_callable=AsyncMock, side_effect=_fake_ollama,
        ),
    )


@pytest.fixture
def agent(db, vault_tmp, data_tmp):
    """Fresh Roundtable with vault + DB fixtures active."""
    from mastisk.paths import ensure_dirs
    ensure_dirs()
    from mastisk.agents.roundtable import Roundtable
    return Roundtable()


# ─────────────────────────────── happy path ───────────────────────────────


def test_roundtable_happy_path(agent, db):
    """All four bridges succeed → 4 perspective rows, status=done, synthesis set."""
    rt_id = _seed_roundtable(db)
    _enqueue_fan_out(rt_id)

    p_claude, p_codex, p_gemini, p_ollama = _patch_all_bridges_ok()
    with p_claude as mc, p_codex as mx, p_gemini as mg, p_ollama as mo:
        asyncio.run(agent.run_once())
    # Claude fires twice: once for its perspective, once for synthesis.
    assert mc.call_count == 2
    assert mx.call_count == 1
    assert mg.call_count == 1
    assert mo.call_count == 1

    rt = db.execute(
        "SELECT * FROM roundtables WHERE id=?", (rt_id,)
    ).fetchone()
    assert rt["status"] == "done"
    assert rt["finished_at"] is not None
    assert rt["synthesis"] == "claude's take on the question"
    assert rt["synthesis_model"] == "claude"

    perspectives = db.execute(
        "SELECT backend, content, error, latency_ms FROM roundtable_perspectives "
        "WHERE roundtable_id=? ORDER BY backend",
        (rt_id,),
    ).fetchall()
    assert [p["backend"] for p in perspectives] == [
        "claude", "codex", "gemini", "ollama",
    ]
    assert all(p["error"] is None for p in perspectives)
    assert all(p["content"] and "take on the question" in p["content"] for p in perspectives)
    # latency_ms should be non-null (int, may be 0 for an AsyncMock).
    assert all(p["latency_ms"] is not None for p in perspectives)

    # Feed row.
    feed = db.execute(
        "SELECT * FROM feed WHERE agent='roundtable' AND verb='roundtable-done'"
    ).fetchall()
    assert len(feed) == 1
    assert feed[0]["obj"] == str(rt_id)
    payload = json.loads(feed[0]["payload_json"] or "{}")
    assert payload["perspective_count"] == 4
    assert payload["synthesis_model"] == "claude"


# ─────────────────────────────── partial failure ───────────────────────────────


def test_one_backend_fails(agent, db):
    """Codex raises → 3 successful + 1 error row; status=done; synthesis still runs."""
    rt_id = _seed_roundtable(db)
    _enqueue_fan_out(rt_id)

    async def _fake_claude(prompt, *args, **kwargs):
        return {"text": "claude took"}

    async def _fake_codex_fail(prompt, *, model=None, timeout=120.0):
        from mastisk.bridges.codex_bridge import CodexError
        raise CodexError("codex binary exploded")

    async def _fake_gemini(prompt, *, model=None, timeout=120.0):
        return {"text": "gemini took"}

    async def _fake_ollama(prompt, model, **kwargs):
        return {"text": "ollama took"}

    with patch(
        "mastisk.agents.roundtable.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=_fake_claude,
    ), patch(
        "mastisk.agents.roundtable.codex_bridge.run_codex",
        new_callable=AsyncMock, side_effect=_fake_codex_fail,
    ), patch(
        "mastisk.agents.roundtable.gemini_bridge.run_gemini",
        new_callable=AsyncMock, side_effect=_fake_gemini,
    ), patch(
        "mastisk.agents.roundtable.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=_fake_ollama,
    ):
        asyncio.run(agent.run_once())

    rt = db.execute(
        "SELECT status, synthesis, synthesis_model FROM roundtables WHERE id=?",
        (rt_id,),
    ).fetchone()
    # Synthesis still runs because 3 succeeded.
    assert rt["status"] == "done"
    assert rt["synthesis"] == "claude took"
    assert rt["synthesis_model"] == "claude"

    rows = db.execute(
        "SELECT backend, content, error FROM roundtable_perspectives "
        "WHERE roundtable_id=? ORDER BY backend",
        (rt_id,),
    ).fetchall()
    by_backend = {r["backend"]: r for r in rows}
    assert by_backend["codex"]["error"] is not None
    assert "CodexError" in by_backend["codex"]["error"]
    assert by_backend["codex"]["content"] is None
    for b in ("claude", "gemini", "ollama"):
        assert by_backend[b]["error"] is None
        assert by_backend[b]["content"]


# ─────────────────────────────── total failure ───────────────────────────────


def test_all_backends_fail(agent, db):
    """Every bridge raises → status=failed, error='all backends failed', no synthesis call."""
    rt_id = _seed_roundtable(db)
    _enqueue_fan_out(rt_id)

    async def _raise(*a, **kw):
        raise RuntimeError("backend down")

    with patch(
        "mastisk.agents.roundtable.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=_raise,
    ) as mc, patch(
        "mastisk.agents.roundtable.codex_bridge.run_codex",
        new_callable=AsyncMock, side_effect=_raise,
    ), patch(
        "mastisk.agents.roundtable.gemini_bridge.run_gemini",
        new_callable=AsyncMock, side_effect=_raise,
    ), patch(
        "mastisk.agents.roundtable.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=_raise,
    ):
        asyncio.run(agent.run_once())
    # Claude called exactly once — for perspective, not synthesis (all-fail
    # path skips the synthesize call).
    assert mc.call_count == 1

    rt = db.execute(
        "SELECT status, error, synthesis FROM roundtables WHERE id=?",
        (rt_id,),
    ).fetchone()
    assert rt["status"] == "failed"
    assert rt["error"] == "all backends failed"
    assert rt["synthesis"] is None

    # All four perspectives recorded with errors.
    rows = db.execute(
        "SELECT backend, error FROM roundtable_perspectives "
        "WHERE roundtable_id=? ORDER BY backend",
        (rt_id,),
    ).fetchall()
    assert len(rows) == 4
    assert all(r["error"] for r in rows)

    feed = db.execute(
        "SELECT * FROM feed WHERE agent='roundtable' AND verb='roundtable-failed'"
    ).fetchall()
    assert len(feed) == 1


# ─────────────────────────────── Claude synthesis fallback ───────────────────────────────


def test_claude_synthesis_fallback_to_ollama(agent, db):
    """Perspectives succeed, but Claude fails at synthesis → fall back to Ollama."""
    rt_id = _seed_roundtable(db)
    _enqueue_fan_out(rt_id)

    # Counter so Claude succeeds for perspective (call 1) then fails (call 2 = synthesis).
    claude_calls = {"n": 0}

    async def _fake_claude(prompt, *args, **kwargs):
        claude_calls["n"] += 1
        if claude_calls["n"] == 1:
            return {"text": "claude's perspective"}
        raise RuntimeError("claude quota exhausted")

    async def _fake_codex(prompt, *, model=None, timeout=120.0):
        return {"text": "codex took"}

    async def _fake_gemini(prompt, *, model=None, timeout=120.0):
        return {"text": "gemini took"}

    # Ollama must serve both the perspective AND synthesis.
    async def _fake_ollama(prompt, model, **kwargs):
        return {"text": f"ollama-from-{model}"}

    with patch(
        "mastisk.agents.roundtable.claude_bridge.run_claude",
        new_callable=AsyncMock, side_effect=_fake_claude,
    ) as mc, patch(
        "mastisk.agents.roundtable.codex_bridge.run_codex",
        new_callable=AsyncMock, side_effect=_fake_codex,
    ), patch(
        "mastisk.agents.roundtable.gemini_bridge.run_gemini",
        new_callable=AsyncMock, side_effect=_fake_gemini,
    ), patch(
        "mastisk.agents.roundtable.ollama_bridge.run_ollama",
        new_callable=AsyncMock, side_effect=_fake_ollama,
    ) as mo:
        asyncio.run(agent.run_once())

    assert mc.call_count == 2  # perspective + synthesis attempt
    assert mo.call_count == 2  # perspective + synthesis fallback

    rt = db.execute(
        "SELECT status, synthesis, synthesis_model FROM roundtables WHERE id=?",
        (rt_id,),
    ).fetchone()
    assert rt["status"] == "done"
    assert rt["synthesis_model"] == "ollama"
    assert rt["synthesis"] and "ollama-from" in rt["synthesis"]


# ─────────────────────────────── context builder ───────────────────────────────


def test_build_context_for_note(agent, db):
    """_build_context('note', str(id)) returns body + summary + classification."""
    # Seed a note.
    db.execute(
        """INSERT INTO notes (
             slug, path, body, body_sha256, source, created_at,
             classification, summary, confidence, tags_json
           )
           VALUES ('s', '_notes/inbox/s.md', 'the full note body here', 'sha',
                   'pwa', ?, 'idea', 'a short summary of the note', 0.8, '[]')""",
        (datetime(2026, 4, 22, 10).isoformat(),),
    )
    note_id = db.execute("SELECT id FROM notes WHERE slug='s'").fetchone()["id"]

    ctx = asyncio.run(agent._build_context("note", str(note_id)))
    assert "the full note body here" in ctx
    assert "a short summary of the note" in ctx
    assert "idea" in ctx
    assert f"Note #{note_id}" in ctx


def test_build_context_for_prompt_is_empty(agent, db):
    ctx = asyncio.run(agent._build_context("prompt", ""))
    assert ctx == ""


def test_build_context_for_unknown_note_returns_empty(agent, db):
    """Input references a non-existent note id → empty string, not crash."""
    ctx = asyncio.run(agent._build_context("note", "99999"))
    assert ctx == ""


# ─────────────────────────────── terminal-status guard ───────────────────────────────


def test_terminal_status_is_skipped(agent, db):
    """A roundtable already in status='done' is not reprocessed."""
    rt_id = _seed_roundtable(db, status="done")
    # Simulate the prior run having written a synthesis.
    db.execute(
        "UPDATE roundtables SET synthesis=?, synthesis_model=? WHERE id=?",
        ("prior synthesis", "claude", rt_id),
    )
    _enqueue_fan_out(rt_id)

    p_claude, p_codex, p_gemini, p_ollama = _patch_all_bridges_ok()
    with p_claude as mc, p_codex as mx, p_gemini as mg, p_ollama as mo:
        asyncio.run(agent.run_once())

    # No bridge should have been called.
    assert mc.call_count == 0
    assert mx.call_count == 0
    assert mg.call_count == 0
    assert mo.call_count == 0

    # Synthesis unchanged.
    rt = db.execute(
        "SELECT status, synthesis FROM roundtables WHERE id=?", (rt_id,)
    ).fetchone()
    assert rt["status"] == "done"
    assert rt["synthesis"] == "prior synthesis"

    # No perspectives inserted.
    n = db.execute(
        "SELECT COUNT(*) AS n FROM roundtable_perspectives WHERE roundtable_id=?",
        (rt_id,),
    ).fetchone()["n"]
    assert n == 0
