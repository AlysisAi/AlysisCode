from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..branding import canonical_user_config_dir, env_get, env_get_from
from ..runtime_kind import RETIRED_RUNTIME_KINDS, RuntimeKind, runtime_kind_values

_DEFAULT_TIMEOUT_S = 15.0
_MAX_TIMEOUT_S = 300.0
_MAX_MANIFEST_BYTES = 32_000
_MAX_SCHEMA_BYTES = 24_000
_MAX_DESCRIPTION_CHARS = 600
_MAX_REQUIRED_ENV_VARS = 32
_MAX_SECRET_REFS = 32
_MAX_NETWORK_HOSTS = 64
_RESERVED_NAME_PREFIXES = ("mcp__",)
_RESERVED_EXACT_NAMES = frozenset({"mcp_resources_list", "mcp_resource_read"})
_VALID_ISOLATION_VALUES = {"subprocess"}
_SUPPORTED_MANIFEST_VERSION = 1
_VALID_NETWORK_ACCESS_VALUES = {
    "unspecified",
    "none",
    "local",
    "restricted",
    "unrestricted",
}
_VALID_FILESYSTEM_SCOPE_VALUES = {
    "unspecified",
    "none",
    "tool_dir",
    "workspace",
    "unrestricted",
}
_VALID_PROCESS_SPAWN_VALUES = {
    "unspecified",
    "none",
    "unrestricted",
}
_DEFAULT_ENABLED_IN = (
    RuntimeKind.INTERACTIVE_CHAT.value,
    RuntimeKind.ONE_SHOT.value,
)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class CustomToolCapabilities:
    read_only: bool = False
    destructive: bool = False
    network_access: str = "unspecified"
    network_hosts: tuple[str, ...] = ()
    filesystem_read_scope: str = "unspecified"
    filesystem_write_scope: str = "unspecified"
    process_spawn: str = "unspecified"
    secret_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "read_only": self.read_only,
            "destructive": self.destructive,
            "network_access": self.network_access,
            "network_hosts": list(self.network_hosts),
            "filesystem_read_scope": self.filesystem_read_scope,
            "filesystem_write_scope": self.filesystem_write_scope,
            "process_spawn": self.process_spawn,
            "secret_refs": list(self.secret_refs),
        }


@dataclass(frozen=True)
class CustomToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    manifest_version: int
    capabilities: CustomToolCapabilities
    output_schema: dict[str, Any] | None
    timeout_s: float
    required_env: tuple[str, ...]
    enabled_in: tuple[str, ...]
    isolation: str
    source_scope: str
    source_path: Path
    relative_tool_path: str
    file_hash: str
    missing_env: tuple[str, ...]

    @property
    def normalized_name(self) -> str:
        return self.name.casefold()

    def metadata(self, *, include_output_schema: bool = False) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "manifest_version": self.manifest_version,
            "source_scope": self.source_scope,
            "relative_tool_path": self.relative_tool_path,
            "file_hash": self.file_hash,
            "capabilities": self.capabilities.to_dict(),
            "has_output_schema": self.output_schema is not None,
        }
        if include_output_schema and self.output_schema is not None:
            payload["output_schema"] = copy.deepcopy(self.output_schema)
        return payload


@dataclass(frozen=True)
class CustomToolIssue:
    source_scope: str
    source_path: Path
    relative_tool_path: str
    code: str
    message: str
    tool_name: str = ""


@dataclass(frozen=True)
class CustomToolDiscoveryResult:
    global_tools: tuple[CustomToolSpec, ...]
    project_tools: tuple[CustomToolSpec, ...]
    effective_tools: tuple[CustomToolSpec, ...]
    shadowed_tools: tuple[CustomToolSpec, ...]
    issues: tuple[CustomToolIssue, ...]

    def effective_tools_by_name(self) -> dict[str, CustomToolSpec]:
        return {tool.normalized_name: tool for tool in self.effective_tools}


def _config_dir() -> Path:
    override = env_get("ALYSIS_CONFIG_DIR")
    if override:
        return Path(override)
    return canonical_user_config_dir()


