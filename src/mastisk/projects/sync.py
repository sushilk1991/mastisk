"""Project file scanner and file-first mutations."""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from slugify import slugify

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.markdown_sections import append_to_section
from mastisk.paths import checklist_templates_dir, projects_dir, vault_dir
from mastisk.routes.notes import atomic_write
from mastisk.settings import get_settings
from mastisk.tasks.parser import format_task_line, generate_uid, parse_task_line

ProjectType = Literal["project", "area", "retainer"]
ProjectStatus = Literal["active", "someday", "paused", "done"]
_VALID_TYPES = {"project", "area", "retainer"}
_VALID_STATUSES = {"active", "someday", "paused", "done"}
_MILESTONE_RE = re.compile(r"^\s*-\s+\[(?P<mark>[ xX])\]\s*(?P<text>.+?)\s*$")
_ACTIVITY_RE = re.compile(
    r"^\s*-\s+(?P<date>\d{4}-\d{2}-\d{2})\s+"
    r"(?P<hours>\d+(?:\.\d+)?)h\s+(?P<text>.+?)\s*$"
)
_UID_RE = re.compile(r"\s*🆔\s*[A-Za-z0-9_-]+")


class MilestoneMissingError(RuntimeError):
    """Raised when a milestone position is not present in the project file."""


class ChecklistTemplateValidationError(ValueError):
    """Raised when a checklist template task cannot be safely copied."""

    def __init__(self, line_number: int, line: str, reason: str) -> None:
        super().__init__(
            f"invalid checklist template line {line_number}: {line.strip()} ({reason})"
        )


