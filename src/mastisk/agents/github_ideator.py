"""GithubIdeator — reads repo context_md and generates idea-notes.

Per spec §9, §10. Each idea becomes a note in vault/_notes/inbox/, picked up
by the Notetaker on the next 30s scan — so ideas flow through classification,
escalation, and are roundtable-able exactly like user-authored notes.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import ClassVar

from mastisk.agents.base import Agent, enqueue
from mastisk.agents.registry import resolve_prompt
from mastisk.bridges import claude_bridge, codex_bridge, ollama_bridge
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import notes_inbox_dir, vault_dir
from mastisk.routes.notes import atomic_write, derive_slug
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.github_ideator")


IDEATE_PROMPT = """You are generating fresh research ideas for a user based on a GitHub repository they've asked you to track.

## About the user
{identity}

## Repository
slug: {slug}
description: {description}

## Rolling context
{context_md}

## Your job
Generate EXACTLY {ideas_per_run} distinct, specific, research-worthy ideas. Each idea should be something the user would find interesting to think about given their stated interests — NOT a generic "consider X". Tie each idea to something concrete from the context (a recent commit message, an open issue, a README claim, etc.).

Respond with a single JSON array — no prose, no markdown, no preamble:

[
  {{
    "title": "short noun phrase, max 60 chars",
    "framing": "2-4 sentences that frame the idea concretely. Reference the specific commit/issue/README claim that triggered it.",
    "angles": ["angle 1", "angle 2"],
    "tags": ["tag-a", "tag-b"]
  }}
]

