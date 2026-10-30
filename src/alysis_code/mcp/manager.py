from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import threading
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..runtime_kind import RuntimeKind, normalize_runtime_kind
from .catalog import McpListedTool
from .config import load_resolved_mcp_config, project_mcp_config_path, user_mcp_config_path
from .errors import McpConfigError as ConfigError
from .forge_scope import ForgeTaskMcpScope
from .models import ResolvedMcpConfig, ResolvedMcpServer
from .native_relay_guard import legacy_windows_description_max_chars, legacy_windows_tool_guidance
from .prompts import McpListedPrompt, prompts_mode_enabled
from .resources import McpListedResource, resources_mode_enabled
from .retention import ephemeral_tool_io, requested_result_summary
from .untrusted_content import (
    MCP_UNTRUSTED_TEXT_CHAR_LIMIT,
    _looks_like_binary_blob,
    build_host_owned_mcp_tool_description,
    build_untrusted_mcp_text_block,
    reduce_model_facing_tool_schema,
    server_instructions_context,
)

if TYPE_CHECKING:
    from .client import McpHttpClient, McpStdioClient
    from .visual_delivery import McpVisualDelivery

_LIVE_TOOL_RUNTIMES = frozenset(
    {
        RuntimeKind.INTERACTIVE_CHAT,
        RuntimeKind.ONE_SHOT,
        RuntimeKind.FORGE_EXEC,
    }
)
_MAX_EXPOSED_MCP_TOOLS = 64
_MAX_SINGLE_TOOL_SCHEMA_BYTES = 32_000
_MAX_TOTAL_SCHEMA_BYTES = 120_000
_TOOL_ALIAS_MAX_LEN = 64
_MAX_STRUCTURED_CONTENT_BYTES = 24_000
_MAX_STRUCTURED_STRING_CHARS = 2_000
_MAX_STRUCTURED_CONTENT_DEPTH = 12
_MAX_LISTED_MCP_RESOURCES_TOTAL = 256
_MAX_LISTED_MCP_RESOURCES_PER_SERVER = 128
_DEFAULT_MCP_RESOURCE_LIST_LIMIT = 20
_MAX_MCP_RESOURCE_LIST_LIMIT = 50
_MAX_LISTED_MCP_PROMPTS_TOTAL = 256
_MAX_LISTED_MCP_PROMPTS_PER_SERVER = 128
_DEFAULT_MCP_PROMPT_LIST_LIMIT = 20
_MAX_MCP_PROMPT_LIST_LIMIT = 50
_ALIAS_CHARS_RE = re.compile(r"[^a-z0-9]+")
_RESOURCE_TOOL_ALIASES = frozenset({"mcp_resources_list", "mcp_resource_read"})


class _McpLiveClient(Protocol):
    """Connection interface used by catalog and lifecycle management."""

    transport: Any

    @property
    def closed(self) -> bool: ...
    @property
    def tools_list_changed(self) -> bool: ...
    @property
    def resources_list_changed(self) -> bool: ...
    @property
    def prompts_list_changed(self) -> bool: ...
    @property
    def session_negotiated(self) -> bool: ...
    @property
    def roots_capability_enabled(self) -> bool: ...
    @property
    def supports_tools(self) -> bool: ...
    @property
    def supports_resources(self) -> bool: ...
    @property
    def supports_prompts(self) -> bool: ...
    @property
    def server_instructions(self) -> str | None: ...
    def ensure_initialized(self) -> None: ...
    def observe_notifications(self) -> None: ...
    def list_tools(self) -> tuple[McpListedTool, ...]: ...
    def list_resources(self) -> tuple[McpListedResource, ...]: ...
    def list_prompts(self) -> tuple[McpListedPrompt, ...]: ...
    def read_resource(self, *, resource_uri: str, allowed_uris: Any = None) -> Any: ...
    def get_prompt(self, *, name: str, arguments: dict[str, str] | None = None) -> Any: ...
    def call_tool(self, *, tool_name: str, arguments: dict[str, Any]) -> Any: ...
    def close(self) -> None: ...


def __getattr__(name: str) -> Any:
    # Keep the canonical public classes and the manager's injection seam, while
    # an empty/disabled catalog needs no transports or OAuth implementation.
    if name not in {"McpStdioClient", "McpHttpClient"}:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from . import client

    value = getattr(client, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"McpStdioClient", "McpHttpClient"})


def _client_factory(name: str) -> Any:
    if name in globals():
        return globals()[name]
    return __getattr__(name)


class _McpClientLease:
    """Stable, session-owned indirection for one live MCP connection.

    Tool definitions keep this lease for the lifetime of the agent session.  A
    lifecycle reconnect can therefore replace the underlying transport without
    leaving model-facing tool callables pointed at a closed client.  Disabling a
    lease is fail-closed: every subsequent operation raises before any transport
    call is made.
    """

    def __init__(self, *, server_id: str, client: _McpLiveClient) -> None:
        self.server_id = server_id
        self._client: _McpLiveClient | None = client
        self._enabled = True
        self._generation = 1
        self._lock = threading.RLock()

    def _require_client(self) -> _McpLiveClient:
        if not self._enabled:
            raise RuntimeError(f"MCP server '{self.server_id}' is disabled for this session.")
        client = self._client
        if client is None or client.closed:
            raise RuntimeError(
                f"MCP server '{self.server_id}' is disconnected for this session. "
                "Reconnect it before retrying the operation."
            )
        return client

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    @property
    def closed(self) -> bool:
        with self._lock:
            client = self._client
            return client is None or client.closed

    @property
    def connected(self) -> bool:
        with self._lock:
            if not self._enabled:
                return False
            client = self._client
            if client is None or client.closed:
                return False
            transport = client.transport
            process = getattr(transport, "process", None)
            if process is not None and process.poll() is not None:
                return False
            return True

    @property
    def transport(self) -> Any:
        """Compatibility-only access used by low-level transport diagnostics/tests."""
        with self._lock:
            return self._require_client().transport

    @property
    def tools_list_changed(self) -> bool:
        with self._lock:
            return self._require_client().tools_list_changed

    @property
    def resources_list_changed(self) -> bool:
        with self._lock:
            return self._require_client().resources_list_changed

    @property
    def prompts_list_changed(self) -> bool:
        with self._lock:
            return self._require_client().prompts_list_changed

    @property
    def session_negotiated(self) -> bool:
        with self._lock:
            return self._require_client().session_negotiated

    @property
    def roots_capability_enabled(self) -> bool:
        with self._lock:
            return self._require_client().roots_capability_enabled

    @property
    def supports_tools(self) -> bool:
        with self._lock:
            return self._require_client().supports_tools

    @property
    def server_instructions(self) -> str | None:
        with self._lock:
            return self._require_client().server_instructions

    @property
    def supports_resources(self) -> bool:
        with self._lock:
            return self._require_client().supports_resources

    @property
    def supports_prompts(self) -> bool:
        with self._lock:
            return self._require_client().supports_prompts

    def ensure_initialized(self) -> None:
        with self._lock:
            self._require_client().ensure_initialized()

    def observe_notifications(self) -> None:
        with self._lock:
            self._require_client().observe_notifications()

    def list_tools(self) -> tuple[McpListedTool, ...]:
        with self._lock:
            return self._require_client().list_tools()

    def list_resources(self) -> tuple[McpListedResource, ...]:
        with self._lock:
            return self._require_client().list_resources()

    def list_prompts(self) -> tuple[McpListedPrompt, ...]:
        with self._lock:
            return self._require_client().list_prompts()

    def read_resource(
        self,
        *,
        resource_uri: str,
        allowed_uris: Any = None,
    ) -> Any:
        with self._lock:
            return self._require_client().read_resource(
                resource_uri=resource_uri,
                allowed_uris=allowed_uris,
            )

    def get_prompt(self, *, name: str, arguments: dict[str, str] | None = None) -> Any:
        with self._lock:
            return self._require_client().get_prompt(name=name, arguments=arguments)

    def call_tool(self, *, tool_name: str, arguments: dict[str, Any]) -> Any:
        with self._lock:
            return self._require_client().call_tool(tool_name=tool_name, arguments=arguments)

    def disable(self) -> bool:
        with self._lock:
            changed = self._enabled or self._client is not None
            self._enabled = False
            client = self._client
            self._client = None
            if client is not None:
                client.close()
            return changed

    def replace_client(self, client: _McpLiveClient) -> None:
        with self._lock:
            old_client = self._client
            self._client = client
            self._enabled = True
            self._generation += 1
            if old_client is not None and old_client is not client:
                old_client.close()

    def close(self) -> None:
        self.disable()


_McpSessionClient = _McpLiveClient | _McpClientLease


def _sanitize_alias_part(value: str, *, fallback: str) -> str:
    lowered = str(value or "").strip().lower()
    ascii_value = unicodedata.normalize("NFKD", lowered).encode("ascii", "ignore").decode("ascii")
    sanitized = _ALIAS_CHARS_RE.sub("_", ascii_value).strip("_")
    sanitized = re.sub(r"_+", "_", sanitized)
    return sanitized or fallback


def _normalize_tool_name_key(value: str) -> str:
    return str(value).casefold()


def _mcp_runtime_enabled(runtime_kind: RuntimeKind) -> bool:
    return runtime_kind in _LIVE_TOOL_RUNTIMES


def _server_namespace(server: ResolvedMcpServer) -> str:
    if server.tool_prefix:
        return _sanitize_alias_part(server.tool_prefix, fallback="server")
    return _sanitize_alias_part(server.id, fallback="server")


def _tool_base_alias(server: ResolvedMcpServer, tool_name: str) -> str:
    return f"mcp__{_server_namespace(server)}__{_sanitize_alias_part(tool_name, fallback='tool')}"


def _hashed_alias(base_alias: str, *, unique_key: str) -> str:
    digest = hashlib.sha1(unique_key.encode("utf-8")).hexdigest()[:10]
    suffix = f"__{digest}"
    prefix_len = max(1, _TOOL_ALIAS_MAX_LEN - len(suffix))
    prefix = base_alias[:prefix_len].rstrip("_")
    if not prefix:
        prefix = "mcp"
    return f"{prefix}{suffix}"


def _stable_json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")))


def _omitted_structured_value(reason: str, *, detail: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "omitted": True,
        "reason": reason,
    }
    if detail:
        payload["summary"] = detail
    return payload


def _sanitize_structured_content_value(value: Any, *, depth: int = 0) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if math.isfinite(value):
            return value
        return _omitted_structured_value("non_finite_number", detail="non-finite number omitted")
    if isinstance(value, str):
        if _looks_like_binary_blob(value):
            return _omitted_structured_value(
                "binary_like_string",
                detail=f"binary-like string omitted ({len(value)} chars)",
            )
        if len(value) > _MAX_STRUCTURED_STRING_CHARS:
            return _omitted_structured_value(
                "string_too_large",
                detail=f"long string omitted ({len(value)} chars)",
            )
        return value
    if depth >= _MAX_STRUCTURED_CONTENT_DEPTH:
        return _omitted_structured_value(
            "max_depth_exceeded",
            detail="nested structured content omitted",
        )
    if isinstance(value, list):
        return [_sanitize_structured_content_value(item, depth=depth + 1) for item in value]
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            sanitized[str(key)] = _sanitize_structured_content_value(item, depth=depth + 1)
        return sanitized
    return _omitted_structured_value(
        "unsupported_type",
        detail=f"{type(value).__name__} omitted",
    )


