"""Gemini CLI bridge. Subprocess wrapper around `gemini --prompt`."""
from __future__ import annotations

import asyncio
import logging
import shutil

log = logging.getLogger("mastisk.gemini")


class GeminiError(RuntimeError):
    pass


async def run_gemini(
    prompt: str,
    *,
    model: str | None = None,
    timeout: float = 120.0,
) -> dict:
    """Invoke `gemini --prompt ...`. Returns {'text': str, 'raw': str}.

    Raises GeminiError on non-zero exit, timeout, or missing binary.
    """
    if shutil.which("gemini") is None:
        raise GeminiError("gemini binary not on PATH")
    # --skip-trust: the daemon runs headless (under launchd), where the CLI's
    # directory-trust check fails with "not running in a trusted directory".
    # This flag is the documented bypass for automated environments.
    cmd = ["gemini", "--skip-trust"]
    if model:
        cmd += ["-m", model]
    cmd += ["--prompt", prompt]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise GeminiError(f"gemini not executable: {e}") from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise GeminiError(f"gemini timed out after {timeout}s")
    if proc.returncode != 0:
        raise GeminiError(stderr.decode("utf-8", errors="replace")[:500] or f"exit {proc.returncode}")
    text = stdout.decode("utf-8", errors="replace")
    return {"text": text, "raw": text}