def project_custom_tools_root(workspace_root: Path) -> Path:
    return workspace_root.resolve() / ".alysis" / "tools"


def global_custom_tools_root(*, user_config_dir: Path | None = None) -> Path:
    base = _resolved_config_dir(user_config_dir=user_config_dir)
    return base / "tools"


def _resolved_config_dir(*, user_config_dir: Path | None = None) -> Path:
    base = user_config_dir.expanduser() if user_config_dir is not None else _config_dir()
    return base.resolve()


def discover_custom_tools(
    *,
    workspace_root: Path,
    built_in_tool_names: set[str] | None = None,
    user_config_dir: Path | None = None,
    env: dict[str, str] | None = None,
) -> CustomToolDiscoveryResult:
    reserved_names = {
        str(name or "").strip().casefold()
        for name in (built_in_tool_names or set())
        if str(name or "").strip()
    }
    effective_env = dict(os.environ if env is None else env)
    resolved_workspace_root = workspace_root.resolve()
    resolved_config_dir = _resolved_config_dir(user_config_dir=user_config_dir)
    global_root = global_custom_tools_root(user_config_dir=user_config_dir)
    project_root = project_custom_tools_root(workspace_root)

    global_tools, global_issues = _discover_scope(
        root=global_root,
        source_scope="global",
        owning_base=resolved_config_dir,
        reserved_names=reserved_names,
        env=effective_env,
    )
    project_tools, project_issues = _discover_scope(
        root=project_root,
        source_scope="project",
        owning_base=resolved_workspace_root,
        reserved_names=reserved_names,
        env=effective_env,
    )

    effective: dict[str, CustomToolSpec] = {tool.normalized_name: tool for tool in global_tools}
    shadowed: list[CustomToolSpec] = []
    for tool in project_tools:
        previous = effective.get(tool.normalized_name)
        if previous is not None and previous.source_scope == "global":
            shadowed.append(previous)
        effective[tool.normalized_name] = tool

    return CustomToolDiscoveryResult(
        global_tools=tuple(global_tools),
        project_tools=tuple(project_tools),
        effective_tools=tuple(
            sorted(effective.values(), key=lambda tool: (tool.name.casefold(), tool.source_scope))
        ),
        shadowed_tools=tuple(sorted(shadowed, key=lambda tool: tool.name.casefold())),
        issues=tuple(
            sorted(
                [*global_issues, *project_issues],
                key=lambda issue: (
                    issue.tool_name.casefold() if issue.tool_name else "~",
                    issue.source_scope,
                    issue.relative_tool_path.casefold(),
                ),
            )
        ),
    )


def _discover_scope(
    *,
    root: Path,
    source_scope: str,
    owning_base: Path,
    reserved_names: set[str],
    env: dict[str, str],
) -> tuple[list[CustomToolSpec], list[CustomToolIssue]]:
    if not _resolved_path_is_within_scope(root, owning_base):
        return [], [
            _tool_issue(
                source_scope=source_scope,
                source_path=root,
                relative_tool_path=_issue_relative_tool_path(
                    root,
                    owning_base=owning_base,
                    source_scope=source_scope,
                ),
                code="path_escape",
                message=_path_escape_message(
                    resolved_path=root.resolve(),
                    owning_base=owning_base,
                    source_scope=source_scope,
                    kind="custom tool root",
                ),
            )
        ]
    if not root.exists() or not root.is_dir():
        return [], []
    try:
        candidates = sorted(child for child in root.rglob("*.py") if child.is_file())
    except OSError as exc:
        return [], [
            CustomToolIssue(
                source_scope=source_scope,
                source_path=root,
                relative_tool_path=_issue_relative_tool_path(
                    root,
                    owning_base=owning_base,
                    source_scope=source_scope,
                ),
                code="scan_failed",
                message=f"failed to scan tool root: {exc}",
            )
        ]

    valid_specs: list[CustomToolSpec] = []
    issues: list[CustomToolIssue] = []
    seen_valid_names: dict[str, CustomToolSpec] = {}
    for path in candidates:
        if path.suffix != ".py":
            continue
        spec, issue = _load_tool_spec(
            path=path,
            source_scope=source_scope,
            root=root,
            owning_base=owning_base,
            reserved_names=reserved_names,
            env=env,
        )
        if issue is not None:
            issues.append(issue)
            continue
        assert spec is not None
        duplicate = seen_valid_names.get(spec.normalized_name)
        if duplicate is not None:
            issues.append(
                CustomToolIssue(
                    source_scope=source_scope,
                    source_path=spec.source_path,
                    relative_tool_path=spec.relative_tool_path,
                    code="duplicate_name",
                    message=(
                        f"duplicate custom tool name '{spec.name}' in {source_scope} scope; "
                        f"already provided by {duplicate.relative_tool_path}"
                    ),
                    tool_name=spec.name,
                )
            )
            continue
        seen_valid_names[spec.normalized_name] = spec
        valid_specs.append(spec)
    return valid_specs, issues


