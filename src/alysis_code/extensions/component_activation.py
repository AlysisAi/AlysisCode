"""Shared plugin ownership and filtering for session and management catalogs."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from ..skills.models import DiscoveredSkills, SkillBundle
from .activation import ActivationDecision
from .models import normalize_extension_id, plugin_slug_from_id
from .state import load_global_state, load_project_state


@dataclass(frozen=True)
class PluginActivationIndex:
    slug_to_plugin_id: dict[str, str]
    skill_lookup_to_plugin_id: dict[str, str]


def build_plugin_activation_index(repo_root: Path) -> PluginActivationIndex:
    slug_to_plugin_id: dict[str, str] = {}
    skill_lookup_to_plugin_id: dict[str, str] = {}
    try:
        states = (load_global_state(), load_project_state(repo_root))
    except RuntimeError:
        states = (load_global_state(),)
    for state in states:
        for raw_plugin_id, record in state.installed.items():
            plugin_id = normalize_extension_id(record.id or raw_plugin_id)
            if not plugin_id:
                continue
            slug_to_plugin_id[plugin_slug_from_id(plugin_id)] = plugin_id
            for skill_id in record.component_ids.get("skill", []):
                normalized_skill = str(skill_id or "").strip().casefold()
                if normalized_skill:
                    skill_lookup_to_plugin_id[normalized_skill] = plugin_id
    return PluginActivationIndex(
        slug_to_plugin_id=slug_to_plugin_id,
        skill_lookup_to_plugin_id=skill_lookup_to_plugin_id,
    )


def component_plugin_allowed(
    plugin_id: str | None,
    activation_decision: ActivationDecision,
    dropped_counts: Counter[str],
) -> bool:
    if plugin_id is None:
        return True
    normalized = normalize_extension_id(plugin_id)
    if normalized in activation_decision.enabled_plugin_ids:
        return True
    dropped_counts[normalized] += 1
    return False


def skill_plugin_id(skill: SkillBundle, index: PluginActivationIndex) -> str | None:
    for lookup_key in skill.lookup_keys():
        plugin_id = index.skill_lookup_to_plugin_id.get(lookup_key.casefold())
        if plugin_id is not None:
            return plugin_id
    return None


def filter_discovered_skills_for_plugins(
    *,
    discovered: DiscoveredSkills,
    activation_decision: ActivationDecision,
    index: PluginActivationIndex,
) -> tuple[DiscoveredSkills, Counter[str]]:
    dropped_counts: Counter[str] = Counter()
    kept_ordered = tuple(
        skill
        for skill in discovered.ordered
        if component_plugin_allowed(
            skill_plugin_id(skill, index),
            activation_decision,
            dropped_counts,
        )
    )
    kept_keys = {skill.name.casefold() for skill in kept_ordered}
    kept_skills = {
        key: skill
        for key, skill in discovered.skills.items()
        if key.casefold() in kept_keys or skill.name.casefold() in kept_keys
    }
    return (
        DiscoveredSkills(
            skills=dict(sorted(kept_skills.items(), key=lambda item: item[0])),
            ordered=kept_ordered,
            issues=discovered.issues,
        ),
        dropped_counts,
    )
