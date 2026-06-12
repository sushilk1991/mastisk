from __future__ import annotations

from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_global_quick_capture_is_wired_to_titlebar_and_shortcut() -> None:
    app = _read("frontend/src/App.tsx")
    titlebar = _read("frontend/src/components/Titlebar.tsx")
    sheet = _read("frontend/src/components/QuickCaptureSheet.tsx")
    api = _read("frontend/src/api.ts")

    assert "QuickCaptureSheet" in app
    assert "quickCaptureOpen" in app
    assert "e.shiftKey && (e.key === 'a' || e.key === 'A')" in app
    assert "openQuickCapture('note')" in app
    assert "tb-capture-btn" in titlebar
    assert "Quick capture (⌘⇧A)" in titlebar
    assert "Enter saves. Shift+Enter adds a line. Esc closes." in sheet
    assert "api.quickCapture(trimmed" in sheet
    assert "destinationTarget(result)" in sheet
    assert "`${BASE}/quick-capture`" in api


def test_personal_os_views_have_create_affordances_and_empty_states() -> None:
    dashboard = _read("frontend/src/components/DashboardViews.tsx")
    library = _read("frontend/src/components/LibraryView.tsx")
    notes = _read("frontend/src/components/NotesView.tsx")
    api = _read("frontend/src/api.ts")

    assert "function DashboardHeader" in dashboard
    assert "function CreatePanel" in dashboard
    assert "function EmptyState" in dashboard
    assert "api.tasks.create" in dashboard
    assert "api.routinesApi.create" in dashboard
    assert "Open today" in dashboard
    assert "New journal log" in dashboard
    assert "New content idea" in dashboard
    assert "No routines yet. Create your first routine" in dashboard
    assert "No captures need triage." in dashboard

    assert "New book" in library
    assert "New quote" in library
    assert "Capture one from scratch" in library
    assert "LibrarySkeleton" in library

    assert "className=\"new-action\"" in notes
    assert "Capture the first thought from here." in notes
    assert "dash-skeleton" in notes

    assert "create: (body: { text: string" in api
    assert "create: (body: { name: string; time_of_day" in api


def test_design_polish_primitives_are_static_guarded() -> None:
    css = _read("frontend/src/styles/mastisk.css")
    agents = _read("frontend/src/components/AgentsView.tsx")

    assert "transition: all" not in css
    assert "will-change: all" not in css
    assert ".quick-capture-sheet" in css
    assert ".dash-empty-state" in css
    assert ".dash-skeleton" in css
    assert "scale(0.96)" in css
    assert "min-height: 44px" in css
    assert "font-variant-numeric: tabular-nums" in css
    assert "text-wrap: balance" in css
    assert "rgba(0, 0, 0, 0.1)" in css
    assert "rgba(255, 255, 255, 0.1)" in css
    assert "prefers-reduced-motion: reduce" in css
    assert "agent-detail-skeleton" in agents
