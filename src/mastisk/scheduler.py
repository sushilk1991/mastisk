"""APScheduler wiring. Thin for now — concrete agents wire in during their step."""
from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("mastisk.scheduler")
_calendar_sync_missing_token_logged = False
_RUN_WHEN_WOKEN_MISFIRE_GRACE = None


def _scheduled_calendar_sync() -> None:
    global _calendar_sync_missing_token_logged

    from mastisk.google_calendar import calendar_token_exists, sync_calendar

    if not calendar_token_exists():
        if not _calendar_sync_missing_token_logged:
            log.info("scheduler: calendar_sync skipped (no token)")
            _calendar_sync_missing_token_logged = True
        return
    _calendar_sync_missing_token_logged = False
    sync_calendar()


async def start_scheduler():
    sched = AsyncIOScheduler(timezone="UTC")

    # Reclaim orphaned `running` jobs — if a previous process was killed
    # mid-compile, those rows never transition to done/failed and _pick_job
    # ignores them (only picks `queued`), so they'd be stuck forever.
    _reclaim_orphaned_running()
    _reclaim_running_blog_posts()
    _reclaim_running_tweet_threads()
    _reclaim_firing_reminders()
    _catch_up_daily_cron_engines()

    # One-shot graph repair on boot: reconnects links the Compiler dropped
    # because sibling articles didn't exist yet. This closes the gap between
    # "articles written before this pass existed" and the first Linter tick
    # (which is ~30s after boot). Pure SQL; no network.
    _graph_repair_once()

    # APScheduler's "interval" trigger fires *after* one interval; passing
    # next_run_time forces the first tick a few seconds after startup so
    # queued jobs drain immediately instead of waiting tick_seconds.
    soon = datetime.now(UTC) + timedelta(seconds=2)

    try:
        from mastisk.agents.scout import Scout
        sched.add_job(
            Scout().run_once, "interval",
            seconds=Scout.tick_seconds, id="scout",
            max_instances=1, next_run_time=soon, coalesce=True,
        )
    except Exception as e:
        log.info("scout not scheduled: %s", e)

    try:
        from mastisk.agents.compiler import Compiler
        sched.add_job(
            Compiler().run_once, "interval",
            seconds=Compiler.tick_seconds, id="compiler",
            max_instances=1, next_run_time=soon, coalesce=True,
        )
    except Exception as e:
        log.info("compiler not scheduled: %s", e)

    try:
        from mastisk.agents.notetaker import Notetaker
        # 30s tick per spec §4 + §10. First run 5s after boot so any files
        # dropped into the vault between process restarts start the two-tick
        # stability clock promptly (first tick = register, second tick = classify).
        sched.add_job(
            Notetaker().run_once, "interval",
            seconds=Notetaker.tick_seconds, id="notetaker",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=5),
            coalesce=True,
        )
    except Exception as e:
        log.info("notetaker not scheduled: %s", e)

    try:
        from mastisk.agents.escalator import Escalator
        # 60s tick per spec §4 + §10. First run 10s after boot so any evaluate
        # jobs queued by the Notetaker right before a restart drain promptly.
        sched.add_job(
            Escalator().run_once, "interval",
            seconds=Escalator.tick_seconds, id="escalator",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=10),
            coalesce=True,
        )
    except Exception as e:
        log.info("escalator not scheduled: %s", e)

    try:
        from mastisk.agents.vault_integrity import vault_integrity_scan
        # Tombstones notes whose vault file was deleted externally (Obsidian,
        # Finder, iOS Files). 5min tick is plenty — this is slow drift, not a
        # hot path. First run 30s after boot.
        sched.add_job(
            vault_integrity_scan, "interval",
            minutes=5, id="vault_integrity",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=30),
            coalesce=True,
        )
        log.info("scheduler: vault_integrity registered (5min tick)")
    except Exception as e:
        log.warning("scheduler: vault_integrity registration failed: %s", e)

    try:
        from mastisk.agents.studio import scan_agent_profiles, scan_agent_skills
        from mastisk.content.sync import scan_content
        from mastisk.inventory.sync import scan_inventory
        from mastisk.journal import scan_journal_days
        from mastisk.library.sync import scan_library
        from mastisk.people.sync import scan_people
        from mastisk.projects.sync import scan_projects
        from mastisk.routes.domains import sync_config_domains
        from mastisk.routines.sync import scan_routines
        from mastisk.tasks.sync import scan_tasks

        def personal_os_scan() -> None:
            sync_config_domains()
            scan_projects()
            scan_tasks()
            scan_routines()
            scan_people()
            scan_library()
            scan_inventory()
            scan_agent_skills()
            scan_agent_profiles()
            scan_content()
            scan_journal_days()

        sched.add_job(
            personal_os_scan, "interval",
            minutes=5, id="personal_os_scan",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=30),
            coalesce=True,
        )
        log.info("scheduler: personal_os_scan registered (5min tick)")
    except Exception as e:
        log.warning("scheduler: personal_os_scan registration failed: %s", e)

    try:
        from mastisk.settings import get_settings
        from mastisk.tasks.recurrence import recurrence_tick

        sched.add_job(
            recurrence_tick, "cron",
            hour=0,
            minute=10,
            timezone=ZoneInfo(get_settings().capture.default_timezone),
            id="recurrence_tick",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_RUN_WHEN_WOKEN_MISFIRE_GRACE,
        )
        log.info("scheduler: recurrence_tick registered (daily 00:10 local, run when woken)")
    except Exception as e:
        log.warning("scheduler: recurrence_tick registration failed: %s", e)

    try:
        from mastisk.agents.retainer_rollover import retainer_rollover
        from mastisk.settings import get_settings

        sched.add_job(
            retainer_rollover,
            "cron",
            hour=0,
            minute=20,
            timezone=ZoneInfo(get_settings().capture.default_timezone),
            id="retainer_rollover",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=_RUN_WHEN_WOKEN_MISFIRE_GRACE,
        )
        log.info("scheduler: retainer_rollover registered (daily 00:20 local, run when woken)")
    except Exception as e:
        log.warning("scheduler: retainer_rollover registration failed: %s", e)

    try:
        from mastisk.dashboard.intelligence import needs_review_scan, slipping_scan

        sched.add_job(
            slipping_scan,
            "interval",
            minutes=60,
            id="slipping_scan",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=75),
            coalesce=True,
        )
        sched.add_job(
            needs_review_scan,
            "interval",
            minutes=60,
            id="needs_review_scan",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=90),
            coalesce=True,
        )
        log.info("scheduler: dashboard intelligence scans registered (60min tick)")
    except Exception as e:
        log.warning("scheduler: dashboard intelligence registration failed: %s", e)

    try:
        from mastisk.google_calendar import calendar_token_exists
        from mastisk.settings import get_settings

        if calendar_token_exists():
            minutes = get_settings().calendar.sync_interval_minutes
            sched.add_job(
                _scheduled_calendar_sync, "interval",
                minutes=minutes, id="calendar_sync",
                max_instances=1,
                next_run_time=datetime.now(UTC) + timedelta(seconds=45),
                coalesce=True,
            )
            log.info("scheduler: calendar_sync registered (%smin tick)", minutes)
        else:
            log.info("scheduler: calendar_sync not registered (no token)")
    except Exception as e:
        log.warning("scheduler: calendar_sync registration failed: %s", e)

    try:
        from mastisk.agents.reminder_engine import reminder_tick
        from mastisk.settings import get_settings

        settings = get_settings()
        sched.add_job(
            reminder_tick, "interval",
            seconds=settings.reminders.tick_seconds, id="reminder_tick",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=15),
            coalesce=True,
        )
        log.info("scheduler: reminder_tick registered (%ss tick)", settings.reminders.tick_seconds)
    except Exception as e:
        log.warning("scheduler: reminder_tick registration failed: %s", e)
    else:
        try:
            from mastisk.agents.reminder_engine import daily_summary_tick

            summary_time = settings.reminders.daily_summary_time.strip()
            if summary_time:
                hour_s, minute_s = summary_time.split(":", 1)
                # Sleep catch-up for daily summaries is intentionally handled by
                # reminder_tick: its durable row is created on the next interval
                # after the configured local time, then fired through the normal
                # pending-reminder queue.
                sched.add_job(
                    daily_summary_tick, "cron",
                    hour=int(hour_s),
                    minute=int(minute_s),
                    timezone=ZoneInfo(settings.capture.default_timezone),
                    id="daily_summary",
                    max_instances=1,
                    coalesce=True,
                )
        except Exception as e:
            log.warning("scheduler: daily_summary registration failed: %s", e)

    try:
        from mastisk.agents.linter import Linter
        # Linter runs slightly after Scout/Compiler so it sees fresh articles
        # on the same boot without racing them.
        sched.add_job(
            Linter().run_once, "interval",
            seconds=Linter.tick_seconds, id="linter",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=30),
            coalesce=True,
        )
    except Exception as e:
        log.info("linter not scheduled: %s", e)

    try:
        from mastisk.agents.artifact_agent import ArtifactAgent
        # ArtifactAgent is job-driven (regenerate endpoint enqueues work).
        # Fire 30s after boot so any pending regenerate jobs from before a
        # restart pick up promptly.
        sched.add_job(
            ArtifactAgent().run_once, "interval",
            seconds=ArtifactAgent.tick_seconds, id="artifact-agent",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=30),
            coalesce=True,
        )
    except Exception as e:
        log.info("artifact-agent not scheduled: %s", e)

    try:
        from mastisk.agents.ingest import IngestAgent
        sched.add_job(
            IngestAgent().run_once, "interval",
            seconds=IngestAgent.tick_seconds, id="ingest",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=20),
            coalesce=True,
        )
        log.info("scheduler: ingest registered (%ss tick)", IngestAgent.tick_seconds)
    except Exception as e:
        log.warning("scheduler: ingest registration failed: %s", e)

    try:
        from mastisk.agents.listener import Listener
        # Listener handles YouTube + podcast transcription jobs. Like Compiler,
        # it's job-driven (CLI / POST /api/listen enqueues work). First tick
        # 30s after boot so any pending transcribe jobs from a crash resume.
        sched.add_job(
            Listener().run_once, "interval",
            seconds=Listener.tick_seconds, id="listener",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=30),
            coalesce=True,
        )
    except Exception as e:
        log.info("listener not scheduled: %s", e)

    try:
        from mastisk.agents.synthesizer import Synthesizer
        # Synthesizer drains any queued synthesizer jobs each tick, then
        # *optionally* attempts one spontaneous cross-article synthesis.
        # First run 60s after boot so the corpus has had a moment to settle
        # after whatever the Compiler did on startup — we don't want to
        # synthesize on half-populated clusters.
        sched.add_job(
            Synthesizer().run_once, "interval",
            seconds=Synthesizer.tick_seconds, id="synthesizer",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=60),
            coalesce=True,
        )
    except Exception as e:
        log.info("synthesizer not scheduled: %s", e)

    try:
        from mastisk.agents.roundtable import Roundtable
        # Roundtable is purely job-driven (POST /api/roundtables enqueues a
        # fan_out job). 10s tick keeps latency low for a user who just clicked
        # the button; first run 5s after boot so any jobs queued before a
        # restart drain promptly.
        sched.add_job(
            Roundtable().run_once, "interval",
            seconds=Roundtable.tick_seconds, id="roundtable",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=5),
            coalesce=True,
        )
        log.info("scheduler: roundtable registered (10s tick)")
    except Exception as e:
        log.warning("scheduler: roundtable registration failed: %s", e)

    try:
        from mastisk.agents.github_poller import GithubPoller
        sched.add_job(
            GithubPoller().run_once, "interval",
            seconds=600, id="github_poller",  # 10-min tick, filters by per-repo cadence
            next_run_time=datetime.now(UTC) + timedelta(seconds=30),
            max_instances=1, coalesce=True,
        )
        log.info("scheduler: github_poller registered (10min tick)")
    except Exception as e:
        log.warning("scheduler: github_poller registration failed: %s", e)

    try:
        from mastisk.agents.github_ideator import GithubIdeator
        sched.add_job(
            GithubIdeator().run_once, "interval",
            seconds=600, id="github_ideator",
            next_run_time=datetime.now(UTC) + timedelta(seconds=90),
            max_instances=1, coalesce=True,
        )
        log.info("scheduler: github_ideator registered (10min tick, 24h per-repo cadence)")
    except Exception as e:
        log.warning("scheduler: github_ideator registration failed: %s", e)

    try:
        from mastisk.agents.blog_writer import BlogWriter
        # BlogWriter is purely job-driven (POST /api/blog-posts enqueues a
        # draft job). 10s tick matches Roundtable — user is blocking on this
        # through the modal. First run 5s after boot so any pending drafts
        # from before a restart drain promptly.
        sched.add_job(
            BlogWriter().run_once, "interval",
            seconds=BlogWriter.tick_seconds, id="blog_writer",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=5),
            coalesce=True,
        )
        log.info("scheduler: blog_writer registered (10s tick)")
    except Exception as e:
        log.warning("scheduler: blog_writer registration failed: %s", e)

    try:
        from mastisk.agents.tweet_writer import TweetWriter
        sched.add_job(
            TweetWriter().run_once, "interval",
            seconds=TweetWriter.tick_seconds, id="tweet_writer",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=5),
            coalesce=True,
        )
        log.info("scheduler: tweet_writer registered (10s tick)")
    except Exception as e:
        log.warning("scheduler: tweet_writer registration failed: %s", e)

    try:
        from mastisk.agents.topic_suggester import TopicSuggester
        # TopicSuggester is timer-driven (no jobs queue). 10-min tick keeps
        # it cheap; the agent self-times via cadence_hours so we won't write
        # twice in the same day. First run 90s after boot — let the boot
        # storm of other agents settle before we start hitting Ollama.
        sched.add_job(
            TopicSuggester().run_once, "interval",
            seconds=TopicSuggester.tick_seconds, id="topic_suggester",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(seconds=90),
            coalesce=True,
        )
        log.info("scheduler: topic_suggester registered (10min tick)")
    except Exception as e:
        log.warning("scheduler: topic_suggester registration failed: %s", e)

    try:
        from mastisk.agents.gardener import Gardener
        # Gardener is timer-driven: hourly tick, self-gating on per-page
        # curated_at cooldowns, the daily weave cap, and a 22h reflection
        # cadence. First run 3min after boot — consolidation is never urgent.
        sched.add_job(
            Gardener().run_once, "interval",
            seconds=Gardener.tick_seconds, id="gardener",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(minutes=3),
            coalesce=True,
        )
        log.info("scheduler: gardener registered (hourly tick)")
    except Exception as e:
        log.warning("scheduler: gardener registration failed: %s", e)

    try:
        from mastisk.agents.meeting_prep import MeetingPrep
        # Self-gates: no-op unless calendar events with attendees exist and
        # prep is enabled; dedup per (event, start) in meeting_preps.
        sched.add_job(
            MeetingPrep().run_once, "interval",
            seconds=MeetingPrep.tick_seconds, id="meeting_prep",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(minutes=2),
            coalesce=True,
        )
        log.info("scheduler: meeting_prep registered (15min tick)")
    except Exception as e:
        log.warning("scheduler: meeting_prep registration failed: %s", e)

    try:
        from mastisk.agents.opinion_gap_miner import OpinionGapMiner
        # OpinionGapMiner is timer-driven, weekly cadence. Hourly tick is
        # plenty — the agent self-times via cadence_hours so we won't run
        # twice in the same week. First run 5 minutes after boot so the
        # initial system settle (compiler + scout + topic_suggester) finishes
        # before this agent starts hitting Ollama.
        sched.add_job(
            OpinionGapMiner().run_once, "interval",
            seconds=OpinionGapMiner.tick_seconds, id="opinion_gap_miner",
            max_instances=1,
            next_run_time=datetime.now(UTC) + timedelta(minutes=5),
            coalesce=True,
        )
        log.info("scheduler: opinion_gap_miner registered (1h tick, 7d cadence)")
    except Exception as e:
        log.warning("scheduler: opinion_gap_miner registration failed: %s", e)

    sched.start()
    log.info("scheduler started")
    return sched