def _normalize_prompt_structured_content(value: Any) -> Any:
    sanitized = _sanitize_structured_content_value(copy.deepcopy(value))
    try:
        content_bytes = _stable_json_size(sanitized)
    except TypeError:
        return _omitted_structured_value(
            "non_serializable",
            detail="non-serializable structured content omitted",
        )
    if content_bytes > _MAX_STRUCTURED_CONTENT_BYTES:
        return _omitted_structured_value(
            "structured_content_too_large",
            detail=f"structured content omitted ({content_bytes} bytes)",
        )
    return sanitized


def _validate_function_tool_schema(
    *,
    server: ResolvedMcpServer,
    tool: McpListedTool,
) -> dict[str, Any]:
    raw_schema = copy.deepcopy(tool.input_schema)
    if raw_schema.get("type") != "object":
        raise ConfigError(
            f"MCP server '{server.id}' tool '{tool.name}' has incompatible inputSchema: "
            "root type must be 'object'."
        )
    properties = raw_schema.get("properties", {})
    if not isinstance(properties, dict):
        raise ConfigError(
            f"MCP server '{server.id}' tool '{tool.name}' has incompatible inputSchema: "
            "'properties' must be an object."
        )
    required = raw_schema.get("required", [])
    if not isinstance(required, list) or any(not isinstance(item, str) for item in required):
        raise ConfigError(
            f"MCP server '{server.id}' tool '{tool.name}' has incompatible inputSchema: "
            "'required' must be an array of strings."
        )
    schema = reduce_model_facing_tool_schema(raw_schema)
    try:
        schema_bytes = _stable_json_size(schema)
    except TypeError as exc:
        raise ConfigError(
            f"MCP server '{server.id}' tool '{tool.name}' has non-serializable inputSchema."
        ) from exc
    if schema_bytes > _MAX_SINGLE_TOOL_SCHEMA_BYTES:
        raise ConfigError(
            f"MCP server '{server.id}' tool '{tool.name}' exposes an inputSchema that is too "
            f"large ({schema_bytes} bytes). Narrow exposure with allowed_tools."
        )
    return schema


def _wrap_tool_result_text(
    *,
    server_id: str,
    tool_name: str,
    text: str,
    mime_type: str | None = None,
) -> str:
    original_char_count = len(text)
    truncated = False
    wrapped_text = text
    if _looks_like_binary_blob(text):
        wrapped_text = "(omitted: binary-like text)"
    elif len(text) > MCP_UNTRUSTED_TEXT_CHAR_LIMIT:
        wrapped_text = text[:MCP_UNTRUSTED_TEXT_CHAR_LIMIT]
        truncated = True
    return build_untrusted_mcp_text_block(
        source_type="tool_result",
        server_id=server_id,
        source_name=tool_name,
        text=wrapped_text,
        mime_type=mime_type,
        original_char_count=original_char_count,
        truncated=truncated,
    )


def mcp_tool_approval_hints(raw_tool: Any) -> tuple[bool, bool]:
    """(read_only, destructive) from a listed tool's MCP annotations.

    Only an explicit ``readOnlyHint: true`` counts as read-only, and only an explicit
    ``destructiveHint: true`` counts as destructive; destructive wins when a server sets
    both. A tool without annotations is neither, so it is treated as a write.
    """
    annotations = raw_tool.get("annotations") if isinstance(raw_tool, dict) else None
    if not isinstance(annotations, dict):
        return False, False
    destructive = annotations.get("destructiveHint") is True
    read_only = annotations.get("readOnlyHint") is True and not destructive
    return read_only, destructive


@dataclass(frozen=True)
class McpToolBinding:
    server_id: str
    tool_name: str
    tool_alias: str
    description: str
    parameters: dict[str, Any]
    client: _McpSessionClient = field(repr=False)
    session_mode: str | None = None
    ephemeral_io: bool = False
    requested_result_summary: bool = False
    visual_delivery: McpVisualDelivery | None = field(default=None, repr=False, compare=False)
    # From the server's tool annotations; they decide whether a call asks for approval.
    read_only_hint: bool = False
    destructive_hint: bool = False

    def bind_session_mode(self, session_mode: str | None) -> McpToolBinding:
        normalized = str(session_mode or "").strip().lower() or None
        return replace(self, session_mode=normalized)

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.session_mode == "readonly":
            raise RuntimeError(
                f"Blocked in readonly mode: MCP tool '{self.tool_alias}' is not available."
            )
        result = self.client.call_tool(
            tool_name=self.tool_name,
            arguments=arguments,
        )
        content_items: list[dict[str, Any]] = []
        images: list[dict[str, Any]] = []
        for item in result.content:
            if item.get("type") == "image":
                # Preserve the existence of rejected/oversized images so the model
                # receives a failed visual receipt rather than silent omission.
                images.append(item)
                content_items.append({"type": "image", "summary": "image omitted"})
                continue
            payload_item = copy.deepcopy(item)
            item_text = payload_item.get("text")
            mime_type = payload_item.get("mime_type")
            if isinstance(item_text, str):
                payload_item["text"] = _wrap_tool_result_text(
                    server_id=self.server_id,
                    tool_name=self.tool_name,
                    text=item_text,
                    mime_type=mime_type if isinstance(mime_type, str) else None,
                )
            content_items.append(payload_item)
        payload: dict[str, Any] = {
            "server_id": self.server_id,
            "tool_alias": self.tool_alias,
            "tool_name": self.tool_name,
            "is_error": result.is_error,
            "content": content_items,
            "content_summary": result.content_summary,
        }
        if result.structured_content is not None:
            payload["structured_content"] = _normalize_prompt_structured_content(
                result.structured_content
            )
        # Each text block is already fenced in content, in its original order.
        # Repeating extracted_text here doubles escaped states and can offload a
        # small paginated response before the model can use its observed targets.
        if images:
            receipt = (
                self.visual_delivery.stage(images, source=self.tool_alias)
                if self.visual_delivery
                else {"visual_delivered": False, "visual_error": "visual_delivery_unavailable"}
            )
            payload.update(receipt)
            if receipt.get("visual_delivered"):
                for item in content_items:
                    if item.get("type") == "image":
                        item["summary"] = "image delivered separately"
        return payload


