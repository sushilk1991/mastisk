"""Claude Code subprocess bridge.

Each call spawns `claude -p <prompt>` inside a disposable workdir seeded with:
  - SOURCE.md  (raw input)
  - PROMPT.md  (agent-specific task spec)
  - SCHEMA.md  (expected output contract)

Claude reads, writes OUTPUT.json (or streams JSON on stdout via --output-format json).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import uuid
from pathlib import Path

from mastisk.paths import tmp_dir
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.claude")


class ClaudeError(RuntimeError):
    pass


def _resolve_cmd(cmd: str) -> str:
    """Return an absolute path for ``cmd``.

    launchd and systemd services run with a minimal PATH, so a bare name like
    ``claude`` fails to resolve even though the interactive shell finds it.
    We augment PATH with the common user-install locations before asking
    ``shutil.which`` — good enough without forcing the user to hand-edit config.
    """
    if "/" in cmd:
        return cmd
    extra = [
        str(Path.home() / ".local" / "bin"),
        str(Path.home() / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]
    path = os.pathsep.join([*extra, os.environ.get("PATH", "")])
    resolved = shutil.which(cmd, path=path)
    return resolved or cmd


async def run_claude(
    prompt: str,
    *,
    source_md: str | None = None,
    schema_md: str | None = None,
    timeout_s: int = 300,
) -> dict:
    """Run Claude headlessly, return parsed JSON from stdout."""
    s = get_settings()
    claude_cmd = _resolve_cmd(s.claude_cmd)

    job_id = uuid.uuid4().hex[:8]
    workdir = tmp_dir() / f"job-{job_id}"
    workdir.mkdir(parents=True, exist_ok=True)

    try:
        if source_md is not None:
            (workdir / "SOURCE.md").write_text(source_md)
        if schema_md is not None:
            (workdir / "SCHEMA.md").write_text(schema_md)
        (workdir / "PROMPT.md").write_text(prompt)

        cmd = [
            claude_cmd, "-p", prompt,
            "--output-format", "json",
            "--permission-mode", "acceptEdits",
            "--add-dir", str(workdir),
        ]
        log.info("claude %s (workdir=%s)", prompt[:80].replace("\n", " "), workdir)
        # stdin=DEVNULL: when the daemon runs under launchd it inherits a
        # piped stdin, which ``claude -p`` will then try to consume — same
        # symptom as codex. Detaching stdin makes the subprocess use the
        # ``-p PROMPT`` arg unambiguously.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=workdir,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            raise ClaudeError(f"claude timed out after {timeout_s}s")

        if proc.returncode != 0:
            raise ClaudeError(
                f"claude exited {proc.returncode}: {stderr.decode(errors='replace')[:2000]}"
            )

        raw = stdout.decode(errors="replace")
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            # Newer claude -p JSON format wraps result under "result" or streams
            for line in reversed(raw.strip().splitlines()):
                try:
                    payload = json.loads(line)
                    break
                except json.JSONDecodeError:
                    continue
            else:
                raise ClaudeError(f"could not parse claude stdout: {raw[:500]}")

        # The `--output-format json` envelope in Claude Code puts the final assistant message in "result"
        if isinstance(payload, dict) and "result" in payload:
            result = payload["result"]
            # result is usually a text message; agents should instruct Claude to return a JSON block
            return {"text": result, "raw": payload}
        return {"text": str(payload), "raw": payload}
    finally:
        # Keep the workdir for 24h for debugging via a GC step; for now clean immediately
        shutil.rmtree(workdir, ignore_errors=True)


def extract_json_block(text: str) -> dict | None:
    """Pull the first ```json ... ``` block out of a Claude response."""
    start = text.find("```json")
    if start == -1:
        start = text.find("```")
        if start == -1:
            return None
        start = text.find("\n", start) + 1
    else:
        start = text.find("\n", start) + 1
    end = text.find("```", start)
    if end == -1:
        return None
    try:
        return json.loads(text[start:end].strip())
    except json.JSONDecodeError:
        return None
