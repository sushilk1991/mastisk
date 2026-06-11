"""Durable reminder queue and daily summary engine."""
from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from mastisk import notify
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.reminder_engine")
FIRING_STALE_AFTER = timedelta(minutes=10)
_logged_invalid_daily_summary_configs: set[tuple[str, str]] = set()


def reminder_tick(*, now: datetime | None = None, ensure_daily_summary: bool = True) -> int:
    """Fire due pending reminders.

    Claiming is a guarded status transition. If two ticks see the same pending
    row, only one can move it to ``firing`` and call the notifier.
    """
    now_dt = _coerce_datetime(now)
    _reclaim_stale_firing_reminders(now_dt)
    if _notify_backend_disabled():
        log.info("reminder_tick skipped: notify backend disabled")
        return 0
    if ensure_daily_summary:
        _ensure_daily_summary_reminder(now_dt)
    now_iso = _utc_iso(now_dt)
    with connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM reminders
                   WHERE status = 'pending'
                     AND deleted_at IS NULL
                     AND fire_at <= ?
                     AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
                   ORDER BY fire_at ASC, id ASC""",
                (now_iso, now_iso),
            )
        ]

    fired = 0
    for row in rows:
        if not _claim_reminder(row["id"], now_iso):
            continue
        try:
            _fire_claimed(row, now_dt)
        except Exception as exc:
            log.exception("reminder %s failed unexpectedly after claim", row["id"])
            _restore_claimed_reminder(row["id"], exc)
            continue
        fired += 1
    return fired


def reclaim_firing_reminders() -> int:
    """Return orphaned claims to pending after a process crash.

    A crash between ``pending -> firing`` and the final sent/failed write is the
    one case the guarded claim cannot resolve by itself.
    """
    with connect() as conn:
        cur = conn.execute(
            """UPDATE reminders
               SET status = 'pending',
                   fired_at = NULL
               WHERE status = 'firing' AND deleted_at IS NULL"""
        )
        return cur.rowcount


def create_task_due_reminder(
    *,
    task_uid: str,
    task_text: str,
    due: str | None,
    lead_minutes: int | None,
    no_reminder: bool,
    now: datetime | None = None,
) -> int | None:
    if no_reminder or due is None or lead_minutes is None:
        return None
    try:
        due_dt = _parse_datetime(due)
    except ValueError:
        log.warning("task %s has unparsable due datetime for reminder: %r", task_uid, due)
        return None
    fire_at = due_dt - timedelta(minutes=lead_minutes)
    if fire_at <= _coerce_datetime(now):
        return None
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO reminders
                 (entity_type, entity_id, fire_at, lead_minutes, kind, status, title, body)
               VALUES ('task', ?, ?, ?, 'task_due', 'pending', 'Task due', ?)""",
            (task_uid, _utc_iso(fire_at), lead_minutes, task_text),
        )
        return int(cur.lastrowid)


def reconcile_task_due_reminders(task_uid: str) -> int:
    """Refresh pending task_due reminders from the current task mirror row."""
    with connect() as conn:
        rows = [
            dict(row)
            for row in conn.execute(
                """SELECT * FROM reminders
                   WHERE entity_type = 'task'
                     AND entity_id = ?
                     AND kind = 'task_due'
                     AND status = 'pending'
                     AND deleted_at IS NULL""",
                (task_uid,),
            )
        ]
    changed = 0
    for row in rows:
        if _sync_pending_task_due_reminder(row):
            changed += 1
    return changed