@dataclass(frozen=True)
class McpHostToolBinding:
    tool_name: str
    tool_alias: str
    description: str
    parameters: dict[str, Any]
    run_handler: Any = field(repr=False)
    session_mode: str | None = None

    def bind_session_mode(self, session_mode: str | None) -> McpHostToolBinding:
        normalized = str(session_mode or "").strip().lower() or None
        return replace(self, session_mode=normalized)

    def run(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if self.session_mode == "readonly":
            raise RuntimeError(
                f"Blocked in readonly mode: MCP tool '{self.tool_alias}' is not available."
            )
        return self.run_handler(arguments)


@dataclass(frozen=True)
class _CollectedServerCatalog:
    server: ResolvedMcpServer
    client: _McpSessionClient
    raw_tools: tuple[McpListedTool, ...]
    filtered_tools: tuple[McpListedTool, ...]
    raw_tool_names: tuple[str, ...]
    listed_resources: tuple[McpListedResource, ...]
    resources_snapshot_loaded: bool
    tools_list_changed: bool
    resources_list_changed: bool
    roots_capability_enabled: bool
    resources_capability_advertised: bool
    session_negotiated: bool


_AdmittedCatalog = tuple[_CollectedServerCatalog, list[tuple[McpListedTool, dict[str, Any]]]]


@dataclass(frozen=True)
class _FlatToolRecord:
    server: ResolvedMcpServer
    client: _McpSessionClient
    tool: McpListedTool
    parameters: dict[str, Any]

    @property
    def unique_key(self) -> str:
        return f"{self.server.id}\x00{self.tool.name}"

    @property
    def base_alias(self) -> str:
        return _tool_base_alias(self.server, self.tool.name)


@dataclass(frozen=True)
class _SnapshottedResourceRecord:
    server: ResolvedMcpServer
    client: _McpSessionClient
    resource: McpListedResource

    @property
    def lookup_key(self) -> tuple[str, str]:
        return (self.server.id, self.resource.uri)


@dataclass(frozen=True)
class _SnapshottedPromptRecord:
    server: ResolvedMcpServer
    client: _McpSessionClient
    prompt: McpListedPrompt

    @property
    def lookup_key(self) -> tuple[str, str]:
        return (self.server.id, self.prompt.name)


_McpManagerToolBinding = McpToolBinding | McpHostToolBinding


class McpManager:
    def __init__(
        self,
        *,
        resolved_config: ResolvedMcpConfig,
        workspace_root: Path,
        runtime_kind: RuntimeKind | str,
        session_id: str | None = None,
    ) -> None:
        self.resolved_config = resolved_config
        self.workspace_root = workspace_root.resolve()
        self.runtime_kind = normalize_runtime_kind(runtime_kind)
        self.session_id = str(session_id or "").strip() or None
        self._resolved_servers = tuple(resolved_config.servers)
        self._active_servers = resolved_config.active_servers_for(self.runtime_kind)
        self._resolved_servers_by_id = {server.id: server for server in self._resolved_servers}
        self._active_servers_by_id = {server.id: server for server in self._active_servers}
        self._live_runtime_enabled = _mcp_runtime_enabled(self.runtime_kind)
        self._closed = False
        self._lifecycle_lock = threading.RLock()
        self._disabled_server_ids: set[str] = set()
        self._client_leases_by_server_id: dict[str, list[_McpClientLease]] = {}
        self._snapshot_loaded = False
        self._snapshot_error: BaseException | None = None
        # Servers left out of the frozen catalog because they failed while it was built.
        self._server_startup_errors: dict[str, BaseException] = {}
        self._tool_bindings: tuple[_McpManagerToolBinding, ...] = ()
        self._resource_records: tuple[_SnapshottedResourceRecord, ...] = ()
        self._resource_lookup: dict[tuple[str, str], _SnapshottedResourceRecord] = {}
        self._resource_snapshotted_server_ids: set[str] = set()
        self._prompt_enabled_servers = tuple(
            server
            for server in self._active_servers
            if prompts_mode_enabled(
                prompts_mode=server.prompts_mode,
                runtime_kind=self.runtime_kind,
            )
        )
        self._prompt_enabled_servers_by_id = {
            server.id: server for server in self._prompt_enabled_servers
        }
        self._prompt_snapshot_loaded = not self._prompt_enabled_servers
        self._prompt_loaded_server_ids: set[str] = set()
        self._prompt_server_errors: dict[str, BaseException] = {}
        self._prompt_records: tuple[_SnapshottedPromptRecord, ...] = ()
        self._prompt_records_by_server_id: dict[str, tuple[_SnapshottedPromptRecord, ...]] = {}
        self._prompt_lookup: dict[tuple[str, str], _SnapshottedPromptRecord] = {}
        self._tool_stale_server_ids: set[str] = set()
        self._resource_stale_server_ids: set[str] = set()
        self._prompt_stale_server_ids: set[str] = set()
        self._clients_by_server_id: dict[str, _McpSessionClient] = {}
        self._server_instructions_by_id: dict[str, str] = {}
        self._prompt_clients_by_server_id: dict[str, _McpSessionClient] = {}
        self._prompt_server_catalogs_by_id = {
            server.id: self._initial_prompt_server_catalog(server)
            for server in self._prompt_enabled_servers
        }
        self._catalog_snapshot: dict[str, Any] = {
            "catalog_initialized": False,
            "live_tool_runtime_enabled": self._live_runtime_enabled,
            "active_server_ids": [server.id for server in self._active_servers],
            "server_catalogs": [],
            "exposed_tool_aliases": [],
            "exposed_tool_names": [],
            "exposed_tool_count": 0,
            "snapshotted_resource_count": 0,
            "resource_tool_names": [],
            "resource_tool_count": 0,
            "prompt_enabled_server_ids": [server.id for server in self._prompt_enabled_servers],
            "prompt_snapshotted_server_ids": [],
            "prompt_snapshot_complete": self._prompt_snapshot_loaded,
            "prompt_snapshot_partial": False,
            "tool_stale_server_ids": [],
            "resource_stale_server_ids": [],
            "prompt_stale_server_ids": [],
            "prompt_server_catalogs": [],
            "snapshotted_prompt_count": 0,
            "manual_prompt_surface_enabled": False,
            "failed_server_ids": [],
        }
        self._sync_prompt_catalog_snapshot_metadata()

    @property
    def resolved_servers(self) -> tuple[ResolvedMcpServer, ...]:
        return self._resolved_servers

    @property
    def active_servers(self) -> tuple[ResolvedMcpServer, ...]:
        return self._active_servers

    @property
    def tool_bindings(self) -> tuple[_McpManagerToolBinding, ...]:
        self._ensure_tool_snapshot()
        return self._tool_bindings

    def server_instructions_context(self) -> str | None:
        """Describe connected servers without treating their prose as authorization."""
        self._ensure_tool_snapshot()
        return server_instructions_context(
            {
                server_id: value
                for server_id, value in self._server_instructions_by_id.items()
                if server_id not in self._disabled_server_ids
            }
        )

    @property
    def closed(self) -> bool:
        return self._closed

    def server_startup_errors(self) -> dict[str, str]:
        """Servers that failed while the catalog was built, with the reason.

        Their tools are not in this session; the other servers' tools are.
        """
        return {server_id: str(exc) for server_id, exc in self._server_startup_errors.items()}

    def startup_metadata(self) -> dict[str, Any]:
        return {
            "config_present": self.resolved_config.has_any_config,
            "user_config_present": self.resolved_config.user_config_present,
            "project_config_present": self.resolved_config.project_config_present,
            "resolved_server_count": len(self._resolved_servers),
            "resolved_server_ids": [server.id for server in self._resolved_servers],
            "active_server_count": len(self._active_servers),
            "active_server_ids": [server.id for server in self._active_servers],
            "live_tool_runtime_enabled": self._live_runtime_enabled,
        }

    def _register_client_lease(
        self,
        *,
        server_id: str,
        client: _McpLiveClient,
    ) -> _McpClientLease:
        if self._client_leases_by_server_id.get(server_id):
            raise RuntimeError(
                f"MCP server '{server_id}' already has a session-owned client lease."
            )
        lease = _McpClientLease(server_id=server_id, client=client)
        self._client_leases_by_server_id[server_id] = [lease]
        return lease

    def _server_lifecycle_target(self, server_id: str) -> ResolvedMcpServer:
        normalized_server_id = str(server_id or "").strip()
        if not normalized_server_id:
            raise RuntimeError("server_id must be a non-empty string.")
        if self._closed:
            raise RuntimeError("MCP manager is closed.")
        server = self._active_servers_by_id.get(normalized_server_id)
        if server is not None:
            if server.trust != "explicit":
                raise RuntimeError(
                    f"MCP server '{normalized_server_id}' is not explicitly trusted."
                )
            if not self._live_runtime_enabled:
                raise RuntimeError(
                    f"MCP server lifecycle is unavailable for runtime '{self.runtime_kind.value}'."
                )
            return server
        if normalized_server_id in self._resolved_servers_by_id:
            raise RuntimeError(
                f"MCP server '{normalized_server_id}' is not active for runtime "
                f"'{self.runtime_kind.value}'."
            )
        raise RuntimeError(f"Unknown MCP server '{normalized_server_id}'.")

    def server_lifecycle_status(self, *, server_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            server = self._server_lifecycle_target(server_id)
            leases = tuple(self._client_leases_by_server_id.get(server.id, ()))
            enabled = server.id not in self._disabled_server_ids
            connected = bool(enabled and any(lease.connected for lease in leases))
            startup_failed = server.id in self._server_startup_errors
            if not enabled:
                connection_state = "disabled"
            elif connected:
                connection_state = "connected"
            elif startup_failed:
                connection_state = "failed"
            elif leases:
                connection_state = "disconnected"
            else:
                connection_state = "not_materialized"
            server_bindings = [
                binding
                for binding in self._tool_bindings
                if isinstance(binding, McpToolBinding) and binding.server_id == server.id
            ]
            resource_count = sum(
                1 for record in self._resource_records if record.server.id == server.id
            )
            prompt_count = len(self._prompt_records_by_server_id.get(server.id, ()))
            return {
                "session_id": self.session_id,
                "server_id": server.id,
                "transport": server.transport,
                "enabled": enabled,
                "connection_state": connection_state,
                "connected": connected,
                "startup_failed": startup_failed,
                "generation": max((lease.generation for lease in leases), default=0),
                "catalog_initialized": self._snapshot_loaded,
                "exposed_tool_count": len(server_bindings),
                "snapshotted_resource_count": resource_count,
                "prompt_snapshot_loaded": server.id in self._prompt_loaded_server_ids,
                "snapshotted_prompt_count": prompt_count,
                "secret_values_included": False,
            }

    def disable_server(self, *, server_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            server = self._server_lifecycle_target(server_id)
            already_disabled = server.id in self._disabled_server_ids
            self._disabled_server_ids.add(server.id)
            changed = not already_disabled
            for lease in self._client_leases_by_server_id.get(server.id, ()):
                changed = lease.disable() or changed
            return {
                **self.server_lifecycle_status(server_id=server.id),
                "changed": changed,
                "action": "disable",
            }

    def _replacement_tool_signature(
        self,
        *,
        server: ResolvedMcpServer,
        client: _McpLiveClient,
    ) -> list[tuple[str, str, dict[str, Any]]]:
        if not client.supports_tools:
            if server.allowed_tools or server.denied_tools:
                raise RuntimeError(
                    f"MCP server '{server.id}' no longer advertises its configured tools."
                )
            return []
        raw_tools = client.list_tools()
        filtered_tools, _raw_names = self._filter_server_tools(
            server=server,
            raw_tools=raw_tools,
        )
        return [
            (
                tool.name,
                build_host_owned_mcp_tool_description(
                    server_id=server.id,
                    tool_name=tool.name,
                    server_description=tool.description,
                    host_guidance=legacy_windows_tool_guidance(server.id, tool.name),
                    description_max_chars=legacy_windows_description_max_chars(
                        server.id, tool.name
                    ),
                ),
                _validate_function_tool_schema(server=server, tool=tool),
            )
            for tool in filtered_tools
        ]

    def _validate_replacement_client(
        self,
        *,
        server: ResolvedMcpServer,
        client: _McpLiveClient,
        validate_prompts: bool = True,
    ) -> None:
        client.ensure_initialized()
        if self._snapshot_loaded:
            if (
                getattr(client, "server_instructions", None) or ""
            ).strip() != self._server_instructions_by_id.get(server.id, ""):
                raise RuntimeError(
                    f"MCP server '{server.id}' usage instructions changed. Recreate the "
                    "session before using the new instructions."
                )
            expected_tools = [
                (binding.tool_name, binding.description, binding.parameters)
                for binding in self._tool_bindings
                if isinstance(binding, McpToolBinding) and binding.server_id == server.id
            ]
            replacement_tools = self._replacement_tool_signature(server=server, client=client)
            if replacement_tools != expected_tools:
                raise RuntimeError(
                    f"MCP server '{server.id}' tool catalog changed. Recreate the IDE session "
                    "before using the new catalog."
                )
        if server.id in self._resource_snapshotted_server_ids:
            replacement_resources = (
                list(client.list_resources()) if client.supports_resources else []
            )
            expected_resources = [
                record.resource
                for record in self._resource_records
                if record.server.id == server.id
            ]
            if replacement_resources != expected_resources:
                raise RuntimeError(
                    f"MCP server '{server.id}' resource catalog changed. Recreate the IDE "
                    "session before using the new catalog."
                )
        if validate_prompts and server.id in self._prompt_loaded_server_ids:
            replacement_prompts = list(client.list_prompts()) if client.supports_prompts else []
            expected_prompts = [
                record.prompt for record in self._prompt_records_by_server_id.get(server.id, ())
            ]
            if replacement_prompts != expected_prompts:
                raise RuntimeError(
                    f"MCP server '{server.id}' prompt catalog changed. Recreate the IDE session "
                    "before using the new catalog."
                )
        # A list_changed notification that races any comparison means the
        # replacement was already stale before it could become session-owned.
        # Never swap such a connection under frozen model-facing bindings.
        client.observe_notifications()
        changed_surfaces: list[str] = []
        if self._snapshot_loaded and client.tools_list_changed:
            changed_surfaces.append("tool")
        if server.id in self._resource_snapshotted_server_ids and client.resources_list_changed:
            changed_surfaces.append("resource")
        if (
            validate_prompts
            and server.id in self._prompt_loaded_server_ids
            and client.prompts_list_changed
        ):
            changed_surfaces.append("prompt")
        if changed_surfaces:
            surfaces = ", ".join(changed_surfaces)
            raise RuntimeError(
                f"MCP server '{server.id}' reported a {surfaces} catalog change while "
                "reconnecting. Recreate the IDE session before using the new catalog."
            )

    def _connect_server(self, *, server_id: str, action: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            server = self._server_lifecycle_target(server_id)
            if server.id in self._server_startup_errors:
                # The session's tool list was fixed without this server's tools.
                raise RuntimeError(
                    f"MCP server '{server.id}' did not start when this session began. "
                    "Start a new session to load its tools."
                )
            if action == "enable" and server.id not in self._disabled_server_ids:
                status = self.server_lifecycle_status(server_id=server.id)
                if status["connected"]:
                    return {**status, "changed": False, "action": action}
            client = self._build_live_client(server)
            try:
                self._validate_replacement_client(server=server, client=client)
                leases = self._client_leases_by_server_id.get(server.id, [])
                if leases:
                    leases[0].replace_client(client)
                    for redundant_lease in leases[1:]:
                        redundant_lease.disable()
                    self._client_leases_by_server_id[server.id] = [leases[0]]
                    live_client: _McpSessionClient = leases[0]
                else:
                    live_client = self._register_client_lease(
                        server_id=server.id,
                        client=client,
                    )
                if server.id in self._clients_by_server_id:
                    self._clients_by_server_id[server.id] = live_client
                elif server.id in self._prompt_clients_by_server_id:
                    self._prompt_clients_by_server_id[server.id] = live_client
                else:
                    # Keep a configured-but-empty server connected to this live manager so
                    # status and later prompt materialization operate on the owned connection.
                    self._clients_by_server_id[server.id] = live_client
                if server.id in self._prompt_clients_by_server_id:
                    self._prompt_clients_by_server_id[server.id] = live_client
                self._disabled_server_ids.discard(server.id)
                # The replacement has just been compared with every frozen
                # surface above. Clear stale state inherited from the closed
                # transport so status reflects the validated connection.
                self._tool_stale_server_ids.discard(server.id)
                self._resource_stale_server_ids.discard(server.id)
                self._prompt_stale_server_ids.discard(server.id)
                self._sync_stale_catalog_snapshot_metadata()
            except Exception:
                client.close()
                raise
            return {
                **self.server_lifecycle_status(server_id=server.id),
                "changed": True,
                "action": action,
            }

    def enable_server(self, *, server_id: str) -> dict[str, Any]:
        return self._connect_server(server_id=server_id, action="enable")

    def restart_server(self, *, server_id: str) -> dict[str, Any]:
        return self._connect_server(server_id=server_id, action="restart")

    def _refresh_stale_state_from_clients(self) -> None:
        for server_id, client in self._clients_by_server_id.items():
            if isinstance(client, _McpClientLease) and not client.enabled:
                continue
            client.observe_notifications()
            if client.tools_list_changed:
                self._tool_stale_server_ids.add(server_id)
            if client.resources_list_changed and server_id in self._resource_snapshotted_server_ids:
                self._resource_stale_server_ids.add(server_id)
        for server_id, client in self._prompt_clients_by_server_id.items():
            if isinstance(client, _McpClientLease) and not client.enabled:
                continue
            client.observe_notifications()
            if client.prompts_list_changed and server_id in self._prompt_loaded_server_ids:
                self._prompt_stale_server_ids.add(server_id)

    def _sync_stale_catalog_snapshot_metadata(self) -> None:
        self._catalog_snapshot["tool_stale_server_ids"] = sorted(self._tool_stale_server_ids)
        self._catalog_snapshot["resource_stale_server_ids"] = sorted(
            self._resource_stale_server_ids
        )
        self._catalog_snapshot["prompt_stale_server_ids"] = sorted(self._prompt_stale_server_ids)

    def catalog_snapshot_metadata(self) -> dict[str, Any]:
        self._refresh_stale_state_from_clients()
        self._sync_stale_catalog_snapshot_metadata()
        snapshot = copy.deepcopy(self._catalog_snapshot)
        server_catalogs = snapshot.get("server_catalogs")
        if isinstance(server_catalogs, list):
            for entry in server_catalogs:
                if not isinstance(entry, dict):
                    continue
                server_id = str(entry.get("server_id") or "").strip()
                client = self._clients_by_server_id.get(server_id)
                if client is not None and not (
                    isinstance(client, _McpClientLease) and not client.enabled
                ):
                    entry["tools_list_changed"] = client.tools_list_changed
                    entry["resources_list_changed"] = client.resources_list_changed
                    entry["tools_snapshot_stale"] = server_id in self._tool_stale_server_ids
                    entry["resources_snapshot_stale"] = server_id in self._resource_stale_server_ids
                    if entry.get("transport") == "http":
                        entry["session_negotiated"] = client.session_negotiated
        prompt_server_catalogs = snapshot.get("prompt_server_catalogs")
        if isinstance(prompt_server_catalogs, list):
            for entry in prompt_server_catalogs:
                if not isinstance(entry, dict):
                    continue
                server_id = str(entry.get("server_id") or "").strip()
                client = self._prompt_clients_by_server_id.get(server_id)
                if server_id in self._prompt_stale_server_ids:
                    entry["prompt_snapshot_stale"] = True
                else:
                    entry.pop("prompt_snapshot_stale", None)
                if client is not None and not (
                    isinstance(client, _McpClientLease) and not client.enabled
                ):
                    if client.prompts_list_changed:
                        entry["prompts_list_changed"] = True
                    else:
                        entry.pop("prompts_list_changed", None)
                    if entry.get("transport") == "http":
                        entry["session_negotiated"] = client.session_negotiated
        return snapshot

    def execution_context_summary(self) -> dict[str, Any]:
        self._ensure_tool_snapshot()
        snapshot = self.catalog_snapshot_metadata()
        servers: list[dict[str, Any]] = []
        for raw_entry in snapshot.get("server_catalogs") or []:
            if not isinstance(raw_entry, dict):
                continue
            server_id = str(raw_entry.get("server_id") or "").strip()
            if not server_id:
                continue
            tool_names = [
                str(name).strip()
                for name in raw_entry.get("exposed_tool_names") or []
                if str(name).strip()
            ]
            servers.append(
                {
                    "server_id": server_id,
                    "tool_names": tool_names,
                    "resources_available": bool(raw_entry.get("snapshotted_resource_count", 0)),
                }
            )
        return {
            "active_server_ids": [server.id for server in self._active_servers],
            "servers": servers,
        }

    def scope_for_forge_task(
        self,
        *,
        task_scope: ForgeTaskMcpScope | None,
    ) -> ForgeTaskScopedMcpManager:
        if task_scope is None or task_scope.is_empty:
            return ForgeTaskScopedMcpManager.without_live_bootstrap(
                resolved_config=self.resolved_config,
                workspace_root=self.workspace_root,
                runtime_kind=self.runtime_kind,
                task_scope=task_scope,
                close_delegate=self,
            )
        return ForgeTaskScopedMcpManager(base_manager=self, task_scope=task_scope)

    def _resource_list_tool_parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "server_id": {
                    "type": "string",
                    "description": "Optional exact MCP server id filter.",
                },
                "query": {
                    "type": "string",
                    "description": "Optional case-insensitive filter over resource uri, name, or description.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": _MAX_MCP_RESOURCE_LIST_LIMIT,
                    "description": (
                        f"Maximum resources to return. Defaults to {_DEFAULT_MCP_RESOURCE_LIST_LIMIT}."
                    ),
                },
            },
            "required": [],
            "additionalProperties": False,
        }

    def _resource_read_tool_parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "server_id": {
                    "type": "string",
                    "description": "Exact MCP server id from mcp_resources_list.",
                },
                "uri": {
                    "type": "string",
                    "description": "Exact snapshotted resource URI from mcp_resources_list.",
                },
            },
            "required": ["server_id", "uri"],
            "additionalProperties": False,
        }

    def _normalize_resource_list_limit(self, value: Any) -> int:
        if value is None:
            return _DEFAULT_MCP_RESOURCE_LIST_LIMIT
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError("mcp_resources_list limit must be an integer.")
        if value < 1 or value > _MAX_MCP_RESOURCE_LIST_LIMIT:
            raise RuntimeError(
                f"mcp_resources_list limit must be between 1 and {_MAX_MCP_RESOURCE_LIST_LIMIT}."
            )
        return value

    def _normalize_required_argument(self, arguments: dict[str, Any], *, field_name: str) -> str:
        value = arguments.get(field_name)
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError(f"{field_name} must be a non-empty string.")
        return value.strip()

    def _list_snapshotted_resources(self, arguments: dict[str, Any]) -> dict[str, Any]:
        server_id_value = arguments.get("server_id")
        if server_id_value is None:
            server_id = None
        elif isinstance(server_id_value, str) and server_id_value.strip():
            server_id = server_id_value.strip()
        else:
            raise RuntimeError("server_id must be a non-empty string when present.")
        query_value = arguments.get("query")
        if query_value is None:
            query = None
        elif isinstance(query_value, str):
            query = query_value.strip().casefold() or None
        else:
            raise RuntimeError("query must be a string when present.")
        limit = self._normalize_resource_list_limit(arguments.get("limit"))
        matching_resources: list[dict[str, Any]] = []
        for record in self._resource_records:
            if server_id is not None and record.server.id != server_id:
                continue
            if query is not None:
                haystack = " ".join(
                    part
                    for part in (
                        record.server.id,
                        record.resource.uri,
                        record.resource.name,
                        record.resource.description or "",
                    )
                    if part
                ).casefold()
                if query not in haystack:
                    continue
            matching_resources.append(record.resource.as_tool_payload(server_id=record.server.id))
        returned_resources = matching_resources[:limit]
        return {
            "resources": returned_resources,
            "returned_count": len(returned_resources),
            "matching_count": len(matching_resources),
            "total_snapshot_count": len(self._resource_records),
        }

    def _read_snapshotted_resource(self, arguments: dict[str, Any]) -> dict[str, Any]:
        server_id = self._normalize_required_argument(arguments, field_name="server_id")
        uri = self._normalize_required_argument(arguments, field_name="uri")
        record = self._resource_lookup.get((server_id, uri))
        if record is None:
            raise RuntimeError(
                "Blocked: MCP resource reads are limited to the frozen session snapshot; "
                f"no snapshotted resource matches server '{server_id}' and uri '{uri}'."
            )
        result = record.client.read_resource(resource_uri=uri, allowed_uris=frozenset({uri}))
        payload: dict[str, Any] = {
            "server_id": record.server.id,
            "uri": record.resource.uri,
            "name": record.resource.name,
            "content_summary": result.content_summary,
            "contents": [
                item.as_tool_payload(server_id=record.server.id, resource_uri=record.resource.uri)
                for item in result.contents
            ],
        }
        mime_type = result.mime_type or record.resource.mime_type
        if mime_type:
            payload["mime_type"] = mime_type
        if record.resource.description:
            payload["description"] = record.resource.description
        if record.resource.size is not None:
            payload["size"] = record.resource.size
        if any(item.text is not None for item in result.contents):
            payload["text"] = build_untrusted_mcp_text_block(
                source_type="resource_read",
                server_id=record.server.id,
                source_name=record.resource.uri,
                text=result.text,
                mime_type=mime_type,
            )
        return payload

    def _build_resource_tool_bindings(self) -> tuple[McpHostToolBinding, ...]:
        if not self._resource_records:
            return ()
        return (
            McpHostToolBinding(
                tool_name="mcp_resources_list",
                tool_alias="mcp_resources_list",
                description=(
                    "List read-only MCP resources from the frozen session snapshot across active "
                    "servers."
                ),
                parameters=self._resource_list_tool_parameters(),
                run_handler=self._list_snapshotted_resources,
            ),
            McpHostToolBinding(
                tool_name="mcp_resource_read",
                tool_alias="mcp_resource_read",
                description=(
                    "Read one frozen-snapshot MCP resource by server_id and uri. Arbitrary "
                    "unsnapshotted URI reads are blocked."
                ),
                parameters=self._resource_read_tool_parameters(),
                run_handler=self._read_snapshotted_resource,
            ),
        )

    def _normalize_prompt_list_limit(self, value: Any) -> int:
        if value is None:
            return _DEFAULT_MCP_PROMPT_LIST_LIMIT
        if isinstance(value, bool) or not isinstance(value, int):
            raise RuntimeError("prompt list limit must be an integer.")
        if value < 1 or value > _MAX_MCP_PROMPT_LIST_LIMIT:
            raise RuntimeError(
                f"prompt list limit must be between 1 and {_MAX_MCP_PROMPT_LIST_LIMIT}."
            )
        return value

    def _close_prompt_clients(self) -> None:
        clients = list(self._prompt_clients_by_server_id.values())
        self._prompt_clients_by_server_id = {}
        for client in clients:
            if client in self._clients_by_server_id.values():
                continue
            client.close()

    def _initial_prompt_server_catalog(self, server: ResolvedMcpServer) -> dict[str, Any]:
        return {
            "server_id": server.id,
            "transport": server.transport,
            "prompts_mode": server.prompts_mode,
            "prompt_snapshot_loaded": False,
            "prompt_snapshot_failed": False,
            "snapshotted_prompt_count": 0,
        }

    def _sync_prompt_catalog_snapshot_metadata(self) -> None:
        prompt_enabled_server_ids = [server.id for server in self._prompt_enabled_servers]
        prompt_snapshotted_server_ids = [
            server.id
            for server in self._prompt_enabled_servers
            if server.id in self._prompt_loaded_server_ids
        ]
        prompt_snapshot_complete = len(prompt_snapshotted_server_ids) == len(
            prompt_enabled_server_ids
        )
        prompt_server_catalogs = [
            copy.deepcopy(self._prompt_server_catalogs_by_id[server.id])
            for server in self._prompt_enabled_servers
        ]
        self._catalog_snapshot["prompt_enabled_server_ids"] = prompt_enabled_server_ids
        self._catalog_snapshot["prompt_snapshotted_server_ids"] = prompt_snapshotted_server_ids
        self._catalog_snapshot["prompt_snapshot_complete"] = prompt_snapshot_complete
        self._catalog_snapshot["prompt_snapshot_partial"] = bool(
            prompt_snapshotted_server_ids
        ) and (not prompt_snapshot_complete)
        self._catalog_snapshot["prompt_server_catalogs"] = prompt_server_catalogs
        snapshotted_prompt_count = sum(
            len(records) for records in self._prompt_records_by_server_id.values()
        )
        self._catalog_snapshot["snapshotted_prompt_count"] = snapshotted_prompt_count
        self._catalog_snapshot["manual_prompt_surface_enabled"] = snapshotted_prompt_count > 0
        self._sync_stale_catalog_snapshot_metadata()

    def _iter_ordered_prompt_records(
        self,
        *,
        server_id: str | None = None,
    ) -> tuple[_SnapshottedPromptRecord, ...]:
        if server_id is not None:
            return self._prompt_records_by_server_id.get(server_id, ())
        ordered_records: list[_SnapshottedPromptRecord] = []
        for server in self._prompt_enabled_servers:
            ordered_records.extend(self._prompt_records_by_server_id.get(server.id, ()))
        return tuple(ordered_records)

    def _prompt_target_server(self, *, server_id: str) -> ResolvedMcpServer:
        normalized_server_id = str(server_id or "").strip()
        if not normalized_server_id:
            raise RuntimeError("server_id must be a non-empty string.")
        prompt_server = self._prompt_enabled_servers_by_id.get(normalized_server_id)
        if prompt_server is not None:
            return prompt_server
        active_server = self._active_servers_by_id.get(normalized_server_id)
        if active_server is not None:
            raise RuntimeError(
                f"MCP server '{normalized_server_id}' does not have prompts enabled for runtime "
                f"'{self.runtime_kind.value}'."
            )
        if normalized_server_id in self._resolved_servers_by_id:
            raise RuntimeError(
                f"MCP server '{normalized_server_id}' is not active for runtime "
                f"'{self.runtime_kind.value}'."
            )
        raise RuntimeError(f"Unknown MCP server '{normalized_server_id}'.")

    def _raise_prompt_server_error(self, *, server_id: str) -> None:
        error = self._prompt_server_errors.get(server_id)
        if error is None:
            return
        if isinstance(error, Exception):
            raise error
        raise RuntimeError(str(error))

    def _drop_prompt_server_snapshot(self, server_id: str) -> None:
        old_records = self._prompt_records_by_server_id.pop(server_id, ())
        if old_records:
            old_lookup_keys = {record.lookup_key for record in old_records}
            self._prompt_records = tuple(
                record
                for record in self._prompt_records
                if record.lookup_key not in old_lookup_keys
            )
            for lookup_key in old_lookup_keys:
                self._prompt_lookup.pop(lookup_key, None)
        old_client = self._prompt_clients_by_server_id.pop(server_id, None)
        if old_client is not None and old_client not in self._clients_by_server_id.values():
            old_client.close()
        self._prompt_loaded_server_ids.discard(server_id)
        self._prompt_server_errors.pop(server_id, None)

    def _ensure_prompt_server_snapshot(
        self,
        server: ResolvedMcpServer,
        *,
        refresh: bool = False,
    ) -> None:
        if self._closed:
            raise RuntimeError("MCP manager is closed.")
        if server.id in self._disabled_server_ids:
            raise RuntimeError(f"MCP server '{server.id}' is disabled for this session.")
        if refresh:
            self._prompt_server_errors.pop(server.id, None)
        if server.id in self._prompt_loaded_server_ids and not refresh:
            return
        if server.id in self._prompt_server_errors:
            self._raise_prompt_server_error(server_id=server.id)
        replacing_loaded_snapshot = refresh and server.id in self._prompt_loaded_server_ids
        replaced_prompt_count = (
            len(self._prompt_records_by_server_id.get(server.id, ()))
            if replacing_loaded_snapshot
            else 0
        )
        if server.trust != "explicit":
            error = ConfigError(
                f"MCP server '{server.id}' uses unsupported trust mode '{server.trust}'. "
                "Live MCP prompt access currently requires trust='explicit'."
            )
            self._prompt_server_errors[server.id] = error
            self._prompt_server_catalogs_by_id[server.id]["prompt_snapshot_failed"] = True
            self._sync_prompt_catalog_snapshot_metadata()
            raise error
        existing_client = self._clients_by_server_id.get(server.id)
        raw_client: _McpLiveClient | None = None
        client: _McpSessionClient
        if existing_client is not None and not refresh:
            client = existing_client
        else:
            raw_client = self._build_live_client(server)
            client = raw_client
        try:
            client.ensure_initialized()
            listed_prompts: tuple[McpListedPrompt, ...] = ()
            if client.supports_prompts:
                listed_prompts = client.list_prompts()
            prompt_count = len(listed_prompts)
            if prompt_count > _MAX_LISTED_MCP_PROMPTS_PER_SERVER:
                raise ConfigError(
                    "MCP prompt exposure is too large for server "
                    f"'{server.id}' ({prompt_count} prompts). Narrow server-side prompt "
                    "exposure or disable prompts_mode."
                )
            total_prompts = len(self._prompt_records) - replaced_prompt_count + prompt_count
            if total_prompts > _MAX_LISTED_MCP_PROMPTS_TOTAL:
                raise ConfigError(
                    "MCP prompt exposure is too large for one session "
                    f"({total_prompts} prompts). Narrow server-side prompt exposure or "
                    "disable prompts_mode."
                )
            if raw_client is not None and isinstance(existing_client, _McpClientLease):
                self._validate_replacement_client(
                    server=server,
                    client=raw_client,
                    validate_prompts=False,
                )
                existing_client.replace_client(raw_client)
                client = existing_client
                raw_client = None
            elif raw_client is not None:
                client = self._register_client_lease(server_id=server.id, client=raw_client)
                raw_client = None
            self._clients_by_server_id[server.id] = client
            new_records = tuple(
                _SnapshottedPromptRecord(
                    server=server,
                    client=client,
                    prompt=prompt,
                )
                for prompt in listed_prompts
            )
            if refresh:
                self._drop_prompt_server_snapshot(server.id)
            self._prompt_records_by_server_id[server.id] = new_records
            if new_records:
                self._prompt_records = self._prompt_records + new_records
                for record in new_records:
                    self._prompt_lookup[record.lookup_key] = record
                self._prompt_clients_by_server_id[server.id] = client
            self._prompt_loaded_server_ids.add(server.id)
            self._prompt_snapshot_loaded = len(self._prompt_loaded_server_ids) == len(
                self._prompt_enabled_servers
            )
            catalog_entry = self._prompt_server_catalogs_by_id[server.id]
            catalog_entry["prompt_snapshot_loaded"] = True
            catalog_entry["prompt_snapshot_failed"] = False
            if client.prompts_list_changed:
                catalog_entry["prompts_list_changed"] = True
                self._prompt_stale_server_ids.add(server.id)
            else:
                catalog_entry.pop("prompts_list_changed", None)
                self._prompt_stale_server_ids.discard(server.id)
            if server.id in self._prompt_stale_server_ids:
                catalog_entry["prompt_snapshot_stale"] = True
            else:
                catalog_entry.pop("prompt_snapshot_stale", None)
            catalog_entry["prompts_capability_advertised"] = client.supports_prompts
            catalog_entry["snapshotted_prompt_count"] = prompt_count
            self._sync_prompt_catalog_snapshot_metadata()
        except Exception as exc:
            if raw_client is not None:
                raw_client.close()
            self._prompt_server_errors[server.id] = exc
            catalog_entry = self._prompt_server_catalogs_by_id[server.id]
            if not replacing_loaded_snapshot:
                catalog_entry["prompt_snapshot_failed"] = True
                catalog_entry["prompt_snapshot_loaded"] = False
                catalog_entry.pop("prompts_list_changed", None)
                catalog_entry.pop("prompts_capability_advertised", None)
                catalog_entry["snapshotted_prompt_count"] = 0
                self._prompt_snapshot_loaded = False
            if server.id in self._prompt_stale_server_ids:
                catalog_entry["prompt_snapshot_stale"] = True
            else:
                catalog_entry.pop("prompt_snapshot_stale", None)
            self._sync_prompt_catalog_snapshot_metadata()
            raise

    def _ensure_prompt_snapshot(
        self,
        *,
        server_id: str | None = None,
        refresh: bool = False,
    ) -> None:
        if server_id is None:
            if self._prompt_snapshot_loaded and not refresh:
                return
            for server in self._prompt_enabled_servers:
                self._ensure_prompt_server_snapshot(server, refresh=refresh)
            self._prompt_snapshot_loaded = True
            self._sync_prompt_catalog_snapshot_metadata()
            return
        self._ensure_prompt_server_snapshot(
            self._prompt_target_server(server_id=server_id),
            refresh=refresh,
        )

    def list_prompts(
        self,
        *,
        server_id: str | None = None,
        query: str | None = None,
        limit: int = _DEFAULT_MCP_PROMPT_LIST_LIMIT,
        refresh: bool = False,
    ) -> dict[str, Any]:
        if server_id is None:
            normalized_server_id = None
        else:
            normalized_server_id = str(server_id).strip()
            if not normalized_server_id:
                raise RuntimeError("server_id must be a non-empty string when present.")
        self._ensure_prompt_snapshot(server_id=normalized_server_id, refresh=refresh)
        self._refresh_stale_state_from_clients()
        self._sync_stale_catalog_snapshot_metadata()
        normalized_query = str(query or "").strip().casefold() or None
        normalized_limit = self._normalize_prompt_list_limit(limit)
        matching_prompts: list[dict[str, Any]] = []
        for record in self._iter_ordered_prompt_records(server_id=normalized_server_id):
            if normalized_query is not None:
                haystack = " ".join(
                    part
                    for part in (
                        record.server.id,
                        record.prompt.name,
                        record.prompt.title or "",
                        record.prompt.description or "",
                        " ".join(argument.name for argument in record.prompt.arguments),
                    )
                    if part
                ).casefold()
                if normalized_query not in haystack:
                    continue
            matching_prompts.append(record.prompt.as_payload(server_id=record.server.id))
        returned_prompts = matching_prompts[:normalized_limit]
        payload: dict[str, Any] = {
            "prompts": returned_prompts,
            "returned_count": len(returned_prompts),
            "matching_count": len(matching_prompts),
            "total_snapshot_count": len(self._prompt_records),
        }
        if refresh:
            payload["refresh_performed"] = True
        if self._prompt_stale_server_ids:
            payload["stale_server_ids"] = sorted(self._prompt_stale_server_ids)
            payload["snapshot_complete"] = bool(
                self._catalog_snapshot.get("prompt_snapshot_complete")
            )
            payload["snapshot_partial"] = bool(
                self._catalog_snapshot.get("prompt_snapshot_partial")
            )
        return payload

    def get_prompt(
        self,
        *,
        server_id: str,
        prompt_name: str,
        arguments: dict[str, str] | None = None,
        refresh: bool = False,
    ) -> dict[str, Any]:
        normalized_server_id = str(server_id or "").strip()
        normalized_prompt_name = str(prompt_name or "").strip()
        if not normalized_prompt_name:
            raise RuntimeError("prompt_name must be a non-empty string.")
        if arguments is not None and not isinstance(arguments, dict):
            raise RuntimeError("arguments must be an object mapping strings to strings.")
        self._ensure_prompt_snapshot(server_id=normalized_server_id, refresh=refresh)
        self._refresh_stale_state_from_clients()
        self._sync_stale_catalog_snapshot_metadata()
        record = self._prompt_lookup.get((normalized_server_id, normalized_prompt_name))
        if record is None:
            raise RuntimeError(
                "Blocked: MCP prompt fetches are limited to the frozen session snapshot; "
                f"no snapshotted prompt matches server '{normalized_server_id}' and prompt "
                f"{normalized_prompt_name!r}."
            )
        result = record.client.get_prompt(name=record.prompt.name, arguments=arguments)
        payload: dict[str, Any] = {
            "server_id": record.server.id,
            "name": record.prompt.name,
            "arguments": [argument.as_payload() for argument in record.prompt.arguments],
            "message_count": len(result.messages),
            "content_summary": result.content_summary,
            "messages": [
                message.as_payload(server_id=record.server.id, prompt_name=record.prompt.name)
                for message in result.messages
            ],
        }
        if refresh:
            payload["refresh_performed"] = True
        if normalized_server_id in self._prompt_stale_server_ids:
            payload["snapshot_stale"] = True
        if record.prompt.title:
            payload["title"] = record.prompt.title
        description = result.description or record.prompt.description
        if description:
            payload["description"] = description
        if arguments:
            payload["applied_arguments"] = {
                str(key): str(value) for key, value in arguments.items()
            }
        if result.text:
            payload["text"] = build_untrusted_mcp_text_block(
                source_type="prompt_get",
                server_id=record.server.id,
                source_name=record.prompt.name,
                text=result.text,
            )
        return payload

    def _ensure_tool_snapshot(self) -> None:
        if self._snapshot_error is not None:
            if isinstance(self._snapshot_error, Exception):
                raise self._snapshot_error
            raise RuntimeError(str(self._snapshot_error))
        if self._snapshot_loaded:
            return
        if self._closed:
            raise RuntimeError("MCP manager is closed.")
        self._snapshot_loaded = True
        if not self._live_runtime_enabled:
            self._catalog_snapshot["catalog_initialized"] = True
            return
        collected_catalogs: tuple[_CollectedServerCatalog, ...] = ()
        try:
            admitted = self._admit_server_catalogs(self._collect_server_catalogs())
            collected_catalogs = tuple(catalog for catalog, _tools in admitted)
            self._server_instructions_by_id = {
                catalog.server.id: instructions.strip()
                for catalog in collected_catalogs
                if isinstance(
                    instructions := getattr(catalog.client, "server_instructions", None), str
                )
                and instructions.strip()
            }
            admitted_tools = {catalog.server.id: tools for catalog, tools in admitted}
            leased_catalogs: list[_CollectedServerCatalog] = []
            for catalog in collected_catalogs:
                if isinstance(catalog.client, _McpClientLease):
                    leased_catalogs.append(catalog)
                    continue
                lease = self._register_client_lease(
                    server_id=catalog.server.id,
                    client=catalog.client,
                )
                if catalog.server.id in self._disabled_server_ids:
                    # A disable issued before first tool materialization must
                    # remain fail-closed after the frozen catalog is built.
                    lease.disable()
                leased_catalogs.append(replace(catalog, client=lease))
            collected_catalogs = tuple(leased_catalogs)
            flat_records: list[_FlatToolRecord] = []
            resource_records: list[_SnapshottedResourceRecord] = []
            for catalog in collected_catalogs:
                for tool, parameters in admitted_tools[catalog.server.id]:
                    flat_records.append(
                        _FlatToolRecord(
                            server=catalog.server,
                            client=catalog.client,
                            tool=tool,
                            parameters=parameters,
                        )
                    )
                for resource in catalog.listed_resources:
                    resource_records.append(
                        _SnapshottedResourceRecord(
                            server=catalog.server,
                            client=catalog.client,
                            resource=resource,
                        )
                    )
            alias_map = self._assign_aliases(flat_records)
            bindings: list[_McpManagerToolBinding] = []
            server_catalogs: list[dict[str, Any]] = []
            clients_with_bindings: dict[str, _McpSessionClient] = {}
            for catalog in collected_catalogs:
                exposed_aliases: list[str] = []
                exposed_tool_names: list[str] = []
                for record in flat_records:
                    if record.server.id != catalog.server.id:
                        continue
                    tool_alias = alias_map[record.unique_key]
                    exposed_aliases.append(tool_alias)
                    exposed_tool_names.append(record.tool.name)
                    clients_with_bindings[catalog.server.id] = catalog.client
                    read_only_hint, destructive_hint = mcp_tool_approval_hints(
                        record.tool.raw_payload
                    )
                    bindings.append(
                        McpToolBinding(
                            server_id=catalog.server.id,
                            tool_name=record.tool.name,
                            tool_alias=tool_alias,
                            description=build_host_owned_mcp_tool_description(
                                server_id=catalog.server.id,
                                tool_name=record.tool.name,
                                server_description=record.tool.description,
                                host_guidance=legacy_windows_tool_guidance(
                                    catalog.server.id, record.tool.name
                                ),
                                description_max_chars=(
                                    legacy_windows_description_max_chars(
                                        catalog.server.id, record.tool.name
                                    )
                                ),
                            ),
                            parameters=copy.deepcopy(record.parameters),
                            client=catalog.client,
                            ephemeral_io=ephemeral_tool_io(record.tool.raw_payload),
                            requested_result_summary=requested_result_summary(
                                record.tool.raw_payload
                            ),
                            read_only_hint=read_only_hint,
                            destructive_hint=destructive_hint,
                        )
                    )
                server_catalogs.append(
                    {
                        "server_id": catalog.server.id,
                        "transport": catalog.server.transport,
                        "roots_mode": catalog.server.roots_mode,
                        "roots_capability_enabled": catalog.roots_capability_enabled,
                        "resources_mode": catalog.server.resources_mode,
                        "resources_capability_advertised": (
                            catalog.resources_capability_advertised
                        ),
                        "resources_snapshot_loaded": catalog.resources_snapshot_loaded,
                        "snapshotted_resource_count": len(catalog.listed_resources),
                        "resources_list_changed": catalog.resources_list_changed,
                        "resources_snapshot_stale": (
                            catalog.resources_list_changed and catalog.resources_snapshot_loaded
                        ),
                        "raw_tool_names": list(catalog.raw_tool_names),
                        "raw_tool_count": len(catalog.raw_tool_names),
                        "exposed_tool_names": exposed_tool_names,
                        "exposed_tool_aliases": exposed_aliases,
                        "exposed_tool_count": len(exposed_tool_names),
                        "tools_list_changed": catalog.tools_list_changed,
                        "tools_snapshot_stale": catalog.tools_list_changed,
                        **(
                            {"session_negotiated": catalog.session_negotiated}
                            if catalog.server.transport == "http"
                            else {}
                        ),
                    }
                )
                # Keep one initialized, manager-owned connection for every active server.
                # This makes lifecycle status honest even when a server currently exposes
                # no tools/resources, and lets later prompt access share the same owner.
                clients_with_bindings[catalog.server.id] = catalog.client
            self._resource_records = tuple(resource_records)
            self._resource_lookup = {record.lookup_key: record for record in self._resource_records}
            self._resource_snapshotted_server_ids = {
                catalog.server.id
                for catalog in collected_catalogs
                if catalog.resources_snapshot_loaded
            }
            resource_tool_bindings = self._build_resource_tool_bindings()
            bindings.extend(resource_tool_bindings)
            self._tool_bindings = tuple(bindings)
            self._clients_by_server_id = clients_with_bindings
            prompt_server_catalogs = copy.deepcopy(
                self._catalog_snapshot.get("prompt_server_catalogs") or []
            )
            prompt_enabled_server_ids = list(
                self._catalog_snapshot.get("prompt_enabled_server_ids") or []
            )
            prompt_snapshotted_server_ids = list(
                self._catalog_snapshot.get("prompt_snapshotted_server_ids") or []
            )
            prompt_snapshot_complete = bool(self._catalog_snapshot.get("prompt_snapshot_complete"))
            prompt_snapshot_partial = bool(self._catalog_snapshot.get("prompt_snapshot_partial"))
            snapshotted_prompt_count = int(
                self._catalog_snapshot.get("snapshotted_prompt_count") or 0
            )
            manual_prompt_surface_enabled = bool(
                self._catalog_snapshot.get("manual_prompt_surface_enabled")
            )
            self._catalog_snapshot = {
                "catalog_initialized": True,
                "live_tool_runtime_enabled": self._live_runtime_enabled,
                "active_server_ids": [server.id for server in self._active_servers],
                "active_server_count": len(self._active_servers),
                "server_catalogs": server_catalogs,
                "exposed_tool_aliases": [binding.tool_alias for binding in self._tool_bindings],
                "exposed_tool_names": [binding.tool_name for binding in self._tool_bindings],
                "exposed_tool_count": len(self._tool_bindings),
                "snapshotted_resource_count": len(self._resource_records),
                "resource_tool_names": [binding.tool_alias for binding in resource_tool_bindings],
                "resource_tool_count": len(resource_tool_bindings),
                "prompt_enabled_server_ids": prompt_enabled_server_ids,
                "prompt_snapshotted_server_ids": prompt_snapshotted_server_ids,
                "prompt_snapshot_complete": prompt_snapshot_complete,
                "prompt_snapshot_partial": prompt_snapshot_partial,
                "tool_stale_server_ids": sorted(self._tool_stale_server_ids),
                "resource_stale_server_ids": sorted(self._resource_stale_server_ids),
                "prompt_stale_server_ids": sorted(self._prompt_stale_server_ids),
                "prompt_server_catalogs": prompt_server_catalogs,
                "snapshotted_prompt_count": snapshotted_prompt_count,
                "manual_prompt_surface_enabled": manual_prompt_surface_enabled,
                "failed_server_ids": sorted(self._server_startup_errors),
            }
            for catalog in collected_catalogs:
                if catalog.tools_list_changed:
                    self._tool_stale_server_ids.add(catalog.server.id)
                if catalog.resources_list_changed and catalog.resources_snapshot_loaded:
                    self._resource_stale_server_ids.add(catalog.server.id)
            self._sync_stale_catalog_snapshot_metadata()
        except Exception as exc:
            self._snapshot_error = exc
            for catalog in collected_catalogs:
                catalog.client.close()
            self.close()
            raise

    def _admit_server_catalogs(
        self,
        catalogs: tuple[_CollectedServerCatalog, ...],
    ) -> list[_AdmittedCatalog]:
        """Check each server's catalog on its own and keep the session inside its limits.

        A server whose tools or resources cannot be exposed, or that would take the session
        past a limit, is left out with its error recorded. Servers before it keep theirs.
        """
        admitted: list[_AdmittedCatalog] = []
        tool_count = 0
        schema_bytes = 0
        resource_count = 0
        for catalog in catalogs:
            server = catalog.server
            try:
                tools = [
                    (tool, _validate_function_tool_schema(server=server, tool=tool))
                    for tool in catalog.filtered_tools
                ]
                server_schema_bytes = sum(_stable_json_size(schema) for _tool, schema in tools)
                server_resources = len(catalog.listed_resources)
                if server_resources > _MAX_LISTED_MCP_RESOURCES_PER_SERVER:
                    raise ConfigError(
                        "MCP resource exposure is too large for server "
                        f"'{server.id}' ({server_resources} resources). Narrow server-side "
                        "resource exposure or disable resources_mode."
                    )
                if tool_count + len(tools) > _MAX_EXPOSED_MCP_TOOLS:
                    raise ConfigError(
                        f"MCP server '{server.id}' was not loaded: MCP tool exposure is too large "
                        f"for one session ({tool_count + len(tools)} tools). Narrow exposure with "
                        "allowed_tools."
                    )
                if schema_bytes + server_schema_bytes > _MAX_TOTAL_SCHEMA_BYTES:
                    raise ConfigError(
                        f"MCP server '{server.id}' was not loaded: MCP tool exposure schema "
                        "footprint is too large for one session "
                        f"({schema_bytes + server_schema_bytes} bytes). Narrow exposure with "
                        "allowed_tools."
                    )
                if resource_count + server_resources > _MAX_LISTED_MCP_RESOURCES_TOTAL:
                    raise ConfigError(
                        f"MCP server '{server.id}' was not loaded: MCP resource exposure is too "
                        f"large for one session ({resource_count + server_resources} resources). "
                        "Narrow server-side resource exposure or disable resources_mode."
                    )
            except Exception as exc:  # noqa: BLE001 - one server must not take down the others
                self._server_startup_errors[server.id] = exc
                self._discard_failed_server(server.id, catalog.client)
                continue
            tool_count += len(tools)
            schema_bytes += server_schema_bytes
            resource_count += server_resources
            admitted.append((catalog, tools))
        return admitted

    def _discard_failed_server(
        self, server_id: str, client: _McpSessionClient | None = None
    ) -> None:
        """Close a failed server's connection, including a lease made before the catalog."""
        if client is not None:
            client.close()
        for lease in self._client_leases_by_server_id.pop(server_id, ()):
            lease.close()

    def _collect_server_catalogs(self) -> tuple[_CollectedServerCatalog, ...]:
        """Connect to every active server. A server that fails is recorded and skipped."""
        catalogs: list[_CollectedServerCatalog] = []
        try:
            for server in self._active_servers:
                try:
                    catalogs.append(self._collect_server_catalog(server))
                except Exception as exc:  # noqa: BLE001 - one server must not take down the others
                    self._server_startup_errors[server.id] = exc
                    self._discard_failed_server(server.id)
        except BaseException:
            for catalog in catalogs:
                catalog.client.close()
            raise
        return tuple(catalogs)

    def _collect_server_catalog(self, server: ResolvedMcpServer) -> _CollectedServerCatalog:
        if server.trust != "explicit":
            raise ConfigError(
                f"MCP server '{server.id}' uses unsupported trust mode '{server.trust}'. "
                "Live MCP tool exposure currently requires trust='explicit'."
            )
        leases = self._client_leases_by_server_id.get(server.id, [])
        existing_lease = leases[0] if leases else None
        raw_client: _McpLiveClient | None = None
        if existing_lease is not None and existing_lease.enabled and not existing_lease.closed:
            client: _McpSessionClient = existing_lease
        else:
            raw_client = self._build_live_client(server)
            client = raw_client
        try:
            client.ensure_initialized()
            raw_tools: tuple[McpListedTool, ...] = ()
            filtered_tools: tuple[McpListedTool, ...] = ()
            raw_tool_names: tuple[str, ...] = ()
            if client.supports_tools:
                raw_tools = client.list_tools()
                filtered_tools, raw_tool_names = self._filter_server_tools(
                    server=server,
                    raw_tools=raw_tools,
                )
            elif server.allowed_tools or server.denied_tools:
                raise ConfigError(
                    f"MCP server '{server.id}' does not advertise tools capability, but "
                    "tool allow/deny policy is configured."
                )
            listed_resources: tuple[McpListedResource, ...] = ()
            resources_snapshot_loaded = resources_mode_enabled(
                resources_mode=server.resources_mode,
                runtime_kind=self.runtime_kind,
            )
            if resources_snapshot_loaded:
                listed_resources = client.list_resources()
            catalog_client = client
            if raw_client is not None and existing_lease is not None:
                if existing_lease.enabled:
                    # Recover an enabled-but-disconnected lease without
                    # invalidating objects that already reference it.
                    existing_lease.replace_client(raw_client)
                else:
                    # A pre-snapshot disable remains disabled. The
                    # temporary connection existed only to freeze the
                    # catalog and must not become reachable afterward.
                    raw_client.close()
                raw_client = None
                catalog_client = existing_lease
            return _CollectedServerCatalog(
                server=server,
                client=catalog_client,
                raw_tools=raw_tools,
                filtered_tools=filtered_tools,
                raw_tool_names=raw_tool_names,
                listed_resources=listed_resources,
                resources_snapshot_loaded=resources_snapshot_loaded,
                tools_list_changed=client.tools_list_changed,
                resources_list_changed=client.resources_list_changed,
                roots_capability_enabled=client.roots_capability_enabled,
                resources_capability_advertised=client.supports_resources,
                session_negotiated=client.session_negotiated,
            )
        except Exception:
            if raw_client is not None:
                raw_client.close()
            elif existing_lease is None:
                client.close()
            raise

    def _build_live_client(self, server: ResolvedMcpServer) -> _McpLiveClient:
        from .native_relay_guard import REFUSAL, is_reserved_relay_name, legacy_windows_launch

        # LR-41 permits only the selected legacy Windows interactive tuple.
        # This is availability, not W11 admission. The old standalone Mac socket
        # relay remains denied; a task-owned Mac launch needs its actual owner.
        # Arbitrary args are data. The transport inspects the exact selected
        # executable and supported execution positions immediately before launch.
        if server.id.rsplit("/", 1)[-1] == "alysis-computer" or (
            server.transport == "stdio" and is_reserved_relay_name(server.command or "")
        ):
            if legacy_windows_launch(server, self.runtime_kind) is None:
                raise ConfigError(REFUSAL)
        if server.transport == "stdio":
            return _client_factory("McpStdioClient")(
                server=server,
                workspace_root=self.workspace_root,
                runtime_kind=self.runtime_kind,
            )
        if server.transport == "http":
            return _client_factory("McpHttpClient")(
                server=server,
                workspace_root=self.workspace_root,
                runtime_kind=self.runtime_kind,
            )
        raise ConfigError(
            f"MCP server '{server.id}' uses unsupported transport '{server.transport}'."
        )

    def _filter_server_tools(
        self,
        *,
        server: ResolvedMcpServer,
        raw_tools: tuple[McpListedTool, ...],
    ) -> tuple[tuple[McpListedTool, ...], tuple[str, ...]]:
        raw_names = tuple(tool.name for tool in raw_tools)
        name_map: dict[str, McpListedTool] = {}
        for tool in raw_tools:
            name_key = _normalize_tool_name_key(tool.name)
            if name_key in name_map:
                raise ConfigError(
                    f"MCP server '{server.id}' reported duplicate tool names that only differ by "
                    f"case: '{name_map[name_key].name}' and '{tool.name}'."
                )
            name_map[name_key] = tool

        if server.allowed_tools:
            missing_allowed = [
                name
                for name in server.allowed_tools
                if _normalize_tool_name_key(name) not in name_map
            ]
            if missing_allowed:
                missing = ", ".join(missing_allowed)
                raise ConfigError(
                    f"MCP server '{server.id}' did not report configured allowed_tools: {missing}"
                )
        if server.denied_tools:
            missing_denied = [
                name
                for name in server.denied_tools
                if _normalize_tool_name_key(name) not in name_map
            ]
            if missing_denied:
                missing = ", ".join(missing_denied)
                raise ConfigError(
                    f"MCP server '{server.id}' did not report configured denied_tools: {missing}"
                )

        allowed_keys = {_normalize_tool_name_key(name) for name in server.allowed_tools}
        denied_keys = {_normalize_tool_name_key(name) for name in server.denied_tools}
        filtered: list[McpListedTool] = []
        for tool in raw_tools:
            if server.id == "alysis-computer":
                from .native_relay_guard import LEGACY_WINDOWS_TOOLS

                if tool.name not in LEGACY_WINDOWS_TOOLS:
                    continue
            tool_key = _normalize_tool_name_key(tool.name)
            if allowed_keys and tool_key not in allowed_keys:
                continue
            if tool_key in denied_keys:
                continue
            filtered.append(tool)
        return tuple(filtered), raw_names

    def _assign_aliases(
        self,
        records: list[_FlatToolRecord],
    ) -> dict[str, str]:
        # Secure-store readers import the MCP package without constructing tool
        # bindings. Load the built-in tool/agent graph only when it is needed.
        from ..tools.registry import iter_builtin_tool_metadata

        reserved_names = {metadata.name for metadata in iter_builtin_tool_metadata()}
        groups: dict[str, list[_FlatToolRecord]] = {}
        for record in records:
            groups.setdefault(record.base_alias, []).append(record)

        assigned: dict[str, str] = {}
        used_aliases: set[str] = set(reserved_names)
        for record in records:
            base_alias = record.base_alias
            group = groups[base_alias]
            alias = base_alias
            needs_hash = (
                len(base_alias) > _TOOL_ALIAS_MAX_LEN or len(group) > 1 or alias in used_aliases
            )
            if needs_hash:
                alias = _hashed_alias(base_alias, unique_key=record.unique_key)
            if alias in used_aliases:
                alias = _hashed_alias(
                    f"{base_alias}__{len(used_aliases)}",
                    unique_key=record.unique_key,
                )
            if alias in used_aliases:
                raise ConfigError(
                    f"Failed to assign a collision-free MCP tool alias for "
                    f"server '{record.server.id}' tool '{record.tool.name}'."
                )
            used_aliases.add(alias)
            assigned[record.unique_key] = alias
        return assigned

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            clients = list(self._clients_by_server_id.values())
            clients.extend(
                client
                for client in self._prompt_clients_by_server_id.values()
                if client not in clients
            )
            # Defensive cleanup: leases are the authoritative owners. Include
            # every lease even if a partially failed catalog build never placed
            # it in either lookup map.
            for leases in self._client_leases_by_server_id.values():
                clients.extend(client for client in leases if client not in clients)
            self._clients_by_server_id = {}
            self._prompt_clients_by_server_id = {}
            self._client_leases_by_server_id = {}
            self._disabled_server_ids = set(self._active_servers_by_id)
            for client in clients:
                client.close()


