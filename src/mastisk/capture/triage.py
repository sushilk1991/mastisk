"""Needs-triage queue helpers for captured personal-OS items."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

from mastisk.agents.reminder_engine import create_task_due_reminder
from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.journal import append_log, scan_journal_days
from mastisk.library.sync import create_quote_file
from mastisk.paths import journal_dir, projects_dir, vault_dir
from mastisk.projects.sync import append_project_log, find_project, get_project
from mastisk.routes.notes import atomic_write, persist_note_capture
from mastisk.routines.sync import RoutineArchivedError, complete_routine_completion
from mastisk.settings import get_settings
from mastisk.tasks.parser import parse_task_line
from mastisk.tasks.sync import (
    append_task_to_host,
    get_task,
    journal_host_for_today,
    scan_task_hosts,
)

CaptureTriageType = str
log = logging.getLogger("mastisk.capture.triage")

_TRIAGE_TAG_RE = re.compile(r"(?<!\S)#needs-triage\b")
_H2_RE = re.compile(r"^##\s+(.+?)\s*$")
_JOURNAL_LOG_PREFIX_RE = re.compile(r"^\s*-\s+\d{2}:\d{2}\s+")
_PROJECT_LOG_PREFIX_RE = re.compile(r"^\s*-\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+")
_NOTE_SHAPED_TARGETS = {"note", "inbox", "content"}


class TriageReclassifyError(ValueError):
    """Raised when a triage item cannot be filed as the requested target."""


@dataclass(frozen=True)
class _SourceFileSnapshot:
    kind: str
    path: Path
    text: str


def list_triage_items(limit: int = 100) -> list[dict[str, Any]]:
    items = [
        *_task_items(),
        *_typed_note_items(),
        *_journal_log_items(),
        *_project_log_items(),
    ]
    items.sort(key=lambda item: item.get("sort_key") or "", reverse=True)
    return [_public_item(item) for item in items[:limit]]


def get_triage_item(item_id: str) -> dict[str, Any] | None:
    for item in list_triage_items(limit=500):
        if item["id"] == item_id:
            return item
    return None


def reclassify_triage_item(item_id: str, target_type: CaptureTriageType) -> dict[str, Any] | None:
    item = get_triage_item(item_id)
    if item is None:
        return None
    if _should_file_as_target(item, target_type):
        # Idempotence: clear the source marker before additive filing, and
        # restore the source file if filing fails. This avoids retry-time
        # duplicates from the old file-then-clear ordering.
        snapshot = _source_file_snapshot(item)
        _clear_triage_marker(item, target_type=target_type)
        try:
            _file_as_target(item, target_type)
        except Exception:
            if snapshot is not None:
                _restore_source_file_snapshot(snapshot)
            raise
        _clear_needs_review_for_item(item)
    else:
        _clear_triage_marker(item, target_type=target_type)
        _clear_needs_review_for_item(item)
    return {
        "ok": True,
        "id": item_id,
        "type": target_type,
    }


def _is_note_in_place_reclassify(item: dict[str, Any], target_type: str) -> bool:
    return item.get("kind") == "note" and target_type in _NOTE_SHAPED_TARGETS


def _should_file_as_target(item: dict[str, Any], target_type: str) -> bool:
    if target_type == "dismiss":
        return False
    kind = item.get("kind")
    if target_type == "task" and kind == "task":
        return False
    if target_type == "journal" and kind == "journal":
        return False
    if target_type == "project_update" and kind == "project_update":
        return False
    return not _is_note_in_place_reclassify(item, target_type)


def _source_file_snapshot(item: dict[str, Any]) -> _SourceFileSnapshot | None:
    path = _source_path_for_item(item)
    if path is None:
        return None
    return _SourceFileSnapshot(
        kind=str(item.get("kind") or ""),
        path=path,
        text=path.read_text(encoding="utf-8"),
    )


def _source_path_for_item(item: dict[str, Any]) -> Path | None:
    kind = item.get("kind")
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    if kind == "task":
        task = get_task(str(source.get("uid") or ""))
        if task is None:
            return None
        return vault_dir() / task["host_path"]
    if kind in {"journal", "project_update", "note"}:
        return vault_dir() / str(source.get("path") or "")
    return None


def _clear_needs_review_for_item(item: dict[str, Any]) -> None:
    try:
        from mastisk.dashboard.intelligence import clear_needs_review
    except Exception:
        return
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    if item.get("kind") == "task" and source.get("uid"):
        clear_needs_review("task", str(source["uid"]), reason="triage_stale")
    elif item.get("kind") == "note" and source.get("note_id") is not None:
        clear_needs_review("note", str(source["note_id"]), reason="triage_stale")


def _restore_source_file_snapshot(snapshot: _SourceFileSnapshot) -> None:
    with host_file_lock(snapshot.path):
        atomic_write(snapshot.path, snapshot.text)
    if snapshot.kind == "task":
        scan_task_hosts([snapshot.path])
    elif snapshot.kind == "journal":
        scan_journal_days([snapshot.path])


def _task_items() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM tasks
               WHERE deleted_at IS NULL AND needs_triage = 1
               ORDER BY updated_at DESC"""
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        task = dict(row)
        items.append(
            {
                "id": f"task:{task['uid']}",
                "kind": "task",
                "detected_type": "task",
                "original_text": task["text"],
                "confidence": None,
                "capture": {
                    "type": "task",
                    "body": task["text"],
                    "due": task.get("due"),
                    "scheduled": task.get("scheduled"),
                    "priority": task.get("priority"),
                    "recurrence": task.get("recurrence"),
                    "domain": task.get("domain"),
                    "project": task.get("project"),
                },
                "source": {
                    "path": task["host_path"],
                    "uid": task["uid"],
                },
                "sort_key": task.get("updated_at") or task.get("created_at") or "",
            }
        )
    return items


