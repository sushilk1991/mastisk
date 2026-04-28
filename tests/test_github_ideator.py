"""GithubIdeator tests — mocks both LLM bridges."""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch

import pytest


def _seed_repo_with_context_and_snapshot(db, slug="a/b"):
    db.execute(
        """INSERT INTO repos (slug, owner, name, display_name, description, context_md)
           VALUES (?, ?, ?, ?, 'desc', '# a/b\n\nrolling context here')""",
        (slug, "a", "b", slug),
    )
    db.execute(
        """INSERT INTO repo_snapshots (repo_slug, polled_at, stars_count)
           VALUES (?, '2026-04-22T10:00:00', 10)""",
        (slug,),
    )


_FAKE_IDEAS_JSON = json.dumps([
    {"title": "test-time compute for search", "framing": "recent commit mentions graph traversal...", "angles": ["branch scoring", "beam width"], "tags": ["search"]},
    {"title": "prompt caching opportunity", "framing": "README claims 10x speedup...", "angles": ["measure on llama", "compare vs batching"], "tags": ["perf"]},
])


def test_ideator_writes_ideas_as_notes(db, vault_tmp):
    from mastisk.agents.github_ideator import GithubIdeator
    from mastisk.agents.base import enqueue
    _seed_repo_with_context_and_snapshot(db)
    enqueue("github_ideator", "ideate", {"repo_slug": "a/b"})

    async def fake_claude(**kwargs):
        return {"text": _FAKE_IDEAS_JSON}

    with patch("mastisk.agents.github_ideator.claude_bridge.run_claude", side_effect=fake_claude):
        asyncio.run(GithubIdeator().run_once())

    notes = db.execute("SELECT * FROM notes ORDER BY id").fetchall()
    assert len(notes) == 2
    bodies = [n["body"] for n in notes]
    assert any("test-time compute for search" in b for b in bodies)
    assert any("prompt caching opportunity" in b for b in bodies)
    # All have the preamble
    assert all("> From repo: a/b" in b for b in bodies)
    # source is 'file' (came from agent-generated drop into inbox)
    assert all(n["source"] == "file" for n in notes)

    # idea_runs row recorded
    runs = db.execute("SELECT * FROM repo_idea_runs WHERE repo_slug='a/b'").fetchall()
    assert len(runs) == 1
    assert runs[0]["model"] == "claude"
    note_ids = json.loads(runs[0]["note_ids_json"])
    assert len(note_ids) == 2

    # last_ideated_at updated
    repo = db.execute("SELECT last_ideated_at FROM repos WHERE slug='a/b'").fetchone()
    assert repo["last_ideated_at"] is not None


def test_ideator_falls_back_to_ollama_on_claude_and_codex_failure(db, vault_tmp):
    from mastisk.agents.github_ideator import GithubIdeator
    from mastisk.agents.base import enqueue
    _seed_repo_with_context_and_snapshot(db)
    enqueue("github_ideator", "ideate", {"repo_slug": "a/b"})

    async def claude_fails(**kwargs): raise RuntimeError("quota")
    async def codex_fails(*args, **kwargs): raise RuntimeError("codex down")
    async def ollama_ok(prompt, model):
        return {"text": _FAKE_IDEAS_JSON}

    with patch("mastisk.agents.github_ideator.claude_bridge.run_claude", side_effect=claude_fails), \
         patch("mastisk.agents.github_ideator.codex_bridge.run_codex", side_effect=codex_fails), \
         patch("mastisk.agents.github_ideator.ollama_bridge.run_ollama", side_effect=ollama_ok):
        asyncio.run(GithubIdeator().run_once())

    runs = db.execute("SELECT * FROM repo_idea_runs WHERE repo_slug='a/b'").fetchall()
    assert runs[0]["model"] == "ollama"
    assert json.loads(runs[0]["note_ids_json"])  # non-empty


