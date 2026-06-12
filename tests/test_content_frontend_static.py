from __future__ import annotations

from pathlib import Path


def test_content_frontend_route_nav_api_and_kanban_are_wired() -> None:
    types = Path("frontend/src/types.ts").read_text(encoding="utf-8")
    router = Path("frontend/src/router.ts").read_text(encoding="utf-8")
    sidebar = Path("frontend/src/components/Sidebar.tsx").read_text(encoding="utf-8")
    api = Path("frontend/src/api.ts").read_text(encoding="utf-8")
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
    dashboard = Path("frontend/src/components/DashboardViews.tsx").read_text(
        encoding="utf-8"
    )

    assert "| 'people' | 'inventory' | 'content'" in types
    assert "'/content': 'content'" in router
    assert "content: '/content'" in router
    assert 'view="content"' in sidebar
    assert "contentApi" in api
    assert "draft: (slug: string)" in api
    assert "export function ContentView" in dashboard
    assert "CONTENT_STATUSES" in dashboard
    assert "kanban" in dashboard
    assert "spawn draft" in dashboard
    assert "view === 'content'" in app
