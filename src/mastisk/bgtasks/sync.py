"""Automation file scanner and file-first mutations.

An automation is a folder: ``vault/_automations/<slug>/`` holding

- ``task.yaml`` — the spec, canonical and user-editable in any editor:
  ``name``, ``instructions`` (natural-language prose the runner re-reads on
  every run), ``active``, ``triggers`` ({cron?: "m h dom mon dow",
  windows?: [{start: "HH:MM", end: "HH:MM"}]}), optional ``model``,
  ``created_at``, plus runtime-managed ``last_*`` fields the runner writes
  back (mirroring Rowboat's flat-field pattern: ``last_attempt_at`` bumps at
  every run start and anchors failure backoff; ``last_run_at``/``_summary``
  only on success so the last good run stays visible; ``last_run_error``
  only on failure, cleared by the next success).
- ``index.md`` — the automation-owned artifact the user reads.

The DB row (``bg_tasks``) is a derived index rebuilt by ``scan_bg_tasks``,
same file-first contract as people/projects/routines.
"""
from __future__ import annotations

import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from slugify import slugify

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.paths import automations_dir, vault_dir
from mastisk.routes.notes import atomic_write

_CREATE_LOCK = threading.Lock()

# User-editable spec fields; everything else in task.yaml is runtime-managed.
_SPEC_FIELDS = ("name", "instructions", "active", "triggers", "model")
_RUNTIME_FIELDS = (
    "created_at", "last_attempt_at", "last_run_at", "last_run_summary", "last_run_error",
)

_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def scan_bg_tasks(paths: list[Path] | None = None) -> dict[str, int]:
    """Mirror task.yaml files into the bg_tasks table.

    ``paths=None`` reconciles the whole directory (soft-deleting rows whose
    folder vanished); a list of task.yaml paths re-scans just those.
    """
    scanned = errors = 0
    if paths is None:
        target_paths = sorted(automations_dir().glob("*/task.yaml"))
    else:
        target_paths = [_absolute(p) for p in paths]

    seen_slugs: list[str] = []
    with connect() as conn:
        for path in target_paths:
            try:
                with host_file_lock(path):
                    parsed = parse_task_file(path)
            except (OSError, ValueError, yaml.YAMLError):
                errors += 1
                continue
            seen_slugs.append(parsed["slug"])
            conn.execute(
                """INSERT INTO bg_tasks
                     (slug, name, instructions, active, triggers_json, model, path,
                      created_at, last_attempt_at, last_run_at, last_run_summary,
                      last_run_error, deleted_at, updated_at)
                   VALUES (:slug, :name, :instructions, :active, :triggers_json, :model,
                           :path, :created_at, :last_attempt_at, :last_run_at,
                           :last_run_summary, :last_run_error, NULL, CURRENT_TIMESTAMP)
                   ON CONFLICT(slug) DO UPDATE SET
                     name=excluded.name, instructions=excluded.instructions,
                     active=excluded.active, triggers_json=excluded.triggers_json,
                     model=excluded.model, path=excluded.path,
                     created_at=excluded.created_at,
                     last_attempt_at=excluded.last_attempt_at,
                     last_run_at=excluded.last_run_at,
                     last_run_summary=excluded.last_run_summary,
                     last_run_error=excluded.last_run_error,
                     deleted_at=NULL, updated_at=CURRENT_TIMESTAMP""",
                parsed | {"triggers_json": _dump_json(parsed["triggers"])},
            )
            scanned += 1
        if paths is None:
            if seen_slugs:
                placeholders = ",".join("?" for _ in seen_slugs)
                conn.execute(
                    f"""UPDATE bg_tasks SET deleted_at = CURRENT_TIMESTAMP
                        WHERE deleted_at IS NULL AND slug NOT IN ({placeholders})""",
                    seen_slugs,
                )
            else:
                conn.execute(
                    "UPDATE bg_tasks SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL",
                )
    return {"scanned": scanned, "errors": errors}


