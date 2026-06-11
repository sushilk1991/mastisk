"""Parse and rewrite inline markdown tasks.

Decision for Phase 3: any ``- [ ]`` or ``- [x]`` checkbox line in a host file is
a task, regardless of section heading. The mirror is derived from host files;
section-aware filtering would make the file contract harder to explain and is
not required by the spec.
"""
from __future__ import annotations

import re
import secrets
import string
from collections.abc import Callable
from datetime import date
from datetime import datetime
from typing import Any

_UNSET = object()

_TASK_RE = re.compile(r"^(?P<prefix>\s*-\s+\[(?P<mark>[ xX])\]\s*)(?P<body>.*)$")
_DUE_RE = re.compile(r"\s*📅\s*(?P<value>\d{4}-\d{2}-\d{2})")
_TIME_RE = re.compile(r"\s*⏰\s*(?P<value>\d{2}:\d{2})")
_SCHEDULED_RE = re.compile(r"\s*⏳\s*(?P<value>\d{4}-\d{2}-\d{2})")
_RECURRENCE_RE = re.compile(
    r"\s*🔁\s*(?P<value>.*?)(?=\s+(?:📅|⏰|⏳|⏫|🔼|🔽|🆔|#[^\s#]+|\[\[)|\s*$)"
)
_UID_RE = re.compile(r"\s*🆔\s*(?P<value>[A-Za-z0-9_-]+)")
_TAG_RE = re.compile(r"(?<!\S)#(?P<value>[A-Za-z0-9_/-]+)")
_LINK_RE = re.compile(r"\[\[(?P<value>[^\]|#]+)(?:[|#][^\]]*)?\]\]")
_PRIORITY_TO_ICON = {"high": "⏫", "medium": "🔼", "low": "🔽"}
_ICON_TO_PRIORITY = {v: k for k, v in _PRIORITY_TO_ICON.items()}
_PRIORITY_RE = re.compile(r"\s*(?P<value>[⏫🔼🔽])")
_MARKER_GLYPHS_RE = re.compile(r"[🆔📅⏳🔁⏫🔼🔽⏰]")
_BASE36 = string.digits + string.ascii_lowercase


def parse_markdown_tasks(markdown: str, *, host_path: str = "") -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for idx, line in enumerate(markdown.splitlines(), start=1):
        parsed = parse_task_line(line, line_number=idx, host_path=host_path)
        if parsed is not None:
            tasks.append(parsed)
    return tasks


def parse_task_line(
    line: str,
    *,
    line_number: int = 1,
    host_path: str = "",
) -> dict[str, Any] | None:
    match = _TASK_RE.match(line)
    if not match:
        return None
    body = match.group("body")
    checked = match.group("mark").lower() == "x"
    due = _due_value(_first_match(_DUE_RE, body), _first_match(_TIME_RE, body))
    scheduled = _first_match(_SCHEDULED_RE, body)
    recurrence = _first_match(_RECURRENCE_RE, body)
    uid = _first_match(_UID_RE, body)
    priority_icon = _first_match(_PRIORITY_RE, body)
    tags = _TAG_RE.findall(body)
    text = _clean_task_text(body)
    return {
        "host_path": host_path,
        "line_number": line_number,
        "checked": checked,
        "status": "done" if checked else "open",
        "text": text,
        "due": due,
        "scheduled": scheduled,
        "recurrence": recurrence,
        "priority": _ICON_TO_PRIORITY.get(priority_icon or ""),
        "tags": tags,
        "links": [m.group("value").strip() for m in _LINK_RE.finditer(body)],
        "uid": uid,
        "needs_triage": "needs-triage" in tags,
    }


def rewrite_task_by_uid(markdown: str, uid: str, **updates: Any) -> str:
    lines = markdown.splitlines(keepends=True)
    rewritten: list[str] = []
    found = False
    for raw in lines:
        line, ending = _split_line_ending(raw)
        parsed = parse_task_line(line)
        if parsed is not None and parsed["uid"] == uid:
            rewritten.append(rewrite_task_line(line, **updates) + ending)
            found = True
        else:
            rewritten.append(raw)
    if not found:
        raise KeyError(f"task uid not found: {uid}")
    return "".join(rewritten)


def rewrite_task_line(
    line: str,
    *,
    checked: bool | object = _UNSET,
    due: str | None | object = _UNSET,
    scheduled: str | None | object = _UNSET,
    recurrence: str | None | object = _UNSET,
    priority: str | None | object = _UNSET,
    uid: str | None | object = _UNSET,
) -> str:
    match = _TASK_RE.match(line)
    if not match:
        raise ValueError("not a markdown task line")

    mark = match.group("mark")
    if checked is not _UNSET:
        mark = "x" if checked else " "
    prefix = re.sub(r"\[[ xX]\]", f"[{mark}]", match.group("prefix"), count=1)
    body = match.group("body")

    if due is not _UNSET:
        body = _DUE_RE.sub("", body)
        body = _TIME_RE.sub("", body)
    if scheduled is not _UNSET:
        body = _SCHEDULED_RE.sub("", body)
    if recurrence is not _UNSET:
        body = _RECURRENCE_RE.sub("", body)
    if priority is not _UNSET:
        body = _PRIORITY_RE.sub("", body)
    if uid is not _UNSET:
        body = _UID_RE.sub("", body)

    additions: list[str] = []
    if due is not _UNSET and due:
        additions.extend(_due_marker_parts(str(due)))
    if scheduled is not _UNSET and scheduled:
        additions.append(f"⏳ {_date_and_clock(str(scheduled))[0]}")
    if recurrence is not _UNSET and recurrence:
        additions.append(f"🔁 {recurrence}")
    if priority is not _UNSET and priority:
        icon = _PRIORITY_TO_ICON.get(str(priority))
        if icon:
            additions.append(icon)
    if uid is not _UNSET and uid:
        additions.append(f"🆔 {uid}")
    if additions:
        body = f"{body.rstrip()} {' '.join(additions)}"
    return f"{prefix}{body.rstrip()}"


