"""File-first Agent Studio profile and skill mirrors."""
from __future__ import annotations

import json
import re
import string
import threading
from pathlib import Path
from typing import Any

import yaml
from slugify import slugify

from mastisk.agents.registry import PromptSlot, agent_definition
from mastisk.db.queries import connect
from mastisk.file_locks import host_file_lock
from mastisk.paths import agent_skills_dir, agents_dir, vault_dir
from mastisk.projects.sync import split_frontmatter
from mastisk.routes.notes import atomic_write

_WRITE_PROFILE_LOCK = threading.Lock()
_WRITE_SKILL_LOCK = threading.Lock()
_SAFE_SKILL_NAME_RE = re.compile(r"^[\w\s.,()&/-]+$")


class PlaceholderValidationError(ValueError):
    def __init__(self, missing: dict[str, list[str]]) -> None:
        self.missing = missing
        parts = [
            f"{slot}: " + ", ".join(names)
            for slot, names in missing.items()
        ]
        super().__init__("invalid prompt overrides: " + "; ".join(parts))


def scan_agent_profiles(paths: list[Path] | None = None) -> dict[str, int]:
    profile_paths = paths if paths is not None else _agent_profile_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for raw_path in profile_paths:
            path = _absolute_path(raw_path)
            if path.parent.name == "skills":
                continue
            if not path.exists() or path.name.startswith("."):
                if paths is not None and path.suffix == ".md":
                    _soft_delete_profile_path(conn, path)
                continue
            with host_file_lock(path):
                profile = parse_agent_profile_file(path)
            agent_id = profile["agent_id"]
            seen.add(agent_id)
            invalid_slots = validate_profile_slots(
                agent_id,
                prompt_override=profile.get("prompt_override") or "",
                slot_overrides=profile.get("slot_overrides") or {},
                raise_on_error=False,
            )
            invalid_reason = _invalid_reason(invalid_slots)
            conn.execute(
                """INSERT INTO agent_profiles
                   (agent_id, path, enabled, model, skills_json, prompt_override,
                    slot_overrides_json, invalid, invalid_reason,
                    invalid_slots_json, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(agent_id) DO UPDATE SET
                     path=excluded.path,
                     enabled=excluded.enabled,
                     model=excluded.model,
                     skills_json=excluded.skills_json,
                     prompt_override=excluded.prompt_override,
                     slot_overrides_json=excluded.slot_overrides_json,
                     invalid=excluded.invalid,
                     invalid_reason=excluded.invalid_reason,
                     invalid_slots_json=excluded.invalid_slots_json,
                     deleted_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    agent_id,
                    profile["path"],
                    1 if profile.get("enabled") else 0,
                    profile.get("model"),
                    json.dumps(profile.get("skills") or []),
                    profile.get("prompt_override") or None,
                    json.dumps(profile.get("slot_overrides") or {}, ensure_ascii=False, sort_keys=True),
                    1 if invalid_slots else 0,
                    invalid_reason,
                    json.dumps(invalid_slots, ensure_ascii=False, sort_keys=True),
                ),
            )
            upserted += 1
        if paths is None:
            _soft_delete_disappeared_profiles(conn, seen)
    return {"upserted": upserted}


def scan_agent_skills(paths: list[Path] | None = None) -> dict[str, int]:
    skill_paths = paths if paths is not None else _agent_skill_paths()
    seen: set[str] = set()
    upserted = 0
    with connect() as conn:
        for raw_path in skill_paths:
            path = _absolute_path(raw_path)
            if not path.exists() or path.name.startswith("."):
                if paths is not None and path.suffix == ".md":
                    _soft_delete_skill_path(conn, path)
                continue
            with host_file_lock(path):
                skill = parse_agent_skill_file(path)
            slug = skill["slug"]
            seen.add(slug)
            conn.execute(
                """INSERT INTO agent_skills
                   (slug, path, name, description, tags_json, body, deleted_at)
                   VALUES (?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(slug) DO UPDATE SET
                     path=excluded.path,
                     name=excluded.name,
                     description=excluded.description,
                     tags_json=excluded.tags_json,
                     body=excluded.body,
                     deleted_at=NULL,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    slug,
                    skill["path"],
                    skill["name"],
                    skill.get("description"),
                    json.dumps(skill.get("tags") or []),
                    skill.get("body") or "",
                ),
            )
            upserted += 1
        if paths is None:
            _soft_delete_disappeared_skills(conn, seen)
    return {"upserted": upserted}


