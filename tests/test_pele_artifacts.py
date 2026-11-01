"""New charts use local Pele SVG; stored Chart.js configs remain compatible."""
from __future__ import annotations

import copy

import pytest

from mastisk.agents.artifact_agent import _ARTIFACT_PROMPT_SCHEMA, ArtifactAgent


def _chart(spec):
    return {"kind": "chart", "title": "Measured throughput", "spec": spec}


def test_prompt_requests_pele():
    """Default generation must emit source text, not a new Chart.js config."""
    assert '"renderer": "pele"' in _ARTIFACT_PROMPT_SCHEMA
    assert '"source"' in _ARTIFACT_PROMPT_SCHEMA
    assert "xychart" in _ARTIFACT_PROMPT_SCHEMA
    assert "Do not invent numbers" in _ARTIFACT_PROMPT_SCHEMA
    assert "Chart.js config" not in _ARTIFACT_PROMPT_SCHEMA


def test_pele_source_is_preserved():
    """Canvas-specific defaults must not pollute Mermaid-based chart specs."""
    spec = {"renderer": "pele", "source": "xychart-beta\n x-axis [A, B]\n bar [10, 20]"}
    original = copy.deepcopy(spec)
    assert ArtifactAgent._is_valid(_chart(spec))
    assert spec == original


@pytest.mark.parametrize("spec", [
    {"renderer": "pele"},
    {"renderer": "pele", "source": None},
    {"renderer": "pele", "source": 42},
    {"renderer": "pele", "source": " \n "},
    {"renderer": "pele", "source": "x" * 50_001},
    {"renderer": "unknown", "source": "pie\n A: 10"},
])
def test_bad_pele_chart_is_rejected(spec):
    """Malformed marked charts must not be mistaken for legacy configs."""
    assert not ArtifactAgent._is_valid(_chart(spec))


def test_legacy_charts_still_work():
    """Existing canvas chart defaults stay unchanged for stored artifacts."""
    spec = {"type": "bar", "data": {"labels": ["A"], "datasets": [{"data": [10]}]}}
    assert ArtifactAgent._is_valid(_chart(spec))
    assert spec["options"]["maintainAspectRatio"] is False


def test_other_artifacts_unchanged():
    """Comparison, timeline, and stat payloads keep their existing contracts."""
    for kind, spec in (
        ("comparison", {"columns": []}),
        ("timeline", {"events": []}),
        ("stat", {"value": 10, "label": "count"}),
    ):
        original = copy.deepcopy(spec)
        assert ArtifactAgent._is_valid({"kind": kind, "title": "Summary", "spec": spec})
        assert spec == original