def create_custom_reminder(
    *,
    fire_at: str,
    title: str,
    body: str,
    entity_type: str | None = None,
    entity_id: str | None = None,
    url: str | None = None,
) -> dict:
    fire_dt = _parse_datetime(fire_at)
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO reminders
                 (entity_type, entity_id, fire_at, kind, status, title, body, url)
               VALUES (?, ?, ?, 'custom', 'pending', ?, ?, ?)""",
            (entity_type, entity_id, _utc_iso(fire_dt), title, body, url),
        )
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _reminder_row(dict(row))


def list_reminders(*, status: str | None = None) -> list[dict]:
    clauses = ["deleted_at IS NULL"]
    params: list[str] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM reminders WHERE "
            + " AND ".join(clauses)
            + " ORDER BY fire_at ASC, id ASC",
            tuple(params),
        ).fetchall()
    return [_reminder_row(dict(row)) for row in rows]


def get_reminder(reminder_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM reminders WHERE id = ? AND deleted_at IS NULL",
            (reminder_id,),
        ).fetchone()
    return _reminder_row(dict(row)) if row else None


def cancel_reminder(reminder_id: int) -> dict | None:
    with connect() as conn:
        cur = conn.execute(
            """UPDATE reminders
               SET status = 'cancelled'
               WHERE id = ? AND status = 'pending' AND deleted_at IS NULL""",
            (reminder_id,),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    return _reminder_row(dict(row))


def retry_reminder(reminder_id: int) -> dict | None:
    with connect() as conn:
        cur = conn.execute(
            """UPDATE reminders
               SET status = 'pending',
                   attempts = 0,
                   next_attempt_at = NULL,
                   last_error = NULL,
                   fired_at = NULL
               WHERE id = ? AND status = 'notify_failed' AND deleted_at IS NULL""",
            (reminder_id,),
        )
        if cur.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM reminders WHERE id = ?", (reminder_id,)).fetchone()
    return _reminder_row(dict(row))


def compose_daily_summary(*, today: date, open_tasks: list[dict]) -> tuple[str, str]:
    due_today: list[dict] = []
    overdue: list[dict] = []
    for task in open_tasks:
        due_date = _task_due_date(task.get("due"))
        if due_date is None:
            continue
        if due_date < today:
            overdue.append(task)
        elif due_date == today:
            due_today.append(task)

    due_today.sort(key=lambda task: task.get("due") or "")
    overdue.sort(key=lambda task: task.get("due") or "")

    title = "Mastisk daily summary"
    lines = [f"{len(due_today)} due today, {len(overdue)} overdue."]
    if due_today:
        lines.extend(["", f"Due today ({len(due_today)})"])
        lines.extend(f"- {task['text']}" for task in due_today)
    if overdue:
        lines.extend(["", f"Overdue ({len(overdue)})"])
        lines.extend(f"- {task['text']}" for task in overdue)
    if not due_today and not overdue:
        lines.extend(["", "No open tasks due today or overdue."])
    return title, "\n".join(lines)


def daily_summary_tick(*, now: datetime | None = None) -> dict | None:
    if _notify_backend_disabled():
        log.info("daily_summary skipped: notify backend disabled")
        return None
    now_dt = _coerce_datetime(now)
    row = _ensure_daily_summary_reminder(now_dt)
    if row is None:
        return None
    reminder_tick(now=now_dt, ensure_daily_summary=False)
    return get_reminder(row["id"])


def _claim_reminder(reminder_id: int, now_iso: str) -> bool:
    with connect() as conn:
        cur = conn.execute(
            """UPDATE reminders
               SET status = 'firing',
                   fired_at = ?,
                   last_error = NULL
               WHERE id = ?
                 AND status = 'pending'
                 AND deleted_at IS NULL
                 AND fire_at <= ?
                 AND (next_attempt_at IS NULL OR next_attempt_at <= ?)""",
            (now_iso, reminder_id, now_iso, now_iso),
        )
        return cur.rowcount == 1


def _reclaim_stale_firing_reminders(now_dt: datetime) -> int:
    cutoff = _utc_iso(now_dt - FIRING_STALE_AFTER)
    with connect() as conn:
        cur = conn.execute(
            """UPDATE reminders
               SET status = 'pending',
                   fired_at = NULL,
                   last_error = 'reclaimed stale firing claim'
               WHERE status = 'firing'
                 AND deleted_at IS NULL
                 AND fired_at IS NOT NULL
                 AND fired_at <= ?""",
            (cutoff,),
        )
        return cur.rowcount


def _restore_claimed_reminder(reminder_id: int, exc: Exception) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE reminders
               SET status = 'pending',
                   fired_at = NULL,
                   last_error = ?
               WHERE id = ? AND status = 'firing' AND deleted_at IS NULL""",
            (f"unexpected error while firing: {exc}", reminder_id),
        )


def _fire_claimed(row: dict, now_dt: datetime) -> None:
    refreshed = _refresh_claimed_task_due_reminder(row, now_dt)
    if refreshed is None:
        return
    row = refreshed

    title, body, url = _notification_content(row)
    if notify.send(title, body, url=url):
        status = (
            "late"
            if _is_late(row["fire_at"], now_dt, get_settings().reminders.late_threshold_minutes)
            else "sent"
        )
        now_iso = _utc_iso(now_dt)
        with connect() as conn:
            conn.execute(
                """UPDATE reminders
                   SET status = ?, fired_at = ?, next_attempt_at = NULL, last_error = NULL
                   WHERE id = ?""",
                (status, now_iso, row["id"]),
            )
            q.append_feed(
                conn,
                agent="reminder_engine",
                verb=status,
                obj=str(row["id"]),
                kind="reminder",
                payload={"kind": row["kind"], "entity_id": row["entity_id"], "title": title},
            )
        return

    _handle_notify_failure(row, now_dt)


