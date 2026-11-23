from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

DEEP_WORK_NOTES = """---
title: Deep Work
author: Cal Newport
coreader_id: 7F3A9C2E-1B44-4D0A-9E55-0A1B2C3D4E5F
source: coreader
---

# Deep Work

## Chapter 1

> The ability to perform deep work is becoming increasingly rare.
> It is also becoming increasingly valuable.

Scarcity plus value is the whole argument. Compare with my notes on attention residue.

[Open in Coreader](coreader://open?book=7F3A9C2E-1B44-4D0A-9E55-0A1B2C3D4E5F&a=a1)

> Clarity about what matters provides clarity about what does not.

![figure](assets/fig-1.png)
"""


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A small Coreader library, with the real home directory out of reach."""
    root = tmp_path / "Coreader"
    deep = root / "Books" / "deep-work"
    (deep / "assets").mkdir(parents=True)
    (deep / "notes.md").write_text(DEEP_WORK_NOTES, encoding="utf-8")
    (deep / "book.json").write_text(
        json.dumps({"id": "7F3A9C2E-1B44-4D0A-9E55-0A1B2C3D4E5F", "title": "Deep Work"}),
        encoding="utf-8",
    )
    (deep / "deep-work.epub").write_bytes(b"PK\x03\x04 not a real epub")
    (deep / "assets" / "fig-1.png").write_bytes(b"\x89PNG")

    # No front matter at all: identity comes from book.json.
    plain = root / "Books" / "plain-article"
    plain.mkdir()
    (plain / "notes.md").write_text(
        "> Hyperbolic discounting explains the snooze button.\n\nTry this on Monday.\n",
        encoding="utf-8",
    )
    (plain / "book.json").write_text(
        json.dumps({"id": "plain-id-42", "title": "On Procrastination", "author": "A. Writer"}),
        encoding="utf-8",
    )

    # A book that has been added but not annotated yet has no notes.md.
    (root / "Books" / "unread").mkdir()
    (root / "README.md").write_text("layout notes", encoding="utf-8")

    monkeypatch.setattr(
        "mastisk.coreader.config.coreader_app_config_path",
        lambda: tmp_path / "no-coreader-app" / "config.json",
    )
    monkeypatch.setattr(
        "mastisk.coreader.config.default_library_root", lambda: tmp_path / "no-default-library",
    )
    return root


def _configure(data_tmp: Path, *, enabled: bool, library_root: Path | str = "") -> None:
    import tomli_w

    (data_tmp / "config.toml").write_bytes(
        tomli_w.dumps(
            {"coreader": {"enabled": enabled, "library_root": str(library_root)}}
        ).encode()
    )


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(path.relative_to(root)): (path.stat().st_mtime_ns, path.stat().st_size)
        for path in sorted(root.rglob("*")) if path.is_file()
    }


def _rows(db) -> dict[str, dict]:
    return {
        row["slug"]: dict(row)
        for row in db.execute("SELECT * FROM coreader_books WHERE deleted_at IS NULL")
    }


def test_connector_is_off_by_default(db, data_tmp, library) -> None:
    from mastisk.coreader.sync import scan_coreader, status

    result = scan_coreader()

    assert result["enabled"] is False
    assert result["upserted"] == 0
    assert _rows(db) == {}
    assert status()["enabled"] is False


def test_scan_indexes_notes_in_place_without_copying_or_touching_them(
    db, data_tmp, vault_tmp, library,
) -> None:
    from mastisk.coreader.sync import scan_coreader

    _configure(data_tmp, enabled=True, library_root=library)
    library_before = _tree(library)
    vault_before = _tree(vault_tmp)

    result = scan_coreader()

    assert result["upserted"] == 2
    assert result["skipped"] is None
    rows = _rows(db)
    assert set(rows) == {"deep-work", "plain-article"}
    deep = rows["deep-work"]
    assert deep["id"] == "7F3A9C2E-1B44-4D0A-9E55-0A1B2C3D4E5F"
    assert deep["title"] == "Deep Work"
    assert deep["author"] == "Cal Newport"
    # Relative, so the row stays valid when the library or the database moves.
    assert deep["path"] == "Books/deep-work/notes.md"
    assert deep["highlight_count"] == 2
    assert rows["plain-article"]["id"] == "plain-id-42"
    assert rows["plain-article"]["title"] == "On Procrastination"
    assert rows["plain-article"]["author"] == "A. Writer"
    # One copy: nothing appears in the vault, nothing changes in the library.
    assert _tree(vault_tmp) == vault_before
    assert _tree(library) == library_before


def test_rescan_skips_unchanged_files_and_picks_up_edits(db, data_tmp, library) -> None:
    from mastisk.coreader.sync import scan_coreader
    from mastisk.db import queries as q

    _configure(data_tmp, enabled=True, library_root=library)
    scan_coreader()

    again = scan_coreader()
    assert (again["upserted"], again["unchanged"]) == (0, 2)

    notes = library / "Books" / "deep-work" / "notes.md"
    notes.write_text(
        DEEP_WORK_NOTES + "\n> Embrace boredom as zanzibar practice.\n", encoding="utf-8",
    )
    edited = scan_coreader()
    assert (edited["upserted"], edited["unchanged"]) == (1, 1)

    hits = [hit for hit in q.search_all(db, "zanzibar") if hit["kind"] == "coreader"]
    assert [hit["title"] for hit in hits] == ["Deep Work"]
    assert "zanzibar" in hits[0]["excerpt"]
    assert hits[0]["link_target"] == (
        "coreader://open?book=7F3A9C2E-1B44-4D0A-9E55-0A1B2C3D4E5F"
    )


def test_deleted_book_is_tombstoned_but_an_unreachable_library_is_not(
    db, data_tmp, library, tmp_path,
) -> None:
    import shutil

    from mastisk.coreader.sync import scan_coreader

    _configure(data_tmp, enabled=True, library_root=library)
    scan_coreader()

    shutil.rmtree(library / "Books" / "plain-article")
    assert scan_coreader()["removed"] == 1
    assert set(_rows(db)) == {"deep-work"}

    # iCloud signed out or the folder not mounted: keep what is indexed.
    library.rename(tmp_path / "elsewhere")
    result = scan_coreader()
    assert result["skipped"].startswith("library not readable")
    assert set(_rows(db)) == {"deep-work"}


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_unreadable_notes_keep_the_last_index_and_ask_falls_back_to_it(
    db, data_tmp, library,
) -> None:
    from mastisk.coreader.sync import read_notes, scan_coreader

    _configure(data_tmp, enabled=True, library_root=library)
    scan_coreader()
    notes = library / "Books" / "deep-work" / "notes.md"
    # Stands in for a file iCloud evicted, which a daemon cannot always fetch.
    notes.write_text(DEEP_WORK_NOTES + "\nchanged\n", encoding="utf-8")
    notes.chmod(0o000)
    try:
        result = scan_coreader()
        fallback = read_notes("Books/deep-work/notes.md")
    finally:
        notes.chmod(0o644)

    assert result["unreadable"] == 1
    assert result["removed"] == 0
    assert set(_rows(db)) == {"deep-work", "plain-article"}
    assert "increasingly rare" in fallback


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read a mode-000 file")
def test_evicted_notes_are_requested_from_icloud_and_then_indexed(
    db, data_tmp, library, monkeypatch,
) -> None:
    from mastisk.coreader import sync

    _configure(data_tmp, enabled=True, library_root=library)
    sync.scan_coreader()
    notes = library / "Books" / "deep-work" / "notes.md"
    notes.write_text(DEEP_WORK_NOTES + "\n> A line added on another device.\n", encoding="utf-8")
    notes.chmod(0o000)
    asked: list[Path] = []

    def fetch(path: Path) -> bool:
        # Stands in for `brctl download`: the file becomes readable.
        asked.append(path)
        path.chmod(0o644)
        return True

    monkeypatch.setattr(sync, "_request_download", fetch)
    result = sync.scan_coreader()

    assert asked == [notes]
    assert result["unreadable"] == 0
    assert result["upserted"] == 1
    assert "added on another device" in sync.read_notes("Books/deep-work/notes.md")


def test_download_is_only_requested_for_icloud_paths(tmp_path, monkeypatch) -> None:
    from mastisk.coreader import sync

    called: list[list[str]] = []
    monkeypatch.setattr(sync.subprocess, "run", lambda *a, **k: called.append(a[0]))
    assert sync._request_download(tmp_path / "notes.md") is False
    assert called == []


def test_bad_front_matter_and_duplicate_ids_do_not_break_the_scan(
    db, data_tmp, library,
) -> None:
    from mastisk.coreader.sync import scan_coreader

    broken = library / "Books" / "broken-yaml"
    broken.mkdir()
    (broken / "notes.md").write_text(
        "---\ntitle: [unclosed\n---\n\n> Still a highlight.\n", encoding="utf-8",
    )
    twin = library / "Books" / "deep-work-copy"
    twin.mkdir()
    (twin / "notes.md").write_text(DEEP_WORK_NOTES, encoding="utf-8")
    _configure(data_tmp, enabled=True, library_root=library)

    scan_coreader()

    rows = _rows(db)
    assert rows["broken-yaml"]["id"] == "broken-yaml"
    assert rows["broken-yaml"]["title"] == "Broken Yaml"
    assert "deep-work-copy" not in rows
    assert "deep-work" in rows


def test_read_notes_stays_inside_the_library(db, data_tmp, library, tmp_path) -> None:
    from mastisk.coreader.sync import read_notes

    _configure(data_tmp, enabled=True, library_root=library)
    secret = tmp_path / "secret.md"
    secret.write_text("do not leak", encoding="utf-8")
    (library / "Books" / "deep-work" / "escape.md").symlink_to(secret)

    assert "increasingly rare" in read_notes("Books/deep-work/notes.md")
    assert read_notes("../secret.md") == ""
    assert read_notes(str(secret)) == ""
    assert read_notes("Books/deep-work/escape.md") == ""


def test_config_changes_apply_without_reloading_settings(
    db, data_tmp, library, tmp_path, monkeypatch,
) -> None:
    from mastisk.coreader.config import load_config
    from mastisk.coreader.sync import scan_coreader

    assert load_config().enabled is False
    assert load_config().root_source == "default"

    # No explicit folder: follow the one the Coreader app records for itself.
    app_config = tmp_path / "coreader-app" / "config.json"
    app_config.parent.mkdir()
    app_config.write_text(json.dumps({"libraryRoot": str(library)}), encoding="utf-8")
    monkeypatch.setattr("mastisk.coreader.config.coreader_app_config_path", lambda: app_config)
    _configure(data_tmp, enabled=True)

    cfg = load_config()
    assert (cfg.enabled, cfg.library_root, cfg.root_source) == (True, library, "coreader-config")
    assert scan_coreader()["upserted"] == 2

    # Switching it off removes the notes from search; no restart, no reload.
    _configure(data_tmp, enabled=False)
    assert scan_coreader()["removed"] == 2
    assert _rows(db) == {}

    (data_tmp / "config.toml").write_text("[coreader]\nenabled = 'yes please'\n")
    assert load_config().enabled is False


def test_ask_reads_the_notes_file_itself_and_cites_the_deep_link(
    db, data_tmp, library, monkeypatch,
) -> None:
    from mastisk.app import create_app
    from mastisk.coreader.sync import scan_coreader

    _configure(data_tmp, enabled=True, library_root=library)
    scan_coreader()
    # Written after the scan: Ask must see the file, not a stored copy.
    notes = library / "Books" / "deep-work" / "notes.md"
    notes.write_text(
        DEEP_WORK_NOTES + "\nLate thought: shallow work expands to fill the day.\n",
        encoding="utf-8",
    )
    prompts: list[str] = []

    async def fake_generate(prompt: str) -> tuple[str, str]:
        prompts.append(prompt)
        return "Deep work is rare and valuable. [S1]", "test"

    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    response = TestClient(create_app()).post(
        "/api/ask", json={"question": "What did I highlight about deep work?", "mode": "wiki"},
    )

    assert response.status_code == 200
    assert 'kind="coreader"' in prompts[0]
    assert "Title: Deep Work" in prompts[0]
    assert "URL: coreader://open?book=7F3A9C2E-1B44-4D0A-9E55-0A1B2C3D4E5F" in prompts[0]
    assert "The ability to perform deep work is becoming increasingly rare." in prompts[0]
    assert "shallow work expands to fill the day" in prompts[0]


def test_api_reports_state_and_rescans(db, data_tmp, library) -> None:
    from mastisk.app import create_app

    client = TestClient(create_app())
    assert client.get("/api/coreader").json()["enabled"] is False

    _configure(data_tmp, enabled=True, library_root=library)
    assert client.post("/api/coreader/scan").json()["upserted"] == 2

    state = client.get("/api/coreader").json()
    assert state["enabled"] is True
    assert state["library_root"] == str(library)
    assert state["root_source"] == "config.toml"
    assert state["library_exists"] is True
    assert state["books"] == 2
    books = client.get("/api/coreader/books").json()
    assert [book["title"] for book in books] == ["Deep Work", "On Procrastination"]
    assert books[1]["deep_link"] == "coreader://open?book=plain-id-42"
    assert "search_text" not in books[0]


def test_cli_connect_status_disconnect(db, data_tmp, library, monkeypatch) -> None:
    import tomllib

    from mastisk.cli import app

    monkeypatch.delenv("MASTISK_URL", raising=False)
    (data_tmp / "config.toml").write_text('ollama_cloud_key = "keep-me"\n')
    runner = CliRunner()

    connected = runner.invoke(app, ["coreader", "connect", "--library", str(library), "--json"])
    assert connected.exit_code == 0, connected.output
    report = json.loads(connected.stdout)
    assert (report["enabled"], report["books"]) == (True, 2)
    assert report["last_scan"]["upserted"] == 2
    config = tomllib.loads((data_tmp / "config.toml").read_text())
    assert config["ollama_cloud_key"] == "keep-me"
    assert config["coreader"] == {"library_root": str(library.resolve()), "enabled": True}

    status = runner.invoke(app, ["coreader", "status", "--json"])
    assert json.loads(status.stdout)["books"] == 2

    disconnected = runner.invoke(app, ["coreader", "disconnect", "--json"])
    report = json.loads(disconnected.stdout)
    assert (report["enabled"], report["books"]) == (False, 0)

    missing = runner.invoke(app, ["coreader", "connect", "--library", str(library / "nope")])
    assert missing.exit_code == 1


def test_cli_refuses_when_the_daemon_is_on_another_machine(
    db, data_tmp, library, monkeypatch,
) -> None:
    from mastisk.cli import app

    monkeypatch.setenv("MASTISK_URL", "http://elsewhere:5555")
    result = CliRunner().invoke(app, ["coreader", "connect", "--library", str(library)])

    assert result.exit_code == 1
    assert not (data_tmp / "config.toml").exists()
