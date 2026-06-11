"""Claude → Codex → Ollama fallback chain.

Use ``run_intelligence(prompt)`` for any generative LLM call that doesn't
have task-specific JSON-shape retry logic. Returns ``(result, provider)``
where result is a dict with a 'text' key (parity with run_claude /
run_codex / run_ollama) and provider is the label of the engine that
actually served. The label is useful for bookkeeping ('model' columns on
persisted rows, feed labels, log lines).

Tier order:
1. Claude Code (claude -p, Opus 4.7 by default)
2. Codex CLI (codex exec, GPT-class) — fallback if Claude is unreachable
3. Ollama heavy model — local last-resort if both cloud bridges fail

Raises ``IntelligenceUnavailable`` only if all three providers fail; the
exception message includes the per-tier root cause so operators don't
chase the wrong layer (without this, only the Ollama error bubbled up).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Literal

from mastisk.bridges import claude_bridge, codex_bridge, ollama_bridge

log = logging.getLogger("mastisk.intelligence")

ProviderLabel = Literal["claude", "codex", "ollama"]


class IntelligenceUnavailable(RuntimeError):
    """All three LLM tiers (Claude, Codex, Ollama) failed for one call."""


async def run_intelligence(
    prompt: str,
    *,
    timeout_s: int = 180,
    classification: bool = False,
) -> tuple[dict, ProviderLabel]:
    claude_err: Exception | None = None
    codex_err: Exception | None = None

    try:
        result = await claude_bridge.run_claude(
            prompt,
            timeout_s=timeout_s,
            classification=classification,
        )
        return result, "claude"
    except (KeyboardInterrupt, asyncio.CancelledError):
        # Signal-style — must propagate, never fall through.
        raise
    except Exception as e:
        claude_err = e
        log.warning("intelligence: claude failed (%s); trying codex", e)

    try:
        result = await codex_bridge.run_codex(prompt, timeout=float(timeout_s))
        return result, "codex"
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as e:
        codex_err = e
        log.warning("intelligence: codex failed (%s); falling back to ollama", e)

    # Ollama tier. ``ollama_bridge.chat`` hardcodes its own httpx timeout
    # (~180s), so we wrap it to honour ``timeout_s`` consistently with the
    # other tiers.
    try:
        text = await asyncio.wait_for(
            ollama_bridge.chat(prompt, cheap=False), timeout=timeout_s,
        )
        return {"text": text, "raw": text}, "ollama"
    except (KeyboardInterrupt, asyncio.CancelledError):
        raise
    except Exception as ollama_err:
        raise IntelligenceUnavailable(
            f"all three tiers failed: claude={claude_err}; "
            f"codex={codex_err}; ollama={ollama_err}"
        ) from ollama_err
