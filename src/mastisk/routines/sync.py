"""Routine mirror scanner and file-first routine mutations."""
from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from slugify import slugify

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.paths import routines_dir, vault_dir
from mastisk.projects.sync import split_frontmatter
from mastisk.routes.notes import atomic_write
from mastisk.routines.streaks import (
    completion_rate_30d,
    current_streak,
    fixed_challenge_progress,
    longest_streak,
)
from mastisk.settings import get_settings

TimeOfDay = Literal["morning", "afternoon", "evening", "anytime"]
StreakType = Literal["ongoing", "fixed"]

_VALID_TIMES = {"morning", "afternoon", "evening", "anytime"}
_VALID_STREAKS = {"ongoing", "fixed"}
_COMPLETION_RE = re.compile(r"^\s*-\s*(?P<date>\d{4}-\d{2}-\d{2})\s*$")
_COMPLETIONS_HEADING_RE = re.compile(r"^##\s+Completions\s*$", re.I)
_NEXT_HEADING_RE = re.compile(r"^##\s+")
_HHMM_RE = re.compile(r"^\d{2}:\d{2}$")


def scan_routines(paths: list[Path] | None = None) -> dict[str, int]:
    routine_paths = paths if paths is not None else _routine_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for raw_path in routine_paths:
            path = _absolute_path(raw_path)
            if not path.exists() or path.name.startswith("."):
                if paths is not None and path.suffix == ".md":
                    _soft_delete_missing_path(conn, path)
                continue
            with host_file_lock(path):
                routine = parse_routine_file(path)
            slug = routine["slug"]
            seen.add(slug)
            conn.execute(
                """INSERT INTO routines
                   (slug, path, name, description, domain, time_of_day, specific_time,
                    notify, streak_type, target_days, start_date, archived, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(slug) DO UPDATE SET
                     path=excluded.path,
                     name=excluded.name,
                     description=excluded.description,
                     domain=excluded.domain,
                     time_of_day=excluded.time_of_day,
                     specific_time=excluded.specific_time,
                     notify=excluded.notify,
                     streak_type=excluded.streak_type,
                     target_days=excluded.target_days,
                     start_date=excluded.start_date,
                     archived=excluded.archived,
                     deleted_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    slug,
                    routine["path"],
                    routine["name"],
                    routine.get("description"),
                    routine.get("domain"),
                    routine["time_of_day"],
                    routine.get("specific_time"),
                    1 if routine.get("notify") else 0,
                    routine["streak_type"],
                    routine.get("target_days"),
                    routine.get("start_date"),
                    1 if routine.get("archived") else 0,
                ),
            )
            conn.execute("DELETE FROM routine_completions WHERE routine_id = ?", (slug,))
            for completion in routine["completions"]:
                conn.execute(
                    """INSERT OR IGNORE INTO routine_completions
                       (routine_id, date)
                       VALUES (?, ?)""",
                    (slug, completion),
                )
            upserted += 1
        if paths is None:
            _soft_delete_disappeared(conn, seen)
    return {"upserted": upserted}


def parse_routine_file(path: Path) -> dict[str, Any]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    name = str(meta.get("name") or path.stem).strip() or path.stem
    time_of_day = _clean_choice(meta.get("time_of_day"), _VALID_TIMES, "anytime")
    streak_type = _clean_choice(meta.get("streak_type"), _VALID_STREAKS, "ongoing")
    specific_time = _clean_specific_time(meta.get("specific_time"))
    return {
        "slug": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "name": name,
        "description": _clean_optional(meta.get("description")),
        "domain": _clean_optional(meta.get("domain")),
        "time_of_day": time_of_day,
        "specific_time": specific_time,
        "notify": bool(meta.get("notify", False)),
        "streak_type": streak_type,
        "target_days": _clean_int(meta.get("target_days")),
        "start_date": _clean_date(meta.get("start_date")),
        "archived": bool(meta.get("archived", False)),
        "completions": _completion_dates_from_body(body),
        "body": body,
        "frontmatter": meta,
    }


def create_routine_file(
    *,
    name: str,
    description: str | None = None,
    domain: str | None = None,
    time_of_day: str = "anytime",
    specific_time: str | None = None,
    notify: bool = False,
    streak_type: str = "ongoing",
    target_days: int | None = None,
    start_date: str | None = None,
) -> dict[str, Any]:
    path = _next_routine_path(name)
    meta = _routine_meta(
        name=name,
        description=description,
        domain=domain,
        time_of_day=time_of_day,
        specific_time=specific_time,
        notify=notify,
        streak_type=streak_type,
        target_days=target_days,
        start_date=start_date,
        archived=False,
    )
    with host_file_lock(path):
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, dump_routine_file(meta, [], "## Completions\n"))
    scan_routines([path])
    row = get_routine(path.stem, include_archived=True)
    if row is None:
        raise RuntimeError(f"routine mirror missing after write: {path.stem}")
    return row


def toggle_routine_completion(
    slug: str,
    *,
    date_value: str | None = None,
    today: str | None = None,
) -> dict[str, Any] | None:
    return _mutate_routine_completion(slug, date_value=date_value, today=today, force=None)


def complete_routine_completion(
    slug: str,
    *,
    date_value: str | None = None,
    today: str | None = None,
) -> dict[str, Any] | None:
    return _mutate_routine_completion(slug, date_value=date_value, today=today, force=True)


def _mutate_routine_completion(
    slug: str,
    *,
    date_value: str | None,
    today: str | None,
    force: bool | None,
) -> dict[str, Any] | None:
    routine = get_routine(slug, include_archived=True)
    if routine is None:
        return None
    target_date = _clean_date(date_value or today or local_today())
    if target_date is None:
        raise ValueError("date must be YYYY-MM-DD")
    path = vault_dir() / routine["path"]
    with host_file_lock(path):
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        file_dates = set(_completion_dates_from_body(body))
        all_dates = file_dates | set(completion_dates(slug))
        completed = force if force is not None else target_date not in all_dates
        if completed:
            all_dates.add(target_date)
        else:
            all_dates.discard(target_date)
        atomic_write(path, dump_routine_file(meta, sorted(all_dates), body))
    scan_routines([path])
    updated = get_routine(slug, include_archived=True)
    if updated is None:
        return None
    return {
        **updated,
        "completed": completed,
        "streak": routine_streak_summary(updated, today=target_date),
    }


def archive_routine(slug: str) -> dict[str, Any] | None:
    routine = get_routine(slug, include_archived=True)
    if routine is None:
        return None
    path = vault_dir() / routine["path"]
    with host_file_lock(path):
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        meta["archived"] = True
        atomic_write(path, dump_routine_file(meta, _completion_dates_from_body(body), body))
    scan_routines([path])
    return get_routine(slug, include_archived=True)


def get_routine(slug: str, *, include_archived: bool = False) -> dict[str, Any] | None:
    clauses = ["slug = ?", "deleted_at IS NULL"]
    params: list[Any] = [slug]
    if not include_archived:
        clauses.append("archived = 0")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM routines WHERE " + " AND ".join(clauses),
            tuple(params),
        ).fetchone()
    return _routine_row(dict(row)) if row else None


def list_routines(*, include_archived: bool = False) -> list[dict[str, Any]]:
    clauses = ["deleted_at IS NULL"]
    if not include_archived:
        clauses.append("archived = 0")
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM routines WHERE "
            + " AND ".join(clauses)
            + " ORDER BY CASE time_of_day WHEN 'morning' THEN 0 WHEN 'afternoon' THEN 1 WHEN 'evening' THEN 2 ELSE 3 END, name",
        ).fetchall()
    return [_routine_row(dict(row)) for row in rows]


def completion_dates(slug: str, *, days: int | None = None, today: str | None = None) -> list[str]:
    params: list[Any] = [slug]
    clauses = ["routine_id = ?"]
    if days is not None:
        end = date.fromisoformat(today or local_today())
        start = end.fromordinal(end.toordinal() - max(days - 1, 0))
        clauses.append("date >= ?")
        params.append(start.isoformat())
        clauses.append("date <= ?")
        params.append(end.isoformat())
    with connect() as conn:
        rows = conn.execute(
            "SELECT date FROM routine_completions WHERE "
            + " AND ".join(clauses)
            + " ORDER BY date",
            tuple(params),
        ).fetchall()
    return [row["date"] for row in rows]


def routine_streak_summary(routine: dict[str, Any], *, today: str | None = None) -> dict[str, Any]:
    day = today or local_today()
    dates = completion_dates(routine["slug"])
    summary: dict[str, Any] = {
        "current": current_streak(dates, today=day),
        "longest": longest_streak(dates),
        "rate_30d": completion_rate_30d(dates, today=day),
    }
    if routine.get("streak_type") == "fixed":
        summary["fixed"] = fixed_challenge_progress(
            dates,
            target_days=routine.get("target_days"),
            start_date=routine.get("start_date"),
            today=day,
        )
    return summary


def local_today(now: datetime | None = None) -> str:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    current = now or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    return current.astimezone(tz).date().isoformat()


def dump_routine_file(meta: dict[str, Any], completions: list[str], body: str) -> str:
    clean = {k: v for k, v in meta.items() if v is not None}
    frontmatter = yaml.safe_dump(
        clean,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{_replace_completions_section(body, completions).lstrip()}"


def _routine_paths() -> list[Path]:
    directory = routines_dir()
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.md") if p.is_file())


def _absolute_path(path: Path) -> Path:
    return path if path.is_absolute() else vault_dir() / path


def _next_routine_path(name: str) -> Path:
    base = slugify(name)[:80] or "routine"
    routines_dir().mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 100):
        slug = base if attempt == 1 else f"{base}-{attempt}"
        path = routines_dir() / f"{slug}.md"
        if path.exists():
            continue
        with connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM routines WHERE slug = ? AND deleted_at IS NULL",
                (slug,),
            ).fetchone()
        if existing is None:
            return path
    raise RuntimeError(f"unable to allocate routine slug for {name!r}")


def _routine_meta(**values: Any) -> dict[str, Any]:
    return {
        "name": str(values["name"]).strip(),
        "description": _clean_optional(values.get("description")),
        "domain": _clean_optional(values.get("domain")),
        "time_of_day": _clean_choice(values.get("time_of_day"), _VALID_TIMES, "anytime"),
        "specific_time": _clean_specific_time(values.get("specific_time")),
        "notify": bool(values.get("notify", False)),
        "streak_type": _clean_choice(values.get("streak_type"), _VALID_STREAKS, "ongoing"),
        "target_days": _clean_int(values.get("target_days")),
        "start_date": _clean_date(values.get("start_date")),
        "archived": bool(values.get("archived", False)),
    }


def _replace_completions_section(body: str, completions: list[str]) -> str:
    lines = body.splitlines()
    section = ["## Completions", *[f"- {day}" for day in sorted(set(completions))]]
    for idx, line in enumerate(lines):
        if not _COMPLETIONS_HEADING_RE.match(line):
            continue
        end = len(lines)
        for cursor in range(idx + 1, len(lines)):
            if _NEXT_HEADING_RE.match(lines[cursor]):
                end = cursor
                break
        combined = [*lines[:idx], *section, *lines[end:]]
        return "\n".join(combined).rstrip() + "\n"
    if lines and lines[-1].strip():
        lines.append("")
    lines.extend(section)
    return "\n".join(lines).rstrip() + "\n"


def _completion_dates_from_body(body: str) -> list[str]:
    dates: set[str] = set()
    lines = body.splitlines()
    start: int | None = None
    for idx, line in enumerate(lines):
        if _COMPLETIONS_HEADING_RE.match(line):
            start = idx + 1
            break
    if start is None:
        return []
    for line in lines[start:]:
        if _NEXT_HEADING_RE.match(line):
            break
        match = _COMPLETION_RE.match(line)
        if not match:
            continue
        parsed = _clean_date(match.group("date"))
        if parsed:
            dates.add(parsed)
    return sorted(dates)


def _routine_row(row: dict[str, Any]) -> dict[str, Any]:
    row["notify"] = bool(row.get("notify"))
    row["archived"] = bool(row.get("archived"))
    return row


def _clean_choice(value: object, valid: set[str], fallback: str) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in valid else fallback


def _clean_optional(value: object) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip()
    return cleaned or None


def _clean_specific_time(value: object) -> str | None:
    cleaned = _clean_optional(value)
    if not cleaned or not _HHMM_RE.match(cleaned):
        return None
    try:
        hour_s, minute_s = cleaned.split(":", 1)
        if 0 <= int(hour_s) <= 23 and 0 <= int(minute_s) <= 59:
            return cleaned
    except ValueError:
        return None
    return None


def _clean_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clean_date(value: object) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _soft_delete_missing_path(conn, path: Path) -> None:
    try:
        rel = str(path.relative_to(vault_dir()))
    except ValueError:
        return
    conn.execute(
        "UPDATE routines SET deleted_at = CURRENT_TIMESTAMP WHERE path = ? AND deleted_at IS NULL",
        (rel,),
    )


def _soft_delete_disappeared(conn, seen: set[str]) -> None:
    if seen:
        placeholders = ",".join("?" for _ in seen)
        conn.execute(
            f"""UPDATE routines SET deleted_at = CURRENT_TIMESTAMP
                WHERE deleted_at IS NULL AND slug NOT IN ({placeholders})""",
            tuple(seen),
        )
    else:
        conn.execute("UPDATE routines SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL")
