"""Phase 8 dashboard intelligence.

All state here is derived/operator state over the markdown mirrors. Activity
counts are intentionally narrow: task status/edit mutations, project log
appends, project-hosted task capture, and journal appends for journal-hosted
tasks already linked to a project.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import yaml

from mastisk.db.queries import connect, txn
from mastisk.paths import vault_dir
from mastisk.routines.sync import local_today
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.dashboard.intelligence")


class FocusFullError(RuntimeError):
    def __init__(self, focus: list[dict[str, Any]]):
        super().__init__("daily focus already has three tasks")
        self.focus = focus


def list_focus(day: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT df.position, df.task_uid AS focus_task_uid, t.*
               FROM daily_focus df
               LEFT JOIN tasks t ON t.uid = df.task_uid AND t.deleted_at IS NULL
               WHERE df.date = ?
               ORDER BY df.position ASC""",
            (day,),
        ).fetchall()
    return [_focus_task_row(dict(row)) for row in rows]


def star_focus(day: str, task_uid: str, *, replace_uid: str | None = None) -> list[dict[str, Any]]:
    with connect() as conn, txn(conn):
        task = conn.execute(
            "SELECT uid FROM tasks WHERE uid = ? AND deleted_at IS NULL",
            (task_uid,),
        ).fetchone()
        if task is None:
            raise KeyError(task_uid)
        if replace_uid:
            replaced = conn.execute(
                """SELECT position FROM daily_focus
                       WHERE date = ? AND task_uid = ?""",
                (day, replace_uid),
            ).fetchone()
            if replaced is None:
                raise ValueError("replace_uid is not focused for this date")
            position = int(replaced["position"])
            conn.execute(
                "DELETE FROM daily_focus WHERE date = ? AND task_uid = ?",
                (day, replace_uid),
            )
            conn.execute(
                """INSERT INTO daily_focus(date, task_uid, position)
                       VALUES (?, ?, ?)
                       ON CONFLICT(date, task_uid) DO UPDATE SET position = excluded.position""",
                (day, task_uid, position),
            )
            _compact_focus_positions(conn, day)
            return _list_focus_with_conn(conn, day)

        already = conn.execute(
            "SELECT 1 FROM daily_focus WHERE date = ? AND task_uid = ?",
            (day, task_uid),
        ).fetchone()
        if already is not None:
            return _list_focus_with_conn(conn, day)

        occupied = {
            int(row["position"])
            for row in conn.execute(
                "SELECT position FROM daily_focus WHERE date = ?",
                (day,),
            ).fetchall()
        }
        if len(occupied) >= 3:
            raise FocusFullError(_list_focus_with_conn(conn, day))
        position = next(pos for pos in (1, 2, 3) if pos not in occupied)
        conn.execute(
            "INSERT INTO daily_focus(date, task_uid, position) VALUES (?, ?, ?)",
            (day, task_uid, position),
        )
        return _list_focus_with_conn(conn, day)


def unstar_focus(day: str, task_uid: str) -> list[dict[str, Any]]:
    with connect() as conn, txn(conn):
        conn.execute(
            "DELETE FROM daily_focus WHERE date = ? AND task_uid = ?",
            (day, task_uid),
        )
        _compact_focus_positions(conn, day)
        return _list_focus_with_conn(conn, day)


def slipping_scan(*, now: datetime | None = None) -> int:
    now_dt = _coerce_datetime(now)
    today = date.fromisoformat(local_today(now_dt))
    computed_at = now_dt.isoformat()
    settings = get_settings().dashboard
    candidates: list[tuple[str, str, str, str]] = []
    with connect() as conn:
        for row in conn.execute(
            """SELECT slug, last_activity_at, staleness_days
               FROM projects
               WHERE deleted_at IS NULL
                 AND status = 'active'
                 AND type IN ('project', 'area')
                 AND COALESCE(slipping_muted, 0) = 0
                 AND (slipping_muted_until IS NULL OR slipping_muted_until < ?)""",
            (today.isoformat(),),
        ).fetchall():
            stale_since = _stale_since(
                row["last_activity_at"], row["staleness_days"], settings.slipping_project_days
            )
            if stale_since is not None and stale_since <= today:
                candidates.append(("project", row["slug"], stale_since.isoformat(), computed_at))
        for row in conn.execute(
            """SELECT uid, last_activity_at, staleness_days
               FROM tasks
               WHERE deleted_at IS NULL
                 AND status = 'open'
                 AND COALESCE(slipping_muted, 0) = 0
                 AND (slipping_muted_until IS NULL OR slipping_muted_until < ?)""",
            (today.isoformat(),),
        ).fetchall():
            stale_since = _stale_since(
                row["last_activity_at"], row["staleness_days"], settings.slipping_task_days
            )
            if stale_since is not None and stale_since <= today:
                candidates.append(("task", row["uid"], stale_since.isoformat(), computed_at))
        with txn(conn):
            conn.execute("DELETE FROM slipping")
            conn.executemany(
                """INSERT INTO slipping(entity_type, entity_id, stale_since, computed_at)
                   VALUES (?, ?, ?, ?)""",
                candidates,
            )
    return len(candidates)


