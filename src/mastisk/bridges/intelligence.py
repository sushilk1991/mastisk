"""Config-driven Codex/Claude/Ollama fallback chain.

Use ``run_intelligence(prompt)`` for any generative LLM call that doesn't
have task-specific JSON-shape retry logic. Returns ``(result, provider)``
where result is a dict with a 'text' key (parity with run_claude /
run_codex / run_ollama) and provider is the label of the engine that
actually served. The label is useful for bookkeeping ('model' columns on
persisted rows, feed labels, log lines).

The order comes from ``[intelligence].provider_order`` and defaults to
Codex → Claude → Ollama. Raises ``IntelligenceUnavailable`` only if every
configured provider fails; the exception message includes each attempted
provider's root cause. ``classification=True`` only has a hard constrained
mode on the Claude tier; Codex and Ollama still follow the configured order.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal, cast

from mastisk.bridges import claude_bridge, codex_bridge, ollama_bridge
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.intelligence")

ProviderLabel = Literal["claude", "codex", "ollama"]


class IntelligenceUnavailable(RuntimeError):
    """Every configured LLM tier failed for one call."""


async def run_intelligence(
    prompt: str,
    *,
    timeout_s: int = 180,
    classification: bool = False,
) -> tuple[dict, ProviderLabel]:
    # classification=True does not reorder providers. The capture router's
    # outer timeout intentionally bounds the full configured chain, so Codex
    # still gets first budget when the default order is active.
    order = [
        cast(ProviderLabel, provider)
        for provider in get_settings().intelligence.provider_order
    ]
    errors: dict[ProviderLabel, Exception] = {}
    last_err: Exception | None = None

    for idx, provider in enumerate(order):
        try:
            return (
                await _run_provider(
                    provider,
                    prompt,
                    timeout_s=timeout_s,
                    classification=classification,
                ),
                provider,
            )
        except (KeyboardInterrupt, asyncio.CancelledError):
            # Signal-style — must propagate, never fall through.
            raise
        except Exception as e:
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
) -> dict:
    if provider == "codex":
        # Local `codex exec --help` exposes no Claude-style no-tools /
        # classification flag. The bridge uses a non-interactive prompt
        # argument and detached stdin; do not pass a made-up kwarg through.
        return await codex_bridge.run_codex(prompt, timeout=float(timeout_s))
    if provider == "claude":
        return await claude_bridge.run_claude(
            prompt,
            timeout_s=timeout_s,
            classification=classification,
        )
    # Ollama tier. ``ollama_bridge.chat`` hardcodes its own httpx timeout
    # (~180s), so we wrap it to honour ``timeout_s`` consistently with the
    # other tiers.
    text = await asyncio.wait_for(
        ollama_bridge.chat(prompt, cheap=False), timeout=timeout_s,
    )
    return {"text": text, "raw": text}
