from __future__ import annotations

import hashlib
import ipaddress
import re
import tomllib
import warnings
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Literal
from urllib.parse import SplitResult, parse_qsl, urlsplit, urlunsplit

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

from .plugin_skill_text import (
    MAX_PLUGIN_SKILL_TEXT_CHARS,
    MAX_PLUGIN_SKILLS,
    allowed_origins,
    is_one_line,
    lint_plugin_skill,
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


CUSTOM_PLUGIN_PREFIX = "custom."
_CUSTOM_PLUGIN_ID_PATTERN = re.compile(r"^custom\.[a-z][a-z0-9_]{0,19}$")
MAX_CUSTOM_TOOLS = 64


def custom_tool_alias(name: str, max_chars: int = 28) -> str:
    """The model-facing part of a custom tool's name: lowercase letters, digits and underscores.

    Short enough that ``mcp__custom_<name>__<alias>`` stays within 64 characters.
    """
    alias = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
    if not alias or not alias[0].isalpha():
        alias = f"t_{alias}"
    if len(alias) <= max_chars:
        return alias
    # A shared prefix is common in real server catalogs. Preserve the original
    # name's identity when shortening it, instead of merging unrelated tools.
    suffix = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    return f"{alias[: max_chars - 13].rstrip('_')}_{suffix}"


class CustomConnectorTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
    #: The person's own mark. A server's readOnlyHint never sets it.
    reads_only: bool = False


# The user's decision for custom plugins: HTTPS only, no localhost (review M2D1-C3 / SOL-1).
_LOCAL_NAME_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".localdomain")
_KEY_LIKE_NAME = re.compile(r"key|token|secret|passw|auth|sig|session|credential", re.IGNORECASE)


def _not_a_public_name(host: str) -> str | None:
    """Why a host isn't a public server name, or None. The URL parser already turns numeric
    spellings (127.1, 2130706433, 0x7f.1) into an address, so any address is refused."""
    name = host.strip("[]").rstrip(".").lower()
    try:
        ipaddress.ip_address(name)
        return "an IP address"
    except ValueError:
        pass
    if name == "localhost" or name.endswith(_LOCAL_NAME_SUFFIXES) or "." not in name:
        return "a local name"
    last = name.rsplit(".", 1)[1]
    if last.isdigit() or re.fullmatch(r"0x[0-9a-f]*", last):
        return "an IP address"
    return None


