"""Skills of the plugins bound in a session, read only from the descriptor verified for binding."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..mcp.connector_host import (
    is_verified_connector,
    verified_connector_plugin,
    verified_custom_plugin,
)
from ..mcp.github_host import GITHUB_MCP_URL, GITHUB_PLUGIN_ID, verified_host_plugin
from ..mcp.google_host import SERVICES
from .manifest import PluginManifest
from .plugin_skill_text import plugin_guidance, plugin_skill_catalog_block
from .registry import find_by_id, load_registry

_TASK_MAX_CHARS = 480


@dataclass(frozen=True)
class PluginSkill:
    #: Namespaced (``github:review-pull-request``): it can never take or hide another skill's name.
    name: str
    plugin_id: str
    plugin_name: str
    version: str
    publisher: str
    tier: str
    title: str
    description: str
    uses_tools: tuple[str, ...]
    text: str
    server_name: str
    url: str


def _bound_plugins(
    host_mcp: dict[str, Any] | None,
    google_mcp: list[dict[str, Any]] | None,
    connector_mcp: list[dict[str, Any]] | None = None,
) -> list[tuple[str, str, str]]:
    plugins = [(GITHUB_PLUGIN_ID, "github", GITHUB_MCP_URL)] if host_mcp is not None else []
    for catalog in google_mcp or []:
        service = catalog.get("service")
        if isinstance(service, str) and service in SERVICES:
            plugins.append(
                (f"alysis.google_{service}", service, f"https://{SERVICES[service]}/mcp/v1")
            )
    for catalog in connector_mcp or []:
        plugin_id = catalog.get("plugin_id")
        if is_verified_connector(plugin_id):
            plugins.append((plugin_id, _VERIFIED, ""))
        elif isinstance(plugin_id, str):
            plugins.append((plugin_id, _CUSTOM, ""))
    return plugins


def _verified(skill_or_plugin: tuple[str, str, str], root: Path) -> PluginManifest | None:
    plugin_id, server_name, url = skill_or_plugin
    if server_name == _CUSTOM:
        custom = verified_custom_plugin(root, plugin_id)
        return custom[1] if custom is not None else None
    if server_name == _VERIFIED:
        connector = verified_connector_plugin(root, plugin_id)
        return connector[1] if connector is not None else None
    verified = verified_host_plugin(root, plugin_id, server_name, url)
    return verified[1] if verified is not None else None


_CUSTOM = "custom"
#: An Alysis-verified connector plugin (``alysis.notion``): bundled, so labelled like GitHub.
_VERIFIED = "verified_connector"


def _skills_of(manifest: PluginManifest, server_name: str, url: str) -> tuple[PluginSkill, ...]:
    if server_name == _CUSTOM:
        # The user's own plugin: guidance written by the person, labelled as such.
        publisher, tier = "You", "custom"
    else:
        entry = find_by_id(load_registry(), manifest.plugin.id)
        publisher = entry.publisher if entry and entry.publisher else manifest.plugin.author
        tier = entry.tier if entry and entry.tier else "community"
    short = manifest.plugin.id.split(".", 1)[1]
    if server_name == _CUSTOM:
        short = "custom_" + short
    return tuple(
        PluginSkill(
            name=f"{short}:{component.name}",
            plugin_id=manifest.plugin.id,
            plugin_name=manifest.plugin.name,
            version=manifest.plugin.version,
            publisher=publisher,
            tier=tier,
            title=component.title,
            description=component.description,
            uses_tools=tuple(component.uses_tools),
            text=component.text,
            server_name=server_name,
            url=url,
        )
        for component in manifest.components.plugin_skill
    )


def active_plugin_skills(
    root: Path,
    *,
    host_mcp: dict[str, Any] | None = None,
    google_mcp: list[dict[str, Any]] | None = None,
    connector_mcp: list[dict[str, Any]] | None = None,
) -> tuple[PluginSkill, ...]:
    """Skills of the host plugins bound in this session: installed, on, verified, current."""
    skills: list[PluginSkill] = []
    for plugin in _bound_plugins(host_mcp, google_mcp, connector_mcp):
        manifest = _verified(plugin, root)
        if manifest is not None:
            skills.extend(_skills_of(manifest, plugin[1], plugin[2]))
    return tuple(skills)


def plugin_skill_still_active(root: Path, skill: PluginSkill) -> bool:
    """Checked at each read, like host MCP calls: off, removed or changed means no read."""
    manifest = _verified((skill.plugin_id, skill.server_name, skill.url), root)
    return manifest is not None and skill in _skills_of(manifest, skill.server_name, skill.url)


def plugin_skill_context(skills: tuple[PluginSkill, ...]) -> str | None:
    return plugin_skill_catalog_block(
        [
            (skill.name, skill.description, f"{skill.plugin_name} {skill.version}")
            for skill in skills
        ]
    )


def find_plugin_skill(skills: tuple[PluginSkill, ...], name: str) -> PluginSkill | None:
    matches = [
        skill
        for skill in skills
        if name in {skill.name, f"{skill.plugin_id}:{skill.name.split(':', 1)[1]}"}
    ]
    return matches[0] if len(matches) == 1 else None


def _guidance(skill: PluginSkill) -> str:
    return plugin_guidance(
        plugin_id=skill.plugin_id,
        version=skill.version,
        publisher=skill.publisher,
        tier=skill.tier,
        name=skill.name,
        text=skill.text,
    )


def read_plugin_skill(root: Path, skill: PluginSkill) -> dict[str, Any]:
    if not plugin_skill_still_active(root, skill):
        return {
            "error": "This plugin is off, removed or changed, so its skill can't be read.",
            "name": skill.name,
        }
    return {
        "name": skill.name,
        "plugin_id": skill.plugin_id,
        "plugin_version": skill.version,
        "trust_level": "publisher_guidance",
        "content": _guidance(skill),
    }


def explicit_plugin_skill_message(skill: PluginSkill, task_text: str) -> str:
    """The person's own `$`/`/skill` choice: the same label and escaping as `skill_read`."""
    task = " ".join(task_text.split())
    if len(task) > _TASK_MAX_CHARS:
        task = task[: _TASK_MAX_CHARS - 1] + "…"
    return (
        "<explicit_skill_context>\n"
        "source: user-selected plugin skill for this turn only\n"
        "trust: lower_priority_than_system_and_direct_user_instructions\n"
        f"name: {skill.name}\n"
        f"description: {skill.description}\n"
        f"plugin: {skill.plugin_name} {skill.version} ({skill.plugin_id})\n"
        "This skill is attached only for the current turn. It does not persist across future turns.\n"
        "task_binding: Treat this wrapper and the next user message as one bound instruction set.\n"
        + (f"task: {task}\n" if task else "")
        + f"{_guidance(skill)}\n"
        "</explicit_skill_context>\n"
    )
