from __future__ import annotations

from datetime import date

from fastapi import APIRouter, HTTPException, Query

from mastisk.google_calendar import (
    CalendarAuthError,
    CalendarSyncError,
    calendar_status,
    clear_calendar_connection,
    events_for_day,
    sync_calendar,
)

router = APIRouter(tags=["calendar"])


@router.get("/calendar/status")
def get_calendar_status() -> dict[str, str | None]:
    return calendar_status()


@router.get("/calendar/today")
def get_today_calendar(date_: date | None = Query(default=None, alias="date")) -> dict:
    day = date_ or date.today()
    status = calendar_status()
    return {
        "date": day.isoformat(),
        "status": status,
        "events": events_for_day(day) if status["status"] == "connected" else [],
    }


@router.post("/calendar/sync")
def force_calendar_sync() -> dict:
    try:
        result = sync_calendar()
    except CalendarAuthError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except CalendarSyncError as e:
        raise HTTPException(status_code=502, detail=str(e))
    return {"ok": True, **result, "status": calendar_status()}


@router.delete("/calendar/connection")
def delete_calendar_connection() -> dict[str, bool]:
    clear_calendar_connection()
    return {"ok": True}
