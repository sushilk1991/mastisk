from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]


def _client(vault_tmp, data_tmp, db):
    from mastisk.settings import reload_settings

    reload_settings()
    from mastisk.app import create_app

    return TestClient(create_app())


def test_registry_derives_placeholders_from_prompt_constants(db) -> None:
    from mastisk.agents import notetaker
    from mastisk.agents.registry import agent_definition, required_placeholders

    definition = agent_definition("notetaker")
    slot = definition.slots[0]

    assert slot.module_attr == "CLASSIFY_PROMPT"
    assert slot.default_prompt == notetaker.CLASSIFY_PROMPT
    assert slot.required_placeholders == ("article_ids", "body", "identity")
    assert required_placeholders(notetaker.CLASSIFY_PROMPT) == ("article_ids", "body", "identity")


def test_profile_scan_roundtrip_invalid_override_falls_back_and_keeps_skills(db, vault_tmp) -> None:
    from mastisk.agents.registry import resolve_prompt
    from mastisk.agents.studio import scan_agent_profiles, scan_agent_skills

    skill_dir = vault_tmp / "_agents" / "skills"
    skill_dir.mkdir(parents=True)
    (skill_dir / "be-specific.md").write_text(
        "---\n"
        "name: Be specific\n"
        "description: Add concrete nouns.\n"
        "tags:\n"
        "  - voice\n"
        "---\n\n"
        "Prefer named mechanisms over generic phrasing.\n",
        encoding="utf-8",
    )
    profile = vault_tmp / "_agents" / "notetaker.md"
    profile.write_text(
        "---\n"
        "enabled: true\n"
        "model: null\n"
        "skills:\n"
        "  - be-specific\n"
        "---\n\n"
        "CUSTOM {identity} {article_ids} {body}\n",
        encoding="utf-8",
    )

    scan_agent_skills()
    scan_agent_profiles()

    resolved = resolve_prompt("notetaker", "classify", "DEFAULT {body}")
    assert resolved.startswith("CUSTOM")
    assert "## Additional instructions (skill: Be specific)" in resolved
    assert "Prefer named mechanisms" in resolved

    profile.write_text(
        "---\n"
        "enabled: true\n"
        "skills:\n"
        "  - be-specific\n"
        "---\n\n"
        "BROKEN {identity} {article_ids}\n",
        encoding="utf-8",
    )
    scan_agent_profiles([profile])

    row = db.execute("SELECT invalid, invalid_reason FROM agent_profiles WHERE agent_id='notetaker'").fetchone()
    assert row["invalid"] == 1
    assert "missing {body}" in row["invalid_reason"]
    resolved = resolve_prompt("notetaker", "classify", "DEFAULT {body}")
    assert resolved.startswith("DEFAULT {body}")
    assert "Prefer named mechanisms" in resolved


def test_agent_profile_slot_override_horizontal_rule_roundtrips(vault_tmp) -> None:
    from mastisk.agents.studio import dump_agent_profile_file, parse_agent_profile_file

    path = vault_tmp / "_agents" / "synthesizer.md"
    path.parent.mkdir(parents=True)
    written = dump_agent_profile_file(
        {
            "enabled": True,
            "skills": [],
            "slot_overrides": {
                "critic": "First instruction\n---\nSecond instruction",
            },
        },
        "PRIMARY {identity} {articles}",
    )
    path.write_text(written, encoding="utf-8")

    parsed = parse_agent_profile_file(path)
    rewritten = dump_agent_profile_file(parsed["frontmatter"], parsed["prompt_override"])

    assert rewritten == written


def test_agent_skill_description_horizontal_rule_roundtrips(vault_tmp) -> None:
    from mastisk.agents.studio import dump_agent_skill_file, parse_agent_skill_file

    path = vault_tmp / "_agents" / "skills" / "rule-test.md"
    path.parent.mkdir(parents=True)
    written = dump_agent_skill_file(
        {
            "name": "Rule test",
            "description": "Before\n---\nAfter",
            "tags": [],
        },
        "Keep the instruction intact.",
    )
    path.write_text(written, encoding="utf-8")

    parsed = parse_agent_skill_file(path)
    rewritten = dump_agent_skill_file(parsed["frontmatter"], parsed["body"])

    assert rewritten == written


