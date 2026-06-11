"""People mirror scanner and file-first CRM mutations."""
from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from slugify import slugify

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.paths import people_dir, vault_dir
from mastisk.projects.sync import split_frontmatter
from mastisk.routes.notes import atomic_write
from mastisk.settings import get_settings

_CREATE_PERSON_LOCK = threading.Lock()
_INTERACTIONS_HEADING_RE = re.compile(r"^##\s+Interactions\s*$", re.I)
_NEXT_HEADING_RE = re.compile(r"^##\s+")
_INTERACTION_RE = re.compile(
    r"^\s*-\s+(?P<ts>\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+(?P<text>.+?)\s*$"
)
_PARTIAL_DATE_RE = re.compile(r"^\d{2}-\d{2}$")
_FULL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def scan_people(paths: list[Path] | None = None) -> dict[str, int]:
    person_paths = paths if paths is not None else _person_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for raw_path in person_paths:
            path = _absolute_path(raw_path)
            if not path.exists() or path.name.startswith("."):
                if paths is not None and path.suffix == ".md":
                    _soft_delete_missing_path(conn, path)
                continue
            with host_file_lock(path):
                person = parse_person_file(path)
            slug = person["slug"]
            seen.add(slug)
            deleted_at_sql = "CURRENT_TIMESTAMP" if person.get("archived") else "NULL"
            conn.execute(
                f"""INSERT INTO people
                   (slug, name, birthday, anniversary, facts_json, follow_up_at, path,
                    deleted_at, last_interaction_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, {deleted_at_sql}, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                     name=excluded.name,
                     birthday=excluded.birthday,
                     anniversary=excluded.anniversary,
                     facts_json=excluded.facts_json,
                     follow_up_at=excluded.follow_up_at,
                     path=excluded.path,
                     deleted_at={deleted_at_sql},
                     last_interaction_at=excluded.last_interaction_at,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    slug,
                    person["name"],
                    person.get("birthday"),
                    person.get("anniversary"),
                    json.dumps(person.get("facts") or {}, ensure_ascii=False, sort_keys=True),
                    person.get("follow_up_at"),
                    person["path"],
                    _last_interaction_at(person["interactions"]),
                ),
            )
            conn.execute("DELETE FROM interactions WHERE person_slug = ?", (slug,))
            for interaction in person["interactions"]:
                conn.execute(
                    """INSERT OR IGNORE INTO interactions
                       (person_slug, ts, text)
                       VALUES (?, ?, ?)""",
                    (slug, interaction["ts"], interaction["text"]),
                )
            upserted += 1
            _safe_reconcile_followup(slug)
        if paths is None:
            _soft_delete_disappeared(conn, seen)
    return {"upserted": upserted}


def parse_person_file(path: Path) -> dict[str, Any]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    name = str(meta.get("name") or path.stem).strip() or path.stem
    interactions, notes_body, interaction_lines = _split_interactions(body)
    facts = meta.get("facts") if isinstance(meta.get("facts"), dict) else {}
    return {
        "slug": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "name": name,
        "birthday": _clean_person_date(meta.get("birthday")),
        "anniversary": _clean_person_date(meta.get("anniversary")),
        "facts": facts,
        "follow_up_at": _clean_datetime(meta.get("follow_up_at")),
        "archived": bool(meta.get("archived", False)),
        "interactions": interactions,
        "interaction_lines": interaction_lines,
        "body": notes_body,
        "frontmatter": meta,
    }


def create_person_file(
    *,
    name: str,
    birthday: str | None = None,
    anniversary: str | None = None,
    facts: dict[str, Any] | None = None,
    body: str | None = None,
    follow_up_at: str | None = None,
    interaction_text: str | None = None,
    interaction_ts: str | None = None,
) -> dict[str, Any]:
    clean_name = name.strip()
    if not clean_name:
        raise ValueError("name must be non-blank")
    meta = {
        "name": clean_name,
        "birthday": _clean_person_date(birthday),
        "anniversary": _clean_person_date(anniversary),
        "facts": facts or {},
        "follow_up_at": _clean_datetime(follow_up_at),
        "archived": False,
    }
    interactions = []
    if interaction_text and interaction_text.strip():
        interactions.append(
            {
                "ts": _clean_interaction_ts(interaction_ts),
                "text": _clean_interaction_text(interaction_text),
            }
        )
    content = dump_person_file(meta, body or "", interactions)
    with _CREATE_PERSON_LOCK:
        path = _create_person_file_exclusive(clean_name, content)
    scan_people([path])
    person = person_payload(path.stem, include_archived=True)
    if person is None:
        raise RuntimeError(f"person mirror missing after write: {path.stem}")
    return person


def append_interaction(
    slug: str,
    text: str,
    *,
    ts: str | datetime | None = None,
) -> dict[str, Any] | None:
    person = get_person(slug, include_archived=True)
    if person is None:
        return None
    if person.get("deleted_at") is not None:
        return None
    clean_text = _clean_interaction_text(text)
    if not clean_text:
        raise ValueError("interaction text must be non-blank")
    path = vault_dir() / person["path"]
    with host_file_lock(path):
        parsed = parse_person_file(path)
        if parsed.get("archived"):
            return None
        interactions = [
            *parsed["interactions"],
            {"ts": _clean_interaction_ts(ts), "text": clean_text},
        ]
        interaction_lines = [
            *parsed.get("interaction_lines", []),
            _format_interaction_line(interactions[-1]),
        ]
        atomic_write(
            path,
            dump_person_file(
                parsed["frontmatter"],
                parsed["body"],
                interactions,
                interaction_lines=interaction_lines,
            ),
        )
    scan_people([path])
    return person_payload(slug)


def patch_person(slug: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    person = get_person(slug, include_archived=True)
    if person is None or person.get("deleted_at") is not None:
        return None
    path = vault_dir() / person["path"]
    with host_file_lock(path):
        parsed = parse_person_file(path)
        meta = dict(parsed["frontmatter"])
        if "name" in updates:
            name = str(updates["name"] or "").strip()
            if not name:
                raise ValueError("name must be non-blank")
            meta["name"] = name
        for key in ("birthday", "anniversary"):
            if key in updates:
                meta[key] = _clean_person_date(updates[key])
        if "facts" in updates:
            next_facts = dict(parsed.get("facts") or {})
            patch = updates["facts"] if isinstance(updates["facts"], dict) else {}
            next_facts.update(patch)
            meta["facts"] = next_facts
        if "follow_up_at" in updates:
            meta["follow_up_at"] = _clean_datetime(updates["follow_up_at"])
        body = str(updates["body"]) if "body" in updates and updates["body"] is not None else parsed["body"]
        atomic_write(
            path,
            dump_person_file(
                meta,
                body,
                parsed["interactions"],
                interaction_lines=parsed.get("interaction_lines", []),
            ),
        )
    scan_people([path])
    return person_payload(slug)


def archive_person(slug: str) -> dict[str, Any] | None:
    person = get_person(slug, include_archived=True)
    if person is None:
        return None
    path = vault_dir() / person["path"]
    with host_file_lock(path):
        parsed = parse_person_file(path)
        meta = dict(parsed["frontmatter"])
        meta["archived"] = True
        atomic_write(
            path,
            dump_person_file(
                meta,
                parsed["body"],
                parsed["interactions"],
                interaction_lines=parsed.get("interaction_lines", []),
            ),
        )
    scan_people([path])
    _cancel_person_followup(slug, "person archived")
    return person_payload(slug, include_archived=True)


def get_person(slug: str, *, include_archived: bool = False) -> dict[str, Any] | None:
    clauses = ["slug = ?"]
    params: list[Any] = [slug]
    if not include_archived:
        clauses.append("deleted_at IS NULL")
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM people WHERE " + " AND ".join(clauses),
            tuple(params),
        ).fetchone()
    return _person_row(dict(row)) if row else None


def find_person(ref: str | None) -> dict[str, Any] | None:
    if not ref:
        return None
    ref = ref.strip()
    if not ref:
        return None
    by_slug = get_person(ref)
    if by_slug is not None:
        return by_slug
    slug_ref = slugify(ref)
    if slug_ref and slug_ref != ref:
        by_slug = get_person(slug_ref)
        if by_slug is not None:
            return by_slug
    normalized_ref = _normalize_person_name(ref)
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM people
               WHERE deleted_at IS NULL
               ORDER BY updated_at DESC"""
        ).fetchall()
    for row in rows:
        person = _person_row(dict(row))
        if _normalize_person_name(str(person.get("name") or "")) == normalized_ref:
            return person
    return None


