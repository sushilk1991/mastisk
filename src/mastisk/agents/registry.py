"""Agent Studio registry and prompt resolution.

Registry entries intentionally point at real module constants. Profile files
may override those templates, but save/scan validation guarantees every
runtime ``str.format`` placeholder remains present before an override is used.
Skills are reusable prompt-augmentation snippets; tagged skill bodies append to
the assembled primary prompt only.
"""
from __future__ import annotations

import importlib
import json
import logging
import string
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from mastisk.db.queries import connect

log = logging.getLogger("mastisk.agent_registry")


@dataclass(frozen=True)
class PromptSlot:
    slot_id: str
    label: str
    module: str
    module_attr: str
    required_placeholders: tuple[str, ...]
    default_prompt: str
    primary: bool = False
    editable: bool = True
    format_template: bool = True


@dataclass(frozen=True)
class AgentDefinition:
    id: str
    name: str
    role: str
    color: str
    implemented: bool
    load_cap: int
    module: str
    slots: tuple[PromptSlot, ...]
    model_supported: bool = False
    model_note: str = "This agent's current bridge path does not accept a per-agent model override."


_AGENT_SPECS: tuple[dict[str, Any], ...] = (
    {
        "id": "scout",
        "name": "Scout",
        "role": "Crawls feeds, blogs, RSS",
        "color": "amber",
        "module": "mastisk.agents.scout",
        "load_cap": 500,
        "slots": (),
    },
    {
        "id": "listener",
        "name": "Listener",
        "role": "Transcribes podcasts + YouTube",
        "color": "violet",
        "module": "mastisk.agents.listener",
        "load_cap": 20,
        "slots": (),
    },
    {
        "id": "compiler",
        "name": "Compiler",
        "role": "Compiles raw -> wiki, builds backlinks",
        "color": "emerald",
        "module": "mastisk.agents.compiler",
        "load_cap": 100,
        "slots": (
            {"slot_id": "schema", "label": "Article schema", "module_attr": "SCHEMA_MD", "primary": True, "format_template": False},
        ),
    },
    {
        "id": "linter",
        "name": "Linter",
        "role": "Health checks, broken links, orphans",
        "color": "blue",
        "module": "mastisk.agents.linter",
        "load_cap": 50,
        "slots": (),
    },
    {
        "id": "synthesizer",
        "name": "Synthesizer",
        "role": "Cross-source synthesis, themes",
        "color": "rose",
        "module": "mastisk.agents.synthesizer",
        "load_cap": 10,
        "slots": (
            {"slot_id": "schema", "label": "Draft schema", "module_attr": "_SCHEMA_MD", "primary": True, "format_template": False},
            {"slot_id": "critic", "label": "Critic prompt", "module_attr": "_CRITIC_TEMPLATE"},
        ),
    },
    {
        "id": "notetaker",
        "name": "Notetaker",
        "role": "Classifies inbox notes, writes summaries",
        "color": "blue",
        "module": "mastisk.agents.notetaker",
        "load_cap": 20,
        "slots": (
            {"slot_id": "classify", "label": "Classification prompt", "module_attr": "CLASSIFY_PROMPT", "primary": True},
        ),
    },
    {
        "id": "escalator",
        "name": "Escalator",
        "role": "Promotes strong notes into wiki articles",
        "color": "amber",
        "module": "mastisk.agents.escalator",
        "load_cap": 10,
        "slots": (
            {"slot_id": "evaluate", "label": "Escalation prompt", "module_attr": "ESCALATE_PROMPT", "primary": True},
        ),
    },
    {
        "id": "ingest",
        "name": "Ingest",
        "role": "Converts documents, audio, journal photos",
        "color": "emerald",
        "module": "mastisk.agents.ingest",
        "load_cap": 10,
        "slots": (
            {"slot_id": "metadata", "label": "Document metadata prompt", "module_attr": "_EXTRACT_PROMPT", "module": "mastisk.ingest.pipeline", "primary": True},
        ),
    },
    {
        "id": "capture_router",
        "name": "Capture Router",
        "role": "Routes quick captures into the right local surface",
        "color": "violet",
        "module": "mastisk.capture.router",
        "load_cap": 5,
        "slots": (
            {"slot_id": "route", "label": "Capture routing prompt", "module_attr": "_PROMPT", "primary": True},
        ),
    },
    {
        "id": "artifact-agent",
        "name": "Artifacts",
        "role": "Renders charts, tables, inline visuals",
        "color": "rose",
        "module": "mastisk.agents.artifact_agent",
        "load_cap": 10,
        "slots": (
            {"slot_id": "generate", "label": "Artifact generation prompt", "module_attr": "_ARTIFACT_PROMPT_SCHEMA", "primary": True, "format_template": False},
        ),
    },
    {
        "id": "github_poller",
        "name": "GitHub Poller",
        "role": "Polls GitHub + local repos, builds context",
        "color": "violet",
        "module": "mastisk.agents.github_poller",
        "load_cap": 5,
        "slots": (),
    },
    {
        "id": "github_ideator",
        "name": "GitHub Ideator",
        "role": "Generates research ideas from repo context",
        "color": "rose",
        "module": "mastisk.agents.github_ideator",
        "load_cap": 5,
        "slots": (
            {"slot_id": "ideate", "label": "Idea generation prompt", "module_attr": "IDEATE_PROMPT", "primary": True},
        ),
    },
    {
        "id": "blog_writer",
        "name": "Blog Writer",
        "role": "Drafts blog posts from recent activity",
        "color": "emerald",
        "module": "mastisk.agents.blog_writer",
        "load_cap": 5,
        "slots": (
            {"slot_id": "rerank", "label": "Source rerank prompt", "module_attr": "RERANK_PROMPT_TEMPLATE", "primary": True},
        ),
    },
    {
        "id": "topic_suggester",
        "name": "Topic Suggester",
        "role": "Finds crossings between your signals and the world",
        "color": "amber",
        "module": "mastisk.agents.topic_suggester",
        "load_cap": 5,
        "slots": (
            {"slot_id": "suggest", "label": "Suggestion prompt", "module_attr": "SUGGEST_PROMPT_TEMPLATE", "primary": True},
        ),
    },
    {
        "id": "opinion_gap_miner",
        "name": "Opinion Gap Miner",
        "role": "Surfaces tensions between your beliefs and outside claims",
        "color": "rose",
        "module": "mastisk.agents.opinion_gap_miner",
        "load_cap": 5,
        "slots": (
            {"slot_id": "suggest", "label": "Gap mining prompt", "module_attr": "SUGGEST_PROMPT_TEMPLATE", "primary": True},
        ),
    },
    {
        "id": "tweet_writer",
        "name": "Tweet Writer",
        "role": "Drafts tweet threads from recent signals",
        "color": "blue",
        "module": "mastisk.agents.tweet_writer",
        "load_cap": 5,
        "slots": (
            {"slot_id": "draft", "label": "Thread draft prompt", "module_attr": "PROMPT_TEMPLATE", "primary": True},
            {"slot_id": "revision", "label": "Revision prompt", "module_attr": "REVISION_PROMPT_TEMPLATE"},
            {"slot_id": "browser_research", "label": "Browser research prompt", "module_attr": "BROWSER_RESEARCH_PLAN_PROMPT"},
        ),
    },
    {
        "id": "roundtable",
        "name": "Roundtable",
        "role": "Runs multi-LLM perspectives on a prompt",
        "color": "amber",
        "module": "mastisk.agents.roundtable",
        "load_cap": 5,
        "slots": (
            {"slot_id": "perspective", "label": "Perspective prompt", "module_attr": "PERSPECTIVE_PROMPT", "primary": True},
            {"slot_id": "synthesis", "label": "Synthesis prompt", "module_attr": "SYNTHESIS_PROMPT"},
        ),
    },
)


