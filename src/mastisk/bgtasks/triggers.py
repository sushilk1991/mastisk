"""Trigger due-checks for automations: cron (missed = skipped) + daily windows.

Cron parsing rides APScheduler's CronTrigger (already a dependency) instead of
a hand-rolled matcher. Semantics copied from Rowboat's scheduler:

- **cron**: due when a scheduled fire time landed within the last
  ``grace_seconds`` AND after the last successful run. A fire missed by more
  than the grace (daemon asleep, Mac lid closed) is skipped, not replayed.
- **windows**: a list of daily HH:MM bands; the automation fires once per day
  per window, anywhere inside the band — forgiving for machines that aren't
  always on. Due when now is inside a band and the last successful run was
  before today's band start.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

_DOW_PART_RE = re.compile(r"^(?:\*|(?P<first>\d+)(?:-(?P<last>\d+))?)(?:/(?P<step>\d+))?$")


def due_trigger(
    triggers: dict,
    last_run_at: str | None,
    *,
    now: datetime,
    tz: ZoneInfo,
    grace_seconds: int = 120,
) -> str | None:
    """Return 'cron' | 'window' | None for the first satisfied trigger."""
    if not triggers:
        return None
    if triggers.get("cron") and _cron_due(
        triggers["cron"], last_run_at, now=now, tz=tz, grace_seconds=grace_seconds,
    ):
        return "cron"
    for window in triggers.get("windows") or []:
        if _window_due(window, last_run_at, now=now, tz=tz):
            return "window"
    return None


def cron_trigger(expr: str, tz: tzinfo) -> CronTrigger:
    """Build a CronTrigger from a standard 5-field crontab; ValueError if invalid."""
    fields = expr.split()
    if len(fields) != 5:
        raise ValueError(f"cron must be a 5-field expression, got {expr!r}")
    fields[4] = _apscheduler_day_of_week(fields[4])
    return CronTrigger.from_crontab(" ".join(fields), timezone=tz)


def _apscheduler_day_of_week(field: str) -> str:
    """Translate a numeric crontab day-of-week field for APScheduler.

    Crontab counts 0 or 7 = Sunday, 1 = Monday; APScheduler counts 0 = Monday
    and rejects 7, so "0 9 * * 1" would fire on Tuesday. Numeric parts are
    expanded to an explicit day list because a crontab range such as 0-2
    (Sun-Tue) wraps in APScheduler's numbering. Day names mean the same in
    both and pass through.
    """
    if not re.search(r"\d", field):
        return field
    if re.search(r"[A-Za-z]", field):
        raise ValueError(f"cron day-of-week mixes names and numbers: {field!r}")
    days: set[int] = set()
    for part in field.split(","):
        match = _DOW_PART_RE.match(part)
        if not match:
            raise ValueError(f"invalid cron day-of-week {field!r}")
        step = int(match.group("step") or 1)
        if match.group("first") is None:
            first, last = 0, 6
        else:
            first = int(match.group("first"))
            last = int(match.group("last") or (6 if match.group("step") else first))
        if step < 1 or first > last or last > 7:
            raise ValueError(f"invalid cron day-of-week {field!r}")
        days.update(day % 7 for day in range(first, last + 1, step))
    if len(days) == 7:
        return "*"
    return ",".join(str(day) for day in sorted((day - 1) % 7 for day in days))


def _cron_due(
    expr: str,
    last_run_at: str | None,
    *,
    now: datetime,
    tz: ZoneInfo,
    grace_seconds: int,
) -> bool:
    try:
        trigger = cron_trigger(expr, tz)
    except ValueError:
        return False
    local_now = now.astimezone(tz)
    window_start = local_now - timedelta(seconds=grace_seconds)
    # The MOST RECENT scheduled fire in (window_start, now]. A cron finer than
    # the grace window (e.g. "* * * * *" with a 120s grace) has several fires
    # in the window; taking only the earliest would let an already-run fire
    # mask a later, legitimately-due one. Walk forward and keep the last.
    most_recent = None
    cursor = window_start
    for _ in range(16):
        fire = trigger.get_next_fire_time(None, cursor)
        if fire is None or fire > local_now:
            break
        most_recent = fire
        cursor = fire + timedelta(seconds=1)
    if most_recent is None:
        return False
    anchor = _parse(last_run_at, tz)
    return anchor is None or most_recent > anchor


def _window_due(
    window: dict,
    last_run_at: str | None,
    *,
    now: datetime,
    tz: ZoneInfo,
) -> bool:
    local_now = now.astimezone(tz)
    try:
        start_h, start_m = map(int, str(window.get("start", "")).split(":"))
        end_h, end_m = map(int, str(window.get("end", "")).split(":"))
    except ValueError:
        return False
    band_start = local_now.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    band_end = local_now.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if not (band_start <= local_now <= band_end):
        return False
    anchor = _parse(last_run_at, tz)
    return anchor is None or anchor < band_start


def _parse(value: str | None, tz: ZoneInfo) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)