def scan_projects(paths: list[Path] | None = None) -> dict[str, int]:
    project_paths = paths if paths is not None else _project_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for path in project_paths:
            if not path.exists() or path.name.startswith("."):
                continue
            with host_file_lock(path):
                project = parse_project_file(path)
            slug = path.stem
            seen.add(slug)
            conn.execute(
                """INSERT INTO projects
                   (slug, path, name, type, domain, status, due, staleness_days, last_activity_at, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT last_activity_at FROM projects WHERE slug = ?), CURRENT_TIMESTAMP), NULL)
                   ON CONFLICT(slug) DO UPDATE SET
                     path=excluded.path,
                     name=excluded.name,
                     type=excluded.type,
                     domain=excluded.domain,
                     status=excluded.status,
                     due=excluded.due,
                     staleness_days=excluded.staleness_days,
                     deleted_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    slug,
                    str(path.relative_to(vault_dir())),
                    project["name"],
                    project["type"],
                    project.get("domain"),
                    project["status"],
                    project.get("due"),
                    project.get("staleness_days"),
                    slug,
                ),
            )
            _sync_milestones(conn, slug, project["milestones"])
            _sync_time_entries(conn, slug, project["time_entries"])
            upserted += 1
        if paths is None:
            if seen:
                placeholders = ",".join("?" for _ in seen)
                conn.execute(
                    f"""UPDATE projects SET deleted_at = CURRENT_TIMESTAMP
                        WHERE deleted_at IS NULL AND slug NOT IN ({placeholders})""",
                    tuple(seen),
                )
                conn.execute(
                    f"""UPDATE milestones SET deleted_at = CURRENT_TIMESTAMP
                        WHERE deleted_at IS NULL AND project_slug NOT IN ({placeholders})""",
                    tuple(seen),
                )
                conn.execute(
                    f"""UPDATE time_entries SET deleted_at = CURRENT_TIMESTAMP
                        WHERE deleted_at IS NULL AND project_slug NOT IN ({placeholders})""",
                    tuple(seen),
                )
            else:
                conn.execute(
                    "UPDATE projects SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL"
                )
                conn.execute(
                    "UPDATE milestones SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL"
                )
                conn.execute(
                    "UPDATE time_entries SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL"
                )
    return {"upserted": upserted}


def parse_project_file(path: Path) -> dict[str, Any]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    name = str(meta.get("name") or path.stem).strip() or path.stem
    project_type = str(meta.get("type") or "project").strip()
    status = str(meta.get("status") or "active").strip()
    if project_type not in _VALID_TYPES:
        project_type = "project"
    if status not in _VALID_STATUSES:
        status = "active"
    return {
        "slug": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "name": name,
        "type": project_type,
        "domain": meta.get("domain"),
        "status": status,
        "due": str(meta["due"]) if meta.get("due") else None,
        "staleness_days": _optional_positive_int(meta.get("staleness_days")),
        "recurring_items": _string_list(meta.get("recurring_items")),
        "rolled_months": _string_list(meta.get("rolled_months")),
        "milestones": _parse_milestones(body),
        "time_entries": _parse_time_entries(body),
        "body": body,
        "frontmatter": meta,
    }


def create_project_file(
    *,
    name: str,
    type: str = "project",
    domain: str | None = None,
    status: str = "active",
    due: str | None = None,
    template: str | None = None,
    recurring_items: list[str] | None = None,
) -> dict[str, Any]:
    project_type = type if type in _VALID_TYPES else "project"
    project_status = status if status in _VALID_STATUSES else "active"
    path = _next_project_path(name)
    meta = {
        "name": name.strip(),
        "type": project_type,
        "domain": domain,
        "status": project_status,
        "due": due,
    }
    if project_type == "retainer":
        meta["recurring_items"] = _clean_string_list(recurring_items or [])
    task_lines = _task_lines_from_template(template) if template else []
    with host_file_lock(path):
        atomic_write(path, dump_project_file(meta, _initial_body(task_lines)))
    scan_projects([path])
    if task_lines:
        from mastisk.tasks.sync import scan_task_hosts

        scan_task_hosts([path])
    return project_payload(path.stem) or parse_project_file(path)


def patch_project_frontmatter(slug: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    project = get_project(slug)
    if project is None:
        return None
    path = vault_dir() / project["path"]
    with host_file_lock(path):
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        for key in ("name", "type", "domain", "status", "due", "recurring_items"):
            if key in updates:
                meta[key] = updates[key]
        if meta.get("type") not in _VALID_TYPES:
            meta["type"] = "project"
        if meta.get("status") not in _VALID_STATUSES:
            meta["status"] = "active"
        if meta.get("type") == "retainer":
            meta["recurring_items"] = _clean_string_list(meta.get("recurring_items") or [])
        else:
            meta.pop("recurring_items", None)
        atomic_write(path, dump_project_file(meta, body))
    scan_projects([path])
    return project_payload(slug)


def append_project_log(slug: str, body: str, *, at: datetime | None = None) -> dict[str, Any] | None:
    project = get_project(slug)
    if project is None:
        return None
    path = vault_dir() / project["path"]
    timestamp = (at or datetime.now().astimezone()).strftime("%Y-%m-%d %H:%M")
    with host_file_lock(path):
        markdown = path.read_text(encoding="utf-8")
        atomic_write(path, append_to_section(markdown, "Log", f"- {timestamp} {body.strip()}"))
    with connect() as conn:
        conn.execute(
            "UPDATE projects SET last_activity_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE slug = ?",
            (slug,),
        )
        conn.execute(
            "DELETE FROM slipping WHERE entity_type = 'project' AND entity_id = ?",
            (slug,),
        )
    return project_payload(slug)


def append_project_milestone(slug: str, text: str) -> dict[str, Any] | None:
    project = get_project(slug)
    if project is None:
        return None
    path = vault_dir() / project["path"]
    if not path.exists():
        _soft_delete_project(slug)
        return None
    with host_file_lock(path):
        markdown = path.read_text(encoding="utf-8")
        line = f"- [ ] {_sanitize_line(text)}"
        atomic_write(path, append_to_section(markdown, "Milestones", line))
    scan_projects([path])
    return project_payload(slug)


def set_project_milestone_done(
    slug: str,
    position: int,
    *,
    done: bool,
) -> dict[str, Any] | None:
    project = get_project(slug)
    if project is None:
        return None
    path = vault_dir() / project["path"]
    if not path.exists():
        _soft_delete_project(slug)
        return None
    with host_file_lock(path):
        markdown = path.read_text(encoding="utf-8")
        atomic_write(path, _rewrite_milestone(markdown, position, done=done))
    scan_projects([path])
    return project_payload(slug)


def append_project_time(
    slug: str,
    *,
    hours: float,
    text: str,
    entry_date: str | None = None,
) -> dict[str, Any] | None:
    project = get_project(slug)
    if project is None:
        return None
    day = _clean_date(entry_date) if entry_date else _local_today()
    if day is None:
        raise ValueError("date must be YYYY-MM-DD")
    path = vault_dir() / project["path"]
    if not path.exists():
        _soft_delete_project(slug)
        return None
    with host_file_lock(path):
        markdown = path.read_text(encoding="utf-8")
        line = f"- {day} {hours:g}h {_sanitize_line(text)}"
        atomic_write(path, append_to_section(markdown, "Activity", line))
    scan_projects([path])
    return project_payload(slug)


def project_payload(slug: str) -> dict[str, Any] | None:
    row = get_project(slug)
    if row is None:
        return None
    path = vault_dir() / row["path"]
    if not path.exists():
        _soft_delete_project(slug)
        return None
    parsed = parse_project_file(path)
    milestones = parsed["milestones"]
    time_entries = _payload_time_entries(parsed["time_entries"])
    return {
        **row,
        "frontmatter": parsed["frontmatter"],
        "body": parsed["body"],
        "milestones": milestones,
        "milestone_progress": _milestone_progress(milestones),
        "time_entries": time_entries,
        "time_totals": _time_totals(time_entries),
        "retainer": _retainer_state(row),
    }


def get_project(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM projects WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        ).fetchone()
    return dict(row) if row else None


def find_project(ref: str | None) -> dict[str, Any] | None:
    if not ref:
        return None
    by_slug = get_project(ref)
    if by_slug is not None:
        return by_slug
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM projects
               WHERE lower(name) = lower(?) AND deleted_at IS NULL
               ORDER BY updated_at DESC
               LIMIT 1""",
            (ref,),
        ).fetchone()
    return dict(row) if row else None


