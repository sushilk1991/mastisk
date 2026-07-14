"""Dated-facts memory convention shared by writer agents.

A fact bullet carries the date it was observed and, when it replaces an
earlier value, keeps that value inline instead of silently dropping it:

    - (2026-07-03) Budget: $50K/yr
    - (2026-07-14) Team size: 18 (previously 12 as of 2026-06-20)

The convention makes facts age gracefully: a dated fact stays useful, an
undated one rots. Writers (Compiler, Gardener) emit it via
``DATED_FACTS_PROMPT``; maintainers (Gardener) parse and supersede via the
helpers here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

DATED_FACT_RE = re.compile(
    r"^\s*[-*]\s+\((?P<date>\d{4}-\d{2}-\d{2})\)\s+(?P<text>.+?)\s*$"
)

_PREVIOUSLY_RE = re.compile(
    r"\s*\((?:previously|was)\s+(?P<prev>.+?)\s+as of\s+(?P<prev_date>\d{4}-\d{2}-\d{2})\)\s*$",
    re.IGNORECASE,
)

# "Key: value" shape — used so supersession can keep just the old value when
# both facts describe the same key ("Team size: 18 (previously 12 …)").
_KEYED_RE = re.compile(r"^(?P<key>[^:]{1,60}):\s*(?P<value>.+)$")


@dataclass(frozen=True)
class DatedFact:
    date: str  # YYYY-MM-DD
    text: str  # fact text without the date prefix or previously-clause
    previous: str | None = None
    previous_as_of: str | None = None

    @property
    def line(self) -> str:
        return format_dated_fact(
            self.text,
            date=self.date,
            previous=self.previous,
            previous_as_of=self.previous_as_of,
        )


def parse_dated_facts(markdown: str) -> list[DatedFact]:
    """Extract dated-fact bullets from a markdown blob (non-matching lines skipped)."""
    facts: list[DatedFact] = []
    for line in markdown.splitlines():
        m = DATED_FACT_RE.match(line)
        if not m:
            continue
        text = m.group("text")
        previous = previous_as_of = None
        pm = _PREVIOUSLY_RE.search(text)
        if pm:
            previous = pm.group("prev")
            previous_as_of = pm.group("prev_date")
            text = text[: pm.start()].rstrip()
        facts.append(
            DatedFact(
                date=m.group("date"),
                text=text,
                previous=previous,
                previous_as_of=previous_as_of,
            )
        )
    return facts


def format_dated_fact(
    text: str,
    *,
    date: str,
    previous: str | None = None,
    previous_as_of: str | None = None,
) -> str:
    line = f"- ({date}) {text.strip()}"
    if previous and previous_as_of:
        line += f" (previously {previous} as of {previous_as_of})"
    return line


def supersede_fact(old: DatedFact, new_text: str, *, date: str) -> str:
    """New fact line that keeps the superseded value inline.

    When both facts share a ``Key: value`` shape with the same key, only the
    old *value* is carried ("Team size: 18 (previously 12 as of …)");
    otherwise the whole old text is kept.
    """
    old_m = _KEYED_RE.match(old.text)
    new_m = _KEYED_RE.match(new_text.strip())
    if (
        old_m
        and new_m
        and old_m.group("key").strip().casefold() == new_m.group("key").strip().casefold()
    ):
        previous = old_m.group("value").strip()
    else:
        previous = old.text
    return format_dated_fact(
        new_text, date=date, previous=previous, previous_as_of=old.date
    )


DATED_FACTS_PROMPT = """\
Dated-facts convention (applies to any "Key facts" list):
- Date every fact: `(2026-07-03) Budget: $50K/yr`. Facts change; a dated fact stays useful, an undated one rots. Use the source's date when it states one, otherwise today's date.
- When a fact supersedes an earlier one, update it in place and keep the old value inline: `(2026-07-14) Team size: 18 (previously 12 as of 2026-06-20)`. Never silently drop the old value — history is data.
- One fact per bullet; concrete and specific (numbers, names, dates over vague claims)."""
