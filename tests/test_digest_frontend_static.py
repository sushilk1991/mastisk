from __future__ import annotations

from pathlib import Path

from tests.frontend_css import frontend_css


def test_digest_thread_titles_are_tooltipped_and_line_clamped() -> None:
    """Existing long DB titles should not expand the digest card vertically."""
    component = Path("frontend/src/components/DigestView.tsx").read_text(encoding="utf-8")
    css = frontend_css()

    assert 'className="thread-title"' in component
    assert "title={thread.title}" in component
    thread_rule = css[css.index(".thread-title {"): css.index(".thread-title:hover")]
    assert "display: -webkit-box" in thread_rule
    assert "-webkit-line-clamp: 2" in thread_rule
    assert "-webkit-box-orient: vertical" in thread_rule
    assert "overflow: hidden" in thread_rule


def test_digest_bodies_are_rendered_as_markdown_not_raw_html() -> None:
    component = Path("frontend/src/components/DigestView.tsx").read_text(encoding="utf-8")

    assert "ReactMarkdown" in component
    assert "dangerouslySetInnerHTML" not in component
    assert "digestBodyMarkdown(thread.body)" in component
