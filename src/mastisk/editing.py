"""DB-backed user-editing locks for full-file editor sessions.

These locks are advisory and intentionally narrow: they block agent-initiated
rewrites such as Notetaker classification/frontmatter writes and task UID
stamping while the rich editor is open. User-initiated appends and route
mutations keep using the existing per-path ``host_file_lock`` so capture flows
do not wedge behind an open editor.

Ownership is per browser/editor session token. Each lock acquisition inserts a
separate ``(path, token)`` row; the path is considered locked while any token
row is alive. Heartbeat and unlock only touch their own token, so closing one
tab cannot clear another tab's lock.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from mastisk.db.queries import connect
from mastisk.paths import vault_dir
from mastisk.vault_paths import normalize_vault_markdown_path

STALE_AFTER_SECONDS = 90


def lock_path(raw_path: str) -> dict[str, str]:
    path = normalize_vault_markdown_path(raw_path)
    token = secrets.token_urlsafe(24)
    with connect() as conn:
        _prune_stale(conn)
        conn.execute(
            """INSERT INTO editing_locks (path, token, locked_at, heartbeat_at)
               VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (path, token),
        )
    return {"path": path, "token": token, "status": "locked"}


def heartbeat_path(raw_path: str, token: str) -> dict[str, str]:
    path = normalize_vault_markdown_path(raw_path)
    with connect() as conn:
        _prune_stale(conn)
        cur = conn.execute(
            """UPDATE editing_locks
               SET heartbeat_at = CURRENT_TIMESTAMP
               WHERE path = ? AND token = ?""",
            (path, token),
        )
    return {"path": path, "token": token, "status": "locked" if cur.rowcount else "missing"}


def unlock_path(raw_path: str, token: str) -> dict[str, str]:
    path = normalize_vault_markdown_path(raw_path)
    with connect() as conn:
        _prune_stale(conn)
        conn.execute("DELETE FROM editing_locks WHERE path = ? AND token = ?", (path, token))
    return {"path": path, "token": token, "status": "unlocked"}


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
            "SELECT 1 FROM editing_locks WHERE path = ? LIMIT 1",
            (path,),
        ).fetchone()
    return row is not None


def _prune_stale(conn) -> None:
    conn.execute(
        "DELETE FROM editing_locks WHERE julianday(heartbeat_at) < julianday('now', ?)",
        (f"-{STALE_AFTER_SECONDS} seconds",),
    )