def parse_task_file(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: task.yaml must be a mapping")
    name = str(raw.get("name") or path.parent.name).strip()
    instructions = str(raw.get("instructions") or "").strip()
    if not instructions:
        raise ValueError(f"{path}: instructions must be non-empty")
    return {
        "slug": path.parent.name,
        "path": str(path.relative_to(vault_dir())),
        "name": name,
        "instructions": instructions,
        "active": 1 if raw.get("active", True) else 0,
        "triggers": validate_triggers(raw.get("triggers")),
        "model": str(raw["model"]).strip() if raw.get("model") else None,
        "created_at": str(raw.get("created_at") or "") or None,
        "last_attempt_at": str(raw.get("last_attempt_at") or "") or None,
        "last_run_at": str(raw.get("last_run_at") or "") or None,
        "last_run_summary": str(raw.get("last_run_summary") or "") or None,
        "last_run_error": str(raw.get("last_run_error") or "") or None,
    }


def validate_triggers(raw: Any) -> dict[str, Any]:
    """Normalize {cron?, windows?}. Absent/empty = manual-only."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("triggers must be a mapping")
    out: dict[str, Any] = {}
    cron = raw.get("cron")
    if cron:
        cron = str(cron).strip()
        if len(cron.split()) != 5:
            raise ValueError(f"cron must be a 5-field expression, got {cron!r}")
        out["cron"] = cron
    windows = raw.get("windows")
    if windows:
        if not isinstance(windows, list):
            raise ValueError("triggers.windows must be a list")
        clean = []
        for w in windows:
            if not isinstance(w, dict):
                raise ValueError("each window must be a mapping")
            start, end = str(w.get("start") or ""), str(w.get("end") or "")
            if not (_HHMM_RE.match(start) and _HHMM_RE.match(end)):
                raise ValueError(f"window times must be HH:MM, got {w!r}")
            if end <= start:
                raise ValueError(f"window end must be after start, got {w!r}")
            clean.append({"start": start, "end": end})
        out["windows"] = clean
    return out


def create_bg_task(
    *,
    name: str,
    instructions: str,
    triggers: dict[str, Any] | None = None,
    model: str | None = None,
    active: bool = True,
) -> dict[str, Any]:
    clean_name = name.strip()
    clean_instructions = instructions.strip()
    if not clean_name:
        raise ValueError("name must be non-blank")
    if not clean_instructions:
        raise ValueError("instructions must be non-blank")
    triggers = validate_triggers(triggers)

    with _CREATE_LOCK:
        base = slugify(clean_name)[:60] or "automation"
        slug = base
        n = 2
        while (automations_dir() / slug).exists():
            slug = f"{base}-{n}"
            n += 1
        folder = automations_dir() / slug
        folder.mkdir(parents=True, exist_ok=True)
        spec = {
            "name": clean_name,
            "instructions": clean_instructions,
            "active": bool(active),
            "created_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        }
        if triggers:
            spec["triggers"] = triggers
        if model:
            spec["model"] = model.strip()
        path = folder / "task.yaml"
        atomic_write(path, yaml.safe_dump(spec, sort_keys=False, allow_unicode=True))
        index = folder / "index.md"
        if not index.exists():
            atomic_write(index, f"# {clean_name}\n\n_Nothing yet — first run pending._\n")
    scan_bg_tasks([path])
    return bg_task_payload(slug)


def patch_bg_task(slug: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    """Update user-editable spec fields, preserving runtime fields."""
    path = automations_dir() / slug / "task.yaml"
    if not path.exists():
        return None
    unknown = set(updates) - set(_SPEC_FIELDS)
    if unknown:
        raise ValueError(f"unknown fields: {sorted(unknown)}")
    with host_file_lock(path):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if "triggers" in updates:
            triggers = validate_triggers(updates["triggers"])
            if triggers:
                raw["triggers"] = triggers
            else:
                raw.pop("triggers", None)
        for field in ("name", "instructions", "model"):
            if field in updates:
                value = (str(updates[field]).strip() if updates[field] is not None else "")
                if field in ("name", "instructions") and not value:
                    raise ValueError(f"{field} must be non-blank")
                if value:
                    raw[field] = value
                else:
                    raw.pop(field, None)
        if "active" in updates:
            raw["active"] = bool(updates["active"])
        atomic_write(path, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    scan_bg_tasks([path])
    return bg_task_payload(slug)


def write_runtime_fields(slug: str, **fields: str | None) -> None:
    """Runner-only writeback of last_* fields into task.yaml."""
    unknown = set(fields) - set(_RUNTIME_FIELDS)
    if unknown:
        raise ValueError(f"not runtime fields: {sorted(unknown)}")
    path = automations_dir() / slug / "task.yaml"
    if not path.exists():
        return
    with host_file_lock(path):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for key, value in fields.items():
            if value is None:
                raw.pop(key, None)
            else:
                raw[key] = value
        atomic_write(path, yaml.safe_dump(raw, sort_keys=False, allow_unicode=True))
    scan_bg_tasks([path])


def read_index(slug: str) -> str:
    path = automations_dir() / slug / "index.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def write_index(slug: str, content: str) -> None:
    path = automations_dir() / slug / "index.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    with host_file_lock(path):
        atomic_write(path, content)


def bg_task_payload(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM bg_tasks WHERE slug = ? AND deleted_at IS NULL", (slug,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["active"] = bool(d["active"])
    d["triggers"] = _load_json(d.pop("triggers_json"))
    return d


def list_bg_tasks(*, include_deleted: bool = False) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM bg_tasks"
            + ("" if include_deleted else " WHERE deleted_at IS NULL")
            + " ORDER BY name COLLATE NOCASE",
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["active"] = bool(d["active"])
        d["triggers"] = _load_json(d.pop("triggers_json"))
        out.append(d)
    return out


def _dump_json(value: Any) -> str:
    import json
    return json.dumps(value or {})


def _load_json(raw: str | None) -> dict[str, Any]:
    import json
    try:
        parsed = json.loads(raw or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        return {}


def _absolute(path: Path) -> Path:
    return path if path.is_absolute() else vault_dir() / path
