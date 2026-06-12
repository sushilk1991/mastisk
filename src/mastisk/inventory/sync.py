"""Inventory mirror scanner and file-first mutations."""
from __future__ import annotations

import math
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

import yaml
from slugify import slugify

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.paths import inventory_dir, vault_dir
from mastisk.projects.sync import split_frontmatter
from mastisk.routes.notes import atomic_write

_CREATE_INVENTORY_LOCK = threading.Lock()
_VALID_STATUSES = {"owned", "sold", "discarded"}


def scan_inventory(paths: list[Path] | None = None) -> dict[str, int]:
    item_paths = paths if paths is not None else _inventory_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for raw_path in item_paths:
            path = _absolute_path(raw_path)
            if not path.exists() or path.name.startswith("."):
                if paths is not None and path.suffix == ".md":
                    _soft_delete_missing_path(conn, path)
                continue
            with host_file_lock(path):
                item = parse_inventory_file(path)
            item_id = item["id"]
            seen.add(item_id)
            conn.execute(
                """INSERT INTO inventory
                   (id, path, name, acquired, value, status, location, photo, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(id) DO UPDATE SET
                     path=excluded.path,
                     name=excluded.name,
                     acquired=excluded.acquired,
                     value=excluded.value,
                     status=excluded.status,
                     location=excluded.location,
                     photo=excluded.photo,
                     deleted_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    item_id,
                    item["path"],
                    item["name"],
                    item.get("acquired"),
                    item.get("value"),
                    item["status"],
                    item.get("location"),
                    item.get("photo"),
                ),
            )
            upserted += 1
        if paths is None:
            _soft_delete_disappeared(conn, seen)
    return {"upserted": upserted}


def parse_inventory_file(path: Path) -> dict[str, Any]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    name = _clean_text(meta.get("name")) or path.stem.replace("-", " ").title()
    return {
        "id": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "name": name,
        "acquired": _clean_date(meta.get("acquired")),
        "value": _clean_value(meta.get("value")),
        "status": _clean_status(meta.get("status")),
        "location": _clean_text(meta.get("location")),
        "photo": _clean_text(meta.get("photo")),
        "body": body,
        "frontmatter": meta,
    }


def create_inventory_file(
    *,
    name: str,
    acquired: str | None = None,
    value: float | int | str | None = None,
    status: str = "owned",
    location: str | None = None,
    photo: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    clean_name = _clean_text(name)
    if not clean_name:
        raise ValueError("name must be non-blank")
    clean_acquired = _clean_patch_date(acquired, field="acquired") if acquired else date.today().isoformat()
    meta = {
        "name": clean_name,
        "acquired": clean_acquired,
        "value": _clean_value(value),
        "status": _clean_status(status),
        "location": _clean_text(location),
        "photo": _clean_text(photo),
    }
    content = dump_inventory_file(meta, notes or "")
    with _CREATE_INVENTORY_LOCK:
        path = _create_inventory_file_exclusive(clean_name, clean_acquired, content)
    scan_inventory([path])
    item = inventory_payload(path.stem)
    if item is None:
        raise RuntimeError(f"inventory mirror missing after write: {path.stem}")
    return item


def patch_inventory(item_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    item = inventory_payload(item_id)
    if item is None:
        return None
    path = vault_dir() / item["path"]
    with host_file_lock(path):
        parsed = parse_inventory_file(path)
        meta = dict(parsed["frontmatter"])
        if "name" in updates:
            clean_name = _clean_text(updates["name"])
            if not clean_name:
                raise ValueError("name must be non-blank")
            meta["name"] = clean_name
        if "status" in updates:
            meta["status"] = _clean_status(updates["status"])
        if "value" in updates:
            meta["value"] = _clean_value(updates["value"])
        if "location" in updates:
            meta["location"] = _clean_text(updates["location"])
        atomic_write(path, dump_inventory_file(meta, parsed["body"]))
    scan_inventory([path])
    return inventory_payload(item_id)


def list_inventory(
    *,
    status: str | None = None,
    location: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if status:
        clauses.append("status = ?")
        params.append(status)
    if location:
        clauses.append("location = ?")
        params.append(location)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM inventory
                WHERE {' AND '.join(clauses)}
                ORDER BY lower(name), name, acquired DESC, id""",
            tuple(params),
        ).fetchall()
    return [_inventory_row(dict(row)) for row in rows]


