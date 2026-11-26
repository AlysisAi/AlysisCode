from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from ..branding import canonical_user_config_dir, env_get, env_get_from
from ..runtime_kind import RuntimeKind
from .errors import McpConfigError as ConfigError
from .models import (
    ProjectMcpConfigFile,
    PromptsMode,
    ResolvedMcpConfig,
    ResolvedMcpHttpOAuthConfig,
    ResolvedMcpServer,
    ResourcesMode,
    RootsMode,
    UserMcpConfigFile,
    UserMcpHttpServer,
    UserMcpStdioServer,
)

_FORBIDDEN_PROJECT_SERVER_FIELDS = {
    "args",
    "command",
    "env",
    "headers",
    "oauth",
    "tool_prefix",
    "transport",
    "trust",
    "url",
}
_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _mcp_config_dir() -> Path:
    override = env_get("ALYSIS_CONFIG_DIR")
    if override:
        return Path(override)
    return canonical_user_config_dir()


def user_mcp_config_path() -> Path:
    return _mcp_config_dir() / "mcp.json"


def project_mcp_config_path(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".alysis" / "mcp.json"


def redact_sensitive_mapping(values: Mapping[str, str] | None) -> dict[str, str]:
    if not values:
        return {}
    return {str(key): "[redacted]" for key in values}


def expand_env_placeholders(
    value: str,
    *,
    env: Mapping[str, str],
    source_path: Path,
    field_path: str,
) -> str:
    text = str(value)

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        resolved = env_get_from(env, var_name)
        if resolved is None:
            raise ConfigError(
                f"Invalid MCP config: {source_path}: {field_path}: missing environment variable "
                f"${{{var_name}}}"
            )
        return resolved

    return _ENV_PATTERN.sub(_replace, text)


def _read_json_object(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        raise ConfigError(f"Failed to read MCP config: {path}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"Invalid MCP config format (expected JSON object): {path}")
    return raw


def _normalize_server_keys(path: Path, raw: dict[str, Any]) -> dict[str, Any]:
    servers = raw.get("servers")
    if servers is None:
        raw["servers"] = {}
        return raw
    if not isinstance(servers, dict):
        raise ConfigError(f"Invalid MCP config: {path}: servers must be an object.")
    normalized_servers: dict[str, Any] = {}
    for raw_id, payload in servers.items():
        from .models import normalize_server_id

        try:
            server_id = normalize_server_id(raw_id)
        except ConfigError as exc:
            raise ConfigError(f"Invalid MCP config: {path}: {exc}") from exc
        if server_id in normalized_servers:
            raise ConfigError(
                f"Invalid MCP config: {path}: duplicate server id after normalization: {server_id}"
            )
        normalized_servers[server_id] = payload
    raw["servers"] = normalized_servers
    return raw


def _raise_project_override_error(path: Path, *, server_id: str, field_name: str) -> None:
    raise ConfigError(
        f"Invalid MCP config: {path}: server '{server_id}': field '{field_name}': "
        "project config cannot override this field."
    )


def _raise_project_narrowing_error(
    path: Path,
    *,
    server_id: str,
    field_name: str,
    reason: str,
) -> None:
    raise ConfigError(
        f"Invalid MCP config: {path}: server '{server_id}': field '{field_name}': {reason}"
    )


def _validate_project_override_boundary(path: Path, raw: dict[str, Any]) -> None:
    servers = raw.get("servers")
    if not isinstance(servers, dict):
        return
    for server_id, payload in servers.items():
        if not isinstance(payload, dict):
            continue
        for field_name in sorted(_FORBIDDEN_PROJECT_SERVER_FIELDS):
            if field_name in payload:
                _raise_project_override_error(
                    path,
                    server_id=str(server_id),
                    field_name=field_name,
                )


def _format_validation_error(path: Path, exc: ValidationError) -> ConfigError:
    details: list[str] = []
    for err in exc.errors(include_url=False):
        loc = ".".join(str(part) for part in err.get("loc", ()))
        msg = str(err.get("msg") or "invalid value")
        detail = f"{path}: {msg}" if not loc else f"{path}: {loc}: {msg}"
        details.append(detail)
    joined = "\n".join(details) if details else f"{path}: invalid MCP config"
    return ConfigError(f"Invalid MCP config:\n{joined}")


def _load_user_mcp_config(path: Path) -> UserMcpConfigFile:
    raw = _normalize_server_keys(path, _read_json_object(path))
    try:
        return UserMcpConfigFile.model_validate(raw)
    except ValidationError as exc:
        raise _format_validation_error(path, exc) from exc


def _load_project_mcp_config(path: Path) -> ProjectMcpConfigFile:
    raw = _normalize_server_keys(path, _read_json_object(path))
    _validate_project_override_boundary(path, raw)
    try:
        return ProjectMcpConfigFile.model_validate(raw)
    except ValidationError as exc:
        raise _format_validation_error(path, exc) from exc


def _expand_user_secret_map(
    values: Mapping[str, str] | None,
    *,
    resolve: bool,
    env: Mapping[str, str],
    source_path: Path,
    field_name: str,
    server_id: str,
) -> tuple[dict[str, str], str | None]:
    """A server's ``env`` or ``headers`` with ``${VAR}`` references filled in, and why not.

    A server that is turned off is never started, so its references stay as written. When a
    variable is missing, that server gets no values and the reason; the other servers, and the
    session, are not affected.
    """
    if not values:
        return {}, None
    if not resolve:
        return dict(values), None
    expanded: dict[str, str] = {}
    for key, raw_value in values.items():
        try:
            expanded[key] = expand_env_placeholders(
                raw_value,
                env=env,
                source_path=source_path,
                field_path=f"servers.{server_id}.{field_name}.{key}",
            )
        except ConfigError as exc:
            return {}, str(exc)
    return expanded, None


def _normalize_name_key(value: str) -> str:
    return value.casefold()


def _merge_enabled(
    *,
    user_value: bool,
    project_value: bool | None,
    project_path: Path,
    server_id: str,
) -> bool:
    if project_value is None:
        return user_value
    if not user_value and project_value:
        _raise_project_narrowing_error(
            project_path,
            server_id=server_id,
            field_name="enabled",
            reason="project config cannot re-enable a server disabled in user config.",
        )
    return bool(project_value)


def _merge_enabled_in(
    *,
    user_value: tuple[RuntimeKind, ...] | None,
    project_value: tuple[RuntimeKind, ...] | None,
    project_path: Path,
    server_id: str,
) -> tuple[RuntimeKind, ...] | None:
    if project_value is None:
        return user_value
    if user_value is None:
        return project_value
    user_set = set(user_value)
    if any(kind not in user_set for kind in project_value):
        _raise_project_narrowing_error(
            project_path,
            server_id=server_id,
            field_name="enabled_in",
            reason="project config cannot broaden enabled_in beyond user config.",
        )
    return project_value


def _merge_allowed_tools(
    *,
    user_value: tuple[str, ...],
    project_value: tuple[str, ...] | None,
    project_path: Path,
    server_id: str,
) -> tuple[str, ...]:
    if project_value is None:
        return user_value
    if not user_value:
        return project_value
    user_set = {_normalize_name_key(name) for name in user_value}
    if any(_normalize_name_key(name) not in user_set for name in project_value):
        _raise_project_narrowing_error(
            project_path,
            server_id=server_id,
            field_name="allowed_tools",
            reason="project config cannot broaden allowed_tools beyond user config.",
        )
    return project_value


def _stable_union_names(
    base: tuple[str, ...],
    extra: tuple[str, ...],
) -> tuple[str, ...]:
    merged = list(base)
    seen = {_normalize_name_key(name) for name in base}
    for name in extra:
        key = _normalize_name_key(name)
        if key in seen:
            continue
        seen.add(key)
        merged.append(name)
    return tuple(merged)


def _merge_denied_tools(
    *,
    user_value: tuple[str, ...],
    project_value: tuple[str, ...] | None,
) -> tuple[str, ...]:
    if project_value is None:
        return user_value
    return _stable_union_names(user_value, project_value)


def _merge_timeout(
    *,
    field_name: str,
    user_value: float,
    project_value: float | None,
    project_path: Path,
    server_id: str,
) -> float:
    if project_value is None:
        return float(user_value)
    if float(project_value) > float(user_value):
        _raise_project_narrowing_error(
            project_path,
            server_id=server_id,
            field_name=field_name,
            reason=f"project config cannot increase {field_name} beyond user config.",
        )
    return float(project_value)


def _merge_roots_mode(
    *,
    user_value: RootsMode,
    project_value: RootsMode | None,
    project_path: Path,
    server_id: str,
) -> RootsMode:
    if project_value is None:
        return user_value
    if user_value == "disabled" and project_value == "workspace":
        _raise_project_narrowing_error(
            project_path,
            server_id=server_id,
            field_name="roots_mode",
            reason="project config cannot broaden roots exposure beyond user config.",
        )
    return project_value


def _merge_resources_mode(
    *,
    user_value: ResourcesMode,
    project_value: ResourcesMode | None,
    project_path: Path,
    server_id: str,
) -> ResourcesMode:
    if project_value is None:
        return user_value
    if user_value == "disabled" and project_value == "listed_read_only":
        _raise_project_narrowing_error(
            project_path,
            server_id=server_id,
            field_name="resources_mode",
            reason="project config cannot broaden resources exposure beyond user config.",
        )
    return project_value


def _merge_prompts_mode(
    *,
    user_value: PromptsMode,
    project_value: PromptsMode | None,
    project_path: Path,
    server_id: str,
) -> PromptsMode:
    if project_value is None:
        return user_value
    if user_value == "disabled" and project_value == "listed_get_only":
        _raise_project_narrowing_error(
            project_path,
            server_id=server_id,
            field_name="prompts_mode",
            reason="project config cannot broaden prompts exposure beyond user config.",
        )
    return project_value


def _validate_merged_tool_policy_overlap(
    *,
    source_path: Path,
    server_id: str,
    allowed_tools: tuple[str, ...],
    denied_tools: tuple[str, ...],
) -> None:
    overlap = sorted(
        name
        for name in allowed_tools
        if _normalize_name_key(name) in {_normalize_name_key(denied) for denied in denied_tools}
    )
    if not overlap:
        return
    joined = ", ".join(overlap)
    _raise_project_narrowing_error(
        source_path,
        server_id=server_id,
        field_name="allowed_tools/denied_tools",
        reason=f"allowed_tools and denied_tools cannot overlap after merge: {joined}",
    )


def _merge_server(
    *,
    server_id: str,
    user_server: UserMcpStdioServer | UserMcpHttpServer,
    project_override: Any | None,
    env: Mapping[str, str],
    user_path: Path,
    project_path: Path,
) -> ResolvedMcpServer:
    enabled = _merge_enabled(
        user_value=user_server.enabled,
        project_value=getattr(project_override, "enabled", None),
        project_path=project_path,
        server_id=server_id,
    )
    enabled_in = _merge_enabled_in(
        user_value=tuple(user_server.enabled_in) if user_server.enabled_in is not None else None,
        project_value=(
            tuple(project_override.enabled_in)
            if getattr(project_override, "enabled_in", None) is not None
            else None
        ),
        project_path=project_path,
        server_id=server_id,
    )
    allowed_tools = _merge_allowed_tools(
        user_value=tuple(user_server.allowed_tools),
        project_value=(
            tuple(project_override.allowed_tools)
            if getattr(project_override, "allowed_tools", None) is not None
            else None
        ),
        project_path=project_path,
        server_id=server_id,
    )
    denied_tools = _merge_denied_tools(
        user_value=tuple(user_server.denied_tools),
        project_value=(
            tuple(project_override.denied_tools)
            if getattr(project_override, "denied_tools", None) is not None
            else None
        ),
    )
    overlap_source_path = user_path if project_override is None else project_path
    _validate_merged_tool_policy_overlap(
        source_path=overlap_source_path,
        server_id=server_id,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
    )
    startup_timeout_s = _merge_timeout(
        field_name="startup_timeout_s",
        user_value=float(user_server.startup_timeout_s),
        project_value=getattr(project_override, "startup_timeout_s", None),
        project_path=project_path,
        server_id=server_id,
    )
    call_timeout_s = _merge_timeout(
        field_name="call_timeout_s",
        user_value=float(user_server.call_timeout_s),
        project_value=getattr(project_override, "call_timeout_s", None),
        project_path=project_path,
        server_id=server_id,
    )
    roots_mode = _merge_roots_mode(
        user_value=user_server.roots_mode,
        project_value=getattr(project_override, "roots_mode", None),
        project_path=project_path,
        server_id=server_id,
    )
    resources_mode = _merge_resources_mode(
        user_value=user_server.resources_mode,
        project_value=getattr(project_override, "resources_mode", None),
        project_path=project_path,
        server_id=server_id,
    )
    prompts_mode = _merge_prompts_mode(
        user_value=user_server.prompts_mode,
        project_value=getattr(project_override, "prompts_mode", None),
        project_path=project_path,
        server_id=server_id,
    )

    if isinstance(user_server, UserMcpStdioServer):
        env_values, settings_error = _expand_user_secret_map(
            user_server.env,
            resolve=enabled,
            env=env,
            source_path=user_path,
            field_name="env",
            server_id=server_id,
        )
        return ResolvedMcpServer(
            id=server_id,
            transport="stdio",
            enabled=enabled,
            enabled_in=enabled_in,
            trust=user_server.trust,
            allowed_tools=allowed_tools,
            denied_tools=denied_tools,
            startup_timeout_s=startup_timeout_s,
            call_timeout_s=call_timeout_s,
            tool_prefix=user_server.tool_prefix,
            roots_mode=roots_mode,
            resources_mode=resources_mode,
            prompts_mode=prompts_mode,
            command=user_server.command,
            args=tuple(user_server.args),
            env=env_values,
            settings_error=settings_error,
        )
    oauth = None
    if user_server.oauth is not None:
        oauth = ResolvedMcpHttpOAuthConfig(
            client_id=user_server.oauth.client_id,
            redirect_host=user_server.oauth.redirect_host,
            redirect_port=user_server.oauth.redirect_port,
            scopes=tuple(user_server.oauth.scopes or []),
            authorization_server_url=user_server.oauth.authorization_server_url,
        )
    headers, settings_error = _expand_user_secret_map(
        user_server.headers,
        resolve=enabled,
        env=env,
        source_path=user_path,
        field_name="headers",
        server_id=server_id,
    )
    return ResolvedMcpServer(
        id=server_id,
        transport="http",
        enabled=enabled,
        enabled_in=enabled_in,
        trust=user_server.trust,
        allowed_tools=allowed_tools,
        denied_tools=denied_tools,
        startup_timeout_s=startup_timeout_s,
        call_timeout_s=call_timeout_s,
        tool_prefix=user_server.tool_prefix,
        roots_mode=roots_mode,
        resources_mode=resources_mode,
        prompts_mode=prompts_mode,
        url=user_server.url,
        headers=headers,
        oauth=oauth,
        settings_error=settings_error,
    )


def load_resolved_mcp_config(
    *,
    workspace_root: Path,
    env: Mapping[str, str] | None = None,
) -> ResolvedMcpConfig:
    resolved_workspace_root = workspace_root.resolve()
    user_path = user_mcp_config_path()
    project_path = project_mcp_config_path(resolved_workspace_root)
    effective_env = os.environ if env is None else env

    user_config = UserMcpConfigFile()
    project_config = ProjectMcpConfigFile()
    user_present = user_path.exists()
    project_present = project_path.exists()

    if user_present:
        user_config = _load_user_mcp_config(user_path)
    if project_present:
        project_config = _load_project_mcp_config(project_path)

    if not user_present and not project_present:
        return ResolvedMcpConfig(
            workspace_root=resolved_workspace_root,
            user_config_path=user_path,
            project_config_path=project_path,
            user_config_present=False,
            project_config_present=False,
            servers=(),
        )

    user_servers = dict(user_config.servers)
    project_servers = dict(project_config.servers)

    for server_id in project_servers:
        if server_id not in user_servers:
            raise ConfigError(
                f"Invalid MCP config: {project_path}: unknown server id '{server_id}'. "
                "Project MCP config can only override user-defined servers."
            )

    resolved_servers = tuple(
        _merge_server(
            server_id=server_id,
            user_server=user_server,
            project_override=project_servers.get(server_id),
            env=effective_env,
            user_path=user_path,
            project_path=project_path,
        )
        for server_id, user_server in user_servers.items()
    )

    return ResolvedMcpConfig(
        workspace_root=resolved_workspace_root,
        user_config_path=user_path,
        project_config_path=project_path,
        user_config_present=user_present,
        project_config_present=project_present,
        servers=resolved_servers,
    )