def list_projects() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT p.*,
                      COALESCE(COUNT(t.uid), 0) AS open_task_count
               FROM projects p
               LEFT JOIN tasks t
                 ON t.project = p.slug
                AND t.status = 'open'
                AND t.deleted_at IS NULL
               WHERE p.deleted_at IS NULL
               GROUP BY p.slug
               ORDER BY p.status, p.updated_at DESC, p.name"""
        ).fetchall()
    return [dict(r) for r in rows]


def list_checklist_templates() -> list[dict[str, Any]]:
    directory = checklist_templates_dir()
    if not directory.exists():
        return []
    rows = []
    for path in sorted(directory.glob("*.md")):
        if not path.is_file() or path.name.startswith("."):
            continue
        rows.append({"name": path.stem, "task_count": len(_template_task_specs(path))})
    return rows


def split_frontmatter(markdown: str) -> tuple[dict[str, Any], str]:
    if not markdown.startswith("---\n"):
        return {}, markdown
    parts = markdown.split("---", 2)
    if len(parts) < 3:
        return {}, markdown
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        meta = {}
    body = parts[2].lstrip("\n")
    return meta, body


def dump_project_file(meta: dict[str, Any], body: str) -> str:
    clean = {k: v for k, v in meta.items() if v is not None}
    frontmatter = yaml.safe_dump(
        clean,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{body.lstrip()}"


def _sync_milestones(conn, slug: str, milestones: list[dict[str, Any]]) -> None:
    positions = [m["position"] for m in milestones]
    for milestone in milestones:
        conn.execute(
            """INSERT INTO milestones
               (project_slug, position, text, done, deleted_at)
               VALUES (?, ?, ?, ?, NULL)
               ON CONFLICT(project_slug, position) DO UPDATE SET
                 text=excluded.text,
                 done=excluded.done,
                 deleted_at=NULL,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                slug,
                milestone["position"],
                milestone["text"],
                1 if milestone["done"] else 0,
            ),
        )
    _soft_delete_missing_positions(conn, "milestones", slug, positions)


def _sync_time_entries(conn, slug: str, entries: list[dict[str, Any]]) -> None:
    positions = [entry["position"] for entry in entries]
    for entry in entries:
        conn.execute(
            """INSERT INTO time_entries
               (project_slug, position, date, hours, text, deleted_at)
               VALUES (?, ?, ?, ?, ?, NULL)
               ON CONFLICT(project_slug, position) DO UPDATE SET
                 date=excluded.date,
                 hours=excluded.hours,
                 text=excluded.text,
                 deleted_at=NULL,
                 updated_at=CURRENT_TIMESTAMP""",
            (
                slug,
                entry["position"],
                entry["date"],
                entry["hours"],
                entry["text"],
            ),
        )
    _soft_delete_missing_positions(conn, "time_entries", slug, positions)