def _load_tool_spec(
    *,
    path: Path,
    source_scope: str,
    root: Path,
    owning_base: Path,
    reserved_names: set[str],
    env: dict[str, str],
) -> tuple[CustomToolSpec | None, CustomToolIssue | None]:
    relative_tool_path = _issue_relative_tool_path(
        path,
        owning_base=owning_base,
        source_scope=source_scope,
    )
    if not _resolved_path_is_within_scope(path, owning_base):
        return None, _tool_issue(
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=relative_tool_path,
            code="path_escape",
            message=_path_escape_message(
                resolved_path=path.resolve(),
                owning_base=owning_base,
                source_scope=source_scope,
                kind="custom tool source path",
            ),
        )
    if path.is_symlink():
        return None, _tool_issue(
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=relative_tool_path,
            code="symlink_rejected",
            message="symlinked tool files are not supported",
        )
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        return None, _tool_issue(
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=relative_tool_path,
            code="read_failed",
            message=f"failed to read tool file: {exc}",
        )
    try:
        source_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        return None, _tool_issue(
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=relative_tool_path,
            code="invalid_utf8",
            message="tool file must be valid UTF-8",
        )
    if len(raw_bytes) > _MAX_MANIFEST_BYTES * 4:
        return None, _tool_issue(
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=relative_tool_path,
            code="file_too_large",
            message="tool file is too large",
        )
    try:
        tree = ast.parse(source_text, filename=os.fspath(path))
    except SyntaxError as exc:
        return None, _tool_issue(
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=relative_tool_path,
            code="syntax_error",
            message=f"syntax error: {exc.msg}",
        )
    manifest_value, manifest_error = _extract_tool_manifest(tree)
    if manifest_error is not None:
        return None, _tool_issue(
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=relative_tool_path,
            code=manifest_error[0],
            message=manifest_error[1],
        )
    assert manifest_value is not None
    if not _has_valid_run_function(tree):
        tool_name = str(manifest_value.get("name") or "")
        return None, _tool_issue(
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=relative_tool_path,
            code="missing_run",
            message="tool file must define top-level def run(args)",
            tool_name=tool_name,
        )
    try:
        spec = _build_tool_spec(
            manifest=manifest_value,
            raw_bytes=raw_bytes,
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=_spec_relative_tool_path(
                path,
                root=root,
                owning_base=owning_base,
                source_scope=source_scope,
            ),
            reserved_names=reserved_names,
            env=env,
        )
    except ValueError as exc:
        tool_name = ""
        raw_name = manifest_value.get("name")
        if isinstance(raw_name, str):
            tool_name = raw_name
        return None, _tool_issue(
            source_scope=source_scope,
            source_path=path,
            relative_tool_path=relative_tool_path,
            code="invalid_manifest",
            message=str(exc),
            tool_name=tool_name,
        )
    return spec, None


