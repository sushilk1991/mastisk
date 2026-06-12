"""Post-edit mirror refresh for vault markdown files."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from mastisk.db.queries import connect

log = logging.getLogger("mastisk.vault_rescan")


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
    from mastisk.agents.notetaker import has_typed_capture_frontmatter, strip_frontmatter

    text = target.read_text(encoding="utf-8")
    body = strip_frontmatter(text)
    body_sha = hashlib.sha256(body.encode("utf-8")).hexdigest()
    typed_capture = has_typed_capture_frontmatter(text)
    with connect() as conn:
        row = conn.execute(
            "SELECT id FROM notes WHERE path = ? AND deleted_at IS NULL",
            (rel_path,),
        ).fetchone()
        if row is None:
            log.warning("vault_rescan: body sync matched no note row for %s", rel_path)
            return
        if typed_capture:
            cur = conn.execute(
                """UPDATE notes
                   SET body = ?, body_sha256 = ?
                   WHERE id = ?""",
                (body, body_sha, row["id"]),
            )
        else:
            cur = conn.execute(
                """UPDATE notes
                   SET body = ?,
                       body_sha256 = ?,
                       classified_at = NULL,
                       classification = NULL,
                       summary = NULL,
                       confidence = NULL,
                       tags_json = '[]',
                       escalation_state = 'none',
                       escalation_trigger = NULL,
                       escalation_article_id = NULL,
                       escalation_retry_count = 0,
                       escalation_next_attempt_at = NULL
                   WHERE id = ?""",
                (body, body_sha, row["id"]),
            )
            conn.execute(
                "DELETE FROM note_links WHERE note_id = ? AND rank > 0",
                (row["id"],),
            )
        if cur.rowcount == 0:
            log.warning("vault_rescan: body sync matched no note row for %s", rel_path)
