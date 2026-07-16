from __future__ import annotations

from pathlib import Path


def _read(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def test_chat_is_grounded_actionable_and_safe_to_render() -> None:
    drawer = _read("frontend/src/components/AskDrawer.tsx")
    api = _read("frontend/src/api.ts")
    types = _read("frontend/src/types.ts")

    assert "ReactMarkdown" in drawer
    assert "dangerouslySetInnerHTML" not in drawer
    assert "Research web" in drawer
    assert "Save as note" in drawer
    assert "messages?:" in api
    assert "mode?:" in api
    assert "sources: AskSource[]" in types
    assert "retrieved_sources?: AskSource[]" in types
    assert "coverage: Record<string, number>" in types


def test_mobile_navigation_moves_primary_actions_out_of_the_header() -> None:
    app = _read("frontend/src/App.tsx")
    nav = _read("frontend/src/components/MobileNav.tsx")
    article = _read("frontend/src/components/ArticleView.tsx")
    css = _read("frontend/src/styles/mastisk.css")

    assert "<MobileNav" in app
    for label in ("Today", "Search", "Chat", "Capture", "Menu"):
        assert f'aria-label="{label}"' in nav
    assert ".mobile-nav" in css
    assert ".tb-actions { display: none; }" in css
    assert "height: 100dvh" in css
    assert "Page context" in article


def test_browser_chat_sends_structured_history_without_rewriting_the_question() -> None:
    sidepanel = _read("extension/sidepanel.js")
    background = _read("extension/background.js")

    assert "function buildMessages()" in sidepanel
    assert "messages: buildMessages()" in sidepanel
    assert "Earlier in this chat:" not in sidepanel
    assert "messages: msg.messages || []" in background
