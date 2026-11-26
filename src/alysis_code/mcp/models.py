from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Literal, cast
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from ..config import ConfigError as AppConfigError
from ..runtime_kind import RETIRED_RUNTIME_KINDS, RuntimeKind, normalize_runtime_kind
from .errors import McpConfigError as ConfigError

TransportKind = Literal["stdio", "http"]
RootsMode = Literal["disabled", "workspace"]
ResourcesMode = Literal["disabled", "listed_read_only"]
PromptsMode = Literal["disabled", "listed_get_only"]

_SERVER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)?$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_TOOL_PREFIX_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def normalize_server_id(raw_value: object) -> str:
    value = str(raw_value).strip().lower()
    if not value:
        raise ConfigError("MCP server id cannot be empty.")
    if not _SERVER_ID_RE.fullmatch(value):
        raise ConfigError(
            f"Invalid MCP server id: {raw_value!r}. Expected lowercase [a-z0-9._-] "
            "with at most one plugin scope slash."
        )
    return value


def normalize_ordered_name_list(
    value: object,
    *,
    field_name: str,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list of strings.")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field_name} items must be strings.")
        cleaned = item.strip()
        if not cleaned:
            continue
        lowered = cleaned.lower()
        if lowered in seen:
            continue
        seen.add(lowered)
        normalized.append(cleaned)
    return normalized


def normalize_ordered_args_list(value: object, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list of strings.")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field_name} items must be strings.")
        cleaned = item.strip()
        if cleaned:
            normalized.append(cleaned)
    return normalized


