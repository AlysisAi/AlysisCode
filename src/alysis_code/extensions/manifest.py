from __future__ import annotations

import re
import tomllib
import warnings
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

MANIFEST_FILENAME = "alysis-plugin.toml"
MAX_MANIFEST_BYTES = 64 * 1024

_PLUGIN_ID_PATTERN = r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_-]*$"
_ENV_NAME_PATTERN = r"^[A-Z][A-Z0-9_]*$"
_KEYWORD_PATTERN = r"^[a-z0-9_-]+$"
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_COMMON_SPDX_LICENSE_IDS = frozenset(
    {
        "Apache-2.0",
        "BSD-2-Clause",
        "BSD-3-Clause",
        "GPL-3.0-only",
        "GPL-3.0-or-later",
        "ISC",
        "LGPL-3.0-only",
        "LGPL-3.0-or-later",
        "MIT",
        "MPL-2.0",
        "Proprietary",
        "Unlicense",
    }
)
_CONTACT_URL_ADAPTER = TypeAdapter(HttpUrl)

PluginId = Annotated[str, Field(max_length=64, pattern=_PLUGIN_ID_PATTERN)]
PluginName = Annotated[str, Field(min_length=1, max_length=60)]
Text200 = Annotated[str, Field(min_length=1, max_length=200)]
NonEmptyString = Annotated[str, Field(min_length=1)]
Keyword = Annotated[str, Field(min_length=1, max_length=32, pattern=_KEYWORD_PATTERN)]
ComponentPath = Annotated[str, Field(min_length=1)]
OptionalComponentId = Annotated[str, Field(min_length=1)] | None
EnvName = Annotated[str, Field(pattern=_ENV_NAME_PATTERN)]
ScopeName = Annotated[str, Field(min_length=1)]
CommandArg = Annotated[str, Field(min_length=1)]


class PluginManifestError(Exception):
    def __init__(self, errors: list[str] | tuple[str, ...]) -> None:
        self.errors = tuple(str(error) for error in errors)
        if self.errors:
            message = "Invalid plugin manifest:\n" + "\n".join(
                f"  - {error}" for error in self.errors
            )
        else:
            message = "Invalid plugin manifest."
        super().__init__(message)


