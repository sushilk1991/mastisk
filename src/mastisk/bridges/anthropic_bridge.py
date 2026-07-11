"""Direct Anthropic Messages API bridge.

The fastest and most reliable intelligence tier: one HTTPS call, no CLI
subprocess, no launchd PATH problems, no interactive-session hangs. Uses
Haiku by default (config: ``[intelligence].anthropic_model``) — cheap enough
to run on every compile, and dramatically stronger than local fallbacks.

Key resolution: ``anthropic_api_key`` in config.toml, or the
``ANTHROPIC_API_KEY`` environment variable. ``available()`` is what the
intelligence chain uses to decide whether this tier participates.
"""
from __future__ import annotations

import json
import logging

import httpx

from mastisk.settings import get_settings

log = logging.getLogger("mastisk.anthropic")

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"


class AnthropicError(RuntimeError):
    pass


def _api_key() -> str | None:
    return get_settings().anthropic_api_key or None


def available() -> bool:
    return bool(_api_key())


async def run_anthropic(
    prompt: str,
    *,
    timeout_s: int = 180,
    model: str | None = None,
    max_tokens: int | None = None,
    json_object: bool = False,
    json_schema: dict | None = None,
) -> dict:
    """Send one user message, return ``{"text": ..., "raw": ...}`` (bridge parity).

    ``json_object=True`` forces the response through a tool call, which makes
    the API guarantee syntactically valid JSON — long HTML-in-JSON article
    bodies otherwise routinely arrive with unescaped inner quotes. The JSON is
    returned both parsed (``"json"`` key) and re-serialized into a fenced
    block (``"text"``) so ``extract_json_block``-based callers work unchanged.
    """
    key = _api_key()
    if not key:
        raise AnthropicError("no Anthropic API key configured")

    s = get_settings().intelligence
    body = {
        "model": model or s.anthropic_model,
        "max_tokens": max_tokens or s.anthropic_max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if json_object:
        # A real schema matters: with a bare {"type": "object"} the model
        # tends to nest its answer under a wrapper key like "parameters".
        body["tools"] = [{
            "name": "emit_json",
            "description": "Emit the JSON object the prompt asks for.",
            "input_schema": json_schema or {"type": "object"},
        }]
        body["tool_choice"] = {"type": "tool", "name": "emit_json"}
    headers = {
        "x-api-key": key,
        "anthropic-version": API_VERSION,
        "content-type": "application/json",
    }
    async with httpx.AsyncClient(timeout=timeout_s) as client:
        try:
            resp = await client.post(API_URL, json=body, headers=headers)
        except httpx.HTTPError as e:
            raise AnthropicError(f"anthropic request failed: {e}") from e

    if resp.status_code != 200:
        raise AnthropicError(
            f"anthropic API {resp.status_code}: {resp.text[:500]}"
        )

    payload = resp.json()
    if json_object:
        tool_input = next(
            (
                block.get("input")
                for block in payload.get("content", [])
                if block.get("type") == "tool_use"
            ),
            None,
        )
        if not isinstance(tool_input, dict):
            raise AnthropicError(
                f"anthropic returned no tool_use JSON: {str(payload)[:300]}"
            )
        # Defensive unwrap of the single-wrapper-key pattern.
        if set(tool_input.keys()) == {"parameters"} and isinstance(
            tool_input["parameters"], dict,
        ):
            tool_input = tool_input["parameters"]
        if payload.get("stop_reason") == "max_tokens":
            raise AnthropicError(
                "anthropic JSON response truncated at max_tokens "
                f"({body['max_tokens']}) — likely invalid"
            )
        text = f"```json\n{json.dumps(tool_input)}\n```"
        return {"text": text, "raw": payload, "json": tool_input}

    text = "".join(
        block.get("text", "")
        for block in payload.get("content", [])
        if block.get("type") == "text"
    )
    if not text:
        raise AnthropicError(f"anthropic returned no text content: {str(payload)[:300]}")
    if payload.get("stop_reason") == "max_tokens":
        log.warning(
            "anthropic response truncated at max_tokens=%s (model=%s)",
            body["max_tokens"], body["model"],
        )
    return {"text": text, "raw": payload}