def normalize_ordered_string_list(
    value: object,
    *,
    field_name: str,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TypeError(f"{field_name} must be a list of strings.")
    normalized: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise TypeError(f"{field_name} items must be strings.")
        cleaned = item.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        normalized.append(cleaned)
    return normalized


def normalize_optional_string(
    value: object,
    *,
    field_name: str,
    lowercase: bool = False,
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string.")
    cleaned = value.strip()
    if not cleaned:
        return None
    if lowercase:
        cleaned = cleaned.lower()
    return cleaned


def normalize_timeout(value: object, *, field_name: str) -> float:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a positive number.")
    if not isinstance(value, (int, float)):
        raise TypeError(f"{field_name} must be a positive number.")
    normalized = float(value)
    if normalized <= 0:
        raise ValueError(f"{field_name} must be > 0.")
    return normalized


def normalize_roots_mode(value: object, *, field_name: str) -> RootsMode:
    normalized = normalize_optional_string(value, field_name=field_name, lowercase=True)
    if normalized is None:
        raise ValueError(f"{field_name} cannot be empty.")
    if normalized not in {"disabled", "workspace"}:
        raise ValueError(f"{field_name} must be 'disabled' or 'workspace'.")
    return cast(RootsMode, normalized)


def normalize_resources_mode(value: object, *, field_name: str) -> ResourcesMode:
    normalized = normalize_optional_string(value, field_name=field_name, lowercase=True)
    if normalized is None:
        raise ValueError(f"{field_name} cannot be empty.")
    if normalized not in {"disabled", "listed_read_only"}:
        raise ValueError(f"{field_name} must be 'disabled' or 'listed_read_only'.")
    return cast(ResourcesMode, normalized)


def normalize_prompts_mode(value: object, *, field_name: str) -> PromptsMode:
    normalized = normalize_optional_string(value, field_name=field_name, lowercase=True)
    if normalized is None:
        raise ValueError(f"{field_name} cannot be empty.")
    if normalized not in {"disabled", "listed_get_only"}:
        raise ValueError(f"{field_name} must be 'disabled' or 'listed_get_only'.")
    return cast(PromptsMode, normalized)


def normalize_runtime_kind_list(value: object) -> list[RuntimeKind] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise TypeError("enabled_in must be a list of runtime kinds.")
    normalized: list[RuntimeKind] = []
    seen: set[RuntimeKind] = set()
    for item in value:
        if isinstance(item, str) and item.strip().lower() in RETIRED_RUNTIME_KINDS:
            continue
        try:
            kind = normalize_runtime_kind(item)
        except AppConfigError as exc:
            raise ValueError(str(exc)) from exc
        if kind in seen:
            continue
        seen.add(kind)
        normalized.append(kind)
    return normalized


def normalize_string_mapping(
    value: object,
    *,
    field_name: str,
    key_validator: Callable[[str], None] | None = None,
) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be an object mapping strings to strings.")
    normalized: dict[str, str] = {}
    for raw_key, raw_item in value.items():
        if not isinstance(raw_key, str):
            raise TypeError(f"{field_name} keys must be strings.")
        if not isinstance(raw_item, str):
            raise TypeError(f"{field_name} values must be strings.")
        key = raw_key.strip()
        if not key:
            raise ValueError(f"{field_name} keys cannot be empty.")
        if key_validator is not None:
            key_validator(key)
        normalized[key] = raw_item.strip()
    return normalized


def validate_http_url(value: str, *, field_name: str) -> str:
    split = urlsplit(value)
    if split.scheme not in {"http", "https"} or not split.netloc:
        raise ValueError(f"{field_name} must be an http:// or https:// URL.")
    return value


def _is_loopback_host(hostname: str | None) -> bool:
    cleaned = str(hostname or "").strip()
    if not cleaned:
        return False
    if cleaned.lower() == "localhost":
        return True
    try:
        return ip_address(cleaned).is_loopback
    except ValueError:
        return False


def validate_https_url(
    value: str,
    *,
    field_name: str,
    allow_loopback_http: bool = False,
) -> str:
    split = urlsplit(value)
    if not split.netloc:
        raise ValueError(f"{field_name} must be an absolute URL.")
    if split.scheme == "https":
        return value
    if split.scheme == "http" and allow_loopback_http and _is_loopback_host(split.hostname):
        return value
    if allow_loopback_http:
        raise ValueError(
            f"{field_name} must be an https:// URL (http:// is only allowed for loopback hosts)."
        )
    raise ValueError(f"{field_name} must be an https:// URL.")


def validate_tcp_port(value: object, *, field_name: str) -> int:
    if isinstance(value, bool):
        raise TypeError(f"{field_name} must be a valid TCP port.")
    if not isinstance(value, int):
        raise TypeError(f"{field_name} must be a valid TCP port.")
    if not 1 <= int(value) <= 65535:
        raise ValueError(f"{field_name} must be between 1 and 65535.")
    return int(value)


def validate_redirect_host(value: str, *, field_name: str) -> str:
    if any(marker in value for marker in ("://", "/", "?", "#", "@")):
        raise ValueError(f"{field_name} must be a hostname or IP address without a port.")
    if any(ch.isspace() for ch in value):
        raise ValueError(f"{field_name} must be a hostname or IP address without whitespace.")
    if ":" in value:
        try:
            ip_address(value)
        except ValueError as exc:
            raise ValueError(
                f"{field_name} must be a hostname or IP address without a port."
            ) from exc
        return value
    try:
        ip_address(value)
        return value
    except ValueError:
        pass
    labels = value.split(".")
    if any(not label for label in labels):
        raise ValueError(f"{field_name} must be a hostname or IP address without a port.")
    for label in labels:
        if len(label) > 63 or not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", label):
            raise ValueError(f"{field_name} must be a hostname or IP address without a port.")
    return value


def validate_env_name(name: str) -> None:
    if not _ENV_NAME_RE.fullmatch(name):
        raise ValueError("env keys must be valid environment variable names.")


def validate_tool_policy_overlap(
    *,
    server_id: str,
    allowed_tools: tuple[str, ...],
    denied_tools: tuple[str, ...],
) -> None:
    overlap = sorted(set(allowed_tools) & set(denied_tools))
    if overlap:
        joined = ", ".join(overlap)
        raise ConfigError(
            f"MCP server '{server_id}' cannot allow and deny the same tool(s): {joined}"
        )


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _PolicyModel(_StrictModel):
    enabled: StrictBool = True
    enabled_in: list[RuntimeKind] | None = None
    trust: StrictStr = "explicit"
    allowed_tools: list[StrictStr] = Field(default_factory=list)
    denied_tools: list[StrictStr] = Field(default_factory=list)
    startup_timeout_s: float = 10.0
    call_timeout_s: float = 60.0
    tool_prefix: StrictStr | None = None
    roots_mode: RootsMode = "disabled"
    resources_mode: ResourcesMode = "disabled"
    prompts_mode: PromptsMode = "disabled"

    @field_validator("enabled_in", mode="before")
    @classmethod
    def _normalize_enabled_in(cls, value: object) -> list[RuntimeKind] | None:
        return normalize_runtime_kind_list(value)

    @field_validator("trust", mode="before")
    @classmethod
    def _normalize_trust(cls, value: object) -> str:
        normalized = normalize_optional_string(value, field_name="trust", lowercase=True)
        if normalized is None:
            raise ValueError("trust cannot be empty.")
        if normalized != "explicit":
            raise ValueError("trust must be 'explicit'; no other MCP trust modes are supported.")
        return normalized

    @field_validator("allowed_tools", "denied_tools", mode="before")
    @classmethod
    def _normalize_tool_lists(cls, value: object, info: Any) -> list[str]:
        return normalize_ordered_name_list(value, field_name=str(info.field_name))

    @field_validator("startup_timeout_s", "call_timeout_s", mode="before")
    @classmethod
    def _normalize_timeouts(cls, value: object, info: Any) -> float:
        return normalize_timeout(value, field_name=str(info.field_name))

    @field_validator("tool_prefix", mode="before")
    @classmethod
    def _normalize_tool_prefix(cls, value: object) -> str | None:
        normalized = normalize_optional_string(value, field_name="tool_prefix", lowercase=True)
        if normalized is None:
            return None
        if not _TOOL_PREFIX_RE.fullmatch(normalized):
            raise ValueError("tool_prefix must match [a-z0-9][a-z0-9_-]*.")
        return normalized

    @field_validator("roots_mode", mode="before")
    @classmethod
    def _normalize_roots_mode(cls, value: object) -> RootsMode:
        return normalize_roots_mode(value, field_name="roots_mode")

    @field_validator("resources_mode", mode="before")
    @classmethod
    def _normalize_resources_mode(cls, value: object) -> ResourcesMode:
        return normalize_resources_mode(value, field_name="resources_mode")

    @field_validator("prompts_mode", mode="before")
    @classmethod
    def _normalize_prompts_mode(cls, value: object) -> PromptsMode:
        return normalize_prompts_mode(value, field_name="prompts_mode")

    @model_validator(mode="after")
    def _validate_tool_policy_overlap(self) -> _PolicyModel:
        overlap = sorted(set(self.allowed_tools) & set(self.denied_tools))
        if overlap:
            joined = ", ".join(overlap)
            raise ValueError(f"allowed_tools and denied_tools cannot overlap: {joined}")
        return self


class UserMcpStdioServer(_PolicyModel):
    transport: Literal["stdio"]
    command: StrictStr
    args: list[StrictStr] = Field(default_factory=list)
    env: dict[StrictStr, StrictStr] | None = None

    @field_validator("command", mode="before")
    @classmethod
    def _normalize_command(cls, value: object) -> str:
        normalized = normalize_optional_string(value, field_name="command")
        if normalized is None:
            raise ValueError("command cannot be empty.")
        return normalized

    @field_validator("args", mode="before")
    @classmethod
    def _normalize_args(cls, value: object) -> list[str]:
        return normalize_ordered_args_list(value, field_name="args")

    @field_validator("env", mode="before")
    @classmethod
    def _normalize_env(cls, value: object) -> dict[str, str] | None:
        return normalize_string_mapping(value, field_name="env", key_validator=validate_env_name)


class UserMcpHttpOAuthConfig(_StrictModel):
    client_id: StrictStr
    redirect_host: StrictStr | None = None
    redirect_port: StrictInt | None = None
    scopes: list[StrictStr] | None = None
    authorization_server_url: StrictStr | None = None

    @field_validator("client_id", mode="before")
    @classmethod
    def _normalize_client_id(cls, value: object) -> str:
        normalized = normalize_optional_string(value, field_name="client_id")
        if normalized is None:
            raise ValueError("client_id cannot be empty.")
        return normalized

    @field_validator("redirect_host", mode="before")
    @classmethod
    def _normalize_redirect_host(cls, value: object) -> str | None:
        normalized = normalize_optional_string(value, field_name="redirect_host")
        if normalized is None:
            return None
        return validate_redirect_host(normalized, field_name="redirect_host")

    @field_validator("redirect_port", mode="before")
    @classmethod
    def _normalize_redirect_port(cls, value: object) -> int | None:
        if value is None:
            return None
        return validate_tcp_port(value, field_name="redirect_port")

    @field_validator("scopes", mode="before")
    @classmethod
    def _normalize_scopes(cls, value: object) -> list[str] | None:
        if value is None:
            return None
        return normalize_ordered_string_list(value, field_name="scopes")

    @field_validator("authorization_server_url", mode="before")
    @classmethod
    def _normalize_authorization_server_url(cls, value: object) -> str | None:
        normalized = normalize_optional_string(value, field_name="authorization_server_url")
        if normalized is None:
            return None
        return validate_https_url(
            normalized,
            field_name="authorization_server_url",
            allow_loopback_http=True,
        )


class UserMcpHttpServer(_PolicyModel):
    transport: Literal["http"]
    url: StrictStr
    headers: dict[StrictStr, StrictStr] | None = None
    oauth: UserMcpHttpOAuthConfig | None = None

    @field_validator("url", mode="before")
    @classmethod
    def _normalize_url(cls, value: object) -> str:
        normalized = normalize_optional_string(value, field_name="url")
        if normalized is None:
            raise ValueError("url cannot be empty.")
        return validate_https_url(normalized, field_name="url", allow_loopback_http=True)

    @field_validator("headers", mode="before")
    @classmethod
    def _normalize_headers(cls, value: object) -> dict[str, str] | None:
        return normalize_string_mapping(value, field_name="headers")

    @model_validator(mode="after")
    def _validate_auth_configuration(self) -> UserMcpHttpServer:
        if self.oauth is None or not self.headers:
            return self
        if any(str(key).casefold() == "authorization" for key in self.headers):
            raise ValueError(
                "oauth cannot be combined with headers.Authorization on the same HTTP server."
            )
        return self


UserMcpServerModel = Annotated[
    UserMcpStdioServer | UserMcpHttpServer, Field(discriminator="transport")
]


class ProjectMcpServerOverride(_StrictModel):
    enabled: StrictBool | None = None
    enabled_in: list[RuntimeKind] | None = None
    allowed_tools: list[StrictStr] | None = None
    denied_tools: list[StrictStr] | None = None
    startup_timeout_s: float | None = None
    call_timeout_s: float | None = None
    roots_mode: RootsMode | None = None
    resources_mode: ResourcesMode | None = None
    prompts_mode: PromptsMode | None = None

    @field_validator("enabled_in", mode="before")
    @classmethod
    def _normalize_enabled_in(cls, value: object) -> list[RuntimeKind] | None:
        return normalize_runtime_kind_list(value)

    @field_validator("allowed_tools", "denied_tools", mode="before")
    @classmethod
    def _normalize_tool_lists(cls, value: object, info: Any) -> list[str] | None:
        if value is None:
            return None
        return normalize_ordered_name_list(value, field_name=str(info.field_name))

    @field_validator("startup_timeout_s", "call_timeout_s", mode="before")
    @classmethod
    def _normalize_timeouts(cls, value: object, info: Any) -> float | None:
        if value is None:
            return None
        return normalize_timeout(value, field_name=str(info.field_name))

    @field_validator("roots_mode", mode="before")
    @classmethod
    def _normalize_roots_mode(cls, value: object) -> RootsMode | None:
        if value is None:
            return None
        return normalize_roots_mode(value, field_name="roots_mode")

    @field_validator("resources_mode", mode="before")
    @classmethod
    def _normalize_resources_mode(cls, value: object) -> ResourcesMode | None:
        if value is None:
            return None
        return normalize_resources_mode(value, field_name="resources_mode")

    @field_validator("prompts_mode", mode="before")
    @classmethod
    def _normalize_prompts_mode(cls, value: object) -> PromptsMode | None:
        if value is None:
            return None
        return normalize_prompts_mode(value, field_name="prompts_mode")

    @model_validator(mode="after")
    def _validate_tool_policy_overlap(self) -> ProjectMcpServerOverride:
        allowed_tools = tuple(self.allowed_tools or [])
        denied_tools = tuple(self.denied_tools or [])
        overlap = sorted(set(allowed_tools) & set(denied_tools))
        if overlap:
            joined = ", ".join(overlap)
            raise ValueError(f"allowed_tools and denied_tools cannot overlap: {joined}")
        return self


class UserMcpConfigFile(_StrictModel):
    schema_version: StrictInt = 1
    servers: dict[str, UserMcpServerModel] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must equal 1.")
        return value


class ProjectMcpConfigFile(_StrictModel):
    schema_version: StrictInt = 1
    servers: dict[str, ProjectMcpServerOverride] = Field(default_factory=dict)

    @field_validator("schema_version")
    @classmethod
    def _validate_schema_version(cls, value: int) -> int:
        if value != 1:
            raise ValueError("schema_version must equal 1.")
        return value


def _redact_sensitive_strings(values: tuple[str, ...]) -> list[str]:
    return ["[redacted]" for _ in values]


@dataclass(frozen=True)
class ResolvedMcpHttpOAuthConfig:
    client_id: str
    redirect_host: str | None = None
    redirect_port: int | None = None
    scopes: tuple[str, ...] = field(default_factory=tuple)
    authorization_server_url: str | None = None


@dataclass(frozen=True)
class ResolvedMcpServer:
    id: str
    transport: TransportKind
    enabled: bool
    enabled_in: tuple[RuntimeKind, ...] | None
    trust: str
    allowed_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]
    startup_timeout_s: float
    call_timeout_s: float
    tool_prefix: str | None
    roots_mode: RootsMode = "disabled"
    resources_mode: ResourcesMode = "disabled"
    prompts_mode: PromptsMode = "disabled"
    command: str | None = None
    args: tuple[str, ...] = field(default_factory=tuple, repr=False)
    env: dict[str, str] = field(default_factory=dict, repr=False)
    url: str | None = field(default=None, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)
    oauth: ResolvedMcpHttpOAuthConfig | None = field(default=None, repr=False)
    # Why this server can't start with its saved settings (a ``${VAR}`` it references is not
    # set). It is never started; the session reports it as failed and runs without it.
    settings_error: str | None = None

    def enabled_for(self, runtime_kind: RuntimeKind) -> bool:
        if not self.enabled:
            return False
        if self.enabled_in is None:
            return True
        return runtime_kind in self.enabled_in

    def redacted_connection_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "transport": self.transport,
            "enabled": self.enabled,
            # None means every runtime; an empty list (e.g. only retired runtimes) means none.
            "enabled_in": (
                [kind.value for kind in self.enabled_in] if self.enabled_in is not None else None
            ),
            "trust": self.trust,
            "allowed_tools": list(self.allowed_tools),
            "denied_tools": list(self.denied_tools),
            "startup_timeout_s": self.startup_timeout_s,
            "call_timeout_s": self.call_timeout_s,
            "tool_prefix": self.tool_prefix,
            "roots_mode": self.roots_mode,
            "resources_mode": self.resources_mode,
            "prompts_mode": self.prompts_mode,
        }
        if self.transport == "stdio":
            payload["command"] = self.command
            payload["args"] = _redact_sensitive_strings(self.args)
            payload["env"] = {key: "[redacted]" for key in self.env}
        else:
            payload["headers"] = {key: "[redacted]" for key in self.headers}
            if self.oauth is not None:
                payload["oauth"] = {
                    "client_id": self.oauth.client_id,
                    "redirect_host": self.oauth.redirect_host,
                    "redirect_port": self.oauth.redirect_port,
                    "scopes": list(self.oauth.scopes),
                    "authorization_server_url": self.oauth.authorization_server_url,
                }
        return payload


@dataclass(frozen=True)
class ResolvedMcpConfig:
    workspace_root: Path
    user_config_path: Path
    project_config_path: Path
    user_config_present: bool
    project_config_present: bool
    servers: tuple[ResolvedMcpServer, ...] = ()

    @property
    def has_any_config(self) -> bool:
        return self.user_config_present or self.project_config_present

    def active_servers_for(self, runtime_kind: RuntimeKind) -> tuple[ResolvedMcpServer, ...]:
        return tuple(server for server in self.servers if server.enabled_for(runtime_kind))