async def stop_scheduler(sched) -> None:
    sched.shutdown(wait=False)


def _reclaim_orphaned_running() -> None:
    from mastisk.db.queries import connect
    with connect() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status='queued', started_at=NULL WHERE status='running'"
        )
        if cur.rowcount:
            log.info("reclaimed %s orphaned running job(s)", cur.rowcount)


def _reclaim_running_blog_posts() -> None:
    """Flip stale blog_posts.status='running' rows to 'failed' at boot.

    Companion sweep to _reclaim_orphaned_running. Jobs table rescue puts the
    job back to queued, but BlogWriter's double-process guard refuses rows
    stuck in 'running' → they'd never be reprocessed. Marking them failed
    lets the user regenerate. See spec §13 'Boot-time reclaim'.
    """
    from mastisk.db import queries as q
    from mastisk.db.queries import connect
    try:
        with connect() as conn:
            n = q.reclaim_running_blog_posts(conn, stale_minutes=60)
        if n:
            log.info("reclaimed %s stale running blog_posts row(s)", n)
    except Exception as e:
        log.warning("blog_posts reclaim skipped: %s", e)


def _reclaim_running_tweet_threads() -> None:
    from mastisk.db import queries as q
    from mastisk.db.queries import connect
    try:
        with connect() as conn:
            n = q.reclaim_running_tweet_threads(conn, stale_minutes=60)
        if n:
            log.info("reclaimed %s stale running tweet_threads row(s)", n)
    except Exception as e:
        log.warning("tweet_threads reclaim skipped: %s", e)


