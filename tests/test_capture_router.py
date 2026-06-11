"""Intent-router unit tests for Phase 2 capture."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

BASE_TS = "2026-06-09T15:30:00-07:00"


def _llm_capture(**overrides) -> dict:
    payload = {
        "type": "note",
        "confidence": 0.91,
        "title": None,
        "body": "cleaned body",
        "domain": None,
        "project": None,
        "person": None,
        "due": None,
        "scheduled": None,
        "priority": None,
        "recurrence": None,
        "reminder_lead_minutes": None,
        "no_reminder": False,
        "review_at": None,
        "tags": [],
        "related": [],
    }
    payload.update(overrides)
    return payload


def test_capture_schema_matches_spec():
    from mastisk.capture.router import Capture

    assert list(Capture.model_fields) == [
        "type",
        "confidence",
        "title",
        "body",
        "domain",
        "project",
        "person",
        "due",
        "scheduled",
        "priority",
        "recurrence",
        "reminder_lead_minutes",
        "no_reminder",
        "review_at",
        "tags",
        "related",
    ]


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("remind me to call Sam tomorrow", "task"),
        ("log that I felt scattered today", "journal"),
        ("journal that launch day was intense", "journal"),
        ("add to the mastisk project shipped capture routing", "project_update"),
        ("save a quote from the podcast: stay hungry", "quote"),
        ("add my new monitor to inventory", "inventory"),
        ("new video idea: local-first personal OS", "content"),
        ("did my vitamins", "routine_done"),
    ],
)
def test_command_override_detection_is_deterministic(text, intent):
    from mastisk.capture.router import detect_command_intent

    assert detect_command_intent(text) == intent


def test_did_my_question_is_not_a_routine_command():
    from mastisk.capture.router import detect_command_intent

    assert detect_command_intent("did my flight get rebooked?") is None


@pytest.mark.asyncio
async def test_command_override_fixes_intent_and_resolves_due(data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "America/Los_Angeles"\n'
        "[reminders]\ndefault_lead_minutes = 20\n"
    )
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_llm_capture(type="note", confidence=0.2, due="2099-01-01"))}, "claude")
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ) as run_mock:
        capture = await route_capture(
            "remind me to call Sam tomorrow 2pm",
            source="watch",
            ts=BASE_TS,
        )

    assert capture.type == "task"
    assert capture.due == "2026-06-10T14:00:00-07:00"
    assert capture.reminder_lead_minutes == 20
    assert capture.no_reminder is False
    prompt = run_mock.call_args.args[0]
    assert "Fixed command intent: task" in prompt


@pytest.mark.asyncio
async def test_route_capture_injects_identity_and_empty_phase3_context(data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "America/Los_Angeles"\n')
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_llm_capture(type="journal", body="felt scattered"))}, "claude")
    with (
        patch("mastisk.capture.router.Agent.load_identity", return_value="## identity\nuser voice"),
        patch(
            "mastisk.capture.router.intelligence.run_intelligence",
            new_callable=AsyncMock,
            return_value=response,
        ) as run_mock,
    ):
        capture = await route_capture("felt scattered today", source="watch", ts=BASE_TS)

    assert capture.type == "journal"
    assert capture.body == "felt scattered"
    assert capture.due is None
    prompt = run_mock.call_args.args[0]
    assert "## identity\nuser voice" in prompt
    assert "Existing domains: []" in prompt
    assert "TODO(Phase 3)" in prompt


@pytest.mark.asyncio
async def test_route_capture_does_not_clobber_scheduled_or_review_at(data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "America/Los_Angeles"\n')
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = (
        {
            "text": json.dumps(
                _llm_capture(
                    type="task",
                    due="2099-01-01",
                    scheduled="2026-07-01",
                    review_at="2026-07-02T09:00:00-07:00",
                )
            )
        },
        "claude",
    )
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ):
        capture = await route_capture(
            "remind me to call Sam tomorrow 2pm",
            source="watch",
            ts=BASE_TS,
        )

    assert capture.due == "2026-06-10T14:00:00-07:00"
    assert capture.scheduled == "2026-07-01"
    assert capture.review_at == "2026-07-02T09:00:00-07:00"


@pytest.mark.asyncio
async def test_route_capture_normalizes_model_enum_casing(data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "America/Los_Angeles"\n')
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_llm_capture(type="Task", priority="High"))}, "claude")
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ):
        capture = await route_capture("follow up with Sam", source="watch", ts=BASE_TS)

    assert capture.type == "task"
    assert capture.priority == "high"


@pytest.mark.asyncio
async def test_no_reminder_phrase_suppresses_default_lead(data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "America/Los_Angeles"\n'
        "[reminders]\ndefault_lead_minutes = 30\n"
    )
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_llm_capture(type="task", due="2099-01-01"))}, "claude")
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ):
        capture = await route_capture(
            "remind me to pay rent tomorrow 2pm no reminder",
            source="watch",
            ts=BASE_TS,
        )

    assert capture.due == "2026-06-10T14:00:00-07:00"
    assert capture.no_reminder is True
    assert capture.reminder_lead_minutes is None


@pytest.mark.asyncio
async def test_explicit_reminder_lead_is_computed_from_text(data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "America/Los_Angeles"\n'
        "[reminders]\ndefault_lead_minutes = 30\n"
    )
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_llm_capture(type="task", due=None))}, "claude")
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ):
        capture = await route_capture(
            "remind me to leave tomorrow 2pm remind me 45 minutes before",
            source="watch",
            ts=BASE_TS,
        )

    assert capture.due == "2026-06-10T14:00:00-07:00"
    assert capture.reminder_lead_minutes == 45
    assert capture.no_reminder is False
