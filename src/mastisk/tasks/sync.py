"""Task mirror scanner and file-first task mutations."""
from __future__ import annotations

import json
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.journal import ensure_day, scan_journal_days
from mastisk.journal import skeleton as journal_skeleton
from mastisk.markdown_sections import append_to_section
from mastisk.paths import journal_dir, projects_dir, vault_dir
from mastisk.projects.sync import get_project, parse_project_file
from mastisk.routes.notes import atomic_write
from mastisk.tasks.parser import (
    ensure_task_uids,
    format_task_line,
    generate_uid,
    parse_markdown_tasks,
    parse_task_line,
    rewrite_task_by_uid,
)


class TaskLineMissingError(RuntimeError):
    """Raised when the mirror row exists but its host line no longer does."""


def scan_tasks() -> dict[str, int]:
    return scan_task_hosts(None)


def scan_task_hosts(
    hosts: list[Path] | None = None,
    *,
    uid_factory: Callable[[], str] | None = None,
) -> dict[str, int]:
    host_paths = hosts if hosts is not None else _full_scan_hosts()
    factory = uid_factory or generate_uid
    seen: set[str] = set()
    scanned_hosts: list[str] = []
    upserted = 0
    assigned = 0
    with connect() as conn:
        for host in host_paths:
            path = _absolute_host_path(host)
            rel_path = str(path.relative_to(vault_dir()))
            scanned_hosts.append(rel_path)
            with host_file_lock(path):
                if not path.exists() or path.name.startswith("."):
                    continue
                markdown = path.read_text(encoding="utf-8")
                markdown_with_uids, new_uids = ensure_task_uids(
                    markdown, uid_factory=factory, existing_uids=seen
                )
                if markdown_with_uids != markdown:
                    atomic_write(path, markdown_with_uids)
                    markdown = markdown_with_uids
                    assigned += len(new_uids)
                project, domain = _project_domain_for_host(path, rel_path)
                for task in parse_markdown_tasks(markdown, host_path=rel_path):
                    uid = task.get("uid")
                    if not uid:
                        continue
                    seen.add(uid)
                    conn.execute(
                        """INSERT INTO tasks
                           (uid, host_path, line_number, text, checked, status, due, scheduled,
                            priority, domain, project, recurrence, tags_json, links_json, needs_triage,
                            last_activity_at, reminder_lead_minutes, no_reminder, review_at, deleted_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                                   COALESCE((SELECT last_activity_at FROM tasks WHERE uid = ?), CURRENT_TIMESTAMP),
                                   COALESCE((SELECT reminder_lead_minutes FROM tasks WHERE uid = ?), NULL),
                                   COALESCE((SELECT no_reminder FROM tasks WHERE uid = ?), 0),
                                   COALESCE((SELECT review_at FROM tasks WHERE uid = ?), NULL),
                                   NULL)
                           ON CONFLICT(uid) DO UPDATE SET
                             host_path=excluded.host_path,
                             line_number=excluded.line_number,
                             text=excluded.text,
                             checked=excluded.checked,
                             status=excluded.status,
                             due=excluded.due,
                             scheduled=excluded.scheduled,
                             priority=excluded.priority,
                             domain=excluded.domain,
                             project=excluded.project,
                             recurrence=excluded.recurrence,
                             tags_json=excluded.tags_json,
                             links_json=excluded.links_json,
                             needs_triage=excluded.needs_triage,
                             last_activity_at=CASE
                               WHEN tasks.status IS NOT excluded.status THEN CURRENT_TIMESTAMP
                               ELSE COALESCE(tasks.last_activity_at, CURRENT_TIMESTAMP)
                             END,
                             deleted_at=NULL,
                             updated_at=CURRENT_TIMESTAMP""",
                        (
                            uid,
                            rel_path,
                            task["line_number"],
                            task["text"],
                            1 if task["checked"] else 0,
                            task["status"],
                            task.get("due"),
                            task.get("scheduled"),
                            task.get("priority"),
                            domain,
                            project,
                            task.get("recurrence"),
                            json.dumps(task.get("tags", [])),
                            json.dumps(task.get("links", [])),
                            1 if task.get("needs_triage") else 0,
                            uid,
                            uid,
                            uid,
                            uid,
                        ),
                    )
                    _reconcile_task_due_reminders(uid)
                    upserted += 1
        _soft_delete_disappeared(conn, scanned_hosts, seen)
    return {"upserted": upserted, "assigned": assigned}