def _extract_tool_manifest(
    tree: ast.Module,
) -> tuple[dict[str, Any] | None, tuple[str, str] | None]:
    manifest_nodes: list[ast.AST] = []
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name) and target.id == "TOOL":
                    manifest_nodes.append(statement.value)
        elif isinstance(statement, ast.AnnAssign):
            if isinstance(statement.target, ast.Name) and statement.target.id == "TOOL":
                manifest_nodes.append(statement.value)
    if not manifest_nodes:
        return None, ("missing_manifest", "tool file must define top-level TOOL = {...}")
    if len(manifest_nodes) > 1:
        return None, ("duplicate_manifest", "tool file defines TOOL more than once")
    try:
        manifest_value = ast.literal_eval(manifest_nodes[0])
    except (SyntaxError, ValueError) as exc:
        return None, ("invalid_manifest", f"TOOL must be a top-level literal dictionary: {exc}")
    if not isinstance(manifest_value, dict):
        return None, ("invalid_manifest", "TOOL must be a top-level literal dictionary")
    try:
        manifest_bytes = _stable_json_size(manifest_value)
    except TypeError as exc:
        return None, ("invalid_manifest", f"TOOL must be JSON-serializable: {exc}")
    if manifest_bytes > _MAX_MANIFEST_BYTES:
        return None, ("invalid_manifest", "TOOL manifest is too large")
    return copy.deepcopy(manifest_value), None


def _has_valid_run_function(tree: ast.Module) -> bool:
    for statement in tree.body:
        if not isinstance(statement, ast.FunctionDef):
            continue
        if statement.name != "run":
            continue
        args = statement.args
        positional = [*args.posonlyargs, *args.args]
        if len(positional) != 1:
            return False
        if args.vararg is not None or args.kwarg is not None or args.kwonlyargs:
            return False
        return True
    return False


def _build_tool_spec(
    *,
    manifest: dict[str, Any],
    raw_bytes: bytes,
    source_scope: str,
    source_path: Path,
    relative_tool_path: str,
    reserved_names: set[str],
    env: dict[str, str],
) -> CustomToolSpec:
    required_keys = {"name", "description", "input_schema"}
    allowed_keys = {
        "manifest_version",
        "name",
        "description",
        "input_schema",
        "output_schema",
        "capabilities",
        "timeout_s",
        "required_env",
        "enabled_in",
        "isolation",
    }
    missing_keys = sorted(required_keys - set(manifest))
    if missing_keys:
        raise ValueError(f"missing required TOOL keys: {', '.join(missing_keys)}")
    unknown_keys = sorted(set(manifest) - allowed_keys)
    if unknown_keys:
        raise ValueError(f"unsupported TOOL keys: {', '.join(unknown_keys)}")

    name = str(manifest.get("name") or "").strip()
    if not name:
        raise ValueError("TOOL.name must be a non-empty string")
    normalized_name = name.casefold()
    if normalized_name in _RESERVED_EXACT_NAMES:
        raise ValueError(f"TOOL.name collides with reserved host tool name: {name}")
    if normalized_name in reserved_names:
        raise ValueError(f"TOOL.name collides with built-in tool name: {name}")
    for prefix in _RESERVED_NAME_PREFIXES:
        if normalized_name.startswith(prefix):
            raise ValueError(f"TOOL.name uses reserved prefix '{prefix}': {name}")

    description = str(manifest.get("description") or "").strip()
    if not description:
        raise ValueError("TOOL.description must be a non-empty string")
    if len(description) > _MAX_DESCRIPTION_CHARS:
        raise ValueError("TOOL.description is too long")

    manifest_version = _validate_manifest_version(manifest.get("manifest_version", 1))
    input_schema = _validate_input_schema(manifest.get("input_schema"))
    output_schema = _validate_output_schema(manifest.get("output_schema"))
    timeout_s = _coerce_timeout(manifest.get("timeout_s", _DEFAULT_TIMEOUT_S))
    required_env = _validate_required_env(manifest.get("required_env", ()))
    capabilities = _validate_capabilities(
        manifest.get("capabilities", {}),
        required_env=required_env,
    )
    enabled_in = _validate_enabled_in(manifest.get("enabled_in", _DEFAULT_ENABLED_IN))
    isolation = _validate_isolation(manifest.get("isolation", "subprocess"))
    file_hash = hashlib.sha256(raw_bytes).hexdigest()
    missing_env = tuple(
        name for name in required_env if not str(env_get_from(env, name) or "").strip()
    )

    return CustomToolSpec(
        name=name,
        description=description,
        input_schema=input_schema,
        manifest_version=manifest_version,
        capabilities=capabilities,
        output_schema=output_schema,
        timeout_s=timeout_s,
        required_env=required_env,
        enabled_in=enabled_in,
        isolation=isolation,
        source_scope=source_scope,
        source_path=source_path.resolve(),
        relative_tool_path=relative_tool_path,
        file_hash=file_hash,
        missing_env=missing_env,
    )