def _reclaim_firing_reminders() -> None:
    try:
        from mastisk.agents.reminder_engine import reclaim_firing_reminders

        n = reclaim_firing_reminders()
        if n:
            log.info("reclaimed %s firing reminder row(s)", n)
    except Exception as e:
        log.warning("reminder reclaim skipped: %s", e)


def _catch_up_daily_cron_engines() -> None:
    try:
        from mastisk.tasks.recurrence import recurrence_tick

        n = recurrence_tick()
        if n:
            log.info("recurrence_tick catch-up materialized %s task(s)", n)
    except Exception as e:
        log.warning("recurrence_tick catch-up skipped: %s", e)

    try:
        from mastisk.agents.retainer_rollover import retainer_rollover

        n = retainer_rollover()
        if n:
            log.info("retainer_rollover catch-up processed %s retainer(s)", n)
    except Exception as e:
        log.warning("retainer_rollover catch-up skipped: %s", e)


def _graph_repair_once() -> None:
    """Run Linter's graph-repair pass once on scheduler startup. Reconnects
    any ``<span class="link" data-target>`` references whose target existed
    but wasn't in the ``links`` table (e.g. articles written by the Compiler
    before the repair pass existed). Logs the count; no feed row is emitted.
    """
    try:
        from mastisk.agents.linter import Linter
        n = Linter.repair_graph()
        if n:
            log.info("boot graph-repair: inserted %s link(s)", n)
    except Exception as e:
        log.warning("boot graph-repair skipped: %s", e)