def _typed_note_items() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, path, body, created_at
               FROM notes
               WHERE deleted_at IS NULL
               ORDER BY created_at DESC"""
        ).fetchall()
    items: list[dict[str, Any]] = []
    for row in rows:
        note = dict(row)
        path = vault_dir() / note["path"]
        try:
            meta, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError, ValueError):
            continue
        if meta.get("needs_triage") is not True:
            continue
        capture = meta.get("capture") if isinstance(meta.get("capture"), dict) else {}
        detected_type = str(capture.get("type") or "note")
        items.append(
            {
                "id": f"note:{note['id']}",
                "kind": "note",
                "detected_type": detected_type,
                "original_text": str(capture.get("body") or note["body"] or body).strip(),
                "confidence": _optional_float(capture.get("confidence")),
                "capture": capture,
                "source": {
                    "path": note["path"],
                    "note_id": note["id"],
                },
                "sort_key": note.get("created_at") or "",
            }
        )
    return items


def _journal_log_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not journal_dir().exists():
        return items
    for path in sorted(journal_dir().glob("*.md")):
        if not path.is_file() or path.name.startswith("."):
            continue
        for line_no, raw in _triage_section_lines(path, "Log"):
            original = _clean_triage_text(_JOURNAL_LOG_PREFIX_RE.sub("", raw))
            items.append(
                {
                    "id": f"journal:{path.stem}:{line_no}",
                    "kind": "journal",
                    "detected_type": "journal",
                    "original_text": original,
                    "confidence": None,
                    "capture": {
                        "type": "journal",
                        "body": original,
                    },
                    "source": {
                        "path": str(path.relative_to(vault_dir())),
                        "date": path.stem,
                        "line_number": line_no,
                    },
                    "sort_key": f"{path.stem}T{line_no:06d}",
                }
            )
    return items


def _project_log_items() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    if not projects_dir().exists():
        return items
    for path in sorted(projects_dir().glob("*.md")):
        if not path.is_file() or path.name.startswith("."):
            continue
        for line_no, raw in _triage_section_lines(path, "Log"):
            original = _clean_triage_text(_PROJECT_LOG_PREFIX_RE.sub("", raw))
            project = get_project(path.stem)
            items.append(
                {
                    "id": f"project:{path.stem}:{line_no}",
                    "kind": "project_update",
                    "detected_type": "project_update",
                    "original_text": original,
                    "confidence": None,
                    "capture": {
                        "type": "project_update",
                        "body": original,
                        "project": path.stem,
                        "domain": project.get("domain") if project else None,
                    },
                    "source": {
                        "path": str(path.relative_to(vault_dir())),
                        "project": path.stem,
                        "line_number": line_no,
                    },
                    "sort_key": project.get("updated_at") if project else path.stem,
                }
            )
    return items


def _triage_section_lines(path: Path, heading: str) -> list[tuple[int, str]]:
    lines = path.read_text(encoding="utf-8").splitlines()
    in_frontmatter = bool(lines and lines[0] == "---")
    current_heading: str | None = None
    found: list[tuple[int, str]] = []
    for idx, line in enumerate(lines, start=1):
        if idx == 1 and in_frontmatter:
            continue
        if in_frontmatter:
            if line == "---":
                in_frontmatter = False
            continue
        match = _H2_RE.match(line)
        if match:
            current_heading = match.group(1).strip().lower()
            continue
        if current_heading == heading.lower() and _TRIAGE_TAG_RE.search(line):
            found.append((idx, line))
    return found


def _file_as_target(item: dict[str, Any], target_type: str) -> None:
    text = str(item.get("original_text") or "").strip()
    if not text:
        return
    capture = item.get("capture") if isinstance(item.get("capture"), dict) else {}
    if target_type == "task":
        row = append_task_to_host(
            _task_host_for_item(item),
            text=text,
            due=_str_or_none(capture.get("due")),
            scheduled=_str_or_none(capture.get("scheduled")),
            recurrence=_str_or_none(capture.get("recurrence")),
            priority=_str_or_none(capture.get("priority")),
            tags=[str(tag) for tag in capture.get("tags", [])] if isinstance(capture.get("tags"), list) else None,
            links=[str(link) for link in capture.get("related", [])] if isinstance(capture.get("related"), list) else None,
            reminder_lead_minutes=_optional_int(capture.get("reminder_lead_minutes")),
            no_reminder=bool(capture.get("no_reminder") or False),
            review_at=_str_or_none(capture.get("review_at")),
        )
        try:
            create_task_due_reminder(
                task_uid=row["uid"],
                task_text=row["text"],
                due=_str_or_none(capture.get("due")),
                lead_minutes=_optional_int(capture.get("reminder_lead_minutes")),
                no_reminder=bool(capture.get("no_reminder") or False),
            )
        except Exception:
            log.exception("capture triage task reminder creation failed for task %s", row["uid"])
        return
    if target_type == "journal":
        day = _journal_day_for_item(item)
        append_log(day, text, _now_in_capture_timezone(), source="triage")
        return
    if target_type == "project_update":
        project_ref = _str_or_none(capture.get("project")) or item.get("source", {}).get("project")
        project = find_project(project_ref)
        if project is None:
            persist_note_capture(
                body=text,
                source="pwa",
                file_content=_typed_note_content({**capture, "type": target_type, "body": text}, needs_triage=False),
            )
            return
        append_project_log(project["slug"], text)
        return
    if target_type == "routine_done":
        routine = _str_or_none(capture.get("routine"))
        if not routine:
            raise TriageReclassifyError("routine_done requires a routine candidate")
        try:
            updated = complete_routine_completion(routine)
        except RoutineArchivedError as exc:
            raise TriageReclassifyError(str(exc)) from exc
        if updated is None:
            raise TriageReclassifyError(f"routine not found: {routine}")
        return
    if target_type == "person":
        from mastisk.people.sync import append_interaction, create_person_file, find_person

        person = find_person(_str_or_none(capture.get("person"))) or find_person(
            _str_or_none(capture.get("title"))
        )
        if person is not None:
            append_interaction(person["slug"], text)
            return
        name = _str_or_none(capture.get("title")) or _str_or_none(capture.get("person"))
        if not name:
            raise TriageReclassifyError("person target requires a person name")
        create_person_file(name=name, interaction_text=text)
        return
    if target_type == "quote":
        tags = (
            [str(tag) for tag in capture.get("tags", [])]
            if isinstance(capture.get("tags"), list)
            else None
        )
        create_quote_file(
            text=text,
            source_type="conversation",
            source_ref=_str_or_none(capture.get("title")),
            tags=tags,
        )
        return
    if target_type == "inventory":
        from mastisk.inventory.sync import create_inventory_file

        name = _str_or_none(capture.get("title")) or text
        notes = text if text.casefold() != name.casefold() else None
        create_inventory_file(name=name, notes=notes)
        return
    if target_type in {"note", "inbox"}:
        persist_note_capture(body=text, source="pwa")
        return
    persist_note_capture(
        body=text,
        source="pwa",
        file_content=_typed_note_content({**capture, "type": target_type, "body": text}, needs_triage=False),
    )


def _task_host_for_item(item: dict[str, Any]) -> Path:
    capture = item.get("capture") if isinstance(item.get("capture"), dict) else {}
    project = find_project(_str_or_none(capture.get("project")))
    if project is not None:
        return vault_dir() / project["path"]
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    source_path = str(source.get("path") or "")
    if source_path.startswith("journal/"):
        return vault_dir() / source_path
    return journal_host_for_today()


def _journal_day_for_item(item: dict[str, Any]) -> str:
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    date_value = source.get("date")
    if isinstance(date_value, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", date_value):
        return date_value
    return _now_in_capture_timezone().date().isoformat()


def _clear_triage_marker(item: dict[str, Any], *, target_type: str) -> None:
    kind = item["kind"]
    source = item.get("source") if isinstance(item.get("source"), dict) else {}
    if kind == "task":
        uid = str(source["uid"])
        task = get_task(uid)
        if task is None:
            return
        path = vault_dir() / task["host_path"]
        if target_type not in {"task", "dismiss"}:
            _rewrite_line_containing(path, uid, _task_line_to_plain_bullet)
            scan_task_hosts([path])
            return
        _rewrite_line_containing(path, uid, _remove_triage_tag)
        scan_task_hosts([path])
        return
    if kind in {"journal", "project_update"}:
        path = vault_dir() / str(source["path"])
        line_number = int(source["line_number"])
        _rewrite_triage_line(
            path,
            line_number,
            original_text=str(item.get("original_text") or ""),
            kind=kind,
        )
        if kind == "journal":
            scan_journal_days([path])
        return
    if kind == "note":
        path = vault_dir() / str(source["path"])
        _clear_note_frontmatter(path, target_type=target_type)


def _rewrite_line_containing(path: Path, needle: str, transform) -> None:
    with host_file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        updated: list[str] = []
        changed = False
        for raw in lines:
            line, ending = _split_line_ending(raw)
            if needle in line and not changed:
                updated.append(transform(line) + ending)
                changed = True
            else:
                updated.append(raw)
        if changed:
            atomic_write(path, "".join(updated))


def _rewrite_line_number(path: Path, line_number: int, transform) -> bool:
    with host_file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        if line_number < 1 or line_number > len(lines):
            return False
        line, ending = _split_line_ending(lines[line_number - 1])
        if not _TRIAGE_TAG_RE.search(line):
            return False
        lines[line_number - 1] = transform(line) + ending
        atomic_write(path, "".join(lines))
    return True


def _rewrite_triage_line(path: Path, line_number: int, *, original_text: str, kind: str) -> None:
    if _rewrite_line_number(path, line_number, _remove_triage_tag):
        return
    prefix_re = _JOURNAL_LOG_PREFIX_RE if kind == "journal" else _PROJECT_LOG_PREFIX_RE
    with host_file_lock(path):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        updated: list[str] = []
        changed = False
        for raw in lines:
            line, ending = _split_line_ending(raw)
            clean = _clean_triage_text(prefix_re.sub("", line))
            if not changed and _TRIAGE_TAG_RE.search(line) and clean == original_text:
                updated.append(_remove_triage_tag(line) + ending)
                changed = True
            else:
                updated.append(raw)
        if changed:
            atomic_write(path, "".join(updated))


def _clear_note_frontmatter(path: Path, *, target_type: str) -> None:
    with host_file_lock(path):
        text = path.read_text(encoding="utf-8")
        meta, body = _split_frontmatter(text)
        meta["needs_triage"] = False
        capture = meta.get("capture")
        if isinstance(capture, dict) and target_type != "dismiss":
            capture["type"] = target_type
            meta["capture"] = capture
        atomic_write(path, _dump_frontmatter(meta, body))


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    parts = text.split("---\n", 2)
    if len(parts) < 3:
        raise ValueError("frontmatter is malformed")
    meta = yaml.safe_load(parts[1]) or {}
    if not isinstance(meta, dict):
        raise ValueError("frontmatter must be a mapping")
    return meta, parts[2].lstrip("\n")


def _dump_frontmatter(meta: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(
        meta,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{frontmatter}\n---\n\n{body.lstrip()}"


def _typed_note_content(capture: dict[str, Any], *, needs_triage: bool) -> str:
    return _dump_frontmatter(
        {
            "capture": capture,
            "needs_triage": needs_triage,
        },
        str(capture.get("body") or ""),
    )


def _remove_triage_tag(line: str) -> str:
    return re.sub(r"\s{2,}", " ", _TRIAGE_TAG_RE.sub("", line)).rstrip()


def _task_line_to_plain_bullet(line: str) -> str:
    parsed = parse_task_line(line)
    text = str(parsed.get("text") if parsed else _clean_triage_text(line)).strip()
    indent_match = re.match(r"^(?P<indent>\s*)-\s+\[[ xX]\]\s*", line)
    indent = indent_match.group("indent") if indent_match else ""
    return f"{indent}- {text}".rstrip()


def _clean_triage_text(value: str) -> str:
    return re.sub(r"\s{2,}", " ", _TRIAGE_TAG_RE.sub("", value)).strip()


def _split_line_ending(raw: str) -> tuple[str, str]:
    if raw.endswith("\r\n"):
        return raw[:-2], "\r\n"
    if raw.endswith("\n"):
        return raw[:-1], "\n"
    return raw, ""


def _now_in_capture_timezone() -> datetime:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(tz)


def _optional_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _public_item(item: dict[str, Any]) -> dict[str, Any]:
    item.pop("sort_key", None)
    return item