def list_people(*, include_archived: bool = False) -> list[dict[str, Any]]:
    clauses = []
    if not include_archived:
        clauses.append("deleted_at IS NULL")
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect() as conn:
        rows = conn.execute(f"SELECT * FROM people{where} ORDER BY lower(name), name").fetchall()
    return [_person_row(dict(row)) for row in rows]


def person_payload(slug: str, *, include_archived: bool = False) -> dict[str, Any] | None:
    row = get_person(slug, include_archived=include_archived)
    if row is None:
        return None
    path = vault_dir() / row["path"]
    if not path.exists():
        _soft_delete_person(slug)
        return None
    parsed = parse_person_file(path)
    return {
        **row,
        "birthday": parsed.get("birthday"),
        "anniversary": parsed.get("anniversary"),
        "facts": parsed.get("facts") or {},
        "follow_up_at": parsed.get("follow_up_at"),
        "frontmatter": parsed["frontmatter"],
        "body": parsed["body"],
        "interactions": parsed["interactions"],
    }


def dump_person_file(
    meta: dict[str, Any],
    body: str,
    interactions: list[dict[str, str]],
    *,
    interaction_lines: list[str] | None = None,
) -> str:
    clean = {
        key: value
        for key, value in meta.items()
        if value is not None and not (key == "facts" and value == {})
    }
    frontmatter = yaml.safe_dump(
        clean,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    notes = body.strip()
    lines = [f"---\n{frontmatter}\n---"]
    if notes:
        lines.extend(["", notes])
    lines.extend(["", "## Interactions"])
    if interaction_lines is None:
        interaction_lines = [
            _format_interaction_line(interaction)
            for interaction in sorted(interactions, key=lambda item: item["ts"])
        ]
    lines.extend(interaction_lines)
    return "\n".join(lines).rstrip() + "\n"


def _person_paths() -> list[Path]:
    directory = people_dir()
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def _absolute_path(path: Path) -> Path:
    return path if path.is_absolute() else vault_dir() / path


def _create_person_file_exclusive(name: str, content: str) -> Path:
    base = slugify(name)[:80] or "person"
    people_dir().mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 100):
        slug = base if attempt == 1 else f"{base}-{attempt}"
        path = people_dir() / f"{slug}.md"
        if path.exists():
            continue
        with connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM people WHERE slug = ? AND deleted_at IS NULL",
                (slug,),
            ).fetchone()
        if existing is not None:
            continue
        try:
            with host_file_lock(path), path.open("x", encoding="utf-8") as handle:
                handle.write(content)
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"unable to allocate person slug for {name!r}")