def list_slipping() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT s.entity_type, s.entity_id, s.stale_since, s.computed_at,
                      p.name AS project_name, p.type AS project_type, p.domain AS project_domain,
                      p.slipping_muted AS project_slipping_muted,
                      p.slipping_muted_until AS project_slipping_muted_until,
                      t.text AS task_text, t.due AS task_due, t.project AS task_project, t.domain AS task_domain,
                      t.slipping_muted AS task_slipping_muted,
                      t.slipping_muted_until AS task_slipping_muted_until
               FROM slipping s
               LEFT JOIN projects p ON s.entity_type = 'project' AND p.slug = s.entity_id
               LEFT JOIN tasks t ON s.entity_type = 'task' AND t.uid = s.entity_id
               ORDER BY s.stale_since ASC, s.entity_type, s.entity_id"""
        ).fetchall()
        active_rows = [dict(row) for row in rows]
        active_keys = {(row["entity_type"], row["entity_id"]) for row in active_rows}
        muted_rows = _muted_slipping_rows(conn, active_keys)
    return [
        _slipping_row(row)
        for row in sorted(
            [*active_rows, *muted_rows],
            key=lambda row: (row["stale_since"], row["entity_type"], row["entity_id"]),
        )
    ]


def snooze_slipping(entity_type: str, entity_id: str, *, days: int = 7) -> dict[str, Any] | None:
    until = (date.fromisoformat(local_today()) + timedelta(days=days)).isoformat()
    return _set_slipping_controls(entity_type, entity_id, muted_until=until, muted=None)


def mute_slipping(entity_type: str, entity_id: str) -> dict[str, Any] | None:
    return _set_slipping_controls(
        entity_type,
        entity_id,
        muted_until=None,
        muted=True,
        clear_muted_until=True,
    )


def unmute_slipping(entity_type: str, entity_id: str) -> dict[str, Any] | None:
    return _set_slipping_controls(
        entity_type,
        entity_id,
        muted_until=None,
        muted=False,
        clear_muted_until=True,
    )


def resurface_for_date(day: str) -> dict[str, Any] | None:
    with connect() as conn:
        pool = _resurfacing_pool(conn)
    if not pool:
        return None
    idx = int(hashlib.sha256(day.encode("utf-8")).hexdigest(), 16) % len(pool)
    return pool[idx]


def _resurfacing_pool(conn) -> list[dict[str, Any]]:
    """Pool for daily resurfacing.

    The Phase 8 spec says "favorited quote/note", but favorites still do not
    exist. Until that operator state lands, use deliberate saved quotes plus
    the strongest existing note-value signal: the note was escalated or linked
    into the wiki. Keeping the pool derived preserves deterministic selection.
    """
    quote_rows = conn.execute(
        """SELECT id, text, source_type, source_ref
           FROM quotes
           WHERE deleted_at IS NULL
           ORDER BY id ASC"""
    ).fetchall()
    quote_items = [
        {
            "kind": "quote",
            "id": row["id"],
            "title": _quote_title(row["text"]),
            "excerpt": _excerpt(row["text"]),
            "link": f"/library/quotes/{row['id']}",
        }
        for row in quote_rows
    ]
    note_rows = conn.execute(
        """SELECT n.id, n.summary, n.body
           FROM notes n
           WHERE n.deleted_at IS NULL
             AND (
               n.escalation_state IN ('auto_done', 'manual_done')
               OR EXISTS (SELECT 1 FROM note_links nl WHERE nl.note_id = n.id)
               OR EXISTS (SELECT 1 FROM articles a WHERE a.source_note_id = n.id)
             )
           ORDER BY n.id ASC"""
    ).fetchall()
    note_items = [
        {
            "kind": "note",
            "id": row["id"],
            "title": _note_title(row["summary"], row["body"], row["id"]),
            "excerpt": _excerpt(row["body"]),
            "link": f"/notes/{row['id']}",
        }
        for row in note_rows
    ]
    return [*quote_items, *note_items]


def needs_review_scan(*, today: date | None = None) -> int:
    scan_day = today or date.fromisoformat(local_today())
    surfaced_at = datetime.now(UTC).isoformat()
    settings = get_settings().dashboard
    candidates = _needs_review_candidates(scan_day, settings.triage_reminder_days)
    candidate_keys = {(c.entity_type, c.entity_id, c.reason) for c in candidates}
    with connect() as conn, txn(conn):
        if candidate_keys:
            clauses = " OR ".join(
                "(entity_type = ? AND entity_id = ? AND reason = ?)"
                for _ in candidate_keys
            )
            params = [part for key in candidate_keys for part in key]
            conn.execute(
                f"""DELETE FROM needs_review
                        WHERE NOT ({clauses})""",
                params,
            )
        else:
            conn.execute("DELETE FROM needs_review")
        for candidate in candidates:
            conn.execute(
                """INSERT INTO needs_review(entity_type, entity_id, reason, surfaced_at)
                       VALUES (?, ?, ?, ?)
                       ON CONFLICT(entity_type, entity_id, reason) DO NOTHING""",
                (
                    candidate.entity_type,
                    candidate.entity_id,
                    candidate.reason,
                    surfaced_at,
                ),
            )
    return len(candidates)


def list_needs_review() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM needs_review
               WHERE dismissed_at IS NULL
               ORDER BY surfaced_at DESC, id DESC"""
        ).fetchall()
        return [_needs_review_row(conn, dict(row)) for row in rows]


