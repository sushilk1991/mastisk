"""Journal day scanner and file-first mutations."""
from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.markdown_sections import append_to_section
from mastisk.paths import journal_dir, vault_dir
from mastisk.routes.notes import atomic_write
from mastisk.settings import get_settings

_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_H2_RE = re.compile(r"(?m)^##\s+(.+?)\s*$")
_LOG_BULLET_RE = re.compile(r"^\s*-\s+\d{2}:\d{2}\b")
_USER_HEADING_RE = re.compile(r"(?m)^(#{1,6}\s)")
_REQUIRED_SECTIONS = ("Tasks", "Log", "Reflections")
_OWNED_FRONTMATTER_KEYS = {"mood", "energy"}
_UNSET = object()


class JournalFrontmatterError(ValueError):
    """Raised when a journal day has frontmatter this module cannot preserve."""


def ensure_day(day: str | date | datetime) -> Path:
    target = _day_path(_coerce_day(day))
    with host_file_lock(target):
        _ensure_day_locked(target)
    return target


def append_log(
    day: str | date | datetime,
    text: str,
    ts: str | datetime,
    source: str | None = None,
) -> dict[str, str]:
    target_day = _coerce_day(day)
    target = _day_path(target_day)
    body = _neutralize_heading_lines(_clean_one_line(text))
    if not body:
        raise ValueError("log text must be non-blank")
    source_suffix = _source_suffix(source)
    line = f"- {_coerce_timestamp(ts).strftime('%H:%M')} {body}{source_suffix}"
    with host_file_lock(target):
        _ensure_day_locked(target)
        frontmatter, markdown_body = _split_frontmatter(target.read_text(encoding="utf-8"))
        updated_body = append_to_section(markdown_body, "Log", line)
        atomic_write(target, _dump_day(frontmatter, updated_body))
    scan_journal_days([target])
    _bump_projects_for_journal_day(target_day.isoformat())
    return {
        "date": target_day.isoformat(),
        "path": str(target.relative_to(vault_dir())),
        "line": line,
    }


def set_reflections(day: str | date | datetime, text: str) -> dict[str, str]:
    target_day = _coerce_day(day)
    target = _day_path(target_day)
    with host_file_lock(target):
        _ensure_day_locked(target)
        frontmatter, markdown_body = _split_frontmatter(target.read_text(encoding="utf-8"))
        updated_body = _replace_section(
            markdown_body,
            "Reflections",
            _neutralize_heading_lines(text.strip()),
        )
        atomic_write(target, _dump_day(frontmatter, updated_body))
    scan_journal_days([target])
    return {"date": target_day.isoformat(), "path": str(target.relative_to(vault_dir()))}


def set_mood_energy(
    day: str | date | datetime,
    mood: int | None | object = _UNSET,
    energy: int | None | object = _UNSET,
) -> dict[str, str]:
    target_day = _coerce_day(day)
    target = _day_path(target_day)
    with host_file_lock(target):
        _ensure_day_locked(target)
        frontmatter, markdown_body = _split_frontmatter(target.read_text(encoding="utf-8"))
        _apply_optional_scale(frontmatter, "mood", mood)
        _apply_optional_scale(frontmatter, "energy", energy)
        atomic_write(target, _dump_day(frontmatter, markdown_body))
    scan_journal_days([target])
    return {"date": target_day.isoformat(), "path": str(target.relative_to(vault_dir()))}