def _validate_manifest_version(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("TOOL.manifest_version must be an integer")
    if value != _SUPPORTED_MANIFEST_VERSION:
        raise ValueError(f"unsupported TOOL.manifest_version: {value}")
    return value


def _validate_input_schema(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("TOOL.input_schema must be a JSON object")
    if value.get("type") != "object":
        raise ValueError("TOOL.input_schema root type must be 'object'")
    properties = value.get("properties", {})
    if not isinstance(properties, dict):
        raise ValueError("TOOL.input_schema.properties must be an object")
    required = value.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ValueError("TOOL.input_schema.required must be an array of strings")
    try:
        schema_bytes = _stable_json_size(value)
    except TypeError as exc:
        raise ValueError(f"TOOL.input_schema must be JSON-serializable: {exc}") from exc
    if schema_bytes > _MAX_SCHEMA_BYTES:
        raise ValueError("TOOL.input_schema is too large")
    return copy.deepcopy(value)


def _validate_output_schema(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("TOOL.output_schema must be a JSON object")
    if value.get("type") != "object":
        raise ValueError("TOOL.output_schema root type must be 'object'")
    try:
        schema_bytes = _stable_json_size(value)
    except TypeError as exc:
        raise ValueError(f"TOOL.output_schema must be JSON-serializable: {exc}") from exc
    if schema_bytes > _MAX_SCHEMA_BYTES:
        raise ValueError("TOOL.output_schema is too large")
    return copy.deepcopy(value)


def _validate_capabilities(
    value: Any,
    *,
    required_env: tuple[str, ...],
) -> CustomToolCapabilities:
    if value in (None, ()):
        value = {}
    if not isinstance(value, dict):
        raise ValueError("TOOL.capabilities must be a JSON object")
    allowed_keys = {
        "read_only",
        "destructive",
        "network_access",
        "network_hosts",
        "network",
        "filesystem",
        "filesystem_read_scope",
        "filesystem_write_scope",
        "process_spawn",
        "secret_refs",
    }
    unknown_keys = sorted(set(value) - allowed_keys)
    if unknown_keys:
        raise ValueError(f"unsupported TOOL.capabilities keys: {', '.join(unknown_keys)}")

    filesystem = value.get("filesystem", {})
    if filesystem in (None, ()):
        filesystem = {}
    if not isinstance(filesystem, dict):
        raise ValueError("TOOL.capabilities.filesystem must be a JSON object")
    filesystem_allowed = {"read", "write", "read_scope", "write_scope"}
    filesystem_unknown = sorted(set(filesystem) - filesystem_allowed)
    if filesystem_unknown:
        raise ValueError(
            f"unsupported TOOL.capabilities.filesystem keys: {', '.join(filesystem_unknown)}"
        )

    read_only = _coerce_bool(value.get("read_only", False), "TOOL.capabilities.read_only")
    destructive = _coerce_bool(value.get("destructive", False), "TOOL.capabilities.destructive")
    if read_only and destructive:
        raise ValueError("TOOL.capabilities cannot be both read_only and destructive")

    network_access = _validate_enum_value(
        value.get("network_access", value.get("network", "unspecified")),
        allowed=_VALID_NETWORK_ACCESS_VALUES,
        label="TOOL.capabilities.network_access",
    )
    network_hosts = _validate_network_hosts(value.get("network_hosts", ()))
    if network_access == "restricted" and not network_hosts:
        raise ValueError(
            "TOOL.capabilities.network_hosts is required when network_access is restricted"
        )
    if network_access != "restricted" and network_hosts:
        raise ValueError(
            "TOOL.capabilities.network_hosts is only valid with restricted network access"
        )
    filesystem_read_scope = _validate_enum_value(
        value.get(
            "filesystem_read_scope",
            filesystem.get("read", filesystem.get("read_scope", "unspecified")),
        ),
        allowed=_VALID_FILESYSTEM_SCOPE_VALUES,
        label="TOOL.capabilities.filesystem_read_scope",
    )
    filesystem_write_scope = _validate_enum_value(
        value.get(
            "filesystem_write_scope",
            filesystem.get("write", filesystem.get("write_scope", "unspecified")),
        ),
        allowed=_VALID_FILESYSTEM_SCOPE_VALUES,
        label="TOOL.capabilities.filesystem_write_scope",
    )
    if read_only and filesystem_write_scope not in {"none", "unspecified"}:
        raise ValueError(
            "TOOL.capabilities.read_only requires filesystem_write_scope to be none or unspecified"
        )

    process_spawn = _validate_enum_value(
        value.get("process_spawn", "unspecified"),
        allowed=_VALID_PROCESS_SPAWN_VALUES,
        label="TOOL.capabilities.process_spawn",
    )
    secret_refs = _validate_secret_refs(value.get("secret_refs", ()))
    if required_env:
        secret_refs = _dedupe_strings([*secret_refs, *required_env])

    return CustomToolCapabilities(
        read_only=read_only,
        destructive=destructive,
        network_access=network_access,
        network_hosts=network_hosts,
        filesystem_read_scope=filesystem_read_scope,
        filesystem_write_scope=filesystem_write_scope,
        process_spawn=process_spawn,
        secret_refs=secret_refs,
    )


def _coerce_bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} must be true or false")
    return value


def _validate_enum_value(value: Any, *, allowed: set[str], label: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized not in allowed:
        raise ValueError(f"{label} must be one of: {', '.join(sorted(allowed))}")
    return normalized


def _validate_secret_refs(value: Any) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, list):
        raise ValueError("TOOL.capabilities.secret_refs must be an array of env var names")
    names: list[str] = []
    for item in value:
        name = str(item or "").strip()
        if not name:
            raise ValueError("TOOL.capabilities.secret_refs cannot contain empty values")
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"TOOL.capabilities.secret_refs contains invalid env var name: {name}")
        names.append(name)
    if len(names) > _MAX_SECRET_REFS:
        raise ValueError("TOOL.capabilities.secret_refs contains too many entries")
    return _dedupe_strings(names)


def _validate_network_hosts(value: Any) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, list):
        raise ValueError("TOOL.capabilities.network_hosts must be an array of host strings")
    hosts: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise ValueError("TOOL.capabilities.network_hosts must contain only strings")
        host = item.strip()
        if not host:
            raise ValueError("TOOL.capabilities.network_hosts cannot contain empty values")
        if "*" in host:
            raise ValueError("TOOL.capabilities.network_hosts does not support wildcards")
        if any(char.isspace() for char in host) or "/" in host:
            raise ValueError(
                "TOOL.capabilities.network_hosts entries must be exact host or IP strings"
            )
        hosts.append(host)
    if len(hosts) > _MAX_NETWORK_HOSTS:
        raise ValueError("TOOL.capabilities.network_hosts contains too many entries")
    return _dedupe_strings(hosts)