def count_open_needs_review() -> int:
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM needs_review WHERE dismissed_at IS NULL"
        ).fetchone()
    return int(row["n"] if row else 0)


def dismiss_needs_review(item_id: int) -> dict[str, Any] | None:
    with connect() as conn, txn(conn):
        cur = conn.execute(
            """UPDATE needs_review
                   SET dismissed_at = CURRENT_TIMESTAMP
                   WHERE id = ? AND dismissed_at IS NULL""",
            (item_id,),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM needs_review WHERE id = ?", (item_id,)).fetchone()
        return _needs_review_row(conn, dict(row))


def clear_needs_review(
    entity_type: str,
    entity_id: str,
    *,
    reason: str | None = None,
) -> int:
    clauses = ["entity_type = ?", "entity_id = ?", "dismissed_at IS NULL"]
    params: list[Any] = [entity_type, entity_id]
    if reason is not None:
        clauses.append("reason = ?")
        params.append(reason)
    with connect() as conn:
        cur = conn.execute(
            f"DELETE FROM needs_review WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return cur.rowcount or 0


@dataclass(frozen=True)
class _NeedsReviewCandidate:
    entity_type: str
    entity_id: str
    reason: str


def _needs_review_candidates(scan_day: date, triage_reminder_days: int) -> list[_NeedsReviewCandidate]:
    cutoff = scan_day - timedelta(days=triage_reminder_days)
    found: dict[tuple[str, str, str], _NeedsReviewCandidate] = {}
    with connect() as conn:
        task_rows = conn.execute(
            """SELECT uid, review_at, needs_triage, updated_at
               FROM tasks
               WHERE deleted_at IS NULL"""
        ).fetchall()
        for row in task_rows:
            uid = str(row["uid"])
            review_day = _parse_date(row["review_at"])
            if review_day is not None and review_day <= scan_day:
                _add_candidate(found, "task", uid, "task_review_due")
            if row["needs_triage"] and _date_part(row["updated_at"]) <= cutoff:
                _add_candidate(found, "task", uid, "triage_stale")

        note_rows = conn.execute(
            """SELECT id, path, created_at
               FROM notes
               WHERE deleted_at IS NULL"""
        ).fetchall()
        for row in note_rows:
            meta = _read_note_frontmatter(row["path"])
            review_day = _review_at_from_meta(meta)
            note_id = str(row["id"])
            if review_day is not None and review_day <= scan_day:
                _add_candidate(found, "note", note_id, "note_review_due")
            if meta.get("needs_triage") is True and _date_part(row["created_at"]) <= cutoff:
                _add_candidate(found, "note", note_id, "triage_stale")
    return list(found.values())


def _add_candidate(
    found: dict[tuple[str, str, str], _NeedsReviewCandidate],
    entity_type: str,
    entity_id: str,
    reason: str,
) -> None:
    key = (entity_type, entity_id, reason)
    found[key] = _NeedsReviewCandidate(*key)


def _read_note_frontmatter(rel_path: str) -> dict[str, Any]:
    path = vault_dir() / rel_path
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if not text.startswith("---\n"):
        return {}
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        return {}
    try:
        meta = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError:
        return {}
    return meta if isinstance(meta, dict) else {}


def _review_at_from_meta(meta: dict[str, Any]) -> date | None:
    value = meta.get("review_at")
    capture = meta.get("capture")
    if value is None and isinstance(capture, dict):
        value = capture.get("review_at")
    return _parse_date(value)


def _list_focus_with_conn(conn, day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT df.position, df.task_uid AS focus_task_uid, t.*
           FROM daily_focus df
           LEFT JOIN tasks t ON t.uid = df.task_uid AND t.deleted_at IS NULL
           WHERE df.date = ?
           ORDER BY df.position ASC""",
        (day,),
    ).fetchall()
    return [_focus_task_row(dict(row)) for row in rows]


def _compact_focus_positions(conn, day: str) -> None:
    rows = conn.execute(
        "SELECT task_uid FROM daily_focus WHERE date = ? ORDER BY position ASC",
        (day,),
    ).fetchall()
    for idx, row in enumerate(rows, start=1):
        conn.execute(
            "UPDATE daily_focus SET position = ? WHERE date = ? AND task_uid = ?",
            (idx, day, row["task_uid"]),
        )


def _focus_task_row(row: dict[str, Any]) -> dict[str, Any]:
    position = row.pop("position")
    focus_task_uid = row.pop("focus_task_uid", None)
    if row.get("uid") is None:
        return {
            "position": position,
            "uid": focus_task_uid,
            "text": "Missing task",
            "checked": True,
            "status": "done",
            "deleted_at": None,
            "focused": True,
        }
    task = _task_row(row)
    task["position"] = position
    task["focused"] = True
    return task


def _task_row(row: dict[str, Any]) -> dict[str, Any]:
    row["checked"] = bool(row.get("checked"))
    row["needs_triage"] = bool(row.get("needs_triage"))
    row["no_reminder"] = bool(row.get("no_reminder"))
    row["recurrence_unparsed"] = bool(row.get("recurrence_unparsed"))
    row["slipping_muted"] = bool(row.get("slipping_muted"))
    row["tags"] = json.loads(row.pop("tags_json") or "[]")
    row["links"] = json.loads(row.pop("links_json") or "[]")
    return row


def _stale_since(
    last_activity_at: str | None,
    override_days: int | None,
    default_days: int,
) -> date | None:
    activity = _parse_datetime(last_activity_at)
    if activity is None:
        return None
    days = override_days if override_days is not None else default_days
    if days < 1:
        days = default_days
    return activity.date() + timedelta(days=days)


def _slipping_row(row: dict[str, Any]) -> dict[str, Any]:
    if row["entity_type"] == "project":
        title = row.get("project_name") or row["entity_id"]
        subtitle = row.get("project_type") or "project"
        domain = row.get("project_domain")
        muted = bool(row.get("project_slipping_muted"))
        muted_until = row.get("project_slipping_muted_until")
        link = "/projects"
    else:
        title = row.get("task_text") or row["entity_id"]
        subtitle = row.get("task_due") or "open task"
        domain = row.get("task_domain") or row.get("task_project")
        muted = bool(row.get("task_slipping_muted"))
        muted_until = row.get("task_slipping_muted_until")
        link = "/tasks"
    return {
        "entity_type": row["entity_type"],
        "entity_id": row["entity_id"],
        "title": title,
        "subtitle": subtitle,
        "domain": domain,
        "stale_since": row["stale_since"],
        "computed_at": row["computed_at"],
        "slipping_muted": muted,
        "slipping_muted_until": muted_until,
        "link": link,
    }


def _muted_slipping_rows(
    conn,
    active_keys: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    today = date.fromisoformat(local_today())
    computed_at = datetime.now(UTC).isoformat()
    settings = get_settings().dashboard
    rows: list[dict[str, Any]] = []
    for row in conn.execute(
        """SELECT slug, name, type, domain, last_activity_at, staleness_days,
                  slipping_muted, slipping_muted_until
           FROM projects
           WHERE deleted_at IS NULL
             AND status = 'active'
             AND type IN ('project', 'area')
             AND COALESCE(slipping_muted, 0) = 1"""
    ).fetchall():
        key = ("project", row["slug"])
        if key in active_keys:
            continue
        stale_since = _stale_since(
            row["last_activity_at"], row["staleness_days"], settings.slipping_project_days
        )
        if stale_since is None or stale_since > today:
            continue
        rows.append(
            {
                "entity_type": "project",
                "entity_id": row["slug"],
                "stale_since": stale_since.isoformat(),
                "computed_at": computed_at,
                "project_name": row["name"],
                "project_type": row["type"],
                "project_domain": row["domain"],
                "project_slipping_muted": row["slipping_muted"],
                "project_slipping_muted_until": row["slipping_muted_until"],
            }
        )
    for row in conn.execute(
        """SELECT uid, text, due, project, domain, last_activity_at, staleness_days,
                  slipping_muted, slipping_muted_until
           FROM tasks
           WHERE deleted_at IS NULL
             AND status = 'open'
             AND COALESCE(slipping_muted, 0) = 1"""
    ).fetchall():
        key = ("task", row["uid"])
        if key in active_keys:
            continue
        stale_since = _stale_since(
            row["last_activity_at"], row["staleness_days"], settings.slipping_task_days
        )
        if stale_since is None or stale_since > today:
            continue
        rows.append(
            {
                "entity_type": "task",
                "entity_id": row["uid"],
                "stale_since": stale_since.isoformat(),
                "computed_at": computed_at,
                "task_text": row["text"],
                "task_due": row["due"],
                "task_project": row["project"],
                "task_domain": row["domain"],
                "task_slipping_muted": row["slipping_muted"],
                "task_slipping_muted_until": row["slipping_muted_until"],
            }
        )
    return rows


def _set_slipping_controls(
    entity_type: str,
    entity_id: str,
    *,
    muted_until: str | None,
    muted: bool | None,
    clear_muted_until: bool = False,
) -> dict[str, Any] | None:
    if entity_type not in {"task", "project"}:
        return None
    table = "tasks" if entity_type == "task" else "projects"
    key = "uid" if entity_type == "task" else "slug"
    updates: list[str] = []
    params: list[Any] = []
    if clear_muted_until:
        updates.append("slipping_muted_until = NULL")
    elif muted_until is not None:
        updates.append("slipping_muted_until = ?")
        params.append(muted_until)
    if muted is not None:
        updates.append("slipping_muted = ?")
        params.append(1 if muted else 0)
    if not updates:
        return None
    params.append(entity_id)
    with connect() as conn, txn(conn):
        cur = conn.execute(
            f"UPDATE {table} SET {', '.join(updates)} WHERE {key} = ?",
            tuple(params),
        )
        if cur.rowcount != 1:
            return None
        conn.execute(
            "DELETE FROM slipping WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        )
    return {"entity_type": entity_type, "entity_id": entity_id, "ok": True}


def _needs_review_row(conn, row: dict[str, Any]) -> dict[str, Any]:
    title = row["entity_id"]
    excerpt = ""
    link = "/"
    if row["entity_type"] == "task":
        task = conn.execute("SELECT text, due FROM tasks WHERE uid = ?", (row["entity_id"],)).fetchone()
        if task is not None:
            title = task["text"]
            excerpt = task["due"] or ""
        link = "/inbox-triage" if row["reason"] == "triage_stale" else "/tasks"
    elif row["entity_type"] == "note":
        note = conn.execute(
            "SELECT id, summary, body FROM notes WHERE id = ?",
            (row["entity_id"],),
        ).fetchone()
        if note is not None:
            title = _note_title(note["summary"], note["body"], note["id"])
            excerpt = _excerpt(note["body"])
            link = f"/notes/{note['id']}"
    return {
        **row,
        "title": title,
        "excerpt": excerpt,
        "link": link,
    }


def _note_title(summary: str | None, body: str | None, note_id: int) -> str:
    for value in (summary, body):
        if value and value.strip():
            return _display_title(value.strip().splitlines()[0])
    return f"Note {note_id}"


def _quote_title(text: str | None) -> str:
    if text and text.strip():
        return _display_title(text.strip().splitlines()[0])
    return "Saved quote"


def _excerpt(value: str | None, limit: int = 180) -> str:
    text = " ".join((value or "").split())
    text = re.sub(r"^>\s*From repo:\s*\S+\s*#\s*", "", text, flags=re.IGNORECASE)
    if len(text) <= limit:
        return text
    return f"{text[: limit - 1].rstrip()}..."


def _display_title(value: str, limit: int = 80) -> str:
    if len(value) <= limit:
        return value
    clipped = value[: limit - 1].rstrip()
    boundary = clipped.rfind(" ")
    if boundary >= limit // 2:
        clipped = clipped[:boundary].rstrip()
    return f"{clipped.rstrip(' ,;:-.')}…"


def _coerce_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _date_part(value: str | None) -> date:
    parsed = _parse_date(value)
    return parsed or date.min
