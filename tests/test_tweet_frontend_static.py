from __future__ import annotations

from pathlib import Path


def _source(name: str) -> str:
    return Path(f"frontend/src/components/{name}").read_text(encoding="utf-8")


def test_tweet_writer_is_compose_first_and_progressively_discloses_sources() -> None:
    source = _source("TweetThreadsView.tsx")

    assert '<form className="tw-compose"' in source
    assert 'htmlFor="tweet-theme"' in source
    assert '<details className="tw-compose-sources">' in source
    assert 'type="radio"' in source
    assert "loadMore" in source
    assert "refresh(true)" in source
    assert "current.filter((row) => !refreshedIds.has(row.id))" in source
    assert 'aria-live="polite"' in source
    assert "rows.length === 0" in source
    assert 'className="tw-list-skeleton"' in source
    assert "style={{" not in source


def test_tweet_detail_has_clear_navigation_and_editorial_workspace() -> None:
    source = _source("TweetThreadView.tsx")

    assert "onClick={() => onNavigate('tweets')}" in source
    assert 'className="tw-workspace-grid"' in source
    assert 'className="tw-thread-canvas"' in source
    assert 'className="tw-editorial-rail"' in source
    assert 'aria-live="polite"' in source
    assert 'aria-expanded={formOpen}' in source
    assert 'data-near-limit={tweet.length > 250 ? \'true\' : \'false\'}' in source
    assert 'data-over-limit={tweet.length > 280 ? \'true\' : \'false\'}' in source
    assert "thread.thread.join('\\n\\n')" in source


def test_tweet_dates_treat_sqlite_timestamps_as_utc() -> None:
    source = Path("frontend/src/date.ts").read_text(encoding="utf-8")

    assert "SQLITE_UTC" in source
    assert "`${value.replace(' ', 'T')}Z`" in source


def test_tweet_ui_motion_is_scoped_and_responsive() -> None:
    css = Path("frontend/src/styles/mastisk.css").read_text(encoding="utf-8")
    section = css.split("/* ---------- Tweet thread workshop ---------- */", 1)[1].split(
        "/* Markdown content rendered", 1
    )[0]

    assert "container-type: inline-size" in section
    assert "@container tw-compose" in section
    assert ".tw-compose-note { display: none; }" in section
    assert "grid-template-columns: 1fr;\n  align-items: stretch;" in section
    assert "@media (prefers-reduced-motion: reduce)" in section
    assert "transition: all" not in section
