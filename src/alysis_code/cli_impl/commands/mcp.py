from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import typer

from ...config import ConfigError
from ...mcp.config import load_resolved_mcp_config
from ...mcp.manager import McpManager, create_mcp_manager
from ...mcp.models import ResolvedMcpServer
from ...mcp.oauth import (
    McpOAuthError,
    discover_authorization_server_metadata,
    discover_authorization_server_metadata_from_url,
    discover_protected_resource_metadata,
    resolve_requested_scopes,
)
from ...mcp.oauth_runtime import perform_authorization_code_login
from ...mcp.oauth_store import (
    McpOAuthTokenStoreError,
    delete_oauth_token_record,
    load_oauth_token_record,
)
from ...mcp.person_approval import exempt_from_rule, marked_read_tools, tool_server_approval
from ...runtime_kind import RuntimeKind, normalize_runtime_kind
from ._shared import _console, _resolve_tool_workspace_root, _Table

if TYPE_CHECKING:
    from rich.console import Console


mcp_app = typer.Typer(add_completion=False, help="Manual MCP inspection commands.")
mcp_prompts_app = typer.Typer(add_completion=False, help="Manual MCP prompt commands.")
mcp_auth_app = typer.Typer(add_completion=False, help="Manual MCP OAuth commands.")
mcp_app.add_typer(mcp_prompts_app, name="prompts")
mcp_app.add_typer(mcp_auth_app, name="auth")


_MANUAL_MCP_PROMPT_RUNTIME_KINDS = (
    RuntimeKind.INTERACTIVE_CHAT,
    RuntimeKind.ONE_SHOT,
    RuntimeKind.FORGE_EXEC,
)


def _parse_manual_mcp_prompt_runtime(value: RuntimeKind | str) -> RuntimeKind:
    try:
        runtime_kind = normalize_runtime_kind(value)
    except ConfigError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if runtime_kind not in _MANUAL_MCP_PROMPT_RUNTIME_KINDS:
        allowed = ", ".join(kind.value for kind in _MANUAL_MCP_PROMPT_RUNTIME_KINDS)
        raise typer.BadParameter(
            f"manual MCP prompt access supports only these runtime profiles: {allowed}."
        )
    return runtime_kind


def _manual_mcp_manager_for_path(*, path: Path, runtime_kind: RuntimeKind | str) -> McpManager:
    workspace_root = _resolve_tool_workspace_root(path=path)
    return create_mcp_manager(
        workspace_root=workspace_root,
        runtime_kind=_parse_manual_mcp_prompt_runtime(runtime_kind),
    )


def _manual_mcp_resolved_config_for_path(*, path: Path) -> Any:
    workspace_root = _resolve_tool_workspace_root(path=path)
    return load_resolved_mcp_config(workspace_root=workspace_root)


def _resolve_mcp_server_by_id(*, resolved_config: Any, server_id: str) -> ResolvedMcpServer:
    normalized_server_id = str(server_id or "").strip().lower()
    for server in resolved_config.servers:
        if server.id == normalized_server_id:
            return server
    raise typer.BadParameter(f"Unknown MCP server id: {server_id}")


def _require_http_oauth_server_for_auth(
    *, server: ResolvedMcpServer, command_name: str
) -> ResolvedMcpServer:
    if server.transport != "http":
        raise typer.BadParameter(
            f"MCP auth {command_name} requires an HTTP MCP server: {server.id}"
        )
    if server.oauth is None:
        raise typer.BadParameter(
            f"MCP server '{server.id}' does not have an oauth block in user config."
        )
    return server