def _dedupe_strings(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


def _validate_required_env(value: Any) -> tuple[str, ...]:
    if value in (None, ()):
        return ()
    if not isinstance(value, list):
        raise ValueError("TOOL.required_env must be an array of env var names")
    names: list[str] = []
    seen: set[str] = set()
    for item in value:
        name = str(item or "").strip()
        if not name:
            raise ValueError("TOOL.required_env cannot contain empty values")
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"TOOL.required_env contains invalid env var name: {name}")
        if name in seen:
            continue
        seen.add(name)
        names.append(name)
    if len(names) > _MAX_REQUIRED_ENV_VARS:
        raise ValueError("TOOL.required_env contains too many entries")
    return tuple(names)


def _validate_enabled_in(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list) and not isinstance(value, tuple):
        raise ValueError("TOOL.enabled_in must be an array of runtime kinds")
    allowed = set(runtime_kind_values())
    items: list[str] = []
    seen: set[str] = set()
    retired_only = bool(value)
    for item in value:
        runtime = str(item or "").strip().lower()
        if runtime in RETIRED_RUNTIME_KINDS:
            continue
        retired_only = False
        if runtime not in allowed:
            raise ValueError(
                "TOOL.enabled_in contains invalid runtime kind: "
                f"{item!r}. Expected one of: {', '.join(runtime_kind_values())}"
            )
        if runtime in seen:
            continue
        seen.add(runtime)
        items.append(runtime)
    if not items:
        if retired_only:
            raise ValueError(
                "TOOL.enabled_in names only runtimes that no longer exist. Expected one of: "
                f"{', '.join(runtime_kind_values())}"
            )
        raise ValueError("TOOL.enabled_in cannot be empty")
    return tuple(items)


