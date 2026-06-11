"""Small deterministic recurrence parser and materializer.

Supported grammar:
- ``every day`` / ``daily``
- ``weekly``
- ``every <weekday>``
- ``every N days|weeks|months``
- ``monthly``
- ``every month on the <N>th``

Monthly dates clamp to the last day of the target month, so Jan 31 + monthly
materializes at Feb 28/29.
"""
from __future__ import annotations

import calendar
import json
import re
from datetime import date, timedelta

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.markdown_sections import append_to_section
from mastisk.paths import vault_dir
from mastisk.routes.notes import atomic_write
from mastisk.routines.sync import local_today
from mastisk.tasks.parser import format_task_line, generate_uid

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def next_due_date(rule: str | None, base_date: str) -> str | None:
    if not rule:
        return None
    base = date.fromisoformat(base_date[:10])
    normalized = _normalize_rule(rule)
    if normalized in {"daily", "every day"}:
        return (base + timedelta(days=1)).isoformat()
    if normalized in {"weekly", "every week"}:
        return (base + timedelta(weeks=1)).isoformat()
    if normalized in {"monthly", "every month"}:
        return _add_months(base, 1).isoformat()

    month_on = re.fullmatch(r"every month on the (\d{1,2})(?:st|nd|rd|th)?", normalized)
    if month_on:
        day = int(month_on.group(1))
        if not 1 <= day <= 31:
            return None
        candidate = _date_in_month(base.year, base.month, day)
        if candidate <= base:
            next_month = _add_months(date(base.year, base.month, 1), 1)
            candidate = _date_in_month(next_month.year, next_month.month, day)
        return candidate.isoformat()

    every_n = re.fullmatch(r"every (\d+) (days|weeks|months)", normalized)
    if every_n:
        amount = int(every_n.group(1))
        if amount <= 0:
            return None
        unit = every_n.group(2)
        if unit == "days":
            return (base + timedelta(days=amount)).isoformat()
        if unit == "weeks":
            return (base + timedelta(weeks=amount)).isoformat()
        return _add_months(base, amount).isoformat()

    weekday = re.fullmatch(r"every (monday|tuesday|wednesday|thursday|friday|saturday|sunday)", normalized)
    if weekday:
        target = _WEEKDAYS[weekday.group(1)]
        delta = (target - base.weekday()) % 7
        if delta == 0:
            delta = 7
        return (base + timedelta(days=delta)).isoformat()
    return None


def recurrence_tick(*, now=None) -> int:
    """Safety net for completions made directly in Obsidian."""
    from mastisk.tasks.sync import scan_tasks

    scan_tasks()
    completed_on = local_today(now)
    with connect() as conn:
        rows = conn.execute(
            """SELECT uid FROM tasks
               WHERE status = 'done'
                 AND recurrence IS NOT NULL
                 AND recurrence != ''
                 AND deleted_at IS NULL
               ORDER BY updated_at ASC"""
        ).fetchall()
    count = 0
    for row in rows:
        if materialize_next_instance(row["uid"], completed_on=completed_on):
            count += 1
    return count


def materialize_next_instance(uid: str, *, completed_on: str | None = None) -> bool:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM tasks WHERE uid = ? AND deleted_at IS NULL",
            (uid,),
        ).fetchone()
    if row is None:
        return False
    task = dict(row)
    if task.get("status") != "done" or not task.get("recurrence"):
        return False

    base_date = (task.get("due") or completed_on or local_today())[:10]
    rule = str(task["recurrence"])
    key = f"{uid}:{base_date}:{rule}"
    if task.get("recurrence_materialized_key") == key:
        return False

    next_day = next_due_date(rule, base_date)
    if next_day is None:
        _set_recurrence_flags(uid, materialized_key=None, unparsed=True)
        return False

    host = vault_dir() / task["host_path"]
    has_due = bool(task.get("due"))
    has_scheduled = bool(task.get("scheduled"))
    if has_due:
        next_due = _next_due_with_time(task.get("due"), next_day)
    elif has_scheduled:
        next_due = None
    else:
        next_due = next_day
    next_scheduled = next_day if has_scheduled else None
    line = format_task_line(
        task["text"],
        due=next_due,
        scheduled=next_scheduled,
        recurrence=rule,
        priority=task.get("priority"),
        tags=json.loads(task.get("tags_json") or "[]"),
        links=json.loads(task.get("links_json") or "[]"),
        uid=generate_uid(),
    )
    with host_file_lock(host):
        markdown = host.read_text(encoding="utf-8")
        atomic_write(host, append_to_section(markdown, "Tasks", line))

    from mastisk.tasks.sync import scan_task_hosts

    scan_task_hosts([host])
    _set_recurrence_flags(uid, materialized_key=key, unparsed=False)
    return True


def _set_recurrence_flags(
    uid: str,
    *,
    materialized_key: str | None,
    unparsed: bool,
) -> None:
    with connect() as conn:
        if materialized_key is None:
            conn.execute(
                """UPDATE tasks
                   SET recurrence_unparsed = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE uid = ?""",
                (1 if unparsed else 0, uid),
            )
        else:
            conn.execute(
                """UPDATE tasks
                   SET recurrence_materialized_key = ?,
                       recurrence_unparsed = ?,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE uid = ?""",
                (materialized_key, 1 if unparsed else 0, uid),
            )


def _normalize_rule(rule: str) -> str:
    normalized = re.sub(r"\s+", " ", rule.strip().lower())
    return normalized.rstrip(".")


def _add_months(day: date, months: int) -> date:
    month_index = day.month - 1 + months
    year = day.year + month_index // 12
    month = month_index % 12 + 1
    return _date_in_month(year, month, day.day)


def _date_in_month(year: int, month: int, desired_day: int) -> date:
    last_day = calendar.monthrange(year, month)[1]
    return date(year, month, min(desired_day, last_day))


def _next_due_with_time(previous_due: str | None, next_day: str) -> str:
    if previous_due and "T" in previous_due:
        return f"{next_day}T{previous_due.split('T', 1)[1]}"
    return next_day
