from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def _capture(**overrides):
    from mastisk.capture.router import Capture

    data = {
        "type": "quote",
        "confidence": 0.96,
        "title": None,
        "body": "The map is not the territory.",
        "domain": None,
        "project": None,
        "person": None,
        "routine": None,
        "due": None,
        "scheduled": None,
        "priority": None,
        "recurrence": None,
        "reminder_lead_minutes": None,
        "no_reminder": False,
        "review_at": None,
        "tags": ["epistemics"],
        "related": [],
        "command_detected": True,
    }
    data.update(overrides)
    return Capture(**data)


def test_scan_library_round_trips_books_quotes_and_thoughts(db, vault_tmp):
    from mastisk.library.sync import scan_library

    book_path = vault_tmp / "library" / "books" / "thinking-in-systems.md"
    book_path.parent.mkdir(parents=True)
    book_path.write_text(
        "---\n"
        "title: Thinking in Systems\n"
        "author: Donella Meadows\n"
        "status: reading\n"
        "rating: 5\n"
        "---\n\n"
        "## Highlights\n"
        "- A system is more than the sum of its parts.\n"
        "- The least obvious part of a system is its function.\n",
        encoding="utf-8",
    )
    quote_path = vault_tmp / "library" / "quotes" / "20260611-systems.md"
    quote_path.parent.mkdir(parents=True)
    quote_path.write_text(
        "---\n"
        "source_type: book\n"
        "source_ref: thinking-in-systems\n"
        "tags:\n"
        "  - systems\n"
        "---\n\n"
        "A system is more than the sum of its parts.\n\n"
        "## Thoughts\n"
        "- 2026-06-11 09:30 This should become a design principle.\n",
        encoding="utf-8",
    )

    result = scan_library()

    assert result["books"] == 1
    assert result["quotes"] == 1
    book = db.execute("SELECT * FROM books WHERE slug = 'thinking-in-systems'").fetchone()
    assert book["title"] == "Thinking in Systems"
    assert book["author"] == "Donella Meadows"
    assert book["status"] == "reading"
    assert book["rating"] == 5
    highlights = db.execute(
        "SELECT text FROM book_highlights WHERE book_slug = ? ORDER BY position",
        ("thinking-in-systems",),
    ).fetchall()
    assert [row["text"] for row in highlights] == [
        "A system is more than the sum of its parts.",
        "The least obvious part of a system is its function.",
    ]
    quote = db.execute("SELECT * FROM quotes WHERE id = '20260611-systems'").fetchone()
    assert quote["source_type"] == "book"
    assert quote["source_ref"] == "thinking-in-systems"
    assert json.loads(quote["tags_json"]) == ["systems"]
    thoughts = db.execute(
        "SELECT ts, text FROM quote_thoughts WHERE quote_id = '20260611-systems'"
    ).fetchall()
    assert [dict(row) for row in thoughts] == [
        {"ts": "2026-06-11 09:30", "text": "This should become a design principle."}
    ]