def _validate_isolation(value: Any) -> str:
    isolation = str(value or "").strip().lower()
    if isolation not in _VALID_ISOLATION_VALUES:
        raise ValueError("TOOL.isolation must be subprocess; inprocess is not supported")
    return isolation


def _coerce_timeout(value: Any) -> float:
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("TOOL.timeout_s must be a positive number") from exc
    if timeout <= 0:
        raise ValueError("TOOL.timeout_s must be positive")
    if timeout > _MAX_TIMEOUT_S:
        raise ValueError(f"TOOL.timeout_s must be <= {_MAX_TIMEOUT_S}")
    return timeout


def _spec_relative_tool_path(
    path: Path,
    root: Path,
    owning_base: Path,
    source_scope: str,
) -> str:
    if source_scope == "project":
        return _issue_relative_tool_path(path, owning_base=owning_base, source_scope=source_scope)
    return path.resolve().relative_to(root.resolve()).as_posix()


def _issue_relative_tool_path(
    path: Path,
    *,
    owning_base: Path,
    source_scope: str,
) -> str:
    try:
        return path.relative_to(owning_base).as_posix()
    except ValueError:
        scope_root = _scope_root_label(source_scope)
        name = path.name.strip()
        if not name or name == Path(scope_root).name:
            return scope_root
        return f"{scope_root}/{name}"


def _scope_root_label(source_scope: str) -> str:
    return ".alysis/tools" if source_scope == "project" else "tools"


def _resolved_path_is_within_scope(path: Path, owning_base: Path) -> bool:
    try:
        path.resolve().relative_to(owning_base)
    except ValueError:
        return False
    return True


def _path_escape_message(
    *,
    resolved_path: Path,
    owning_base: Path,
    source_scope: str,
    kind: str,
) -> str:
    scope_label = "workspace-owned" if source_scope == "project" else "config-owned"
    return (
        f"resolved {kind} is outside the {scope_label} custom tools scope: "
        f"{resolved_path} (expected under {owning_base})"
    )


def _tool_issue(
    *,
    source_scope: str,
    source_path: Path,
    relative_tool_path: str,
    code: str,
    message: str,
    tool_name: str = "",
) -> CustomToolIssue:
    return CustomToolIssue(
        source_scope=source_scope,
        source_path=source_path.resolve(),
        relative_tool_path=relative_tool_path,
        code=code,
        message=message,
        tool_name=tool_name,
    )


def _stable_json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))
