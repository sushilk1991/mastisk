"""Regression tests for route-level input bounds and foreign-key handling."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings
    reload_settings()
    from mastisk.app import create_app
    with TestClient(create_app()) as c:
        yield c


def _seed_article(db, article_id: str = "known-article") -> str:
    db.execute(
        "INSERT INTO articles (id, kind, title, slug) VALUES (?, 'Concept', ?, ?)",
        (article_id, article_id.title(), article_id),
    )
    db.commit()
    return article_id


# ─────────────────────────────── bounded limits ───────────────────────────────

_LIMIT_URLS = [
    "/api/articles?limit={}",
    "/api/feed?limit={}",
    "/api/podcasts?limit={}",
    "/api/search?q_param=x&limit={}",
    "/api/search/x?limit={}",
    "/api/sources?limit={}",
    "/api/jobs?limit={}",
    "/api/digest?top_n={}",
]


@pytest.mark.parametrize("url", _LIMIT_URLS)
@pytest.mark.parametrize("value", [-1, 0, 1_000_000])
def test_limit_parameters_are_bounded(client, url, value):
    assert client.get(url.format(value)).status_code == 422


@pytest.mark.parametrize("url", _LIMIT_URLS)
def test_limit_parameters_accept_defaults_and_caller_values(client, url):
    # 100 is the largest value any in-repo caller sends (PWA podcasts list),
    # except digest's top_n, which no caller overrides.
    value = 5 if "top_n" in url else 100
    assert client.get(url.format(value)).status_code == 200
    assert client.get(url.split("?")[0] + ("?q_param=x" if "q_param" in url else "")).status_code == 200


# ─────────────────────────────── unknown articles ───────────────────────────────

def test_pin_and_unpin_unknown_article_return_404(client, db):
    assert client.post("/api/articles/nope/pin").status_code == 404
    assert client.delete("/api/articles/nope/pin").status_code == 404
    assert db.execute("SELECT COUNT(*) AS n FROM signals").fetchone()["n"] == 0

    article_id = _seed_article(db)
    assert client.post(f"/api/articles/{article_id}/pin").json() == {"ok": True}
    assert client.delete(f"/api/articles/{article_id}/pin").json() == {"ok": True}


def test_signal_unknown_article_returns_404_and_unknown_kind_422(client, db):
    unknown = client.post("/api/signals", json={"kind": "opened", "article_id": "nope"})
    assert unknown.status_code == 404
    assert client.post("/api/signals", json={"kind": "bogus"}).status_code == 422

    article_id = _seed_article(db)
    known = client.post("/api/signals", json={"kind": "opened", "article_id": article_id})
    assert known.json() == {"ok": True}
    # article_id stays optional.
    assert client.post("/api/signals", json={"kind": "skipped"}).json() == {"ok": True}


@pytest.mark.parametrize("verdict", ["yes", "no", "clear"])
def test_digest_feedback_unknown_article_returns_404(client, verdict):
    r = client.post(
        "/api/digest/feedback",
        json={"article_id": "nope", "digest_date": "2026-01-02", "verdict": verdict},
    )
    assert r.status_code == 404


def test_digest_feedback_known_article_still_records(client, db):
    article_id = _seed_article(db)
    r = client.post(
        "/api/digest/feedback",
        json={"article_id": article_id, "digest_date": "2026-01-02", "verdict": "yes"},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "verdict": "yes"}


# ─────────────────────────────── feeds / graph ───────────────────────────────

def test_list_feeds_shape_has_no_items_ever(client):
    assert client.post("/api/feeds", json={"url": "https://example.com/rss"}).status_code == 200
    feeds = client.get("/api/feeds").json()["feeds"]
    assert [f["url"] for f in feeds] == ["https://example.com/rss"]
    assert feeds[0]["enabled"] is True
    assert "items_ever" not in feeds[0]


def test_graph_formats_expire_independently(client, db, monkeypatch):
    from types import SimpleNamespace

    from mastisk.routes import graph_route

    graph_route._graph_cache.update(full=b"", compact=b"")
    now = {"t": 1_000.0}
    monkeypatch.setattr(
        graph_route, "time", SimpleNamespace(monotonic=lambda: now["t"]),
    )

    assert client.get("/api/graph/compact").json()["stats"]["pages"] == 0
    _seed_article(db)

    # Refreshing "full" inside compact's TTL must not extend compact's expiry.
    now["t"] += graph_route.GRAPH_CACHE_TTL_SEC - 10
    assert client.get("/api/graph").json()["stats"]["pages"] == 1
    assert client.get("/api/graph/compact").json()["stats"]["pages"] == 0  # still fresh

    now["t"] += 20  # past compact's TTL, inside full's
    assert client.get("/api/graph/compact").json()["stats"]["pages"] == 1


def test_open_questions_are_paged_with_totals(client, db):
    for n in range(3):
        db.execute(
            "INSERT INTO articles (id, slug, kind, title, updated_at) VALUES (?, ?, 'Source', ?, ?)",
            (f"a{n}", f"a{n}", f"Article {n}", f"2026-11-0{n + 1} 10:00:00"),
        )
        db.execute(
            "INSERT INTO article_sections (article_id, idx, heading, body, kind) "
            "VALUES (?, 0, 'Open questions', ?, 'open')",
            (f"a{n}", f"<p>question {n}</p>"),
        )
    db.commit()

    first = client.get("/api/open-questions?limit=2").json()
    rest = client.get("/api/open-questions?limit=2&offset=2").json()

    assert first["total"] == 3 and first["articles"] == 3
    assert [q["article_id"] for q in first["questions"]] == ["a2", "a1"]
    assert [q["article_id"] for q in rest["questions"]] == ["a0"]
    assert client.get("/api/open-questions?limit=0").status_code == 422
