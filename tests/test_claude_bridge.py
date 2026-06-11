"""Unit tests for claude_bridge helpers (no subprocess fired).

The interesting helper here is ``extract_json_block`` — it has to tolerate
both fenced ``` ```json {...} ``` ``` blocks (Claude's documented contract)
AND naked ``{...}`` (Claude Code's recent versions emit this often despite
fence-enforcing prompts; Codex and Ollama emit it routinely). Without the
naked fallback every caller had to wrap the extractor with the same retry
chain, which was the literal source of "every escalation fails because
Claude responses don't have a fence" bugs.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from mastisk.bridges.claude_bridge import extract_json_block


def test_extracts_fenced_json_block():
    """The original happy path: ``` ```json {...} ``` ``` is extracted."""
    text = '```json\n{"a": 1, "b": "two"}\n```'
    assert extract_json_block(text) == {"a": 1, "b": "two"}


def test_extracts_unlabeled_fenced_block():
    """Plain ``` ``` ``` (no ``json`` after the opener) also works."""
    text = '```\n{"x": true}\n```'
    assert extract_json_block(text) == {"x": True}


def test_extracts_naked_braces():
    """No fence at all → outermost braces span is parsed. This is the
    case that made every escalator Claude call fail before the fix."""
    text = 'Here is the JSON: {"ok": true}'
    assert extract_json_block(text) == {"ok": True}


def test_extracts_naked_braces_with_surrounding_prose():
    """Realistic Claude-Code response shape with prose around the JSON."""
    text = 'I will return JSON.\n\n{"classification": "idea", "confidence": 0.9}\n\nLet me know if you need more.'
    assert extract_json_block(text) == {"classification": "idea", "confidence": 0.9}


def test_falls_back_to_naked_when_fence_is_malformed():
    """Pin this: a fence is present but unclosed → fall back to naked-
    braces. Without this the extractor would return None and callers would
    treat valid output as a failure."""
    text = '```json\n{"a": 1}'  # missing closing ```
    assert extract_json_block(text) == {"a": 1}


def test_returns_none_on_truly_no_json():
    """No fence, no braces → None (caller treats as parse failure)."""
    assert extract_json_block("nothing parseable here") is None


def test_returns_none_on_invalid_json_inside_braces():
    """Naked-braces span is taken but the slice doesn't parse → None."""
    assert extract_json_block("text {not actually json}") is None


def test_naked_braces_handles_nested_objects():
    """Outermost {...} span captures nested objects correctly."""
    text = 'Result: {"outer": {"inner": [1, 2]}}'
    assert extract_json_block(text) == {"outer": {"inner": [1, 2]}}


@pytest.mark.asyncio
async def test_run_claude_classification_uses_no_tools_plan_mode(data_tmp):
    from mastisk.bridges.claude_bridge import run_claude
    from mastisk.settings import reload_settings

    reload_settings()
    mock_proc = AsyncMock()
    mock_proc.returncode = 0
    mock_proc.communicate.return_value = (b'{"result": "{\\"ok\\": true}"}', b"")
    captured_args: list[str] = []

    async def fake_exec(*args, **kwargs):
        captured_args.extend(args)
        return mock_proc

    with (
        patch("mastisk.bridges.claude_bridge._resolve_cmd", return_value="/usr/bin/claude"),
        patch(
            "mastisk.bridges.claude_bridge.asyncio.create_subprocess_exec",
            side_effect=fake_exec,
        ),
    ):
        result = await run_claude("classify this", classification=True)

    assert result["text"] == '{"ok": true}'
    mode_idx = captured_args.index("--permission-mode")
    assert captured_args[mode_idx + 1] == "plan"
    tools_idx = captured_args.index("--tools")
    assert captured_args[tools_idx + 1] == ""
