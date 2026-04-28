"""APScheduler wiring. Thin for now — concrete agents wire in during their step."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger("mastisk.scheduler")


async def start_scheduler():
    sched = AsyncIOScheduler(timezone="UTC")

    # Reclaim orphaned `running` jobs — if a previous process was killed
    # mid-compile, those rows never transition to done/failed and _pick_job
    # ignores them (only picks `queued`), so they'd be stuck forever.
    _reclaim_orphaned_running()
    _reclaim_running_blog_posts()

    # One-shot graph repair on boot: reconnects links the Compiler dropped
    # because sibling articles didn't exist yet. This closes the gap between
    # "articles written before this pass existed" and the first Linter tick
    # (which is ~30s after boot). Pure SQL; no network.
    _graph_repair_once()

    # APScheduler's "interval" trigger fires *after* one interval; passing
    # next_run_time forces the first tick a few seconds after startup so
    # queued jobs drain immediately instead of waiting tick_seconds.
    soon = datetime.now(timezone.utc) + timedelta(seconds=2)

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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=10),
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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
            coalesce=True,
        )
        log.info("scheduler: vault_integrity registered (5min tick)")
    except Exception as e:
        log.warning("scheduler: vault_integrity registration failed: %s", e)

    try:
        from mastisk.agents.linter import Linter
        # Linter runs slightly after Scout/Compiler so it sees fresh articles
        # on the same boot without racing them.
        sched.add_job(
            Linter().run_once, "interval",
            seconds=Linter.tick_seconds, id="linter",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
            coalesce=True,
        )
    except Exception as e:
        log.info("artifact-agent not scheduled: %s", e)

    try:
        from mastisk.agents.listener import Listener
        # Listener handles YouTube + podcast transcription jobs. Like Compiler,
        # it's job-driven (CLI / POST /api/listen enqueues work). First tick
        # 30s after boot so any pending transcribe jobs from a crash resume.
        sched.add_job(
            Listener().run_once, "interval",
            seconds=Listener.tick_seconds, id="listener",
            max_instances=1,
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=60),
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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),
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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=5),
            coalesce=True,
        )
        log.info("scheduler: blog_writer registered (10s tick)")
    except Exception as e:
        log.warning("scheduler: blog_writer registration failed: %s", e)

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
            next_run_time=datetime.now(timezone.utc) + timedelta(seconds=90),
            coalesce=True,
        )
        log.info("scheduler: topic_suggester registered (10min tick)")
    except Exception as e:
        log.warning("scheduler: topic_suggester registration failed: %s", e)

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
            next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5),
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
