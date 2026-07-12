"""Config-driven Anthropic/Codex/Claude/Ollama fallback chain.

Use ``run_intelligence(prompt)`` for any generative LLM call that doesn't
have task-specific JSON-shape retry logic. Returns ``(result, provider)``
where result is a dict with a 'text' key (parity with run_anthropic /
run_claude / run_codex / run_ollama) and provider is the label of the engine
that actually served. The label is useful for bookkeeping ('model' columns on
persisted rows, feed labels, log lines).

The order comes from ``[intelligence].provider_order`` and defaults to
Codex → Claude → Ollama. When an Anthropic API key is configured
(``anthropic_api_key`` in config.toml or ``ANTHROPIC_API_KEY`` in the env)
the direct-API "anthropic" tier is auto-prepended unless the user already
listed it explicitly — one HTTPS call beats spawning a CLI subprocess on
every request.

Two reliability layers on top of the raw order:

- **Availability filter**: tiers whose prerequisite is visibly missing
  (no API key, CLI binary not on PATH) are skipped without burning a
  timeout on them.
- **Circuit breaker**: a provider that fails ``breaker_failure_threshold``
  times in a row is skipped for ``breaker_cooldown_s`` seconds. Observed in
  the field: a hung codex tier taxing every single call with a 180s timeout
  before Claude got a chance.

Raises ``IntelligenceUnavailable`` only if every usable provider fails; the
exception message includes each attempted provider's root cause.
``classification=True`` only has a hard constrained mode on the Claude tier.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal, cast

from mastisk.bridges import anthropic_bridge, claude_bridge, codex_bridge, ollama_bridge
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.intelligence")

ProviderLabel = Literal["anthropic", "claude", "codex", "ollama"]


class IntelligenceUnavailable(RuntimeError):
    """Every configured LLM tier failed for one call."""


# ── Circuit breaker (module-level, per-process) ──────────────────────────────
_consecutive_failures: dict[str, int] = {}
_tripped_until: dict[str, float] = {}


def _breaker_open(provider: str) -> bool:
    return time.monotonic() < _tripped_until.get(provider, 0.0)


def _record_failure(provider: str) -> None:
    s = get_settings().intelligence
    n = _consecutive_failures.get(provider, 0) + 1
    _consecutive_failures[provider] = n
    if n >= s.breaker_failure_threshold:
        _tripped_until[provider] = time.monotonic() + s.breaker_cooldown_s
        log.warning(
            "intelligence: circuit breaker tripped for %s (%d consecutive failures, "
            "cooling down %ds)", provider, n, s.breaker_cooldown_s,
        )


def _record_success(provider: str) -> None:
    _consecutive_failures[provider] = 0
    _tripped_until.pop(provider, None)


def reset_breakers() -> None:
    """Test hook / manual reset."""
    _consecutive_failures.clear()
    _tripped_until.clear()


def effective_order() -> list[ProviderLabel]:
    """Configured order, upgraded with the anthropic tier and breaker-filtered.

    Missing binaries/keys are NOT filtered here — those tiers fail in
    milliseconds on their own and the fallback loop moves on. Only two
    adjustments happen: the anthropic tier is prepended when a key is
    configured but the user hasn't listed it, and providers in breaker
    cooldown are skipped (they've proven they fail *slowly*).
    """
    s = get_settings().intelligence
    configured = [cast(ProviderLabel, p) for p in s.provider_order]
    if (
        s.anthropic_auto
        and "anthropic" not in configured
        and anthropic_bridge.available()
    ):
        configured = [cast(ProviderLabel, "anthropic"), *configured]

    usable = [p for p in configured if not _breaker_open(p)]
    # Never return an empty chain: if every breaker is open, fall back to the
    # full order so calls still get attempted rather than failing instantly.
    return usable or configured


async def run_intelligence(
    prompt: str,
    *,
    timeout_s: int = 180,
    classification: bool = False,
    json_object: bool = False,
    json_schema: dict | None = None,
) -> tuple[dict, ProviderLabel]:
    # json_object=True asks tiers that support constrained output (currently
    # only the Anthropic API) to guarantee a syntactically valid JSON object.
    # CLI tiers ignore it — callers still run extract_json_block either way.
    # classification=True does not reorder providers. The capture router's
    # outer timeout intentionally bounds the full configured chain.
    order = effective_order()
    errors: dict[ProviderLabel, Exception] = {}
    last_err: Exception | None = None

    for idx, provider in enumerate(order):
        try:
            result = await _run_provider(
                provider,
                prompt,
                timeout_s=timeout_s,
                classification=classification,
                json_object=json_object,
                json_schema=json_schema,
            )
            _record_success(provider)
            return result, provider
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Signal-style — must propagate, never fall through.
            raise
        except Exception as e:
            _record_failure(provider)
            errors[provider] = e
            last_err = e
            next_provider = order[idx + 1] if idx + 1 < len(order) else None
            if next_provider is None:
                break
            log.warning(
                "intelligence: %s failed (%s); trying %s",
                provider,
                e,
                next_provider,
            )

    msg = "; ".join(
        f"{provider}={errors[provider]}" for provider in order if provider in errors
    )
    raise IntelligenceUnavailable(f"all configured tiers failed: {msg}") from last_err


async def _run_provider(
    provider: ProviderLabel,
    prompt: str,
    *,
    timeout_s: int,
    classification: bool,
    json_object: bool = False,
    json_schema: dict | None = None,
) -> dict:
    if provider == "anthropic":
        return await anthropic_bridge.run_anthropic(
            prompt, timeout_s=timeout_s, json_object=json_object,
            json_schema=json_schema,
        )
    s = get_settings().intelligence
    if provider == "codex":
        # Local `codex exec --help` exposes no Claude-style no-tools /
        # classification flag. The bridge uses a non-interactive prompt
        # argument and detached stdin; do not pass a made-up kwarg through.
        return await codex_bridge.run_codex(
            prompt,
            model=s.codex_model or None,
            reasoning_effort=s.codex_reasoning_effort or None,
            timeout=float(timeout_s),
        )
    if provider == "claude":
        return await claude_bridge.run_claude(
            prompt,
            timeout_s=timeout_s,
            classification=classification,
            model=s.claude_model or None,
        )
    # Ollama tier. ``ollama_bridge.chat`` hardcodes its own httpx timeout
    # (~180s), so we wrap it to honour ``timeout_s`` consistently with the
    # other tiers.
    text = await asyncio.wait_for(
        ollama_bridge.chat(prompt, cheap=False), timeout=timeout_s,
    )
    return {"text": text, "raw": text}
