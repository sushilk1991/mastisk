"""Intent-router unit tests for Phase 2 capture."""
from __future__ import annotations

import asyncio
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
        "command_detected",
    ]


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("remind me to call Sam tomorrow", "task"),
        ("log that I felt scattered today", "journal"),
        ("journal that launch day was intense", "journal"),
        ("add to the mastisk project shipped capture routing", "project_update"),
        ("save a quote from the podcast: stay hungry", "quote"),
        ("save this quote from the podcast: stay hungry", "quote"),
        ("save that quote from the podcast: stay hungry", "quote"),
        ("add my new monitor to inventory", "inventory"),
        ("new video idea: local-first personal OS", "content"),
    ],
)
def test_command_override_detection_is_deterministic(text, intent):
    from mastisk.capture.router import detect_command_intent

    assert detect_command_intent(text) == intent


def test_did_my_question_is_not_a_routine_command():
    from mastisk.capture.router import detect_command_intent

    assert detect_command_intent("did my flight get rebooked?") is None


def test_did_my_phrase_is_only_a_hint_for_now():
    from mastisk.capture.router import detect_command_hint, detect_command_intent

    assert detect_command_intent("did my vitamins") is None
    assert detect_command_hint("did my vitamins") == "routine_done"


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
    assert capture.command_detected is True
    prompt = run_mock.call_args.args[0]
    assert "Fixed command intent: task" in prompt
    assert run_mock.call_args.kwargs["classification"] is True


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
    assert "Existing projects: []" in prompt
    assert "TODO(Phase 3)" not in prompt
    assert "Command hint intent: null" in prompt
    assert "untrusted user/source data" in prompt


@pytest.mark.asyncio
async def test_route_capture_injects_existing_domains_and_projects(data_tmp, db):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "America/Los_Angeles"\n')
    db.execute("INSERT INTO domains (slug, name) VALUES ('work', 'Work')")
    db.execute(
        """INSERT INTO projects (slug, path, name, type, domain, status)
           VALUES ('mastisk', 'projects/mastisk.md', 'Mastisk', 'project', 'work', 'active')"""
    )
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_llm_capture(type="task", project="mastisk"))}, "claude")
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ) as run_mock:
        capture = await route_capture("follow up on Mastisk", source="watch", ts=BASE_TS)

    assert capture.project == "mastisk"
    prompt = run_mock.call_args.args[0]
    assert '"slug": "work"' in prompt
    assert '"name": "Work"' in prompt
    assert '"slug": "mastisk"' in prompt
    assert '"name": "Mastisk"' in prompt


@pytest.mark.asyncio
async def test_route_capture_uses_configured_router_timeout(data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "America/Los_Angeles"\nrouter_timeout_s = 11\n'
    )
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_llm_capture(type="journal"))}, "claude")
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ) as run_mock:
        await route_capture("felt focused", source="watch", ts=BASE_TS)

    assert run_mock.call_args.kwargs["timeout_s"] == 11
    assert run_mock.call_args.kwargs["classification"] is True


@pytest.mark.asyncio
async def test_route_capture_enforces_total_router_timeout(data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[capture]\ndefault_timezone = "America/Los_Angeles"\nrouter_timeout_s = 1\n'
    )
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    async def slow_router(*args, **kwargs):
        await asyncio.sleep(10)
        return ({"text": json.dumps(_llm_capture(type="journal"))}, "claude")

    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        side_effect=slow_router,
    ), pytest.raises(TimeoutError):
        await route_capture("felt focused", source="watch", ts=BASE_TS)


@pytest.mark.asyncio
async def test_invalid_client_ts_is_logged_and_treated_as_server_time(data_tmp, caplog):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "America/Los_Angeles"\n')
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_llm_capture(type="journal", due=None))}, "claude")
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ) as run_mock:
        capture = await route_capture("felt focused", source="watch", ts="not-a-date")

    assert capture.type == "journal"
    assert run_mock.call_count == 1
    prompt = run_mock.call_args.args[0]
    assert "timestamp: \n" in prompt
    assert "invalid client timestamp" in caplog.text


@pytest.mark.asyncio
async def test_did_my_hint_does_not_force_intent_or_skip_gates(data_tmp):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "America/Los_Angeles"\n')
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_llm_capture(type="note", confidence=0.2))}, "claude")
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ) as run_mock:
        capture = await route_capture("did my vitamins", source="watch", ts=BASE_TS)

    assert capture.type == "note"
    assert capture.command_detected is False
    prompt = run_mock.call_args.args[0]
    assert "Fixed command intent: null" in prompt
    assert "Command hint intent: routine_done" in prompt


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


@pytest.mark.asyncio
async def test_past_due_task_keeps_due_without_default_reminder(data_tmp):
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
            "remind me today at 2pm",
            source="watch",
            ts=BASE_TS,
        )

    assert capture.due == "2026-06-09T14:00:00-07:00"
    assert capture.reminder_lead_minutes is None
