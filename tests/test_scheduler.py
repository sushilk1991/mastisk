from __future__ import annotations

import logging

import pytest


@pytest.mark.asyncio
async def test_invalid_daily_summary_time_does_not_hide_reminder_tick_log(
    data_tmp, db, monkeypatch, caplog
):
    cfg = data_tmp / "config.toml"
    cfg.write_text(
        '[reminders]\ndaily_summary_time = "bad"\n'
        '[notify]\nbackend = "ntfy"\nntfy_topic = "test"\n',
        encoding="utf-8",
    )
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk import scheduler

    monkeypatch.setattr(scheduler, "_reclaim_orphaned_running", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_running_blog_posts", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_running_tweet_threads", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_firing_reminders", lambda: None)
    monkeypatch.setattr(scheduler, "_graph_repair_once", lambda: None)

    class FakeScheduler:
        def __init__(self, timezone):
            self.timezone = timezone
            self.jobs: list[str | None] = []

        def add_job(self, func, trigger, **kwargs):
            self.jobs.append(kwargs.get("id"))

        def start(self):
            return None

        def shutdown(self, wait=False):
            return None

    monkeypatch.setattr(scheduler, "AsyncIOScheduler", FakeScheduler)

    with caplog.at_level(logging.INFO, logger="mastisk.scheduler"):
        sched = await scheduler.start_scheduler()

    assert "reminder_tick" in sched.jobs
    assert "scheduler: reminder_tick registered" in caplog.text
    assert "scheduler: daily_summary registration failed" in caplog.text


@pytest.mark.asyncio
async def test_daily_cron_jobs_allow_wake_from_sleep_misfires(data_tmp, db, monkeypatch):
    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "UTC"\n', encoding="utf-8")
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk import scheduler

    monkeypatch.setattr(scheduler, "_reclaim_orphaned_running", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_running_blog_posts", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_running_tweet_threads", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_firing_reminders", lambda: None)
    monkeypatch.setattr(scheduler, "_catch_up_daily_cron_engines", lambda: None, raising=False)
    monkeypatch.setattr(scheduler, "_graph_repair_once", lambda: None)

    class FakeScheduler:
        def __init__(self, timezone):
            self.timezone = timezone
            self.jobs: dict[str | None, dict] = {}

        def add_job(self, func, trigger, **kwargs):
            self.jobs[kwargs.get("id")] = {"trigger": trigger, **kwargs}

        def start(self):
            return None

        def shutdown(self, wait=False):
            return None

    monkeypatch.setattr(scheduler, "AsyncIOScheduler", FakeScheduler)

    sched = await scheduler.start_scheduler()

    assert sched.jobs["recurrence_tick"]["misfire_grace_time"] is None
    assert sched.jobs["retainer_rollover"]["misfire_grace_time"] is None


@pytest.mark.asyncio
async def test_scheduler_runs_daily_cron_catchups_on_boot(data_tmp, db, monkeypatch):
    from mastisk import scheduler

    calls: list[str] = []
    monkeypatch.setattr(scheduler, "_reclaim_orphaned_running", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_running_blog_posts", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_running_tweet_threads", lambda: None)
    monkeypatch.setattr(scheduler, "_reclaim_firing_reminders", lambda: None)
    monkeypatch.setattr(scheduler, "_catch_up_daily_cron_engines", lambda: calls.append("catchup"), raising=False)
    monkeypatch.setattr(scheduler, "_graph_repair_once", lambda: None)

    class FakeScheduler:
        def __init__(self, timezone):
            self.timezone = timezone

        def add_job(self, func, trigger, **kwargs):
            return None

        def start(self):
            return None

        def shutdown(self, wait=False):
            return None

    monkeypatch.setattr(scheduler, "AsyncIOScheduler", FakeScheduler)

    await scheduler.start_scheduler()

    assert calls == ["catchup"]
