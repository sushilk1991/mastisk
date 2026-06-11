"""Project file scanner and file-first mutations."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import yaml
from slugify import slugify

from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.markdown_sections import append_to_section
from mastisk.paths import projects_dir, vault_dir
from mastisk.routes.notes import atomic_write

ProjectType = Literal["project", "area"]
ProjectStatus = Literal["active", "someday", "paused", "done"]
_VALID_TYPES = {"project", "area"}
_VALID_STATUSES = {"active", "someday", "paused", "done"}


def scan_projects(paths: list[Path] | None = None) -> dict[str, int]:
    project_paths = paths if paths is not None else _project_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for path in project_paths:
            if not path.exists() or path.name.startswith("."):
                continue
            project = parse_project_file(path)
            slug = path.stem
            seen.add(slug)
            conn.execute(
                """INSERT INTO projects
                   (slug, path, name, type, domain, status, due, last_activity_at, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT last_activity_at FROM projects WHERE slug = ?), CURRENT_TIMESTAMP), NULL)
                   ON CONFLICT(slug) DO UPDATE SET
                     path=excluded.path,
                     name=excluded.name,
                     type=excluded.type,
                     domain=excluded.domain,
                     status=excluded.status,
                     due=excluded.due,
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
                    slug,
                ),
            )
            upserted += 1
        if paths is None:
            if seen:
                placeholders = ",".join("?" for _ in seen)
                conn.execute(
                    f"""UPDATE projects SET deleted_at = CURRENT_TIMESTAMP
                        WHERE deleted_at IS NULL AND slug NOT IN ({placeholders})""",
                    tuple(seen),
                )
            else:
                conn.execute(
                    "UPDATE projects SET deleted_at = CURRENT_TIMESTAMP WHERE deleted_at IS NULL"
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
    with host_file_lock(path):
        atomic_write(path, dump_project_file(meta, "## Log\n\n## Tasks\n"))
    scan_projects([path])
    return get_project(path.stem) or parse_project_file(path)


def patch_project_frontmatter(slug: str, updates: dict[str, Any]) -> dict[str, Any] | None:
    project = get_project(slug)
    if project is None:
        return None
    path = vault_dir() / project["path"]
    with host_file_lock(path):
        meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
        for key in ("name", "type", "domain", "status", "due"):
            if key in updates:
                meta[key] = updates[key]
        if meta.get("type") not in _VALID_TYPES:
            meta["type"] = "project"
        if meta.get("status") not in _VALID_STATUSES:
            meta["status"] = "active"
        atomic_write(path, dump_project_file(meta, body))
    scan_projects([path])
    return get_project(slug)


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
    return get_project(slug)


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
