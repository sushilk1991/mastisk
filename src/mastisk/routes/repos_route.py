"""Repos API — register GitHub repos for ingestion + ideation."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from mastisk.agents.base import enqueue
from mastisk.bridges import github_bridge
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.settings import get_settings

router = APIRouter(prefix="/api/repos", tags=["repos"])


class AddRepoRequest(BaseModel):
    slug: str = Field(min_length=3)


class AddLocalRepoRequest(BaseModel):
    path: str = Field(min_length=1)


def _parse_slug(slug: str) -> tuple[str, str]:
    s = slug.strip().lower()
    if "/" not in s or s.count("/") != 1:
        raise HTTPException(status_code=422, detail="slug must be 'owner/repo'")
    owner, name = s.split("/", 1)
    if not owner or not name:
        raise HTTPException(status_code=422, detail="slug must be 'owner/repo'")
    return owner, name


@router.post("", status_code=201)
async def add_repo_endpoint(req: AddRepoRequest) -> dict:
    owner, name = _parse_slug(req.slug)
    # Verify the repo exists (via GitHub API)
    try:
        meta = await github_bridge.fetch_repo_metadata(owner, name)
    except github_bridge.GithubNotFound:
        # Tailor the hint to whether a PAT is configured. Without a PAT, a 404
        # most often means the repo is private and unreachable — so point the
        # user at Settings. With a PAT, the more likely cause is the PAT not
        # having access to this specific org/repo.
        has_pat = bool(get_settings().github.pat)
        if has_pat:
            detail = "repo not found on GitHub (or your PAT doesn't have access to it)"
        else:
            detail = "repo not found. If this is a private repo, add a GitHub PAT via Settings → GitHub, then retry."
        raise HTTPException(status_code=404, detail=detail)
    except github_bridge.GithubAuthError:
        raise HTTPException(status_code=401, detail="GitHub PAT invalid or missing scope")
    except github_bridge.GithubRateLimited:
        raise HTTPException(status_code=429, detail="GitHub rate limit exceeded; try later")
    except github_bridge.GithubError as e:
        raise HTTPException(status_code=502, detail=f"GitHub error: {e}")

    slug = meta["slug"]
    with connect() as conn:
        q.insert_repo(
            conn, slug=slug, owner=meta["owner"], name=meta["name"],
            display_name=meta["display_name"], description=meta["description"],
            default_branch=meta["default_branch"], is_private=meta["is_private"],
        )
    enqueue("github_poller", "poll", {"repo_slug": slug})
    return {
        "slug": slug,
        "display_name": meta["display_name"],
        "description": meta["description"],
    }


@router.post("/local", status_code=201)
async def add_local_repo_endpoint(req: AddLocalRepoRequest) -> dict:
    from pathlib import Path

    from mastisk.bridges import local_git_bridge
    p = Path(req.path).expanduser().resolve()
    if not p.is_dir():
        raise HTTPException(status_code=422, detail=f"not a directory: {p}")
    if not (p / ".git").exists():
        raise HTTPException(status_code=422, detail=f"not a git repo: {p}")
    slug = local_git_bridge.derive_local_slug(p)
    with connect() as conn:
        # Insert (or un-tombstone) a local repo row. Upsert, not INSERT OR
        # REPLACE: REPLACE deletes the row first, which cascades to
        # repo_snapshots / repo_idea_runs and drops context_md.
        conn.execute(
            """INSERT INTO repos
               (slug, source_type, owner, name, display_name, description,
                default_branch, is_private, local_path)
               VALUES (?, 'local', 'local', ?, ?, NULL, NULL, 0, ?)
               ON CONFLICT(slug) DO UPDATE SET
                   source_type = excluded.source_type,
                   owner = excluded.owner,
                   name = excluded.name,
                   display_name = excluded.display_name,
                   local_path = excluded.local_path,
                   deleted_at = NULL""",
            (slug, p.name, f"local:{p.name}", str(p)),
        )
    enqueue("github_poller", "poll", {"repo_slug": slug})
    return {"slug": slug, "local_path": str(p), "display_name": f"local:{p.name}"}


@router.get("")
async def list_repos_endpoint() -> list[dict]:
    with connect() as conn:
        rows = q.list_repos(conn)
    result = []
    for r in rows:
        with connect() as conn:
            snap = q.latest_repo_snapshot(conn, r["slug"])
        result.append({
            "slug": r["slug"],
            "source_type": r.get("source_type") or "github",
            "local_path": r.get("local_path"),
            "display_name": r["display_name"],
            "description": r["description"],
            "is_private": bool(r["is_private"]),
            "last_polled_at": r["last_polled_at"],
            "last_ideated_at": r["last_ideated_at"],
            "snapshot": {
                "polled_at": snap["polled_at"] if snap else None,
                "stars_count": snap["stars_count"] if snap else None,
                "forks_count": snap["forks_count"] if snap else None,
                "open_issues_count": snap["open_issues_count"] if snap else None,
                "open_prs_count": snap["open_prs_count"] if snap else None,
                "error": snap["error"] if snap else None,
            } if snap else None,
        })
    return result


def _repo_detail_payload(r: dict, snap: dict | None) -> dict:
    return {
        "slug": r["slug"],
        "source_type": r.get("source_type") or "github",
        "local_path": r.get("local_path"),
        "display_name": r["display_name"],
        "description": r["description"],
        "is_private": bool(r["is_private"]),
        "default_branch": r["default_branch"],
        "added_at": r["added_at"],
        "last_polled_at": r["last_polled_at"],
        "last_ideated_at": r["last_ideated_at"],
        "context_md": r["context_md"],
        "latest_snapshot": snap,
    }


# ─── Slug-indexed endpoints (frontend uses these) ─────────────────────────
# Local repos have slugs like `local:/Users/foo` with colons + slashes that
# collide with the /{owner}/{name} shape. We use `{slug:path}` with a verb
# root (`/by-slug/`, `/poll/`, `/ideate/`) so the greedy path converter has
# no other segments to compete with. Frontend can send slug raw — the only
# char that would break the URL is `#`, which slugs don't contain.

@router.get("/by-slug/{slug:path}")
async def get_repo_by_slug_endpoint(slug: str) -> dict:
    with connect() as conn:
        r = q.get_repo(conn, slug)
        if r is None or r.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="repo not found")
        snap = q.latest_repo_snapshot(conn, slug)
    return _repo_detail_payload(r, snap)


@router.delete("/by-slug/{slug:path}", status_code=204)
async def delete_repo_by_slug_endpoint(slug: str) -> None:
    with connect() as conn:
        r = q.get_repo(conn, slug)
        if r is None or r.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="repo not found")
        q.soft_delete_repo(conn, slug)


@router.post("/poll/{slug:path}", status_code=202)
async def poll_now_by_slug_endpoint(slug: str) -> dict:
    with connect() as conn:
        r = q.get_repo(conn, slug)
    if r is None or r.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="repo not found")
    enqueue("github_poller", "poll", {"repo_slug": slug})
    return {"ok": True}


@router.post("/ideate/{slug:path}", status_code=202)
async def ideate_now_by_slug_endpoint(slug: str) -> dict:
    with connect() as conn:
        r = q.get_repo(conn, slug)
    if r is None or r.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="repo not found")
    enqueue("github_ideator", "ideate", {"repo_slug": slug})
    return {"ok": True}


@router.get("/ideas/{slug:path}")
async def list_ideas_for_repo_endpoint(slug: str) -> dict:
    """Return notes produced by the GithubIdeator for this repo, plus the
    most recent ideation runs (with errors surfaced) for visibility.

    Same verb-root /ideas/ path convention as /poll/ and /ideate/ — keeps
    local-repo slugs (which contain slashes) from fighting the router.
    """
    with connect() as conn:
        r = q.get_repo(conn, slug)
        if r is None or r.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="repo not found")
        ideas = q.list_ideas_for_repo(conn, slug)
        runs = q.list_recent_idea_runs(conn, slug)
    return {"ideas": ideas, "runs": runs}


# ─── Filesystem browser — used by the Add Repo modal's folder picker ─────


class BrowseEntry(BaseModel):
    name: str
    path: str           # absolute
    is_dir: bool
    is_git_repo: bool   # only meaningful when is_dir


class BrowseResponse(BaseModel):
    path: str           # absolute, resolved
    parent: str | None  # absolute, or None if we're at the user's home
    entries: list[BrowseEntry]


@router.get("/browse")
async def browse_fs_endpoint(path: str | None = None) -> BrowseResponse:
    """List directory entries. Restricted to ~ by default.

    Each directory is flagged is_git_repo=True if it contains a .git dir.
    Symlinks are resolved; hidden entries (starting with '.') are excluded
    except for .git (so a cloned repo's top level still shows).
    """
    home = Path.home().resolve()

    if path is None or not path:
        start = home
    else:
        try:
            start = Path(path).expanduser().resolve()
        except (RuntimeError, OSError) as e:
            raise HTTPException(status_code=422, detail=f"invalid path: {e}")

    # Security: must be under $HOME
    try:
        start.relative_to(home)
    except ValueError:
        raise HTTPException(status_code=403, detail=f"path not under home dir: {start}")

    if not start.is_dir():
        raise HTTPException(status_code=404, detail=f"not a directory: {start}")

    parent: str | None = None
    if start != home:
        parent = str(start.parent)

    entries: list[BrowseEntry] = []
    try:
        for child in sorted(start.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            name = child.name
            # Skip hidden entries except .git (so we can show git folders)
            if name.startswith(".") and name != ".git":
                continue
            try:
                is_dir = child.is_dir()
            except OSError:
                continue
            is_git = False
            if is_dir:
                try:
                    is_git = (child / ".git").exists()
                except OSError:
                    is_git = False
            entries.append(BrowseEntry(
                name=name,
                path=str(child.resolve()),
                is_dir=is_dir,
                is_git_repo=is_git,
            ))
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=f"permission denied: {e}")

    return BrowseResponse(path=str(start), parent=parent, entries=entries)


# ─── /{owner}/{name} — legacy github-only endpoints, kept for bookmarks ───

@router.get("/{owner}/{name}")
async def get_repo_endpoint(owner: str, name: str) -> dict:
    slug = f"{owner.lower()}/{name.lower()}"
    with connect() as conn:
        r = q.get_repo(conn, slug)
        if r is None or r.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="repo not found")
        snap = q.latest_repo_snapshot(conn, slug)
    return _repo_detail_payload(r, snap)


@router.delete("/{owner}/{name}", status_code=204)
async def delete_repo_endpoint(owner: str, name: str) -> None:
    slug = f"{owner.lower()}/{name.lower()}"
    with connect() as conn:
        r = q.get_repo(conn, slug)
        if r is None or r.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="repo not found")
        q.soft_delete_repo(conn, slug)


@router.post("/{owner}/{name}/poll-now", status_code=202)
async def poll_now_endpoint(owner: str, name: str) -> dict:
    slug = f"{owner.lower()}/{name.lower()}"
    with connect() as conn:
        r = q.get_repo(conn, slug)
    if r is None or r.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="repo not found")
    enqueue("github_poller", "poll", {"repo_slug": slug})
    return {"ok": True}


@router.post("/{owner}/{name}/ideate-now", status_code=202)
async def ideate_now_endpoint(owner: str, name: str) -> dict:
    slug = f"{owner.lower()}/{name.lower()}"
    with connect() as conn:
        r = q.get_repo(conn, slug)
    if r is None or r.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="repo not found")
    enqueue("github_ideator", "ideate", {"repo_slug": slug})
    return {"ok": True}