class ForgeTaskScopedMcpManager:
    def __init__(
        self,
        *,
        base_manager: McpManager | None,
        task_scope: ForgeTaskMcpScope | None,
        resolved_config: ResolvedMcpConfig | None = None,
        workspace_root: Path | None = None,
        runtime_kind: RuntimeKind | str | None = None,
        close_delegate: McpManager | None = None,
    ) -> None:
        self._base_manager = base_manager
        self._close_delegate = close_delegate or base_manager
        self._task_scope = task_scope
        if base_manager is not None:
            self._resolved_config = base_manager.resolved_config
            self._resolved_servers = base_manager.resolved_servers
            self._workspace_root = base_manager.workspace_root
            self._runtime_kind = base_manager.runtime_kind
        else:
            if resolved_config is None or workspace_root is None or runtime_kind is None:
                raise RuntimeError(
                    "ForgeTaskScopedMcpManager requires resolved_config, workspace_root, "
                    "and runtime_kind when no live base_manager is provided."
                )
            self._resolved_config = resolved_config
            self._resolved_servers = tuple(resolved_config.servers)
            self._workspace_root = workspace_root.resolve()
            self._runtime_kind = normalize_runtime_kind(runtime_kind)
        self._filtered_bindings: tuple[_McpManagerToolBinding, ...] | None = None
        self._scope_metadata: dict[str, Any] | None = None
        self._closed = False

    @classmethod
    def without_live_bootstrap(
        cls,
        *,
        resolved_config: ResolvedMcpConfig,
        workspace_root: Path,
        runtime_kind: RuntimeKind | str,
        task_scope: ForgeTaskMcpScope | None,
        close_delegate: McpManager | None = None,
    ) -> ForgeTaskScopedMcpManager:
        return cls(
            base_manager=None,
            task_scope=task_scope,
            resolved_config=resolved_config,
            workspace_root=workspace_root,
            runtime_kind=runtime_kind,
            close_delegate=close_delegate,
        )

    @property
    def resolved_config(self) -> ResolvedMcpConfig:
        return self._resolved_config

    @property
    def resolved_servers(self) -> tuple[ResolvedMcpServer, ...]:
        return self._resolved_servers

    @property
    def active_servers(self) -> tuple[ResolvedMcpServer, ...]:
        if self._base_manager is None:
            return ()
        return self._base_manager.active_servers

    @property
    def workspace_root(self) -> Path:
        return self._workspace_root

    @property
    def runtime_kind(self) -> RuntimeKind:
        return self._runtime_kind

    @property
    def closed(self) -> bool:
        if self._close_delegate is not None:
            return self._close_delegate.closed
        return self._closed

    @property
    def tool_bindings(self) -> tuple[_McpManagerToolBinding, ...]:
        self._ensure_filtered_bindings()
        assert self._filtered_bindings is not None
        return self._filtered_bindings

    def server_startup_errors(self) -> dict[str, str]:
        if self._base_manager is None:
            return {}
        return self._base_manager.server_startup_errors()

    def startup_metadata(self) -> dict[str, Any]:
        if self._base_manager is not None:
            metadata = copy.deepcopy(self._base_manager.startup_metadata())
        else:
            metadata = {
                "config_present": self._resolved_config.has_any_config,
                "user_config_present": self._resolved_config.user_config_present,
                "project_config_present": self._resolved_config.project_config_present,
                "resolved_server_count": len(self._resolved_servers),
                "resolved_server_ids": [server.id for server in self._resolved_servers],
                "active_server_count": 0,
                "active_server_ids": [],
                "live_tool_runtime_enabled": False,
            }
        metadata.update(self._scope_metadata_payload())
        return metadata

    def catalog_snapshot_metadata(self) -> dict[str, Any]:
        if self._base_manager is not None:
            snapshot = self._base_manager.catalog_snapshot_metadata()
        else:
            snapshot = {
                "catalog_initialized": True,
                "live_tool_runtime_enabled": False,
                "active_server_ids": [],
                "active_server_count": 0,
                "server_catalogs": [],
                "exposed_tool_aliases": [],
                "exposed_tool_names": [],
                "exposed_tool_count": 0,
                "snapshotted_resource_count": 0,
                "resource_tool_names": [],
                "resource_tool_count": 0,
            }
        snapshot["forge_task_mcp_scope"] = self._scope_metadata_payload()
        self._apply_filtered_tool_snapshot_metadata(snapshot)
        return snapshot

    def execution_context_summary(self) -> dict[str, Any]:
        self._ensure_filtered_bindings()
        summary = {
            "active_server_ids": [],
            "servers": [],
            "task_scope": self._task_scope_payload(),
        }
        if self._base_manager is None:
            return summary
        assert self._filtered_bindings is not None
        filtered_tool_names: dict[str, list[str]] = {}
        for binding in self._filtered_bindings:
            if not isinstance(binding, McpToolBinding):
                continue
            filtered_tool_names.setdefault(binding.server_id, []).append(binding.tool_name)
        base_summary = self._base_manager.execution_context_summary()
        filtered_servers: list[dict[str, Any]] = []
        allow_resources = bool(self._task_scope and self._task_scope.allow_resources)
        for raw_entry in base_summary.get("servers") or []:
            if not isinstance(raw_entry, dict):
                continue
            server_id = str(raw_entry.get("server_id") or "").strip()
            if not server_id:
                continue
            tool_names = list(filtered_tool_names.get(server_id) or [])
            resources_available = bool(allow_resources and raw_entry.get("resources_available"))
            if not tool_names and not resources_available:
                continue
            filtered_servers.append(
                {
                    "server_id": server_id,
                    "tool_names": tool_names,
                    "resources_available": resources_available,
                }
            )
        summary["active_server_ids"] = [entry["server_id"] for entry in filtered_servers]
        summary["servers"] = filtered_servers
        return summary

    def close(self) -> None:
        if self._close_delegate is not None:
            self._close_delegate.close()
            return
        self._closed = True

    def _scope_metadata_payload(self) -> dict[str, Any]:
        self._ensure_filtered_bindings()
        assert self._scope_metadata is not None
        return copy.deepcopy(self._scope_metadata)

    def _task_scope_payload(self) -> dict[str, Any]:
        return {
            "present": self._task_scope is not None,
            "allow_resources": bool(self._task_scope and self._task_scope.allow_resources),
            "allowed_tools": [
                {
                    "server_id": item.server_id,
                    "tool_name": item.tool_name,
                }
                for item in (self._task_scope.allowed_tools if self._task_scope is not None else ())
            ],
        }

    def _ensure_filtered_bindings(self) -> None:
        if self._filtered_bindings is not None:
            return
        if self._base_manager is None:
            allow_resources = bool(self._task_scope and self._task_scope.allow_resources)
            allowed_live_tool_count = (
                len(self._task_scope.allowed_tools) if self._task_scope is not None else 0
            )
            self._filtered_bindings = ()
            self._scope_metadata = {
                "forge_task_mcp_scope_present": self._task_scope is not None,
                "forge_task_resources_allowed": allow_resources,
                "forge_task_allowed_live_tool_count": allowed_live_tool_count,
                "forge_task_filtered_live_tool_count": 0,
                "forge_task_filtered_resource_tool_count": 0,
                "forge_task_filtered_tool_count": 0,
                "forge_task_live_bootstrap_skipped": True,
            }
            return
        base_bindings = self._base_manager.tool_bindings
        allowed_live_pairs = {
            (item.server_id, item.tool_name)
            for item in (self._task_scope.allowed_tools if self._task_scope is not None else ())
        }
        available_live_pairs = {
            (binding.server_id, binding.tool_name): binding
            for binding in base_bindings
            if isinstance(binding, McpToolBinding)
        }
        unknown_allowed_pairs = [
            f"{server_id}/{tool_name}"
            for server_id, tool_name in allowed_live_pairs
            if (server_id, tool_name) not in available_live_pairs
        ]
        if unknown_allowed_pairs:
            unknown_label = ", ".join(sorted(unknown_allowed_pairs))
            raise ConfigError(
                "Forge task mcp_scope references MCP tools that are not present in the frozen "
                f"session catalog: {unknown_label}"
            )

        allow_resources = bool(self._task_scope and self._task_scope.allow_resources)
        filtered_bindings: list[_McpManagerToolBinding] = []
        filtered_live_tool_count = 0
        filtered_resource_tool_count = 0
        for binding in base_bindings:
            if isinstance(binding, McpToolBinding):
                if (binding.server_id, binding.tool_name) not in allowed_live_pairs:
                    continue
                filtered_live_tool_count += 1
                filtered_bindings.append(binding)
                continue
            if binding.tool_alias in _RESOURCE_TOOL_ALIASES and allow_resources:
                filtered_resource_tool_count += 1
                filtered_bindings.append(binding)

        self._filtered_bindings = tuple(filtered_bindings)
        self._scope_metadata = {
            "forge_task_mcp_scope_present": self._task_scope is not None,
            "forge_task_resources_allowed": allow_resources,
            "forge_task_allowed_live_tool_count": len(allowed_live_pairs),
            "forge_task_filtered_live_tool_count": filtered_live_tool_count,
            "forge_task_filtered_resource_tool_count": filtered_resource_tool_count,
            "forge_task_filtered_tool_count": len(filtered_bindings),
            "forge_task_live_bootstrap_skipped": False,
        }

    def _apply_filtered_tool_snapshot_metadata(self, snapshot: dict[str, Any]) -> None:
        self._ensure_filtered_bindings()
        filtered_bindings = tuple(self._filtered_bindings or ())
        snapshot["exposed_tool_aliases"] = [binding.tool_alias for binding in filtered_bindings]
        snapshot["exposed_tool_names"] = [binding.tool_name for binding in filtered_bindings]
        snapshot["exposed_tool_count"] = len(filtered_bindings)
        resource_aliases = [
            binding.tool_alias
            for binding in filtered_bindings
            if isinstance(binding, McpHostToolBinding)
            and binding.tool_alias in _RESOURCE_TOOL_ALIASES
        ]
        snapshot["resource_tool_names"] = resource_aliases
        snapshot["resource_tool_count"] = len(resource_aliases)

        live_bindings_by_server_id: dict[str, list[McpToolBinding]] = {}
        for binding in filtered_bindings:
            if isinstance(binding, McpToolBinding):
                live_bindings_by_server_id.setdefault(binding.server_id, []).append(binding)

        server_catalogs = snapshot.get("server_catalogs")
        if not isinstance(server_catalogs, list):
            return
        for entry in server_catalogs:
            if not isinstance(entry, dict):
                continue
            server_id = str(entry.get("server_id") or "").strip()
            live_bindings = live_bindings_by_server_id.get(server_id) or []
            entry["exposed_tool_aliases"] = [binding.tool_alias for binding in live_bindings]
            entry["exposed_tool_names"] = [binding.tool_name for binding in live_bindings]
            entry["exposed_tool_count"] = len(live_bindings)


