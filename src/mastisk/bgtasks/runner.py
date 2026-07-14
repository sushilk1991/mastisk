"""Automation runner — executes one prose-defined task through the LLM chain.

The model never touches the filesystem: it returns structured JSON and the
runner applies the writes (so a confused model can't clobber task.yaml — the
failure Rowboat guards against by excluding task-management tools). Two modes,
decided per run by the model from the instruction verbs, Rowboat-style:

- **output** — return the complete new ``index.md`` ("maintain / track /
  digest / brief" verbs). The artifact is the deliverable.
- **action** — the only side-effect Mastisk offers today is a push
  notification; the model returns the notification text plus a one-line
  journal entry appended under ``## Journal`` in index.md.
- **skip** — nothing worth doing this run (no fabrication when a source is
  unavailable or nothing changed).

Context given to the model: the instructions, the current index.md, the
trigger that fired, and a deterministic pull of recent wiki activity (the
wiki is the automation's primary data source).
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

from mastisk.agents.registry import resolve_prompt
from mastisk.bgtasks import sync, triggers
from mastisk.bridges import claude_bridge, intelligence
from mastisk.db import queries as q
from mastisk.db.queries import connect
from mastisk.settings import get_settings

log = logging.getLogger("mastisk.automations")

RUNNER_PROMPT = """You are a self-running automation inside Mastisk, a personal knowledge wiki.
You fire on a schedule or when the user clicks Run, and act on persistent
instructions the user wrote. There is **no user present**: never ask
clarifying questions, never hedge, never produce chat-style output.

# Your instructions (re-read them fresh — they are the spec)
{instructions}

# Trigger
{trigger_line}

# Current index.md (your artifact — the user reads this)
<<<
{index_md}
>>>

# Recent wiki activity (your primary data source)
{wiki_context}

# Decide the mode from the instruction verbs
- "maintain / track / summarize / digest / brief / keep updated" → mode "output":
  return the complete new index.md, aligned to the instructions as of today.
  Keep what's still true, fold in what's new, drop what the instructions say
  to drop. Lead with the newest material. Markdown, with the same H1.
- "notify / alert / remind / tell me" → mode "action": return a short
  notification (title + body) and a one-line journal entry; the runner sends
  the push and appends the journal line for you.
- Both kinds of verbs → mode "output" with notify filled in as well.
- Nothing qualifying happened, or a needed source is unavailable → mode
  "skip" with the reason in "summary". NEVER fabricate content to have
  something to write.

# Reply
Return a single JSON object in a ```json``` fenced block:

```json
{{
  "mode": "output" | "action" | "skip",
  "index_md": "complete new file content (mode=output only)",
  "journal_line": "one line, what you did (mode=action only)",
  "notify": {{"title": "...", "body": "..."}},
  "summary": "1-2 sentences: the action and the substance"
}}
```

