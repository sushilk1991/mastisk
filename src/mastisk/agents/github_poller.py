"""GithubPoller — hourly per-repo snapshot + context_md regeneration.

Per spec §8.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from mastisk.agents.base import Agent, enqueue
from mastisk.bridges import github_bridge
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.github_poller")


def _compose_context_md(
    meta: dict,
    readme_excerpt: str | None,
    commits: list[dict],
    issues: list[dict],
    prs: list[dict],
) -> str:
    parts = []
    parts.append(f"# {meta['display_name'] or meta['slug']}\n")
    if meta.get("description"):
        parts.append(f"_{meta['description']}_\n")
    parts.append(
        f"**stars:** {meta.get('stars_count', 0)} · **forks:** {meta.get('forks_count', 0)} · "
        f"**visibility:** {'private' if meta.get('is_private') else 'public'}\n"
    )
    if readme_excerpt:
        parts.append("## README (excerpt)\n")
        parts.append(readme_excerpt.strip() + "\n")
    if commits:
        parts.append("\n## Recent commits\n")
        for c in commits[:10]:
            parts.append(f"- `{c['sha'][:7] if c.get('sha') else 'unknown'}` {c.get('message','')} — _{c.get('author','')}_")
    if issues:
        parts.append("\n## Open issues\n")
        for i in issues[:5]:
            lbl = f" [{','.join(i.get('labels', []))}]" if i.get("labels") else ""
            parts.append(f"- #{i['number']} {i['title']}{lbl}")
    if prs:
        parts.append("\n## Open PRs\n")
        for p in prs[:5]:
            parts.append(f"- #{p['number']} {p['title']} — _{p.get('author','')}_")
    return "\n".join(parts) + "\n"


def _compose_local_context_md(repo: dict, state: dict) -> str:
    parts = []
    parts.append(f"# {repo.get('display_name') or repo['slug']}\n")
    parts.append(f"**path:** `{repo.get('local_path')}`\n")
    parts.append(
        f"**branch:** `{state['branch']}` · "
        f"**dirty files:** {state['dirty_count']} · "
        f"**tracked:** {state['tracked_count']}\n"
    )
    if state.get("readme_excerpt"):
        parts.append("## README (excerpt)\n")
        parts.append(state["readme_excerpt"].strip() + "\n")
    if state.get("changelog_excerpt"):
        parts.append("\n## CHANGELOG (excerpt)\n")
        parts.append(state["changelog_excerpt"].strip() + "\n")
    if state.get("commits"):
        parts.append("\n## Recent commits\n")
        for c in state["commits"][:10]:
            parts.append(f"- `{c['sha'][:7]}` {c['message']} — _{c['author']}_")
    if state.get("top_files"):
        parts.append("\n## Top-level file tree (up to 200 files, scrubbed)\n")
        parts.append("```")
        parts.extend(state["top_files"][:50])  # show 50 in the context
        parts.append("```")
    return "\n".join(parts) + "\n"


class GithubPoller(Agent):
    name: ClassVar[str] = "github_poller"
    tick_seconds: ClassVar[int] = 600  # 10-min tick; filters by poll_interval_minutes per repo

    async def run_once(self) -> None:
        """Enqueue due repos, then process one job (standard Agent.run_once semantics)."""
        if self.disabled_tick():
            return
        settings = get_settings().github
        now = datetime.now(timezone.utc)
        due_cutoff = now - timedelta(minutes=settings.poll_interval_minutes)

        with connect() as conn:
            rows = conn.execute(
                """SELECT slug FROM repos
                   WHERE deleted_at IS NULL
                     AND (last_polled_at IS NULL OR last_polled_at < ?)
                   ORDER BY COALESCE(last_polled_at, '1970-01-01') ASC""",
                (due_cutoff.isoformat(),),
            ).fetchall()

        for row in rows:
            slug = row["slug"]
            # Avoid enqueuing if there's already a queued/running job for this repo
            with connect() as conn:
                existing = conn.execute(
                    """SELECT 1 FROM jobs
                       WHERE agent = 'github_poller' AND status IN ('queued', 'running')
                         AND payload_json = ?""",
                    (json.dumps({"repo_slug": slug}),),
                ).fetchone()
            if existing:
                continue
            enqueue("github_poller", "poll", {"repo_slug": slug})

        await super().run_once()  # process one job

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job["payload_json"] or "{}")
        slug = payload.get("repo_slug")
        if not slug:
            log.warning("github_poller: no repo_slug in job %s", job["id"])
            return

        with connect() as conn:
            repo = q.get_repo(conn, slug)
        if repo is None or repo.get("deleted_at") is not None:
            log.info("github_poller: skip deleted/missing repo %s", slug)
            return

        source_type = repo.get("source_type") or "github"
        if source_type == "local":
            await self._poll_local(repo)
        else:
            await self._poll_github(repo)

    async def _poll_github(self, repo: dict) -> None:
        slug = repo["slug"]
        owner = repo["owner"]
        name = repo["name"]
        now_iso = datetime.now(timezone.utc).isoformat()

        # Fetch everything we need. Any failure writes an error snapshot + updates last_polled_at.
        try:
            meta_task = asyncio.create_task(github_bridge.fetch_repo_metadata(owner, name))
            commits_task = asyncio.create_task(github_bridge.fetch_recent_commits(owner, name, per_page=20))
            issues_task = asyncio.create_task(github_bridge.fetch_open_issues(owner, name, per_page=10))
            prs_task = asyncio.create_task(github_bridge.fetch_open_prs(owner, name, per_page=10))
            readme_task = asyncio.create_task(github_bridge.fetch_readme(owner, name))
            meta, commits, issues, prs, readme_result = await asyncio.gather(
                meta_task, commits_task, issues_task, prs_task, readme_task,
            )
        except github_bridge.GithubRateLimited as e:
            log.warning("github_poller: rate-limited on %s (%s); will retry next tick", slug, e)
            self.emit_feed(verb="rate-limited", obj=slug, kind="repo")
            return
        except github_bridge.GithubAuthError as e:
            log.error("github_poller: auth failed (%s)", e)
            self.emit_feed(verb="auth-failed", obj=slug, kind="repo")
            with connect() as conn:
                q.insert_repo_snapshot(
                    conn, repo_slug=slug, polled_at=now_iso, error=f"auth: {e}",
                )
                q.mark_repo_polled(conn, slug=slug, polled_at=now_iso)
            return
        except github_bridge.GithubNotFound as e:
            log.warning("github_poller: not found on %s (%s)", slug, e)
            self.emit_feed(verb="not-found", obj=slug, kind="repo")
            with connect() as conn:
                q.insert_repo_snapshot(
                    conn, repo_slug=slug, polled_at=now_iso, error=f"not-found: {e}",
                )
                q.mark_repo_polled(conn, slug=slug, polled_at=now_iso)
            return
        except Exception as e:
            log.exception("github_poller: unexpected error on %s", slug)
            with connect() as conn:
                q.insert_repo_snapshot(
                    conn, repo_slug=slug, polled_at=now_iso, error=f"{type(e).__name__}: {e}",
                )
                q.mark_repo_polled(conn, slug=slug, polled_at=now_iso)
            return

        readme_excerpt = None
        readme_hash = None
        if readme_result is not None:
            readme_excerpt, readme_hash = readme_result

        latest_commit_sha = commits[0]["sha"] if commits else None
        latest_commit_at = commits[0].get("date") if commits else None

        # Update repo metadata (stars/forks/description/display_name may have changed)
        with connect() as conn:
            q.insert_repo(
                conn,
                slug=slug, owner=meta["owner"], name=meta["name"],
                display_name=meta["display_name"], description=meta["description"],
                default_branch=meta["default_branch"], is_private=meta["is_private"],
            )

        context_md = _compose_context_md(meta, readme_excerpt, commits, issues, prs)

        with connect() as conn:
            q.insert_repo_snapshot(
                conn, repo_slug=slug, polled_at=now_iso,
                latest_commit_sha=latest_commit_sha, latest_commit_at=latest_commit_at,
                open_issues_count=len(issues), open_prs_count=len(prs),
                stars_count=meta.get("stars_count"), forks_count=meta.get("forks_count"),
                commits_json=json.dumps(commits), issues_json=json.dumps(issues),
                prs_json=json.dumps(prs),
                readme_hash=readme_hash, readme_excerpt=readme_excerpt,
            )
            q.update_repo_context(conn, slug=slug, context_md=context_md, polled_at=now_iso)

        self.emit_feed(verb="polled", obj=slug, kind="repo",
                       payload={"commits": len(commits), "issues": len(issues), "prs": len(prs)})

    async def _poll_local(self, repo: dict) -> None:
        from pathlib import Path
        from mastisk.bridges import local_git_bridge
        slug = repo["slug"]
        local_path = repo.get("local_path")
        if not local_path:
            log.warning("github_poller(local): no local_path for %s", slug)
            return
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            state = await local_git_bridge.fetch_local_repo_state(Path(local_path))
        except local_git_bridge.LocalGitError as e:
            log.warning("github_poller(local): %s failed (%s)", slug, e)
            with connect() as conn:
                q.insert_repo_snapshot(
                    conn, repo_slug=slug, polled_at=now_iso, error=f"local: {e}",
                )
                q.mark_repo_polled(conn, slug=slug, polled_at=now_iso)
            return

        context_md = _compose_local_context_md(repo, state)
        commits = state["commits"]
        latest_commit_sha = commits[0]["sha"] if commits else None
        latest_commit_at = commits[0].get("date") if commits else None

        with connect() as conn:
            q.insert_repo_snapshot(
                conn, repo_slug=slug, polled_at=now_iso,
                latest_commit_sha=latest_commit_sha,
                latest_commit_at=latest_commit_at,
                open_issues_count=None, open_prs_count=None,
                stars_count=None, forks_count=None,
                commits_json=json.dumps(commits),
                issues_json=None, prs_json=None,
                readme_hash=state.get("readme_hash"),
                readme_excerpt=state.get("readme_excerpt"),
            )
            q.update_repo_context(conn, slug=slug, context_md=context_md, polled_at=now_iso)

        self.emit_feed(verb="polled", obj=slug, kind="repo",
                       payload={"source": "local", "commits": len(commits), "dirty": state["dirty_count"]})
