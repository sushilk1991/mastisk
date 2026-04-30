"""Roundtable — fans a prompt out to multiple LLM backends in parallel, synthesizes.

Per spec §§7, 8, 11, 12. Each bridge call is wrapped so one backend's failure
records an ``error`` perspective row but doesn't cancel the others. Synthesis
runs after all perspectives resolve; uses Claude by default, Ollama fallback
if Claude fails. On ``_handle(job)``:

1. Load the roundtable row; if status is terminal (done/failed), skip.
2. Transition to ``running``.
3. Build the effective prompt from identity + context + user question.
4. Fan out via ``asyncio.gather`` to all configured backends.
5. If ≥1 perspective succeeded: build synthesis prompt, call Claude
   (or Ollama if Claude raises). Write synthesis.
6. Transition to ``done`` (or ``failed`` if all perspectives failed).

See spec §11 for the full error-handling matrix.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import ClassVar

from mastisk.agents.base import Agent
from mastisk.bridges import claude_bridge, codex_bridge, gemini_bridge, ollama_bridge
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.roundtable")


PERSPECTIVE_PROMPT = """You are one of several AI models consulted on a question. Give your OWN take — don't hedge, don't synthesize others' views, don't claim to speak for multiple models.

## About the user
{identity}

## Context
{context}

## Question
{user_prompt}

## Your response
Be specific, cite concrete mechanisms, and acknowledge uncertainty where real. 200-500 words. Plain prose, no fenced code unless code is genuinely the answer.
"""


SYNTHESIS_PROMPT = """Four models were asked the same question. Their answers are below. Write a single synthesis paragraph that:
- Names where they agree.
- Names where they disagree, with who said what.
- Identifies the strongest single insight across the set.
- Flags anything suspicious (hallucination, surface-level reasoning) by backend name.

Keep it under 250 words. Plain prose.

## Question
{user_prompt}

