from __future__ import annotations

from pathlib import Path


def test_inventory_frontend_route_nav_and_api_are_wired() -> None:
    types = Path("frontend/src/types.ts").read_text(encoding="utf-8")
    router = Path("frontend/src/router.ts").read_text(encoding="utf-8")
    sidebar = Path("frontend/src/components/Sidebar.tsx").read_text(encoding="utf-8")
    api = Path("frontend/src/api.ts").read_text(encoding="utf-8")
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert "| 'tasks' | 'projects' | 'routines' | 'journal' | 'people' | 'inventory'" in types
    assert "'/inventory': 'inventory'" in router
    assert "inventory: '/inventory'" in router
    assert 'view="inventory"' in sidebar
    assert "inventoryApi" in api
    assert "inventoryApi.delete" in Path(
        "frontend/src/components/DashboardViews.tsx"
    ).read_text(encoding="utf-8")
    assert "archive" in Path("frontend/src/components/DashboardViews.tsx").read_text(
        encoding="utf-8"
    )
    assert "delete: (id: string)" in api
    assert "export function InventoryView" in Path(
        "frontend/src/components/DashboardViews.tsx"
    ).read_text(encoding="utf-8")
    assert "'task', 'journal', 'note', 'project_update', 'routine_done', 'person', 'quote', 'inventory', 'content'" in Path(
        "frontend/src/components/DashboardViews.tsx"
    ).read_text(encoding="utf-8")
    assert "view === 'inventory'" in app
