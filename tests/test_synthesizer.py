"""Synthesizer prompt tests.

These tests keep the feedback loop intact while guarding against the title
template collapse that made many Synthesis pages sound alike.
"""
from __future__ import annotations

import pytest


@pytest.fixture
def synthesizer(vault_tmp, data_tmp):
    from mastisk.paths import ensure_dirs
    ensure_dirs()
    from mastisk.agents.synthesizer import Synthesizer
    return Synthesizer()


def test_prompt_keeps_feedback_examples_but_discourages_title_template_copying(
    synthesizer,
):
    """Accepted examples stay in the loop, but title cadence is not copied."""
    members = [
        {
            "id": "copilot-cowork-file-exfiltration-prompt-injection-2026",
            "kind": "Source",
            "title": "Microsoft Copilot Cowork Exfiltrates Files via Poisoned Skills",
            "summary": "Prompt injection makes delegated agents leak files.",
            "body_md": "The exploit routes file contents through a poisoned skill.",
        },
        {
            "id": "california-ab1856-linux-age-verification-exemption-2026",
            "kind": "Source",
            "title": "California Moves to Exempt Open-Source OSes From Age Checks",
            "summary": "A state bill changes where age checks apply.",
            "body_md": "The policy boundary shifts into operating systems.",
        },
    ]
    positives = [
        {
            "title": "Generation Got Cheap. The Channel That Catches Bad Output Got Cut.",
            "body_md": "A kept synthesis with concrete links.",
        }
    ]
    negatives = [
        {
            "title": "Nobody Is Selling the Model. They're Selling What Sits Next to It.",
            "body_md": "A rejected draft.",
            "user_feedback": "Writing is very random overall.",
        }
    ]

    prompt = synthesizer._draft_prompt(members, positives, negatives)

    assert "examples the user has ACCEPTED" in prompt
    assert positives[0]["title"] in prompt
    assert "examples the user REJECTED" in prompt
    assert "Do not clone their" in prompt
    assert "title diversity guardrails" in prompt
    assert "Learn judgment from the accepted/rejected examples" in prompt
    assert "Microsoft Copilot Cowork Exfiltrates Files via Poisoned Skills" in prompt
    assert "California Moves to Exempt Open-Source OSes From Age Checks" in prompt


def test_title_guidance_requires_cluster_specific_titles():
    """Title guidance should bias toward concrete anchors, not generic aphorisms."""
    from mastisk.agents.synthesizer import Synthesizer

    guidance = Synthesizer._render_title_guidance([
        {"title": "Digital Age Assurance Act"},
        {"title": "Norway's National Library Trains a Sovereign Norwegian LLM"},
    ])

    assert "specific enough that it would look wrong" in guidance
    assert "The X moved" in guidance
    assert "layer, boundary, constraint, check" in guidance
    assert "Digital Age Assurance Act" in guidance
    assert "Norway's National Library Trains a Sovereign Norwegian LLM" in guidance


def test_title_guidance_requires_short_single_clause_titles():
    """Synthesis titles must not become whole headline paragraphs."""
    from mastisk.agents.synthesizer import _SCHEMA_MD, Synthesizer

    guidance = Synthesizer._render_title_guidance([
        {"title": "Digital Age Assurance Act"},
    ])

    contract = f"{_SCHEMA_MD}\n{guidance}"
    assert "single clause" in contract
    assert "70 characters" in contract
    assert "No subtitles" in contract
    assert "no em-dash appendages" in contract
