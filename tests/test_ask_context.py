from __future__ import annotations

import asyncio
import json
from datetime import date
from types import SimpleNamespace

from fastapi.testclient import TestClient


def test_ask_conversations_are_persisted_listed_and_reopened(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    async def fake_generate(prompt: str) -> tuple[str, str]:
        return "Use a durable event cursor. [S1]", "test"

    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        first = client.post(
            "/api/ask",
            json={"question": "How should reconnect work?", "mode": "wiki"},
        )
        assert first.status_code == 200
        conversation_id = first.json()["conversation_id"]

        second = client.post(
            "/api/ask",
            json={
                "question": "What changes with live evidence?",
                "mode": "research",
                "conversation_id": conversation_id,
                "messages": [
                    {"role": "user", "content": "How should reconnect work?"},
                    {"role": "assistant", "content": "Use a durable event cursor."},
                ],
            },
        )
        assert second.status_code == 200
        assert second.json()["conversation_id"] == conversation_id

        listing = client.get("/api/ask/conversations")
        assert listing.status_code == 200
        assert listing.json()["conversations"][0]["id"] == conversation_id
        assert listing.json()["conversations"][0]["title"] == "How should reconnect work?"
        assert listing.json()["conversations"][0]["mode"] == "research"
        assert listing.json()["conversations"][0]["message_count"] == 4

        reopened = client.get(f"/api/ask/conversations/{conversation_id}")
        assert reopened.status_code == 200
        assert [message["role"] for message in reopened.json()["messages"]] == [
            "user", "assistant", "user", "assistant",
        ]
        assert reopened.json()["messages"][-1]["response"]["provider"] == "test"

        deleted = client.delete(f"/api/ask/conversations/{conversation_id}")
        assert deleted.status_code == 200
        assert client.get(f"/api/ask/conversations/{conversation_id}").status_code == 404


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
    assert "Source dates: 2026-07-16" in captured["prompt"]
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


def test_research_mode_reports_when_live_web_evidence_is_unavailable(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    captured: dict[str, str] = {}

    async def fake_search_web(_question: str) -> list[dict]:
        return []

    async def fake_generate(prompt: str) -> tuple[str, str]:
        captured["prompt"] = prompt
        return "I could only check Mastisk for this turn.", "test"

    monkeypatch.setattr("mastisk.routes.ask._search_web", fake_search_web)
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={"question": "Research current agent governance.", "mode": "research"},
        )

    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "wiki"
    assert body["research_status"] == "unavailable"
    assert body["coverage"].get("web", 0) == 0
    assert "Live web search returned no usable evidence" in captured["prompt"]
    assert "Live web results are included" not in captured["prompt"]


def test_research_reserves_context_for_web_before_large_wiki_hits(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    for index in range(12):
        db.execute(
            """INSERT INTO articles
                 (id, kind, title, slug, summary, body_md, confidence)
               VALUES (?, 'Concept', ?, ?, ?, ?, 0.7)""",
            (
                f"approval-{index}",
                f"Approval system {index}",
                f"approval-{index}",
                "Human approval for tool-using agents",
                "Approval evidence " * 500,
            ),
        )
    captured: dict[str, str] = {}

    async def fake_search_web(_question: str) -> list[dict]:
        return [{
            "id": "https://example.com/live-approval",
            "kind": "web",
            "title": "Live approval guidance",
            "href": "https://example.com/live-approval",
            "excerpt": "Current guidance",
            "content": "This live evidence must survive the context budget.",
            "untrusted": True,
        }]

    async def fake_generate(prompt: str) -> tuple[str, str]:
        captured["prompt"] = prompt
        return "The web evidence is present.", "test"

    monkeypatch.setattr("mastisk.routes.ask._search_web", fake_search_web)
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={
                "question": "Research human approval for tool-using agents.",
                "mode": "research",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["coverage"]["web"] == 1
    assert any(source["kind"] == "web" for source in body["retrieved_sources"])
    assert "This live evidence must survive the context budget." in captured["prompt"]


def test_web_search_retries_instruction_heavy_questions_as_keywords(
    monkeypatch,
) -> None:
    import asyncio

    calls: list[str] = []

    class FakeWriter:
        async def _fetch_public_web_results(self, query: str) -> list[dict]:
            calls.append(query)
            if len(calls) == 1:
                return []
            return [{
                "title": "Human approval for tool-using agents",
                "url": "https://example.com/approval",
                "snippet": "Use explicit approval gates for consequential actions.",
            }]

        async def _fetch_public_web_excerpt(self, _url: str) -> str:
            return "Approval gates should be inspectable and attributable."

    monkeypatch.setattr("mastisk.agents.blog_writer.BlogWriter", FakeWriter)

    from mastisk.routes.ask import _search_web

    rows = asyncio.run(_search_web(
        "Research the latest practical guidance on human approval for "
        "tool-using agents and compare it with my wiki.",
    ))

    assert calls == [
        "Research the latest practical guidance on human approval for "
        "tool-using agents and compare it with my wiki.",
        "practical guidance human approval tool using agents 2026",
    ]
    assert rows[0]["kind"] == "web"
    assert rows[0]["content"] == "Approval gates should be inspectable and attributable."
    assert rows[0]["retrieved_at"] == date.today().isoformat()


def test_live_web_recall_honors_disabled_web_search_setting(monkeypatch) -> None:
    import asyncio

    monkeypatch.setattr(
        "mastisk.settings.get_settings",
        lambda: SimpleNamespace(blog=SimpleNamespace(web_search_enabled=False)),
    )

    from mastisk.routes.ask import _search_web

    assert asyncio.run(_search_web("current private-memory systems")) == []


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


def test_intelligent_ask_expands_recall_and_checks_live_freshness(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    db.execute(
        """INSERT INTO notes
             (slug, path, body, body_sha256, source, created_at, classified_at)
           VALUES ('dated-retry-learning', '_notes/inbox/dated-retry-learning.md',
                   'The cobalt-hourglass retry design was retired after provider APIs changed.',
                   'dated-retry-learning-hash', 'cli', '2025-03-04T09:30:00+00:00',
                   '2025-03-04T09:31:00+00:00')"""
    )
    captured: dict[str, str] = {}

    async def fake_plan(_req):
        return {
            "queries": ["cobalt-hourglass retry design"],
            "needs_live_web": True,
            "web_query": "current provider retry API guidance",
            "freshness_reason": "Provider APIs change quickly.",
        }

    async def fake_search_web(question: str) -> list[dict]:
        captured["web_query"] = question
        return [{
            "id": "https://example.com/current-retries",
            "kind": "web",
            "title": "Current retry guidance",
            "href": "https://example.com/current-retries",
            "excerpt": "August 2026 guidance.",
            "content": "Current providers require idempotency keys for retry safety.",
            "untrusted": True,
        }]

    async def fake_generate(prompt: str) -> tuple[str, str]:
        captured["prompt"] = prompt
        return "The old design was retired; use current idempotency guidance. [S2] [S3]", "test"

    async def fake_approve(_req, query: str) -> str:
        return query

    monkeypatch.setattr(
        "mastisk.routes.ask._plan_intelligent_recall", fake_plan,
    )
    monkeypatch.setattr(
        "mastisk.routes.ask._approve_public_web_query", fake_approve,
    )
    monkeypatch.setattr("mastisk.routes.ask._search_web", fake_search_web)
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={"question": "What should I use now?", "intelligent": True},
        )

    assert response.status_code == 200
    body = response.json()
    assert captured["web_query"] == "current provider retry API guidance"
    assert any(source["id"] == "1" for source in body["retrieved_sources"])
    assert any(source["kind"] == "web" for source in body["retrieved_sources"])
    assert body["mode"] == "research"
    assert body["research_status"] == "available"
    assert "2025-03-04" in captured["prompt"]
    assert "Provider APIs change quickly" in captured["prompt"]
    note_source = next(
        source for source in body["retrieved_sources"] if source["id"] == "1"
    )
    assert note_source["updated_at"] is None


def test_intelligent_ask_uses_model_planned_queries_with_bounded_planner_time(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    db.execute(
        """INSERT INTO notes
             (slug, path, body, body_sha256, source, created_at)
           VALUES ('quasar-saddle', '_notes/inbox/quasar-saddle.md',
                   'Quasar-saddle was the verified fix for duplicate completion receipts.',
                   'quasar-saddle-hash', 'cli', '2026-01-02T10:00:00+00:00')"""
    )
    observed: dict = {}

    async def fake_intelligence(prompt: str, **kwargs):
        observed["planner_prompt"] = prompt
        observed["planner_kwargs"] = kwargs
        return ({
            "text": """```json
{"queries":["quasar-saddle duplicate completion receipts"],"needs_live_web":false,"web_query":"","freshness_reason":"This is a historical implementation decision."}
```""",
        }, "test")

    async def fake_generate(prompt: str) -> tuple[str, str]:
        observed["answer_prompt"] = prompt
        return "Use the verified quasar-saddle fix. [S2]", "test"

    monkeypatch.setattr(
        "mastisk.bridges.intelligence.run_intelligence", fake_intelligence,
    )
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={"question": "What prior fix applies here?", "intelligent": True},
        )

    assert response.status_code == 200
    assert any(
        source["id"] == "1" for source in response.json()["retrieved_sources"]
    )
    assert observed["planner_kwargs"] == {
        "timeout_s": 60,
        "json_object": True,
        "classification": True,
        "provider_order": ("anthropic", "claude", "ollama"),
    }
    assert f"Today is {date.today().isoformat()}" in observed["planner_prompt"]
    assert "historical implementation decision" in observed["answer_prompt"]


def test_intelligent_ask_fails_closed_when_planner_omits_public_web_query(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    searched: list[str] = []

    async def fake_plan(_req):
        return {
            "queries": ["private retry incident"],
            "needs_live_web": True,
            "web_query": "",
            "freshness_reason": "Current evidence may matter.",
        }

    async def fake_search_web(question: str) -> list[dict]:
        searched.append(question)
        return []

    async def fake_generate(_prompt: str) -> tuple[str, str]:
        return "No current public evidence was checked.", "test"

    monkeypatch.setattr("mastisk.routes.ask._plan_intelligent_recall", fake_plan)
    monkeypatch.setattr("mastisk.routes.ask._search_web", fake_search_web)
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={
                "question": "Check /Users/private/repo incident secret-customer-42",
                "intelligent": True,
            },
        )

    assert response.status_code == 200
    assert searched == []
    assert response.json()["research_status"] == "unavailable"


def test_intelligent_ask_rejects_planner_echo_of_raw_private_task(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    private_task = "Check /Users/private/repo incident secret-customer-42"
    searched: list[str] = []

    async def fake_plan(_req):
        return {
            "queries": ["private retry incident"],
            "needs_live_web": True,
            "web_query": private_task,
            "freshness_reason": "Current evidence may matter.",
        }

    async def fake_search_web(question: str) -> list[dict]:
        searched.append(question)
        return []

    async def fake_generate(_prompt: str) -> tuple[str, str]:
        return "The private task stayed local.", "test"

    monkeypatch.setattr("mastisk.routes.ask._plan_intelligent_recall", fake_plan)
    monkeypatch.setattr("mastisk.routes.ask._search_web", fake_search_web)
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={"question": private_task, "intelligent": True},
        )

    assert response.status_code == 200
    assert searched == []
    assert response.json()["research_status"] == "unavailable"


def test_public_web_query_requires_independent_tool_free_privacy_review(
    monkeypatch,
) -> None:
    from mastisk.routes.ask import AskRequest, _approve_public_web_query

    observed: dict = {}

    async def fake_intelligence(prompt: str, **kwargs):
        observed["prompt"] = prompt
        observed["kwargs"] = kwargs
        return {
            "text": json.dumps({
                "safe": True,
                "public_query": "OpenAI Responses API retry guidance 2026",
            }),
        }, "claude"

    monkeypatch.setattr(
        "mastisk.bridges.intelligence.run_intelligence", fake_intelligence,
    )
    req = AskRequest(
        question="In /Users/private/repo, is our OpenAI retry guidance current?",
        intelligent=True,
    )

    approved = asyncio.run(
        _approve_public_web_query(req, "current OpenAI retry API guidance")
    )

    assert approved == "OpenAI Responses API retry guidance 2026"
    assert "/Users/private/repo" in observed["prompt"]
    assert observed["kwargs"] == {
        "timeout_s": 60,
        "classification": True,
        "json_object": True,
        "provider_order": ("anthropic", "claude", "ollama"),
    }


def test_public_web_query_rejects_exact_echo_from_private_history(monkeypatch) -> None:
    from mastisk.routes.ask import AskMessage, AskRequest, _approve_public_web_query

    called = False

    async def fake_intelligence(*_args, **_kwargs):
        nonlocal called
        called = True
        return {"text": "{}"}, "test"

    monkeypatch.setattr(
        "mastisk.bridges.intelligence.run_intelligence", fake_intelligence,
    )
    req = AskRequest(
        question="Is this current?",
        intelligent=True,
        messages=[AskMessage(role="user", content="secret-customer-42 rollout")],
    )

    approved = asyncio.run(
        _approve_public_web_query(req, "secret-customer-42 rollout")
    )

    assert approved == ""
    assert called is False


def test_browser_page_does_not_mislabel_dropped_live_results_as_available(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    captured: dict = {}

    async def fake_plan(_req):
        return {
            "queries": ["current retry guidance"],
            "needs_live_web": True,
            "web_query": "public retry guidance 2026",
            "freshness_reason": "The API may have changed.",
        }

    async def fake_approve(_req, query: str) -> str:
        return query

    async def fake_search_web(_query: str) -> list[dict]:
        return [{
            "id": "https://example.com/too-short",
            "kind": "web",
            "title": "Short result",
            "href": "https://example.com/too-short",
            "excerpt": "tiny",
            "content": "tiny",
            "untrusted": True,
        }]

    async def fake_generate(prompt: str) -> tuple[str, str]:
        captured["prompt"] = prompt
        return "No usable live evidence was included.", "test"

    monkeypatch.setattr("mastisk.routes.ask._plan_intelligent_recall", fake_plan)
    monkeypatch.setattr(
        "mastisk.routes.ask._approve_public_web_query", fake_approve,
    )
    monkeypatch.setattr("mastisk.routes.ask._search_web", fake_search_web)
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={
                "question": "What is current?",
                "intelligent": True,
                "page_url": "https://private.example/current-page",
                "page_title": "Current private page",
                "page_content": "Browser-provided context " * 20,
            },
        )

    assert response.status_code == 200
    assert response.json()["research_status"] == "unavailable"
    assert "Live web search returned no usable evidence" in captured["prompt"]


def test_intelligent_ask_keeps_user_question_ahead_of_six_planned_queries(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    db.execute(
        """INSERT INTO notes
             (slug, path, body, body_sha256, source, created_at)
           VALUES ('actual-question-hit', '_notes/inbox/actual-question-hit.md',
                   'obsidian-lemur is the exact durable answer.',
                   'actual-question-hit-hash', 'cli', '2026-08-09T10:00:00+00:00')"""
    )

    async def fake_plan(_req):
        return {
            "queries": [f"irrelevant-planner-query-{index}" for index in range(6)],
            "needs_live_web": False,
            "web_query": "",
            "freshness_reason": "Historical local decision.",
        }

    async def fake_generate(_prompt: str) -> tuple[str, str]:
        return "Use obsidian-lemur. [S2]", "test"

    monkeypatch.setattr("mastisk.routes.ask._plan_intelligent_recall", fake_plan)
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={"question": "What is obsidian-lemur?", "intelligent": True},
        )

    assert response.status_code == 200
    assert any(
        source["id"] == "1" for source in response.json()["retrieved_sources"]
    )


def test_intelligent_ask_keeps_history_with_six_planned_queries(
    db, monkeypatch,
) -> None:
    from mastisk.app import create_app

    db.execute(
        """INSERT INTO notes
             (slug, path, body, body_sha256, source, created_at)
           VALUES ('history-only-hit', '_notes/inbox/history-only-hit.md',
                   'heliotrope-sse was the prior reconnect incident.',
                   'history-only-hit-hash', 'cli', '2026-08-09T10:00:00+00:00')"""
    )

    async def fake_plan(_req):
        return {
            "queries": [f"irrelevant-planner-query-{index}" for index in range(6)],
            "needs_live_web": False,
            "web_query": "",
            "freshness_reason": "Historical local decision.",
        }

    async def fake_generate(_prompt: str) -> tuple[str, str]:
        return "Use the prior reconnect incident. [S2]", "test"

    monkeypatch.setattr("mastisk.routes.ask._plan_intelligent_recall", fake_plan)
    monkeypatch.setattr("mastisk.routes.ask._generate_answer", fake_generate)

    with TestClient(create_app()) as client:
        response = client.post(
            "/api/ask",
            json={
                "question": "What should happen after that?",
                "intelligent": True,
                "messages": [{
                    "role": "user",
                    "content": "What did heliotrope-sse teach us?",
                }],
            },
        )

    assert response.status_code == 200
    assert any(
        source["id"] == "1" for source in response.json()["retrieved_sources"]
    )


def test_answer_generation_uses_tool_free_provider_chain(monkeypatch) -> None:
    from mastisk.routes.ask import _generate_answer

    observed: dict = {}

    async def fake_intelligence(prompt: str, **kwargs):
        observed["prompt"] = prompt
        observed["kwargs"] = kwargs
        return {"text": "grounded answer"}, "claude"

    monkeypatch.setattr(
        "mastisk.bridges.intelligence.run_intelligence", fake_intelligence,
    )

    answer, provider = asyncio.run(_generate_answer("untrusted source content"))

    assert (answer, provider) == ("grounded answer", "claude")
    assert observed["kwargs"] == {
        "timeout_s": 180,
        "classification": True,
        "provider_order": ("anthropic", "claude", "ollama"),
    }
