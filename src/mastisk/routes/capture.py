"""Token-authenticated capture ingress with Phase-2 intent routing."""
from __future__ import annotations

import hmac
import logging
import re
from datetime import datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field, field_validator

from mastisk.agents.reminder_engine import create_task_due_reminder
from mastisk.capture.router import Capture, route_capture
from mastisk.content.sync import create_content_file
from mastisk.inventory.sync import create_inventory_file
from mastisk.journal import JournalFrontmatterError, append_log
from mastisk.library.sync import create_quote_file, find_book, match_book_in_text
from mastisk.paths import vault_dir
from mastisk.people.sync import append_interaction, create_person_file, find_person
from mastisk.projects.sync import append_project_log, find_project
from mastisk.routes.notes import persist_note_capture
from mastisk.routines.sync import RoutineArchivedError, complete_routine_completion, local_today
from mastisk.settings import get_settings, read_capture_bearer_token
from mastisk.tasks.sync import append_task_to_host, journal_host_for_today

router = APIRouter(prefix="/api/capture", tags=["capture"])
log = logging.getLogger("mastisk.capture")


def require_capture_token(authorization: str | None) -> None:
    """Bearer-token gate for the ingress."""
    token = read_capture_bearer_token()
    if not token:
        raise HTTPException(
            status_code=503,
            detail="capture ingress not configured - run `mastisk capture-token`",
        )
    expected = f"Bearer {token}".encode("utf-8", "surrogateescape")
    actual = authorization.encode("utf-8", "surrogateescape") if authorization else b""
    if not hmac.compare_digest(actual, expected):
        raise HTTPException(status_code=401, detail="invalid or missing token")


class CaptureRequest(BaseModel):
    text: str = Field(min_length=1)
    source: Literal["watch", "phone", "pwa", "cli"] = "watch"
    # Request timestamp used by the router for relative-date resolution.
    ts: str | None = None

    @field_validator("text")
    @classmethod
    def _non_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must be non-blank")
        return v