def append_task_to_host(
    host: Path,
    *,
    text: str,
    due: str | None = None,
    scheduled: str | None = None,
    recurrence: str | None = None,
    priority: str | None = None,
    tags: list[str] | None = None,
    links: list[str] | None = None,
    uid: str | None = None,
    reminder_lead_minutes: int | None = None,
    no_reminder: bool | None = None,
    review_at: str | None = None,
) -> dict[str, Any]:
    path = _absolute_host_path(host)
    task_uid = uid or generate_uid()
    line = format_task_line(
        text,
        due=due,
        scheduled=scheduled,
        recurrence=recurrence,
        priority=priority,
        tags=tags,
        links=links,
        uid=task_uid,
    )
    parsed_line = parse_task_line(line)
    if parsed_line is None or parsed_line.get("uid") != task_uid:
        raise RuntimeError("formatted task line did not round-trip generated uid")
    if _is_journal_day_path(path):
        ensure_day(path.stem)
    with host_file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            atomic_write(
                path,
                journal_skeleton() if _is_journal_day_path(path) else "## Tasks\n\n## Log\n",
            )
        markdown = path.read_text(encoding="utf-8")
        atomic_write(path, append_to_section(markdown, "Tasks", line))
    scan_task_hosts([path])
    if _is_journal_day_path(path):
        scan_journal_days([path])
    _persist_capture_event_facts(
        task_uid,
        reminder_lead_minutes=reminder_lead_minutes,
        no_reminder=no_reminder,
        review_at=review_at,
    )
    row = get_task(task_uid)
    if row is None:
        raise RuntimeError(f"task mirror missing after write: {task_uid}")
    if row.get("project"):
        _bump_project_activity(row["project"])
    return row


def rewrite_task(uid: str, **updates: Any) -> dict[str, Any] | None:
    task = get_task(uid)
    if task is None:
        return None
    path = vault_dir() / task["host_path"]
    try:
        with host_file_lock(path):
            markdown = path.read_text(encoding="utf-8")
            atomic_write(path, rewrite_task_by_uid(markdown, uid, **updates))
    except (FileNotFoundError, KeyError) as exc:
        scan_task_hosts([path])
        raise TaskLineMissingError(f"task line not found: {uid}") from exc
    scan_task_hosts([path])
    # Dashboard activity is user mutation only: task toggle/edit, project log
    # append, project-hosted task capture, and journal logs for journal-hosted
    # tasks already linked to a project. Passive file scans preserve history.
    _bump_task_activity(uid)
    updated_task = get_task(uid)
    if updated_task and updated_task.get("project"):
        _bump_project_activity(updated_task["project"])
    if updates.get("checked") is True:
        from mastisk.tasks.recurrence import materialize_next_instance

        materialize_next_instance(uid)
    return get_task(uid)


def get_task(uid: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE uid = ? AND deleted_at IS NULL",
            (uid,),
        ).fetchone()
    return _task_row(dict(row)) if row else None


