from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_ask_prompt_includes_personal_os_mirror_context(db, monkeypatch):
    db.execute(
        """INSERT INTO tasks
             (uid, host_path, line_number, text, checked, status, due,
              tags_json, links_json)
           VALUES ('task-water', 'journal/2026-06-13.md', 1,
                   'Change water filter', 0, 'open', '2026-06-13T14:00:00',
                   '[]', '[]')"""
    )
    db.execute(
        """INSERT INTO people (slug, name, facts_json, path)
           VALUES ('anjali-rao', 'Anjali Rao', '{"note":"filter owner"}',
                   'people/anjali-rao.md')"""
    )
    db.execute(
        """INSERT INTO books (slug, path, title, author, status, summary)
           VALUES ('designing-data-intensive-applications',
                   'library/books/ddia.md',
                   'Designing Data-Intensive Applications',
                   'Martin Kleppmann', 'reading', 'Systems book')"""
    )

    captured: dict[str, str] = {}

    async def fake_chat(prompt: str, cheap: bool = True) -> str:
        captured["prompt"] = prompt
        return "answer"

    monkeypatch.setattr("mastisk.bridges.ollama_bridge.chat", fake_chat)

    from mastisk.routes.ask import AskRequest, ask

    result = await ask(
        AskRequest(question="Anjali water filter Kleppmann")
    )

    assert result["answer"] == "answer"
    prompt = captured["prompt"]
    assert "## Task: Change water filter" in prompt
    assert "## Person: Anjali Rao" in prompt
    assert "## Book: Designing Data-Intensive Applications" in prompt
    assert "Personal OS context" in prompt