def test_prompt_validation_rejects_unknown_fields_and_skill_braces_are_safe(db, vault_tmp) -> None:
    from mastisk.agents.registry import resolve_prompt
    from mastisk.agents.studio import scan_agent_profiles, scan_agent_skills

    skill_dir = vault_tmp / "_agents" / "skills"
    skill_dir.mkdir(parents=True)
    (skill_dir / "json-style.md").write_text(
        "---\nname: JSON style\n---\n\nUse JSON examples like {\"tone\": \"direct\"}.\n",
        encoding="utf-8",
    )
    profile = vault_tmp / "_agents" / "notetaker.md"
    profile.write_text(
        "---\n"
        "enabled: true\n"
        "skills:\n"
        "  - json-style\n"
        "---\n\n"
        "SAFE {identity} {article_ids} {body}\n",
        encoding="utf-8",
    )
    scan_agent_skills()
    scan_agent_profiles()

    template = resolve_prompt("notetaker", "classify", "DEFAULT {body}")
    rendered = template.format(identity="", article_ids="", body="")
    assert '{"tone": "direct"}' in rendered

    profile.write_text(
        "---\nenabled: true\n---\n\n"
        "BROKEN {identity} {article_ids} {body} {unexpected}\n",
        encoding="utf-8",
    )
    scan_agent_profiles([profile])

    row = db.execute(
        "SELECT invalid, invalid_reason FROM agent_profiles WHERE agent_id='notetaker'"
    ).fetchone()
    assert row["invalid"] == 1
    assert "unknown {unexpected}" in row["invalid_reason"]
    assert resolve_prompt("notetaker", "classify", "DEFAULT {body}") == "DEFAULT {body}"


def test_skill_name_braces_do_not_break_prompt_formatting(db, vault_tmp) -> None:
    from mastisk.agents.registry import resolve_prompt
    from mastisk.agents.studio import scan_agent_profiles

    db.execute(
        """INSERT INTO agent_skills (slug, path, name, tags_json, body)
           VALUES ('brace-name', '_agents/skills/brace-name.md', 'Use {foo} style', '[]', 'Keep it direct.')"""
    )
    profile_dir = vault_tmp / "_agents"
    profile_dir.mkdir(parents=True)
    (profile_dir / "notetaker.md").write_text(
        "---\n"
        "enabled: true\n"
        "skills:\n"
        "  - brace-name\n"
        "---\n\n"
        "SAFE {identity} {article_ids} {body}\n",
        encoding="utf-8",
    )

    scan_agent_profiles()

    template = resolve_prompt("notetaker", "classify", "DEFAULT {body}")
    rendered = template.format(identity="", article_ids="", body="")

    assert "## Additional instructions (skill: Use {foo} style)" in rendered
    assert "Keep it direct." in rendered


def test_agent_routes_validate_placeholders_and_tag_skills(db, vault_tmp, data_tmp) -> None:
    with _client(vault_tmp, data_tmp, db) as client:
        skill = client.post(
            "/api/agent-skills",
            json={
                "name": "Sharper JSON",
                "slug": "sharper-json",
                "description": "Keep classifier output strict.",
                "tags": ["json"],
                "body": "Return only the requested object.",
            },
        )
        assert skill.status_code == 201, skill.text

        bad = client.put(
            "/api/agents/notetaker/profile",
            json={"prompt_override": "BROKEN {identity} {article_ids}"},
        )
        assert bad.status_code == 422, bad.text
        assert "{body}" in bad.text

        unknown = client.put(
            "/api/agents/notetaker/profile",
            json={"prompt_override": "BROKEN {identity} {article_ids} {body} {extra}"},
        )
        assert unknown.status_code == 422, unknown.text
        assert "unknown {extra}" in unknown.text

        good = client.put(
            "/api/agents/notetaker/profile",
            json={
                "enabled": False,
                "skills": ["sharper-json"],
                "prompt_override": "OK {identity} {article_ids} {body}",
            },
        )
        assert good.status_code == 200, good.text
        body = good.json()
        assert body["profile"]["enabled"] is False
        assert body["profile"]["skills"] == ["sharper-json"]

        detail = client.get("/api/agents/notetaker")
        assert detail.status_code == 200, detail.text
        payload = detail.json()
        assert payload["agent"]["id"] == "notetaker"
        assert payload["slots"][0]["override"] == "OK {identity} {article_ids} {body}"
        assert payload["skills"][0]["slug"] == "sharper-json"
        assert (vault_tmp / "_agents" / "notetaker.md").exists()


