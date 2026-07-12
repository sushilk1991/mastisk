"""Codex CLI bridge. Subprocess wrapper around `codex exec`.

Non-interactive invocation: `codex exec [PROMPT]`. Model override via `-c model=...`.
"""
from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("mastisk.codex")


class CodexError(RuntimeError):
    pass


async def run_codex(
    prompt: str,
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """Invoke `codex exec`. Returns {'text': str, 'raw': str}.

    Raises CodexError on non-zero exit, timeout, or missing binary.
    """
    if shutil.which("codex") is None:
        raise CodexError("codex binary not on PATH")
    # ``--skip-git-repo-check``: the daemon's launchd cwd is not a trusted
    # codex directory, so without this flag codex refuses to run with
    # ``Not inside a trusted directory``. We use codex purely as a stateless
    # LLM (no code edits, no repo writes), so the trust check adds no value.
    cmd = ["codex", "exec", "--skip-git-repo-check"]
    if model:
        cmd += ["-c", f'model="{model}"']
    if reasoning_effort:
        cmd += ["-c", f'model_reasoning_effort="{reasoning_effort}"']
    cmd.append(prompt)
    # stdin=DEVNULL is load-bearing: when the daemon runs under launchd, the
    # subprocess inherits a piped stdin. ``codex exec PROMPT`` then sees stdin
    # as piped, falls into "read additional input from stdin" mode, and hangs
    # / errors out instead of using the positional prompt arg. Detaching stdin
    # lets it use the arg path the docs advertise.
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise CodexError(f"codex not executable: {e}") from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise CodexError(f"codex timed out after {timeout}s")
    if proc.returncode != 0:
        # Codex prints a long startup banner (workdir, model, sandbox, the
        # echoed prompt) BEFORE the actual error message, so a head-truncated
        # stderr hides the real error behind boilerplate. Take the tail.
        msg = stderr.decode("utf-8", errors="replace").strip()
        if len(msg) > 500:
            msg = "…" + msg[-500:]
        raise CodexError(msg or f"exit {proc.returncode}")
    text = stdout.decode("utf-8", errors="replace")
    return {"text": text, "raw": text}
