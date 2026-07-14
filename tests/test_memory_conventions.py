"""Dated-facts convention: parsing, formatting, supersession, prompt wiring."""
from mastisk.memory_conventions import (
    DATED_FACTS_PROMPT,
    DatedFact,
    format_dated_fact,
    parse_dated_facts,
    supersede_fact,
)


def test_parse_plain_dated_fact():
    facts = parse_dated_facts("- (2026-07-03) Budget: $50K/yr")
    assert facts == [DatedFact(date="2026-07-03", text="Budget: $50K/yr")]


def test_parse_supersession_clause():
    md = "- (2026-07-14) Team size: 18 (previously 12 as of 2026-06-20)"
    [fact] = parse_dated_facts(md)
    assert fact.date == "2026-07-14"
    assert fact.text == "Team size: 18"
    assert fact.previous == "12"
    assert fact.previous_as_of == "2026-06-20"


def test_parse_skips_non_fact_lines():
    md = (
        "## Key facts\n"
        "Some prose.\n"
        "- undated bullet stays out\n"
        "* (2026-01-02) asterisk bullets count\n"
        "- (2026-3-4) malformed date stays out\n"
    )
    facts = parse_dated_facts(md)
    assert [f.text for f in facts] == ["asterisk bullets count"]


def test_format_round_trips_through_parse():
    line = format_dated_fact(
        "Team size: 18", date="2026-07-14", previous="12", previous_as_of="2026-06-20"
    )
    assert line == "- (2026-07-14) Team size: 18 (previously 12 as of 2026-06-20)"
    [fact] = parse_dated_facts(line)
    assert fact.line == line


def test_supersede_keyed_fact_keeps_only_old_value():
    [old] = parse_dated_facts("- (2026-06-20) Team size: 12")
    line = supersede_fact(old, "Team size: 18", date="2026-07-14")
    assert line == "- (2026-07-14) Team size: 18 (previously 12 as of 2026-06-20)"


def test_supersede_unkeyed_fact_keeps_whole_old_text():
    [old] = parse_dated_facts("- (2026-06-20) Ships weekly on Fridays")
    line = supersede_fact(old, "Ships twice a week", date="2026-07-14")
    assert line == (
        "- (2026-07-14) Ships twice a week "
        "(previously Ships weekly on Fridays as of 2026-06-20)"
    )


def test_compiler_schema_carries_the_convention():
    from mastisk.agents.compiler import SCHEMA_MD

    assert "Key facts" in SCHEMA_MD
    assert "previously 12 as of 2026-06-20" in SCHEMA_MD
    assert "__DATED_FACTS_PROMPT__" not in SCHEMA_MD
    assert DATED_FACTS_PROMPT.splitlines()[0] in SCHEMA_MD
