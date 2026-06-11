"""Deterministic date normalization for the capture router."""
from __future__ import annotations

BASE_TS = "2026-06-09T15:30:00-07:00"  # Tuesday afternoon in Los Angeles.
TZ = "America/Los_Angeles"


def test_resolves_relative_dates_without_inventing_times():
    from mastisk.capture.dates import resolve_datetime

    assert resolve_datetime("follow up today", BASE_TS, TZ) == "2026-06-09"
    assert resolve_datetime("follow up tomorrow", BASE_TS, TZ) == "2026-06-10"
    assert resolve_datetime("follow up Thursday", BASE_TS, TZ) == "2026-06-11"
    assert resolve_datetime("review this next week", BASE_TS, TZ) == "2026-06-15"


def test_resolves_relative_dates_with_times():
    from mastisk.capture.dates import resolve_datetime

    assert (
        resolve_datetime("remind me tomorrow 2pm", BASE_TS, TZ)
        == "2026-06-10T14:00:00-07:00"
    )
    assert (
        resolve_datetime("remind me today at 4pm", BASE_TS, TZ)
        == "2026-06-09T16:00:00-07:00"
    )
    assert (
        resolve_datetime("remind me tomorrow at 14:00", BASE_TS, TZ)
        == "2026-06-10T14:00:00-07:00"
    )


def test_resolves_time_only_to_next_occurrence():
    from mastisk.capture.dates import resolve_datetime

    assert (
        resolve_datetime("remind me at 4pm", BASE_TS, TZ)
        == "2026-06-09T16:00:00-07:00"
    )
    assert (
        resolve_datetime("remind me at 2pm", BASE_TS, TZ)
        == "2026-06-10T14:00:00-07:00"
    )


def test_tonight_has_a_deterministic_evening_time():
    from mastisk.capture.dates import resolve_datetime

    assert (
        resolve_datetime("journal tonight", BASE_TS, TZ)
        == "2026-06-09T20:00:00-07:00"
    )


def test_model_iso_is_used_only_when_code_has_no_better_signal():
    from mastisk.capture.dates import resolve_datetime

    assert (
        resolve_datetime(
            "follow up with Sam",
            BASE_TS,
            TZ,
            model_iso="2026-06-12T09:00:00-07:00",
        )
        == "2026-06-12T09:00:00-07:00"
    )
    assert (
        resolve_datetime(
            "follow up tomorrow 2pm",
            BASE_TS,
            TZ,
            model_iso="2099-01-01T09:00:00-08:00",
        )
        == "2026-06-10T14:00:00-07:00"
    )


def test_unparseable_returns_none():
    from mastisk.capture.dates import resolve_datetime

    assert resolve_datetime("sometime when things calm down", BASE_TS, TZ) is None