class CustomConnectorComponent(BaseModel):
    """The user's own online tool server (Plugins M2d): HTTPS only, the app keeps the login."""

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    url: HttpUrl
    auth: Literal["none", "api_key", "oauth"]
    #: For an API key: the header main sends it in, and whether main writes "Bearer " first.
    auth_header: Annotated[str, Field(pattern=r"^[A-Za-z0-9-]{1,64}$")] | None = None
    auth_bearer: bool = False
    #: For OAuth (M2d-2): where the person signs in, and how Alysis's client was registered. The
    #: client id and the tokens stay in the app's store; changing these needs consent again.
    oauth_issuer: HttpUrl | None = None
    oauth_client: Literal["dynamic", "own"] | None = None
    oauth_scopes: list[Annotated[str, Field(pattern=r"^[\x21\x23-\x5b\x5d-\x7e]{1,128}$")]] = Field(
        default_factory=list, max_length=32
    )
    tools: list[CustomConnectorTool] = Field(min_length=1, max_length=MAX_CUSTOM_TOOLS)

    @model_validator(mode="after")
    def _validate_connector(self) -> CustomConnectorComponent:
        if str(self.url.scheme).lower() != "https" or not self.url.host:
            raise ValueError("url must be an https address")
        if self.url.username or self.url.password or self.url.fragment:
            raise ValueError("url may not contain a user name, password or fragment")
        problem = _not_a_public_name(str(self.url.host))
        if problem:
            raise ValueError(
                f"url must be a public server name, not {problem}: custom plugins are HTTPS only;"
                " a program on this computer belongs under your own tool servers"
            )
        if any(_KEY_LIKE_NAME.search(name) for name, _ in parse_qsl(self.url.query or "", True)):
            raise ValueError("url may not carry a key in its query; use the API key setting")
        if (self.auth == "api_key") != (self.auth_header is not None):
            raise ValueError("auth_header is required for an API key, and only then")
        if self.auth != "api_key" and self.auth_bearer:
            raise ValueError("auth_bearer needs an API key")
        oauth = self.auth == "oauth"
        if oauth != (self.oauth_issuer is not None) or oauth != (self.oauth_client is not None):
            raise ValueError("oauth_issuer and oauth_client are required for OAuth, and only then")
        if self.oauth_scopes and not oauth:
            raise ValueError("oauth_scopes need OAuth")
        if self.oauth_issuer is not None and (
            str(self.oauth_issuer.scheme).lower() != "https"
            or not self.oauth_issuer.host
            or self.oauth_issuer.username
            or self.oauth_issuer.password
            or self.oauth_issuer.fragment
        ):
            raise ValueError("oauth_issuer must be an https address")
        # RFC 8414: an issuer has no query or fragment (review OA-C2).
        if self.oauth_issuer is not None and (
            self.oauth_issuer.query or self.oauth_issuer.fragment
        ):
            raise ValueError("oauth_issuer can't have a query or fragment")
        if self.oauth_issuer is not None and (
            problem := _not_a_public_name(str(self.oauth_issuer.host))
        ):
            raise ValueError(f"oauth_issuer must be a public server name, not {problem}")
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("tool names must be unique")
        if len({custom_tool_alias(name) for name in names}) != len(names):
            raise ValueError("tool names must stay distinct once lowercased for the model")
        return self


VERIFIED_PLUGIN_PREFIX = "alysis."
_VERIFIED_PLUGIN_ID_PATTERN = re.compile(r"^alysis\.[a-z][a-z0-9_]{0,19}$")
MAX_VERIFIED_TOOLS = 96
#: The shortest alias room any verified plugin has: ``mcp__<20 characters>__<alias>``.
VERIFIED_TOOL_ALIAS_CHARS = 37
DEFAULT_CONNECTOR_ARGUMENT_BYTES = 8 * 1024
MAX_VERIFIED_ARGUMENT_BYTES = 32 * 1024


def verified_tool_alias(plugin_id: str, name: str) -> str:
    """The model-facing part of a verified connector tool's name (``mcp__notion__<alias>``).

    Uses all the room ``mcp__<name>__`` leaves within 64 characters; names distinct at the
    shortest room (checked in the descriptor) stay distinct.
    """
    short = plugin_id.removeprefix(VERIFIED_PLUGIN_PREFIX)
    return custom_tool_alias(name, 64 - len(f"mcp__{short}__"))


def _https_address(value: str, field: str, *, query: bool) -> SplitResult:
    """A public https address written exactly as reviewed; the text is kept as is, unparsed."""
    parts = urlsplit(value)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or "#" in value
        or any(c.isspace() for c in value)
    ):
        raise ValueError(f"{field} must be an https address without a user name or fragment")
    if parts.query and not query:
        raise ValueError(f"{field} can't have a query")
    if problem := _not_a_public_name(parts.hostname):
        raise ValueError(f"{field} must be a public server name, not {problem}")
    return parts


