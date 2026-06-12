"""Blog writer API — on-demand long-form drafts from recent synthesis.

See docs/superpowers/specs/2026-04-22-blog-writer-design.md §11 for the
endpoint contract.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.agents.base import enqueue
from mastisk.agents.blog_writer import candidate_count
from mastisk.content.sync import blog_content_source, content_payload
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.paths import vault_dir
from mastisk.settings import get_settings

router = APIRouter(prefix="/api/blog-posts", tags=["blog"])


class CreateBlogRequest(BaseModel):
    """POST /api/blog-posts body. Theme is optional free text (≤500 chars)."""
    theme: str = ""
    window_days: int = Field(default=14)

    @field_validator("theme")
    @classmethod
    def _theme_len(cls, v: str) -> str:
        if len(v) > 500:
            raise ValueError("theme must be ≤500 chars")
        return v


@router.post("", status_code=202)
async def create_blog_post_endpoint(req: CreateBlogRequest) -> dict:
    settings = get_settings().blog
    if req.window_days not in settings.allowed_window_days:
        raise HTTPException(
            status_code=422,
            detail=f"window_days must be one of {settings.allowed_window_days}",
        )

    # Pre-enqueue empty-pool check. Cheaper than enqueueing a job that's
    # guaranteed to fail and clearer UX. Matches spec §6.4 / §11.
    if candidate_count(req.window_days) == 0:
        raise HTTPException(
            status_code=422,
            detail="no sources in window — try widening window_days or capturing more notes first",
        )

    theme_clean = req.theme.strip()
    with connect() as conn:
        bp_id = q.create_blog_post(
            conn, theme=theme_clean, window_days=req.window_days,
        )
        # Topic-suggester link: if the user drafted from an active suggestion
        # whose title equals the theme, mark it used. Title comparison is
        # case-insensitive on a trimmed value to absorb minor PWA round-trip
        # noise. Multi-row safety: in the unlikely event two active rows
        # share a title, all of them get linked to this draft so neither
        # lingers in the active list.
        if theme_clean:
            conn.execute(
                """UPDATE topic_suggestions
                   SET used_blog_id = ?
                   WHERE used_blog_id IS NULL
                     AND dismissed_at IS NULL
                     AND lower(trim(title)) = lower(?)""",
                (bp_id, theme_clean),
            )
    enqueue("blog_writer", "draft", {"blog_post_id": bp_id})
    return {"id": bp_id, "status": "pending"}


@router.get("")
async def list_blog_posts_endpoint(limit: int = 50, before: int | None = None) -> list[dict]:
    if limit < 1 or limit > 500:
        raise HTTPException(status_code=422, detail="limit must be 1..500")
    with connect() as conn:
        rows = q.list_blog_posts(conn, limit=limit, before_id=before)
    return [_summary(r) for r in rows]


@router.get("/{bp_id}")
async def get_blog_post_endpoint(bp_id: int) -> dict:
    with connect() as conn:
        bp = q.get_blog_post(conn, bp_id)
        if bp is None or bp.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="blog post not found")
        source_rows = q.list_blog_post_sources(conn, bp_id)
        sources = [_resolve_source(conn, s) for s in source_rows]

    body_md, file_error = _read_body(bp)
    return {
        **_detail(bp),
        "body_md": body_md,
        "error": file_error or bp.get("error"),
        "sources": sources,
    }


@router.post("/{bp_id}/regenerate", status_code=202)
async def regenerate_blog_post_endpoint(bp_id: int) -> dict:
    with connect() as conn:
        bp = q.get_blog_post(conn, bp_id)
        if bp is None or bp.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="blog post not found")
        if bp["status"] not in ("done", "failed"):
            raise HTTPException(
                status_code=409,
                detail=f"cannot regenerate from status={bp['status']}",
            )
        # Jobs-queue precondition: refuse if a stale blog_writer job for this
        # bp_id is still queued/running. JSON extraction handles content-spawned
        # payloads that carry seed fields beyond blog_post_id.
        in_flight = conn.execute(
            """SELECT 1 FROM jobs
               WHERE agent = 'blog_writer'
                 AND status IN ('queued', 'running')
                 AND json_valid(payload_json)
                 AND json_extract(payload_json, '$.blog_post_id') = ?""",
            (bp_id,),
        ).fetchone()
        if in_flight is not None:
            raise HTTPException(
                status_code=409, detail="an earlier blog job is still in flight",
            )
        # Cascade the sources table ourselves (CASCADE on the FK would also
        # work, but doing it in-row-reset keeps the SQL explicit + visible).
        # Wrapped in a txn so a crash between the two statements can't leave
        # sources deleted with the row still in its old status.
        with q.txn(conn):
            q.delete_blog_post_sources(conn, bp_id)
            q.reset_blog_post_for_regenerate(conn, bp_id)

    payload = _regenerate_payload(bp)
    enqueue("blog_writer", "draft", payload)
    return {"id": bp_id, "status": "pending"}


@router.post("/{bp_id}/save-as-note", status_code=201)
async def save_blog_post_as_note_endpoint(bp_id: int) -> dict:
    with connect() as conn:
        bp = q.get_blog_post(conn, bp_id)
    if bp is None or bp.get("deleted_at") is not None:
        raise HTTPException(status_code=404, detail="blog post not found")
    if bp.get("saved_as_note_id"):
        with connect() as conn:
            existing = q.get_note(conn, bp["saved_as_note_id"])
        if existing and existing.get("deleted_at") is None:
            return {
                "note_id": existing["id"],
                "slug": existing["slug"],
                "reused": True,
            }
    if bp.get("status") != "done":
        raise HTTPException(status_code=409, detail="blog post not finished")

    # Write a note whose body reflects the draft. Same vault-inbox path as
    # Roundtable's save-as-note — the Notetaker will classify it on its next
    # tick and the escalator may research it later.
    from mastisk.paths import notes_inbox_dir
    from mastisk.routes.notes import atomic_write, derive_slug

    body_md, _err = _read_body(bp)
    preview_body = body_md or bp.get("body_preview") or ""
    ts = datetime.now().astimezone()
    body = _body_from_blog(bp, preview_body)
    slug_source = bp.get("title") or f"blog-{bp_id}"
    slug = derive_slug(slug_source, ts)
    target = notes_inbox_dir() / f"{slug}.md"
    atomic_write(target, body)
    rel_path = str(target.relative_to(vault_dir()))
    with connect() as conn:
        note_id = q.insert_note(
            conn, slug=slug, path=rel_path, body=body,
            source="pwa", created_at=ts,
        )
        row = q.get_note(conn, note_id)
        conn.execute(
            "UPDATE blog_posts SET saved_as_note_id = ? WHERE id = ?",
            (note_id, bp_id),
        )
    actual_path = vault_dir() / row["path"]
    if actual_path != target:
        target.rename(actual_path)
    return {"note_id": note_id, "slug": row["slug"], "reused": False}


@router.delete("/{bp_id}", status_code=204)
async def delete_blog_post_endpoint(bp_id: int) -> None:
    with connect() as conn:
        bp = q.get_blog_post(conn, bp_id)
        if bp is None or bp.get("deleted_at") is not None:
            raise HTTPException(status_code=404, detail="blog post not found")
        q.soft_delete_blog_post(conn, bp_id)
    # Best-effort file unlink. Missing file is fine — the user may have moved
    # it out of the vault manually (iCloud unsubscribe, etc.).
    if bp.get("path"):
        try:
            file_path = vault_dir() / bp["path"]
            file_path.unlink(missing_ok=True)
        except OSError:
            pass


# ───── helpers ─────


def _regenerate_payload(bp: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {"blog_post_id": bp["id"]}
    content_slug = (bp.get("content_slug") or "").strip()
    if not content_slug:
        return payload
    item = content_payload(content_slug)
    if item is None:
        raise HTTPException(
            status_code=409,
            detail="originating content item no longer exists",
        )
    payload["content_slug"] = item["slug"]
    payload["content_source"] = blog_content_source(item)
    return payload


def _summary(row: dict) -> dict:
    """Compact list-view shape — no body_md, no sources."""
    tags = _parse_tags(row.get("tags_json"))
    return {
        "id": row["id"],
        "slug": row.get("slug"),
        "path": row.get("path"),
        "title": row.get("title"),
        "theme": row.get("theme") or "",
        "window_days": row["window_days"],
        "status": row["status"],
        "model": row.get("model"),
        "tags": tags,
        "word_count": row.get("word_count"),
        "body_preview": row.get("body_preview"),
        "created_at": row["created_at"],
        "finished_at": row.get("finished_at"),
        "saved_as_note_id": row.get("saved_as_note_id"),
    }


def _detail(row: dict) -> dict:
    """Detail-view shape — everything on the row, minus body_md (which the
    caller assembles from the vault file)."""
    return _summary(row)


def _parse_tags(tags_json: str | None) -> list[str]:
    if not tags_json:
        return []
    try:
        v = json.loads(tags_json)
        return [str(t) for t in v] if isinstance(v, list) else []
    except json.JSONDecodeError:
        return []


def _read_body(bp: dict) -> tuple[str | None, str | None]:
    """Read the draft's body from the vault file. Returns (body_md, error).

    If the row is pre-done (status in pending/running/failed with no path yet)
    the body is None with no error set — the UI renders the running-state
    placeholder. If the path is set but the file is gone (iCloud shenanigans,
    manual delete), returns (None, "file missing") — row stays browsable.
    """
    path = bp.get("path")
    if not path:
        return None, None
    file_path = vault_dir() / path
    if not file_path.exists():
        return None, "file missing"
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"file unreadable: {e}"
    # Strip the YAML frontmatter to return just body_md. Frontend renders the
    # body only; provenance is available via the sources[] field below.
    return _strip_frontmatter(text), None


def _strip_frontmatter(text: str) -> str:
    """Return the body portion of a markdown file with a YAML frontmatter.

    If the text doesn't start with ``---\n``, returns it unchanged.
    """
    if not text.startswith("---\n"):
        return text
    end = text.find("\n---\n", 4)
    if end == -1:
        return text
    return text[end + len("\n---\n"):].lstrip("\n")


def _resolve_source(conn: Any, row: dict) -> dict:
    """Resolve a blog_post_sources row into the API response shape.

    Populates the ``resolved`` shell the PWA uses to render citation tooltips
    without a second round-trip. Marks ``deleted: true`` if the underlying
    row is tombstoned / missing.
    """
    kind = row["kind"]
    ref = row["ref"]
    resolved: dict[str, Any] = {"deleted": False}
    if kind == "note":
        try:
            note_id = int(ref)
        except (TypeError, ValueError):
            note_id = None
        if note_id is None:
            resolved = {"title": "(invalid note ref)", "deleted": True}
        else:
            n = conn.execute(
                "SELECT slug, summary, classification, deleted_at FROM notes WHERE id = ?",
                (note_id,),
            ).fetchone()
            if n is None or n["deleted_at"] is not None:
                resolved = {"title": "(note deleted)", "deleted": True}
            else:
                resolved = {
                    "title": (n["summary"] or n["classification"] or "note")[:120],
                    "summary": n["summary"],
                    "slug": n["slug"],
                    "deleted": False,
                }
    elif kind == "article":
        a = conn.execute(
            "SELECT slug, title, summary FROM articles WHERE id = ?", (ref,),
        ).fetchone()
        if a is None:
            resolved = {"title": "(article deleted)", "deleted": True}
        else:
            # Pull the article's primary public source URL so the PWA can
            # build copy/share output that links to the original on the
            # open web instead of an internal slug.
            url_row = conn.execute(
                """SELECT s.url FROM sources s
                   JOIN article_sources a ON a.source_id = s.id
                   WHERE a.article_id = ? AND s.url IS NOT NULL
                   ORDER BY s.fetched_at ASC LIMIT 1""",
                (ref,),
            ).fetchone()
            resolved = {
                "title": a["title"],
                "summary": a["summary"],
                "slug": a["slug"],
                "url": url_row["url"] if url_row else None,
                "deleted": False,
            }
    elif kind == "content":
        c = conn.execute(
            """SELECT slug, title, kind, status, path
               FROM content_items
               WHERE slug = ? AND deleted_at IS NULL""",
            (ref,),
        ).fetchone()
        if c is None:
            resolved = {"title": "(content item deleted)", "deleted": True}
        else:
            resolved = {
                "title": c["title"],
                "summary": f"{c['kind']} / {c['status']}",
                "slug": c["slug"],
                "path": c["path"],
                "deleted": False,
            }
    elif kind == "roundtable":
        try:
            rt_id = int(ref)
        except (TypeError, ValueError):
            rt_id = None
        if rt_id is None:
            resolved = {"title": "(invalid roundtable ref)", "deleted": True}
        else:
            rt = conn.execute(
                "SELECT prompt, synthesis FROM roundtables WHERE id = ?", (rt_id,),
            ).fetchone()
            if rt is None:
                resolved = {"title": "(roundtable deleted)", "deleted": True}
            else:
                resolved = {
                    "title": (rt["prompt"] or "roundtable")[:120],
                    "summary": (rt["synthesis"] or "")[:240],
                    "deleted": False,
                }
    return {
        "n": row["rank"],
        "kind": kind,
        "ref": ref,
        "used": bool(row["used"]),
        "origin": row.get("origin"),
        "resolved": resolved,
    }


def _body_from_blog(bp: dict, body_md: str) -> str:
    """Format a blog draft as a note body. Matches Roundtable's style."""
    title = bp.get("title") or f"blog #{bp['id']}"
    preamble = f"> From blog draft #{bp['id']}: {title}\n\n"
    excerpt = (body_md or "")[:500]
    return preamble + excerpt