def _soft_delete_missing_positions(
    conn,
    table: str,
    slug: str,
    positions: list[int],
) -> None:
    sql = f"UPDATE {table} SET deleted_at = CURRENT_TIMESTAMP WHERE project_slug = ? AND deleted_at IS NULL"
    params: list[Any] = [slug]
    if positions:
        placeholders = ",".join("?" for _ in positions)
        sql += f" AND position NOT IN ({placeholders})"
        params.extend(positions)
    conn.execute(sql, tuple(params))


def _soft_delete_project(slug: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE projects SET deleted_at = CURRENT_TIMESTAMP WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        )
        conn.execute(
            "UPDATE milestones SET deleted_at = CURRENT_TIMESTAMP WHERE project_slug = ? AND deleted_at IS NULL",
            (slug,),
        )
        conn.execute(
            "UPDATE time_entries SET deleted_at = CURRENT_TIMESTAMP WHERE project_slug = ? AND deleted_at IS NULL",
            (slug,),
        )


def _parse_milestones(body: str) -> list[dict[str, Any]]:
    milestones = []
    for line in _section_lines(body, "Milestones"):
        match = _MILESTONE_RE.match(line)
        if not match:
            continue
        milestones.append(
            {
                "position": len(milestones) + 1,
                "text": _UID_RE.sub("", match.group("text")).strip(),
                "done": match.group("mark").lower() == "x",
            }
        )
    return milestones


def _parse_time_entries(body: str) -> list[dict[str, Any]]:
    entries = []
    for line in _section_lines(body, "Activity"):
        match = _ACTIVITY_RE.match(line)
        if not match:
            continue
        parsed_date = _clean_date(match.group("date"))
        if parsed_date is None:
            continue
        try:
            hours = float(match.group("hours"))
        except ValueError:
            continue
        if hours <= 0:
            continue
        entries.append(
            {
                "position": len(entries) + 1,
                "date": parsed_date,
                "hours": hours,
                "text": match.group("text").strip(),
            }
        )
    return entries


def _section_lines(body: str, heading: str) -> list[str]:
    lines = body.splitlines()
    section_re = re.compile(rf"^##\s+{re.escape(heading)}\s*$", re.I)
    next_section_re = re.compile(r"^##\s+")
    for idx, line in enumerate(lines):
        if not section_re.match(line):
            continue
        end = len(lines)
        for cursor in range(idx + 1, len(lines)):
            if next_section_re.match(lines[cursor]):
                end = cursor
                break
        return lines[idx + 1:end]
    return []


def _rewrite_milestone(markdown: str, position: int, *, done: bool) -> str:
    if position < 1:
        raise MilestoneMissingError(f"milestone not found: {position}")
    lines = markdown.splitlines(keepends=True)
    active = False
    count = 0
    rewritten: list[str] = []
    for raw in lines:
        line, ending = _split_line_ending(raw)
        heading = re.match(r"^##\s+(?P<heading>.+?)\s*$", line)
        if heading:
            active = heading.group("heading").strip().lower() == "milestones"
            rewritten.append(raw)
            continue
        if active and _MILESTONE_RE.match(line):
            count += 1
            if count == position:
                mark = "x" if done else " "
                line = re.sub(r"\[[ xX]\]", f"[{mark}]", line, count=1)
                rewritten.append(line + ending)
                continue
        rewritten.append(raw)
    if count < position:
        raise MilestoneMissingError(f"milestone not found: {position}")
    return "".join(rewritten)


def _payload_time_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(entries, key=lambda entry: (entry["date"], entry["position"]), reverse=True)


def _milestone_progress(milestones: list[dict[str, Any]]) -> dict[str, int]:
    total = len(milestones)
    done = sum(1 for milestone in milestones if milestone["done"])
    percent = int(round((done / total) * 100)) if total else 0
    return {"done": done, "total": total, "percent": percent}


def _time_totals(entries: list[dict[str, Any]]) -> dict[str, float]:
    today = date.fromisoformat(_local_today())
    cutoff = today - timedelta(days=29)
    total = sum(float(entry["hours"]) for entry in entries)
    last_30 = sum(
        float(entry["hours"])
        for entry in entries
        if date.fromisoformat(entry["date"]) >= cutoff
    )
    return {
        "total_hours": round(total, 2),
        "last_30_days_hours": round(last_30, 2),
    }


