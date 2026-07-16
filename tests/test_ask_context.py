from __future__ import annotations

from fastapi.testclient import TestClient


def test_ask_uses_conversation_history_to_retrieve_raw_notes(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    captured: dict[str, str] = {}

    async def fake_generate(prompt: str) -> tuple[str, str]:
        captured["prompt"] = prompt
        return "Reconnect from the last event id. [S2]", "test"

    monkeypatch.setattr(
        "mastisk.routes.ask._generate_answer", fake_generate, raising=False,
    )

    with TestClient(create_app()) as client:
        note = client.post(
            "/api/notes",
            json={
                "text": (
                    "Long-lived SSE streams need auth refresh plus durable reconnect "
                    "from the last event id when a token expires mid-generation."
                ),
                "source": "pwa",
            },
        )
        assert note.status_code == 201

        response = client.post(
            "/api/ask",
            json={
                "question": "What should happen after that?",
                "messages": [
                    {
                        "role": "user",
                        "content": "What does my wiki say about long-lived SSE streams?",
                    },
                ],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["answer"] == "Reconnect from the last event id. [S2]"
    assert body["provider"] == "test"
    assert any(source["kind"] == "note" for source in body["sources"])
    assert body["coverage"]["note"] >= 1
    assert "durable reconnect from the last event id" in captured["prompt"]
    assert "What does my wiki say about long-lived SSE streams?" in captured["prompt"]


def test_ask_hydrates_article_body_instead_of_only_its_summary(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    db.execute(
        """INSERT INTO articles
             (id, kind, title, slug, summary, body_md, confidence)
           VALUES ('control-plane', 'Synthesis', 'Capability needs a control plane',
                   'control-plane', 'Systems need inspectable control.',
                   'The unresolved choice is local harness, vendor platform, or independent institution.',
                   0.8)"""
    )
    captured: dict[str, str] = {}

    async def fake_generate(prompt: str) -> tuple[str, str]:
        captured["prompt"] = prompt
        return "The choice has three homes. [S2]", "test"

    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={
                "question": (
                    "Where can the unresolved control choice live in Capability "
                    "needs a control plane?"
                ),
            },
        )

    assert response.status_code == 200
    assert "local harness, vendor platform, or independent institution" in captured["prompt"]
    assert any(
        source["id"] == "control-plane" and source["kind"] == "article"
        for source in response.json()["retrieved_sources"]
    )


def test_personal_profile_is_always_in_context_before_large_search_results(
    db, vault_tmp, monkeypatch,
) -> None:
    from mastisk.app import create_app

    profile_dir = vault_tmp / "_self"
    profile_dir.mkdir()
    (profile_dir / "identity.md").write_text(
        "I optimize for inspectable systems and dislike hidden control layers.",
        encoding="utf-8",
    )
    for index in range(12):
        db.execute(
            """INSERT INTO articles
                 (id, kind, title, slug, summary, body_md, confidence)
               VALUES (?, 'Concept', ?, ?, ?, ?, 0.7)""",
            (
                f"agent-{index}",
                f"Agent system {index}",
                f"agent-{index}",
                "Agent system design",
                "Agent " + ("system context " * 500),
            ),
        )
    captured: dict[str, str] = {}

    async def fake_generate(prompt: str) -> tuple[str, str]:
        captured["prompt"] = prompt
        return "Use inspectable boundaries. [S2]", "test"

    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={"question": "How should I design an agent system?"},
        )

    assert response.status_code == 200
    assert "dislike hidden control layers" in captured["prompt"]
    sources = response.json()["sources"]
    assert any(source["id"] == "self:identity" for source in sources)
    assert response.json()["coverage"]["profile"] == 1


def test_chat_searches_canonical_journal_body_not_only_mirror_metadata(
    db, vault_tmp, monkeypatch,
) -> None:
    from mastisk.app import create_app

    journal_dir = vault_tmp / "journal"
    journal_dir.mkdir()
    journal_path = journal_dir / "2026-07-16.md"
    journal_path.write_text(
        "## Log\nThe cobalt-hourglass experiment made the queue easier to inspect.",
        encoding="utf-8",
    )
    db.execute(
        """INSERT INTO journal_days (date, path, log_count)
           VALUES ('2026-07-16', 'journal/2026-07-16.md', 1)"""
    )
    captured: dict[str, str] = {}

    async def fake_generate(prompt: str) -> tuple[str, str]:
        captured["prompt"] = prompt
        return "The experiment improved inspectability. [S2]", "test"

    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={"question": "What happened in the cobalt-hourglass experiment?"},
        )

    assert response.status_code == 200
    assert "made the queue easier to inspect" in captured["prompt"]
    assert response.json()["coverage"]["journal"] == 1
    assert any(source["kind"] == "journal" for source in response.json()["sources"])


def test_research_mode_adds_live_web_evidence_without_claiming_an_action(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    captured: dict[str, str] = {}

    async def fake_search_web(_question: str) -> list[dict]:
        return [{
            "id": "https://example.com/agent-governance",
            "kind": "web",
            "title": "Agent governance field report",
            "href": "https://example.com/agent-governance",
            "excerpt": "A July 2026 field report.",
            "content": "The report requires independent action receipts before trust.",
            "untrusted": True,
        }]

    async def fake_generate(prompt: str) -> tuple[str, str]:
        captured["prompt"] = prompt
        return "The live report requires action receipts. [S2]", "test"

    monkeypatch.setattr("mastisk.routes.ask._search_web", fake_search_web)
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={
                "question": "Research current approaches to agent governance.",
                "mode": "research",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "research"
    assert body["coverage"]["web"] == 1
    assert any(source["href"] == "https://example.com/agent-governance" for source in body["sources"])
    assert "Live web results are included" in captured["prompt"]
    assert "Never claim you saved, created, emailed" in captured["prompt"]


def test_retrieved_context_is_not_mislabeled_as_cited_evidence(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    async def fake_generate(_prompt: str) -> tuple[str, str]:
        return "I do not have enough evidence to answer that.", "test"

    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post("/api/ask", json={"question": "What is missing?"})

    assert response.status_code == 200
    body = response.json()
    assert body["sources"] == []
    assert body["cites"] == []
    assert len(body["retrieved_sources"]) >= 1
    assert body["retrieved_sources"][0]["cited"] is False
