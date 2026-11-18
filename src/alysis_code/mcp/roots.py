from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..runtime_kind import RuntimeKind, normalize_runtime_kind
from .models import ResolvedMcpServer, RootsMode
from .server_requests import (
    McpInvalidServerRequestParamsError,
    McpServerRequestContext,
    McpServerRequestHandler,
    McpUnsupportedServerRequestError,
)

_ROOTS_ENABLED_RUNTIME_KINDS = frozenset(
    {
        RuntimeKind.INTERACTIVE_CHAT,
        RuntimeKind.ONE_SHOT,
    }
)


@dataclass(frozen=True)
class McpRootDescriptor:
    uri: str
    name: str

    def as_payload(self) -> dict[str, str]:
        return {
            "uri": self.uri,
            "name": self.name,
        }


def roots_runtime_supported(runtime_kind: RuntimeKind | str) -> bool:
    return normalize_runtime_kind(runtime_kind) in _ROOTS_ENABLED_RUNTIME_KINDS


def roots_capability_enabled(
    *,
    roots_mode: RootsMode,
    runtime_kind: RuntimeKind | str,
) -> bool:
    return roots_mode == "workspace" and roots_runtime_supported(runtime_kind)


def _workspace_root_name(workspace_root: Path) -> str:
    resolved = workspace_root.resolve()
    return resolved.name or resolved.anchor or resolved.as_posix() or "/"


def build_workspace_root_descriptor(workspace_root: Path) -> McpRootDescriptor:
    resolved = workspace_root.resolve()
    return McpRootDescriptor(
        uri=resolved.as_uri(),
        name=_workspace_root_name(resolved),
    )


def build_roots_list_result(
    *,
    roots_mode: RootsMode,
    workspace_root: Path,
    runtime_kind: RuntimeKind | str,
) -> dict[str, Any]:
    if not roots_capability_enabled(roots_mode=roots_mode, runtime_kind=runtime_kind):
        raise McpUnsupportedServerRequestError(method="roots/list")
    root = build_workspace_root_descriptor(workspace_root)
    return {"roots": [root.as_payload()]}


class McpRootsRequestHandler(McpServerRequestHandler):
    def __init__(self, *, server: ResolvedMcpServer) -> None:
        self._server = server

    def client_capabilities(self, *, context: McpServerRequestContext) -> dict[str, Any]:
        if roots_capability_enabled(
            roots_mode=self._server.roots_mode,
            runtime_kind=context.runtime_kind,
        ):
            return {"roots": {}}
        return {}

    def handle_request(
        self,
        *,
        context: McpServerRequestContext,
        method: str,
        request_id: int | str,
        params: Any | None,
    ) -> dict[str, Any]:
        del request_id
        if method != "roots/list":
            raise McpUnsupportedServerRequestError(method=method)
        if params is not None and not isinstance(params, dict):
            raise McpInvalidServerRequestParamsError(
                method=method,
                detail="Expected an object when params is present.",
            )
        return build_roots_list_result(
            roots_mode=self._server.roots_mode,
            workspace_root=context.workspace_root,
            runtime_kind=context.runtime_kind,
        )
