"""Topic-suggester API — surface daily blog-topic seeds for the user.

The TopicSuggester agent writes ``topic_suggestions`` rows on a daily cadence;
this router exposes them to the PWA. Two endpoints:

- ``GET /api/topic-suggestions`` returns active rows (not dismissed, not yet
  used to draft a post), newest first, with each source ref resolved to a
  short label so the UI can render a tooltip without a second round-trip.
- ``POST /api/topic-suggestions/{id}/dismiss`` marks a row dismissed.

A note on linking: when a blog draft is created with a theme that exactly
matches an active suggestion's title, ``blog_route.create_blog_post_endpoint``
sets the suggestion's ``used_blog_id`` so it disappears from the active list.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from mastisk.db.queries import connect

log = logging.getLogger("mastisk.topic_suggester_route")

router = APIRouter(prefix="/api/topic-suggestions", tags=["topic-suggestions"])

_MAX_LIST = 5


@router.get("")
async def list_topic_suggestions_endpoint() -> dict:
    """Active suggestions, newest first. Capped at 5 rows.

    'Active' = ``dismissed_at IS NULL AND used_blog_id IS NULL``. Used rows
    leave the active list when the user drafts from one (the link happens in
    blog_route's POST handler), and dismissed rows leave when the user clicks
    the dismiss action.
    """
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, kind, title, hook, angle, source_refs_json,
                      created_at
               FROM topic_suggestions
               WHERE dismissed_at IS NULL AND used_blog_id IS NULL
               ORDER BY created_at DESC, id DESC
               LIMIT ?""",
            (_MAX_LIST,),
        ).fetchall()
        suggestions = [_row_to_response(conn, dict(r)) for r in rows]
    return {"suggestions": suggestions}


@router.post("/{sid}/dismiss", status_code=204)
async def dismiss_topic_suggestion_endpoint(sid: int) -> None:
    """Mark a suggestion dismissed. Idempotent — already-dismissed is a no-op."""
    with connect() as conn:
        row = conn.execute(
            "SELECT id, dismissed_at FROM topic_suggestions WHERE id = ?",
            (sid,),
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="topic suggestion not found")
        if row["dismissed_at"] is not None:
            return None
        conn.execute(
            """UPDATE topic_suggestions SET dismissed_at = CURRENT_TIMESTAMP
               WHERE id = ? AND dismissed_at IS NULL""",
            (sid,),
        )
    return None


# ───── helpers ─────


def _row_to_response(conn: Any, row: dict) -> dict:
    """Transform a DB row into the API shape, resolving source refs to labels."""
    refs = _parse_source_refs(row.get("source_refs_json"))
    resolved = [_resolve_ref(conn, r) for r in refs]
    return {
        "id": row["id"],
        "kind": row["kind"],
        "title": row["title"],
        "hook": row["hook"],
        "angle": row.get("angle"),
        "source_refs": resolved,
        "created_at": row["created_at"],
    }


def _parse_source_refs(raw: str | None) -> list[dict]:
    """Tolerantly parse the source_refs_json column. Bad rows return [].

    Per spec: list of {kind: str, ref: int|str}. We don't enforce shape past
    the dict check — ``_resolve_ref`` will downgrade bad entries to deleted
    placeholders rather than 500ing the request.
    """
    if not raw:
        return []
    try:
        v = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("topic_suggester_route: bad source_refs_json: %r", raw)
        return []
    if not isinstance(v, list):
        return []
    out: list[dict] = []
    for entry in v:
        if isinstance(entry, dict):
            out.append(entry)
    return out


def _resolve_ref(conn: Any, ref: dict) -> dict:
    """Return ``{kind, ref, label}`` — the lightweight label is the article
    title (for kind='article'), or the note's first line of body / summary.

    On any lookup failure we mark the ref ``deleted: true`` so the UI can
    grey it out. The original kind/ref are preserved either way."""
    kind = ref.get("kind")
    raw_ref = ref.get("ref")
    out: dict[str, Any] = {"kind": kind, "ref": raw_ref}
    if kind == "note":
        try:
            note_id = int(raw_ref) if raw_ref is not None else None
        except (TypeError, ValueError):
            note_id = None
        if note_id is None:
            out["label"] = "(invalid note ref)"
            out["deleted"] = True
            return out
        n = conn.execute(
            "SELECT summary, body, deleted_at FROM notes WHERE id = ?",
            (note_id,),
        ).fetchone()
        if n is None or n["deleted_at"] is not None:
            out["label"] = "(note deleted)"
            out["deleted"] = True
            return out
        label = (n["summary"] or "").strip()
        if not label:
            # First non-empty line of body, capped.
            for line in (n["body"] or "").splitlines():
                line = line.strip()
                if line:
                    label = line
                    break
        out["label"] = (label or f"note #{note_id}")[:120]
        out["deleted"] = False
    elif kind == "article":
        a = conn.execute(
            "SELECT title FROM articles WHERE id = ?", (raw_ref,),
        ).fetchone()
        if a is None:
            out["label"] = "(article deleted)"
            out["deleted"] = True
            return out
        out["label"] = (a["title"] or f"article {raw_ref}")[:120]
        out["deleted"] = False
    elif kind == "roundtable":
        try:
            rt_id = int(raw_ref) if raw_ref is not None else None
        except (TypeError, ValueError):
            rt_id = None
        if rt_id is None:
            out["label"] = "(invalid roundtable ref)"
            out["deleted"] = True
            return out
        rt = conn.execute(
            "SELECT prompt FROM roundtables WHERE id = ?", (rt_id,),
        ).fetchone()
        if rt is None:
            out["label"] = "(roundtable deleted)"
            out["deleted"] = True
            return out
        out["label"] = ((rt["prompt"] or "")[:120]) or f"roundtable #{rt_id}"
        out["deleted"] = False
    else:
        out["label"] = f"({kind or 'unknown'} ref)"
        out["deleted"] = True
    return out
