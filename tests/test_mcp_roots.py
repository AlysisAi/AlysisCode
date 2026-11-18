from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.mcp.models import ResolvedMcpServer
from alysis_code.mcp.roots import (
    McpRootsRequestHandler,
    build_workspace_root_descriptor,
)
from alysis_code.mcp.server_requests import (
    McpServerRequestContext,
    McpUnsupportedServerRequestError,
)
from alysis_code.runtime_kind import RuntimeKind


def _server(*, roots_mode: str = "disabled") -> ResolvedMcpServer:
    return ResolvedMcpServer(
        id="alpha",
        transport="stdio",
        enabled=True,
        enabled_in=None,
        trust="explicit",
        allowed_tools=(),
        denied_tools=(),
        startup_timeout_s=10.0,
        call_timeout_s=60.0,
        tool_prefix=None,
        roots_mode=roots_mode,
        command="tool",
    )


def _context(workspace_root: Path, *, runtime_kind: RuntimeKind) -> McpServerRequestContext:
    return McpServerRequestContext.create(
        server_id="alpha",
        workspace_root=workspace_root,
        runtime_kind=runtime_kind,
        fallback_runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )


def test_workspace_root_descriptor_uses_file_uri_and_stable_name(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    descriptor = build_workspace_root_descriptor(workspace)

    assert descriptor.uri == workspace.resolve().as_uri()
    assert descriptor.name == "workspace"


@pytest.mark.parametrize(
    ("runtime_kind", "expected_enabled"),
    [
        (RuntimeKind.INTERACTIVE_CHAT, True),
        (RuntimeKind.ONE_SHOT, True),
        (RuntimeKind.SUBAGENT, False),
    ],
)
def test_roots_request_handler_advertises_capability_only_in_supported_runtimes(
    tmp_path: Path,
    runtime_kind: RuntimeKind,
    expected_enabled: bool,
) -> None:
    handler = McpRootsRequestHandler(server=_server(roots_mode="workspace"))

    capabilities = handler.client_capabilities(
        context=_context(tmp_path, runtime_kind=runtime_kind)
    )

    assert ("roots" in capabilities) is expected_enabled
    if expected_enabled:
        assert capabilities["roots"] == {}
        assert "listChanged" not in capabilities["roots"]
    else:
        assert capabilities == {}


def test_roots_request_handler_returns_workspace_root_result(tmp_path: Path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    handler = McpRootsRequestHandler(server=_server(roots_mode="workspace"))

    result = handler.handle_request(
        context=_context(workspace, runtime_kind=RuntimeKind.INTERACTIVE_CHAT),
        method="roots/list",
        request_id="server-request-1",
        params={},
    )

    assert result == {
        "roots": [
            {
                "uri": workspace.resolve().as_uri(),
                "name": "repo",
            }
        ]
    }


@pytest.mark.parametrize(
    ("roots_mode", "runtime_kind"),
    [
        ("disabled", RuntimeKind.INTERACTIVE_CHAT),
        ("workspace", RuntimeKind.SUBAGENT),
    ],
)
def test_roots_request_handler_rejects_disabled_or_unsupported_roots(
    tmp_path: Path,
    roots_mode: str,
    runtime_kind: RuntimeKind,
) -> None:
    handler = McpRootsRequestHandler(server=_server(roots_mode=roots_mode))

    with pytest.raises(McpUnsupportedServerRequestError):
        handler.handle_request(
            context=_context(tmp_path, runtime_kind=runtime_kind),
            method="roots/list",
            request_id="server-request-1",
            params={},
        )


def test_roots_request_handler_rejects_other_server_request_methods(tmp_path: Path) -> None:
    handler = McpRootsRequestHandler(server=_server(roots_mode="workspace"))

    with pytest.raises(McpUnsupportedServerRequestError):
        handler.handle_request(
            context=_context(tmp_path, runtime_kind=RuntimeKind.INTERACTIVE_CHAT),
            method="sampling/createMessage",
            request_id="server-request-1",
            params={},
        )