def test_books_routes_enrich_offline_patch_and_highlight_quote_recovery(
    db, vault_tmp, data_tmp, monkeypatch
):
    async def fake_search(title, author=None, client=None):
        assert title == "Thinking in Systems"
        return {
            "title": "Thinking in Systems",
            "authors": ["Donella Meadows"],
            "cover_url": "https://covers.example/1.jpg",
            "year": 2008,
            "subjects": ["Systems"],
            "ol_work_key": "/works/OL1W",
        }

    monkeypatch.setattr("mastisk.library.sync.openlibrary.search_book", fake_search)
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/books",
            json={"title": "Thinking in Systems", "lookup": True},
        )
        assert created.status_code == 201, created.text
        assert created.json()["author"] == "Donella Meadows"
        assert created.json()["cover_url"] == "https://covers.example/1.jpg"

        patched = client.patch(
            "/api/books/thinking-in-systems",
            json={"status": "finished", "rating": 5, "finished": "2026-06-11"},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == "finished"
        assert patched.json()["rating"] == 5

        highlighted = client.post(
            "/api/books/thinking-in-systems/highlights",
            json={"text": "A system is more than the sum of its parts."},
        )
        assert highlighted.status_code == 201, highlighted.text
        detail = client.get("/api/books/thinking-in-systems").json()
        assert detail["highlights"][0]["quote_id"]
        quote_id = detail["highlights"][0]["quote_id"]
        assert client.get(f"/api/quotes/{quote_id}").json()["source_ref"] == "thinking-in-systems"

        # Simulate crash after book highlight append but before quote file creation.
        db.execute("DELETE FROM quotes WHERE id = ?", (quote_id,))
        quote_file = vault_tmp / "library" / "quotes" / f"{quote_id}.md"
        quote_file.unlink()

        from mastisk.library.sync import scan_library

        scan_library()
        recovered = db.execute(
            "SELECT quote_id FROM book_highlights WHERE book_slug = 'thinking-in-systems'"
        ).fetchone()
        assert recovered["quote_id"]
        assert (vault_tmp / "library" / "quotes" / f"{recovered['quote_id']}.md").exists()

        async def failing_search(title, author=None, client=None):
            raise RuntimeError("offline")

        monkeypatch.setattr("mastisk.library.sync.openlibrary.search_book", failing_search)
        offline = client.post(
            "/api/books",
            json={"title": "Offline Book", "author": "Local Author", "lookup": True},
        )
        assert offline.status_code == 201, offline.text
        assert offline.json()["slug"] == "offline-book"
        assert offline.json()["author"] == "Local Author"


def test_scan_library_tombstones_deleted_book_highlights_without_resurrecting_quotes(
    db, vault_tmp
):
    from mastisk.library.sync import add_book_highlight, create_book_file, scan_library

    book = create_book_file(title="Thinking in Systems", author="Donella Meadows")
    highlight = add_book_highlight(book["slug"], "A system is more than the sum of its parts.")
    quote_id = highlight["quote_id"]
    quote_path = vault_tmp / "library" / "quotes" / f"{quote_id}.md"
    assert quote_path.exists()

    (vault_tmp / book["path"]).unlink()
    quote_path.unlink()

    scan_library()

    assert not quote_path.exists()
    book_row = db.execute("SELECT deleted_at FROM books WHERE slug = ?", (book["slug"],)).fetchone()
    assert book_row["deleted_at"] is not None
    highlight_row = db.execute(
        "SELECT deleted_at FROM book_highlights WHERE book_slug = ?",
        (book["slug"],),
    ).fetchone()
    assert highlight_row["deleted_at"] is not None
    quote_row = db.execute("SELECT deleted_at FROM quotes WHERE id = ?", (quote_id,)).fetchone()
    assert quote_row["deleted_at"] is not None


def test_find_book_slug_shortcut_respects_author(db, vault_tmp):
    from mastisk.library.sync import find_book, scan_books

    book_dir = vault_tmp / "library" / "books"
    book_dir.mkdir(parents=True)
    (book_dir / "becoming.md").write_text(
        "---\n"
        "title: Becoming\n"
        "author: Michelle Obama\n"
        "status: want\n"
        "---\n\n"
        "## Highlights\n",
        encoding="utf-8",
    )
    (book_dir / "becoming-2.md").write_text(
        "---\n"
        "title: Becoming\n"
        "author: Cindy Crawford\n"
        "status: want\n"
        "---\n\n"
        "## Highlights\n",
        encoding="utf-8",
    )
    scan_books()

    assert find_book("Becoming", author="Michelle Obama")["slug"] == "becoming"
    assert find_book("Becoming", author="Cindy Crawford")["slug"] == "becoming-2"
    assert find_book("becoming", author="Cindy Crawford")["slug"] == "becoming-2"


def test_quotes_routes_append_thoughts_file_first(db, vault_tmp, data_tmp):
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/quotes",
            json={
                "text": "Attention is the rarest and purest form of generosity.",
                "source_type": "article",
                "source_ref": "simone-weil",
                "tags": ["attention"],
            },
        )
        assert created.status_code == 201, created.text
        quote_id = created.json()["id"]

        first = client.post(
            f"/api/quotes/{quote_id}/thoughts",
            json={"text": "This is about product surfaces too.", "ts": "2026-06-11 10:00"},
        )
        assert first.status_code == 201, first.text
        second = client.post(
            f"/api/quotes/{quote_id}/thoughts",
            json={"text": "Append-only means no quiet rewrite.", "ts": "2026-06-11 10:05"},
        )
        assert second.status_code == 201, second.text

        detail = client.get(f"/api/quotes/{quote_id}").json()
        assert [thought["text"] for thought in detail["thoughts"]] == [
            "This is about product surfaces too.",
            "Append-only means no quiet rewrite.",
        ]
        file_text = (vault_tmp / "library" / "quotes" / f"{quote_id}.md").read_text(
            encoding="utf-8"
        )
        assert "- 2026-06-11 10:00 This is about product surfaces too." in file_text
        assert "- 2026-06-11 10:05 Append-only means no quiet rewrite." in file_text


