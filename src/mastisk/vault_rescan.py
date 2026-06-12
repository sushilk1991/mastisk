"""Post-edit mirror refresh for vault markdown files."""
from __future__ import annotations

import hashlib
from pathlib import Path

from mastisk.db.queries import connect


def rescan_vault_markdown_path(rel_path: str, target: Path) -> None:
    if rel_path.startswith("journal/"):
        from mastisk.journal import scan_journal_days
        from mastisk.tasks.sync import scan_task_hosts

        scan_journal_days([target])
        scan_task_hosts([target], respect_editing_lock=False)
        return
    if rel_path.startswith("projects/"):
        from mastisk.projects.sync import scan_projects
        from mastisk.tasks.sync import scan_task_hosts

        scan_projects([target])
        scan_task_hosts([target], respect_editing_lock=False)
        return
    if rel_path.startswith("content/"):
        from mastisk.content.sync import scan_content
        from mastisk.tasks.sync import scan_task_hosts

        scan_content([target])
        scan_task_hosts([target], respect_editing_lock=False)
        return
    if rel_path.startswith("_notes/"):
        _sync_note_body(rel_path, target)


def _sync_note_body(rel_path: str, target: Path) -> None:
    from mastisk.agents.notetaker import strip_frontmatter

    text = target.read_text(encoding="utf-8")
    body = strip_frontmatter(text)
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    with connect() as conn:
        conn.execute(
            "UPDATE notes SET body = ?, body_sha256 = ? WHERE path = ? AND deleted_at IS NULL",
            (body, body_sha, rel_path),
        )
