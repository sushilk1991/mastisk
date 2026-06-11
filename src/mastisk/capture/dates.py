"""Deterministic relative-date normalization for captured text."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

_WEEKDAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_NEXT_WEEKDAY_RE = re.compile(
    r"\bnext\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I
)
_TIME_12H_RE = re.compile(r"\b(?:at\s+)?(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?m\.?\b", re.I)
_TIME_24H_RE = re.compile(r"\b(?:at\s+)?([01]?\d|2[0-3]):([0-5]\d)\b", re.I)


def resolve_datetime(
    text: str,
    ts: str | datetime | None,
    timezone: str,
    *,
    model_iso: str | None = None,
) -> str | None:
    """Resolve date/time language in ``text`` against request timestamp ``ts``.

    Returns an ISO date for date-only phrases, an ISO datetime for phrases that
    include or imply a time, and ``None`` when neither code nor the model's ISO
    field yields a parseable value.
    """
    base = _parse_base(ts, timezone)
    lowered = text.lower()

    parsed_time = _extract_time(lowered)
    parsed_date: date | None = None
    forced_time: time | None = None
    date_kind: str | None = None

    if re.search(r"\bnext\s+week\b", lowered):
        days = 7 - base.weekday()
        parsed_date = (base + timedelta(days=days)).date()
        date_kind = "relative"
    elif re.search(r"\btomorrow\b", lowered):
        parsed_date = (base + timedelta(days=1)).date()
        date_kind = "relative"
    elif re.search(r"\btoday\b", lowered):
        parsed_date = base.date()
        date_kind = "relative"
    elif re.search(r"\btonight\b", lowered):
        parsed_date = base.date()
        forced_time = time(20, 0)
        date_kind = "relative"
    else:
        next_weekday_match = _NEXT_WEEKDAY_RE.search(lowered)
        weekday_match = next_weekday_match or _WEEKDAY_RE.search(lowered)
        if next_weekday_match:
            target_weekday = _WEEKDAYS[next_weekday_match.group(1).lower()]
            days = (target_weekday - base.weekday()) % 7 or 7
            parsed_date = (base + timedelta(days=days)).date()
            date_kind = "next_weekday"
        elif weekday_match:
            target_weekday = _WEEKDAYS[weekday_match.group(1).lower()]
            days = (target_weekday - base.weekday()) % 7
            parsed_date = (base + timedelta(days=days)).date()
            date_kind = "weekday"

    parsed_time = parsed_time or forced_time
    if parsed_date is not None:
        if parsed_time is None:
            return parsed_date.isoformat()
        candidate = datetime.combine(parsed_date, parsed_time, tzinfo=base.tzinfo)
        if candidate <= base and date_kind == "weekday":
            candidate += timedelta(days=7)
        return _format_datetime(candidate)

    if parsed_time is not None:
        candidate = datetime.combine(base.date(), parsed_time, tzinfo=base.tzinfo)
        if candidate <= base:
            candidate += timedelta(days=1)
        return _format_datetime(candidate)

    return _normalize_model_iso(model_iso, base.tzinfo)


def normalize_model_datetime(
    model_iso: str | None,
    ts: str | datetime | None,
    timezone: str,
) -> str | None:
    """Normalize a model-supplied ISO value without scanning text for dates."""
    base = _parse_base(ts, timezone)
    return _normalize_model_iso(model_iso, base.tzinfo)


def _parse_base(ts: str | datetime | None, timezone: str) -> datetime:
    tz = ZoneInfo(timezone)
    if isinstance(ts, datetime):
        base = ts
    elif ts:
        base = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    else:
        base = datetime.now(tz)
    if base.tzinfo is None:
        return base.replace(tzinfo=tz)
    return base.astimezone(tz)


def _extract_time(text: str) -> time | None:
    m = _TIME_12H_RE.search(text)
    if m:
        hour = int(m.group(1))
        minute = int(m.group(2) or "0")
        if not 1 <= hour <= 12:
            return None
        meridiem = m.group(3).lower()
        if meridiem == "p" and hour != 12:
            hour += 12
        if meridiem == "a" and hour == 12:
            hour = 0
        return time(hour, minute)

    m = _TIME_24H_RE.search(text)
    if m:
        return time(int(m.group(1)), int(m.group(2)))
    return None


def _normalize_model_iso(value: str | None, tzinfo) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw).isoformat()
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    parsed = parsed.replace(tzinfo=tzinfo) if parsed.tzinfo is None else parsed.astimezone(tzinfo)
    return _format_datetime(parsed)


def _format_datetime(value: datetime) -> str:
    return value.isoformat(timespec="seconds")
