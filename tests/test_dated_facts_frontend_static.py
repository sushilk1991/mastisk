from __future__ import annotations

from pathlib import Path


def test_article_view_decorates_dated_facts() -> None:
    """Fact bullets render their date and supersession clause as quiet metadata."""
    component = Path("frontend/src/components/ArticleView.tsx").read_text(encoding="utf-8")
    css = Path("frontend/src/styles/mastisk.css").read_text(encoding="utf-8")

    assert "export function decorateFactDates" in component
    # Both HTML-bearing section kinds run through the decorator.
    assert component.count("decorateFactDates(s.body)") == 2
    assert 'class="fact-date"' in component
    assert 'class="fact-prev"' in component

    assert ".art-body .fact-date" in css
    assert ".art-body .fact-prev" in css