def _split_interactions(body: str) -> tuple[list[dict[str, str]], str, list[str]]:
    lines = body.splitlines()
    interactions: list[dict[str, str]] = []
    interaction_lines: list[str] = []
    notes: list[str] = []
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        if not _INTERACTIONS_HEADING_RE.match(line):
            notes.append(line)
            idx += 1
            continue
        idx += 1
        while idx < len(lines):
            current = lines[idx]
            if _NEXT_HEADING_RE.match(current):
                break
            interaction_lines.append(current)
            match = _INTERACTION_RE.match(current)
            if match:
                interactions.append(
                    {
                        "ts": match.group("ts"),
                        "text": match.group("text").strip(),
                    }
                )
            idx += 1
        continue
    return (
        sorted(interactions, key=lambda item: item["ts"]),
        "\n".join(notes).strip(),
        interaction_lines,
    )


def _format_interaction_line(interaction: dict[str, str]) -> str:
    return f"- {interaction['ts']} {interaction['text']}"


def _clean_person_date(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if _PARTIAL_DATE_RE.match(text):
        return text
    if _FULL_DATE_RE.match(text):
        return text
    return None


def _normalize_person_name(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip().casefold()


def _clean_datetime(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = str(value).strip()
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _clean_interaction_ts(value: str | datetime | None) -> str:
    if isinstance(value, datetime):
        current = value
    elif isinstance(value, str) and value.strip():
        text = value.strip()
        if _INTERACTION_RE.match(f"- {text} x"):
            return text[:16]
        try:
            current = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            current = _now_in_capture_timezone()
    else:
        current = _now_in_capture_timezone()
    if current.tzinfo is not None:
        current = current.astimezone(ZoneInfo(get_settings().capture.default_timezone))
    return current.strftime("%Y-%m-%d %H:%M")


def _clean_interaction_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _now_in_capture_timezone() -> datetime:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    return datetime.now(tz)


def _last_interaction_at(interactions: list[dict[str, str]]) -> str | None:
    if not interactions:
        return None
    return max(interaction["ts"] for interaction in interactions)


def _person_row(row: dict[str, Any]) -> dict[str, Any]:
    row["facts"] = json.loads(row.pop("facts_json") or "{}")
    return row


def _soft_delete_missing_path(conn, path: Path) -> None:
    try:
        rel = str(path.relative_to(vault_dir()))
    except ValueError:
        return
    conn.execute(
        "UPDATE people SET deleted_at = CURRENT_TIMESTAMP WHERE path = ? AND deleted_at IS NULL",
        (rel,),
    )


def _soft_delete_disappeared(conn, seen: set[str]) -> None:
    if seen:
        placeholders = ",".join("?" for _ in seen)
        conn.execute(
            f"""UPDATE people SET deleted_at = CURRENT_TIMESTAMP
                WHERE deleted_at IS NULL AND slug NOT IN ({placeholders})""",
            tuple(seen),
        )
    else:
        conn.execute("UPDATE people SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL")


def _soft_delete_person(slug: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE people SET deleted_at = CURRENT_TIMESTAMP WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        )


def _safe_reconcile_followup(slug: str) -> None:
    try:
        from mastisk.agents.reminder_engine import reconcile_person_followup_reminder

        reconcile_person_followup_reminder(slug)
    except Exception:
        return


def _cancel_person_followup(slug: str, reason: str) -> None:
    with connect() as conn:
        conn.execute(
            """UPDATE reminders
               SET status = 'cancelled',
                   next_attempt_at = NULL,
                   last_error = ?
               WHERE entity_type = 'person'
                 AND entity_id = ?
                 AND kind = 'followup'
                 AND status = 'pending'
                 AND deleted_at IS NULL""",
            (reason, f"followup:{slug}"),
        )
