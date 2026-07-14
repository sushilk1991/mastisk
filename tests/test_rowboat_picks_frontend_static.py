"""Static guards for the rowboat-picks PWA surfaces (suggestions, automations,
verdicts, prep cards): interaction polish that regressions would silently eat."""
from __future__ import annotations

from pathlib import Path


def _css() -> str:
    return Path("frontend/src/styles/mastisk.css").read_text(encoding="utf-8")


def test_new_surfaces_have_staggered_reduced_motion_safe_entrances() -> None:
    css = _css()
    assert "@keyframes row-enter" in css
    assert "animation-delay: 50ms" in css
    reduced = css[css.index("@media (prefers-reduced-motion: reduce)"):]
    assert "animation: none" in reduced
    assert "scale: 1" in reduced


def test_press_feedback_uses_specific_transitions_not_all() -> None:
    css = _css()
    idx = css.index("transition-property: color, border-color, background, scale")
    assert idx > -1
    assert "scale: 0.96" in css
    # The polish block must never fall back to transition: all.
    block = css[css.index("/* shared polish"):css.index("/* automations")]
    assert "transition: all" not in block


def test_hit_areas_extended_without_overlap() -> None:
    css = _css()
    assert ".verdict-btn::after, .sugg-ref::after" in css
    assert "inset: -8px -1px" in css


def test_dynamic_numbers_are_tabular() -> None:
    css = _css()
    assert "font-variant-numeric: tabular-nums" in css


def test_prep_brief_is_line_clamped() -> None:
    css = _css()
    rule = css[css.index(".cal-prep {"):css.index(".cal-prep-label")]
    assert "-webkit-line-clamp" in rule
    assert "overflow: hidden" in rule


def test_verdict_buttons_are_accessible() -> None:
    component = Path("frontend/src/components/ArticleView.tsx").read_text(encoding="utf-8")
    assert 'aria-label="More like this"' in component
    assert 'aria-label="Less like this"' in component
    assert "aria-pressed={verdict === 'liked'}" in component
    assert 'aria-label="Why didn' in component  # reason input labelled


def test_automation_create_form_has_visible_labels() -> None:
    component = Path("frontend/src/components/AutomationsView.tsx").read_text(encoding="utf-8")
    assert 'htmlFor="auto-create-name"' in component
    assert 'htmlFor="auto-create-instructions"' in component
    assert 'aria-labelledby="auto-detail-instructions-label"' in component


def test_focus_visible_rings_on_new_controls() -> None:
    css = _css()
    assert ".verdict-btn:focus-visible" in css
    assert "outline: 2px solid var(--accent)" in css