@lru_cache(maxsize=1)
def agent_definitions() -> tuple[AgentDefinition, ...]:
    return tuple(_definition_from_spec(spec) for spec in _AGENT_SPECS)


def agent_definition(agent_id: str) -> AgentDefinition:
    for definition in agent_definitions():
        if definition.id == agent_id:
            return definition
    raise KeyError(agent_id)


def agent_catalog() -> list[dict[str, Any]]:
    return [
        {
            "id": definition.id,
            "name": definition.name,
            "role": definition.role,
            "color": definition.color,
            "implemented": definition.implemented,
            "load_cap": definition.load_cap,
        }
        for definition in agent_definitions()
    ]


def slot_definition(agent_id: str, slot_id: str) -> PromptSlot:
    definition = agent_definition(agent_id)
    for slot in definition.slots:
        if slot.slot_id == slot_id:
            return slot
    raise KeyError(f"{agent_id}:{slot_id}")


def required_placeholders(template: str) -> tuple[str, ...]:
    names: set[str] = set()
    formatter = string.Formatter()
    for _, field_name, format_spec, _ in formatter.parse(template):
        if field_name:
            names.add(_normalise_field_name(field_name))
        if format_spec:
            names.update(required_placeholders(format_spec))
    return tuple(sorted(names))


def resolve_prompt(agent_id: str, slot_id: str, default: str) -> str:
    """Return a valid profile override or ``default``.

    Invalid hand-edited overrides are ignored per slot. Tagged skill snippets
    append only to the primary slot so secondary templates remain focused.
    """
    try:
        slot = slot_definition(agent_id, slot_id)
    except KeyError:
        return default

    profile = _profile_row(agent_id)
    prompt = default
    skill_slugs: list[str] = []
    if profile is not None:
        invalid_slots = _loads_dict(profile.get("invalid_slots_json"))
        if slot_id not in invalid_slots:
            if slot.primary:
                override = str(profile.get("prompt_override") or "")
            else:
                override = str(_loads_dict(profile.get("slot_overrides_json")).get(slot_id) or "")
            if override.strip():
                prompt = override
        skill_slugs = _loads_list(profile.get("skills_json"))

    if slot.primary and skill_slugs:
        prompt = _append_skill_bodies(prompt, skill_slugs, escape_braces=slot.format_template)
    return prompt


