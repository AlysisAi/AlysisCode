from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from ...branding import env_get
from ...extensions.install import (
    EnableResult,
    PluginInstallError,
    TrustPromptRequest,
    disable_plugin,
    enable_plugin,
    install_plugin,
    uninstall_plugin,
)
from ...extensions.models import normalize_extension_id
from ...extensions.paths import project_extensions_path
from ...extensions.registry import find_by_id, load_registry
from ...extensions.registry import search as search_extensions
from ...extensions.state import (
    compute_effective_enabled,
    load_global_state,
    load_project_overrides,
    load_project_state,
)
from ...extensions.workspace_trust import is_workspace_trusted
from . import _patchable
from ._shared import _console, _Table

ext_app = typer.Typer(add_completion=False, help="Extension management commands.")


@ext_app.command("search")
def ext_search(
    query: str = typer.Argument(..., help="Search query for extension id/name/description/tags."),
) -> None:
    console = _console()
    try:
        registry = _patchable("load_registry", load_registry)()
    except RuntimeError as e:
        console.print(f"[red]Extension registry error:[/red] {e}")
        raise typer.Exit(code=2) from e

    matches = search_extensions(registry, query)
    if not matches:
        console.print(f"No extensions found for query: {query}")
        return

    table = _Table(title=f"Extensions matching '{query}'")
    table.add_column("id")
    table.add_column("name")
    table.add_column("version")
    table.add_column("tags")
    for entry in matches:
        tags = ", ".join(entry.tags) if entry.tags else "-"
        table.add_row(entry.id, entry.name, entry.version or "-", tags)
    console.print(table)


