"""Monthly retainer task materialization."""
from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from mastisk.file_locks import host_file_lock
from mastisk.markdown_sections import append_to_section
from mastisk.paths import vault_dir
from mastisk.projects.sync import (
    dump_project_file,
    list_projects,
    parse_project_file,
    scan_projects,
    split_frontmatter,
)
from mastisk.routes.notes import atomic_write
from mastisk.settings import get_settings
from mastisk.tasks.parser import (
    format_task_line,
    generate_uid,
    parse_task_line,
    rewrite_task_line,
)
from mastisk.tasks.sync import scan_task_hosts

log = logging.getLogger("mastisk.retainer_rollover")


def retainer_rollover(
    *,
    now: datetime | None = None,
    uid_factory: Callable[[], str] | None = None,
) -> int:
    """Materialize active retainers once per local month.

    Idempotency is file-canonical: each retainer records processed months in
    frontmatter as ``rolled_months: [YYYY-MM]``. The scheduler runs this daily;
    if the Mac sleeps through the 1st, the first later tick in that month
    catches up instead of silently skipping the month.
    """
    local_now = _local_now(now)
    month_start = local_now.date().replace(day=1)
    month = month_start.strftime("%Y-%m")
    month_end = _month_end(month_start).isoformat()
    factory = uid_factory or generate_uid

    scan_projects()
    processed = 0
    for project in list_projects():
        if project.get("type") != "retainer" or project.get("status") != "active":
            continue
        path = vault_dir() / project["path"]
        with host_file_lock(path):
            parsed = parse_project_file(path)
            if parsed["type"] != "retainer" or parsed["status"] != "active":
                continue
            meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
            rolled_months = _string_list(meta.get("rolled_months"))
            if month in rolled_months:
                continue
            recurring_items = _string_list(meta.get("recurring_items"))
            body = _carry_forward_overdue_tasks(body, month_start, month_end)
            if recurring_items:
                body = _append_month_tasks(body, month, month_end, recurring_items, factory)
            meta["rolled_months"] = [*rolled_months, month]
            atomic_write(path, dump_project_file(meta, body))
        scan_projects([path])
        scan_task_hosts([path])
        processed += 1
    return processed


def _append_month_tasks(
    body: str,
    month: str,
    month_end: str,
    recurring_items: list[str],
    uid_factory: Callable[[], str],
) -> str:
    updated = append_to_section(body, "Tasks", f"### {month}")
    for item in recurring_items:
        updated = append_to_section(
            updated,
            "Tasks",
            format_task_line(item, due=month_end, uid=uid_factory()),
        )
    return updated


def _carry_forward_overdue_tasks(body: str, month_start: date, month_end: str) -> str:
    # Carry-forward semantics: open task lines in this retainer file whose due
    # date is before the first day of the materialized month are re-dated to the
    # new month end. Done tasks and undated tasks stay unchanged.
    lines = body.splitlines(keepends=True)
    rewritten: list[str] = []
    in_milestones = False
    for raw in lines:
        line, ending = _split_line_ending(raw)
        heading = line.strip().lower()
        if heading.startswith("## "):
            in_milestones = heading == "## milestones"
        parsed = None if in_milestones else parse_task_line(line)
        due_date = _safe_task_due_date(parsed, line) if parsed is not None else None
        if (
            parsed is not None
            and not parsed["checked"]
            and due_date is not None
            and due_date < month_start
        ):
            rewritten.append(
                rewrite_task_line(line, due=month_end, uid=parsed.get("uid")) + ending
            )
        else:
            rewritten.append(raw)
    return "".join(rewritten)


def _safe_task_due_date(parsed: dict | None, line: str) -> date | None:
    if parsed is None or not parsed.get("due"):
        return None
    raw = str(parsed["due"])[:10]
    try:
        return date.fromisoformat(raw)
    except ValueError:
        log.warning("retainer_rollover: skipping task with malformed due date %r: %s", raw, line)
        return None


def _local_now(now: datetime | None) -> datetime:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    return current.astimezone(tz)


def _month_end(month_start: date) -> date:
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    return next_month - timedelta(days=1)


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _split_line_ending(raw: str) -> tuple[str, str]:
    if raw.endswith("\r\n"):
        return raw[:-2], "\r\n"
    if raw.endswith("\n"):
        return raw[:-1], "\n"
    if raw.endswith("\r"):
        return raw[:-1], "\r"
    return raw, ""
