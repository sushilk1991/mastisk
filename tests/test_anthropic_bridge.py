"""Anthropic API bridge + intelligence-chain integration tests. All HTTP mocked."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from mastisk.bridges import anthropic_bridge


def _reload_settings() -> None:
    from mastisk.settings import reload_settings
    reload_settings()


def _ok_response(text: str = "hello") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "model": "claude-haiku-4-5-20251001",
        },
        request=httpx.Request("POST", anthropic_bridge.API_URL),
    )


def test_available_false_without_key(data_tmp):
    _reload_settings()
    assert anthropic_bridge.available() is False


def test_available_true_with_config_key(data_tmp):
    (data_tmp / "config.toml").write_text(
        'anthropic_api_key = "sk-ant-test"\n', encoding="utf-8",
    )
    _reload_settings()
    assert anthropic_bridge.available() is True


def test_run_anthropic_parses_text(data_tmp):
    (data_tmp / "config.toml").write_text(
        'anthropic_api_key = "sk-ant-test"\n', encoding="utf-8",
    )
    _reload_settings()

    with patch.object(
        httpx.AsyncClient, "post", new=AsyncMock(return_value=_ok_response("hi there")),
    ) as m_post:
        result = asyncio.run(anthropic_bridge.run_anthropic("prompt"))
    assert result["text"] == "hi there"
    body = m_post.call_args.kwargs["json"]
    assert body["model"] == "claude-haiku-4-5-20251001"
    assert body["messages"] == [{"role": "user", "content": "prompt"}]
    headers = m_post.call_args.kwargs["headers"]
    assert headers["x-api-key"] == "sk-ant-test"


def test_run_anthropic_raises_without_key(data_tmp):
    _reload_settings()
    with pytest.raises(anthropic_bridge.AnthropicError, match="no Anthropic API key"):
        asyncio.run(anthropic_bridge.run_anthropic("prompt"))


def test_run_anthropic_raises_on_http_error(data_tmp):
    (data_tmp / "config.toml").write_text(
        'anthropic_api_key = "sk-ant-test"\n', encoding="utf-8",
    )
    _reload_settings()
    err = httpx.Response(
        429, json={"error": {"message": "rate limited"}},
        request=httpx.Request("POST", anthropic_bridge.API_URL),
    )
    with (
        patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=err)),
        pytest.raises(anthropic_bridge.AnthropicError, match="429"),
    ):
        asyncio.run(anthropic_bridge.run_anthropic("prompt"))


def _tool_response(tool_input, stop_reason: str = "tool_use") -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "tool_use", "name": "emit_json", "input": tool_input}],
            "stop_reason": stop_reason,
        },
        request=httpx.Request("POST", anthropic_bridge.API_URL),
    )


def test_run_anthropic_json_object_forces_tool_and_parses(data_tmp):
    (data_tmp / "config.toml").write_text(
        'anthropic_api_key = "sk-ant-test"\n', encoding="utf-8",
    )
    _reload_settings()
    schema = {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}

    with patch.object(
        httpx.AsyncClient, "post",
        new=AsyncMock(return_value=_tool_response({"id": "x", "title": 'He said "hi"'})),
    ) as m_post:
        result = asyncio.run(
            anthropic_bridge.run_anthropic("p", json_object=True, json_schema=schema),
        )
    body = m_post.call_args.kwargs["json"]
    assert body["tool_choice"] == {"type": "tool", "name": "emit_json"}
    assert body["tools"][0]["input_schema"] == schema
    assert result["json"] == {"id": "x", "title": 'He said "hi"'}
    # The fenced text round-trips through extract_json_block-style parsing.
    import json as _json
    fenced = result["text"]
    assert fenced.startswith("```json\n") and fenced.endswith("\n```")
    assert _json.loads(fenced[len("```json\n"):-len("\n```")])["title"] == 'He said "hi"'


def test_run_anthropic_json_object_unwraps_parameters_wrapper(data_tmp):
    (data_tmp / "config.toml").write_text(
        'anthropic_api_key = "sk-ant-test"\n', encoding="utf-8",
    )
    _reload_settings()
    with patch.object(
        httpx.AsyncClient, "post",
        new=AsyncMock(return_value=_tool_response({"parameters": {"id": "wrapped"}})),
    ):
        result = asyncio.run(anthropic_bridge.run_anthropic("p", json_object=True))
    assert result["json"] == {"id": "wrapped"}


def test_run_anthropic_json_object_rejects_truncation(data_tmp):
    (data_tmp / "config.toml").write_text(
        'anthropic_api_key = "sk-ant-test"\n', encoding="utf-8",
    )
    _reload_settings()
    with (
        patch.object(
            httpx.AsyncClient, "post",
            new=AsyncMock(return_value=_tool_response({"id": "cut"}, stop_reason="max_tokens")),
        ),
        pytest.raises(anthropic_bridge.AnthropicError, match="truncated"),
    ):
        asyncio.run(anthropic_bridge.run_anthropic("p", json_object=True))


def test_anthropic_auto_false_disables_prepend(data_tmp):
    (data_tmp / "config.toml").write_text(
        'anthropic_api_key = "sk-ant-test"\n'
        "[intelligence]\n"
        "anthropic_auto = false\n",
        encoding="utf-8",
    )
    _reload_settings()
    from mastisk.bridges import intelligence

    assert intelligence.effective_order() == ["codex", "claude", "ollama"]


def test_intelligence_auto_prepends_anthropic_when_key_present(data_tmp):
    """A configured key upgrades the default chain without a config edit."""
    (data_tmp / "config.toml").write_text(
        'anthropic_api_key = "sk-ant-test"\n', encoding="utf-8",
    )
    _reload_settings()
    from mastisk.bridges import intelligence

    assert intelligence.effective_order() == ["anthropic", "codex", "claude", "ollama"]

    with patch(
        "mastisk.bridges.intelligence.anthropic_bridge.run_anthropic",
        new=AsyncMock(return_value={"text": "api-served", "raw": {}}),
    ):
        result, label = asyncio.run(intelligence.run_intelligence("hello"))
    assert label == "anthropic"
    assert result["text"] == "api-served"


def test_intelligence_explicit_order_not_double_prepended(data_tmp):
    (data_tmp / "config.toml").write_text(
        'anthropic_api_key = "sk-ant-test"\n'
        "[intelligence]\n"
        'provider_order = ["claude", "anthropic"]\n',
        encoding="utf-8",
    )
    _reload_settings()
    from mastisk.bridges import intelligence

    assert intelligence.effective_order() == ["claude", "anthropic"]


def test_provider_order_accepts_anthropic(data_tmp):
    (data_tmp / "config.toml").write_text(
        "[intelligence]\n"
        'provider_order = ["anthropic", "ollama"]\n',
        encoding="utf-8",
    )
    _reload_settings()
    from mastisk.settings import get_settings

    assert get_settings().intelligence.provider_order == ["anthropic", "ollama"]


def test_breaker_skips_slow_failing_provider_after_threshold(data_tmp):
    """3 consecutive codex failures trip the breaker; the next call skips codex."""
    _reload_settings()
    from mastisk.bridges import intelligence

    with (
        patch(
            "mastisk.bridges.intelligence.codex_bridge.run_codex",
            new=AsyncMock(side_effect=RuntimeError("codex timed out")),
        ) as m_codex,
        patch(
            "mastisk.bridges.intelligence.claude_bridge.run_claude",
            new=AsyncMock(return_value={"text": "ok"}),
        ),
    ):
        for _ in range(3):
            _, label = asyncio.run(intelligence.run_intelligence("hi"))
            assert label == "claude"
        assert m_codex.call_count == 3
        assert intelligence.effective_order() == ["claude", "ollama"]

        # Fourth call: codex is in cooldown and must not be attempted.
        _, label = asyncio.run(intelligence.run_intelligence("hi"))
        assert label == "claude"
        assert m_codex.call_count == 3


def test_breaker_success_resets_failure_count(data_tmp):
    _reload_settings()
    from mastisk.bridges import intelligence

    flaky = AsyncMock(
        side_effect=[RuntimeError("down"), {"text": "ok", "raw": "ok"}, RuntimeError("down")],
    )
    with (
        patch("mastisk.bridges.intelligence.codex_bridge.run_codex", new=flaky),
        patch(
            "mastisk.bridges.intelligence.claude_bridge.run_claude",
            new=AsyncMock(return_value={"text": "fallback"}),
        ),
    ):
        asyncio.run(intelligence.run_intelligence("hi"))  # codex fails → claude
        asyncio.run(intelligence.run_intelligence("hi"))  # codex ok → reset
        asyncio.run(intelligence.run_intelligence("hi"))  # codex fails again (count=1)
    assert intelligence.effective_order() == ["codex", "claude", "ollama"]
