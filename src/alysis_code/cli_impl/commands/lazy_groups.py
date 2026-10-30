"""Load CLI command groups through normal Typer routing when selected."""

from __future__ import annotations

from importlib import import_module
from typing import Any

import typer
from typer.core import TyperGroup
from typer.main import get_group

# This is the CLI's existing registration table, not an alternate dispatcher.
# Root options/callbacks and each group's original parsing remain in Typer.
_GROUPS = {
    "memory": ("memory", "memory_app"),
    "auth": ("auth", "auth_app"),
    "config": ("config", "config_app"),
    "profile": ("profile", "profile_app"),
    "update": ("update", "update_app"),
    "sessions": ("sessions", "sessions_app"),
    "forge": ("forge", "forge_app"),
    "server": ("server", "server_app"),
    "ext": ("extensions", "ext_app"),
    "report": ("report", "report_app"),
    "mcp": ("mcp", "mcp_app"),
    "skill": ("skills", "skill_app"),
    "agents": ("agents", "agents_app"),
    "conventions": ("conventions", "conventions_app"),
    "tool": ("tools", "tool_app"),
    "hooks": ("hooks", "hooks_app"),
    "sandbox": ("sandbox", "sandbox_app"),
    "ide-bridge": ("ide_bridge", "ide_bridge_app"),
}
_EXPORTS = {attribute: (module, attribute) for module, attribute in _GROUPS.values()}
_EXPORTS.update(
    forge_assets_app=(".assets_cli", "assets_app"),
    mcp_auth_app=("mcp", "mcp_auth_app"),
    mcp_prompts_app=("mcp", "mcp_prompts_app"),
)


def group_export(name: str) -> typer.Typer:
    try:
        module, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(name) from None
    return getattr(import_module(f".{module}", __package__), attribute)


class LazyCommandGroup(TyperGroup):
    def list_commands(self, ctx: Any) -> list[str]:
        return sorted(set(super().list_commands(ctx)) | _GROUPS.keys())

    def get_command(self, ctx: Any, cmd_name: str) -> Any:
        command = super().get_command(ctx, cmd_name)
        if command is None and cmd_name in _GROUPS:
            # get_group retains a group even when it has only one command.
            # get_command(Typer) would flatten e.g. `server start` into `server`.
            command = get_group(group_export(_GROUPS[cmd_name][1]))
            command.name = cmd_name
            self.add_command(command, cmd_name)
        return command