def test_agent_skill_create_rejects_unsafe_name_characters(db, vault_tmp, data_tmp) -> None:
    with _client(vault_tmp, data_tmp, db) as client:
        response = client.post(
            "/api/agent-skills",
            json={"slug": "brace-name", "name": "Use {foo} style", "body": "Keep it direct."},
        )

    assert response.status_code == 422, response.text
    assert "name" in response.text


def test_agent_profile_rejects_attribute_and_index_placeholders(db, vault_tmp, data_tmp) -> None:
    with _client(vault_tmp, data_tmp, db) as client:
        attr = client.put(
            "/api/agents/notetaker/profile",
            json={"prompt_override": "BROKEN {identity} {article_ids} {body.text}"},
        )
        index = client.put(
            "/api/agents/notetaker/profile",
            json={"prompt_override": "BROKEN {identity} {article_ids} {body[0]}"},
        )

    assert attr.status_code == 422, attr.text
    assert "body.text" in attr.text
    assert index.status_code == 422, index.text
    assert "body[0]" in index.text


def test_agent_profile_disable_succeeds_with_existing_invalid_override(db, vault_tmp, data_tmp) -> None:
    profile = vault_tmp / "_agents" / "notetaker.md"
    profile.parent.mkdir(parents=True)
    profile.write_text(
        "---\n"
        "enabled: true\n"
        "skills: []\n"
        "---\n\n"
        "BROKEN {identity} {article_ids}\n",
        encoding="utf-8",
    )

    with _client(vault_tmp, data_tmp, db) as client:
        listed = client.get("/api/agents")
        assert listed.status_code == 200, listed.text
        disabled = client.put("/api/agents/notetaker/profile", json={"enabled": False})

    assert disabled.status_code == 200, disabled.text
    payload = disabled.json()["profile"]
    assert payload["enabled"] is False
    assert payload["invalid"] is True
    assert "missing {body}" in payload["invalid_reason"]
    assert "BROKEN {identity} {article_ids}" in profile.read_text(encoding="utf-8")


def test_agent_scanners_flag_malformed_files_and_continue(db, vault_tmp, data_tmp) -> None:
    from mastisk.agents.studio import scan_agent_profiles, scan_agent_skills

    agent_dir = vault_tmp / "_agents"
    skill_dir = agent_dir / "skills"
    skill_dir.mkdir(parents=True)
    (agent_dir / "notetaker.md").write_text(
        "---\n"
        "enabled: [\n"
        "---\n\n"
        "BROKEN\n",
        encoding="utf-8",
    )
    (skill_dir / "bad-skill.md").write_text(
        "---\n"
        "name: [\n"
        "---\n\n"
        "Bad body\n",
        encoding="utf-8",
    )
    (skill_dir / "good-skill.md").write_text(
        "---\n"
        "name: Good skill\n"
        "---\n\n"
        "Good body\n",
        encoding="utf-8",
    )

    scan_agent_profiles()
    scan_agent_skills()

    profile = db.execute(
        "SELECT invalid, invalid_reason FROM agent_profiles WHERE agent_id='notetaker'"
    ).fetchone()
    bad_skill = db.execute(
        "SELECT invalid, invalid_reason FROM agent_skills WHERE slug='bad-skill'"
    ).fetchone()
    good_skill = db.execute(
        "SELECT invalid, body FROM agent_skills WHERE slug='good-skill'"
    ).fetchone()
    assert profile["invalid"] == 1
    assert "parse" in profile["invalid_reason"].lower()
    assert bad_skill["invalid"] == 1
    assert "parse" in bad_skill["invalid_reason"].lower()
    assert good_skill["invalid"] == 0
    assert good_skill["body"] == "Good body"


