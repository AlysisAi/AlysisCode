"""Explicit per-turn skills for IDE hosts, using the same resolver as /skill."""

from __future__ import annotations

from typing import Any

from ..extensions.plugin_skills import (
    explicit_plugin_skill_message,
    find_plugin_skill,
    plugin_skill_still_active,
)
from ..skills import build_explicit_skill_context_message, resolve_skill_by_name
from .protocol import ProtocolError


def validate_skill_invocation(
    session: Any, value: Any, *, sensitive_media: bool = False, request_id: Any = None
) -> dict[str, str] | None:
    if value is None:
        return None
    if sensitive_media or not getattr(session, "skills_enabled", False):
        raise ProtocolError(
            "skills_unavailable", "Skills are unavailable in this session.", request_id=request_id
        )
    if (
        not isinstance(value, dict)
        or set(value) != {"name", "arguments"}
        or not isinstance(value.get("name"), str)
        or not value["name"].strip()
        or len(value["name"]) > 256
        or not isinstance(value.get("arguments"), str)
        or len(value["arguments"]) > 1_000_000
    ):
        raise ProtocolError(
            "invalid_skill", "skill requires a name and text arguments.", request_id=request_id
        )
    name = value["name"].strip()
    plugin_skill = find_plugin_skill(getattr(session, "plugin_skills", ()), name)
    if plugin_skill is not None:
        return {"name": plugin_skill.name, "arguments": value["arguments"]}
    skill = resolve_skill_by_name(getattr(session, "skill_registry", None), name)
    if skill is None:
        raise ProtocolError(
            "skill_not_found", f"Skill unavailable in this session: {name}", request_id=request_id
        )
    return {"name": skill.name, "arguments": value["arguments"]}


def skill_context_message(session: Any, value: dict[str, str]) -> str:
    selection = validate_skill_invocation(session, value)
    assert selection is not None
    plugin_skill = find_plugin_skill(getattr(session, "plugin_skills", ()), selection["name"])
    if plugin_skill is not None:
        # The same label and escaping as skill_read, and the same "plugin still on" check.
        if not plugin_skill_still_active(session.root, plugin_skill):
            raise ProtocolError(
                "skill_not_found",
                f"Skill unavailable in this session: {plugin_skill.name} (its plugin is off)",
            )
        return explicit_plugin_skill_message(plugin_skill, selection["arguments"])
    skill = resolve_skill_by_name(session.skill_registry, selection["name"])
    assert skill is not None
    return build_explicit_skill_context_message(skill=skill, task_text=selection["arguments"])
