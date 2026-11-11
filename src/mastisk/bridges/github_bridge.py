"""GitHub REST v3 bridge. Minimal async wrapper around the subset of endpoints we use.

Auth via PAT (optional). Surfaces rate-limit info from response headers. Caller
is responsible for throttling; this bridge only reports what it sees.
"""
from __future__ import annotations

import base64
import hashlib
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from mastisk.settings import get_settings

log = logging.getLogger("mastisk.github")

BASE_URL = "https://api.github.com"
UA = "mastisk-github-agent/0.1"


class GithubError(RuntimeError):
    """Raised when GitHub returns an error we can't recover from."""
    pass


class GithubRateLimited(GithubError):
    pass


class GithubAuthError(GithubError):
    pass


class GithubNotFound(GithubError):
    pass


@dataclass
class RateLimit:
    remaining: int
    reset_at: int   # unix ts
    limit: int


def _headers() -> dict[str, str]:
    s = get_settings().github
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": UA,
    }
    if s.pat:
        h["Authorization"] = f"Bearer {s.pat}"
    return h


def _parse_ratelimit(resp: httpx.Response) -> RateLimit | None:
    try:
        return RateLimit(
            remaining=int(resp.headers.get("x-ratelimit-remaining", "0")),
            reset_at=int(resp.headers.get("x-ratelimit-reset", "0")),
            limit=int(resp.headers.get("x-ratelimit-limit", "0")),
        )
    except ValueError:
        return None


async def _get(client: httpx.AsyncClient, path: str) -> tuple[httpx.Response, RateLimit | None]:
    resp = await client.get(path, headers=_headers())
    rl = _parse_ratelimit(resp)
    if resp.status_code == 401:
        raise GithubAuthError("GitHub PAT is invalid or lacks scope")
    if resp.status_code == 403 and rl and rl.remaining == 0:
        raise GithubRateLimited(f"rate-limited until {rl.reset_at}")
    if resp.status_code == 404:
        raise GithubNotFound(f"not found: {path}")
    if not resp.is_success:
        raise GithubError(f"GET {path} → {resp.status_code}: {resp.text[:200]}")
    return resp, rl


async def fetch_repo_metadata(owner: str, name: str) -> dict[str, Any]:
    """GET /repos/{owner}/{name}. Returns the parsed JSON + a normalized subset for our use."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp, _ = await _get(client, f"/repos/{owner}/{name}")
    data = resp.json()
    return {
        "slug": f"{owner}/{name}".lower(),
        "owner": data["owner"]["login"],
        "name": data["name"],
        "display_name": data["full_name"],
        "description": data.get("description"),
        "default_branch": data.get("default_branch"),
        "is_private": 1 if data.get("private") else 0,
        "stars_count": int(data.get("stargazers_count") or 0),
        "forks_count": int(data.get("forks_count") or 0),
    }


async def fetch_recent_commits(owner: str, name: str, per_page: int = 20) -> list[dict]:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp, _ = await _get(client, f"/repos/{owner}/{name}/commits?per_page={per_page}")
    out = []
    for c in resp.json():
        out.append({
            "sha": c.get("sha"),
            "message": next(iter((c.get("commit", {}).get("message") or "").splitlines()), "")[:200],
            "author": (c.get("commit", {}).get("author", {}) or {}).get("name"),
            "date": (c.get("commit", {}).get("author", {}) or {}).get("date"),
        })
    return out


async def fetch_open_issues(owner: str, name: str, per_page: int = 10) -> list[dict]:
    """Returns non-PR open issues."""
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp, _ = await _get(
            client, f"/repos/{owner}/{name}/issues?state=open&per_page={per_page}"
        )
    out = []
    for i in resp.json():
        # GitHub's /issues endpoint returns PRs too; filter them out
        if "pull_request" in i:
            continue
        out.append({
            "number": i.get("number"),
            "title": i.get("title", "")[:200],
            "labels": [lbl.get("name") for lbl in (i.get("labels") or []) if lbl.get("name")],
        })
    return out


async def fetch_open_prs(owner: str, name: str, per_page: int = 10) -> list[dict]:
    async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
        resp, _ = await _get(
            client, f"/repos/{owner}/{name}/pulls?state=open&per_page={per_page}"
        )
    out = []
    for p in resp.json():
        out.append({
            "number": p.get("number"),
            "title": p.get("title", "")[:200],
            "author": (p.get("user") or {}).get("login"),
        })
    return out


async def fetch_readme(owner: str, name: str) -> tuple[str, str] | None:
    """Returns (excerpt, sha256_hash) for the README. None if no README exists."""
    try:
        async with httpx.AsyncClient(base_url=BASE_URL, timeout=30.0) as client:
            resp, _ = await _get(client, f"/repos/{owner}/{name}/readme")
    except GithubNotFound:
        return None
    data = resp.json()
    content_b64 = data.get("content", "")
    try:
        raw = base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception:
        raw = content_b64
    excerpt = raw[:2000]
    h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return excerpt, h
