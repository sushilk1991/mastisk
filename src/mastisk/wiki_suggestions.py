"""Wiki-suggestions queue: vault mirror + decision orchestration.

The DB (``wiki_suggestions`` table, helpers in ``db/queries.py``) is the
source of truth for the stub gate; this module renders the pending shortlist
into a small vault file so the queue is readable from the phone / Obsidian,
and wraps user decisions so routes don't touch two layers.

Unlike people/projects the vault file is a one-way mirror (machine-derived
from compile activity), so there is no scan-back: edits to the file are
overwritten on the next render.
"""
from __future__ import annotations

import logging
from pathlib import Path

from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.paths import vault_dir
from mastisk.routes.notes import atomic_write

log = logging.getLogger("mastisk.wiki_suggestions")

# Rowboat keeps the shortlist curated at 8-12 — long lists stop being read.
SHORTLIST_SIZE = 12


def suggestions_file() -> Path:
    return vault_dir() / "_suggestions" / "suggested-topics.md"


def render_vault_file() -> Path | None:
    """Mirror the pending shortlist to the vault. Skips the write when the
    rendered content is unchanged so iCloud isn't churned on every compile."""
    with connect() as conn:
        pending = q.list_wiki_suggestions(conn, status="pending", limit=SHORTLIST_SIZE)

    lines = [
        "---",
        "generated_by: mastisk stub gate",
        "---",
        "",
        "# Suggested topics",
        "",
        "_Wiki-link targets waiting for enough independent references (or your",
        "promote/dismiss call in the PWA). This file is machine-rendered —",
        "edits here are overwritten._",
        "",
    ]
    if not pending:
        lines.append("(queue is empty)")
    for s in pending:
        refs = ", ".join(s["referrers"][:4]) + ("…" if len(s["referrers"]) > 4 else "")
        lines.append(
            f"- **{s['title']}** (`{s['slug']}`) — seen in {s['occurrences']} "
            f"article{'s' if s['occurrences'] != 1 else ''}: {refs}"
        )
    content = "\n".join(lines) + "\n"

    path = suggestions_file()
    try:
        with host_file_lock(path):
            if path.exists() and path.read_text() == content:
                return path
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, content)
    except OSError as e:
        log.warning("wiki_suggestions: vault mirror write failed: %s", e)
        return None
    return path


def decide(slug: str, *, action: str) -> dict | None:
    """Apply promote/dismiss/restore, then refresh the vault mirror."""
    with connect() as conn, q.txn(conn):
        row = q.decide_wiki_suggestion(conn, slug, action=action)
    if row is not None:
        render_vault_file()
    return row