def parse_agent_profile_file(path: Path) -> dict[str, Any]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    slot_overrides = meta.get("slot_overrides") if isinstance(meta.get("slot_overrides"), dict) else {}
    return {
        "agent_id": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "enabled": bool(meta.get("enabled", True)),
        "model": _clean_optional(meta.get("model")),
        "skills": _string_list(meta.get("skills")),
        "prompt_override": body.strip() or "",
        "slot_overrides": {
            str(key): str(value).strip()
            for key, value in slot_overrides.items()
            if value is not None and str(value).strip()
        },
        "frontmatter": meta,
        "body": body,
    }


def parse_agent_skill_file(path: Path) -> dict[str, Any]:
    meta, body = split_frontmatter(path.read_text(encoding="utf-8"))
    name = _clean_optional(meta.get("name")) or path.stem.replace("-", " ").title()
    validate_skill_name(name)
    return {
        "slug": path.stem,
        "path": str(path.relative_to(vault_dir())),
        "name": name,
        "description": _clean_optional(meta.get("description")),
        "tags": _string_list(meta.get("tags")),
        "body": body.strip(),
        "frontmatter": meta,
    }


def write_agent_profile(agent_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    definition = agent_definition(agent_id)
    path = agents_dir() / f"{definition.id}.md"
    with _WRITE_PROFILE_LOCK, host_file_lock(path):
        if path.exists():
            parsed = parse_agent_profile_file(path)
            meta = dict(parsed["frontmatter"])
            body = parsed["prompt_override"]
            slot_overrides = dict(parsed["slot_overrides"])
        else:
            meta = {"enabled": True, "model": None, "skills": []}
            body = ""
            slot_overrides = {}

        if "enabled" in updates:
            meta["enabled"] = bool(updates["enabled"])
        if "model" in updates:
            meta["model"] = _clean_optional(updates["model"])
        if "skills" in updates:
            meta["skills"] = _string_list(updates["skills"])
        if "prompt_override" in updates:
            value = updates["prompt_override"]
            body = "" if value is None else str(value).strip()
        if "slot_overrides" in updates:
            slot_overrides = {
                str(key): str(value).strip()
                for key, value in (updates["slot_overrides"] or {}).items()
                if value is not None and str(value).strip()
            }

        validate_profile_slots(agent_id, prompt_override=body, slot_overrides=slot_overrides)
        if slot_overrides:
            meta["slot_overrides"] = slot_overrides
        else:
            meta.pop("slot_overrides", None)
        meta.setdefault("enabled", True)
        meta.setdefault("skills", [])
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(path, dump_agent_profile_file(meta, body))

    scan_agent_profiles([path])
    return profile_payload(agent_id)


def write_agent_skill(
    *,
    slug: str | None,
    name: str,
    description: str | None = None,
    tags: list[str] | None = None,
    body: str = "",
    overwrite: bool = False,
) -> dict[str, Any]:
    clean_name = _clean_optional(name)
    if not clean_name:
        raise ValueError("name must be non-blank")
    validate_skill_name(clean_name)
    skill_slug = slugify(slug or clean_name)[:80] or "skill"
    path = agent_skills_dir() / f"{skill_slug}.md"
    with _WRITE_SKILL_LOCK, host_file_lock(path):
        if path.exists() and not overwrite:
            raise FileExistsError(skill_slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        meta = {
            "name": clean_name,
            "description": _clean_optional(description),
            "tags": _string_list(tags),
        }
        atomic_write(path, dump_agent_skill_file(meta, body))
    scan_agent_skills([path])
    skill = skill_payload(skill_slug)
    if skill is None:
        raise RuntimeError(f"agent skill mirror missing after write: {skill_slug}")
    return skill


def delete_agent_skill(slug: str) -> bool:
    skill = skill_payload(slug)
    if skill is None:
        return False
    path = vault_dir() / skill["path"]
    with host_file_lock(path):
        path.unlink(missing_ok=True)
    scan_agent_skills([path])
    return True


def validate_skill_name(name: str) -> None:
    if not _SAFE_SKILL_NAME_RE.fullmatch(name):
        raise ValueError("name may only contain letters, numbers, spaces, and . , ( ) & / -")


def profile_payload(agent_id: str) -> dict[str, Any]:
    try:
        definition = agent_definition(agent_id)
    except KeyError:
        definition = None
    with connect() as conn:
        row = conn.execute(
            """SELECT *
               FROM agent_profiles
               WHERE agent_id = ? AND deleted_at IS NULL""",
            (agent_id,),
        ).fetchone()
    if row is None:
        return {
            "agent_id": agent_id,
            "path": f"_agents/{agent_id}.md",
            "enabled": True,
            "model": None,
            "skills": [],
            "prompt_override": "",
            "slot_overrides": {},
            "invalid": False,
            "invalid_reason": None,
            "invalid_slots": {},
            "model_supported": bool(definition and definition.model_supported),
            "model_note": definition.model_note if definition else "",
        }
    data = dict(row)
    return {
        "agent_id": data["agent_id"],
        "path": data["path"],
        "enabled": bool(data["enabled"]),
        "model": data.get("model"),
        "skills": _loads_list(data.get("skills_json")),
        "prompt_override": data.get("prompt_override") or "",
        "slot_overrides": _loads_dict(data.get("slot_overrides_json")),
        "invalid": bool(data.get("invalid")),
        "invalid_reason": data.get("invalid_reason"),
        "invalid_slots": _loads_dict(data.get("invalid_slots_json")),
        "model_supported": bool(definition and definition.model_supported),
        "model_note": definition.model_note if definition else "",
    }


def list_skills() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """SELECT *
               FROM agent_skills
               WHERE deleted_at IS NULL
               ORDER BY lower(name), slug"""
        ).fetchall()
    return [_skill_row(dict(row)) for row in rows]


def skill_payload(slug: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            """SELECT *
               FROM agent_skills
               WHERE slug = ? AND deleted_at IS NULL""",
            (slug,),
        ).fetchone()
    return _skill_row(dict(row)) if row else None


def validate_profile_slots(
    agent_id: str,
    *,
    prompt_override: str,
    slot_overrides: dict[str, str],
    raise_on_error: bool = True,
) -> dict[str, list[str]]:
    try:
        definition = agent_definition(agent_id)
    except KeyError:
        missing = {"profile": ["unknown-agent"]}
        if raise_on_error:
            raise PlaceholderValidationError(missing) from None
        return missing
    slots = {slot.slot_id: slot for slot in definition.slots}
    invalid: dict[str, list[str]] = {}
    primary = next((slot for slot in definition.slots if slot.primary), None)
    if primary is not None and prompt_override.strip():
        errors = _template_validation_errors(primary, prompt_override)
        if errors:
            invalid[primary.slot_id] = errors
    for slot_id, override in slot_overrides.items():
        slot = slots.get(slot_id)
        if slot is None:
            invalid[slot_id] = ["unknown-slot"]
            continue
        if not override.strip():
            continue
        errors = _template_validation_errors(slot, override)
        if errors:
            invalid[slot_id] = errors
    if invalid and raise_on_error:
        raise PlaceholderValidationError(invalid)
    return invalid


def dump_agent_profile_file(meta: dict[str, Any], body: str) -> str:
    frontmatter = yaml.dump(
        _clean_meta(meta),
        Dumper=_LiteralDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    clean_body = body.strip()
    if clean_body:
        return f"---\n{frontmatter}\n---\n\n{clean_body}\n"
    return f"---\n{frontmatter}\n---\n"


def dump_agent_skill_file(meta: dict[str, Any], body: str) -> str:
    frontmatter = yaml.dump(
        _clean_meta(meta),
        Dumper=_LiteralDumper,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=True,
    ).strip()
    clean_body = body.strip()
    if clean_body:
        return f"---\n{frontmatter}\n---\n\n{clean_body}\n"
    return f"---\n{frontmatter}\n---\n"


def _agent_profile_paths() -> list[Path]:
    directory = agents_dir()
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def _agent_skill_paths() -> list[Path]:
    directory = agent_skills_dir()
    if not directory.exists():
        return []
    return sorted(path for path in directory.glob("*.md") if path.is_file())


def _absolute_path(path: Path) -> Path:
    return path if path.is_absolute() else vault_dir() / path


def _soft_delete_profile_path(conn, path: Path) -> None:
    rel = _relative_path(path)
    conn.execute(
        "UPDATE agent_profiles SET deleted_at=CURRENT_TIMESTAMP WHERE path=?",
        (rel,),
    )


def _soft_delete_skill_path(conn, path: Path) -> None:
    rel = _relative_path(path)
    conn.execute(
        "UPDATE agent_skills SET deleted_at=CURRENT_TIMESTAMP WHERE path=?",
        (rel,),
    )


def _soft_delete_disappeared_profiles(conn, seen: set[str]) -> None:
    if seen:
        placeholders = ",".join("?" for _ in seen)
        conn.execute(
            f"""UPDATE agent_profiles
                SET deleted_at=CURRENT_TIMESTAMP
                WHERE deleted_at IS NULL AND agent_id NOT IN ({placeholders})""",
            tuple(seen),
        )
    else:
        conn.execute("UPDATE agent_profiles SET deleted_at=CURRENT_TIMESTAMP WHERE deleted_at IS NULL")


def _soft_delete_disappeared_skills(conn, seen: set[str]) -> None:
    if seen:
        placeholders = ",".join("?" for _ in seen)
        conn.execute(
            f"""UPDATE agent_skills
                SET deleted_at=CURRENT_TIMESTAMP
                WHERE deleted_at IS NULL AND slug NOT IN ({placeholders})""",
            tuple(seen),
        )
    else:
        conn.execute("UPDATE agent_skills SET deleted_at=CURRENT_TIMESTAMP WHERE deleted_at IS NULL")


def _relative_path(path: Path) -> str:
    try:
        return str(path.relative_to(vault_dir()))
    except ValueError:
        return str(path)


def _template_validation_errors(slot: PromptSlot, template: str) -> list[str]:
    if not slot.format_template:
        return []
    errors: list[str] = []
    fields, parse_errors = _template_fields(template)
    errors.extend(parse_errors)
    allowed = set(slot.required_placeholders)
    unknown = sorted(name for name in fields if name not in allowed)
    errors.extend(f"unknown {{{name}}}" for name in unknown)
    missing = [name for name in slot.required_placeholders if not _contains_placeholder(template, name)]
    errors.extend(f"missing {{{name}}}" for name in missing)
    return errors


def _template_fields(template: str) -> tuple[set[str], list[str]]:
    formatter = string.Formatter()
    fields: set[str] = set()
    errors: list[str] = []

    def walk(text: str) -> None:
        try:
            parsed = formatter.parse(text)
            for _, field_name, format_spec, _ in parsed:
                if field_name == "":
                    errors.append("positional placeholder {} is not allowed")
                elif field_name:
                    if "." in field_name or "[" in field_name:
                        errors.append(f"attribute/index placeholders are not allowed: {{{field_name}}}")
                    else:
                        fields.add(field_name)
                if format_spec:
                    walk(format_spec)
        except (ValueError, IndexError):
            errors.append("malformed template braces")

    walk(template)
    return fields, errors


def _contains_placeholder(template: str, name: str) -> bool:
    return re.search(r"(?<!\{)\{" + re.escape(name) + r"(?:[!:]|\})(?!\})", template) is not None


def _invalid_reason(invalid_slots: dict[str, list[str]]) -> str | None:
    if not invalid_slots:
        return None
    parts = [
        f"{slot} " + ", ".join(names)
        for slot, names in invalid_slots.items()
    ]
    return "; ".join(parts)


def _skill_row(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "slug": row["slug"],
        "path": row["path"],
        "name": row["name"],
        "description": row.get("description"),
        "tags": _loads_list(row.get("tags_json")),
        "body": row.get("body") or "",
    }


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out


def _clean_optional(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _clean_meta(meta: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in meta.items()
        if value is not None and value != "" and value != {}
    }


def _loads_list(raw: object) -> list[str]:
    try:
        data = json.loads(str(raw or "[]"))
    except json.JSONDecodeError:
        return []
    return _string_list(data)


def _loads_dict(raw: object) -> dict[str, Any]:
    try:
        data = json.loads(str(raw or "{}"))
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


class _LiteralDumper(yaml.SafeDumper):
    pass


def _str_representer(dumper: yaml.Dumper, data: str):
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_LiteralDumper.add_representer(str, _str_representer)