def list_tasks(
    *,
    status: str | None = None,
    due_before: str | None = None,
    domain: str | None = None,
    project: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if due_before:
        clauses.append("due IS NOT NULL AND due < ?")
        params.append(due_before)
    if domain:
        clauses.append("domain = ?")
        params.append(domain)
    if project:
        clauses.append("project = ?")
        params.append(project)
    sql = (
        "SELECT * FROM tasks WHERE "
        + " AND ".join(clauses)
        + " ORDER BY due IS NULL, due ASC, updated_at DESC"
    )
    with connect() as conn:
        rows = conn.execute(sql, tuple(params)).fetchall()
    return [_task_row(dict(r)) for r in rows]


def journal_host_for_today(ts: str | None = None) -> Path:
    if ts:
        try:
            day = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
        except ValueError:
            day = date.today()
    else:
        day = date.today()
    return ensure_day(day)


def _task_row(row: dict[str, Any]) -> dict[str, Any]:
    row["checked"] = bool(row.get("checked"))
    row["needs_triage"] = bool(row.get("needs_triage"))
    row["no_reminder"] = bool(row.get("no_reminder"))
    row["recurrence_unparsed"] = bool(row.get("recurrence_unparsed"))
    row["tags"] = json.loads(row.pop("tags_json") or "[]")
    row["links"] = json.loads(row.pop("links_json") or "[]")
    return row


def _persist_capture_event_facts(
    uid: str,
    *,
    reminder_lead_minutes: int | None,
    no_reminder: bool | None,
    review_at: str | None,
) -> None:
    updates: list[str] = []
    params: list[Any] = []
    if reminder_lead_minutes is not None:
        updates.append("reminder_lead_minutes = ?")
        params.append(reminder_lead_minutes)
    if no_reminder is not None:
        updates.append("no_reminder = ?")
        params.append(1 if no_reminder else 0)
    if review_at is not None:
        updates.append("review_at = ?")
        params.append(review_at)
    if not updates:
        return
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(uid)
    with connect() as conn:
        conn.execute(
            f"UPDATE tasks SET {', '.join(updates)} WHERE uid = ?",
            tuple(params),
        )


def _default_task_hosts() -> list[Path]:
    paths: list[Path] = []
    if journal_dir().exists():
        paths.extend(sorted(p for p in journal_dir().glob("*.md") if p.is_file()))
    if projects_dir().exists():
        paths.extend(sorted(p for p in projects_dir().glob("*.md") if p.is_file()))
    return paths


def _full_scan_hosts() -> list[Path]:
    paths: list[Path] = []
    seen: set[Path] = set()
    for path in [*_default_task_hosts(), *_mirror_task_hosts()]:
        absolute = _absolute_host_path(path)
        if absolute in seen:
            continue
        seen.add(absolute)
        paths.append(absolute)
    return paths


def _mirror_task_hosts() -> list[Path]:
    with connect() as conn:
        rows = conn.execute("SELECT DISTINCT host_path FROM tasks").fetchall()
    return [vault_dir() / row["host_path"] for row in rows]


def _absolute_host_path(path: Path) -> Path:
    return path if path.is_absolute() else vault_dir() / path


def _is_journal_day_path(path: Path) -> bool:
    try:
        rel = path.relative_to(journal_dir())
    except ValueError:
        return False
    if rel.parent != Path(".") or rel.suffix != ".md":
        return False
    try:
        date.fromisoformat(rel.stem)
    except ValueError:
        return False
    return True


def _project_domain_for_host(path: Path, rel_path: str) -> tuple[str | None, str | None]:
    if not rel_path.startswith("projects/") or not rel_path.endswith(".md"):
        return None, None
    slug = Path(rel_path).stem
    project = get_project(slug)
    if project is None:
        try:
            project = parse_project_file(path)
        except Exception:
            project = None
    return slug, project.get("domain") if project else None


def _soft_delete_disappeared(conn, scanned_hosts: list[str], seen: set[str]) -> None:
    if not scanned_hosts:
        return
    host_placeholders = ",".join("?" for _ in scanned_hosts)
    params: list[Any] = list(scanned_hosts)
    sql = f"""UPDATE tasks SET deleted_at = CURRENT_TIMESTAMP
              WHERE deleted_at IS NULL AND host_path IN ({host_placeholders})"""
    if seen:
        uid_placeholders = ",".join("?" for _ in seen)
        sql += f" AND uid NOT IN ({uid_placeholders})"
        params.extend(seen)
    conn.execute(sql, tuple(params))


def _reconcile_task_due_reminders(uid: str) -> None:
    from mastisk.agents.reminder_engine import reconcile_task_due_reminders

    reconcile_task_due_reminders(uid)


def _bump_task_activity(uid: str) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE tasks
               SET last_activity_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE uid = ?""",
            (uid,),
        )


def _bump_project_activity(slug: str) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE projects
               SET last_activity_at = CURRENT_TIMESTAMP,
                   updated_at = CURRENT_TIMESTAMP
               WHERE slug = ? AND deleted_at IS NULL""",
            (slug,),
        )