## Perspectives
{perspectives_block}
"""


def _ollama_fallback_model() -> str:
    return get_settings().summarize_model_heavy


class Roundtable(Agent):
    """Roundtable agent. See module docstring + spec §§7, 8, 11, 12."""

    name: ClassVar[str] = "roundtable"
    tick_seconds: ClassVar[int] = 10  # poll for new jobs every 10s

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job["payload_json"] or "{}")
        rt_id = payload.get("roundtable_id")
        if rt_id is None:
            log.warning("roundtable: no roundtable_id in job %s", job["id"])
            return

        with connect() as conn:
            rt = q.get_roundtable(conn, rt_id)
        if rt is None:
            log.warning("roundtable: unknown roundtable_id %s", rt_id)
            return
        # Guard against double-processing: if a roundtable is already terminal
        # (done/failed), skip. A second `running` is OK (a prior attempt may
        # have crashed mid-flight; reprocessing is safe — perspectives are
        # append-only by backend but synthesis overwrites).
        if rt["status"] not in ("pending", "running"):
            log.info(
                "roundtable: %s already %s, skipping", rt_id, rt["status"]
            )
            return

        # Transition to running.
        with connect() as conn:
            q.update_roundtable_status(conn, rt_id=rt_id, status="running")

        # Build the effective prompt + context.
        identity = self.load_identity()
        context = await self._build_context(rt["input_type"], rt["input_ref"])
        settings = get_settings().roundtable

        per_prompt = PERSPECTIVE_PROMPT.format(
            identity=identity,
            context=context or "(none)",
            user_prompt=rt["prompt"],
        )

        # Fan out. return_exceptions=True is defensive belt-and-suspenders:
        # _run_backend already catches every exception internally, but if
        # something slips (e.g. a CancelledError), we don't want to lose the
        # perspectives from the other backends.
        tasks = [
            self._run_backend(rt_id, backend, per_prompt, settings)
            for backend in settings.backends
        ]
        await asyncio.gather(*tasks, return_exceptions=True)

        # Collect perspectives.
        with connect() as conn:
            perspectives = q.list_roundtable_perspectives(conn, rt_id)

        succeeded = [p for p in perspectives if p["error"] is None and p["content"]]
        if not succeeded:
            # Every backend failed. No point calling Claude for synthesis.
            with connect() as conn:
                q.update_roundtable_status(
                    conn, rt_id=rt_id, status="failed",
                    error="all backends failed", finished=True,
                )
            self.emit_feed(
                verb="roundtable-failed", obj=str(rt_id), kind="roundtable",
            )
            return

        # Synthesis.
        synthesis, synthesis_model = await self._synthesize(
            rt["prompt"], perspectives, settings
        )
        with connect() as conn:
            q.update_roundtable_status(
                conn, rt_id=rt_id, status="done",
                synthesis=synthesis, synthesis_model=synthesis_model,
                finished=True,
            )
        self.emit_feed(
            verb="roundtable-done", obj=str(rt_id), kind="roundtable",
            payload={
                "perspective_count": len(succeeded),
                "synthesis_model": synthesis_model,
            },
        )

    # ───── context builder ─────

    async def _build_context(self, input_type: str, input_ref: str) -> str:
        """Construct a context block based on ``input_type`` / ``input_ref``.

        Per spec §8:
        - ``note``: body + classification + summary.
        - ``article``: title + summary + body_md (truncated).
        - ``prompt``: empty (caller handles the prompt-only case).
        """
        settings = get_settings().roundtable
        if input_type == "note":
            try:
                nid = int(input_ref)
            except (ValueError, TypeError):
                return ""
            with connect() as conn:
                n = q.get_note(conn, nid)
            if n is None:
                return ""
            lines = [f"Note #{nid}"]
            if n.get("classification"):
                lines.append(f"classification: {n['classification']}")
            if n.get("summary"):
                lines.append(f"summary: {n['summary']}")
            body = (n.get("body") or "")[: settings.context_max_chars]
            lines.append("\n---\n" + body)
            return "\n".join(lines)
        if input_type == "article":
            with connect() as conn:
                row = conn.execute(
                    "SELECT id, title, summary, body_md FROM articles WHERE id = ?",
                    (input_ref,),
                ).fetchone()
            if row is None:
                return ""
            body = (row["body_md"] or "")[: settings.context_max_chars]
            return (
                f"Article: {row['title']}\n\n"
                f"{row['summary'] or ''}\n\n---\n{body}"
            )
        return ""

    # ───── fan-out ─────

    async def _run_backend(
        self, rt_id: int, backend: str, prompt: str, settings,
    ) -> None:
        """Invoke one backend; always writes a perspective row.

        We swallow all exceptions inside this coroutine so a single bridge's
        failure records an ``error`` but never cancels siblings via gather.
        """
        model = settings.perspective_models.get(backend)
        started = datetime.now().astimezone().isoformat()
        t0 = time.perf_counter()
        content: str | None = None
        error: str | None = None
        try:
            if backend == "claude":
                # claude_bridge.run_claude is async — must await.
                result = await claude_bridge.run_claude(prompt)
                content = (
                    result.get("text") if isinstance(result, dict) else str(result)
                )
            elif backend == "codex":
                result = await codex_bridge.run_codex(
                    prompt, model=model, timeout=float(settings.timeout_seconds),
                )
                content = result.get("text") if isinstance(result, dict) else str(result)
            elif backend == "gemini":
                result = await gemini_bridge.run_gemini(
                    prompt, model=model, timeout=float(settings.timeout_seconds),
                )
                content = result.get("text") if isinstance(result, dict) else str(result)
            elif backend == "ollama":
                # ollama_bridge.run_ollama requires model positionally. If the
                # settings dict doesn't have an ollama entry, fall back to the
                # default rather than passing None (which would 400 at the
                # httpx layer).
                result = await ollama_bridge.run_ollama(
                    prompt, model or _ollama_fallback_model(),
                )
                content = result.get("text") if isinstance(result, dict) else str(result)
            else:
                error = f"unknown backend: {backend}"
        except Exception as e:
            # Truncate to keep the DB row modest; the original exception is
            # already on the logger.
            error = f"{type(e).__name__}: {e}"[:500]
            log.info(
                "roundtable: backend %s failed for rt %s: %s", backend, rt_id, e,
            )
        finished = datetime.now().astimezone().isoformat()
        latency_ms = int((time.perf_counter() - t0) * 1000)
        with connect() as conn:
            q.insert_roundtable_perspective(
                conn, roundtable_id=rt_id, backend=backend, model=model,
                content=content, error=error,
                latency_ms=latency_ms, started_at=started, finished_at=finished,
            )

    # ───── synthesis ─────

    async def _synthesize(
        self, user_prompt: str, perspectives: list[dict], settings,
    ) -> tuple[str, str]:
        """Build the synthesis prompt and call Claude (or Ollama fallback).

        Returns ``(text, model_used)`` where ``model_used`` is one of
        ``"claude"``, ``"ollama"``, or ``"none"`` if both fail. Per spec §11
        "Synthesis call fails" row: we still return a string so the DB column
        isn't NULL in the failure path; the caller writes it as-is.
        """
        lines: list[str] = []
        for p in perspectives:
            backend_label = p["backend"].title()
            if p.get("content"):
                lines.append(f"### {backend_label}\n{p['content']}")
            elif p.get("error"):
                lines.append(f"### {backend_label}\n*(failed: {p['error']})*")
        block = "\n\n".join(lines)
        prompt = SYNTHESIS_PROMPT.format(
            user_prompt=user_prompt, perspectives_block=block,
        )

        # Try Claude first; fall back to Ollama if it fails (quota, binary
        # missing, timeout — all handled by claude_bridge raising ClaudeError).
        try:
            result = await claude_bridge.run_claude(prompt)
            text = (
                result.get("text") if isinstance(result, dict) else str(result)
            )
            return (text or "").strip(), "claude"
        except Exception as e:
            log.warning(
                "roundtable: Claude synthesis failed (%s), falling back to Ollama",
                e,
            )
        try:
            model = (
                settings.perspective_models.get("ollama") or _ollama_fallback_model()
            )
            result = await ollama_bridge.run_ollama(prompt, model)
            text = (
                result.get("text") if isinstance(result, dict) else str(result)
            )
            return (text or "").strip(), "ollama"
        except Exception as e:
            log.error("roundtable: Ollama synthesis also failed (%s)", e)
            return f"(synthesis unavailable: {e})", "none"