def test_scan_quotes_skips_duplicate_source_hash_file(db, vault_tmp, caplog):
    from mastisk.library.sync import dump_quote_file, scan_quotes

    quote_dir = vault_tmp / "library" / "quotes"
    quote_dir.mkdir(parents=True)
    content = dump_quote_file(
        {"source_type": "book", "source_ref": "same-book", "tags": []},
        "Duplicate quote text.",
        [],
    )
    (quote_dir / "20260611-a.md").write_text(content, encoding="utf-8")
    (quote_dir / "20260611-b.md").write_text(content, encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger="mastisk.library.sync"):
        result = scan_quotes()

    assert result == {"upserted": 1}
    rows = db.execute("SELECT id, path FROM quotes WHERE deleted_at IS NULL").fetchall()
    assert len(rows) == 1
    assert "duplicate quote source hash" in caplog.text


def test_create_quote_file_dedupes_concurrent_create_race(db, vault_tmp, monkeypatch):
    from mastisk.library import sync

    original_find = sync._find_quote_by_source_hash

    def slow_miss(*args, **kwargs):
        found = original_find(*args, **kwargs)
        if found is None:
            time.sleep(0.05)
        return found

    monkeypatch.setattr(sync, "_find_quote_by_source_hash", slow_miss)

    def create():
        return sync.create_quote_file(
            text="Race-safe quote.",
            source_type="conversation",
            source_ref="same-source",
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: create(), range(2)))

    assert {result["id"] for result in results} == {results[0]["id"]}
    assert len(list((vault_tmp / "library" / "quotes").glob("*.md"))) == 1
    rows = db.execute("SELECT id FROM quotes WHERE deleted_at IS NULL").fetchall()
    assert len(rows) == 1


def test_kindle_parser_handles_good_bad_bom_and_crlf():
    from mastisk.library.kindle import parse_clippings

    raw = (
        "\ufeffThinking in Systems (Donella Meadows)\r\n"
        "- Your Highlight on page 2 | Location 31-32 | Added on Thursday, June 11, 2026 9:10:00 AM\r\n"
        "\r\n"
        "A system is more than the sum of its parts.\r\n"
        "==========\r\n"
        "Bad block without enough structure\r\n"
        "==========\r\n"
        "Range Book (A. Author)\r\n"
        "- Your Highlight at Loc. 44-45 | Added on Thursday, June 11, 2026 9:20:00 AM\r\n"
        "\r\n"
        "Loc variant works.\r\n"
        "==========\r\n"
    )

    parsed = parse_clippings(raw)

    assert [(item.title, item.author, item.content) for item in parsed.highlights] == [
        ("Thinking in Systems", "Donella Meadows", "A system is more than the sum of its parts."),
        ("Range Book", "A. Author", "Loc variant works."),
    ]
    assert len(parsed.review_blocks) == 1
    assert "Bad block" in parsed.review_blocks[0].raw_block


@pytest.mark.parametrize(
    ("metadata", "content", "expected_highlights", "expected_review_reason"),
    [
        (
            "- Your Highlight on page 2 | Location 31-32 | Added on Thursday, June 11, 2026 9:10:00 AM",
            "A system is more than the sum of its parts.",
            1,
            None,
        ),
        (
            "- Your Note on page 2 | Location 31 | Added on Thursday, June 11, 2026 9:11:00 AM",
            "This reminds me of feedback loops.",
            0,
            "unsupported_note",
        ),
        (
            "- Your Bookmark on page 2 | Location 31 | Added on Thursday, June 11, 2026 9:12:00 AM",
            "",
            0,
            "unsupported_bookmark",
        ),
        (
            "- Your Annotation on page 2 | Location 31 | Added on Thursday, June 11, 2026 9:13:00 AM",
            "Unexpected Kindle export type.",
            0,
            "unsupported_unknown",
        ),
    ],
)
def test_kindle_parser_imports_only_highlights_and_reviews_other_types(
    metadata,
    content,
    expected_highlights,
    expected_review_reason,
):
    from mastisk.library.kindle import parse_clippings

    raw = (
        "Thinking in Systems (Donella Meadows)\n"
        f"{metadata}\n"
        "\n"
        f"{content}\n"
        "==========\n"
    )

    parsed = parse_clippings(raw)

    assert len(parsed.highlights) == expected_highlights
    if expected_review_reason is None:
        assert parsed.review_blocks == []
        assert parsed.highlights[0].content == content
    else:
        assert parsed.highlights == []
        assert len(parsed.review_blocks) == 1
        assert parsed.review_blocks[0].reason == expected_review_reason
        assert parsed.review_blocks[0].parsed_title == "Thinking in Systems"
        assert parsed.review_blocks[0].parsed_author == "Donella Meadows"