def _retainer_state(project: dict[str, Any]) -> dict[str, Any] | None:
    if project.get("type") != "retainer":
        return None
    today = date.fromisoformat(_local_today())
    month_start = today.replace(day=1)
    month_end = _month_end(month_start)
    with connect() as conn:
        rows = conn.execute(
            """SELECT uid, text, status, due
               FROM tasks
               WHERE project = ?
                 AND deleted_at IS NULL
                 AND due IS NOT NULL
                 AND substr(due, 1, 10) >= ?
                 AND substr(due, 1, 10) <= ?
               ORDER BY status, line_number""",
            (project["slug"], month_start.isoformat(), month_end.isoformat()),
        ).fetchall()
    tasks = [dict(row) for row in rows]
    done = sum(1 for task in tasks if task["status"] == "done")
    return {
        "current_month": month_start.strftime("%Y-%m"),
        "month_end": month_end.isoformat(),
        "done": done,
        "open": len(tasks) - done,
        "total": len(tasks),
        "tasks": tasks,
    }


def _task_lines_from_template(name: str) -> list[str]:
    path = _checklist_template_path(name)
    if path is None or not path.exists():
        raise FileNotFoundError(f"checklist template not found: {name}")
    lines = []
    for line_number, raw_line, spec in _iter_template_task_specs(path):
        try:
            lines.append(
                format_task_line(
                    spec["text"],
                    due=spec.get("due"),
                    scheduled=spec.get("scheduled"),
                    recurrence=spec.get("recurrence"),
                    priority=spec.get("priority"),
                    tags=spec.get("tags", []),
                    links=spec.get("links", []),
                    uid=generate_uid(),
                )
            )
        except ValueError as exc:
            raise ChecklistTemplateValidationError(line_number, raw_line, str(exc)) from exc
    return lines


def _iter_template_task_specs(path: Path):
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        parsed = parse_task_line(line)
        if parsed is None or parsed["checked"]:
            continue
        yield line_number, line, parsed


def _template_task_specs(path: Path) -> list[dict[str, Any]]:
    return [
        spec
        for _line_number, _raw_line, spec in _iter_template_task_specs(path)
    ]


def _checklist_template_path(name: str) -> Path | None:
    if "/" in name or "\\" in name or name in {"", ".", ".."}:
        return None
    return checklist_templates_dir() / f"{name}.md"


def _initial_body(task_lines: list[str]) -> str:
    lines = ["## Log", "", "## Tasks"]
    lines.extend(task_lines)
    lines.extend(["", "## Milestones", "", "## Activity", ""])
    return "\n".join(lines)


def _optional_positive_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _project_paths() -> list[Path]:
    directory = projects_dir()
    if not directory.exists():
        return []
    return sorted(p for p in directory.glob("*.md") if p.is_file())


def _next_project_path(name: str) -> Path:
    base = slugify(name)[:80] or "project"
    projects_dir().mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 100):
        slug = base if attempt == 1 else f"{base}-{attempt}"
        path = projects_dir() / f"{slug}.md"
        if path.exists():
            continue
        with connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM projects WHERE slug = ? AND deleted_at IS NULL",
                (slug,),
            ).fetchone()
        if existing is None:
            return path
    raise RuntimeError(f"unable to allocate project slug for {name!r}")


def _clean_date(value: object) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        return None


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return _clean_string_list(value)


def _clean_string_list(values: list[object]) -> list[str]:
    return [cleaned for value in values if (cleaned := _sanitize_line(value))]


def _sanitize_line(value: object) -> str:
    return re.sub(r"\s+", " ", str(value)).strip()


def _local_today() -> str:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(tz).date().isoformat()


def _month_end(month_start: date) -> date:
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1)
    return next_month - timedelta(days=1)


def _split_line_ending(raw: str) -> tuple[str, str]:
    if raw.endswith("\r\n"):
        return raw[:-2], "\r\n"
    if raw.endswith("\n"):
        return raw[:-1], "\n"
    if raw.endswith("\r"):
        return raw[:-1], "\r"
    return raw, ""
