"""Content pipeline scanner and file-first mutations."""
from __future__ import annotations

import re
import threading
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from slugify import slugify

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.paths import content_dir, vault_dir
from mastisk.projects.sync import split_frontmatter
from mastisk.routes.notes import atomic_write

_CREATE_CONTENT_LOCK = threading.Lock()
_VALID_KINDS = {"video", "article", "podcast", "newsletter"}
_VALID_STATUSES = {"idea", "outline", "editing", "waiting", "published", "done"}
CONTENT_STATUSES = ("idea", "outline", "editing", "waiting", "published", "done")


def scan_content(paths: list[Path] | None = None) -> dict[str, int]:
    item_paths = paths if paths is not None else _content_paths()
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
                item = parse_content_file(path)
            slug = item["slug"]
            seen.add(slug)
            deleted_at_sql = "CURRENT_TIMESTAMP" if item.get("archived") else "NULL"
            conn.execute(
                f"""INSERT INTO content_items
                   (slug, path, title, kind, status, domain, channel, url,
                    publish_date, needs_triage, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, {deleted_at_sql})
                   ON CONFLICT(slug) DO UPDATE SET
                     path=excluded.path,
                     title=excluded.title,
                     kind=excluded.kind,
                     status=excluded.status,
                     domain=excluded.domain,
                     channel=excluded.channel,
                     url=excluded.url,
                     publish_date=excluded.publish_date,
                     needs_triage=excluded.needs_triage,
                     deleted_at={deleted_at_sql},
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    slug,
                    item["path"],
                    item["title"],
                    item["kind"],
                    item["status"],
                    item.get("domain"),
                    item.get("channel"),
                    item.get("url"),
                    item.get("publish_date"),
                    1 if item.get("needs_triage") else 0,
                ),
            )
            upserted += 1
        if paths is None:
            _soft_delete_disappeared(conn, seen)
    return {"upserted": upserted}


def parse_content_file(path: Path) -> dict[str, Any]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    title = _clean_text(meta.get("title")) or path.stem.replace("-", " ").title()
    return {
        "slug": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "title": title,
        "kind": _clean_kind(meta.get("kind")),
        "status": _clean_status(meta.get("status")),
        "domain": _clean_text(meta.get("domain")),
        "channel": _clean_text(meta.get("channel")),
        "url": _clean_text(meta.get("url")),
        "publish_date": _clean_date(meta.get("publish_date")),
        "needs_triage": meta.get("needs_triage") is True,
        "archived": meta.get("archived") is True,
        "body": body,
        "frontmatter": meta,
    }


def create_content_file(
    *,
    title: str,
    kind: str = "article",
    status: str = "idea",
    domain: str | None = None,
    channel: str | None = None,
    url: str | None = None,
    publish_date: str | None = None,
    outline: str | None = None,
    checklist_template: str | None = None,
    needs_triage: bool = False,
) -> dict[str, Any]:
    clean_title = _clean_text(title)
    if not clean_title:
        raise ValueError("title must be non-blank")
    meta: dict[str, Any] = {
        "title": clean_title,
        "kind": _require_kind(kind),
        "status": _require_status(status),
        "domain": _clean_text(domain),
        "channel": _clean_text(channel),
        "url": _clean_text(url),
        "publish_date": _clean_patch_date(publish_date, field="publish_date")
        if publish_date else None,
    }
    if needs_triage:
        meta["needs_triage"] = True
    task_lines = _task_lines_from_template(checklist_template) if checklist_template else []
    body = _initial_body(outline or "", task_lines)
    content = dump_content_file(meta, body)
    with _CREATE_CONTENT_LOCK:
        path = _create_content_file_exclusive(clean_title, content)
    scan_content([path])
    if task_lines:
        from mastisk.tasks.sync import scan_task_hosts

        scan_task_hosts([path])
    item = content_payload(path.stem)
    if item is None:
        raise RuntimeError(f"content mirror missing after write: {path.stem}")
    return item


def patch_content(slug: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    item = content_payload(slug)
    if item is None:
        return None
    path = vault_dir() / item["path"]
    with host_file_lock(path):
        parsed = parse_content_file(path)
        meta = dict(parsed["frontmatter"])
        if "status" in updates:
            meta["status"] = _require_status(updates["status"])
        if "domain" in updates:
            meta["domain"] = _clean_text(updates["domain"])
        if "channel" in updates:
            meta["channel"] = _clean_text(updates["channel"])
        if "url" in updates:
            meta["url"] = _clean_text(updates["url"])
        if "publish_date" in updates:
            meta["publish_date"] = _clean_patch_date(
                updates["publish_date"], field="publish_date"
            )
        atomic_write(path, dump_content_file(meta, parsed["body"]))
    scan_content([path])
    return content_payload(slug)


def clear_content_triage(slug: str) -> dict[str, Any] | None:
    item = content_payload(slug)
    if item is None:
        return None
    path = vault_dir() / item["path"]
    with host_file_lock(path):
        parsed = parse_content_file(path)
        meta = dict(parsed["frontmatter"])
        meta.pop("needs_triage", None)
        atomic_write(path, dump_content_file(meta, parsed["body"]))
    scan_content([path])
    return content_payload(slug)


def list_content(
    *,
    kind: str | None = None,
    status: str | None = None,
    domain: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if kind:
        clauses.append("kind = ?")
        params.append(kind)
    if status:
        clauses.append("status = ?")
        params.append(status)
    if domain:
        clauses.append("domain = ?")
        params.append(domain)
    order_case = " ".join(
        f"WHEN '{value}' THEN {index}" for index, value in enumerate(CONTENT_STATUSES)
    )
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT * FROM content_items
                WHERE {' AND '.join(clauses)}
                ORDER BY CASE status {order_case} ELSE 99 END,
                         updated_at DESC, lower(title), slug""",
            tuple(params),
        ).fetchall()
    return [_content_row(dict(row)) for row in rows]


