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
    from mastisk.db.queries import MAX_GENERATED_ARTICLE_TITLE_CHARS

    guidance = Synthesizer._render_title_guidance([
        {"title": "Digital Age Assurance Act"},
    ])

    contract = f"{_SCHEMA_MD}\n{guidance}"
    assert "single clause" in contract
    assert f"{MAX_GENERATED_ARTICLE_TITLE_CHARS} characters" in contract
    assert "No subtitles" in contract
    assert "no em-dash appendages" in contract


def _member(article_id):
    return {"id": article_id, "kind": "Source", "title": article_id.title(),
            "summary": "", "body_md": "", "updated_at": "2026-10-31 12:00:00"}


def test_draft_prompt_carries_the_shared_voice_guide_before_the_schema(synthesizer):
    from mastisk.agents.base import wiki_voice

    prompt = synthesizer._draft_prompt([_member("a")], [], [])

    assert wiki_voice()
    assert wiki_voice() in prompt
    # The JSON contract stays last so prose guidance cannot displace it.
    assert prompt.index(wiki_voice()) < prompt.index('"kind": "Synthesis"')


def test_draft_prompt_omits_interest_themes(synthesizer, monkeypatch):
    """learnings.md steered every synthesis toward the same few subjects."""
    seen = {}

    def fake_identity(files=("identity", "interests", "dislikes", "style", "learnings")):
        seen["files"] = files
        return "# About the user"

    monkeypatch.setattr(type(synthesizer), "load_identity", staticmethod(fake_identity))
    synthesizer._draft_prompt([_member("a")], [], [])

    assert seen["files"] == ("identity", "style")


def test_related_links_exclude_members_the_draft_left_out(synthesizer):
    members = [_member("used"), _member("linked"), _member("unused")]
    data = {
        "title": "T",
        "sections": [{"h": "H", "body": '<p><span class="link" data-target="linked">x</span></p>'}],
        "related": [{"id": "used", "label": "Used", "weight": 0.9}],
    }

    related = synthesizer._normalise_draft(data, members)["related"]

    assert [r["id"] for r in related] == ["used", "linked"]


def test_related_links_fall_back_to_whole_cluster_when_draft_names_none(synthesizer):
    members = [_member("a"), _member("b")]

    related = synthesizer._normalise_draft({"title": "T", "sections": []}, members)["related"]

    assert [r["id"] for r in related] == ["a", "b"]


def test_synthesis_markdown_keeps_paragraph_breaks():
    """The Synthesizer's private copy of the renderer had lost this fix."""
    from mastisk.agents.synthesizer import _sections_to_md

    md = _sections_to_md([{"h": "H", "body": "<p>One ends.</p><p>Two starts.</p>"}])

    assert "One ends.\n\nTwo starts." in md


def test_drafter_reads_member_articles_whole(synthesizer):
    member = _member("a") | {"body_md": "start " + "middle " * 2000 + "THE-LAST-FACT"}

    assert "THE-LAST-FACT" in synthesizer._draft_prompt([member], [], [])