def _format_mcp_oauth_timestamp(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _mcp_oauth_expiry_summary(record: Any | None) -> str:
    if record is None:
        return "-"
    expires_text = _format_mcp_oauth_timestamp(record.expires_at)
    if record.expires_at <= datetime.now(UTC):
        return f"expired ({expires_text})"
    return expires_text


def _mcp_auth_status_table(*, rows: list[dict[str, str]]) -> Any:
    table = _Table(title=f"MCP OAuth Status ({len(rows)})")
    table.add_column("server_id", no_wrap=True)
    table.add_column("auth_enabled", no_wrap=True)
    table.add_column("token", no_wrap=True)
    table.add_column("expires")
    table.add_column("granted_scopes")
    for row in rows:
        table.add_row(
            row["server_id"],
            row["auth_enabled"],
            row["token"],
            row["expires"],
            row["granted_scopes"],
        )
    return table


def _build_mcp_auth_status_rows(
    *, resolved_config: Any, server_id: str | None
) -> list[dict[str, str]]:
    selected_servers: list[ResolvedMcpServer]
    if server_id is None:
        selected_servers = [
            server
            for server in resolved_config.servers
            if server.transport == "http" and server.oauth is not None
        ]
    else:
        selected_servers = [
            _resolve_mcp_server_by_id(resolved_config=resolved_config, server_id=server_id)
        ]
    rows: list[dict[str, str]] = []
    for server in selected_servers:
        token_record = None
        if server.transport == "http" and server.oauth is not None:
            token_record = load_oauth_token_record(server.id)
        rows.append(
            {
                "server_id": server.id,
                "auth_enabled": "yes"
                if server.transport == "http" and server.oauth is not None
                else "no",
                "token": "present" if token_record is not None else "absent",
                "expires": _mcp_oauth_expiry_summary(token_record),
                "granted_scopes": (
                    ", ".join(token_record.granted_scopes)
                    if token_record and token_record.granted_scopes
                    else "-"
                ),
            }
        )
    return rows


def _parse_prompt_cli_arguments(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for raw_item in values:
        item = str(raw_item or "")
        if "=" not in item:
            raise typer.BadParameter("--arg values must use key=value.")
        raw_key, raw_value = item.split("=", 1)
        key = raw_key.strip()
        if not key:
            raise typer.BadParameter("--arg keys cannot be empty.")
        if key in parsed:
            raise typer.BadParameter(f"duplicate --arg key: {key}")
        parsed[key] = raw_value
    return parsed


def _print_json_payload(*, console: Console, payload: dict[str, Any]) -> None:
    console.file.write(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n")
    console.file.flush()


def _mcp_prompt_argument_label(arguments: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for raw_item in arguments:
        if not isinstance(raw_item, dict):
            continue
        name = str(raw_item.get("name") or "").strip()
        if not name:
            continue
        required = bool(raw_item.get("required"))
        parts.append(f"{name}{'*' if required else ''}")
    return ", ".join(parts) if parts else "-"


def _mcp_prompt_list_table(*, prompts: list[dict[str, Any]]) -> Any:
    table = _Table(title=f"MCP Prompts ({len(prompts)})")
    table.add_column("server_id", no_wrap=True)
    table.add_column("name", no_wrap=True)
    table.add_column("title", no_wrap=True)
    table.add_column("arguments")
    table.add_column("description")
    for prompt in prompts:
        title = str(prompt.get("title") or "-").strip() or "-"
        description = str(prompt.get("description") or "-").strip() or "-"
        table.add_row(
            str(prompt.get("server_id") or "").strip(),
            str(prompt.get("name") or "").strip(),
            title,
            _mcp_prompt_argument_label(list(prompt.get("arguments") or [])),
            description,
        )
    return table


def _mcp_status_table(*, rows: list[dict[str, str]]) -> Any:
    table = _Table(title=f"MCP Status ({len(rows)})")
    table.add_column("server_id", no_wrap=True)
    table.add_column("transport", no_wrap=True)
    table.add_column("tools")
    table.add_column("resources")
    table.add_column("prompts")
    table.add_column("operator_action")
    for row in rows:
        table.add_row(
            row["server_id"],
            row["transport"],
            row["tools"],
            row["resources"],
            row["prompts"],
            row["operator_action"],
        )
    return table


def _mcp_status_surface_label(*, loaded: bool, count: int | None, stale: bool) -> str:
    if loaded:
        count_label = str(count if count is not None else 0)
        label = f"loaded:{count_label}"
    else:
        label = "not_loaded"
    if stale:
        label += " stale:yes"
    return label


def _mcp_status_tools(bindings: list[Any], *, marked: frozenset[str]) -> list[dict[str, Any]]:
    return [
        {
            "name": binding.tool_name,
            "server_says_read_only": getattr(binding, "read_only_hint", False) is True,
            "server_says_destructive": getattr(binding, "destructive_hint", False) is True,
            "marked_read_by_you": binding.tool_name in marked,
        }
        for binding in sorted(bindings, key=lambda item: item.tool_name)
    ]


def _mcp_status_payload(
    *,
    manager: McpManager,
    bootstrap_errors: list[str],
    prompt_errors: dict[str, str],
) -> dict[str, Any]:
    metadata = manager.catalog_snapshot_metadata()
    server_catalogs = {
        str(entry.get("server_id") or ""): entry
        for entry in metadata.get("server_catalogs") or []
        if isinstance(entry, dict)
    }
    prompt_catalogs = {
        str(entry.get("server_id") or ""): entry
        for entry in metadata.get("prompt_server_catalogs") or []
        if isinstance(entry, dict)
    }
    prompt_enabled_ids = {
        str(server_id) for server_id in metadata.get("prompt_enabled_server_ids") or []
    }
    tool_stale_ids = {str(server_id) for server_id in metadata.get("tool_stale_server_ids") or []}
    resource_stale_ids = {
        str(server_id) for server_id in metadata.get("resource_stale_server_ids") or []
    }
    prompt_stale_ids = {
        str(server_id) for server_id in metadata.get("prompt_stale_server_ids") or []
    }
    startup_errors = manager.server_startup_errors()
    # What a chat engine with the same environment will do for each call (desktop rule).
    approval = tool_server_approval()
    bindings_by_server: dict[str, list[Any]] = {}
    for binding in getattr(manager, "tool_bindings", ()):
        server_id = getattr(binding, "server_id", None)
        if isinstance(server_id, str):
            bindings_by_server.setdefault(server_id, []).append(binding)
    rows: list[dict[str, str]] = []
    json_rows: list[dict[str, Any]] = []
    for server in manager.active_servers:
        server_catalog = server_catalogs.get(server.id)
        prompt_catalog = prompt_catalogs.get(server.id)
        tool_loaded = server_catalog is not None
        tool_count = (
            int(server_catalog.get("exposed_tool_count") or 0)
            if server_catalog is not None
            else None
        )
        resource_loaded = bool(
            server_catalog is not None and server_catalog.get("resources_snapshot_loaded")
        )
        resource_count = (
            int(server_catalog.get("snapshotted_resource_count") or 0)
            if resource_loaded and server_catalog is not None
            else None
        )
        prompt_loaded = bool(prompt_catalog and prompt_catalog.get("prompt_snapshot_loaded"))
        prompt_failed = bool(prompt_catalog and prompt_catalog.get("prompt_snapshot_failed"))
        prompt_count = (
            int(prompt_catalog.get("snapshotted_prompt_count") or 0)
            if prompt_catalog is not None
            else None
        )
        prompt_stale = server.id in prompt_stale_ids
        tool_stale = server.id in tool_stale_ids
        resource_stale = server.id in resource_stale_ids
        actions: list[str] = []
        if tool_stale:
            actions.append("new session for tools")
        if resource_stale:
            actions.append("new session for resources")
        if prompt_stale:
            actions.append("mcp prompts --refresh")
        if server.id in prompt_errors:
            actions.append("prompt snapshot error")
        action_label = "; ".join(actions) if actions else "-"
        if prompt_failed:
            prompt_label = "failed"
        elif server.id in prompt_enabled_ids:
            prompt_label = _mcp_status_surface_label(
                loaded=prompt_loaded,
                count=prompt_count,
                stale=prompt_stale,
            )
            prompt_label += " refresh:yes"
        else:
            prompt_label = "disabled"
        startup_error = startup_errors.get(server.id)
        row = {
            "server_id": server.id,
            "transport": server.transport,
            "tools": (
                "failed"
                if startup_error is not None
                else _mcp_status_surface_label(
                    loaded=tool_loaded,
                    count=tool_count,
                    stale=tool_stale,
                )
            ),
            "resources": _mcp_status_surface_label(
                loaded=resource_loaded,
                count=resource_count,
                stale=resource_stale,
            ),
            "prompts": prompt_label,
            "operator_action": action_label,
        }
        rows.append(row)
        json_rows.append(
            {
                "server_id": server.id,
                "transport": server.transport,
                "tools_snapshot_loaded": tool_loaded,
                "tools_snapshot_count": tool_count,
                "tools_snapshot_stale": tool_stale,
                "resources_snapshot_loaded": resource_loaded,
                "resources_snapshot_count": resource_count,
                "resources_snapshot_stale": resource_stale,
                "prompts_enabled": server.id in prompt_enabled_ids,
                "prompts_snapshot_loaded": prompt_loaded,
                "prompts_snapshot_count": prompt_count,
                "prompts_snapshot_stale": prompt_stale,
                "manual_prompt_refresh_available": server.id in prompt_enabled_ids,
                "operator_action": actions,
                "prompt_error": prompt_errors.get(server.id),
                "startup_error": startup_error,
                "call_approval": (
                    "every_call"
                    if approval is not None and not exempt_from_rule(approval, server)
                    else "permission_mode"
                ),
                "tools": _mcp_status_tools(
                    bindings_by_server.get(server.id, []),
                    marked=marked_read_tools(approval, server),
                ),
            }
        )
    return {
        "rows": json_rows,
        "table_rows": rows,
        "tool_server_approval": {"version": 1, "active": approval is not None},
        "bootstrap_errors": bootstrap_errors,
        "prompt_errors": prompt_errors,
        "metadata": metadata,
    }


def _print_prompt_get_payload(*, console: Console, payload: dict[str, Any]) -> None:
    from rich.text import Text

    metadata = _Table(title=f"MCP Prompt: {payload['server_id']}/{payload['name']}")
    metadata.add_column("field")
    metadata.add_column("value")
    metadata.add_row("server_id", str(payload.get("server_id") or ""))
    metadata.add_row("name", str(payload.get("name") or ""))
    metadata.add_row("title", str(payload.get("title") or "-"))
    metadata.add_row("description", str(payload.get("description") or "-"))
    metadata.add_row(
        "arguments",
        _mcp_prompt_argument_label(list(payload.get("arguments") or [])),
    )
    metadata.add_row("message_count", str(payload.get("message_count") or 0))
    metadata.add_row("content_summary", str(payload.get("content_summary") or "-"))
    if payload.get("refresh_performed"):
        metadata.add_row("refresh_performed", "yes")
    if payload.get("snapshot_stale"):
        metadata.add_row("snapshot_stale", "yes")
    if isinstance(payload.get("applied_arguments"), dict):
        metadata.add_row(
            "applied_arguments",
            json.dumps(payload["applied_arguments"], ensure_ascii=True, sort_keys=True),
        )
    console.print(metadata)

    message_rows = list(payload.get("messages") or [])
    if not message_rows:
        return
    messages_table = _Table(title="Prompt Messages")
    messages_table.add_column("#", no_wrap=True)
    messages_table.add_column("role", no_wrap=True)
    messages_table.add_column("summary")
    messages_table.add_column("text")
    for index, raw_message in enumerate(message_rows, start=1):
        if not isinstance(raw_message, dict):
            continue
        messages_table.add_row(
            str(index),
            str(raw_message.get("role") or "").strip(),
            str(raw_message.get("content_summary") or "-"),
            Text(str(raw_message.get("text") or "-")),
        )
    console.print(messages_table)


@mcp_auth_app.command("login")
def mcp_auth_login(
    server_id: str = typer.Argument(..., help="Exact MCP server id."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    try:
        resolved_config = _manual_mcp_resolved_config_for_path(path=path)
        server = _require_http_oauth_server_for_auth(
            server=_resolve_mcp_server_by_id(resolved_config=resolved_config, server_id=server_id),
            command_name="login",
        )
        assert server.url is not None
        assert server.oauth is not None
        configured_scopes = tuple(server.oauth.scopes or ())
        protected_metadata = None
        if not configured_scopes or server.oauth.authorization_server_url is None:
            protected_metadata = discover_protected_resource_metadata(
                server_id=server.id,
                resource_server_url=server.url,
            )
        if server.oauth.authorization_server_url is not None:
            metadata = discover_authorization_server_metadata_from_url(
                server_id=server.id,
                authorization_server_url=server.oauth.authorization_server_url,
            )
        elif protected_metadata is not None:
            metadata = discover_authorization_server_metadata_from_url(
                server_id=server.id,
                authorization_server_url=protected_metadata.authorization_servers[0],
            )
        else:
            metadata = discover_authorization_server_metadata(
                server_id=server.id,
                resource_server_url=server.url,
            )
        requested_scopes = resolve_requested_scopes(
            configured_scopes=configured_scopes,
            challenge_scope=None,
            metadata_scopes_supported=(
                protected_metadata.scopes_supported if protected_metadata is not None else None
            ),
            existing_granted_scopes=None,
            purpose="login",
        )
        result = perform_authorization_code_login(
            server_id=server.id,
            authorization_server_metadata=metadata,
            resource_server_url=server.url,
            client_id=server.oauth.client_id,
            scopes=requested_scopes,
            redirect_host=server.oauth.redirect_host,
            redirect_port=server.oauth.redirect_port,
            output_write=lambda message: console.print(message, highlight=False),
        )
        scopes_text = (
            ", ".join(result.token_record.granted_scopes)
            if result.token_record.granted_scopes
            else "-"
        )
        console.print(f"OAuth login succeeded for MCP server '{server.id}'.")
        console.print(f"Expires: {_format_mcp_oauth_timestamp(result.token_record.expires_at)}")
        console.print(f"Granted scopes: {scopes_text}")
    except (ConfigError, McpOAuthError, McpOAuthTokenStoreError, typer.BadParameter) as exc:
        message = str(exc).replace("\n", " ")
        console.print(f"[red]{message}[/red]", soft_wrap=True)
        raise typer.Exit(code=1) from exc


@mcp_auth_app.command("status")
def mcp_auth_status(
    server: str | None = typer.Option(
        None, "--server", help="Optional exact MCP server id filter."
    ),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    try:
        resolved_config = _manual_mcp_resolved_config_for_path(path=path)
        rows = _build_mcp_auth_status_rows(resolved_config=resolved_config, server_id=server)
        if not rows:
            console.print("No OAuth-enabled HTTP MCP servers configured.")
            return
        console.print(_mcp_auth_status_table(rows=rows))
    except (ConfigError, McpOAuthTokenStoreError, typer.BadParameter) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@mcp_auth_app.command("logout")
def mcp_auth_logout(
    server_id: str = typer.Argument(..., help="Exact MCP server id."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
) -> None:
    console = _console()
    try:
        resolved_config = _manual_mcp_resolved_config_for_path(path=path)
        server = _require_http_oauth_server_for_auth(
            server=_resolve_mcp_server_by_id(resolved_config=resolved_config, server_id=server_id),
            command_name="logout",
        )
        removed = delete_oauth_token_record(server.id)
        if removed:
            console.print(f"Cleared stored OAuth tokens for MCP server '{server.id}'.")
            return
        console.print(f"No stored OAuth tokens found for MCP server '{server.id}'.")
    except (ConfigError, McpOAuthTokenStoreError, typer.BadParameter) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@mcp_app.command("status")
def mcp_status(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
    runtime: str = typer.Option(
        RuntimeKind.INTERACTIVE_CHAT.value,
        "--runtime",
        help=(
            "Runtime profile used to resolve enabled MCP servers: "
            "interactive_chat, one_shot, or forge_exec."
        ),
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit deterministic JSON output."),
) -> None:
    console = _console()
    manager: McpManager | None = None
    bootstrap_errors: list[str] = []
    prompt_errors: dict[str, str] = {}
    try:
        manager = _manual_mcp_manager_for_path(path=path, runtime_kind=runtime)
        try:
            _ = manager.tool_bindings
        except (ConfigError, RuntimeError) as exc:
            bootstrap_errors.append(str(exc))
        prompt_enabled_ids = [
            str(server_id)
            for server_id in manager.catalog_snapshot_metadata().get("prompt_enabled_server_ids")
            or []
        ]
        for prompt_server_id in prompt_enabled_ids:
            try:
                manager.list_prompts(server_id=prompt_server_id, limit=1)
            except (ConfigError, RuntimeError) as exc:
                prompt_errors[prompt_server_id] = str(exc)
        payload = _mcp_status_payload(
            manager=manager,
            bootstrap_errors=bootstrap_errors,
            prompt_errors=prompt_errors,
        )
        if as_json:
            _print_json_payload(
                console=console,
                payload={key: value for key, value in payload.items() if key != "table_rows"},
            )
            return
        table_rows = list(payload.get("table_rows") or [])
        if not table_rows:
            console.print("No active MCP servers for this runtime.")
            return
        from rich.markup import escape

        console.print(_mcp_status_table(rows=table_rows))
        for json_row in payload.get("rows") or []:
            if json_row.get("startup_error"):
                console.print(
                    f"[yellow]MCP server {escape(str(json_row['server_id']))} did not start:"
                    f"[/yellow] {escape(str(json_row['startup_error']))}"
                )
        for error in bootstrap_errors:
            console.print(f"[yellow]Snapshot warning:[/yellow] {error}")
        for server_id, error in prompt_errors.items():
            console.print(f"[yellow]Prompt warning for {server_id}:[/yellow] {error}")
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        if manager is not None:
            manager.close()


@mcp_prompts_app.command("list")
def mcp_prompts_list(
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
    runtime: str = typer.Option(
        RuntimeKind.INTERACTIVE_CHAT.value,
        "--runtime",
        help=(
            "Runtime profile used to resolve enabled MCP servers: "
            "interactive_chat, one_shot, or forge_exec."
        ),
    ),
    server: str | None = typer.Option(
        None, "--server", help="Optional exact MCP server id filter."
    ),
    query: str | None = typer.Option(None, "--query", help="Optional text filter."),
    limit: int = typer.Option(20, "--limit", min=1, max=50, help="Maximum prompts to display."),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Explicitly refresh the targeted prompt snapshot before listing.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit deterministic JSON output."),
) -> None:
    console = _console()
    manager: McpManager | None = None
    try:
        manager = _manual_mcp_manager_for_path(path=path, runtime_kind=runtime)
        try:
            payload = manager.list_prompts(
                server_id=server,
                query=query,
                limit=limit,
                refresh=refresh,
            )
        except (ConfigError, RuntimeError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        prompts = list(payload.get("prompts") or [])
        if as_json:
            _print_json_payload(console=console, payload=payload)
            return
        if not prompts:
            console.print("No MCP prompts available.")
            return
        console.print(_mcp_prompt_list_table(prompts=prompts))
        console.print(
            f"[dim]Returned:[/dim] {payload['returned_count']}  "
            f"[dim]Matching:[/dim] {payload['matching_count']}  "
            f"[dim]Snapshot:[/dim] {payload['total_snapshot_count']}"
        )
        if payload.get("refresh_performed"):
            console.print("[dim]Prompt snapshot refreshed explicitly.[/dim]")
        stale_server_ids = payload.get("stale_server_ids")
        if stale_server_ids:
            console.print(
                "[yellow]Stale prompt snapshot:[/yellow] "
                f"{', '.join(str(item) for item in stale_server_ids)}"
            )
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        if manager is not None:
            manager.close()


@mcp_prompts_app.command("get")
def mcp_prompts_get(
    server_id: str = typer.Argument(..., help="Exact MCP server id."),
    prompt_name: str = typer.Argument(..., help="Exact MCP prompt name."),
    path: Path = typer.Option(Path("."), "--path", help="Workspace or repository path."),
    runtime: str = typer.Option(
        RuntimeKind.INTERACTIVE_CHAT.value,
        "--runtime",
        help=(
            "Runtime profile used to resolve enabled MCP servers: "
            "interactive_chat, one_shot, or forge_exec."
        ),
    ),
    arg: list[str] = typer.Option(None, "--arg", help="Prompt string argument as key=value."),
    refresh: bool = typer.Option(
        False,
        "--refresh",
        help="Explicitly refresh this server's prompt snapshot before fetching.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Emit deterministic JSON output."),
) -> None:
    console = _console()
    manager: McpManager | None = None
    try:
        manager = _manual_mcp_manager_for_path(path=path, runtime_kind=runtime)
        try:
            payload = manager.get_prompt(
                server_id=server_id,
                prompt_name=prompt_name,
                arguments=_parse_prompt_cli_arguments(list(arg or [])),
                refresh=refresh,
            )
        except (ConfigError, RuntimeError) as exc:
            console.print(f"[red]{exc}[/red]")
            raise typer.Exit(code=1) from exc
        if as_json:
            _print_json_payload(console=console, payload=payload)
            return
        _print_prompt_get_payload(console=console, payload=payload)
    except ConfigError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc
    finally:
        if manager is not None:
            manager.close()
