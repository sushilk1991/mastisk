"""DB-backed user-editing locks for full-file editor sessions.

These locks are advisory and intentionally narrow: they block agent-initiated
rewrites such as Notetaker classification/frontmatter writes and task UID
stamping while the rich editor is open. User-initiated appends and route
mutations keep using the existing per-path ``host_file_lock`` so capture flows
do not wedge behind an open editor.
"""
from __future__ import annotations

from pathlib import Path

from mastisk.db.queries import connect
from mastisk.paths import vault_dir
from mastisk.vault_paths import normalize_vault_markdown_path

STALE_AFTER_SECONDS = 90


def lock_path(raw_path: str) -> dict[str, str]:
    path = normalize_vault_markdown_path(raw_path)
    with connect() as conn:
        _prune_stale(conn)
        conn.execute(
            """INSERT INTO editing_locks (path, locked_at, heartbeat_at)
               VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               ON CONFLICT(path) DO UPDATE SET
                 locked_at = COALESCE(editing_locks.locked_at, CURRENT_TIMESTAMP),
                 heartbeat_at = CURRENT_TIMESTAMP""",
            (path,),
        )
    return {"path": path, "status": "locked"}


def heartbeat_path(raw_path: str) -> dict[str, str]:
    path = normalize_vault_markdown_path(raw_path)
    with connect() as conn:
        _prune_stale(conn)
        conn.execute(
            """INSERT INTO editing_locks (path, locked_at, heartbeat_at)
               VALUES (?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
               ON CONFLICT(path) DO UPDATE SET heartbeat_at = CURRENT_TIMESTAMP""",
            (path,),
        )
    return {"path": path, "status": "locked"}


def unlock_path(raw_path: str) -> dict[str, str]:
    path = normalize_vault_markdown_path(raw_path)
    with connect() as conn:
        _prune_stale(conn)
        conn.execute("DELETE FROM editing_locks WHERE path = ?", (path,))
    return {"path": path, "status": "unlocked"}


def is_user_editing(path_or_rel: str | Path) -> bool:
    if isinstance(path_or_rel, Path):
        try:
            raw_path = str(path_or_rel.relative_to(vault_dir()))
        except ValueError:
            return False
    else:
        raw_path = str(path_or_rel)
    try:
        path = normalize_vault_markdown_path(raw_path)
    except ValueError:
        return False
    with connect() as conn:
        _prune_stale(conn)
        row = conn.execute(
            "SELECT 1 FROM editing_locks WHERE path = ?",
            (path,),
        ).fetchone()
    return row is not None


def _prune_stale(conn) -> None:
    conn.execute(
        "DELETE FROM editing_locks WHERE julianday(heartbeat_at) < julianday('now', ?)",
        (f"-{STALE_AFTER_SECONDS} seconds",),
    )