def inventory_payload(item_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM inventory WHERE id = ? AND deleted_at IS NULL",
            (item_id,),
        ).fetchone()
    if row is None:
        return None
    payload = _inventory_row(dict(row))
    path = vault_dir() / payload["path"]
    if not path.exists():
        _soft_delete_inventory(item_id)
        return None
    parsed = parse_inventory_file(path)
    return {
        **payload,
        "name": parsed["name"],
        "acquired": parsed.get("acquired"),
        "value": parsed.get("value"),
        "status": parsed["status"],
        "location": parsed.get("location"),
        "photo": parsed.get("photo"),
        "frontmatter": parsed["frontmatter"],
        "body": parsed["body"],
    }


def total_value(items: list[dict[str, Any]]) -> float:
    return round(sum(float(item["value"]) for item in items if item.get("value") is not None), 2)


def dump_inventory_file(meta: dict[str, Any], body: str) -> str:
    frontmatter = yaml.safe_dump(
        _clean_frontmatter(meta),
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    clean_body = body.strip()
    return f"---\n{frontmatter}\n---\n\n{clean_body}\n" if clean_body else f"---\n{frontmatter}\n---\n"


def _inventory_paths() -> list[Path]:
    directory = inventory_dir()
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def _absolute_path(path: Path) -> Path:
    return path if path.is_absolute() else vault_dir() / path


def _create_inventory_file_exclusive(name: str, acquired: str, content: str) -> Path:
    name_slug = slugify(name)[:60] or "item"
    base = f"{name_slug}-{acquired}"
    inventory_dir().mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 100):
        item_id = base if attempt == 1 else f"{base}-{attempt}"
        path = inventory_dir() / f"{item_id}.md"
        if path.exists():
            continue
        with connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM inventory WHERE id = ? AND deleted_at IS NULL",
                (item_id,),
            ).fetchone()
        if existing is not None:
            continue
        try:
            with host_file_lock(path), path.open("x", encoding="utf-8") as handle:
                handle.write(content)
            return path
        except FileExistsError:
            continue
    raise RuntimeError(f"unable to allocate inventory id for {name!r}")


def _clean_frontmatter(meta: dict[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in meta.items():
        if key == "photo":
            clean[key] = _clean_text(value)
        elif value is not None and value != "":
            clean[key] = value
    clean["name"] = _clean_text(clean.get("name")) or "Untitled item"
    clean["status"] = _clean_status(clean.get("status"))
    if clean.get("acquired") is not None:
        clean["acquired"] = _clean_date(clean.get("acquired"))
    if clean.get("value") is not None:
        clean["value"] = _clean_value(clean.get("value"))
    if clean.get("location") is not None:
        clean["location"] = _clean_text(clean.get("location"))
    clean.setdefault("photo", None)
    return clean


def _clean_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _clean_date(value: object) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    candidate = text[:10]
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", candidate):
        return None
    try:
        datetime.strptime(candidate, "%Y-%m-%d")
    except ValueError:
        return None
    return candidate


def _clean_patch_date(value: object, *, field: str) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    cleaned = _clean_date(text)
    if cleaned is None:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return cleaned


def _clean_value(value: object) -> float | None:
    if value in (None, ""):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return number


def _clean_status(value: object) -> str:
    if isinstance(value, str):
        status = value.strip().lower()
        if status in _VALID_STATUSES:
            return status
    return "owned"


def _inventory_row(row: dict[str, Any]) -> dict[str, Any]:
    return row


def _soft_delete_missing_path(conn, path: Path) -> None:
    try:
        rel = str(path.relative_to(vault_dir()))
    except ValueError:
        return
    conn.execute(
        "UPDATE inventory SET deleted_at = CURRENT_TIMESTAMP WHERE path = ? AND deleted_at IS NULL",
        (rel,),
    )


def _soft_delete_disappeared(conn, seen: set[str]) -> None:
    if seen:
        placeholders = ",".join("?" for _ in seen)
        conn.execute(
            f"""UPDATE inventory SET deleted_at = CURRENT_TIMESTAMP
                WHERE deleted_at IS NULL AND id NOT IN ({placeholders})""",
            tuple(seen),
        )
    else:
        conn.execute("UPDATE inventory SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL")


def _soft_delete_inventory(item_id: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE inventory SET deleted_at = CURRENT_TIMESTAMP WHERE id = ? AND deleted_at IS NULL",
            (item_id,),
        )
