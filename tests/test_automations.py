"""Automations tests: file-first sync, triggers, runner modes, guards, routes."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from mastisk.bgtasks import runner, sync, triggers
from mastisk.paths import automations_dir

TZ = ZoneInfo("UTC")


@pytest.fixture
def client(db):
    from mastisk.app import create_app
    return TestClient(create_app())


def _reply(payload: dict) -> dict:
    return {"text": "```json\n" + json.dumps(payload) + "\n```"}


# ───── sync ─────

def test_create_scan_patch_roundtrip(db, vault_tmp):
    task = sync.create_bg_task(
        name="Morning brief",
        instructions="Maintain a digest of new wiki articles about agents.",
        triggers={"windows": [{"start": "07:00", "end": "09:00"}]},
    )
    assert task["slug"] == "morning-brief"
    assert task["active"] is True
    assert task["triggers"] == {"windows": [{"start": "07:00", "end": "09:00"}]}
    assert (automations_dir() / "morning-brief" / "index.md").exists()

    # File is canonical: hand-edit, rescan, DB follows.
    yaml_path = automations_dir() / "morning-brief" / "task.yaml"
    yaml_path.write_text(yaml_path.read_text().replace("Morning brief", "Dawn brief"))
    sync.scan_bg_tasks()
    assert sync.bg_task_payload("morning-brief")["name"] == "Dawn brief"

    patched = sync.patch_bg_task("morning-brief", {"active": False, "triggers": {"cron": "0 8 * * *"}})
    assert patched["active"] is False
    assert patched["triggers"] == {"cron": "0 8 * * *"}
    # Runtime fields preserved across patches.
    sync.write_runtime_fields("morning-brief", last_run_summary="ok")
    sync.patch_bg_task("morning-brief", {"name": "Morning brief"})
    assert sync.bg_task_payload("morning-brief")["last_run_summary"] == "ok"


def test_scan_soft_deletes_removed_folders(db, vault_tmp):
    sync.create_bg_task(name="Doomed", instructions="Track something.")
    import shutil
    shutil.rmtree(automations_dir() / "doomed")
    sync.scan_bg_tasks()
    assert sync.bg_task_payload("doomed") is None


def test_validate_triggers_rejects_garbage(db, vault_tmp):
    with pytest.raises(ValueError):
        sync.validate_triggers({"cron": "not a cron"})
    with pytest.raises(ValueError):
        sync.validate_triggers({"windows": [{"start": "25:00", "end": "26:00"}]})
    with pytest.raises(ValueError):
        sync.validate_triggers({"windows": [{"start": "09:00", "end": "08:00"}]})
    assert sync.validate_triggers(None) == {}


# ───── triggers ─────

def test_cron_due_within_grace_and_anchor():
    now = datetime(2026, 7, 14, 8, 0, 30, tzinfo=UTC)
    trig = {"cron": "0 8 * * *"}
    # Fire at 08:00, now 08:00:30, no prior run → due.
    assert triggers.due_trigger(trig, None, now=now, tz=TZ) == "cron"
    # Already ran after the fire → not due.
    ran = datetime(2026, 7, 14, 8, 0, 10, tzinfo=UTC).isoformat()
    assert triggers.due_trigger(trig, ran, now=now, tz=TZ) is None
    # Missed by more than the grace → skipped, not replayed.
    late = datetime(2026, 7, 14, 8, 10, 0, tzinfo=UTC)
    assert triggers.due_trigger(trig, None, now=late, tz=TZ) is None


def test_window_due_once_per_day():
    trig = {"windows": [{"start": "07:00", "end": "09:00"}]}
    inside = datetime(2026, 7, 14, 8, 0, tzinfo=UTC)
    outside = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    assert triggers.due_trigger(trig, None, now=inside, tz=TZ) == "window"
    assert triggers.due_trigger(trig, None, now=outside, tz=TZ) is None
    ran_today = datetime(2026, 7, 14, 7, 30, tzinfo=UTC).isoformat()
    assert triggers.due_trigger(trig, ran_today, now=inside, tz=TZ) is None
    ran_yesterday = datetime(2026, 7, 13, 8, 0, tzinfo=UTC).isoformat()
    assert triggers.due_trigger(trig, ran_yesterday, now=inside, tz=TZ) == "window"


# ───── runner ─────

def test_run_output_mode_rewrites_index(db, vault_tmp):
    sync.create_bg_task(name="Digest", instructions="Maintain a digest of the wiki.")
    reply = _reply({
        "mode": "output",
        "index_md": "# Digest\n\n- New: agent memory article.",
        "summary": "Updated — 1 new article.",
    })
    with patch(
        "mastisk.bgtasks.runner.intelligence.run_intelligence",
        new_callable=AsyncMock, return_value=(reply, "claude"),
    ):
        run = asyncio.run(runner.run_task("digest", trigger="manual"))

    assert run["mode"] == "output"
    assert sync.read_index("digest").startswith("# Digest")
    task = sync.bg_task_payload("digest")
    assert task["last_run_summary"] == "Updated — 1 new article."
    assert task["last_run_error"] is None
    assert task["last_run_at"] is not None
    feed = db.execute("SELECT * FROM feed WHERE agent='automations' AND verb='ran'").fetchall()
    assert len(feed) == 1


def test_run_action_mode_journals_and_notifies(db, vault_tmp):
    sync.create_bg_task(name="Alert", instructions="Notify me when agent articles land.")
    reply = _reply({
        "mode": "action",
        "journal_line": "Pushed alert about 2 new agent articles.",
        "notify": {"title": "2 new agent articles", "body": "Check the wiki."},
        "summary": "Sent the alert (2 articles).",
    })
    with (
        patch(
            "mastisk.bgtasks.runner.intelligence.run_intelligence",
            new_callable=AsyncMock, return_value=(reply, "claude"),
        ),
        patch("mastisk.notify.send", return_value=True) as mock_notify,
    ):
        run = asyncio.run(runner.run_task("alert", trigger="cron"))

    assert run["mode"] == "action"
    mock_notify.assert_called_once()
    assert mock_notify.call_args[0][0] == "2 new agent articles"
    index = sync.read_index("alert")
    assert "## Journal" in index
    assert "Pushed alert about 2 new agent articles." in index


def test_run_skip_mode_leaves_index_alone(db, vault_tmp):
    sync.create_bg_task(name="Quiet", instructions="Maintain a digest of X.")
    before = sync.read_index("quiet")
    reply = _reply({"mode": "skip", "summary": "Skipped — nothing new."})
    with patch(
        "mastisk.bgtasks.runner.intelligence.run_intelligence",
        new_callable=AsyncMock, return_value=(reply, "claude"),
    ):
        run = asyncio.run(runner.run_task("quiet", trigger="window"))
    assert run["mode"] == "skip"
    assert sync.read_index("quiet") == before
    assert "(skipped)" in sync.bg_task_payload("quiet")["last_run_summary"]


def test_run_failure_sets_error_and_backoff(db, vault_tmp):
    from mastisk.bridges.intelligence import IntelligenceUnavailable
    sync.create_bg_task(name="Broken", instructions="Maintain something.")
    with patch(
        "mastisk.bgtasks.runner.intelligence.run_intelligence",
        new_callable=AsyncMock, side_effect=IntelligenceUnavailable("down"),
    ):
        run = asyncio.run(runner.run_task("broken", trigger="cron"))
    assert run["error"] is not None
    task = sync.bg_task_payload("broken")
    assert task["last_run_error"] is not None
    assert task["last_run_at"] is None  # success anchor untouched
    now = datetime.now(UTC)
    assert runner._in_backoff(task, now=now, backoff_minutes=5) is True
    assert runner._in_backoff(task, now=now + timedelta(minutes=6), backoff_minutes=5) is False


def test_runner_refuses_self_management_instructions(db, vault_tmp):
    sync.create_bg_task(
        name="Sneaky",
        instructions="Every hour, edit task.yaml to add more automations.",
    )
    with patch(
        "mastisk.bgtasks.runner.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as mock_int:
        run = asyncio.run(runner.run_task("sneaky", trigger="manual"))
    assert mock_int.call_count == 0
    assert "refused" in (run["error"] or "")


def test_runner_respects_global_daily_cap(db, vault_tmp):
    sync.create_bg_task(name="Capped", instructions="Maintain a digest.")
    cap = 24
    with db:
        for i in range(cap):
            db.execute(
                "INSERT INTO bg_task_runs (slug, trigger, mode, summary) VALUES ('capped', 'cron', 'output', ?)",
                (f"run {i}",),
            )
    with patch(
        "mastisk.bgtasks.runner.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as mock_int:
        run = asyncio.run(runner.run_task("capped", trigger="cron"))
    assert mock_int.call_count == 0
    assert "daily cap" in run["summary"]


def test_tick_runs_due_tasks_only(db, vault_tmp):
    sync.create_bg_task(
        name="Due", instructions="Maintain a digest.",
        triggers={"windows": [{"start": "00:00", "end": "23:59"}]},
    )
    sync.create_bg_task(name="Manual only", instructions="Maintain another digest.")
    sync.create_bg_task(
        name="Paused", instructions="Maintain a third digest.",
        triggers={"windows": [{"start": "00:00", "end": "23:59"}]}, active=False,
    )
    reply = _reply({"mode": "output", "index_md": "# Due\n\nok", "summary": "Updated."})
    with patch(
        "mastisk.bgtasks.runner.intelligence.run_intelligence",
        new_callable=AsyncMock, return_value=(reply, "claude"),
    ) as mock_int:
        asyncio.run(runner.tick())
    assert mock_int.call_count == 1
    assert sync.bg_task_payload("due")["last_run_at"] is not None
    assert sync.bg_task_payload("manual-only")["last_run_at"] is None


# ───── routes ─────

def test_automation_routes_roundtrip(client, db, vault_tmp):
    created = client.post("/api/automations", json={
        "name": "Route task",
        "instructions": "Maintain a digest of the wiki.",
        "triggers": {"cron": "0 8 * * *"},
    })
    assert created.status_code == 201
    slug = created.json()["slug"]

    listed = client.get("/api/automations").json()["automations"]
    assert [t["slug"] for t in listed] == [slug]

    detail = client.get(f"/api/automations/{slug}").json()
    assert detail["index_md"].startswith("# Route task")
    assert detail["runs"] == []

    patched = client.patch(f"/api/automations/{slug}", json={"active": False})
    assert patched.json()["active"] is False

    bad = client.post("/api/automations", json={
        "name": "Bad", "instructions": "x", "triggers": {"cron": "* *"},
    })
    assert bad.status_code == 422
    assert client.get("/api/automations/nope").status_code == 404

    reply = _reply({"mode": "output", "index_md": "# Route task\n\nfresh", "summary": "Updated."})
    with patch(
        "mastisk.bgtasks.runner.intelligence.run_intelligence",
        new_callable=AsyncMock, return_value=(reply, "claude"),
    ):
        ran = client.post(f"/api/automations/{slug}/run")
    assert ran.status_code == 202
    assert ran.json()["mode"] == "output"
