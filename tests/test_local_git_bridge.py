"""Local git bridge tests. Uses a real temp git repo (no mocking — git is cheap)."""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest


def _init_temp_repo(tmp_path: Path) -> Path:
    p = tmp_path / "repo"
    p.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=p, check=True)
    (p / "README.md").write_text("# Test Repo\n\nA thing.")
    (p / ".env").write_text("SECRET=topsecret")  # should be scrubbed
    (p / "src").mkdir()
    (p / "src" / "main.py").write_text("print('hi')\n")
    subprocess.run(["git", "add", "."], cwd=p, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "initial"], cwd=p, check=True)
    return p


def test_is_secret_path():
    from mastisk.bridges.local_git_bridge import _is_secret_path
    assert _is_secret_path(".env")
    assert _is_secret_path(".env.local")
    assert _is_secret_path("config/.env.production")
    assert _is_secret_path("key.pem")
    assert _is_secret_path("aws-credentials.txt")
    assert _is_secret_path("node_modules/foo/bar.js")
    assert _is_secret_path(".git/objects/abc")
    assert not _is_secret_path("README.md")
    assert not _is_secret_path("src/main.py")


def test_fetch_local_repo_state(tmp_path):
    from mastisk.bridges.local_git_bridge import fetch_local_repo_state
    p = _init_temp_repo(tmp_path)

    state = asyncio.run(fetch_local_repo_state(p))

    assert state["branch"] in ("main", "master")
    assert len(state["commits"]) == 1
    assert state["commits"][0]["message"] == "initial"
    assert state["dirty_count"] == 0
    assert state["readme_excerpt"] is not None
    assert "# Test Repo" in state["readme_excerpt"]
    assert state["readme_hash"] is not None

    # The scrubbed .env file must NOT appear in top_files
    assert not any(".env" in f for f in state["top_files"])
    # But the tracked non-secret files should be there
    assert "README.md" in state["top_files"]
    assert "src/main.py" in state["top_files"]


def test_fetch_local_repo_state_raises_on_non_repo(tmp_path):
    from mastisk.bridges.local_git_bridge import LocalGitError, fetch_local_repo_state
    with pytest.raises(LocalGitError, match="not a git repo"):
        asyncio.run(fetch_local_repo_state(tmp_path))


def test_fetch_local_repo_state_raises_on_missing_dir(tmp_path):
    from mastisk.bridges.local_git_bridge import LocalGitError, fetch_local_repo_state
    with pytest.raises(LocalGitError):
        asyncio.run(fetch_local_repo_state(tmp_path / "does-not-exist"))


def test_derive_local_slug_is_absolute(tmp_path):
    from mastisk.bridges.local_git_bridge import derive_local_slug
    p = _init_temp_repo(tmp_path)
    slug = derive_local_slug(p)
    assert slug.startswith("local:")
    assert str(p.resolve()) in slug


def test_dirty_files_detected(tmp_path):
    from mastisk.bridges.local_git_bridge import fetch_local_repo_state
    p = _init_temp_repo(tmp_path)
    (p / "uncommitted.md").write_text("new file")
    state = asyncio.run(fetch_local_repo_state(p))
    assert state["dirty_count"] >= 1
