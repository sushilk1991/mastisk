"""Web-clip ingestion endpoints used by the browser extension."""
from __future__ import annotations

import hashlib

import pytest
from fastapi import HTTPException

from mastisk.routes.ingest import WebClipRequest, get_web_clip_status, ingest_web_clip

URL = "https://example.com/essay"


def _clip(**overrides) -> WebClipRequest:
    base = {
        "url": URL,
        "title": "An Essay",
        "content": "Long-form body of the essay. " * 20,
    }
    base.update(overrides)
    return WebClipRequest(**base)


@pytest.mark.asyncio
async def test_web_clip_creates_source_and_queues_compiler(db):
    result = await ingest_web_clip(_clip())
    assert result["status"] == "queued"
    src_id = result["source_id"]
    assert src_id == hashlib.sha256(URL.encode()).hexdigest()[:16]

    row = db.execute("SELECT * FROM sources WHERE id=?", (src_id,)).fetchone()
    assert row is not None
    assert row["kind"] == "web"
    assert row["url"] == URL
    assert row["title"] == "An Essay"

    job = db.execute(
        "SELECT * FROM jobs WHERE agent='compiler' AND kind='compile'"
    ).fetchone()
    assert job is not None

    raw = open(row["raw_path"]).read()
    assert raw.startswith("# An Essay")
    assert URL in raw


@pytest.mark.asyncio
async def test_web_clip_dedupes_by_url(db):
    first = await ingest_web_clip(_clip())
    dup = await ingest_web_clip(_clip())
    # Second call returns current status instead of re-inserting.
    import json
    body = json.loads(dup.body)
    assert body["source_id"] == first["source_id"]
    count = db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    assert count == 1
    jobs = db.execute("SELECT COUNT(*) c FROM jobs").fetchone()["c"]
    assert jobs == 1


@pytest.mark.asyncio
async def test_web_clip_selection_gets_distinct_source(db):
    await ingest_web_clip(_clip())
    sel = await ingest_web_clip(_clip(selection="a striking passage"))
    assert sel["status"] == "queued"
    count = db.execute("SELECT COUNT(*) c FROM sources").fetchone()["c"]
    assert count == 2


@pytest.mark.asyncio
async def test_web_clip_rejects_empty_content(db):
    with pytest.raises(Exception):
        await ingest_web_clip(_clip(content="", selection=None))


@pytest.mark.asyncio
async def test_web_clip_rejects_non_http_url(db):
    with pytest.raises(ValueError):
        _clip(url="file:///etc/passwd")


@pytest.mark.asyncio
async def test_status_reports_article_when_compiled(db):
    result = await ingest_web_clip(_clip())
    src_id = result["source_id"]

    status = get_web_clip_status(src_id)
    assert status["status"] == "queued"
    assert status["article"] is None

    db.execute(
        """INSERT INTO articles (id, kind, title, slug, body_md)
           VALUES ('art-1', 'source', 'An Essay', 'an-essay', 'body')"""
    )
    db.execute(
        "INSERT INTO article_sources (article_id, source_id) VALUES ('art-1', ?)",
        (src_id,),
    )
    status = get_web_clip_status(src_id)
    assert status["status"] == "done"
    assert status["article"]["id"] == "art-1"
    assert status["article"]["slug"] == "an-essay"


def test_status_404_for_unknown_source(db):
    with pytest.raises(HTTPException) as exc:
        get_web_clip_status("nope")
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_web_clip_reenqueues_orphaned_pending_source(db):
    # A source row with no compile job (crash between insert and enqueue)
    # must self-heal on re-clip instead of staying pending forever.
    db.execute(
        "INSERT INTO sources (id, kind, url, title) VALUES (?, 'web', ?, 'An Essay')",
        (hashlib.sha256(URL.encode()).hexdigest()[:16], URL),
    )
    import json
    res = await ingest_web_clip(_clip())
    body = json.loads(res.body)
    assert body["status"] == "queued"
    jobs = db.execute("SELECT COUNT(*) c FROM jobs WHERE agent='compiler'").fetchone()["c"]
    assert jobs == 1


@pytest.mark.asyncio
async def test_web_clip_rejects_non_http_hero(db):
    req = _clip(hero_image_url="javascript:alert(1)")
    assert req.hero_image_url is None
    req = _clip(hero_image_url="https://example.com/img.png")
    assert req.hero_image_url == "https://example.com/img.png"


@pytest.mark.asyncio
async def test_ask_survives_hostile_page_title(db, monkeypatch):
    async def fake_chat(prompt: str, cheap: bool = True) -> str:
        return "answer"

    monkeypatch.setattr("mastisk.bridges.ollama_bridge.chat", fake_chat)

    from mastisk.routes.ask import AskRequest, ask

    # A title that is a lone double-quote used to raise an FTS5 syntax error.
    result = await ask(
        AskRequest(question="what is this?", page_title='"', page_content="body")
    )
    assert result["answer"] == "answer"


@pytest.mark.asyncio
async def test_ask_tags_article_hits(db, monkeypatch):
    async def fake_chat(prompt: str, cheap: bool = True) -> str:
        return "answer"

    monkeypatch.setattr("mastisk.bridges.ollama_bridge.chat", fake_chat)

    db.execute(
        """INSERT INTO articles (id, kind, title, slug, summary, body_md)
           VALUES ('art-fts', 'concept', 'Attention Economics', 'attention-economics',
                   'Scarce attention', 'body about attention economics')"""
    )
    db.execute(
        """INSERT INTO tasks (uid, host_path, line_number, text, checked, status,
                              tags_json, links_json)
           VALUES ('task-att', 'journal/x.md', 1, 'Read attention paper', 0, 'open',
                   '[]', '[]')"""
    )

    from mastisk.routes.ask import AskRequest, ask

    result = await ask(AskRequest(question="attention"))
    kinds = {h["title"]: h["is_article"] for h in result["hits"]}
    assert kinds.get("Attention Economics") is True
    assert any(v is False for v in kinds.values())


@pytest.mark.asyncio
async def test_ask_includes_browser_page_context(db, monkeypatch):
    captured: dict[str, str] = {}

    async def fake_chat(prompt: str, cheap: bool = True) -> str:
        captured["prompt"] = prompt
        return "answer"

    monkeypatch.setattr("mastisk.bridges.ollama_bridge.chat", fake_chat)

    from mastisk.routes.ask import AskRequest, ask

    result = await ask(
        AskRequest(
            question="how does this relate to my notes?",
            page_url="https://example.com/essay",
            page_title="An Essay",
            page_content="The essay argues that attention is the scarcest resource.",
        )
    )
    assert result["answer"] == "answer"
    prompt = captured["prompt"]
    assert "Currently reading in the browser: An Essay" in prompt
    assert "attention is the scarcest resource" in prompt
    assert "draw parallels" in prompt