def scan_journal_days(paths: list[Path] | None = None) -> dict[str, int]:
    day_paths = paths if paths is not None else _journal_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for raw_path in day_paths:
            path = _absolute_path(raw_path)
            rel_path = _relative_path(path)
            if (
                not path.exists()
                or path.name.startswith(".")
                or path.suffix != ".md"
                or not _is_valid_day(path.stem)
            ):
                if paths is not None and rel_path is not None:
                    conn.execute(
                        """UPDATE journal_days SET deleted_at = CURRENT_TIMESTAMP
                           WHERE path = ? AND deleted_at IS NULL""",
                        (rel_path,),
                    )
                continue
            with host_file_lock(path):
                try:
                    parsed = parse_journal_file(path)
                except JournalFrontmatterError:
                    if paths is not None:
                        raise
                    seen.add(path.stem)
                    continue
            seen.add(parsed["date"])
            conn.execute(
                """INSERT INTO journal_days
                   (date, path, mood, energy, log_count, has_reflections, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(date) DO UPDATE SET
                     path=excluded.path,
                     mood=excluded.mood,
                     energy=excluded.energy,
                     log_count=excluded.log_count,
                     has_reflections=excluded.has_reflections,
                     deleted_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    parsed["date"],
                    parsed["path"],
                    parsed.get("mood"),
                    parsed.get("energy"),
                    parsed["log_count"],
                    1 if parsed["has_reflections"] else 0,
                ),
            )
            upserted += 1
        if paths is None:
            _soft_delete_disappeared(conn, seen)
    return {"upserted": upserted}


def parse_journal_file(path: Path) -> dict[str, Any]:
    frontmatter, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    sections = parse_sections(body)
    return {
        "date": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "frontmatter": frontmatter,
        "body_md": body,
        "sections": sections,
        "mood": _optional_scale(frontmatter.get("mood")),
        "energy": _optional_scale(frontmatter.get("energy")),
        "log_count": _log_count(sections.get("Log", "")),
        "has_reflections": bool(sections.get("Reflections", "").strip()),
    }


def parse_sections(markdown: str) -> dict[str, str]:
    matches = list(_H2_RE.finditer(markdown))
    sections: dict[str, str] = {}
    for idx, match in enumerate(matches):
        heading = match.group(1).strip()
        body_start = _line_end(markdown, match)
        body_end = matches[idx + 1].start() if idx + 1 < len(matches) else len(markdown)
        sections[heading] = markdown[body_start:body_end].strip("\n")
    return sections


def list_journal_days(*, limit: int = 30) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM journal_days
               WHERE deleted_at IS NULL
               ORDER BY date DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_journal_row(dict(row)) for row in rows]


def get_journal_day(day: str) -> dict[str, Any] | None:
    target = _day_path(_coerce_day(day))
    scan_journal_days([target])
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM journal_days WHERE date = ? AND deleted_at IS NULL",
            (day,),
        ).fetchone()
    return _journal_row(dict(row)) if row else None


def assemble_journal_day(day: str) -> dict[str, Any] | None:
    row = get_journal_day(day)
    if row is None:
        return None
    path = vault_dir() / row["path"]
    with host_file_lock(path):
        parsed = parse_journal_file(path)
    return {
        **row,
        "frontmatter": parsed["frontmatter"],
        "body_md": parsed["body_md"],
        "sections": parsed["sections"],
        "tasks": _tasks_for_day(day),
        "routine_completions": _routine_completions_for_day(day),
        "fired_reminders": _fired_reminders_for_day(day),
    }


def skeleton() -> str:
    return "## Tasks\n\n## Log\n\n## Reflections\n"


def _ensure_day_locked(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        atomic_write(path, skeleton())
        return
    frontmatter, body = _split_frontmatter(path.read_text(encoding="utf-8"))
    updated = _ensure_required_sections(body)
    if updated != body:
        atomic_write(path, _dump_day(frontmatter, updated))


def _ensure_required_sections(markdown: str) -> str:
    updated = markdown
    for heading in _REQUIRED_SECTIONS:
        if _has_section(updated, heading):
            continue
        if updated and not updated.endswith("\n"):
            updated += "\n"
        if updated and not updated.endswith("\n\n"):
            updated += "\n"
        updated += f"## {heading}\n"
    return updated


def _replace_section(markdown: str, heading: str, text: str) -> str:
    if not _has_section(markdown, heading):
        markdown = _ensure_section(markdown, heading)
    match = _section_match(markdown, heading)
    if match is None:
        raise RuntimeError(f"missing journal section after ensure: {heading}")
    body_start = _line_end(markdown, match)
    next_heading = _next_heading_match(markdown, body_start)
    body_end = body_start + next_heading.start() if next_heading else len(markdown)
    replacement = f"{text.rstrip()}\n" if text.strip() else ""
    return f"{markdown[:body_start]}{replacement}{markdown[body_end:]}"


def _ensure_section(markdown: str, heading: str) -> str:
    updated = markdown
    if updated and not updated.endswith("\n"):
        updated += "\n"
    if updated and not updated.endswith("\n\n"):
        updated += "\n"
    return f"{updated}## {heading}\n"


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise JournalFrontmatterError(
            "journal frontmatter is malformed; fix it before mutating"
        )
    raw_frontmatter = parts[1]
    body = parts[2].lstrip("\n")
    try:
        parsed = yaml.safe_load(raw_frontmatter) or {}
    except yaml.YAMLError as exc:
        raise JournalFrontmatterError(
            "journal frontmatter is invalid YAML; fix it before mutating"
        ) from exc
    if not isinstance(parsed, dict):
        raise JournalFrontmatterError(
            "journal frontmatter must be a mapping; fix it before mutating"
        )
    return parsed, body


def _dump_day(frontmatter: dict[str, Any], body: str) -> str:
    clean = {
        key: value
        for key, value in frontmatter.items()
        if value is not None or key not in _OWNED_FRONTMATTER_KEYS
    }
    body_text = body.lstrip("\n")
    if not clean:
        return body_text
    fm_yaml = yaml.safe_dump(
        clean,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{fm_yaml}\n---\n\n{body_text}"


def _tasks_for_day(day: str) -> list[dict[str, Any]]:
    host_path = f"journal/{day}.md"
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE deleted_at IS NULL
                 AND (
                   host_path = ?
                   OR due = ?
                   OR due LIKE ?
                   OR scheduled = ?
                   OR scheduled LIKE ?
                 )
               ORDER BY status, due IS NULL, due ASC, updated_at DESC""",
            (host_path, day, f"{day}T%", day, f"{day}T%"),
        ).fetchall()
    return [_task_row(dict(row)) for row in rows]