class VerifiedConnectorTool(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")]
    #: Alysis's review. It is the only source of what is a read; a server's hint never counts.
    reads_only: bool


class VerifiedHostArgument(BaseModel):
    """An argument the desktop app fills in after approval; the AI can never set it."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")]
    source: Literal["atlassian_site"]


class VerifiedConnectorComponent(BaseModel):
    """A vendor's official online tool server, reviewed by Alysis in a pinned bundled descriptor.

    Like a custom connector, the app keeps the login and calls the server. The reviewed tool
    list is the only list offered, and its marks are the only source of what is a read.
    """

    model_config = ConfigDict(extra="forbid")

    id: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]
    url: Annotated[str, Field(min_length=1, max_length=512)]
    #: The RFC 8707 / 9728 resource; the url without its query when not given.
    resource: Annotated[str, Field(min_length=1, max_length=512)] | None = None
    oauth_issuer: Annotated[str, Field(min_length=1, max_length=512)]
    #: Extra origins for the authorize, token, register and revoke endpoints.
    oauth_sign_in_origins: list[Annotated[str, Field(min_length=1, max_length=256)]] = Field(
        default_factory=list, max_length=8
    )
    oauth_client: Literal["dynamic"]
    oauth_scopes: list[
        Annotated[str, Field(pattern=r"^[\x21\x23-\x26\x28-\x5b\x5d-\x7e]{1,128}$")]
    ] = Field(min_length=1, max_length=32)
    tools: list[VerifiedConnectorTool] = Field(min_length=1, max_length=MAX_VERIFIED_TOOLS)
    host_arguments: list[VerifiedHostArgument] = Field(default_factory=list, max_length=4)
    max_argument_bytes: Annotated[
        int, Field(ge=DEFAULT_CONNECTOR_ARGUMENT_BYTES, le=MAX_VERIFIED_ARGUMENT_BYTES)
    ] = DEFAULT_CONNECTOR_ARGUMENT_BYTES

    @property
    def effective_resource(self) -> str:
        if self.resource is not None:
            return self.resource
        parts = urlsplit(self.url)
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))

    @model_validator(mode="after")
    def _validate_connector(self) -> VerifiedConnectorComponent:
        url = _https_address(self.url, "url", query=True)
        if any(_KEY_LIKE_NAME.search(name) for name, _ in parse_qsl(url.query, True)):
            raise ValueError("url may not carry a key in its query")
        if self.resource is not None:
            _https_address(self.resource, "resource", query=False)
        _https_address(self.oauth_issuer, "oauth_issuer", query=False)
        for origin in self.oauth_sign_in_origins:
            parts = _https_address(origin, "oauth_sign_in_origins", query=False)
            if parts.path or origin != f"https://{parts.netloc}":
                raise ValueError("oauth_sign_in_origins entries are bare https origins")
        if len(set(self.oauth_sign_in_origins)) != len(self.oauth_sign_in_origins):
            raise ValueError("oauth_sign_in_origins must be unique")
        if len(set(self.oauth_scopes)) != len(self.oauth_scopes):
            raise ValueError("oauth_scopes must be unique")
        names = [tool.name for tool in self.tools]
        if len(set(names)) != len(names):
            raise ValueError("tool names must be unique")
        if len({custom_tool_alias(name, VERIFIED_TOOL_ALIAS_CHARS) for name in names}) != len(
            names
        ):
            raise ValueError("tool names must stay distinct once lowercased for the model")
        arguments = [argument.name for argument in self.host_arguments]
        if len(set(arguments)) != len(arguments):
            raise ValueError("host_arguments names must be unique")
        return self


class PluginSkillComponent(BaseModel):
    """Plain-text publisher guidance inside the signed descriptor; never files, never authority."""

    model_config = ConfigDict(extra="forbid")

    name: Annotated[str, Field(pattern=r"^[a-z][a-z0-9-]{0,47}$")]
    title: Annotated[str, Field(min_length=1, max_length=80)]
    description: Annotated[str, Field(min_length=1, max_length=140)]
    uses_tools: list[str] = Field(default_factory=list, max_length=16)
    text: Annotated[str, Field(min_length=1)]


class Components(BaseModel):
    model_config = ConfigDict(extra="forbid")

    skill: list[SkillComponent] = Field(default_factory=list)
    # Inside [components], so a changed skill text is an access change (Needs review).
    plugin_skill: list[PluginSkillComponent] = Field(
        default_factory=list, max_length=MAX_PLUGIN_SKILLS
    )
    custom_connector: list[CustomConnectorComponent] = Field(default_factory=list, max_length=1)
    verified_connector: list[VerifiedConnectorComponent] = Field(default_factory=list, max_length=1)
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

    @model_validator(mode="after")
    def _validate_custom_plugin(self) -> PluginManifest:
        """`custom.*` is the user's own online plugin, and nothing else may be one."""
        components = self.components
        if not self.plugin.id.startswith(CUSTOM_PLUGIN_PREFIX):
            if components.custom_connector:
                raise ValueError("components.custom_connector: only for custom.* plugins")
            return self
        if not _CUSTOM_PLUGIN_ID_PATTERN.fullmatch(self.plugin.id):
            raise ValueError("plugin.id: a custom plugin id is custom.<lowercase name>")
        if len(components.custom_connector) != 1:
            raise ValueError("a custom plugin has exactly one components.custom_connector")
        if (
            components.skill
            or components.tool
            or components.mcp_server
            or components.hook
            or components.verified_connector
        ):
            raise ValueError(
                "a custom plugin can't run programs: no skill folders, tools, MCP servers or hooks"
            )
        # The person's own text reaches approval labels and the skill catalog (review M2-C1).
        for field, value in (("name", self.plugin.name), ("description", self.plugin.description)):
            if not is_one_line(value):
                raise ValueError(f"plugin.{field}: a custom plugin's {field} must be one line")
        return self

    @model_validator(mode="after")
    def _validate_verified_connector(self) -> PluginManifest:
        """A verified connector is an Alysis-reviewed plugin of its own: plugin skills only."""
        components = self.components
        if not components.verified_connector:
            return self
        if not _VERIFIED_PLUGIN_ID_PATTERN.fullmatch(self.plugin.id):
            raise ValueError(
                "components.verified_connector: only for alysis.<lowercase name> plugins"
            )
        if (
            components.skill
            or components.tool
            or components.mcp_server
            or components.hook
            or components.custom_connector
        ):
            raise ValueError("components.verified_connector: only plugin skills may accompany it")
        return self

    @model_validator(mode="after")
    def _validate_plugin_skills(self) -> PluginManifest:
        skills = self.components.plugin_skill
        if not skills:
            return self
        if len({skill.name for skill in skills}) != len(skills):
            raise ValueError("components.plugin_skill: names must be unique")
        if not is_one_line(self.plugin.name):
            raise ValueError("plugin.name: must be one line when the plugin carries skills")
        if sum(len(skill.text) for skill in skills) > MAX_PLUGIN_SKILL_TEXT_CHARS:
            raise ValueError(
                f"components.plugin_skill: more than {MAX_PLUGIN_SKILL_TEXT_CHARS} characters"
            )
        origins = allowed_origins(
            [str(server.url) for server in self.components.mcp_server if server.url]
            + [str(connector.url) for connector in self.components.custom_connector]
            + [
                origin
                for connector in self.components.verified_connector
                for origin in (connector.url, *connector.oauth_sign_in_origins)
            ]
            + ([str(self.plugin.homepage)] if self.plugin.homepage else [])
        )
        for skill in skills:
            problems = lint_plugin_skill(
                title=skill.title,
                description=skill.description,
                text=skill.text,
                uses_tools=skill.uses_tools,
                allowed_origins=origins,
            )
            if problems:
                raise ValueError(f"components.plugin_skill {skill.name}: " + "; ".join(problems))
        return self


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
        + len(manifest.components.plugin_skill)
        + len(manifest.components.custom_connector)
        + len(manifest.components.verified_connector)
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
    "CustomConnectorComponent",
    "CustomConnectorTool",
    "PluginSkillComponent",
    "SkillComponent",
    "ToolComponent",
    "VerifiedConnectorComponent",
    "VerifiedConnectorTool",
    "VerifiedHostArgument",
    "_validate_manifest_invariants",
    "load_manifest",
]
