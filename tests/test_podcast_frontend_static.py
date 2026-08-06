"""Static guards for the podcast reader's section rendering contract."""
from __future__ import annotations

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_diagram_branch_uses_mermaid_and_keeps_html_for_other_sections():
    """Raw Mermaid must not be injected as HTML or shown as flowchart source text."""
    source = (ROOT / "frontend/src/components/PodcastView.tsx").read_text(encoding="utf-8")
    branch = re.compile(
        r"s\.kind\s*===\s*['\"]diagram['\"]\s*\?\s*"
        r"<MermaidBlock\s+source=\{s\.body\}\s*/>\s*:\s*"
        r"<div\s+dangerouslySetInnerHTML=\{\{\s*__html:\s*s\.body\s*\}\}\s*/>",
        re.DOTALL,
    )

    assert branch.search(source), (
        "PodcastView must route diagram bodies to MermaidBlock and only render "
        "non-diagram section bodies as HTML"
    )