def _ext_component_list(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "-"


def _print_plugin_trust_request(console: Any, request: TrustPromptRequest) -> None:
    permissions = request.permissions_summary
    console.print("[bold]Plugin install trust request[/bold]")
    console.print(f"Plugin: {request.plugin_name} ({request.plugin_id})")
    console.print(f"Version: {request.version}")
    console.print(f"Source: {request.source_url}")
    pin_label = "Package content pin" if request.source_kind == "bundled" else "Commit"
    console.print(f"{pin_label}: {request.commit}")
    console.print(f"Manifest SHA-256: {request.manifest_sha256}")
    console.print(f"Description: {request.description}")
    if request.is_reinstall_with_new_commit:
        console.print(
            "[yellow]This reinstall uses a new package revision and requires renewed trust.[/yellow]"
        )
    console.print(
        "Components: "
        f"skills={len(request.components.skill_ids)}, "
        f"tools={len(request.components.tool_ids)}, "
        f"mcp_servers={len(request.components.mcp_server_ids)}, "
        f"hooks={len(request.components.hook_ids)}"
    )
    console.print(
        "Permissions: "
        f"network={'yes' if permissions.network else 'no'}, "
        f"filesystem_write={'yes' if permissions.filesystem_write else 'no'}, "
        f"env={_ext_component_list(permissions.required_env)}, "
        f"mcp_scopes={_ext_component_list(permissions.mcp_scopes)}, "
        f"hook_events={_ext_component_list(permissions.hook_events)}"
    )
    if request.security is not None:
        console.print(f"Security contact: {request.security.contact}")


def _ext_trust_prompt(*, yes: bool, ci: bool, console: Any) -> Callable[[TrustPromptRequest], bool]:
    silent_accept = ci or env_get("ALYSIS_CI") == "1"

    def prompt(request: TrustPromptRequest) -> bool:
        if silent_accept:
            return True
        _print_plugin_trust_request(console, request)
        if yes:
            return True
        return bool(typer.confirm("Trust and install this plugin?", default=False))

    return prompt


@ext_app.command("install")
def ext_install(
    source: str = typer.Argument(..., help="Registry id or pinned git source."),
    path: Path = typer.Option(Path("."), "--path", help="Repository root for project installs."),
    project: bool = typer.Option(False, "--project", help="Install into this repository."),
    yes: bool = typer.Option(False, "--yes", help="Accept the trust prompt without asking."),
    ci: bool = typer.Option(False, "--ci", help="Accept trust silently for CI automation."),
) -> None:
    console = _console()
    try:
        result = _patchable("install_plugin", install_plugin)(
            source=source,
            repo_root=path.resolve(),
            project=project,
            trust_prompt=_ext_trust_prompt(yes=yes, ci=ci, console=console),
        )
    except PluginInstallError as exc:
        console.print(f"[red]Plugin install failed:[/red] {exc}")
        for rollback_error in exc.rollback_errors:
            console.print(f"[yellow]Rollback issue:[/yellow] {rollback_error}")
        raise typer.Exit(code=1) from exc
    except RuntimeError as exc:
        console.print(f"[red]Plugin install error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    console.print(
        f"Installed plugin {result.plugin_id} {result.version} "
        f"({result.scope}, {result.commit[:12]})."
    )


@ext_app.command("uninstall")
def ext_uninstall(
    plugin_id: str = typer.Argument(..., help="Plugin id."),
    path: Path = typer.Option(Path("."), "--path", help="Repository root for project installs."),
    project: bool = typer.Option(False, "--project", help="Uninstall from this repository."),
    yes: bool = typer.Option(False, "--yes", help="Confirm uninstall without asking."),
) -> None:
    console = _console()
    scope = "project" if project else "user"
    if not yes and not typer.confirm(
        f"Uninstall plugin {plugin_id!r} from {scope} scope?", default=False
    ):
        console.print("Plugin uninstall cancelled.")
        raise typer.Exit(code=1)
    try:
        result = _patchable("uninstall_plugin", uninstall_plugin)(
            plugin_id=plugin_id,
            repo_root=path.resolve(),
            project=project,
        )
    except PluginInstallError as exc:
        console.print(f"[red]Plugin uninstall failed:[/red] {exc}")
        for rollback_error in exc.rollback_errors:
            console.print(f"[yellow]Removal issue:[/yellow] {rollback_error}")
        raise typer.Exit(code=1) from exc
    except RuntimeError as exc:
        console.print(f"[red]Plugin uninstall error:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    console.print(f"Uninstalled plugin {result.plugin_id} from {result.scope} scope.")


def _print_enable_result(console: Any, result: EnableResult) -> None:
    noop = " (no-op)" if result.no_op else ""
    console.print(f"Plugin {result.plugin_id} is {result.new_state} in {result.scope} scope{noop}.")


def _confirm_extension_state_change(
    *,
    plugin_id: str,
    scope: str,
    action: str,
    yes: bool,
    console: Any,
) -> bool:
    if yes:
        return True
    if typer.confirm(
        f"{action.capitalize()} plugin {plugin_id!r} in {scope} scope?", default=False
    ):
        return True
    console.print(f"Plugin {action} cancelled.")
    return False


def _installed_record_for_id(state: Any, ext_id: str) -> Any | None:
    normalized = normalize_extension_id(ext_id)
    for installed_id, installed in state.installed.items():
        if normalize_extension_id(installed_id) == normalized:
            return installed
    return None


def _workspace_trust_status(repo_root: Path) -> str:
    overrides_path = project_extensions_path(repo_root)
    if not overrides_path.exists():
        return "no overrides file"
    try:
        raw_bytes = overrides_path.read_bytes()
    except OSError as exc:
        raise RuntimeError(f"Failed to read {overrides_path}") from exc
    overrides_sha256 = hashlib.sha256(raw_bytes).hexdigest()
    return (
        f"trusted ({overrides_sha256})"
        if is_workspace_trusted(repo_root=repo_root, overrides_sha256=overrides_sha256)
        else f"untrusted ({overrides_sha256})"
    )


@ext_app.command("enable")
def ext_enable(
    plugin_id: str = typer.Argument(..., help="Plugin id."),
    path: Path = typer.Option(Path("."), "--path", help="Repository root for project overrides."),
    project: bool = typer.Option(False, "--project", help="Enable in this repository."),
    yes: bool = typer.Option(False, "--yes", help="Confirm enable without asking."),
) -> None:
    console = _console()
    scope = "project" if project else "user"
    if not _confirm_extension_state_change(
        plugin_id=plugin_id,
        scope=scope,
        action="enable",
        yes=yes,
        console=console,
    ):
        raise typer.Exit(code=1)
    try:
        result = _patchable("enable_plugin", enable_plugin)(
            plugin_id=plugin_id,
            repo_root=path.resolve(),
            project=project,
        )
    except PluginInstallError as exc:
        console.print(f"[red]Plugin enable failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except RuntimeError as exc:
        console.print(f"[red]Plugin enable error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    _print_enable_result(console, result)


@ext_app.command("disable")
def ext_disable(
    plugin_id: str = typer.Argument(..., help="Plugin id."),
    path: Path = typer.Option(Path("."), "--path", help="Repository root for project overrides."),
    project: bool = typer.Option(False, "--project", help="Disable in this repository."),
    yes: bool = typer.Option(False, "--yes", help="Confirm disable without asking."),
) -> None:
    console = _console()
    scope = "project" if project else "user"
    if not _confirm_extension_state_change(
        plugin_id=plugin_id,
        scope=scope,
        action="disable",
        yes=yes,
        console=console,
    ):
        raise typer.Exit(code=1)
    try:
        result = _patchable("disable_plugin", disable_plugin)(
            plugin_id=plugin_id,
            repo_root=path.resolve(),
            project=project,
        )
    except PluginInstallError as exc:
        console.print(f"[red]Plugin disable failed:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    except RuntimeError as exc:
        console.print(f"[red]Plugin disable error:[/red] {exc}")
        raise typer.Exit(code=2) from exc
    _print_enable_result(console, result)


@ext_app.command("info")
def ext_info(
    ext_id: str = typer.Argument(..., help="Extension id."),
    path: Path = typer.Option(Path("."), "--path", help="Repository root for project overrides."),
) -> None:
    console = _console()
    try:
        registry = _patchable("load_registry", load_registry)()
        global_state = load_global_state()
        project_overrides = load_project_overrides(path.resolve())
        project_state = load_project_state(path.resolve())
    except RuntimeError as e:
        console.print(f"[red]Extensions state error:[/red] {e}")
        raise typer.Exit(code=2) from e

    entry = find_by_id(registry, ext_id)
    normalized = normalize_extension_id(entry.id if entry is not None else ext_id)
    global_record = _installed_record_for_id(global_state, normalized)
    project_record = _installed_record_for_id(project_state, normalized)
    installed_record = global_record or project_record
    if entry is None and installed_record is None:
        console.print(f"[red]Extension not found:[/red] {ext_id}")
        raise typer.Exit(code=1)

    effective_enabled = compute_effective_enabled(global_state, project_overrides)
    installed_scopes = []
    if global_record is not None:
        installed_scopes.append("user")
    if project_record is not None:
        installed_scopes.append("project")
    user_enabled = normalized in {
        normalize_extension_id(item) for item in global_state.enabled
    } or bool(global_record and global_record.enabled)
    if normalized in {normalize_extension_id(item) for item in project_overrides.enabled}:
        project_override_state = "enabled"
    elif normalized in {normalize_extension_id(item) for item in project_overrides.disabled}:
        project_override_state = "disabled"
    else:
        project_override_state = "absent"

    table = _Table(title=f"Extension: {entry.id if entry is not None else ext_id}")
    table.add_column("field")
    table.add_column("value")
    table.add_row("id", entry.id if entry is not None else ext_id)
    table.add_row(
        "name",
        entry.name
        if entry is not None
        else ((installed_record.id or ext_id) if installed_record else ext_id),
    )
    table.add_row("description", entry.description if entry is not None else "-")
    table.add_row("repo", entry.repo if entry is not None else "-")
    bundled = getattr(entry or installed_record, "source_kind", "git") == "bundled"
    table.add_row(
        "package content pin" if bundled else "commit",
        entry.commit if entry is not None else installed_record.commit or "-",
    )
    table.add_row(
        "version", (entry.version if entry is not None else installed_record.version) or "-"
    )
    table.add_row("tags", ", ".join(entry.tags) if entry is not None and entry.tags else "-")
    table.add_row(
        "permissions",
        ", ".join(entry.permissions) if entry is not None and entry.permissions else "-",
    )
    table.add_row("installed", "yes" if installed_record is not None else "no")
    table.add_row("installed scopes", ", ".join(installed_scopes) if installed_scopes else "-")
    table.add_row("enabled (user)", "yes" if user_enabled else "no")
    table.add_row("enabled (project override)", project_override_state)
    table.add_row("enabled (effective)", "yes" if normalized in effective_enabled else "no")
    table.add_row("workspace trust", _workspace_trust_status(path.resolve()))
    if installed_record is not None:
        table.add_row("installed trust", installed_record.trust or "-")
        table.add_row("installed source", installed_record.source or "-")
        table.add_row(
            "installed package content pin" if bundled else "installed commit",
            installed_record.commit or "-",
        )
        table.add_row("manifest_sha256", installed_record.manifest_sha256 or "-")
        table.add_row("installed_at", installed_record.installed_at or "-")
        table.add_row("source_url", installed_record.source_url or "-")
        table.add_row("scope", installed_record.scope or "-")
        table.add_row("component_ids", json.dumps(installed_record.component_ids, sort_keys=True))
    console.print(table)


@ext_app.command("list")
def ext_list(
    path: Path = typer.Option(Path("."), "--path", help="Repository root for project overrides."),
) -> None:
    console = _console()
    try:
        global_state = load_global_state()
        project_overrides = load_project_overrides(path.resolve())
    except RuntimeError as e:
        console.print(f"[red]Extensions state error:[/red] {e}")
        raise typer.Exit(code=2) from e

    if not global_state.installed:
        console.print("No extensions installed.")
        return

    effective_enabled = compute_effective_enabled(global_state, project_overrides)
    table = _Table(title="Installed Extensions")
    table.add_column("id")
    table.add_column("enabled")
    table.add_column("version")
    table.add_column("trust")
    table.add_column("revision")
    for ext_id in sorted(global_state.installed):
        installed = global_state.installed[ext_id]
        enabled = "yes" if normalize_extension_id(ext_id) in effective_enabled else "no"
        table.add_row(
            ext_id,
            enabled,
            installed.version or "-",
            installed.trust or "-",
            installed.commit or "-",
        )
    console.print(table)