@router.post("", status_code=201)
async def capture(
    req: CaptureRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    """Route a capture into the personal OS.

    General "follow up with Anjali Thursday" captures stay tasks: task due
    dates/reminders are the general action mechanism. The People branch only
    records person-typed CRM interactions; `/api/people/{slug}` owns
    `follow_up_at` as the CRM-native follow-up field.
    """
    require_capture_token(authorization)
    return await route_and_persist_capture(req.text, source=req.source, ts=req.ts)


async def route_and_persist_capture(
    text: str,
    *,
    source: Literal["watch", "phone", "pwa", "cli"],
    ts: str | None = None,
) -> dict:
    """Route a text capture and persist it after caller-owned auth checks."""

    try:
        routed = await route_capture(text, source=source, ts=ts)
    except Exception:
        log.exception("capture router failed; falling back to raw inbox note")
        return _persist_inbox_fallback(text, source, ts=ts)

    if routed.type == "inbox" or (
        not routed.command_detected and routed.confidence < 0.5
    ):
        return _persist_inbox_fallback(text, source, ts=ts)

    needs_triage = False if routed.command_detected else routed.confidence < 0.85

    if routed.type == "task":
        try:
            return _persist_task_capture(routed, ts=ts, needs_triage=needs_triage)
        except Exception:
            log.exception("capture task write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(text, source, ts=ts)

    if routed.type == "journal":
        try:
            return _persist_journal_capture(
                routed,
                ts=ts,
                source=source,
                needs_triage=needs_triage,
            )
        except JournalFrontmatterError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception:
            log.exception("capture journal write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(text, source, ts=ts)

    if routed.type == "routine_done" and routed.routine:
        try:
            filed = _persist_routine_done_capture(routed, ts=ts)
            if filed is not None:
                return {**filed, "needs_triage": needs_triage}
        except RoutineArchivedError:
            return _persist_inbox_fallback(text, source, ts=ts)
        except Exception:
            log.exception("capture routine_done write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(text, source, ts=ts)

    if routed.type == "project_update" and routed.project:
        try:
            filed = _persist_project_update_capture(routed, needs_triage=needs_triage)
            if filed is not None:
                return {**filed, "needs_triage": needs_triage}
        except Exception:
            log.exception("capture project update write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(text, source, ts=ts)

    if routed.type == "person":
        try:
            filed = _persist_person_capture(routed, ts=ts, needs_triage=needs_triage)
            if filed is not None:
                return filed
        except Exception:
            log.exception("capture person write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(text, source, ts=ts)

    if routed.type == "quote":
        try:
            filed = _persist_quote_capture(
                routed,
                raw_text=text,
                needs_triage=needs_triage,
            )
            if filed is not None:
                return filed
        except Exception:
            log.exception("capture quote write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(text, source, ts=ts)

    if routed.type == "inventory":
        try:
            filed = _persist_inventory_capture(
                routed,
                raw_text=text,
                ts=ts,
                needs_triage=needs_triage,
            )
            if filed is not None:
                return filed
        except Exception:
            log.exception("capture inventory write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(text, source, ts=ts)

    if routed.type == "content":
        try:
            filed = _persist_content_capture(
                routed,
                raw_text=text,
                needs_triage=needs_triage,
            )
            if filed is not None:
                return filed
        except Exception:
            log.exception("capture content write failed; falling back to raw inbox note")
            return _persist_inbox_fallback(text, source, ts=ts)

    try:
        row = persist_note_capture(
            body=routed.body,
            source=source,
            slug_text=text,
            ts=_capture_local_datetime(ts) if ts else None,
            file_content=(
                _body_with_capture_frontmatter(routed, needs_triage=needs_triage)
                if _has_typed_capture_metadata(routed)
                else routed.body
            ),
        )
    except Exception:
        log.exception("capture typed note write failed; falling back to raw inbox note")
        return _persist_inbox_fallback(text, source, ts=ts)
    return {
        "id": row["id"],
        "type": routed.type,
        "destination": row["path"],
        "needs_triage": needs_triage,
    }


def _persist_task_capture(capture: Capture, *, ts: str | None, needs_triage: bool) -> dict:
    project = find_project(capture.project)
    host = (vault_dir() / project["path"]) if project is not None else journal_host_for_today(ts)
    tags = _with_needs_triage_tag(capture.tags, needs_triage=needs_triage)
    row = append_task_to_host(
        host,
        text=capture.body,
        due=capture.due,
        scheduled=_date_marker(capture.scheduled),
        recurrence=capture.recurrence,
        priority=capture.priority,
        tags=tags,
        links=capture.related,
        reminder_lead_minutes=capture.reminder_lead_minutes,
        no_reminder=capture.no_reminder,
        review_at=capture.review_at,
    )
    try:
        create_task_due_reminder(
            task_uid=row["uid"],
            task_text=row["text"],
            due=capture.due,
            lead_minutes=capture.reminder_lead_minutes,
            no_reminder=capture.no_reminder,
        )
    except Exception:
        log.exception("capture task reminder creation failed for task %s", row["uid"])
    return {
        "id": row["uid"],
        "type": "task",
        "destination": row["host_path"],
        "needs_triage": needs_triage,
    }


def _persist_journal_capture(
    capture: Capture,
    *,
    ts: str | None,
    source: str,
    needs_triage: bool,
) -> dict:
    at = _capture_local_datetime(ts)
    body = (
        f"{capture.body.rstrip()} #needs-triage"
        if needs_triage and "#needs-triage" not in capture.body
        else capture.body
    )
    result = append_log(at.date().isoformat(), body, at, source=source)
    return {
        "id": result["date"],
        "type": "journal",
        "destination": result["path"],
        "needs_triage": needs_triage,
    }


def _persist_project_update_capture(capture: Capture, *, needs_triage: bool) -> dict | None:
    project = find_project(capture.project)
    if project is None:
        return None
    body = (
        f"{capture.body.rstrip()} #needs-triage"
        if needs_triage and "#needs-triage" not in capture.body
        else capture.body
    )
    updated = append_project_log(project["slug"], body)
    if updated is None:
        return None
    return {
        "id": updated["slug"],
        "type": "project_update",
        "destination": updated["path"],
    }


def _persist_routine_done_capture(capture: Capture, *, ts: str | None) -> dict | None:
    if not capture.routine:
        return None
    day = local_today(_parse_client_datetime(ts))
    updated = complete_routine_completion(capture.routine, date_value=day)
    if updated is None:
        return None
    return {
        "id": updated["slug"],
        "type": "routine_done",
        "routine_slug": updated["slug"],
        "destination": updated["path"],
        "streak": updated["streak"],
    }


def _persist_person_capture(
    capture: Capture,
    *,
    ts: str | None,
    needs_triage: bool,
) -> dict | None:
    if needs_triage:
        return None
    person = find_person(capture.person) or find_person(capture.title)
    interaction_ts = _capture_local_datetime(ts).strftime("%Y-%m-%d %H:%M")
    if person is not None:
        updated = append_interaction(person["slug"], capture.body, ts=interaction_ts)
        if updated is None:
            return None
        return {
            "id": updated["slug"],
            "type": "person",
            "destination": updated["path"],
            "needs_triage": needs_triage,
        }
    if needs_triage:
        return None
    name = (capture.title or "").strip()
    if not name:
        return None
    created = create_person_file(
        name=name,
        interaction_text=capture.body,
        interaction_ts=interaction_ts,
    )
    return {
        "id": created["slug"],
        "type": "person",
        "destination": created["path"],
        "needs_triage": False,
    }


def _persist_quote_capture(
    capture: Capture,
    *,
    raw_text: str,
    needs_triage: bool,
) -> dict | None:
    if needs_triage:
        return None
    source_type, source_ref = _infer_quote_source(raw_text, capture)
    quote = create_quote_file(
        text=capture.body,
        source_type=source_type,
        source_ref=source_ref,
        tags=capture.tags,
    )
    return {
        "id": quote["id"],
        "type": "quote",
        "destination": quote["path"],
        "needs_triage": False,
    }


def _infer_quote_source(raw_text: str, capture: Capture) -> tuple[str, str | None]:
    if _has_source_word(raw_text, "book"):
        book = match_book_in_text(raw_text) or find_book(capture.title)
        return "book", book["slug"] if book is not None else None
    if _has_source_word(raw_text, "podcast"):
        return "podcast", capture.title
    if _has_source_word(raw_text, "article"):
        return "article", capture.title
    if find_book(capture.title) is not None:
        book = find_book(capture.title)
        return "book", book["slug"] if book is not None else None
    return "conversation", capture.title


def _persist_inventory_capture(
    capture: Capture,
    *,
    raw_text: str,
    ts: str | None,
    needs_triage: bool,
) -> dict | None:
    if needs_triage:
        return None
    name = _inventory_name(capture, raw_text)
    if not name:
        return None
    notes = capture.body.strip() if capture.body.strip().casefold() != name.casefold() else None
    item = create_inventory_file(
        name=name,
        acquired=_capture_local_datetime(ts).date().isoformat(),
        status="owned",
        notes=notes,
    )
    return {
        "id": item["id"],
        "type": "inventory",
        "destination": item["path"],
        "needs_triage": False,
    }


def _persist_content_capture(
    capture: Capture,
    *,
    raw_text: str,
    needs_triage: bool,
) -> dict | None:
    title = _content_title(capture, raw_text)
    if not title:
        return None
    item = create_content_file(
        title=title,
        kind=_content_kind(raw_text),
        status="idea",
        domain=capture.domain,
        outline=capture.body,
        needs_triage=needs_triage,
    )
    return {
        "id": item["slug"],
        "type": "content",
        "destination": item["path"],
        "needs_triage": needs_triage,
    }


def _inventory_name(capture: Capture | dict[str, Any], raw_text: str) -> str | None:
    title = _capture_text_field(capture, "title")
    if title:
        return title
    match = re.match(
        r"^\s*add\s+(?P<name>.+?)\s+to\s+(?:my\s+)?inventory\b",
        raw_text,
        re.I,
    )
    if match:
        return match.group("name").strip()
    return _capture_text_field(capture, "body")


def _content_kind(raw_text: str) -> str:
    match = re.match(
        r"^\s*new\s+(?P<kind>video|article|podcast|newsletter|content)\s+idea\b",
        raw_text,
        re.I,
    )
    if not match:
        return "article"
    kind = match.group("kind").lower()
    return "article" if kind == "content" else kind


def _content_title(capture: Capture, raw_text: str) -> str | None:
    title = _capture_text_field(capture, "title")
    if title:
        return title
    match = re.match(
        r"^\s*new\s+(?:video|article|podcast|newsletter|content)\s+idea\s*:?\s*(?P<title>.+?)\s*$",
        raw_text,
        re.I,
    )
    if match:
        return match.group("title").strip()
    return _capture_text_field(capture, "body")


def _capture_text_field(capture: Capture | dict[str, Any], key: str) -> str | None:
    value = capture.get(key) if isinstance(capture, dict) else getattr(capture, key, None)
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _has_source_word(text: str, word: str) -> bool:
    return re.search(rf"\b{re.escape(word)}\b", text, flags=re.IGNORECASE) is not None


def _date_marker(value: str | None) -> str | None:
    if not value:
        return None
    return value[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", value) else value


def _with_needs_triage_tag(tags: list[str], *, needs_triage: bool) -> list[str]:
    if not needs_triage or "needs-triage" in tags:
        return tags
    return [*tags, "needs-triage"]


def _persist_inbox_fallback(text: str, source: str, *, ts: str | None = None) -> dict:
    row = persist_note_capture(
        body=text,
        source=source,
        ts=_capture_local_datetime(ts) if ts else None,
    )
    return {
        "id": row["id"],
        "type": "inbox",
        "destination": row["path"],
        "needs_triage": True,
    }


def _parse_client_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _capture_local_datetime(value: str | None) -> datetime:
    tz = ZoneInfo(get_settings().capture.default_timezone)
    parsed = _parse_client_datetime(value)
    current = parsed or datetime.now(tz)
    if current.tzinfo is None:
        current = current.replace(tzinfo=tz)
    return current.astimezone(tz)


def _has_typed_capture_metadata(capture: Capture) -> bool:
    return capture.type not in {"note", "inbox"}


def _body_with_capture_frontmatter(capture: Capture, *, needs_triage: bool) -> str:
    frontmatter = {
        "capture": capture.model_dump(),
        "needs_triage": needs_triage,
    }
    fm_yaml = yaml.safe_dump(
        frontmatter,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    return f"---\n{fm_yaml}\n---\n\n{capture.body}"
