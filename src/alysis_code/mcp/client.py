from __future__ import annotations

import copy
from collections.abc import Callable, Collection
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar
from urllib.parse import SplitResult, urlsplit, urlunsplit

from .. import __version__
from ..runtime_kind import RuntimeKind
from .catalog import McpListedTool as McpListedTool
from .errors import McpError, McpProtocolError, McpRemoteError
from .jsonrpc import JsonRpcErrorObject, JsonRpcNotification, JsonRpcResponse
from .models import ResolvedMcpServer
from .oauth import McpOAuthError
from .prompts import (
    McpGetPromptResult,
    McpListedPrompt,
    McpPromptNormalizationError,
    normalize_get_prompt_result,
    normalize_list_prompts_result,
)
from .resources import (
    McpListedResource,
    McpReadResourceResult,
    McpResourceNormalizationError,
    normalize_list_resources_result,
    normalize_read_resource_result,
)
from .roots import McpRootsRequestHandler
from .server_requests import McpServerRequestContext, McpServerRequestHandler
from .transport_http import (
    McpHttpTransport,
    McpHttpTransportError,
    McpHttpTransportSessionExpiredError,
)
from .transport_stdio import McpStdioTransport, McpStdioTransportError

MCP_PROTOCOL_VERSION = "2024-11-05"
MCP_STDIO_PROTOCOL_VERSION = MCP_PROTOCOL_VERSION
MCP_HTTP_PREFERRED_PROTOCOL_VERSION = "2025-11-25"
MCP_HTTP_SUPPORTED_PROTOCOL_VERSIONS = (
    MCP_HTTP_PREFERRED_PROTOCOL_VERSION,
    "2025-06-18",
    "2025-03-26",
)
_SUPPORTED_PROTOCOL_VERSIONS_BY_TRANSPORT = {
    "stdio": (MCP_STDIO_PROTOCOL_VERSION,),
    "http": MCP_HTTP_SUPPORTED_PROTOCOL_VERSIONS,
}
# Stdio observes immediate server activity after `notifications/initialized`
# without making a quiet startup wait for the full configured timeout.
_STDIO_INITIALIZED_NOTIFICATION_COMPLETION_TIMEOUT_S = 0.5
# Bound encoded image retention before validation by the tool's visual channel.
_MAX_IMAGE_BASE64_CHARS = ((8 * 1024 * 1024 + 2) // 3) * 4
_ReadOnlyRequestResult = TypeVar("_ReadOnlyRequestResult")


class McpClientError(McpError):
    pass


class McpClientProtocolError(McpClientError, McpProtocolError):
    pass


class McpClientRemoteError(McpClientError, McpRemoteError):
    pass


class _McpClientTransport(Protocol):
    @property
    def closed(self) -> bool: ...

    def request(
        self,
        *,
        method: str,
        params: Any | None,
        timeout_s: float,
    ) -> JsonRpcResponse: ...

    def send_notification(
        self,
        *,
        method: str,
        params: Any | None,
        completion_timeout_s: float | None = None,
    ) -> None: ...

    def drain_notifications(self) -> tuple[JsonRpcNotification, ...]: ...

    def close(self) -> None: ...

    def stderr_tail(self) -> str: ...


def _redacted_server_url(value: str | None) -> str:
    split = urlsplit(str(value or ""))
    hostname = split.hostname or ""
    if split.port is None:
        netloc = hostname
    else:
        netloc = f"{hostname}:{split.port}"
    redacted = SplitResult(
        scheme=split.scheme,
        netloc=netloc,
        path=split.path or "/",
        query="",
        fragment="",
    )
    return urlunsplit(redacted)


def _server_context(server: ResolvedMcpServer) -> str:
    if server.transport == "http":
        return f"MCP server '{server.id}' (url '{_redacted_server_url(server.url)}')"
    return f"MCP server '{server.id}' (command '{server.command}')"


def _build_client_error(
    *,
    server: ResolvedMcpServer,
    message: str,
    stderr_tail: str = "",
    error_type: type[McpClientError] = McpClientError,
) -> McpClientError:
    full_message = f"{_server_context(server)}: {message}"
    cleaned_tail = str(stderr_tail or "").strip()
    if cleaned_tail:
        full_message += f"\nstderr tail:\n{cleaned_tail}"
    return error_type(full_message)


def _initial_protocol_version_for_transport(transport: str) -> str:
    if transport == "http":
        return MCP_HTTP_PREFERRED_PROTOCOL_VERSION
    return MCP_STDIO_PROTOCOL_VERSION


def _supported_protocol_versions_for_transport(transport: str) -> tuple[str, ...]:
    return tuple(_SUPPORTED_PROTOCOL_VERSIONS_BY_TRANSPORT.get(transport, ()))


def _validate_protocol_version(
    *,
    server: ResolvedMcpServer,
    transport: str,
    value: Any,
    stderr_tail: str,
) -> str:
    protocol_version = _require_string(
        value,
        server=server,
        context="initialize.protocolVersion",
    )
    supported_versions = _supported_protocol_versions_for_transport(transport)
    if protocol_version in supported_versions:
        return protocol_version
    if not supported_versions:
        supported_text = "no supported versions configured"
    elif len(supported_versions) == 1:
        supported_text = repr(supported_versions[0])
    else:
        supported_text = ", ".join(repr(item) for item in supported_versions)
    raise _build_client_error(
        server=server,
        message=(
            "server reported unsupported protocol version "
            f"{protocol_version!r}; expected one of {supported_text}."
        ),
        stderr_tail=stderr_tail,
        error_type=McpClientProtocolError,
    )


def _require_object(
    value: Any,
    *,
    server: ResolvedMcpServer,
    context: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _build_client_error(
            server=server,
            message=f"{context} must be an object.",
            error_type=McpClientProtocolError,
        )
    return value


def _require_string(
    value: Any,
    *,
    server: ResolvedMcpServer,
    context: str,
) -> str:
    if not isinstance(value, str):
        raise _build_client_error(
            server=server,
            message=f"{context} must be a string.",
            error_type=McpClientProtocolError,
        )
    cleaned = value.strip()
    if not cleaned:
        raise _build_client_error(
            server=server,
            message=f"{context} cannot be empty.",
            error_type=McpClientProtocolError,
        )
    return cleaned


@dataclass(frozen=True)
class McpToolCallResult:
    is_error: bool
    structured_content: Any | None
    content: tuple[dict[str, Any], ...]
    content_summary: str
    extracted_text: str
    raw_payload: dict[str, Any] = field(repr=False)


class _BaseMcpClient:
    transport: _McpClientTransport
    _transport_error_types: tuple[type[BaseException], ...]

    def __init__(
        self,
        *,
        server: ResolvedMcpServer,
        workspace_root: Path,
        transport: _McpClientTransport,
        transport_error_types: tuple[type[BaseException], ...],
        runtime_kind: RuntimeKind,
        server_request_context: McpServerRequestContext,
        server_request_handler: McpServerRequestHandler,
    ) -> None:
        self.server = server
        self.workspace_root = workspace_root.resolve()
        self.transport = transport
        self._transport_error_types = transport_error_types
        self.runtime_kind = runtime_kind
        self._server_request_context = server_request_context
        self._server_request_handler = server_request_handler
        self._client_capabilities = copy.deepcopy(
            server_request_handler.client_capabilities(context=server_request_context)
        )
        self._initialized = False
        self._supports_tools = False
        self._supports_resources = False
        self._supports_prompts = False
        self._server_info: dict[str, Any] = {}
        self._server_capabilities: dict[str, Any] = {}
        self._server_instructions: str | None = None
        self._tools_list_changed = False
        self._resources_list_changed = False
        self._prompts_list_changed = False
        self._negotiated_protocol_version: str | None = None

    @property
    def closed(self) -> bool:
        return self.transport.closed

    @property
    def tools_list_changed(self) -> bool:
        return self._tools_list_changed

    @property
    def resources_list_changed(self) -> bool:
        return self._resources_list_changed

    @property
    def prompts_list_changed(self) -> bool:
        return self._prompts_list_changed

    @property
    def server_info(self) -> dict[str, Any]:
        return dict(self._server_info)

    @property
    def server_capabilities(self) -> dict[str, Any]:
        return copy.deepcopy(self._server_capabilities)

    @property
    def server_instructions(self) -> str | None:
        return self._server_instructions

    @property
    def negotiated_protocol_version(self) -> str | None:
        return self._negotiated_protocol_version

    @property
    def session_negotiated(self) -> bool:
        return False

    @property
    def roots_capability_enabled(self) -> bool:
        return isinstance(self._client_capabilities.get("roots"), dict)

    @property
    def supports_tools(self) -> bool:
        return self._supports_tools

    @property
    def supports_resources(self) -> bool:
        return self._supports_resources

    @property
    def supports_prompts(self) -> bool:
        return self._supports_prompts

    def _diagnostic_tail(self) -> str:
        return self.transport.stderr_tail()

    def _drain_notifications(self) -> tuple[JsonRpcNotification, ...]:
        notifications = self.transport.drain_notifications()
        for notification in notifications:
            if notification.method == "notifications/tools/list_changed":
                self._tools_list_changed = True
            elif notification.method == "notifications/resources/list_changed":
                self._resources_list_changed = True
            elif notification.method == "notifications/prompts/list_changed":
                self._prompts_list_changed = True
        return notifications

    def observe_notifications(self) -> None:
        self._drain_notifications()

    def _raise_remote_error(
        self,
        *,
        method: str,
        error: JsonRpcErrorObject,
    ) -> None:
        raise _build_client_error(
            server=self.server,
            message=(f"remote JSON-RPC error during '{method}': {error.code} {error.message}"),
            stderr_tail=self._diagnostic_tail(),
            error_type=McpClientRemoteError,
        )

    def _response_result_object(
        self,
        *,
        method: str,
        response: JsonRpcResponse,
    ) -> dict[str, Any]:
        if response.error is not None:
            self._raise_remote_error(method=method, error=response.error)
        return _require_object(
            response.result,
            server=self.server,
            context=f"'{method}' result",
        )

    def _reset_initialization_state(self) -> None:
        self._initialized = False
        self._supports_tools = False
        self._supports_resources = False
        self._supports_prompts = False
        self._server_info = {}
        self._server_capabilities = {}
        self._server_instructions = None
        self._negotiated_protocol_version = None

    def _apply_negotiated_protocol_version(self, protocol_version: str) -> None:
        self._negotiated_protocol_version = protocol_version

    def _initialize_once(self) -> None:
        response = self.transport.request(
            method="initialize",
            params={
                "protocolVersion": _initial_protocol_version_for_transport(self.server.transport),
                "capabilities": copy.deepcopy(self._client_capabilities),
                "clientInfo": {
                    "name": "alysis-code",
                    "version": __version__,
                },
            },
            timeout_s=self.server.startup_timeout_s,
        )
        result = self._response_result_object(method="initialize", response=response)
        protocol_version = _validate_protocol_version(
            server=self.server,
            transport=self.server.transport,
            value=result.get("protocolVersion"),
            stderr_tail=self._diagnostic_tail(),
        )
        capabilities = _require_object(
            result.get("capabilities"),
            server=self.server,
            context="initialize.capabilities",
        )
        tools_capability = capabilities.get("tools")
        if tools_capability is not None and not isinstance(tools_capability, dict):
            raise _build_client_error(
                server=self.server,
                message="initialize.capabilities.tools must be an object when present.",
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            )
        resources_capability = capabilities.get("resources")
        if resources_capability is not None and not isinstance(resources_capability, dict):
            raise _build_client_error(
                server=self.server,
                message="initialize.capabilities.resources must be an object when present.",
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            )
        prompts_capability = capabilities.get("prompts")
        if prompts_capability is not None and not isinstance(prompts_capability, dict):
            raise _build_client_error(
                server=self.server,
                message="initialize.capabilities.prompts must be an object when present.",
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            )
        server_info = result.get("serverInfo") or {}
        if not isinstance(server_info, dict):
            raise _build_client_error(
                server=self.server,
                message="initialize.serverInfo must be an object when present.",
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            )
        instructions = result.get("instructions")
        if instructions is not None and not isinstance(instructions, str):
            raise _build_client_error(
                server=self.server,
                message="initialize.instructions must be a string when present.",
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            )
        self._server_instructions = instructions
        self._server_info = copy.deepcopy(server_info)
        self._server_capabilities = copy.deepcopy(capabilities)
        self._supports_tools = "tools" in capabilities
        self._supports_resources = "resources" in capabilities
        self._supports_prompts = "prompts" in capabilities
        self._apply_negotiated_protocol_version(protocol_version)
        notification_timeout_s = self.server.startup_timeout_s
        if self.server.transport == "stdio":
            notification_timeout_s = min(
                notification_timeout_s, _STDIO_INITIALIZED_NOTIFICATION_COMPLETION_TIMEOUT_S
            )
        # HTTP must wait for the server's acknowledgement of the notification;
        # a short stdio observation window is not a network request timeout.
        self.transport.send_notification(
            method="notifications/initialized",
            params={},
            completion_timeout_s=notification_timeout_s,
        )
        self._initialized = True
        self._drain_notifications()

    def ensure_initialized(self) -> None:
        if self._initialized:
            self._drain_notifications()
            return
        try:
            self._initialize_once()
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, (McpClientError, *self._transport_error_types)):
                self.close()
                raise
            self.close()
            raise _build_client_error(
                server=self.server,
                message=f"initialize failed: {exc}",
                stderr_tail=self._diagnostic_tail(),
            ) from exc

    def _require_tools_capability(self, *, method: str) -> None:
        self.ensure_initialized()
        if self._supports_tools:
            return
        raise _build_client_error(
            server=self.server,
            message=f"server does not advertise tools capability for '{method}'.",
            stderr_tail=self._diagnostic_tail(),
            error_type=McpClientProtocolError,
        )

    def _require_resources_capability(self, *, method: str) -> None:
        self.ensure_initialized()
        if self._supports_resources:
            return
        raise _build_client_error(
            server=self.server,
            message=f"server does not advertise resources capability for '{method}'.",
            stderr_tail=self._diagnostic_tail(),
            error_type=McpClientProtocolError,
        )

    def _require_prompts_capability(self, *, method: str) -> None:
        self.ensure_initialized()
        if self._supports_prompts:
            return
        raise _build_client_error(
            server=self.server,
            message=f"server does not advertise prompts capability for '{method}'.",
            stderr_tail=self._diagnostic_tail(),
            error_type=McpClientProtocolError,
        )

    def _list_tools_once(self) -> tuple[McpListedTool, ...]:
        tools: list[McpListedTool] = []
        seen_cursors: set[str] = set()
        next_cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if next_cursor is not None:
                params["cursor"] = next_cursor
            response = self.transport.request(
                method="tools/list",
                params=params,
                timeout_s=self.server.call_timeout_s,
            )
            result = self._response_result_object(method="tools/list", response=response)
            page_tools = result.get("tools")
            if not isinstance(page_tools, list):
                raise _build_client_error(
                    server=self.server,
                    message="tools/list result.tools must be an array.",
                    stderr_tail=self._diagnostic_tail(),
                    error_type=McpClientProtocolError,
                )
            for raw_tool in page_tools:
                payload = _require_object(
                    raw_tool,
                    server=self.server,
                    context="tools/list tool entry",
                )
                tools.append(
                    McpListedTool(
                        name=_require_string(
                            payload.get("name"),
                            server=self.server,
                            context="tools/list tool.name",
                        ),
                        description=str(payload.get("description") or "").strip(),
                        input_schema=_require_object(
                            payload.get("inputSchema"),
                            server=self.server,
                            context="tools/list tool.inputSchema",
                        ),
                        raw_payload=copy.deepcopy(payload),
                    )
                )
            raw_cursor = result.get("nextCursor")
            if raw_cursor is None:
                break
            next_cursor = _require_string(
                raw_cursor,
                server=self.server,
                context="tools/list nextCursor",
            )
            if next_cursor in seen_cursors:
                raise _build_client_error(
                    server=self.server,
                    message=f"tools/list returned repeated nextCursor {next_cursor!r}.",
                    stderr_tail=self._diagnostic_tail(),
                    error_type=McpClientProtocolError,
                )
            seen_cursors.add(next_cursor)
        self._drain_notifications()
        return tuple(tools)

    def list_tools(self) -> tuple[McpListedTool, ...]:
        self._require_tools_capability(method="tools/list")
        return self._list_tools_once()

    def _list_resources_once(self) -> tuple[McpListedResource, ...]:
        resources: list[McpListedResource] = []
        seen_cursors: set[str] = set()
        seen_uris: set[str] = set()
        next_cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if next_cursor is not None:
                params["cursor"] = next_cursor
            response = self.transport.request(
                method="resources/list",
                params=params,
                timeout_s=self.server.call_timeout_s,
            )
            result = self._response_result_object(method="resources/list", response=response)
            try:
                page_resources, raw_cursor = normalize_list_resources_result(result)
            except McpResourceNormalizationError as exc:
                raise _build_client_error(
                    server=self.server,
                    message=str(exc),
                    stderr_tail=self._diagnostic_tail(),
                    error_type=McpClientProtocolError,
                ) from exc
            for resource in page_resources:
                if resource.uri in seen_uris:
                    raise _build_client_error(
                        server=self.server,
                        message=f"resources/list returned repeated resource URI {resource.uri!r}.",
                        stderr_tail=self._diagnostic_tail(),
                        error_type=McpClientProtocolError,
                    )
                seen_uris.add(resource.uri)
                resources.append(resource)
            if raw_cursor is None:
                break
            if raw_cursor in seen_cursors:
                raise _build_client_error(
                    server=self.server,
                    message=f"resources/list returned repeated nextCursor {raw_cursor!r}.",
                    stderr_tail=self._diagnostic_tail(),
                    error_type=McpClientProtocolError,
                )
            seen_cursors.add(raw_cursor)
            next_cursor = raw_cursor
        self._drain_notifications()
        return tuple(resources)

    def list_resources(self) -> tuple[McpListedResource, ...]:
        self._require_resources_capability(method="resources/list")
        return self._list_resources_once()

    def _list_prompts_once(self) -> tuple[McpListedPrompt, ...]:
        prompts: list[McpListedPrompt] = []
        seen_cursors: set[str] = set()
        seen_names: set[str] = set()
        next_cursor: str | None = None
        while True:
            params: dict[str, Any] = {}
            if next_cursor is not None:
                params["cursor"] = next_cursor
            response = self.transport.request(
                method="prompts/list",
                params=params,
                timeout_s=self.server.call_timeout_s,
            )
            result = self._response_result_object(method="prompts/list", response=response)
            try:
                page_prompts, raw_cursor = normalize_list_prompts_result(result)
            except McpPromptNormalizationError as exc:
                raise _build_client_error(
                    server=self.server,
                    message=str(exc),
                    stderr_tail=self._diagnostic_tail(),
                    error_type=McpClientProtocolError,
                ) from exc
            for prompt in page_prompts:
                name_key = prompt.name.casefold()
                if name_key in seen_names:
                    raise _build_client_error(
                        server=self.server,
                        message=f"prompts/list returned repeated prompt name {prompt.name!r}.",
                        stderr_tail=self._diagnostic_tail(),
                        error_type=McpClientProtocolError,
                    )
                seen_names.add(name_key)
                prompts.append(prompt)
            if raw_cursor is None:
                break
            if raw_cursor in seen_cursors:
                raise _build_client_error(
                    server=self.server,
                    message=f"prompts/list returned repeated nextCursor {raw_cursor!r}.",
                    stderr_tail=self._diagnostic_tail(),
                    error_type=McpClientProtocolError,
                )
            seen_cursors.add(raw_cursor)
            next_cursor = raw_cursor
        self._drain_notifications()
        return tuple(prompts)

    def list_prompts(self) -> tuple[McpListedPrompt, ...]:
        self._require_prompts_capability(method="prompts/list")
        return self._list_prompts_once()

    def _normalize_prompt_arguments(
        self,
        *,
        arguments: dict[str, str] | None,
    ) -> dict[str, str] | None:
        if arguments is None:
            return None
        if not isinstance(arguments, dict):
            raise _build_client_error(
                server=self.server,
                message="prompts/get arguments must be an object mapping strings to strings.",
                error_type=McpClientProtocolError,
            )
        normalized: dict[str, str] = {}
        for raw_key, raw_value in arguments.items():
            key = _require_string(
                raw_key,
                server=self.server,
                context="prompts/get argument name",
            )
            if not isinstance(raw_value, str):
                raise _build_client_error(
                    server=self.server,
                    message="prompts/get argument values must be strings.",
                    error_type=McpClientProtocolError,
                )
            normalized[key] = raw_value
        return normalized

    def _get_prompt_once(
        self,
        *,
        name: str,
        arguments: dict[str, str] | None,
    ) -> McpGetPromptResult:
        params: dict[str, Any] = {"name": name}
        normalized_arguments = self._normalize_prompt_arguments(arguments=arguments)
        if normalized_arguments:
            params["arguments"] = normalized_arguments
        response = self.transport.request(
            method="prompts/get",
            params=params,
            timeout_s=self.server.call_timeout_s,
        )
        result = self._response_result_object(method="prompts/get", response=response)
        try:
            normalized = normalize_get_prompt_result(result, expected_name=name)
        except McpPromptNormalizationError as exc:
            raise _build_client_error(
                server=self.server,
                message=str(exc),
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            ) from exc
        self._drain_notifications()
        return normalized

    def get_prompt(
        self,
        *,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> McpGetPromptResult:
        self._require_prompts_capability(method="prompts/get")
        normalized_name = _require_string(
            name,
            server=self.server,
            context="prompts/get name",
        )
        return self._get_prompt_once(name=normalized_name, arguments=arguments)

    def _read_resource_once(
        self,
        *,
        resource_uri: str,
        allowed_uris: Collection[str] | None = None,
    ) -> McpReadResourceResult:
        if allowed_uris is None:
            raise _build_client_error(
                server=self.server,
                message=(
                    "resources/read requires a frozen resource snapshot allowlist from "
                    "resources/list; arbitrary URI reads are blocked."
                ),
                error_type=McpClientError,
            )
        if isinstance(allowed_uris, str):
            raise _build_client_error(
                server=self.server,
                message="resources/read allowed_uris must be a collection of URIs, not a string.",
                error_type=McpClientProtocolError,
            )
        if not isinstance(allowed_uris, Collection):
            raise _build_client_error(
                server=self.server,
                message="resources/read allowed_uris must be a collection of URI strings.",
                error_type=McpClientProtocolError,
            )
        normalized_allowed_uris = {
            _require_string(
                item,
                server=self.server,
                context="resources/read allowed URI",
            )
            for item in allowed_uris
        }
        if resource_uri not in normalized_allowed_uris:
            raise _build_client_error(
                server=self.server,
                message=(
                    "resources/read is limited to the frozen resource snapshot for this session; "
                    f"URI {resource_uri!r} was not snapshotted."
                ),
                error_type=McpClientError,
            )
        response = self.transport.request(
            method="resources/read",
            params={"uri": resource_uri},
            timeout_s=self.server.call_timeout_s,
        )
        result = self._response_result_object(method="resources/read", response=response)
        try:
            normalized = normalize_read_resource_result(result, expected_uri=resource_uri)
        except McpResourceNormalizationError as exc:
            raise _build_client_error(
                server=self.server,
                message=str(exc),
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            ) from exc
        self._drain_notifications()
        return normalized

    def read_resource(
        self,
        *,
        resource_uri: str,
        allowed_uris: Collection[str] | None = None,
    ) -> McpReadResourceResult:
        self._require_resources_capability(method="resources/read")
        normalized_uri = _require_string(
            resource_uri,
            server=self.server,
            context="resources/read uri",
        )
        return self._read_resource_once(resource_uri=normalized_uri, allowed_uris=allowed_uris)

    def _call_tool_once(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> McpToolCallResult:
        response = self.transport.request(
            method="tools/call",
            params={
                "name": tool_name,
                "arguments": arguments,
            },
            timeout_s=self.server.call_timeout_s,
        )
        result = self._response_result_object(method="tools/call", response=response)
        content = result.get("content")
        if content is None:
            content = []
        if not isinstance(content, list):
            raise _build_client_error(
                server=self.server,
                message="tools/call result.content must be an array when present.",
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            )
        is_error = bool(result.get("isError", False))
        if "isError" in result and not isinstance(result.get("isError"), bool):
            raise _build_client_error(
                server=self.server,
                message="tools/call result.isError must be a boolean when present.",
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            )
        normalized_content, content_summary, extracted_text = _normalize_content_items(
            server=self.server,
            content_items=content,
            stderr_tail=self._diagnostic_tail(),
        )
        self._drain_notifications()
        return McpToolCallResult(
            is_error=is_error,
            structured_content=copy.deepcopy(result.get("structuredContent")),
            content=normalized_content,
            content_summary=content_summary,
            extracted_text=extracted_text,
            raw_payload=copy.deepcopy(result),
        )

    def call_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> McpToolCallResult:
        self._require_tools_capability(method="tools/call")
        if not isinstance(arguments, dict):
            raise _build_client_error(
                server=self.server,
                message="tools/call arguments must be an object.",
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            )
        return self._call_tool_once(tool_name=tool_name, arguments=arguments)

    def close(self) -> None:
        self.transport.close()


class McpStdioClient(_BaseMcpClient):
    def __init__(
        self,
        *,
        server: ResolvedMcpServer,
        workspace_root: Path,
        runtime_kind: RuntimeKind | str | None = None,
    ) -> None:
        resolved_workspace_root = workspace_root.resolve()
        request_context = McpServerRequestContext.create(
            server_id=server.id,
            workspace_root=resolved_workspace_root,
            runtime_kind=runtime_kind,
            fallback_runtime_kind=RuntimeKind.SUBAGENT,
        )
        request_handler = McpRootsRequestHandler(server=server)
        super().__init__(
            server=server,
            workspace_root=resolved_workspace_root,
            transport=McpStdioTransport(
                server=server,
                workspace_root=resolved_workspace_root,
                server_request_context=request_context,
                server_request_handler=request_handler,
            ),
            transport_error_types=(McpStdioTransportError,),
            runtime_kind=request_context.runtime_kind,
            server_request_context=request_context,
            server_request_handler=request_handler,
        )


class McpHttpClient(_BaseMcpClient):
    transport: McpHttpTransport

    def __init__(
        self,
        *,
        server: ResolvedMcpServer,
        workspace_root: Path,
        runtime_kind: RuntimeKind | str | None = None,
    ) -> None:
        resolved_workspace_root = workspace_root.resolve()
        request_context = McpServerRequestContext.create(
            server_id=server.id,
            workspace_root=resolved_workspace_root,
            runtime_kind=runtime_kind,
            fallback_runtime_kind=RuntimeKind.SUBAGENT,
        )
        request_handler = McpRootsRequestHandler(server=server)
        super().__init__(
            server=server,
            workspace_root=resolved_workspace_root,
            transport=McpHttpTransport(
                server=server,
                workspace_root=resolved_workspace_root,
                server_request_context=request_context,
                server_request_handler=request_handler,
            ),
            transport_error_types=(McpHttpTransportError, McpOAuthError),
            runtime_kind=request_context.runtime_kind,
            server_request_context=request_context,
            server_request_handler=request_handler,
        )

    @property
    def session_negotiated(self) -> bool:
        return self.transport.session_negotiated

    def _apply_negotiated_protocol_version(self, protocol_version: str) -> None:
        super()._apply_negotiated_protocol_version(protocol_version)
        self.transport.set_negotiated_protocol_version(protocol_version)

    def _reset_http_session_state(self) -> None:
        self.transport.reset_session_state()
        self._reset_initialization_state()

    def _reinitialize_after_session_expiry(self) -> None:
        self._reset_http_session_state()
        self.ensure_initialized()

    def _retry_read_only_request_once(
        self,
        *,
        method: str,
        request_fn: Callable[[], _ReadOnlyRequestResult],
    ) -> _ReadOnlyRequestResult:
        for attempt in range(2):
            try:
                return request_fn()
            except McpHttpTransportSessionExpiredError as exc:
                if attempt > 0:
                    raise _build_client_error(
                        server=self.server,
                        message=(
                            f"HTTP MCP session expired during '{method}' even after one "
                            "reinitialize+retry."
                        ),
                        error_type=McpClientError,
                    ) from exc
                self._reinitialize_after_session_expiry()
        raise AssertionError("unreachable")

    def list_tools(self) -> tuple[McpListedTool, ...]:
        self._require_tools_capability(method="tools/list")
        return self._retry_read_only_request_once(
            method="tools/list", request_fn=self._list_tools_once
        )

    def list_resources(self) -> tuple[McpListedResource, ...]:
        self._require_resources_capability(method="resources/list")
        return self._retry_read_only_request_once(
            method="resources/list",
            request_fn=self._list_resources_once,
        )

    def list_prompts(self) -> tuple[McpListedPrompt, ...]:
        self._require_prompts_capability(method="prompts/list")
        return self._retry_read_only_request_once(
            method="prompts/list",
            request_fn=self._list_prompts_once,
        )

    def read_resource(
        self,
        *,
        resource_uri: str,
        allowed_uris: Collection[str] | None = None,
    ) -> McpReadResourceResult:
        self._require_resources_capability(method="resources/read")
        normalized_uri = _require_string(
            resource_uri,
            server=self.server,
            context="resources/read uri",
        )
        return self._retry_read_only_request_once(
            method="resources/read",
            request_fn=lambda: self._read_resource_once(
                resource_uri=normalized_uri,
                allowed_uris=allowed_uris,
            ),
        )

    def get_prompt(
        self,
        *,
        name: str,
        arguments: dict[str, str] | None = None,
    ) -> McpGetPromptResult:
        self._require_prompts_capability(method="prompts/get")
        normalized_name = _require_string(
            name,
            server=self.server,
            context="prompts/get name",
        )
        return self._retry_read_only_request_once(
            method="prompts/get",
            request_fn=lambda: self._get_prompt_once(
                name=normalized_name,
                arguments=arguments,
            ),
        )

    def call_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> McpToolCallResult:
        self._require_tools_capability(method="tools/call")
        if not isinstance(arguments, dict):
            raise _build_client_error(
                server=self.server,
                message="tools/call arguments must be an object.",
                stderr_tail=self._diagnostic_tail(),
                error_type=McpClientProtocolError,
            )
        try:
            return self._call_tool_once(tool_name=tool_name, arguments=arguments)
        except McpHttpTransportSessionExpiredError as exc:
            reinitialize_error: Exception | None = None
            try:
                self._reinitialize_after_session_expiry()
            except Exception as recovery_exc:  # noqa: BLE001
                if isinstance(recovery_exc, Exception):
                    reinitialize_error = recovery_exc
                else:
                    reinitialize_error = RuntimeError(str(recovery_exc))
            message = (
                "HTTP MCP session expired before or during 'tools/call'; the active call was "
                "not automatically retried because it may be side-effectful."
            )
            if reinitialize_error is not None:
                message += f" Session reinitialization also failed: {reinitialize_error}"
            raise _build_client_error(
                server=self.server,
                message=message,
                error_type=McpClientError,
            ) from exc


def _normalize_content_items(
    *,
    server: ResolvedMcpServer,
    content_items: list[Any],
    stderr_tail: str,
) -> tuple[tuple[dict[str, Any], ...], str, str]:
    normalized: list[dict[str, Any]] = []
    summaries: list[str] = []
    text_parts: list[str] = []
    for index, raw_item in enumerate(content_items):
        item = _require_object(
            raw_item,
            server=server,
            context=f"tools/call content[{index}]",
        )
        item_type = _require_string(
            item.get("type"),
            server=server,
            context=f"tools/call content[{index}].type",
        )
        if item_type == "text":
            text = _require_string(
                item.get("text"),
                server=server,
                context=f"tools/call content[{index}].text",
            )
            normalized.append({"type": "text", "text": text, "summary": f"text({len(text)} chars)"})
            summaries.append(f"text({len(text)} chars)")
            text_parts.append(text)
            continue

        # Keep bounded image content in memory for the tool binding's visual
        # channel. The binding removes these bytes before hooks or persistence.
        if item_type == "image":
            data = item.get("data")
            if isinstance(data, str) and len(data) <= _MAX_IMAGE_BASE64_CHARS:
                normalized.append(
                    {
                        "type": "image",
                        "data": data,
                        "mime_type": item.get("mimeType"),
                        "summary": "image",
                    }
                )
                summaries.append("image")
                continue

        summary = f"{item_type} item omitted"
        payload: dict[str, Any] = {
            "type": item_type,
            "summary": summary,
        }
        mime_type = item.get("mimeType")
        if isinstance(mime_type, str) and mime_type.strip():
            payload["mime_type"] = mime_type.strip()
            summary = f"{mime_type.strip()} {summary}"
            payload["summary"] = summary
        uri = item.get("uri")
        if isinstance(uri, str) and uri.strip():
            payload["uri"] = uri.strip()
        if item_type == "resource":
            resource = item.get("resource")
            if isinstance(resource, dict):
                resource_uri = resource.get("uri")
                if isinstance(resource_uri, str) and resource_uri.strip():
                    payload["resource_uri"] = resource_uri.strip()
                resource_mime = resource.get("mimeType")
                if isinstance(resource_mime, str) and resource_mime.strip():
                    payload["resource_mime_type"] = resource_mime.strip()
        normalized.append(payload)
        summaries.append(summary)
    return tuple(normalized), ", ".join(summaries) or "no content", "\n\n".join(text_parts)
