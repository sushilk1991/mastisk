"""GitHub bridge tests. Mocks httpx responses so no real API calls happen."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest


def _fake_response(status_code: int, json_data=None, headers: dict | None = None) -> httpx.Response:
    """Construct a real httpx.Response for testing."""
    request = httpx.Request("GET", "https://api.github.com/foo")
    resp = httpx.Response(
        status_code=status_code,
        json=json_data if json_data is not None else {},
        headers=headers or {},
        request=request,
    )
    return resp


@pytest.mark.asyncio
async def test_fetch_repo_metadata_parses_fields():
    from mastisk.bridges import github_bridge
    fake = _fake_response(200, json_data={
        "name": "claude-code",
        "owner": {"login": "anthropics"},
        "full_name": "anthropics/claude-code",
        "description": "Agentic coding tool",
        "default_branch": "main",
        "private": False,
        "stargazers_count": 12345,
        "forks_count": 678,
    })
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=fake)):
        result = await github_bridge.fetch_repo_metadata("anthropics", "claude-code")
    assert result["slug"] == "anthropics/claude-code"
    assert result["display_name"] == "anthropics/claude-code"
    assert result["is_private"] == 0
    assert result["stars_count"] == 12345


@pytest.mark.asyncio
async def test_fetch_repo_metadata_404_raises_notfound():
    from mastisk.bridges import github_bridge
    fake = _fake_response(404, json_data={"message": "Not Found"})
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=fake)):
        with pytest.raises(github_bridge.GithubNotFound):
            await github_bridge.fetch_repo_metadata("nope", "nope")


@pytest.mark.asyncio
async def test_fetch_repo_metadata_401_raises_autherror():
    from mastisk.bridges import github_bridge
    fake = _fake_response(401, json_data={"message": "Bad credentials"})
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=fake)):
        with pytest.raises(github_bridge.GithubAuthError):
            await github_bridge.fetch_repo_metadata("any", "any")


@pytest.mark.asyncio
async def test_fetch_repo_metadata_ratelimit_raises():
    from mastisk.bridges import github_bridge
    fake = _fake_response(403, headers={
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": "9999999999",
        "x-ratelimit-limit": "60",
    })
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=fake)):
        with pytest.raises(github_bridge.GithubRateLimited):
            await github_bridge.fetch_repo_metadata("any", "any")


@pytest.mark.asyncio
async def test_fetch_recent_commits():
    from mastisk.bridges import github_bridge
    fake = _fake_response(200, json_data=[
        {"sha": "abc123", "commit": {"message": "fix: bug\n\nlong body", "author": {"name": "Alice", "date": "2026-04-22T10:00:00Z"}}},
        {"sha": "def456", "commit": {"message": "feat: x", "author": {"name": "Bob", "date": "2026-04-21T08:00:00Z"}}},
    ])
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=fake)):
        commits = await github_bridge.fetch_recent_commits("a", "b", per_page=20)
    assert len(commits) == 2
    assert commits[0]["message"] == "fix: bug"  # first line only
    assert commits[0]["author"] == "Alice"


@pytest.mark.asyncio
async def test_fetch_recent_commits_tolerates_empty_message():
    """An empty commit message must not fail the whole poll."""
    from mastisk.bridges import github_bridge
    fake = _fake_response(200, json_data=[
        {"sha": "abc", "commit": {"message": "", "author": {"name": "A", "date": "2026-04-22T10:00:00Z"}}},
        {"sha": "def", "commit": {"message": None, "author": None}},
    ])
    with patch("httpx.AsyncClient.get", new=AsyncMock(return_value=fake)):
        commits = await github_bridge.fetch_recent_commits("a", "b")
    assert [c["message"] for c in commits] == ["", ""]


@pytest.mark.asyncio
async def test_fetch_open_issues_excludes_prs():
    from mastisk.bridges import github_bridge
    fake = _fake_response(200, json_data=[
        {"number": 1, "title": "real issue", "labels": [{"name": "bug"}]},
        {"number": 2, "title": "a PR", "labels": [], "pull_request": {"url": "..."}},
        {"number": 3, "title": "another issue", "labels": []},
    ])
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=fake)):
        issues = await github_bridge.fetch_open_issues("a", "b")
    # PR should be filtered out
    assert len(issues) == 2
    assert all("pull_request" not in str(i) for i in issues)
    assert issues[0]["labels"] == ["bug"]


@pytest.mark.asyncio
async def test_fetch_readme_decodes_base64_and_hashes():
    import base64 as b64

    from mastisk.bridges import github_bridge
    content = "# My Project\n\nA cool thing."
    encoded = b64.b64encode(content.encode()).decode()
    fake = _fake_response(200, json_data={"content": encoded})
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=fake)):
        result = await github_bridge.fetch_readme("a", "b")
    assert result is not None
    excerpt, sha = result
    assert "# My Project" in excerpt
    assert len(sha) == 64  # sha256 hex


@pytest.mark.asyncio
async def test_fetch_readme_returns_none_on_404():
    from mastisk.bridges import github_bridge
    fake = _fake_response(404)
    with patch.object(httpx.AsyncClient, "get", new=AsyncMock(return_value=fake)):
        result = await github_bridge.fetch_readme("a", "b")
    assert result is None


def test_schema_has_github_tables(db):
    tables = {
        r["name"] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "repos" in tables
    assert "repo_snapshots" in tables
    assert "repo_idea_runs" in tables


def test_repo_fk_cascade_on_delete(db):
    db.execute(
        """INSERT INTO repos (slug, owner, name) VALUES ('a/b', 'a', 'b')"""
    )
    db.execute(
        """INSERT INTO repo_snapshots (repo_slug, polled_at) VALUES ('a/b', '2026-04-22T10:00:00')"""
    )
    # Hard DELETE should cascade
    db.execute("DELETE FROM repos WHERE slug = 'a/b'")
    count = db.execute("SELECT COUNT(*) AS c FROM repo_snapshots WHERE repo_slug = 'a/b'").fetchone()["c"]
    assert count == 0
