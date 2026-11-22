"""Mirror the Coreader library's reading notes without copying them.

Coreader keeps one Markdown file per book at ``<library>/Books/<slug>/notes.md``
and that file is the only copy of the reader's highlights and notes. Mastisk
never writes into the library. It records where each file is, plus a derived
search index that is rebuilt whenever the file changes, and reads the file
itself when Ask needs the notes.

Paths are stored relative to the library root, so the mirror survives the
library moving to another folder or the database moving to another Mac.
"""
from __future__ import annotations

import json
import logging
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import yaml

from mastisk.coreader.config import CoreaderConfig, load_config
from mastisk.db.queries import connect
from mastisk.projects.sync import split_frontmatter

log = logging.getLogger("mastisk.coreader.sync")

# Bounds the derived index for one book; the file itself is never truncated.
_MAX_INDEX_CHARS = 400_000
_SCAN_LOCK = threading.Lock()
_last_scan: dict[str, Any] | None = None


def deep_link(book_id: object) -> str:
    return f"coreader://open?book={quote(str(book_id), safe='')}"


def scan_coreader() -> dict[str, Any]:
    """Bring the mirror in line with the library. Safe to call at any time."""
    global _last_scan
    with _SCAN_LOCK:
        cfg = load_config()
        result = _scan(cfg)
        result["scanned_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        _last_scan = result
        return result


def _scan(cfg: CoreaderConfig) -> dict[str, Any]:
    result: dict[str, Any] = {
        "enabled": cfg.enabled,
        "library_root": str(cfg.library_root),
        "upserted": 0,
        "unchanged": 0,
        "unreadable": 0,
        "removed": 0,
        "skipped": None,
    }
    with connect() as conn:
        if not cfg.enabled:
            # Turning the connector off takes the notes out of search and Ask.
            result["removed"] = _tombstone(conn, keep=None)
            return result
        try:
            folders = sorted(
                p for p in cfg.books_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            )
        except OSError as exc:
            # A missing or unreachable library (iCloud signed out, disk not
            # mounted) is not the reader deleting every book: keep the rows.
            result["skipped"] = f"library not readable: {exc}"
            return result

        existing = {
            row["path"]: dict(row)
            for row in conn.execute(
                "SELECT id, path, file_mtime_ns, file_size, deleted_at FROM coreader_books"
            )
        }
        kept_paths: set[str] = set()
        seen_ids: set[str] = set()
        for folder in folders:
            notes = folder / "notes.md"
            rel = f"Books/{folder.name}/notes.md"
            row = existing.get(rel)

            def keep_previous(row: dict | None = row, rel: str = rel) -> None:
                result["unreadable"] += 1
                if row is not None and row["deleted_at"] is None:
                    kept_paths.add(rel)
                    seen_ids.add(row["id"])

            try:
                stat = notes.stat()
            except FileNotFoundError:
                # iCloud can replace an evicted file with a ".name.icloud" stub.
                if (folder / ".notes.md.icloud").exists():
                    keep_previous()
                continue
            except OSError:
                keep_previous()
                continue
            if (
                row is not None
                and row["deleted_at"] is None
                and row["file_mtime_ns"] == stat.st_mtime_ns
                and row["file_size"] == stat.st_size
            ):
                kept_paths.add(rel)
                seen_ids.add(row["id"])
                result["unchanged"] += 1
                continue
            try:
                text = notes.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                # A background process cannot always fetch a file iCloud has
                # evicted (EDEADLK). Keep what was indexed last time.
                log.warning("coreader: cannot read %s: %s", notes, exc)
                keep_previous()
                continue
            book = parse_notes(text, folder)
            if book["id"] in seen_ids:
                log.warning(
                    "coreader: %s repeats id %s of another book folder; ignored",
                    rel, book["id"],
                )
                continue
            # The same folder under a new id replaces its old row.
            conn.execute(
                "DELETE FROM coreader_books WHERE path = ? AND id != ?", (rel, book["id"])
            )
            conn.execute(
                """INSERT INTO coreader_books
                   (id, slug, path, title, author, highlight_count, search_text,
                    file_mtime_ns, file_size, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(id) DO UPDATE SET
                     slug=excluded.slug,
                     path=excluded.path,
                     title=excluded.title,
                     author=excluded.author,
                     highlight_count=excluded.highlight_count,
                     search_text=excluded.search_text,
                     file_mtime_ns=excluded.file_mtime_ns,
                     file_size=excluded.file_size,
                     deleted_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    book["id"], folder.name, rel, book["title"], book["author"],
                    book["highlight_count"], book["search_text"],
                    stat.st_mtime_ns, stat.st_size,
                ),
            )
            kept_paths.add(rel)
            seen_ids.add(book["id"])
            result["upserted"] += 1
        result["removed"] = _tombstone(conn, keep=kept_paths)
    return result


def parse_notes(text: str, folder: Path) -> dict[str, Any]:
    """Read one notes.md as ordinary Markdown with optional front matter.

    Nothing beyond the front matter keys is assumed about the layout, so a
    change to how Coreader formats highlights cannot break the mirror.
    """
    try:
        meta, body = split_frontmatter(text)
    except yaml.YAMLError as exc:
        log.warning("coreader: bad front matter in %s: %s", folder.name, exc)
        meta, body = {}, text
    book_json = _book_json(folder) if not (meta.get("coreader_id") and meta.get("title")) else {}
    book_id = _text(meta.get("coreader_id")) or _text(book_json.get("id")) or folder.name
    title = (
        _text(meta.get("title"))
        or _text(book_json.get("title"))
        or folder.name.replace("-", " ").title()
    )
    return {
        "id": book_id,
        "title": title,
        "author": _text(meta.get("author")) or _text(book_json.get("author")),
        "highlight_count": _count_blockquotes(body),
        "search_text": body[:_MAX_INDEX_CHARS],
    }


def read_notes(rel_path: object) -> str:
    """The notes file for a mirror row, read from the library at this moment."""
    if not rel_path:
        return ""
    cfg = load_config()
    if not cfg.enabled:
        return ""
    try:
        root = cfg.library_root.resolve()
        target = (root / str(rel_path)).resolve()
    except OSError:
        return ""
    if not target.is_relative_to(root):
        return ""
    try:
        return target.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        # Evicted by iCloud or mid-replace: answer from the last index instead.
        with connect() as conn:
            row = conn.execute(
                "SELECT search_text FROM coreader_books WHERE path = ? AND deleted_at IS NULL",
                (str(rel_path),),
            ).fetchone()
        return str(row["search_text"]).strip() if row else ""


def list_books() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, slug, path, title, author, highlight_count, updated_at
               FROM coreader_books WHERE deleted_at IS NULL
               ORDER BY title COLLATE NOCASE"""
        ).fetchall()
    return [{**dict(row), "deep_link": deep_link(row["id"])} for row in rows]


def status() -> dict[str, Any]:
    cfg = load_config()
    with connect() as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM coreader_books WHERE deleted_at IS NULL"
        ).fetchone()[0]
    return {
        "enabled": cfg.enabled,
        "library_root": str(cfg.library_root),
        "root_source": cfg.root_source,
        "library_exists": cfg.books_dir.is_dir(),
        "books": count,
        # Only this process's scans; null until the first one after a start.
        "last_scan": _last_scan,
    }


def _tombstone(conn, *, keep: set[str] | None) -> int:
    rows = conn.execute(
        "SELECT path FROM coreader_books WHERE deleted_at IS NULL"
    ).fetchall()
    gone = [row["path"] for row in rows if keep is None or row["path"] not in keep]
    conn.executemany(
        "UPDATE coreader_books SET deleted_at = CURRENT_TIMESTAMP WHERE path = ?",
        [(path,) for path in gone],
    )
    return len(gone)


def _book_json(folder: Path) -> dict[str, Any]:
    try:
        data = json.loads((folder / "book.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _text(value: object) -> str | None:
    if isinstance(value, list):
        value = ", ".join(str(item).strip() for item in value if str(item).strip())
    if value is None or isinstance(value, dict | bool):
        return None
    text = str(value).strip()
    return text or None


def _count_blockquotes(body: str) -> int:
    count = 0
    inside = False
    for line in body.splitlines():
        quoted = line.lstrip().startswith(">")
        if quoted and not inside:
            count += 1
        inside = quoted
    return count