def _routine_completions_for_day(day: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT rc.routine_id, rc.date, rc.created_at, r.name, r.time_of_day
               FROM routine_completions rc
               JOIN routines r ON r.slug = rc.routine_id
               WHERE rc.date = ?
                 AND r.deleted_at IS NULL
               ORDER BY r.time_of_day, r.name""",
            (day,),
        ).fetchall()
    return [dict(row) for row in rows]


def _fired_reminders_for_day(day: str) -> list[dict[str, Any]]:
    start_utc, end_utc = _local_day_utc_bounds(day)
    with connect() as conn:
        rows = conn.execute(
            """SELECT *
               FROM reminders
               WHERE deleted_at IS NULL
                 AND status IN ('sent', 'late', 'notify_failed')
                 AND COALESCE(fired_at, fire_at) >= ?
                 AND COALESCE(fired_at, fire_at) < ?
               ORDER BY COALESCE(fired_at, fire_at) DESC, id DESC""",
            (start_utc, end_utc),
        ).fetchall()
    return [dict(row) for row in rows]


def _bump_projects_for_journal_day(day: str) -> None:
    host_path = f"journal/{day}.md"
    with connect() as conn:
        rows = conn.execute(
            """SELECT DISTINCT project
               FROM tasks
               WHERE host_path = ?
                 AND project IS NOT NULL
                 AND deleted_at IS NULL""",
            (host_path,),
        ).fetchall()
        for row in rows:
            conn.execute(
                """UPDATE projects
                   SET last_activity_at = CURRENT_TIMESTAMP,
                       updated_at = CURRENT_TIMESTAMP
                   WHERE slug = ? AND deleted_at IS NULL""",
                (row["project"],),
            )
            conn.execute(
                "DELETE FROM slipping WHERE entity_type = 'project' AND entity_id = ?",
                (row["project"],),
            )


def _task_row(row: dict[str, Any]) -> dict[str, Any]:
    import json

    row["checked"] = bool(row.get("checked"))
    row["needs_triage"] = bool(row.get("needs_triage"))
    row["no_reminder"] = bool(row.get("no_reminder"))
    row["recurrence_unparsed"] = bool(row.get("recurrence_unparsed"))
    row["tags"] = json.loads(row.pop("tags_json") or "[]")
    row["links"] = json.loads(row.pop("links_json") or "[]")
    return row


def _journal_paths() -> list[Path]:
    directory = journal_dir()
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.md") if p.is_file())


def _absolute_path(path: Path) -> Path:
    return path if path.is_absolute() else vault_dir() / path


def _relative_path(path: Path) -> str | None:
    try:
        return str(path.relative_to(vault_dir()))
    except ValueError:
        return None


def _day_path(day: date) -> Path:
    return journal_dir() / f"{day.isoformat()}.md"


def _coerce_day(value: str | date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not _DAY_RE.match(value):
        raise ValueError("date must be YYYY-MM-DD")
    return date.fromisoformat(value)


def _is_valid_day(value: str) -> bool:
    if not _DAY_RE.match(value):
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _coerce_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _local_day_utc_bounds(day: str) -> tuple[str, str]:
    local_day = date.fromisoformat(day)
    tz = ZoneInfo(get_settings().capture.default_timezone)
    start_local = datetime.combine(local_day, time.min, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(UTC).isoformat(), end_local.astimezone(UTC).isoformat()


def _clean_one_line(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _neutralize_heading_lines(value: str) -> str:
    """Escape user heading markers so journal section parsing remains structural."""
    return _USER_HEADING_RE.sub(lambda match: "\\" + match.group(1), value)


def _source_suffix(source: str | None) -> str:
    cleaned = _clean_one_line(source or "")
    return f" [source: {cleaned}]" if cleaned else ""


def _apply_optional_scale(
    frontmatter: dict[str, Any],
    field: str,
    value: int | None | object,
) -> None:
    if value is _UNSET:
        return
    if value is None:
        frontmatter.pop(field, None)
        return
    frontmatter[field] = _clean_scale(value, field)


def _clean_scale(value: int, field: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 5:
        raise ValueError(f"{field} must be between 1 and 5")
    return parsed


def _optional_scale(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if 1 <= parsed <= 5 else None


def _log_count(log_body: str) -> int:
    return sum(1 for line in log_body.splitlines() if _LOG_BULLET_RE.match(line))


def _section_match(markdown: str, heading: str) -> re.Match[str] | None:
    pattern = re.compile(rf"(?im)^##\s+{re.escape(heading)}\s*$")
    return pattern.search(markdown)


def _has_section(markdown: str, heading: str) -> bool:
    return _section_match(markdown, heading) is not None


def _next_heading_match(markdown: str, start: int) -> re.Match[str] | None:
    return re.search(r"(?m)^##\s+", markdown[start:])


def _line_end(markdown: str, match: re.Match[str]) -> int:
    newline = markdown.find("\n", match.end())
    return len(markdown) if newline == -1 else newline + 1


def _soft_delete_disappeared(conn, seen: set[str]) -> None:
    if seen:
        placeholders = ",".join("?" for _ in seen)
        conn.execute(
            f"""UPDATE journal_days SET deleted_at = CURRENT_TIMESTAMP
                WHERE deleted_at IS NULL AND date NOT IN ({placeholders})""",
            tuple(seen),
        )
    else:
        conn.execute("UPDATE journal_days SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL")


def _journal_row(row: dict[str, Any]) -> dict[str, Any]:
    row["has_reflections"] = bool(row.get("has_reflections"))
    return row