def test_agent_get_routes_read_mirror_without_scanning(db, vault_tmp, data_tmp) -> None:
    from mastisk.agents.studio import scan_agent_profiles, scan_agent_skills

    agent_dir = vault_tmp / "_agents"
    skill_dir = agent_dir / "skills"
    skill_dir.mkdir(parents=True)
    (agent_dir / "notetaker.md").write_text(
        "---\n"
        "enabled: true\n"
        "skills: []\n"
        "---\n\n"
        "OK {identity} {article_ids} {body}\n",
        encoding="utf-8",
    )
    (skill_dir / "mirror-skill.md").write_text(
        "---\n"
        "name: Mirror skill\n"
        "---\n\n"
        "Mirror body\n",
        encoding="utf-8",
    )
    scan_agent_profiles()
    scan_agent_skills()

    with (
        patch(
            "mastisk.routes.agents_route.scan_agent_profiles",
            side_effect=AssertionError("GET routes must not scan profiles"),
            create=True,
        ),
        patch(
            "mastisk.routes.agents_route.scan_agent_skills",
            side_effect=AssertionError("GET routes must not scan skills"),
            create=True,
        ),
        _client(vault_tmp, data_tmp, db) as client,
    ):
        agents = client.get("/api/agents")
        detail = client.get("/api/agents/notetaker")
        skills = client.get("/api/agent-skills")
        skill = client.get("/api/agent-skills/mirror-skill")

    assert agents.status_code == 200, agents.text
    assert detail.status_code == 200, detail.text
    assert skills.status_code == 200, skills.text
    assert skill.status_code == 200, skill.text
    assert skill.json()["name"] == "Mirror skill"


def test_agent_skill_crud_routes_are_file_first(db, vault_tmp, data_tmp) -> None:
    with _client(vault_tmp, data_tmp, db) as client:
        created = client.post(
            "/api/agent-skills",
            json={"slug": "voice-tightener", "name": "Voice tightener", "body": "Cut filler."},
        )
        assert created.status_code == 201, created.text
        path = vault_tmp / "_agents" / "skills" / "voice-tightener.md"
        assert path.exists()
        assert "Cut filler." in path.read_text(encoding="utf-8")

        patched = client.put(
            "/api/agent-skills/voice-tightener",
            json={"name": "Voice tightener", "description": "Shorten prose.", "tags": ["voice"], "body": "Cut filler twice."},
        )
        assert patched.status_code == 200, patched.text
        assert patched.json()["description"] == "Shorten prose."

        listed = client.get("/api/agent-skills")
        assert listed.status_code == 200, listed.text
        assert [row["slug"] for row in listed.json()["skills"]] == ["voice-tightener"]

        deleted = client.delete("/api/agent-skills/voice-tightener")
        assert deleted.status_code == 200, deleted.text
        assert not path.exists()
        assert client.get("/api/agent-skills").json()["skills"] == []


def test_notetaker_override_via_api_reaches_bridge_call(db, vault_tmp, data_tmp) -> None:
    from mastisk.agents.base import enqueue
    from mastisk.agents.notetaker import Notetaker
    from mastisk.db.queries import insert_note
    from mastisk.paths import ensure_dirs, notes_inbox_dir, vault_dir

    ensure_dirs()
    with _client(vault_tmp, data_tmp, db) as client:
        skill = client.post(
            "/api/agent-skills",
            json={"slug": "strict-style", "name": "Strict style", "body": "Skill body reached runtime."},
        )
        assert skill.status_code == 201, skill.text
        saved = client.put(
            "/api/agents/notetaker/profile",
            json={
                "skills": ["strict-style"],
                "prompt_override": "OVERRIDE {identity}\n{article_ids}\n{body}",
            },
        )
        assert saved.status_code == 200, saved.text

    inbox = notes_inbox_dir()
    p = inbox / "143522-agent-studio.md"
    p.write_text("agent studio test body", encoding="utf-8")
    note_id = insert_note(
        db,
        slug="143522-agent-studio",
        path=str(p.relative_to(vault_dir())),
        body="agent studio test body",
        source="pwa",
        created_at=datetime(2026, 6, 12, 14, 35, 22),
    )
    enqueue("notetaker", "classify", {"note_id": note_id})

    response = (
        {
            "text": json.dumps({
                "classification": "idea",
                "summary": "agent studio override",
                "confidence": 0.9,
                "tags": ["agents"],
                "related_articles": [],
            }),
        },
        "claude",
    )
    with patch(
        "mastisk.agents.notetaker.intelligence.run_intelligence",
        new_callable=AsyncMock,
        return_value=response,
    ) as run_mock:
        asyncio.run(Notetaker().run_once())

    prompt = run_mock.call_args.args[0]
    assert prompt.startswith("OVERRIDE")
    assert "agent studio test body" in prompt
    assert "## Additional instructions (skill: Strict style)" in prompt
    assert "Skill body reached runtime." in prompt