def test_kindle_import_is_idempotent_and_review_lifecycle(db, vault_tmp, data_tmp):
    text = (
        "Thinking in Systems (Donella Meadows)\n"
        "- Your Highlight on page 2 | Location 31-32 | Added on Thursday, June 11, 2026 9:10:00 AM\n"
        "\n"
        "A system is more than the sum of its parts.\n"
        "==========\n"
        "Malformed only\n"
        "==========\n"
    )
    with _client(vault_tmp, data_tmp, db) as client:
        first = client.post(
            "/api/import/kindle",
            files={"file": ("My Clippings.txt", text, "text/plain")},
        )
        assert first.status_code == 200, first.text
        assert first.json()["imported"] == 1
        assert first.json()["review_count"] == 1

        second = client.post(
            "/api/import/kindle",
            files={"file": ("My Clippings.txt", text, "text/plain")},
        )
        assert second.status_code == 200, second.text
        assert second.json()["imported"] == 0
        assert second.json()["skipped_duplicates"] == 1

        review = client.get("/api/import/kindle/review").json()
        assert len(review) == 1
        review_id = review[0]["id"]

        retried = client.post(
            f"/api/import/kindle/review/{review_id}/retry-as-quote",
            json={"text": "Recovered clipping", "source_type": "conversation", "tags": ["kindle-review"]},
        )
        assert retried.status_code == 201, retried.text
        assert retried.json()["status"] == "resolved"
        assert client.get("/api/import/kindle/review").json() == []

        third = client.post(
            "/api/import/kindle",
            files={"file": ("My Clippings.txt", "Still malformed", "text/plain")},
        )
        assert third.json()["review_count"] == 1
        review_id = client.get("/api/import/kindle/review").json()[0]["id"]
        dismissed = client.post(f"/api/import/kindle/review/{review_id}/dismiss")
        assert dismissed.status_code == 200, dismissed.text
        assert client.get("/api/import/kindle/review").json() == []


def test_capture_quote_command_creates_quote_with_inferred_source_and_book_match(
    db, vault_tmp, data_tmp
):
    from mastisk.library.sync import create_book_file

    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\nbearer_token = "test-token"\n', encoding="utf-8")
    create_book_file(title="Thinking in Systems", author="Donella Meadows")
    with _client(vault_tmp, data_tmp, db) as client, patch(
        "mastisk.routes.capture.route_capture", new_callable=AsyncMock
    ) as router:
        router.return_value = _capture(
            body="A system is more than the sum of its parts.",
            title="Thinking in Systems",
            tags=["systems"],
        )
        saved = client.post(
            "/api/capture",
            json={
                "text": "save this quote from the book Thinking in Systems: A system is more than the sum of its parts.",
                "source": "watch",
                "ts": "2026-06-11T09:00:00+00:00",
            },
            headers={"Authorization": "Bearer test-token"},
        )

    assert saved.status_code == 201, saved.text
    body = saved.json()
    assert body["type"] == "quote"
    quote = db.execute("SELECT * FROM quotes WHERE id = ?", (body["id"],)).fetchone()
    assert quote["source_type"] == "book"
    assert quote["source_ref"] == "thinking-in-systems"


@pytest.mark.asyncio
async def test_router_prompt_includes_book_context(data_tmp, db, vault_tmp):
    from mastisk.library.sync import create_book_file
    from mastisk.settings import reload_settings

    cfg = data_tmp / "config.toml"
    cfg.write_text('[capture]\ndefault_timezone = "UTC"\n', encoding="utf-8")
    reload_settings()
    create_book_file(title="Thinking in Systems", author="Donella Meadows")

    from mastisk.capture.router import route_capture

    response = ({"text": json.dumps(_capture().model_dump())}, "claude")
    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ) as run_mock:
        await route_capture("save this quote from the book Thinking in Systems: x", "watch", None)

    prompt = run_mock.call_args.args[0]
    assert "Existing books:" in prompt
    assert '"slug": "thinking-in-systems"' in prompt
    assert '"title": "Thinking in Systems"' in prompt


def test_resurfacing_pool_includes_quotes(db, vault_tmp, data_tmp):
    from mastisk.library.sync import create_quote_file

    quote = create_quote_file(
        text="The map is not the territory.",
        source_type="conversation",
        tags=["epistemics"],
    )

    with _client(vault_tmp, data_tmp, db) as client:
        surfaced = client.get("/api/resurface/2026-06-11")

    assert surfaced.status_code == 200, surfaced.text
    assert surfaced.json()["kind"] == "quote"
    assert surfaced.json()["id"] == quote["id"]
    assert surfaced.json()["link"] == f"/library/quotes/{quote['id']}"
