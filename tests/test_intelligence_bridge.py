"""Intelligence-bridge unit tests. Mocks each tier so no subprocess fires.

See src/mastisk/bridges/intelligence.py — the Claude → Codex → Ollama
fallback helper.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest


def _patch_chain(*, claude=None, codex=None, ollama_chat=None):
    """Compose three patches over the intelligence module's tier calls.

    Each kw can be either a return_value or a side_effect callable. ``None``
    leaves the AsyncMock with its default (which raises on call) so a tier
    we don't expect to be invoked still asserts loudly if it is.
    """
    def _mk(value):
        if isinstance(value, BaseException) or (
            isinstance(value, type) and issubclass(value, BaseException)
        ):
            return AsyncMock(side_effect=value)
        if callable(value):
            return AsyncMock(side_effect=value)
        if value is None:
            return AsyncMock(side_effect=AssertionError("tier should not be called"))
        return AsyncMock(return_value=value)

    return (
        patch(
            "mastisk.bridges.intelligence.claude_bridge.run_claude",
            new=_mk(claude),
        ),
        patch(
            "mastisk.bridges.intelligence.codex_bridge.run_codex",
            new=_mk(codex),
        ),
        patch(
            "mastisk.bridges.intelligence.ollama_bridge.chat",
            new=_mk(ollama_chat),
        ),
    )


def test_claude_success_returns_claude_label():
    """Claude returns ok → result + 'claude' label, codex/ollama untouched."""
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        claude={"text": "ok"},
        # codex/ollama default to AssertionError-on-call
    )
    with p_claude as m_c, p_codex as m_x, p_ollama as m_o:
        result, label = asyncio.run(intelligence.run_intelligence("hello"))
    assert label == "claude"
    assert result == {"text": "ok"}
    assert m_c.call_count == 1
    assert m_x.call_count == 0
    assert m_o.call_count == 0


def test_claude_fail_codex_success_returns_codex_label():
    """Claude raises → Codex returns ok → result + 'codex' label."""
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        claude=RuntimeError("claude down"),
        codex={"text": "from-codex", "raw": "from-codex"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama as m_o:
        result, label = asyncio.run(intelligence.run_intelligence("hello"))
    assert label == "codex"
    assert result["text"] == "from-codex"
    assert m_c.call_count == 1
    assert m_x.call_count == 1
    assert m_o.call_count == 0


def test_both_cloud_fail_falls_back_to_ollama():
    """Claude + Codex raise → Ollama serves → 'ollama' label."""
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        claude=RuntimeError("claude down"),
        codex=RuntimeError("codex down"),
        ollama_chat="ollama-text",
    )
    with p_claude as m_c, p_codex as m_x, p_ollama as m_o:
        result, label = asyncio.run(intelligence.run_intelligence("hello"))
    assert label == "ollama"
    assert result == {"text": "ollama-text", "raw": "ollama-text"}
    assert m_c.call_count == 1
    assert m_x.call_count == 1
    assert m_o.call_count == 1


def test_all_three_fail_raises_with_full_chain_context():
    """All three raise → IntelligenceUnavailable carries every tier's
    error, not just the Ollama one. Without the chain context, operators
    chase the wrong layer when (e.g.) Codex was the real root cause and
    Ollama just happens to also be down."""
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        claude=RuntimeError("claude down"),
        codex=RuntimeError("codex down"),
        ollama_chat=RuntimeError("ollama dead"),
    )
    with p_claude, p_codex, p_ollama, pytest.raises(
        intelligence.IntelligenceUnavailable
    ) as exc_info:
        asyncio.run(intelligence.run_intelligence("hello"))
    msg = str(exc_info.value)
    assert "claude=claude down" in msg
    assert "codex=codex down" in msg
    assert "ollama=ollama dead" in msg


def test_timeout_kwarg_is_forwarded_to_each_tier():
    """timeout_s passes through as int to claude, float to codex.

    Pins the cast: codex_bridge.run_codex uses ``timeout`` (float seconds),
    not ``timeout_s``. Mismatching here would silently break the codex tier.
    """
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        claude=RuntimeError("nope"),
        codex={"text": "ok", "raw": "ok"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama:
        asyncio.run(intelligence.run_intelligence("hi", timeout_s=42))

    # Claude was called with the kwargs we expected.
    assert m_c.call_args.kwargs.get("timeout_s") == 42
    # Codex got float(timeout_s) under the kw name 'timeout'.
    assert m_x.call_args.kwargs.get("timeout") == pytest.approx(42.0)
    assert isinstance(m_x.call_args.kwargs.get("timeout"), float)


def test_classification_mode_is_forwarded_to_claude_only():
    """Classification mode constrains Claude; fallback tiers keep their existing API."""
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        claude=RuntimeError("nope"),
        codex={"text": "ok", "raw": "ok"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama:
        asyncio.run(intelligence.run_intelligence("hi", classification=True))

    assert m_c.call_args.kwargs["classification"] is True
    assert "classification" not in m_x.call_args.kwargs