def _handle_notify_failure(row: dict, now_dt: datetime) -> None:
    settings = get_settings().reminders
    attempts = _increment_attempts(row["id"])
    error = "notifier returned False"
    if attempts >= settings.max_attempts:
        with connect() as conn:
            conn.execute(
                """UPDATE reminders
                   SET status = 'notify_failed',
                       fired_at = NULL,
                       next_attempt_at = NULL,
                       last_error = ?
                   WHERE id = ?""",
                (error, row["id"]),
            )
            q.append_feed(
                conn,
                agent="reminder_engine",
                verb="notify_failed",
                obj=str(row["id"]),
                kind="reminder",
                payload={"kind": row["kind"], "entity_id": row["entity_id"], "error": error},
            )
        log.warning("reminder %s notify failed permanently after %s attempts", row["id"], attempts)
        return

    next_at = now_dt + timedelta(seconds=_retry_backoff_seconds(attempts))
    with connect() as conn:
        conn.execute(
            """UPDATE reminders
               SET status = 'pending',
                   fired_at = NULL,
                   next_attempt_at = ?,
                   last_error = ?
               WHERE id = ?""",
            (_utc_iso(next_at), error, row["id"]),
        )
    log.warning("reminder %s notify failed; retrying at %s", row["id"], _utc_iso(next_at))


def _increment_attempts(reminder_id: int) -> int:
    with connect() as conn:
        row = conn.execute(
            """UPDATE reminders
               SET attempts = attempts + 1
               WHERE id = ?
               RETURNING attempts""",
            (reminder_id,),
        ).fetchone()
    if row is None:
        return 1
    return int(row["attempts"])


def _sync_pending_task_due_reminder(row: dict) -> bool:
    target, reason = _task_due_target(row)
    if reason is not None:
        with connect() as conn:
            cur = conn.execute(
                """UPDATE reminders
                   SET status = 'cancelled',
                   fired_at = NULL,
                   next_attempt_at = NULL,
                   last_error = ?
                   WHERE id = ? AND status = 'pending' AND deleted_at IS NULL""",
                (reason, row["id"]),
            )
            return cur.rowcount == 1
    assert target is not None
    with connect() as conn:
        cur = conn.execute(
            """UPDATE reminders
               SET fire_at = ?,
                   lead_minutes = ?,
                   title = 'Task due',
                   body = ?,
                   next_attempt_at = NULL,
                   last_error = NULL
               WHERE id = ? AND status = 'pending' AND deleted_at IS NULL""",
            (target["fire_at"], target["lead_minutes"], target["body"], row["id"]),
        )
        return cur.rowcount == 1


def _refresh_claimed_task_due_reminder(row: dict, now_dt: datetime) -> dict | None:
    target, reason = _task_due_target(row)
    if reason is not None:
        _cancel_claimed(row, reason)
        return None
    if target is None:
        return row
    if _parse_datetime(target["fire_at"]) > now_dt:
        _reschedule_claimed(row, target)
        return None
    refreshed = {
        **row,
        "fire_at": target["fire_at"],
        "lead_minutes": target["lead_minutes"],
        "title": "Task due",
        "body": target["body"],
    }
    with connect() as conn:
        conn.execute(
            """UPDATE reminders
               SET fire_at = ?,
                   lead_minutes = ?,
                   title = 'Task due',
                   body = ?,
                   last_error = NULL
               WHERE id = ? AND status = 'firing' AND deleted_at IS NULL""",
            (target["fire_at"], target["lead_minutes"], target["body"], row["id"]),
        )
    return refreshed


def _task_due_target(row: dict) -> tuple[dict | None, str | None]:
    if row.get("kind") != "task_due" or row.get("entity_type") != "task":
        return None, None
    with connect() as conn:
        task = conn.execute(
            """SELECT status, no_reminder, deleted_at, due, reminder_lead_minutes, text
               FROM tasks WHERE uid = ?""",
            (row.get("entity_id"),),
        ).fetchone()
    if task is None:
        return None, "task missing"
    if task["deleted_at"] is not None:
        return None, "task deleted"
    if task["no_reminder"]:
        return None, "task no_reminder"
    if task["status"] != "open":
        return None, "task is not open"
    if not task["due"]:
        return None, "task due removed"
    lead_minutes = task["reminder_lead_minutes"]
    if lead_minutes is None:
        lead_minutes = row.get("lead_minutes")
    if lead_minutes is None:
        return None, "task reminder lead removed"
    try:
        due_dt = _parse_datetime(task["due"])
    except ValueError:
        return None, "task due unparsable"
    fire_at = due_dt - timedelta(minutes=int(lead_minutes))
    return {
        "fire_at": _utc_iso(fire_at),
        "lead_minutes": int(lead_minutes),
        "body": task["text"],
    }, None


