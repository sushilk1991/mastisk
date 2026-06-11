from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_library_frontend_route_nav_and_api_are_wired() -> None:
    types = _read("frontend/src/types.ts")
    router = _read("frontend/src/router.ts")
    app = _read("frontend/src/App.tsx")
    sidebar = _read("frontend/src/components/Sidebar.tsx")
    api = _read("frontend/src/api.ts")

    assert "| 'notes' | 'note' | 'library'" in types
    assert "'/library': 'library'" in router
    assert "pathname.startsWith('/library/books/')" in router
    assert "pathname.startsWith('/library/quotes/')" in router
    assert "<LibraryView" in app
    assert 'view="library"' in sidebar
    assert "libraryApi" in api
    assert "importFile" in api
