"""Pure streak math for routines.

Day boundary decision: all callers pass local ISO dates. API/engine callers use
``[capture] default_timezone`` to derive today's date before calling these
helpers.
"""
from __future__ import annotations

from datetime import date, timedelta


def current_streak(completion_dates: set[str] | list[str], *, today: str) -> int:
    dates = _date_set(completion_dates)
    anchor = date.fromisoformat(today)
    if anchor not in dates:
        anchor = anchor - timedelta(days=1)
        if anchor not in dates:
            return 0
    streak = 0
    cursor = anchor
    while cursor in dates:
        streak += 1
        cursor -= timedelta(days=1)
    return streak


def longest_streak(completion_dates: set[str] | list[str]) -> int:
    dates = sorted(_date_set(completion_dates))
    if not dates:
        return 0
    longest = current = 1
    previous = dates[0]
    for day in dates[1:]:
        if day == previous + timedelta(days=1):
            current += 1
        else:
            longest = max(longest, current)
            current = 1
        previous = day
    return max(longest, current)


def completion_rate_30d(completion_dates: set[str] | list[str], *, today: str) -> float:
    dates = _date_set(completion_dates)
    end = date.fromisoformat(today)
    start = end - timedelta(days=29)
    done = sum(1 for day in dates if start <= day <= end)
    return done / 30


def fixed_challenge_progress(
    completion_dates: set[str] | list[str],
    *,
    target_days: int | None,
    start_date: str | None,
    today: str,
) -> dict:
    target = int(target_days or 0)
    if not start_date or target <= 0:
        return {
            "days_done": 0,
            "target_days": target,
            "remaining": max(target, 0),
            "complete": False,
        }
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(today)
    done = sum(1 for day in _date_set(completion_dates) if start <= day <= end)
    remaining = max(target - done, 0)
    return {
        "days_done": done,
        "target_days": target,
        "remaining": remaining,
        "complete": target > 0 and done >= target,
    }


def _date_set(values: set[str] | list[str]) -> set[date]:
    parsed: set[date] = set()
    for value in values:
        try:
            parsed.add(date.fromisoformat(str(value)))
        except ValueError:
            continue
    return parsed