def ensure_task_uids(
    markdown: str,
    *,
    uid_factory: Callable[[], str] | None = None,
    existing_uids: set[str] | None = None,
) -> tuple[str, list[str]]:
    factory = uid_factory or generate_uid
    seen: set[str] = set(existing_uids or set())
    assigned: list[str] = []
    rewritten: list[str] = []
    for raw in markdown.splitlines(keepends=True):
        line, ending = _split_line_ending(raw)
        parsed = parse_task_line(line)
        if parsed is None:
            rewritten.append(raw)
            continue
        current_uid = parsed.get("uid")
        if current_uid and current_uid not in seen:
            seen.add(current_uid)
            rewritten.append(raw)
            continue
        uid = _next_unique_uid(factory, seen)
        seen.add(uid)
        assigned.append(uid)
        rewritten.append(rewrite_task_line(line, uid=uid) + ending)
    return "".join(rewritten), assigned


def format_task_line(
    text: str,
    *,
    checked: bool = False,
    due: str | None = None,
    scheduled: str | None = None,
    recurrence: str | None = None,
    priority: str | None = None,
    tags: list[str] | None = None,
    links: list[str] | None = None,
    uid: str | None = None,
) -> str:
    marker = "x" if checked else " "
    parts = [f"- [{marker}] {_sanitize_inline(text)}"]
    if due:
        parts.extend(_due_marker_parts(due))
    if scheduled:
        parts.append(f"⏳ {_date_and_clock(scheduled)[0]}")
    if recurrence:
        parts.append(f"🔁 {recurrence}")
    if priority and priority in _PRIORITY_TO_ICON:
        parts.append(_PRIORITY_TO_ICON[priority])
    for tag in tags or []:
        clean = _sanitize_tag(tag)
        if clean:
            parts.append(f"#{clean}")
    for link in links or []:
        clean = _sanitize_inline(link).strip("[]")
        if clean:
            parts.append(f"[[{clean}]]")
    if uid:
        parts.append(f"🆔 {uid}")
    return " ".join(parts)


def normalize_date_or_datetime(value: str) -> str:
    raw = value.strip()
    if not raw:
        raise ValueError("date value must be non-blank")
    if "T" not in raw:
        try:
            return date.fromisoformat(raw[:10]).isoformat()
        except ValueError as exc:
            raise ValueError("date value must be ISO date or datetime") from exc
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError as exc:
        raise ValueError("date value must be ISO date or datetime") from exc


def generate_uid(length: int = 8) -> str:
    return "".join(secrets.choice(_BASE36) for _ in range(length))


def _first_match(pattern: re.Pattern[str], body: str) -> str | None:
    match = pattern.search(body)
    if not match:
        return None
    return match.group("value").strip()


def _clean_task_text(body: str) -> str:
    stripped = _DUE_RE.sub("", body)
    stripped = _TIME_RE.sub("", stripped)
    stripped = _SCHEDULED_RE.sub("", stripped)
    stripped = _RECURRENCE_RE.sub("", stripped)
    stripped = _PRIORITY_RE.sub("", stripped)
    stripped = _UID_RE.sub("", stripped)
    stripped = _LINK_RE.sub("", stripped)
    stripped = _TAG_RE.sub("", stripped)
    return re.sub(r"\s+", " ", stripped).strip()


def _sanitize_inline(value: object) -> str:
    cleaned = _MARKER_GLYPHS_RE.sub("", str(value))
    return re.sub(r"\s+", " ", cleaned).strip()


def _sanitize_tag(value: object) -> str:
    cleaned = _sanitize_inline(value).lstrip("#")
    return re.sub(r"\s+", "-", cleaned).strip("-")


def _due_value(day: str | None, clock: str | None) -> str | None:
    if not day:
        return None
    if clock:
        return f"{day}T{clock}:00"
    return day


def _due_marker_parts(value: str) -> list[str]:
    day, clock = _date_and_clock(value)
    parts = [f"📅 {day}"]
    if clock:
        parts.append(f"⏰ {clock}")
    return parts


def _date_and_clock(value: str) -> tuple[str, str | None]:
    raw = value.strip()
    if "T" not in raw:
        return normalize_date_or_datetime(raw), None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10], None
    return parsed.date().isoformat(), parsed.strftime("%H:%M")


def _split_line_ending(raw: str) -> tuple[str, str]:
    if raw.endswith("\r\n"):
        return raw[:-2], "\r\n"
    if raw.endswith("\n"):
        return raw[:-1], "\n"
    if raw.endswith("\r"):
        return raw[:-1], "\r"
    return raw, ""


def _next_unique_uid(factory: Callable[[], str], existing: set[str]) -> str:
    for _ in range(100):
        uid = factory()
        if uid not in existing:
            return uid
    raise RuntimeError("unable to generate unique task uid")