def content_payload(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM content_items WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        ).fetchone()
    if row is None:
        return None
    payload = _content_row(dict(row))
    path = vault_dir() / payload["path"]
    if not path.exists():
        _soft_delete_content(slug)
        return None
    parsed = parse_content_file(path)
    if parsed.get("archived"):
        _soft_delete_content(slug)
        return None
    from mastisk.tasks.sync import list_tasks, scan_task_hosts

    scan_task_hosts([path])
    return {
        **payload,
        "title": parsed["title"],
        "kind": parsed["kind"],
        "status": parsed["status"],
        "domain": parsed.get("domain"),
        "channel": parsed.get("channel"),
        "url": parsed.get("url"),
        "publish_date": parsed.get("publish_date"),
        "needs_triage": parsed.get("needs_triage", False),
        "frontmatter": parsed["frontmatter"],
        "body": parsed["body"],
        "tasks": list_tasks_by_host(payload["path"], list_tasks(status="open")),
    }


def list_tasks_by_host(host_path: str, tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [task for task in tasks if task.get("host_path") == host_path]


def dump_content_file(meta: dict[str, Any], body: str) -> str:
    clean = {key: value for key, value in meta.items() if value is not None and value != ""}
    frontmatter = yaml.safe_dump(
        clean,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    clean_body = body.strip()
    return f"---\n{frontmatter}\n---\n\n{clean_body}\n" if clean_body else f"---\n{frontmatter}\n---\n"


def _content_paths() -> list[Path]:
    directory = content_dir()
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def _absolute_path(path: Path) -> Path:
    return path if path.is_absolute() else vault_dir() / path


def _create_content_file_exclusive(title: str, content: str) -> Path:
    base = slugify(title)[:80] or "content"
    content_dir().mkdir(parents=True, exist_ok=True)
    for attempt in range(1, 100):
        slug = base if attempt == 1 else f"{base}-{attempt}"
        path = content_dir() / f"{slug}.md"
        if path.exists():
            continue
        with connect() as conn:
            existing = conn.execute(
                "SELECT 1 FROM content_items WHERE slug = ? AND deleted_at IS NULL",
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
    raise RuntimeError(f"unable to allocate content slug for {title!r}")


def _task_lines_from_template(name: str) -> list[str]:
    from mastisk.projects.sync import _task_lines_from_template as project_task_lines

    return project_task_lines(name)


def _initial_body(outline: str, task_lines: list[str]) -> str:
    parts: list[str] = []
    clean_outline = outline.strip()
    if clean_outline:
        parts.append(
            clean_outline if re.search(r"^##\s+", clean_outline, re.M)
            else f"## Outline\n\n{clean_outline}"
        )
    else:
        parts.append("## Outline")
    if task_lines:
        parts.append("## Checklist\n\n" + "\n".join(task_lines))
    return "\n\n".join(parts).strip() + "\n"


def _clean_text(value: object) -> str | None:
    if value in (None, ""):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text or None


def _clean_kind(value: object) -> str:
    if isinstance(value, str):
        kind = value.strip().lower()
        if kind in _VALID_KINDS:
            return kind
    return "article"


def _clean_status(value: object) -> str:
    if isinstance(value, str):
        status = value.strip().lower()
        if status in _VALID_STATUSES:
            return status
    return "idea"


def _require_kind(value: object) -> str:
    kind = _clean_kind(value)
    if kind != value and (
        not isinstance(value, str) or value.strip().lower() not in _VALID_KINDS
    ):
        raise ValueError("kind must be one of video, article, podcast, newsletter")
    return kind


def _require_status(value: object) -> str:
    status = _clean_status(value)
    if status != value and (
        not isinstance(value, str) or value.strip().lower() not in _VALID_STATUSES
    ):
        raise ValueError("status must be one of idea, outline, editing, waiting, published, done")
    return status


def _clean_date(value: object) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    candidate = text[:10]
    try:
        return date.fromisoformat(candidate).isoformat()
    except ValueError:
        return None


def _clean_patch_date(value: object, *, field: str) -> str | None:
    text = _clean_text(value)
    if not text:
        return None
    cleaned = _clean_date(text)
    if cleaned is None:
        raise ValueError(f"{field} must be YYYY-MM-DD")
    return cleaned


def _content_row(row: dict[str, Any]) -> dict[str, Any]:
    row["needs_triage"] = bool(row.get("needs_triage"))
    return row


def _soft_delete_missing_path(conn, path: Path) -> None:
    try:
        rel = str(path.relative_to(vault_dir()))
    except ValueError:
        return
    conn.execute(
        "UPDATE content_items SET deleted_at = CURRENT_TIMESTAMP WHERE path = ? AND deleted_at IS NULL",
        (rel,),
    )


def _soft_delete_disappeared(conn, seen: set[str]) -> None:
    clauses = ["deleted_at IS NULL"]
    params: list[Any] = []
    if seen:
        placeholders = ",".join("?" for _ in seen)
        clauses.append(f"slug NOT IN ({placeholders})")
        params.extend(seen)
    rows = conn.execute(
        f"SELECT slug, path FROM content_items WHERE {' AND '.join(clauses)}",
        tuple(params),
    ).fetchall()
    for row in rows:
        path = vault_dir() / row["path"]
        if path.exists():
            continue
        conn.execute(
            """UPDATE content_items SET deleted_at = CURRENT_TIMESTAMP
               WHERE slug = ? AND deleted_at IS NULL""",
            (row["slug"],),
        )


def _soft_delete_content(slug: str) -> None:
    with connect() as conn:
        conn.execute(
            "UPDATE content_items SET deleted_at = CURRENT_TIMESTAMP WHERE slug = ? AND deleted_at IS NULL",
            (slug,),
        )
