from __future__ import annotations

from pathlib import Path


def test_command_palette_knows_personal_os_result_kinds() -> None:
    types_source = Path("frontend/src/types.ts").read_text(encoding="utf-8")
    palette_source = Path("frontend/src/components/CommandPalette.tsx").read_text(encoding="utf-8")

    for kind in ("task", "project", "routine", "journal", "person", "book", "quote", "inventory", "content"):
        assert f"'{kind}'" in types_source
        assert f"{kind}:" in palette_source

    assert "case 'book':" in palette_source
    assert "case 'quote':" in palette_source
    assert "onNavigate('library', `book:${r.id}`)" in palette_source
    assert "onNavigate('library', `quote:${r.id}`)" in palette_source
    assert "Search everything in Mastisk" in palette_source