def _reschedule_claimed(row: dict, target: dict) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE reminders
               SET status = 'pending',
                   fire_at = ?,
                   lead_minutes = ?,
                   title = 'Task due',
                   body = ?,
                   fired_at = NULL,
                   next_attempt_at = NULL,
                   last_error = NULL
               WHERE id = ? AND status = 'firing' AND deleted_at IS NULL""",
            (target["fire_at"], target["lead_minutes"], target["body"], row["id"]),
        )


def _cancel_claimed(row: dict, reason: str) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE reminders
               SET status = 'cancelled',
                   next_attempt_at = NULL,
                   last_error = ?
               WHERE id = ?""",
            (reason, row["id"]),
        )
        q.append_feed(
            conn,
            agent="reminder_engine",
            verb="cancelled",
            obj=str(row["id"]),
            kind="reminder",
            payload={"kind": row["kind"], "entity_id": row["entity_id"], "reason": reason},
        )


def _notification_content(row: dict) -> tuple[str, str, str | None]:
    title = row.get("title")
    body = row.get("body")
    if row.get("kind") == "task_due" and (not title or not body):
        with connect() as conn:
            task = conn.execute(
                "SELECT text FROM tasks WHERE uid = ? AND deleted_at IS NULL",
                (row.get("entity_id"),),
            ).fetchone()
        title = title or "Task due"
        body = body or (task["text"] if task else f"Task {row.get('entity_id')} is due")
    return title or "Mastisk reminder", body or "Reminder due", row.get("url")


def _open_tasks_with_due() -> list[dict]:
    with connect() as conn:
        return [
            dict(row)
            for row in conn.execute(
                """SELECT uid, text, due
                   FROM tasks
                   WHERE status = 'open'
                     AND deleted_at IS NULL
                     AND due IS NOT NULL
                   ORDER BY due ASC"""
            )
        ]


def _ensure_daily_summary_reminder(now_dt: datetime) -> dict | None:
    settings = get_settings()
    configured_time = settings.reminders.daily_summary_time.strip()
    if not configured_time:
        return None
    try:
        summary_time = _parse_hhmm(configured_time)
        tz = ZoneInfo(settings.capture.default_timezone)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        _log_invalid_daily_summary_config_once(configured_time, str(exc))
        return None

    local_now = now_dt.astimezone(tz)
    fire_local = datetime.combine(local_now.date(), summary_time, tzinfo=tz)
    fire_at = fire_local.astimezone(UTC)
    if now_dt < fire_at:
        return None

    entity_id = local_now.date().isoformat()
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM reminders
               WHERE kind = 'daily_summary'
                 AND entity_id = ?
                 AND deleted_at IS NULL""",
            (entity_id,),
        ).fetchone()
    if row:
        return _reminder_row(dict(row))

    open_tasks = _open_tasks_with_due()
    title, body = compose_daily_summary(today=local_now.date(), open_tasks=open_tasks)
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO reminders
                 (entity_type, entity_id, fire_at, kind, status, title, body)
               VALUES ('daily_summary', ?, ?, 'daily_summary', 'pending', ?, ?)""",
            (entity_id, _utc_iso(fire_at), title, body),
        )
        row = conn.execute(
            """SELECT * FROM reminders
               WHERE kind = 'daily_summary'
                 AND entity_id = ?
                 AND deleted_at IS NULL""",
            (entity_id,),
        ).fetchone()
    return _reminder_row(dict(row)) if row else None


def _log_invalid_daily_summary_config_once(value: str, error: str) -> None:
    key = (value, error)
    if key in _logged_invalid_daily_summary_configs:
        return
    _logged_invalid_daily_summary_configs.add(key)
    log.warning("daily summary disabled by invalid reminders config: %s", error)


def _task_due_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _is_late(fire_at: str, now_dt: datetime, late_threshold_minutes: int) -> bool:
    return now_dt - _parse_datetime(fire_at) > timedelta(minutes=late_threshold_minutes)


def _retry_backoff_seconds(attempts: int) -> int:
    backoff = get_settings().reminders.retry_backoff_seconds
    if not backoff:
        return 60
    return int(backoff[min(attempts - 1, len(backoff) - 1)])


def _notify_backend_disabled() -> bool:
    return get_settings().notify.backend.strip().lower() == "none"


def _parse_hhmm(value: str) -> time:
    hour_s, minute_s = value.split(":", 1)
    return time(hour=int(hour_s), minute=int(minute_s))


def _parse_datetime(value: str) -> datetime:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_default_timezone())
    return dt.astimezone(UTC)


def _coerce_datetime(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now().astimezone().astimezone(UTC)
    if value.tzinfo is None:
        value = value.replace(tzinfo=_default_timezone())
    return value.astimezone(UTC)


def _default_timezone() -> ZoneInfo:
    return ZoneInfo(get_settings().capture.default_timezone)


def _utc_iso(value: datetime) -> str:
    return _coerce_datetime(value).isoformat()


def _reminder_row(row: dict) -> dict:
    return row