Rules:
- Ideas must be distinct — no near-duplicates across entries.
- "angles" are 2-3 bullet directions someone could explore; keep them crisp.
- "tags" are 1-3 lowercased kebab-case topic tags.
- Avoid generic ideas ("write a blog post about X"); aim for research threads.
"""


def _extract_json_array(text: str) -> list:
    """Lenient JSON array extractor. Handles fenced blocks and loose brackets."""
    t = text.strip()
    # Try raw parse
    try:
        parsed = json.loads(t)
        if isinstance(parsed, list):
            return parsed
    except Exception:
        pass
    # Try fenced block
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", t, re.DOTALL)
    if m:
        parsed = json.loads(m.group(1))
        if isinstance(parsed, list):
            return parsed
    # Last resort: first [ through last ]
    start = t.find("[")
    end = t.rfind("]")
    if start >= 0 and end > start:
        parsed = json.loads(t[start:end + 1])
        if isinstance(parsed, list):
            return parsed
    raise ValueError(f"no JSON array found in: {t[:200]}")


def _idea_to_note_body(slug: str, idea: dict) -> str:
    """Format an idea dict as a note body with the repo preamble."""
    title = str(idea.get("title", "")).strip()[:200] or "(untitled)"
    framing = str(idea.get("framing", "")).strip()
    angles = [str(a).strip() for a in (idea.get("angles") or []) if a]
    tags = [str(t).strip().lower() for t in (idea.get("tags") or []) if t]

    lines = [f"> From repo: {slug}", "", f"# {title}", "", framing]
    if angles:
        lines.extend(["", "## Angles"])
        lines.extend(f"- {a}" for a in angles)
    if tags:
        lines.extend(["", f"tags: {', '.join(tags)}"])
    return "\n".join(lines) + "\n"


class GithubIdeator(Agent):
    name: ClassVar[str] = "github_ideator"
    tick_seconds: ClassVar[int] = 600  # 10-min tick; per-repo 24h cadence enforced below

    async def run_once(self) -> None:
        """Enqueue repos due for ideation, then process one job."""
        if self.disabled_tick():
            return
        settings = get_settings().github
        now = datetime.now(timezone.utc)
        # Cutoff for 'last_ideated_at'
        cutoff = now - timedelta(hours=settings.ideate_min_interval_hours)

        with connect() as conn:
            rows = conn.execute(
                """SELECT r.slug FROM repos r
                   WHERE r.deleted_at IS NULL
                     AND (r.last_ideated_at IS NULL OR r.last_ideated_at < ?)
                     AND EXISTS (
                         SELECT 1 FROM repo_snapshots s
                         WHERE s.repo_slug = r.slug AND s.error IS NULL
                     )
                   ORDER BY COALESCE(r.last_ideated_at, '1970-01-01') ASC""",
                (cutoff.isoformat(),),
            ).fetchall()

        for row in rows:
            slug = row["slug"]
            with connect() as conn:
                existing = conn.execute(
                    """SELECT 1 FROM jobs
                       WHERE agent = 'github_ideator' AND status IN ('queued', 'running')
                         AND payload_json = ?""",
                    (json.dumps({"repo_slug": slug}),),
                ).fetchone()
            if existing:
                continue
            enqueue("github_ideator", "ideate", {"repo_slug": slug})

        await super().run_once()

    async def _handle(self, job: dict) -> None:
        payload = json.loads(job["payload_json"] or "{}")
        slug = payload.get("repo_slug")
        if not slug:
            log.warning("github_ideator: no repo_slug in job %s", job["id"])
            return

        settings = get_settings().github

        with connect() as conn:
            repo = q.get_repo(conn, slug)
            snapshot = q.latest_repo_snapshot(conn, slug) if repo else None

        if repo is None or repo.get("deleted_at") is not None:
            log.info("github_ideator: skip deleted/missing repo %s", slug)
            return
        if not repo.get("context_md"):
            log.info("github_ideator: skip %s (no context yet — wait for poller)", slug)
            return
        if snapshot is None or snapshot.get("error"):
            log.info("github_ideator: skip %s (no good snapshot)", slug)
            return

        # Build prompt
        identity = self.load_identity()
        prompt = resolve_prompt("github_ideator", "ideate", IDEATE_PROMPT).format(
            identity=identity,
            slug=slug,
            description=repo.get("description") or "(no description)",
            context_md=repo["context_md"],
            ideas_per_run=settings.ideas_per_run,
        )

        # Call Claude → Codex → Ollama
        now_iso = datetime.now(timezone.utc).isoformat()
        ideas: list[dict] = []
        model_used = ""
        error_msg: str | None = None
        try:
            result = await claude_bridge.run_claude(prompt=prompt)
            text = result.get("text", "") if isinstance(result, dict) else str(result)
            ideas = _extract_json_array(text)
            model_used = "claude"
        except Exception as e:
            log.warning("github_ideator: Claude failed for %s (%s); trying Codex", slug, e)
            try:
                result = await codex_bridge.run_codex(prompt)
                text = result.get("text", "") if isinstance(result, dict) else str(result)
                ideas = _extract_json_array(text)
                model_used = "codex"
            except Exception as e2:
                log.warning(
                    "github_ideator: Codex failed for %s (%s); falling back to Ollama",
                    slug, e2,
                )
                try:
                    result = await ollama_bridge.run_ollama(prompt, get_settings().summarize_model_heavy)
                    text = result.get("text", "")
                    ideas = _extract_json_array(text)
                    model_used = "ollama"
                except Exception as e3:
                    log.error("github_ideator: Ollama also failed for %s (%s)", slug, e3)
                    error_msg = (
                        f"all models failed: claude={e}; codex={e2}; ollama={e3}"
                    )

        # If we got ideas, write each as a note
        note_ids: list[int] = []
        if ideas:
            ts = datetime.now().astimezone()
            for i, idea in enumerate(ideas[: settings.ideas_per_run]):
                try:
                    body = _idea_to_note_body(slug, idea)
                    # Derive slug from the idea's title; fall back to generic if missing
                    title_seed = idea.get("title") or f"{slug}-idea-{i}"
                    file_slug = derive_slug(str(title_seed), ts)
                    target = notes_inbox_dir() / f"{file_slug}.md"
                    atomic_write(target, body)
                    rel_path = str(target.relative_to(vault_dir()))
                    with connect() as conn:
                        note_id = q.insert_note(
                            conn, slug=file_slug, path=rel_path, body=body,
                            source="file",  # came from an external source, not direct user capture
                            created_at=ts,
                        )
                        row = q.get_note(conn, note_id)
                    # Reconcile filename if slug-collision suffix applied
                    actual_path = vault_dir() / row["path"]
                    if actual_path != target:
                        target.rename(actual_path)
                    note_ids.append(note_id)
                except Exception:
                    log.exception("github_ideator: failed to write idea %d for %s", i, slug)

        # Record the ideation run
        with connect() as conn:
            q.insert_repo_idea_run(
                conn, repo_slug=slug, ideated_at=now_iso,
                snapshot_id=snapshot["id"],
                note_ids_json=json.dumps(note_ids),
                model=model_used or "none",
                error=error_msg,
            )
            if note_ids:
                # Only mark ideated if we actually wrote something, so a failure retries
                q.mark_repo_ideated(conn, slug=slug, ideated_at=now_iso)

        self.emit_feed(
            verb="ideated" if note_ids else "ideation-failed",
            obj=slug, kind="repo",
            payload={"note_count": len(note_ids), "model": model_used or "none"},
        )
