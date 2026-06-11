from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_today_calendar_load_is_isolated_from_primary_batch() -> None:
    source = (ROOT / "frontend/src/components/DashboardViews.tsx").read_text(
        encoding="utf-8"
    )
    load_body = source.split("async function load()", 1)[1].split("useEffect", 1)[0]
    primary_batch = load_body.split("await Promise.all([", 1)[1].split("]);", 1)[0]

    assert "api.calendar.today" not in primary_batch
    assert "async function loadCalendarToday()" in source
    assert "setCalendarErr" in source
    assert "calendarErr={calendarErr}" in source