def test_ideator_uses_codex_when_claude_fails(db, vault_tmp):
    """Claude raises; Codex serves the ideas. ``model`` column says 'codex'.
    Pins the new middle tier between Claude and Ollama."""
    from mastisk.agents.github_ideator import GithubIdeator
    from mastisk.agents.base import enqueue
    _seed_repo_with_context_and_snapshot(db)
    enqueue("github_ideator", "ideate", {"repo_slug": "a/b"})

    async def claude_fails(**kwargs): raise RuntimeError("quota")
    async def codex_ok(*args, **kwargs):
        return {"text": _FAKE_IDEAS_JSON, "raw": _FAKE_IDEAS_JSON}

    async def ollama_should_not_run(*args, **kwargs):  # pragma: no cover
        raise AssertionError("ollama must not be called when codex serves")

    with patch("mastisk.agents.github_ideator.claude_bridge.run_claude", side_effect=claude_fails), \
         patch("mastisk.agents.github_ideator.codex_bridge.run_codex", side_effect=codex_ok), \
         patch("mastisk.agents.github_ideator.ollama_bridge.run_ollama", side_effect=ollama_should_not_run):
        asyncio.run(GithubIdeator().run_once())

    runs = db.execute("SELECT * FROM repo_idea_runs WHERE repo_slug='a/b'").fetchall()
    assert runs[0]["model"] == "codex"
    assert json.loads(runs[0]["note_ids_json"])  # non-empty


def test_ideator_records_error_when_all_models_fail(db, vault_tmp):
    from mastisk.agents.github_ideator import GithubIdeator
    from mastisk.agents.base import enqueue
    _seed_repo_with_context_and_snapshot(db)
    enqueue("github_ideator", "ideate", {"repo_slug": "a/b"})

    async def always_fails(*args, **kwargs): raise RuntimeError("down")

    with patch("mastisk.agents.github_ideator.claude_bridge.run_claude", side_effect=always_fails), \
         patch("mastisk.agents.github_ideator.codex_bridge.run_codex", side_effect=always_fails), \
         patch("mastisk.agents.github_ideator.ollama_bridge.run_ollama", side_effect=always_fails):
        asyncio.run(GithubIdeator().run_once())

    runs = db.execute("SELECT * FROM repo_idea_runs WHERE repo_slug='a/b'").fetchall()
    assert len(runs) == 1
    assert runs[0]["error"] is not None
    assert "all models failed" in runs[0]["error"]
    # Note count 0
    notes = db.execute("SELECT COUNT(*) AS c FROM notes").fetchone()
    assert notes["c"] == 0
    # last_ideated_at NOT updated (so it retries)
    repo = db.execute("SELECT last_ideated_at FROM repos WHERE slug='a/b'").fetchone()
    assert repo["last_ideated_at"] is None


def test_ideator_skips_repo_without_context(db, vault_tmp):
    """A repo that's been added but not yet polled (no context_md) is skipped."""
    from mastisk.agents.github_ideator import GithubIdeator
    from mastisk.agents.base import enqueue
    db.execute("INSERT INTO repos (slug, owner, name, display_name) VALUES ('x/y', 'x', 'y', 'x/y')")
    # No snapshot. No context_md.
    enqueue("github_ideator", "ideate", {"repo_slug": "x/y"})

    async def fake_claude(**kwargs): return {"text": _FAKE_IDEAS_JSON}
    with patch("mastisk.agents.github_ideator.claude_bridge.run_claude", side_effect=fake_claude):
        asyncio.run(GithubIdeator().run_once())

    runs = db.execute("SELECT COUNT(*) AS c FROM repo_idea_runs").fetchone()["c"]
    assert runs == 0
    notes = db.execute("SELECT COUNT(*) AS c FROM notes").fetchone()["c"]
    assert notes == 0


def test_extract_json_array_handles_fenced_block():
    from mastisk.agents.github_ideator import _extract_json_array
    fenced = "```json\n[{\"title\": \"x\"}]\n```"
    assert _extract_json_array(fenced) == [{"title": "x"}]


def test_idea_to_note_body_includes_preamble():
    from mastisk.agents.github_ideator import _idea_to_note_body
    body = _idea_to_note_body("a/b", {
        "title": "foo", "framing": "bar baz",
        "angles": ["a1", "a2"], "tags": ["t1"],
    })
    assert body.startswith("> From repo: a/b")
    assert "# foo" in body
    assert "- a1" in body
    assert "tags: t1" in body
