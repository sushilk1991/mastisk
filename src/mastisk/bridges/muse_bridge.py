"""Stateless Muse exec adapter: no tools, no workspace trust, and bounded execution."""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile

from mastisk.settings import get_settings

_MAX_MODEL_STEPS = 1
_ERROR_TAIL_CHARS = 500


def _final_text(raw: str) -> str:
    """Read only a successful terminal result; partial deltas are not completion."""
    terminal = None
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if not isinstance(event, dict):
            continue

        event_type = event.get("payload_type")
        if isinstance(event_type, str) and event_type.startswith("run.terminal."):
            terminal = event

    if terminal is None or terminal.get("payload_type") != "run.terminal.completed":
        raise RuntimeError("Muse returned no successful terminal result")

    payload = terminal.get("payload")
    if not isinstance(payload, dict) or payload.get("terminal") != "completed":
        raise RuntimeError("Muse terminal result is not completed")

    text = payload.get("text")
    if not isinstance(text, str) or not text.strip():
        raise RuntimeError("Muse returned an empty final answer")

    return text


async def _run_muse(
    prompt: str, *, model: str | None = None,
    reasoning_effort: str | None = None, timeout: float = 180.0,
) -> dict:
    """Isolated exec -> JSONL terminal -> final text; timeout/cancellation reap the child."""
    command = get_settings().muse_cmd
    executable = shutil.which(command)
    if executable is None:
        raise RuntimeError(f"{command} binary not on PATH")

    # Untrusted article text must never acquire filesystem, shell, or web-tool authority.
    with tempfile.TemporaryDirectory(prefix="mastisk-muse-") as workdir:
        args = [
            executable, "exec", "--json", "--preset", "empty",
            "--no-session-log", "--no-foreign-personal-context",
            "--disable-write", "--disable-shell", "--disable-web-tools",
            "--disable-reminders", "--disable-request-user-input",
            "--max-model-steps", str(_MAX_MODEL_STEPS),
        ]
        if model:
            args.extend(["--model", model])
        if reasoning_effort:
            args.extend(["--reasoning-effort", reasoning_effort])
        args.append(prompt)

        process = await asyncio.create_subprocess_exec(
            *args, cwd=workdir, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise RuntimeError(f"Muse timed out after {timeout}s") from exc
        except asyncio.CancelledError:
            process.kill()
            await process.wait()
            raise

        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace").strip()[-_ERROR_TAIL_CHARS:]
            raise RuntimeError(f"Muse exited {process.returncode}: {detail}")

        raw = stdout.decode("utf-8", errors="replace")
        return {"text": _final_text(raw), "raw": raw}