def create_mcp_manager(
    *,
    workspace_root: Path,
    runtime_kind: RuntimeKind | str,
    session_id: str | None = None,
) -> McpManager:
    resolved_kind = normalize_runtime_kind(runtime_kind)
    resolved_config = load_resolved_mcp_config(
        workspace_root=workspace_root,
    )
    return McpManager(
        resolved_config=resolved_config,
        workspace_root=workspace_root,
        runtime_kind=resolved_kind,
        session_id=session_id,
    )


def create_forge_task_scoped_mcp_manager(
    *,
    workspace_root: Path,
    session_id: str | None = None,
    task_scope: ForgeTaskMcpScope | None,
) -> ForgeTaskScopedMcpManager:
    resolved_kind = RuntimeKind.FORGE_EXEC
    if task_scope is None or task_scope.is_empty:
        resolved_workspace_root = workspace_root.resolve()
        return ForgeTaskScopedMcpManager.without_live_bootstrap(
            resolved_config=ResolvedMcpConfig(
                workspace_root=resolved_workspace_root,
                user_config_path=user_mcp_config_path(),
                project_config_path=project_mcp_config_path(resolved_workspace_root),
                user_config_present=False,
                project_config_present=False,
                servers=(),
            ),
            workspace_root=resolved_workspace_root,
            runtime_kind=resolved_kind,
            task_scope=task_scope,
        )
    resolved_config = load_resolved_mcp_config(
        workspace_root=workspace_root,
    )
    manager = McpManager(
        resolved_config=resolved_config,
        workspace_root=workspace_root,
        runtime_kind=resolved_kind,
        session_id=session_id,
    )
    return manager.scope_for_forge_task(task_scope=task_scope)


def build_mcp_execution_context_summary(
    *,
    workspace_root: Path,
    runtime_kind: RuntimeKind | str,
) -> dict[str, Any] | None:
    try:
        resolved_kind = normalize_runtime_kind(runtime_kind)
        resolved_config = load_resolved_mcp_config(workspace_root=workspace_root)
        active_servers = resolved_config.active_servers_for(resolved_kind)
        return {
            "active_server_ids": [server.id for server in active_servers],
            "servers": [
                {
                    "server_id": server.id,
                    "tool_names": [name for name in server.allowed_tools if str(name).strip()],
                    "resources_available": resources_mode_enabled(
                        server.resources_mode,
                        runtime_kind=resolved_kind,
                    ),
                }
                for server in active_servers
            ],
        }
    except Exception:
        return None


# Preserve the existing non-private star-import surface, including canonical
# client classes that are now materialized only when explicitly requested.
__all__ = [name for name in globals() if not name.startswith("_")] + [
    "McpHttpClient",
    "McpStdioClient",
]