def agent_enabled(agent_id: str) -> bool:
    profile = _profile_row(agent_id)
    if profile is None:
        return True
    return bool(profile.get("enabled", 1))


def _definition_from_spec(spec: dict[str, Any]) -> AgentDefinition:
    module_name = str(spec["module"])
    slots = []
    for index, raw_slot in enumerate(spec.get("slots") or ()):
        slot_module = str(raw_slot.get("module") or module_name)
        module = importlib.import_module(slot_module)
        attr = str(raw_slot["module_attr"])
        template = str(getattr(module, attr))
        slots.append(
            PromptSlot(
                slot_id=str(raw_slot["slot_id"]),
                label=str(raw_slot["label"]),
                module=slot_module,
                module_attr=attr,
                required_placeholders=(
                    required_placeholders(template)
                    if raw_slot.get("format_template", True)
                    else ()
                ),
                default_prompt=template,
                primary=bool(raw_slot.get("primary", index == 0)),
                editable=bool(raw_slot.get("editable", True)),
                format_template=bool(raw_slot.get("format_template", True)),
            )
        )
    return AgentDefinition(
        id=str(spec["id"]),
        name=str(spec["name"]),
        role=str(spec["role"]),
        color=str(spec["color"]),
        implemented=bool(spec.get("implemented", True)),
        load_cap=int(spec.get("load_cap", 10)),
        module=module_name,
        slots=tuple(slots),
        model_supported=bool(spec.get("model_supported", False)),
        model_note=str(
            spec.get(
                "model_note",
                "This agent's current bridge path does not accept a per-agent model override.",
            )
        ),
    )


def _normalise_field_name(field_name: str) -> str:
    head = field_name.split(".", 1)[0].split("[", 1)[0]
    return head


def _profile_row(agent_id: str) -> dict[str, Any] | None:
    try:
        with connect() as conn:
            row = conn.execute(
                """SELECT *
                   FROM agent_profiles
                   WHERE agent_id = ? AND deleted_at IS NULL""",
                (agent_id,),
            ).fetchone()
    except Exception as exc:
        log.info("agent registry: profile lookup skipped for %s: %s", agent_id, exc)
        return None
    return dict(row) if row else None


def _append_skill_bodies(prompt: str, skill_slugs: list[str], *, escape_braces: bool) -> str:
    rows = []
    try:
        with connect() as conn:
            for slug in skill_slugs:
                row = conn.execute(
                    """SELECT slug, name, body
                       FROM agent_skills
                       WHERE slug = ? AND deleted_at IS NULL AND invalid = 0""",
                    (slug,),
                ).fetchone()
                if row and str(row["body"] or "").strip():
                    rows.append(dict(row))
    except Exception as exc:
        log.info("agent registry: skill lookup skipped: %s", exc)
        return prompt

    parts = [prompt.rstrip()]
    for row in rows:
        name = str(row.get("name") or row["slug"])
        body = str(row.get("body") or "").strip()
        if escape_braces:
            name = name.replace("{", "{{").replace("}", "}}")
            body = body.replace("{", "{{").replace("}", "}}")
        parts.append(f"\n\n## Additional instructions (skill: {name})\n{body}")
    return "".join(parts)


def _loads_list(raw: object) -> list[str]:
    try:
        data = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    if not isinstance(data, list):
        return []
    return [str(item) for item in data if str(item).strip()]


def _loads_dict(raw: object) -> dict[str, Any]:
    try:
        data = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}
