"""GithubPoller tests — mocks the bridge entirely."""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest


def _seed_repo(db, slug="a/b", owner="a", name="b"):
    db.execute(
        """INSERT INTO repos (slug, owner, name, display_name, description)
           VALUES (?, ?, ?, ?, 'desc')""",
        (slug, owner, name, f"{owner}/{name}"),
    )


@pytest.fixture
def mock_bridge():
    async def fake_meta(o, n):
        return {
            "slug": f"{o}/{n}", "owner": o, "name": n, "display_name": f"{o}/{n}",
            "description": "test repo", "default_branch": "main", "is_private": 0,
            "stars_count": 42, "forks_count": 3,
        }
    async def fake_commits(o, n, per_page=20):
        return [
            {"sha": "abc123def456", "message": "fix: a bug", "author": "Alice", "date": "2026-04-22T10:00:00Z"},
            {"sha": "789abc", "message": "feat: new", "author": "Bob", "date": "2026-04-21T08:00:00Z"},
        ]
    async def fake_issues(o, n, per_page=10):
        return [{"number": 1, "title": "an open issue", "labels": ["bug"]}]
    async def fake_prs(o, n, per_page=10):
        return [{"number": 2, "title": "a PR", "author": "Carol"}]
    async def fake_readme(o, n):
        return ("# Test Repo\n\nA thing.", "a" * 64)
    with patch("mastisk.agents.github_poller.github_bridge.fetch_repo_metadata", side_effect=fake_meta), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_recent_commits", side_effect=fake_commits), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_open_issues", side_effect=fake_issues), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_open_prs", side_effect=fake_prs), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_readme", side_effect=fake_readme):
        yield


def test_poller_writes_snapshot_and_context(db, vault_tmp, mock_bridge):
    from mastisk.agents.base import enqueue
    from mastisk.agents.github_poller import GithubPoller
    _seed_repo(db)
    enqueue("github_poller", "poll", {"repo_slug": "a/b"})

    asyncio.run(GithubPoller().run_once())

    snap = db.execute("SELECT * FROM repo_snapshots WHERE repo_slug='a/b'").fetchone()
    assert snap is not None
    assert snap["error"] is None
    assert snap["stars_count"] == 42
    assert snap["open_issues_count"] == 1
    assert snap["open_prs_count"] == 1
    assert "fix: a bug" in snap["commits_json"]

    repo = db.execute("SELECT * FROM repos WHERE slug='a/b'").fetchone()
    assert repo["context_md"] is not None
    assert "# a/b" in repo["context_md"]
    assert "fix: a bug" in repo["context_md"]
    assert repo["last_polled_at"] is not None


def test_poller_handles_not_found(db, vault_tmp):
    from mastisk.agents.base import enqueue
    from mastisk.agents.github_poller import GithubPoller
    from mastisk.bridges import github_bridge
    _seed_repo(db, slug="gone/zero", owner="gone", name="zero")
    enqueue("github_poller", "poll", {"repo_slug": "gone/zero"})
    async def raise_404(*a, **kw): raise github_bridge.GithubNotFound("404")
    with patch("mastisk.agents.github_poller.github_bridge.fetch_repo_metadata", side_effect=raise_404), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_recent_commits", side_effect=raise_404), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_open_issues", side_effect=raise_404), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_open_prs", side_effect=raise_404), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_readme", side_effect=raise_404):
        asyncio.run(GithubPoller().run_once())

    snap = db.execute("SELECT * FROM repo_snapshots WHERE repo_slug='gone/zero'").fetchone()
    assert snap is not None
    assert "not-found" in (snap["error"] or "")


def test_poller_handles_auth_error(db, vault_tmp):
    from mastisk.agents.base import enqueue
    from mastisk.agents.github_poller import GithubPoller
    from mastisk.bridges import github_bridge
    _seed_repo(db)
    enqueue("github_poller", "poll", {"repo_slug": "a/b"})
    async def raise_auth(*a, **kw): raise github_bridge.GithubAuthError("401")
    with patch("mastisk.agents.github_poller.github_bridge.fetch_repo_metadata", side_effect=raise_auth), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_recent_commits", side_effect=raise_auth), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_open_issues", side_effect=raise_auth), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_open_prs", side_effect=raise_auth), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_readme", side_effect=raise_auth):
        asyncio.run(GithubPoller().run_once())

    snap = db.execute("SELECT * FROM repo_snapshots WHERE repo_slug='a/b'").fetchone()
    assert snap is not None
    assert "auth" in (snap["error"] or "")


def test_poller_rate_limited_defers(db, vault_tmp):
    """Rate limit → no snapshot written; will retry next tick."""
    from mastisk.agents.base import enqueue
    from mastisk.agents.github_poller import GithubPoller
    from mastisk.bridges import github_bridge
    _seed_repo(db)
    enqueue("github_poller", "poll", {"repo_slug": "a/b"})
    async def raise_rl(*a, **kw): raise github_bridge.GithubRateLimited("limited")
    with patch("mastisk.agents.github_poller.github_bridge.fetch_repo_metadata", side_effect=raise_rl), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_recent_commits", side_effect=raise_rl), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_open_issues", side_effect=raise_rl), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_open_prs", side_effect=raise_rl), \
         patch("mastisk.agents.github_poller.github_bridge.fetch_readme", side_effect=raise_rl):
        asyncio.run(GithubPoller().run_once())

    count = db.execute("SELECT COUNT(*) AS c FROM repo_snapshots WHERE repo_slug='a/b'").fetchone()["c"]
    assert count == 0  # no snapshot on rate limit — we retry next tick


def test_poller_polls_local_repo(db, vault_tmp, tmp_path):
    """End-to-end: add a local repo row, run poller, expect snapshot + context_md."""
    import subprocess

    from mastisk.agents.base import enqueue
    from mastisk.agents.github_poller import GithubPoller

    # Build a real temp git repo
    p = tmp_path / "myproj"
    p.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.email", "x@x.x"], cwd=p, check=True)
    subprocess.run(["git", "config", "user.name", "x"], cwd=p, check=True)
    (p / "README.md").write_text("# Local Test")
    subprocess.run(["git", "add", "."], cwd=p, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first commit"], cwd=p, check=True)

    slug = f"local:{p.resolve()}"
    db.execute(
        """INSERT INTO repos (slug, source_type, owner, name, display_name, local_path)
           VALUES (?, 'local', 'local', 'myproj', 'local:myproj', ?)""",
        (slug, str(p)),
    )
    enqueue("github_poller", "poll", {"repo_slug": slug})

    asyncio.run(GithubPoller().run_once())

    snap = db.execute("SELECT * FROM repo_snapshots WHERE repo_slug = ?", (slug,)).fetchone()
    assert snap is not None, "expected a snapshot row"
    assert snap["error"] is None
    # commits_json contains 'first commit'
    assert "first commit" in (snap["commits_json"] or "")
    repo = db.execute("SELECT context_md FROM repos WHERE slug = ?", (slug,)).fetchone()
    assert repo["context_md"] is not None
    assert "# local:myproj" in repo["context_md"] or "# myproj" in repo["context_md"]
