"""Local git repo bridge. Reads state via `git` subprocess.

Secret scrubbing: files matching common secret-ish patterns are excluded
from any content the bridge returns. The caller embeds this in LLM prompts,
so leaking a .env would ship it to the model provider. Defense in depth.
"""
from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import logging
from pathlib import Path

log = logging.getLogger("mastisk.local_git")


SCRUBBED_PATTERNS = [
    ".env", ".env.*",
    "*.pem", "*.key", "*.p12", "*.pfx",
    "*credentials*", "*secret*",
    "id_rsa*", "id_dsa*", "id_ecdsa*", "id_ed25519*",
    ".aws/*", ".ssh/*",
    "node_modules/*", ".git/*", ".venv/*", "venv/*",
    "__pycache__/*", "*.pyc",
    "dist/*", "build/*", ".next/*",
]


class LocalGitError(RuntimeError):
    pass


def _is_secret_path(rel_path: str) -> bool:
    """True if rel_path matches any scrub pattern (case-insensitive)."""
    lower = rel_path.lower()
    for pat in SCRUBBED_PATTERNS:
        if fnmatch.fnmatch(lower, pat.lower()) or fnmatch.fnmatch(
            lower, pat.lower().lstrip("*/").rstrip("/*")
        ):
            return True
        # Handle nested: check any path segment
        parts = lower.split("/")
        for part in parts:
            if fnmatch.fnmatch(part, pat.lower()):
                return True
    return False


async def _git(cwd: Path, *args: str, timeout: float = 10.0) -> str:
    """Run a git command; returns stdout or raises LocalGitError."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as e:
        raise LocalGitError(f"git not on PATH: {e}") from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise LocalGitError(f"git {args[0]} timed out after {timeout}s")
    if proc.returncode != 0:
        raise LocalGitError(stderr.decode("utf-8", errors="replace")[:500])
    return stdout.decode("utf-8", errors="replace")


async def fetch_local_repo_state(path: Path) -> dict:
    """Read local git state. Returns a dict matching the shape the poller expects.

    Structure:
      {
        "branch": "main",
        "commits": [{sha, author, date, message}, ...],
        "dirty_count": int,
        "tracked_count": int,
        "top_files": ["README.md", "src/mastisk/...", ...],  # up to 200, scrubbed
        "readme_excerpt": str | None,  # first 2000 chars of README.md if present + not scrubbed
        "changelog_excerpt": str | None,
        "readme_hash": str | None,
      }
    """
    if not path.is_dir():
        raise LocalGitError(f"not a directory: {path}")
    if not (path / ".git").exists():
        raise LocalGitError(f"not a git repo: {path}")

    branch = (await _git(path, "rev-parse", "--abbrev-ref", "HEAD")).strip()

    # Commits
    log_out = await _git(path, "log", "--pretty=format:%H|%an|%aI|%s", "-n", "20")
    commits: list[dict] = []
    for line in log_out.splitlines():
        parts = line.split("|", 3)
        if len(parts) == 4:
            commits.append({
                "sha": parts[0],
                "author": parts[1],
                "date": parts[2],
                "message": parts[3][:200],
            })

    # Dirty files
    status_out = await _git(path, "status", "--porcelain")
    dirty_count = sum(1 for line in status_out.splitlines() if line.strip())

    # Tracked files
    ls_out = await _git(path, "ls-files")
    all_tracked = [f.strip() for f in ls_out.splitlines() if f.strip()]
    tracked_count = len(all_tracked)
    top_files = [f for f in all_tracked if not _is_secret_path(f)][:200]

    # README + CHANGELOG
    readme_excerpt: str | None = None
    readme_hash: str | None = None
    for name in ("README.md", "README", "README.rst"):
        p = path / name
        if p.is_file() and not _is_secret_path(name):
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
                readme_excerpt = raw[:2000]
                readme_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
                break
            except Exception:
                continue

    changelog_excerpt: str | None = None
    for name in ("CHANGELOG.md", "CHANGELOG", "HISTORY.md"):
        p = path / name
        if p.is_file() and not _is_secret_path(name):
            try:
                changelog_excerpt = p.read_text(encoding="utf-8", errors="replace")[:1000]
                break
            except Exception:
                continue

    return {
        "branch": branch,
        "commits": commits,
        "dirty_count": dirty_count,
        "tracked_count": tracked_count,
        "top_files": top_files,
        "readme_excerpt": readme_excerpt,
        "readme_hash": readme_hash,
        "changelog_excerpt": changelog_excerpt,
    }


def derive_local_slug(path: Path) -> str:
    """Deterministic slug for a local path. Always `local:<absolute-path>`."""
    return f"local:{path.resolve()}"
