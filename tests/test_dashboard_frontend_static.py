from __future__ import annotations

from pathlib import Path


def _dashboard_source() -> str:
    return Path("frontend/src/components/DashboardViews.tsx").read_text(encoding="utf-8")


def test_projects_detail_load_has_stale_response_guard():
    source = _dashboard_source()

    assert "const selectedRef = useRef<string | null>(null);" in source
    assert "const detailRequestRef = useRef(0);" in source
    assert "selectedRef.current !== slug" in source


def test_journal_append_clears_input_after_success_only():
    source = _dashboard_source()

    assert "await api.journalApi.appendLog(today, text);\n      setEntry('');" in source
    assert "await api.journalApi.appendLog(selected, text);\n      setEntry('');" in source
    assert "setEntry('');\n    await api.journalApi.appendLog" not in source


def test_fire_and_forget_mutations_use_shared_error_handler():
    source = _dashboard_source()

    assert "function runMutation" in source
    assert ".then(onChanged)" not in source


def test_done_tasks_have_their_own_bucket():
    source = _dashboard_source()

    assert "const TASK_GROUPS = ['overdue', 'today', 'upcoming', 'someday', 'done']" in source
    assert "if (task.status !== 'open') return 'done';" in source


def test_slipping_muted_items_have_unmute_action():
    source = _dashboard_source()

    assert "api.slipping.unmute" in source
    assert "item.slipping_muted" in source


def test_today_calendar_slot_uses_real_calendar_api():
    source = _dashboard_source()

    assert "api.calendar.today(today)" in source
    assert "mastisk calendar-connect" in source
    assert '<QuietPlaceholder title="Calendar" phase="Phase 9" />' not in source


def test_system_rail_exposes_calendar_health_actions():
    source = Path("frontend/src/components/SystemRail.tsx").read_text(encoding="utf-8")

    assert "api.calendar.status" in source
    assert "api.calendar.sync" in source
    assert "api.calendar.disconnect" in source


def test_people_followup_datetime_local_uses_timezone_helpers():
    source = _dashboard_source()

    assert "followUpIsoToDatetimeLocal(row.follow_up_at)" in source
    assert "followUpDatetimeLocalToIso(followUpAt)" in source
    assert "datetime-local has no zone" in source
    assert "getTimezoneOffset()" in source
    assert "row.follow_up_at ?? '').slice(0, 16)" not in source
    assert "updated.follow_up_at ?? '').slice(0, 16)" not in source


def test_markdown_editor_uses_codemirror_and_protects_frontmatter():
    source = Path("frontend/src/components/MarkdownEditor.tsx").read_text(encoding="utf-8")

    assert "EditorView" in source
    assert "markdown()" in source
    assert "baseSha256" in source
    assert "content_sha256" in source
    assert "function splitFrontmatter" in source
    assert "frontmatter-readonly" in source
    assert "api.attachments.upload" in source
    assert "components={markdownPreviewComponents}" in source
    assert "previewAttachmentUrl" in source
    assert "/api/attachments/" in source
    assert "domEventHandlers" in source
    assert "const [currentBaseSha256, setCurrentBaseSha256]" in source
    assert "setCurrentBaseSha256(saved.content_sha256)" in source
    assert "rescanWarning" in source
    assert "saved.rescan_failed" in source


def test_editor_affordances_cover_journal_notes_projects_and_content():
    dashboard = _dashboard_source()
    note = Path("frontend/src/components/NoteView.tsx").read_text(encoding="utf-8")

    assert "setJournalEditor({ path: detail.path" in dashboard
    assert "setProjectEditor({ path: detail.path" in dashboard
    assert "setContentEditor({ path: detail.path" in dashboard
    assert "setEditorTarget({ path: note.path" in note
    assert "VaultMarkdownEditor" in dashboard
    assert "VaultMarkdownEditor" in note


def test_editor_api_client_exposes_vault_lock_and_attachment_routes():
    source = Path("frontend/src/api.ts").read_text(encoding="utf-8")

    assert "vaultFile:" in source
    assert "editing:" in source
    assert "attachments:" in source
    assert "/vault/file" in source
    assert "base_sha256" in source
    assert "/editing/heartbeat" in source
    assert "/attachments" in source