The summary is a data point, not a sign-off: "Updated — 3 new articles on
agent memory, strongest is X." beats "Done!".
"""

RUN_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "mode": {"type": "string", "enum": ["output", "action", "skip"]},
        "index_md": {"type": "string"},
        "journal_line": {"type": "string"},
        "notify": {
            "type": "object",
            "properties": {"title": {"type": "string"}, "body": {"type": "string"}},
        },
        "summary": {"type": "string"},
    },
    "required": ["mode", "summary"],
}

# A running automation must not manage automations (Rowboat's recursive-
# cascade guard, enforced at our layer: we refuse to run the task at all).
_FORBIDDEN_MARKERS = ("task.yaml", "_automations/", "background task", "automation spec")


async def tick() -> None:
    """Scheduler entry: fire every due, active automation sequentially."""
    cfg = get_settings().automations
    if not cfg.enabled:
        return
    tz = ZoneInfo(get_settings().capture.default_timezone)
    now = datetime.now(UTC)
    for task in sync.list_bg_tasks():
        if not task["active"]:
            continue
        if _in_backoff(task, now=now, backoff_minutes=cfg.retry_backoff_minutes):
            continue
        fired = triggers.due_trigger(
            task["triggers"], task["last_run_at"], now=now, tz=tz,
            grace_seconds=cfg.grace_seconds,
        )
        if fired is None:
            continue
        try:
            await run_task(task["slug"], trigger=fired)
        except Exception:
            log.exception("automations: %s run failed", task["slug"])


async def run_task(slug: str, *, trigger: str) -> dict:
    """Run one automation now. Returns the bg_task_runs row as a dict."""
    task = sync.bg_task_payload(slug)
    if task is None:
        raise ValueError(f"unknown automation: {slug}")

    cfg = get_settings().automations
    with connect() as conn:
        runs_today = conn.execute(
            "SELECT COUNT(*) FROM bg_task_runs WHERE started_at >= datetime('now', 'start of day')",
        ).fetchone()[0]
    if runs_today >= cfg.daily_run_cap:
        return _record_run(slug, trigger, mode="skip",
                           summary=f"Skipped — global daily cap ({cfg.daily_run_cap}) reached.")

    lowered = task["instructions"].lower()
    if any(marker in lowered for marker in _FORBIDDEN_MARKERS):
        return _record_run(
            slug, trigger, mode="skip",
            error="refused: instructions reference automation management "
                  "(an automation may not manage automations)",
        )

    started = datetime.now(UTC).replace(microsecond=0).isoformat()
    sync.write_runtime_fields(slug, last_attempt_at=started)

    index_md = sync.read_index(slug)
    prompt = resolve_prompt("automations", "runner", RUNNER_PROMPT).format(
        instructions=task["instructions"],
        trigger_line=_trigger_line(trigger),
        index_md=index_md[:20000] or "(empty)",
        wiki_context=_wiki_context(),
    )

    try:
        resp, provider = await intelligence.run_intelligence(
            prompt, timeout_s=300, json_object=True, json_schema=RUN_JSON_SCHEMA,
        )
        data = claude_bridge.extract_json_block(resp.get("text") or "")
        if not data or data.get("mode") not in ("output", "action", "skip"):
            raise ValueError(f"unusable reply from {provider}")
    except Exception as e:
        sync.write_runtime_fields(slug, last_run_error=str(e)[:500])
        return _record_run(slug, trigger, mode=None, error=str(e)[:500])

    mode = data["mode"]
    summary = str(data.get("summary") or "").strip()[:500] or f"{mode} run"

    if mode == "output":
        new_index = data.get("index_md")
        if isinstance(new_index, str) and new_index.strip():
            sync.write_index(slug, new_index.rstrip("\n") + "\n")
    if mode in ("output", "action"):
        journal_line = data.get("journal_line")
        if mode == "action" and isinstance(journal_line, str) and journal_line.strip():
            sync.write_index(slug, _append_journal(sync.read_index(slug), journal_line.strip()))
        notify_spec = data.get("notify")
        if isinstance(notify_spec, dict) and notify_spec.get("title"):
            from mastisk import notify
            notify.send(
                str(notify_spec["title"])[:100],
                str(notify_spec.get("body") or "")[:500],
            )

    if mode == "skip":
        # A judged skip is not a failure — but it doesn't advance last_run_at
        # for window triggers either, or a "nothing new yet at 7am" skip
        # would eat the whole band. Cron anchors on fire-time proximity, so
        # repeated skips are naturally bounded to one per fire.
        finished = datetime.now(UTC).replace(microsecond=0).isoformat()
        sync.write_runtime_fields(slug, last_run_error=None, last_run_at=finished,
                                  last_run_summary=f"(skipped) {summary}")
    else:
        finished = datetime.now(UTC).replace(microsecond=0).isoformat()
        sync.write_runtime_fields(
            slug, last_run_at=finished, last_run_summary=summary, last_run_error=None,
        )

    q_row = _record_run(slug, trigger, mode=mode, summary=summary)
    with connect() as conn:
        q.append_feed(
            conn, agent="automations", verb="ran" if mode != "skip" else "skipped",
            obj=f"{task['name']}: {summary[:60]}", kind="automation",
            touched_pages=1 if mode == "output" else 0,
            payload={"slug": slug, "trigger": trigger, "mode": mode},
        )
    return q_row


def _record_run(slug: str, trigger: str, *, mode: str | None,
                summary: str | None = None, error: str | None = None) -> dict:
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO bg_task_runs (slug, trigger, finished_at, mode, summary, error)
               VALUES (?, ?, CURRENT_TIMESTAMP, ?, ?, ?)""",
            (slug, trigger, mode, summary, error),
        )
        row = conn.execute(
            "SELECT * FROM bg_task_runs WHERE id = ?", (cur.lastrowid,),
        ).fetchone()
    return dict(row)


def _in_backoff(task: dict, *, now: datetime, backoff_minutes: int) -> bool:
    """Failure backoff anchored on last_attempt_at (Rowboat's retry-storm guard):
    only applies while the last run errored."""
    if not task.get("last_run_error") or not task.get("last_attempt_at"):
        return False
    try:
        attempted = datetime.fromisoformat(task["last_attempt_at"])
    except ValueError:
        return False
    if attempted.tzinfo is None:
        attempted = attempted.replace(tzinfo=UTC)
    return (now - attempted).total_seconds() < backoff_minutes * 60


def _trigger_line(trigger: str) -> str:
    return {
        "manual": "Manual — the user clicked Run. Do a full refresh.",
        "cron": "Scheduled (cron). Use as a baseline tick.",
        "window": "Scheduled (daily window). Use as a baseline tick.",
    }.get(trigger, trigger)


def _wiki_context(*, limit: int = 20) -> str:
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, kind, title, summary, updated_at FROM articles
               WHERE updated_by != 'Compiler (stub)'
               ORDER BY updated_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    if not rows:
        return "(the wiki is empty)"
    lines = [
        f"- [{r['updated_at']}] {r['kind']}: {r['title']} — {(r['summary'] or '')[:160]} (id: {r['id']})"
        for r in rows
    ]
    return "Most recently updated wiki articles:\n" + "\n".join(lines)


def _append_journal(index_md: str, line: str) -> str:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    stamp = datetime.now(tz).strftime("%Y-%m-%d %H:%M")
    entry = f"- {stamp} — {line}"
    if "## Journal" in index_md:
        head, _, tail = index_md.partition("## Journal")
        tail_lines = tail.split("\n")
        # Insert right after the heading (and its blank line if present).
        insert_at = 1
        while insert_at < len(tail_lines) and not tail_lines[insert_at].strip():
            insert_at += 1
        tail_lines.insert(insert_at, entry)
        return head + "## Journal" + "\n".join(tail_lines)
    base = index_md.rstrip("\n") if index_md.strip() else "# Automation"
    return f"{base}\n\n## Journal\n\n{entry}\n"
