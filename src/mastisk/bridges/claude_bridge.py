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
    classification: bool = False,
    model: str | None = None,
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

        permission_mode = "plan" if classification else "acceptEdits"
        cmd = [
            claude_cmd, "-p", prompt,
            "--output-format", "json",
            "--permission-mode", permission_mode,
            "--add-dir", str(workdir),
        ]
        if classification:
            cmd += ["--tools", ""]
        if model:
            # e.g. "haiku" — an alias the CLI resolves to its current Haiku.
            cmd += ["--model", model]
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
        except TimeoutError as e:
            proc.kill()
            raise ClaudeError(f"claude timed out after {timeout_s}s") from e

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
    """Pull a JSON object out of an LLM response.

    Tolerates two shapes: (1) fenced ``` ```json {...} ``` ``` block (Claude's
    documented contract) and (2) naked ``{...}`` (Claude Code's recent
    versions emit this often despite a fence-enforcing prompt; Codex and
    Ollama emit it routinely). Without the naked-braces fallback every
    caller had to wrap this with the same retry → which was the literal
    source of "every escalation fails / every Codex call fails" bugs.
    """
    start = text.find("```json")
    if start == -1:
        start = text.find("```")
        if start != -1:
            start = text.find("\n", start) + 1
    else:
        start = text.find("\n", start) + 1
    if start != -1:
        end = text.find("```", start)
        if end != -1:
            try:
                return json.loads(text[start:end].strip())
            except json.JSONDecodeError:
                pass  # fall through to naked-braces parse
    # Naked-braces fallback: take the outermost {...} span.
    open_idx = text.find("{")
    close_idx = text.rfind("}")
    if open_idx >= 0 and close_idx > open_idx:
        try:
            return json.loads(text[open_idx : close_idx + 1])
        except json.JSONDecodeError:
            return None
    return None
