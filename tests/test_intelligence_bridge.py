"""Intelligence-bridge unit tests. Mocks each tier so no subprocess fires."""
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


def _reload_settings() -> None:
    from mastisk.settings import reload_settings

    reload_settings()


def test_default_provider_order_is_codex_first(data_tmp):
    """The default chain starts with Codex, then falls back to Claude/Ollama."""
    _reload_settings()
    from mastisk.settings import get_settings

    assert get_settings().intelligence.provider_order == [
        "codex",
        "claude",
        "ollama",
    ]


def test_default_codex_success_returns_codex_label(data_tmp):
    """Codex returns ok → result + 'codex' label, claude/ollama untouched."""
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        codex={"text": "ok", "raw": "ok"},
        # claude/ollama default to AssertionError-on-call
    )
    with p_claude as m_c, p_codex as m_x, p_ollama as m_o:
        result, label = asyncio.run(intelligence.run_intelligence("hello"))
    assert label == "codex"
    assert result == {"text": "ok", "raw": "ok"}
    assert m_x.call_count == 1
    assert m_c.call_count == 0
    assert m_o.call_count == 0


def test_codex_fail_claude_success_returns_claude_label(data_tmp):
    """Codex raises → Claude returns ok → result + 'claude' label."""
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        codex=RuntimeError("codex down"),
        claude={"text": "from-claude"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama as m_o:
        result, label = asyncio.run(intelligence.run_intelligence("hello"))
    assert label == "claude"
    assert result["text"] == "from-claude"
    assert m_x.call_count == 1
    assert m_c.call_count == 1
    assert m_o.call_count == 0


def test_config_override_changes_provider_order(data_tmp):
    """TOML order is authoritative; Ollama can be tried before cloud bridges."""
    (data_tmp / "config.toml").write_text(
        "[intelligence]\n"
        'provider_order = ["ollama", "codex"]\n',
        encoding="utf-8",
    )
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        ollama_chat="ollama-text",
    )
    with p_claude as m_c, p_codex as m_x, p_ollama as m_o:
        result, label = asyncio.run(intelligence.run_intelligence("hello"))
    assert label == "ollama"
    assert result == {"text": "ollama-text", "raw": "ollama-text"}
    assert m_o.call_count == 1
    assert m_x.call_count == 0
    assert m_c.call_count == 0


def test_call_override_can_prefer_default_claude_for_editorial_work(data_tmp):
    """A quality-sensitive caller can bypass the shared compiler-oriented pins."""
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        claude={"text": "editorial draft"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama as m_o:
        result, label = asyncio.run(
            intelligence.run_intelligence(
                "hello",
                provider_order=("claude", "codex", "ollama"),
                cli_model_overrides={"claude": None},
            )
        )

    assert label == "claude"
    assert result["text"] == "editorial draft"
    assert m_c.call_args.kwargs["model"] is None
    assert m_x.call_count == 0
    assert m_o.call_count == 0


def test_codex_and_claude_fail_falls_back_to_ollama(data_tmp):
    """Codex + Claude raise → Ollama serves → 'ollama' label."""
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        codex=RuntimeError("codex down"),
        claude=RuntimeError("claude down"),
        ollama_chat="ollama-text",
    )
    with p_claude as m_c, p_codex as m_x, p_ollama as m_o:
        result, label = asyncio.run(intelligence.run_intelligence("hello"))
    assert label == "ollama"
    assert result == {"text": "ollama-text", "raw": "ollama-text"}
    assert m_x.call_count == 1
    assert m_c.call_count == 1
    assert m_o.call_count == 1


def test_all_three_fail_raises_with_full_chain_context(data_tmp):
    """All configured tiers failing includes every attempted root cause."""
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        codex=RuntimeError("codex down"),
        claude=RuntimeError("claude down"),
        ollama_chat=RuntimeError("ollama dead"),
    )
    with p_claude, p_codex, p_ollama, pytest.raises(
        intelligence.IntelligenceUnavailable
    ) as exc_info:
        asyncio.run(intelligence.run_intelligence("hello"))
    msg = str(exc_info.value)
    assert msg.startswith("all configured tiers failed: codex=codex down")
    assert "codex=codex down" in msg
    assert "claude=claude down" in msg
    assert "ollama=ollama dead" in msg


def test_timeout_kwarg_is_forwarded_to_each_tier(data_tmp):
    """timeout_s passes through as int to claude, float to codex.

    Pins the cast: codex_bridge.run_codex uses ``timeout`` (float seconds),
    not ``timeout_s``. Mismatching here would silently break the codex tier.
    """
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        codex=RuntimeError("nope"),
        claude={"text": "ok"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama:
        asyncio.run(intelligence.run_intelligence("hi", timeout_s=42))

    # Codex got float(timeout_s) under the kw name 'timeout'.
    assert m_x.call_args.kwargs.get("timeout") == pytest.approx(42.0)
    assert isinstance(m_x.call_args.kwargs.get("timeout"), float)
    # Claude was called with the kwargs we expected.
    assert m_c.call_args.kwargs.get("timeout_s") == 42


def test_classification_mode_is_forwarded_to_claude_only(data_tmp):
    """Classification constrains Claude; Codex has no equivalent bridge kwarg."""
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        codex=RuntimeError("nope"),
        claude={"text": "ok"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama:
        asyncio.run(intelligence.run_intelligence("hi", classification=True))

    assert "classification" not in m_x.call_args.kwargs
    assert m_c.call_args.kwargs["classification"] is True


def test_classification_mode_keeps_codex_first(data_tmp):
    """Classification requests still honor the configured provider order."""
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        codex={"text": "ok", "raw": "ok"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama as m_o:
        result, label = asyncio.run(
            intelligence.run_intelligence("hi", classification=True)
        )

    assert label == "codex"
    assert result["text"] == "ok"
    assert "classification" not in m_x.call_args.kwargs
    assert m_c.call_count == 0
    assert m_o.call_count == 0


def test_cli_tier_model_pins_are_forwarded(data_tmp):
    """Default pins: codex runs gpt-5.6-luna at low effort, claude runs haiku."""
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        codex=RuntimeError("nope"),
        claude={"text": "ok"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama:
        asyncio.run(intelligence.run_intelligence("hi"))

    assert m_x.call_args.kwargs.get("model") == "gpt-5.6-luna"
    assert m_x.call_args.kwargs.get("reasoning_effort") == "low"
    assert m_c.call_args.kwargs.get("model") == "haiku"


def test_cli_tier_model_pins_can_be_cleared(data_tmp):
    """Empty-string TOML values fall back to each CLI's own default model."""
    (data_tmp / "config.toml").write_text(
        "[intelligence]\n"
        'codex_model = ""\n'
        'codex_reasoning_effort = ""\n'
        'claude_model = ""\n',
        encoding="utf-8",
    )
    _reload_settings()
    from mastisk.bridges import intelligence

    p_claude, p_codex, p_ollama = _patch_chain(
        codex=RuntimeError("nope"),
        claude={"text": "ok"},
    )
    with p_claude as m_c, p_codex as m_x, p_ollama:
        asyncio.run(intelligence.run_intelligence("hi"))

    assert m_x.call_args.kwargs.get("model") is None
    assert m_c.call_args.kwargs.get("model") is None


def test_provider_order_rejects_unknown_provider(data_tmp):
    (data_tmp / "config.toml").write_text(
        "[intelligence]\n"
        'provider_order = ["codex", "gemini"]\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    with pytest.raises(ValueError, match="provider_order"):
        reload_settings()