class PluginMeta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: PluginId
    name: PluginName
    version: NonEmptyString
    description: Text200
    author: Text200
    license: NonEmptyString
    homepage: HttpUrl | None = None
    repository: HttpUrl | None = None
    keywords: list[Keyword] = Field(default_factory=list, max_length=8)

    @field_validator("version")
    @classmethod
    def _validate_version(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as exc:
            raise ValueError("must be a valid PEP 440 / SemVer version") from exc
        return value

    @field_validator("license")
    @classmethod
    def _validate_license(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be empty")
        return value


class Compatibility(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    # Manifests written before the rename declare this key as ``alysis``.
    # With extra="forbid" they would fail validation outright, so the old
    # spelling stays accepted as an alias.
    alysis: NonEmptyString = Field(
        validation_alias="alysis",
        serialization_alias="alysis",
    )
    platforms: list[Literal["linux", "darwin", "windows"]] = Field(
        default_factory=lambda: ["linux", "darwin", "windows"]
    )

    @field_validator("alysis")
    @classmethod
    def _validate_alysis(cls, value: str) -> str:
        try:
            SpecifierSet(value)
        except InvalidSpecifier as exc:
            raise ValueError("must be a valid PEP 440 specifier set") from exc
        return value


class SkillComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: ComponentPath
    id: OptionalComponentId = None
    enabled: bool = True


class ToolComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: ComponentPath
    id: OptionalComponentId = None
    enabled: bool = True
    description: Text200
    required_env: list[EnvName] = Field(default_factory=list)
    network: bool
    filesystem: Literal["none", "read", "write"]
    timeout_sec: Annotated[int, Field(ge=1, le=600)] = 60


class McpServerComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(min_length=1)]
    enabled: bool = True
    host_managed: bool = False
    transport: Literal["stdio", "http"]
    command: list[CommandArg] | None = Field(default=None, min_length=1)
    url: HttpUrl | None = None
    env: list[EnvName] = Field(default_factory=list)
    scopes: list[ScopeName] = Field(min_length=1, max_length=32)
    oauth: dict[str, Any] | None = None

    @model_validator(mode="after")
    def _validate_transport(self) -> McpServerComponent:
        if self.host_managed and (self.transport != "http" or self.env or self.oauth is not None):
            raise ValueError(
                "host_managed MCP requires HTTP without environment or OAuth credentials"
            )
        if self.transport == "stdio":
            if self.command is None:
                raise ValueError("command is required when transport is 'stdio'")
            if self.url is not None:
                raise ValueError("url is not allowed when transport is 'stdio'")
            return self

        if self.url is None:
            raise ValueError("url is required when transport is 'http'")
        if self.command is not None:
            raise ValueError("command is not allowed when transport is 'http'")

        scheme = str(self.url.scheme).lower()
        host = str(self.url.host or "").lower()
        if scheme == "http" and host not in {"localhost", "127.0.0.1"}:
            raise ValueError("url must use https unless host is localhost or 127.0.0.1")
        return self


class HookComponent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: Literal["PreToolUse", "PostToolUse", "PostWrite", "SessionStart", "SessionStop"]
    path: ComponentPath
    id: OptionalComponentId = None
    enabled: bool = True


class Components(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: list[SkillComponent] = Field(default_factory=list)
    tool: list[ToolComponent] = Field(default_factory=list)
    mcp_server: list[McpServerComponent] = Field(default_factory=list)
    hook: list[HookComponent] = Field(default_factory=list)


class Security(BaseModel):
    model_config = ConfigDict(extra="forbid")

    contact: NonEmptyString
    policy_url: HttpUrl | None = None
    disclosure_days: Annotated[int, Field(ge=1, le=365)] | None = None

    @field_validator("contact")
    @classmethod
    def _validate_contact(cls, value: str) -> str:
        if _EMAIL_PATTERN.fullmatch(value):
            return value
        try:
            parsed = _CONTACT_URL_ADAPTER.validate_python(value)
        except ValidationError as exc:
            raise ValueError("must be an email address or https URL") from exc
        if str(parsed.scheme).lower() != "https":
            raise ValueError("must be an email address or https URL")
        return value


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    plugin: PluginMeta
    compatibility: Compatibility
    components: Components = Field(default_factory=Components)
    security: Security | None = None


def resolve_manifest_path(plugin_root: Path) -> Path:
    """Return the canonical plugin manifest location."""
    return plugin_root / MANIFEST_FILENAME


def load_manifest(plugin_root: Path) -> PluginManifest:
    manifest_path = resolve_manifest_path(plugin_root)
    text = _read_manifest_text(manifest_path)

    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise PluginManifestError([_format_toml_error(exc)]) from exc

    try:
        manifest = PluginManifest.model_validate(raw)
    except ValidationError as exc:
        raise PluginManifestError(_format_validation_errors(exc)) from exc

    invariant_messages = _validate_manifest_invariants(manifest, plugin_root)
    warning_messages = [
        message.removeprefix("WARN: ").strip()
        for message in invariant_messages
        if message.startswith("WARN: ")
    ]
    error_messages = [message for message in invariant_messages if not message.startswith("WARN: ")]

    license_warning = _license_warning(manifest)
    if license_warning is not None:
        warning_messages.append(license_warning)

    if error_messages:
        raise PluginManifestError(
            [*error_messages, *(f"WARN: {message}" for message in warning_messages)]
        )

    for message in warning_messages:
        warnings.warn(message, stacklevel=2)

    return manifest


def _validate_manifest_invariants(manifest: PluginManifest, plugin_root: Path) -> list[str]:
    messages: list[str] = []
    total_components = (
        len(manifest.components.skill)
        + len(manifest.components.tool)
        + len(manifest.components.mcp_server)
        + len(manifest.components.hook)
    )
    if total_components > 32:
        messages.append(
            f"components: total component count {total_components} exceeds the cap of 32"
        )
    if total_components == 0:
        messages.append(
            "WARN: components: manifest declares no components; metadata-only plugins are allowed "
            "but contribute nothing."
        )

    plugin_root_resolved = plugin_root.resolve()
    for collection_name, index, raw_path in _iter_component_paths(manifest):
        path_label = f"components.{collection_name}[{index}].path"
        if _is_path_absolute(raw_path):
            messages.append(f"{path_label}: absolute paths are not allowed")
            continue
        if _contains_parent_reference(raw_path):
            messages.append(f"{path_label}: path escapes or could resolve outside plugin root")
            continue

        component_path = _resolve_component_path(plugin_root_resolved, raw_path)
        try:
            component_path.relative_to(plugin_root_resolved)
        except ValueError:
            messages.append(f"{path_label}: resolved path is outside plugin root: {raw_path}")
            continue

        if not component_path.exists():
            messages.append(f"{path_label}: path does not exist: {raw_path}")

    for collection_name, ids in _effective_ids_by_type(manifest).items():
        seen: dict[str, int] = {}
        for index, effective_id in enumerate(ids):
            if effective_id in seen:
                messages.append(
                    "components."
                    f"{collection_name}[{index}]: duplicate effective id {effective_id!r} "
                    f"within {collection_name}"
                )
                continue
            seen[effective_id] = index

    return messages


def _read_manifest_text(manifest_path: Path) -> str:
    try:
        size = manifest_path.stat().st_size
    except FileNotFoundError as exc:
        raise PluginManifestError([f"manifest file not found: {manifest_path}"]) from exc
    except OSError as exc:
        raise PluginManifestError([f"failed to stat manifest file: {manifest_path}"]) from exc

    if size > MAX_MANIFEST_BYTES:
        raise PluginManifestError(
            [f"manifest file exceeds the {MAX_MANIFEST_BYTES}-byte limit: {size} bytes"]
        )

    try:
        raw_bytes = manifest_path.read_bytes()
    except OSError as exc:
        raise PluginManifestError([f"failed to read manifest file: {manifest_path}"]) from exc

    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PluginManifestError(["manifest file is not valid UTF-8"]) from exc


def _format_toml_error(exc: tomllib.TOMLDecodeError) -> str:
    line = getattr(exc, "lineno", None)
    column = getattr(exc, "colno", None)
    if line is None or column is None:
        position = getattr(exc, "pos", None)
        document = getattr(exc, "doc", None)
        if isinstance(position, int) and isinstance(document, str):
            prefix = document[:position]
            line = prefix.count("\n") + 1
            column = position - prefix.rfind("\n")
    location = ""
    if isinstance(line, int) and isinstance(column, int):
        location = f" at line {line}, column {column}"
    reason = getattr(exc, "msg", None) or str(exc)
    return f"toml parse error{location}: {reason}"


def _format_validation_errors(exc: ValidationError) -> list[str]:
    formatted: list[str] = []
    for error in exc.errors():
        location = _format_error_location(error.get("loc", ()))
        message = _normalize_validation_message(str(error.get("msg", "Invalid value")))
        if location:
            formatted.append(f"{location}: {message}")
        else:
            formatted.append(message)
    return formatted


def _format_error_location(location: tuple[Any, ...] | list[Any]) -> str:
    parts: list[str] = []
    for item in location:
        if isinstance(item, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{item}]"
            else:
                parts.append(f"[{item}]")
            continue
        parts.append(str(item))
    return ".".join(parts)


def _normalize_validation_message(message: str) -> str:
    return message.removeprefix("Value error, ")


def _iter_component_paths(manifest: PluginManifest) -> list[tuple[str, int, str]]:
    paths: list[tuple[str, int, str]] = []
    for collection_name, components in (
        ("skill", manifest.components.skill),
        ("tool", manifest.components.tool),
        ("hook", manifest.components.hook),
    ):
        for index, component in enumerate(components):
            paths.append((collection_name, index, component.path))
    return paths


def _effective_ids_by_type(manifest: PluginManifest) -> dict[str, list[str]]:
    return {
        "skill": [
            component.id or _default_component_id(component.path)
            for component in manifest.components.skill
        ],
        "tool": [
            component.id or _default_component_id(component.path)
            for component in manifest.components.tool
        ],
        "mcp_server": [component.id for component in manifest.components.mcp_server],
        "hook": [
            component.id or _default_component_id(component.path)
            for component in manifest.components.hook
        ],
    }


def _default_component_id(path_value: str) -> str:
    return PureWindowsPath(path_value).name or PurePosixPath(path_value).name


def _is_path_absolute(path_value: str) -> bool:
    posix_path = PurePosixPath(path_value)
    windows_path = PureWindowsPath(path_value)
    return bool(posix_path.is_absolute() or windows_path.is_absolute() or windows_path.drive)


def _contains_parent_reference(path_value: str) -> bool:
    posix_parts = PurePosixPath(path_value).parts
    windows_parts = PureWindowsPath(path_value).parts
    return ".." in posix_parts or ".." in windows_parts


def _resolve_component_path(plugin_root: Path, path_value: str) -> Path:
    windows_path = PureWindowsPath(path_value)
    parts = [part for part in windows_path.parts if part not in {"", "."}]
    return plugin_root.joinpath(*parts).resolve()


def _license_warning(manifest: PluginManifest) -> str | None:
    license_id = manifest.plugin.license.strip()
    if license_id in _COMMON_SPDX_LICENSE_IDS:
        return None
    return (
        f"plugin.license: {license_id!r} is not in the common SPDX allow-list; "
        "double-check the identifier."
    )


__all__ = [
    "Compatibility",
    "Components",
    "HookComponent",
    "MANIFEST_FILENAME",
    "resolve_manifest_path",
    "MAX_MANIFEST_BYTES",
    "McpServerComponent",
    "PluginManifest",
    "PluginManifestError",
    "PluginMeta",
    "Security",
    "SkillComponent",
    "ToolComponent",
    "_validate_manifest_invariants",
    "load_manifest",
]
