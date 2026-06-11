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