def test_enabled_false_noops_notetaker_tick(db, vault_tmp, data_tmp) -> None:
    from mastisk.agents.base import enqueue
    from mastisk.agents.notetaker import Notetaker
    from mastisk.db.queries import insert_note
    from mastisk.paths import ensure_dirs, notes_inbox_dir, vault_dir

    ensure_dirs()
    with _client(vault_tmp, data_tmp, db) as client:
        saved = client.put("/api/agents/notetaker/profile", json={"enabled": False})
        assert saved.status_code == 200, saved.text

    p = notes_inbox_dir() / "100000-disabled.md"
    p.write_text("should not classify", encoding="utf-8")
    note_id = insert_note(
        db,
        slug="100000-disabled",
        path=str(p.relative_to(vault_dir())),
        body="should not classify",
        source="pwa",
        created_at=datetime(2026, 6, 12, 10, 0, 0),
    )
    enqueue("notetaker", "classify", {"note_id": note_id})

    with patch(
        "mastisk.agents.notetaker.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as run_mock:
        asyncio.run(Notetaker().run_once())

    assert run_mock.call_count == 0
    row = db.execute("SELECT status FROM jobs WHERE agent='notetaker'").fetchone()
    assert row["status"] == "queued"


def test_enabled_false_noops_capture_router(db, vault_tmp, data_tmp) -> None:
    from mastisk.capture.router import route_capture

    with _client(vault_tmp, data_tmp, db) as client:
        saved = client.put("/api/agents/capture_router/profile", json={"enabled": False})
        assert saved.status_code == 200, saved.text

    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as run_mock:
        capture = asyncio.run(route_capture("remember this raw thought", "pwa", None))

    assert run_mock.call_count == 0
    assert capture.type == "inbox"
    assert capture.body == "remember this raw thought"


def test_disabled_capture_router_still_records_routine_command(db, vault_tmp, data_tmp) -> None:
    from mastisk.routines.sync import create_routine_file
    from mastisk.routes.capture import route_and_persist_capture

    create_routine_file(name="Morning Vitamins", time_of_day="morning")
    with _client(vault_tmp, data_tmp, db) as client:
        saved = client.put("/api/agents/capture_router/profile", json={"enabled": False})
        assert saved.status_code == 200, saved.text

    with patch(
        "mastisk.capture.router.intelligence.run_intelligence",
        new_callable=AsyncMock,
    ) as run_mock:
        result = asyncio.run(
            route_and_persist_capture(
                "did my vitamins",
                source="pwa",
                ts="2026-06-11T09:00:00-07:00",
            )
        )

    assert run_mock.call_count == 0
    assert result["type"] == "routine_done"
    assert result["routine_slug"] == "morning-vitamins"
    assert "- 2026-06-11" in (vault_tmp / "routines" / "morning-vitamins.md").read_text(
        encoding="utf-8"
    )


def test_frontend_agent_studio_static_contract() -> None:
    source = (ROOT / "frontend/src/components/AgentsView.tsx").read_text(encoding="utf-8")
    router = (ROOT / "frontend/src/router.ts").read_text(encoding="utf-8")
    css = (ROOT / "frontend/src/styles/mastisk.css").read_text(encoding="utf-8")

    assert "api.agentDetail" in source
    assert "Customize" in source
    assert "Reset to default" in source
    assert "cmd+enter" in source.lower() or "⌘↵" in source
    assert "override ignored" in source
    assert "Additional instructions" in source
    assert "isSafeSkillName" in source
    assert "letters, numbers, spaces" in source
    assert "invalidPlaceholderFields" in source
    assert "attribute/index placeholders" in source
    assert "dirtyProfilePatch" in source
    assert "prompt_override" in source
    assert "this agent has no editable prompts" in source
    assert "detail.slots.length === 0" in source
    assert "agent_detail" in router
    assert "/agents/" in router
    assert ".agent-studio-layout" in css
    assert ".agent-card.clickable" in css
