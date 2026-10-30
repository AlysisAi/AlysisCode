"""The desktop rule for the user's own MCP tool servers: every call asks a person (M2c).

The desktop app turns it on with ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL. A server's own
read-only claim grants nothing; only a tool the person marked "only reads" (stored by the
app, tied to the server's identity) runs without asking. The app-registered Computer Use
server and resource reads keep the permission-mode rules, call for call.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from test_approval_review import _Surface, _tools

from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.cli_impl.commands.mcp import _mcp_status_payload
from alysis_code.mcp.manager import McpHostToolBinding, McpToolBinding
from alysis_code.mcp.models import ResolvedMcpServer
from alysis_code.surface import StandingApproval

ENV = "ALYSIS_DESKTOP_TOOL_SERVER_APPROVAL"
RELAY = "/Applications/Alysis Code.app/Contents/Resources/computer-use/alysis-computer-mcp"
MODES = ["review", "auto", "fullaccess"]


class _Client:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def call_tool(self, *, tool_name: str, arguments: dict[str, Any]) -> SimpleNamespace:
        self.calls.append((tool_name, arguments))
        return SimpleNamespace(
            is_error=False,
            content=[{"type": "text", "text": "ok"}],
            content_summary="ok",
            structured_content=None,
            extracted_text="ok",
        )


def _server(
    server_id: str = "docs",
    *,
    command: str = "/usr/local/bin/docs-mcp",
    args: tuple[str, ...] = ("--port", "0"),
    url: str | None = None,
) -> ResolvedMcpServer:
    http = url is not None
    return ResolvedMcpServer(
        id=server_id,
        transport="http" if http else "stdio",
        enabled=True,
        enabled_in=None,
        trust="explicit",
        allowed_tools=(),
        denied_tools=(),
        startup_timeout_s=10,
        call_timeout_s=60,
        tool_prefix=None,
        command=None if http else command,
        args=() if http else args,
        url=url,
    )


def _identity(server: ResolvedMcpServer) -> str:
    """The contract the app computes too: transport, then URL or command and arguments."""
    parts = (
        [server.transport, server.url or ""]
        if server.transport == "http"
        else [server.transport, server.command or "", *server.args]
    )
    return hashlib.sha256("\0".join(parts).encode("utf-8")).hexdigest()


def _manager(
    server: ResolvedMcpServer,
    *,
    tool: str = "update",
    read_only: bool = False,
    destructive: bool = False,
    active: bool = True,
) -> tuple[SimpleNamespace, McpToolBinding, _Client]:
    client = _Client()
    binding = McpToolBinding(
        server_id=server.id,
        tool_name=tool,
        tool_alias=f"mcp__{server.id.replace('-', '_').replace('/', '_')}__{tool}",
        description="Fixture tool",
        parameters={"type": "object"},
        client=client,  # type: ignore[arg-type]
        read_only_hint=read_only,
        destructive_hint=destructive,
    )
    manager = SimpleNamespace(tool_bindings=(binding,), active_servers=(server,) if active else ())
    return manager, binding, client


def _reviewer() -> SimpleNamespace:
    return SimpleNamespace(
        bound=True,
        review=lambda *_a, **_k: pytest.fail("The AI reviewer must not decide this call"),
        record_person_decision=lambda **_k: None,
    )


@pytest.fixture
def desktop_rule(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    marks = tmp_path / "tool-server-read-marks.json"
    monkeypatch.setenv(
        ENV,
        json.dumps(
            {
                "version": 1,
                "read_marks": str(marks),
                "exempt": [{"server_id": "alysis-computer", "command": RELAY}],
            }
        ),
    )
    return marks


def _mark(marks: Path, server: ResolvedMcpServer, *tools: str, identity: str | None = None) -> None:
    marks.write_text(
        json.dumps(
            {
                "version": 1,
                "servers": {
                    server.id: {
                        "identity": identity or _identity(server),
                        "read_tools": list(tools),
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _run(tmp_path: Path, manager, binding, *, mode: str, allow: bool, **kwargs) -> _Surface:
    surface = kwargs.pop("surface", None) or _Surface(allow=allow)
    kwargs.setdefault("reviewer", None)
    tools = _tools(tmp_path, surface, mode=mode, mcp_manager=manager, **kwargs)
    try:
        tools[binding.tool_alias].run({"value": "fixture"})
    except AgentRuntimeError:
        pass
    return surface


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("claims_read_only", [False, True])
@pytest.mark.parametrize("saved_allow_rule", [False, True])
def test_every_call_to_your_tool_server_asks_a_person(
    tmp_path: Path, desktop_rule: Path, mode: str, claims_read_only: bool, saved_allow_rule: bool
):
    server = _server()
    manager, binding, client = _manager(server, read_only=claims_read_only)
    standing = StandingApproval("allow", "saved allow rule") if saved_allow_rule else None
    surface = _run(
        tmp_path,
        manager,
        binding,
        mode=mode,
        allow=False,
        surface=_Surface(allow=False, standing=standing),
        reviewer=_reviewer() if mode == "auto" else None,
    )
    [request] = surface.requests
    assert request.metadata["always_ask_person"] is True
    assert request.metadata["mandatory_explicit_approval"] is True
    assert request.metadata["tool_server_rule"] == "ask_every_call"
    assert request.metadata["server_says_read_only"] is claims_read_only
    assert client.calls == [], "A declined call must never reach the server"


@pytest.mark.parametrize("mode", MODES)
def test_an_approved_call_runs_once(tmp_path: Path, desktop_rule: Path, mode: str):
    manager, binding, client = _manager(_server())
    surface = _run(tmp_path, manager, binding, mode=mode, allow=True)
    assert len(surface.requests) == 1
    assert client.calls == [("update", {"value": "fixture"})]


@pytest.mark.parametrize("mode", MODES)
def test_a_tool_you_marked_only_reads_runs_without_asking(
    tmp_path: Path, desktop_rule: Path, mode: str
):
    server = _server()
    manager, binding, client = _manager(server, tool="search")
    _mark(desktop_rule, server, "search")
    surface = _run(tmp_path, manager, binding, mode=mode, allow=False)
    assert surface.requests == []
    assert client.calls == [("search", {"value": "fixture"})]


def test_a_mark_counts_only_for_the_tool_you_marked(tmp_path: Path, desktop_rule: Path):
    server = _server()
    manager, binding, client = _manager(server, tool="delete_all")
    _mark(desktop_rule, server, "search")
    surface = _run(tmp_path, manager, binding, mode="fullaccess", allow=False)
    assert len(surface.requests) == 1 and client.calls == []


@pytest.mark.parametrize(
    "other",
    [
        _server(command="/tmp/another-program"),
        _server(args=("--port", "0", "--write")),
        _server(url="https://other.example/mcp"),
    ],
)
def test_a_mark_for_another_program_under_the_same_name_does_not_count(
    tmp_path: Path, desktop_rule: Path, other: ResolvedMcpServer
):
    server = _server()
    manager, binding, client = _manager(server, tool="search")
    _mark(desktop_rule, server, "search", identity=_identity(other))
    surface = _run(tmp_path, manager, binding, mode="fullaccess", allow=False)
    assert len(surface.requests) == 1 and client.calls == []


def test_an_http_server_mark_is_tied_to_its_url(tmp_path: Path, desktop_rule: Path):
    server = _server("remote", url="https://tools.example/mcp")
    manager, binding, client = _manager(server, tool="search")
    _mark(desktop_rule, server, "search")
    surface = _run(tmp_path, manager, binding, mode="review", allow=False)
    assert surface.requests == [] and len(client.calls) == 1


@pytest.mark.parametrize(
    "problem", ["missing", "not-json", "wrong-version", "oversized", "symlink", "directory"]
)
def test_a_marks_file_that_cannot_be_trusted_means_every_call_asks(
    tmp_path: Path, desktop_rule: Path, problem: str
):
    server = _server()
    manager, binding, client = _manager(server, tool="search")
    if problem != "missing":
        real = tmp_path / "real-marks.json"
        _mark(real, server, "search")
        if problem == "not-json":
            desktop_rule.write_text("{not json", encoding="utf-8")
        elif problem == "wrong-version":
            payload = json.loads(real.read_text(encoding="utf-8"))
            payload["version"] = 2
            desktop_rule.write_text(json.dumps(payload), encoding="utf-8")
        elif problem == "oversized":
            payload = json.loads(real.read_text(encoding="utf-8"))
            payload["padding"] = "x" * (300 * 1024)
            desktop_rule.write_text(json.dumps(payload), encoding="utf-8")
        elif problem == "symlink":
            os.symlink(real, desktop_rule)
        else:
            desktop_rule.mkdir()
    surface = _run(tmp_path, manager, binding, mode="fullaccess", allow=False)
    assert len(surface.requests) == 1 and client.calls == []


@pytest.mark.parametrize("mode", MODES)
def test_nobody_to_ask_means_the_call_is_not_dispatched(
    tmp_path: Path, desktop_rule: Path, mode: str
):
    class _NobodyToAsk(_Surface):
        host_managed_approvals = False

    manager, binding, client = _manager(_server())
    tools = _tools(
        tmp_path,
        _NobodyToAsk(allow=True),
        mode=mode,
        reviewer=None,
        mcp_manager=manager,
        non_interactive=True,
    )
    with pytest.raises(AgentRuntimeError) as caught:
        tools[binding.tool_alias].run({"value": "fixture"})
    assert caught.value.result_payload["dispatch_state"] == "not_dispatched"
    assert client.calls == []


@pytest.mark.parametrize(
    "server",
    [
        _server("acme.tools/search"),  # owned by a plugin installed from the CLI
        _server("alysis-computer", command="/tmp/not-the-app-relay"),
        _server("alysis-computer", url="https://relay.example/mcp"),  # the exemption is stdio only
    ],
)
def test_other_servers_ask_too(tmp_path: Path, desktop_rule: Path, server: ResolvedMcpServer):
    manager, binding, client = _manager(server)
    surface = _run(tmp_path, manager, binding, mode="fullaccess", allow=False)
    assert len(surface.requests) == 1 and client.calls == []


def test_a_server_the_engine_cannot_find_asks(tmp_path: Path, desktop_rule: Path):
    manager, binding, client = _manager(_server(), active=False)
    surface = _run(tmp_path, manager, binding, mode="fullaccess", allow=False)
    assert len(surface.requests) == 1 and client.calls == []


def test_an_unreadable_setting_keeps_the_rule_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(ENV, "{not json")
    manager, binding, client = _manager(_server())
    surface = _run(tmp_path, manager, binding, mode="fullaccess", allow=False)
    assert len(surface.requests) == 1 and client.calls == []


def test_the_card_carries_the_rule_and_the_redacted_arguments(tmp_path: Path, desktop_rule: Path):
    manager, binding, _client = _manager(_server(), read_only=True)
    surface = _Surface(allow=False)
    tools = _tools(tmp_path, surface, mode="review", reviewer=None, mcp_manager=manager)
    with pytest.raises(AgentRuntimeError):
        tools[binding.tool_alias].run({"query": "plans", "password": "fixture-sensitive-value"})
    [request] = surface.requests
    assert request.preview.startswith("Run MCP tool\nserver: docs\ntool: update\nargs:\n")
    assert '"query": "plans"' in request.preview
    assert "fixture-sensitive-value" not in request.preview
    assert "before every call" in request.reason


def _outcome(tmp_path: Path, manager_factory, *, mode: str, allow: bool, reviewer=None):
    manager, binding, client = manager_factory()
    surface = _Surface(allow=allow)
    tools = _tools(tmp_path, surface, mode=mode, mcp_manager=manager, reviewer=reviewer)
    error = None
    try:
        tools[binding.tool_alias].run({"value": "fixture"})
    except AgentRuntimeError as exc:
        error = (type(exc).__name__, str(exc))
    return (
        [(r.kind, r.reason, r.preview, r.command, r.metadata) for r in surface.requests],
        client.calls if hasattr(client, "calls") else client,
        error,
    )


def _resource_manager():
    calls: list[dict[str, Any]] = []
    binding = McpHostToolBinding(
        "mcp_resource_read",
        "mcp_resource_read",
        "Read a snapshotted resource",
        {"type": "object"},
        lambda args: calls.append(args) or {"ok": True},
    )
    return SimpleNamespace(tool_bindings=(binding,), active_servers=()), binding, calls


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("allow", [True, False])
@pytest.mark.parametrize(
    "factory",
    [
        pytest.param(
            lambda: _manager(_server("alysis-computer", command=RELAY, args=("/tmp/s.sock",))),
            id="computer-use",
        ),
        pytest.param(
            lambda: _manager(
                _server("alysis-computer", command=RELAY, args=("/tmp/s.sock",)), read_only=True
            ),
            id="computer-use-read",
        ),
        pytest.param(
            lambda: _manager(
                _server("alysis-computer", command=RELAY, args=("/tmp/s.sock",)), destructive=True
            ),
            id="computer-use-destructive",
        ),
        pytest.param(_resource_manager, id="resource-read"),
    ],
)
def test_computer_use_and_resource_reads_keep_todays_rules_call_for_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str, allow: bool, factory
):
    monkeypatch.delenv(ENV, raising=False)
    before = _outcome(tmp_path, factory, mode=mode, allow=allow)
    monkeypatch.setenv(
        ENV,
        json.dumps(
            {
                "version": 1,
                "read_marks": str(tmp_path / "marks.json"),
                "exempt": [{"server_id": "alysis-computer", "command": RELAY}],
            }
        ),
    )
    assert _outcome(tmp_path, factory, mode=mode, allow=allow) == before


@pytest.mark.parametrize("mode", MODES)
def test_without_the_desktop_rule_nothing_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
):
    monkeypatch.delenv(ENV, raising=False)
    manager, binding, client = _manager(_server(), read_only=True)
    surface = _run(tmp_path, manager, binding, mode=mode, allow=False)
    assert surface.requests == []
    assert len(client.calls) == 1


class _StatusManager:
    def __init__(self, *pairs: tuple[ResolvedMcpServer, list[McpToolBinding]]) -> None:
        self.active_servers = tuple(server for server, _ in pairs)
        self.tool_bindings = tuple(binding for _, bindings in pairs for binding in bindings)

    def catalog_snapshot_metadata(self) -> dict[str, Any]:
        return {
            "server_catalogs": [
                {"server_id": server.id, "exposed_tool_count": 1} for server in self.active_servers
            ]
        }

    def server_startup_errors(self) -> dict[str, str]:
        return {}


def _binding(server: ResolvedMcpServer, tool: str, **hints: bool) -> McpToolBinding:
    return _manager(server, tool=tool, **hints)[1]


def test_mcp_status_reports_what_the_engine_will_do(desktop_rule: Path):
    docs = _server()
    computer = _server("alysis-computer", command=RELAY, args=("/tmp/s.sock",))
    _mark(desktop_rule, docs, "search")
    payload = _mcp_status_payload(
        manager=_StatusManager(
            (
                docs,
                [
                    _binding(docs, "search", read_only=True),
                    _binding(docs, "drop", destructive=True),
                ],
            ),
            (computer, [_binding(computer, "computer_action")]),
        ),
        bootstrap_errors=[],
        prompt_errors={},
    )
    assert payload["tool_server_approval"] == {"version": 1, "active": True}
    rows = {row["server_id"]: row for row in payload["rows"]}
    assert rows["docs"]["call_approval"] == "every_call"
    assert rows["docs"]["tools"] == [
        {
            "name": "drop",
            "server_says_read_only": False,
            "server_says_destructive": True,
            "marked_read_by_you": False,
        },
        {
            "name": "search",
            "server_says_read_only": True,
            "server_says_destructive": False,
            "marked_read_by_you": True,
        },
    ]
    assert rows["alysis-computer"]["call_approval"] == "permission_mode"


def test_mcp_status_without_the_desktop_rule(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(ENV, raising=False)
    docs = _server()
    payload = _mcp_status_payload(
        manager=_StatusManager((docs, [_binding(docs, "search", read_only=True)])),
        bootstrap_errors=[],
        prompt_errors={},
    )
    assert payload["tool_server_approval"] == {"version": 1, "active": False}
    [row] = payload["rows"]
    assert row["call_approval"] == "permission_mode"
    assert row["tools"][0]["marked_read_by_you"] is False


@pytest.mark.parametrize(
    "server,expected",
    [
        (_server(), "bfc3ab50be10c4d10563e820de9b9f9ab92fac9ac0e0381df7cd151be1863f05"),
        (
            _server("remote", url="https://tools.example/mcp"),
            "d192e85a70e049a9e616e118d97e749de88b1ed06880f1c2589b566e2b25e172",
        ),
        (
            _server(command="/opt/ünïcode/srv", args=("--name", "日本")),
            "8cfe485644d0995a1e0df9c210c4951c8a53c42279574a13d69e9866f623938f",
        ),
    ],
)
def test_the_identity_matches_the_app(server: ResolvedMcpServer, expected: str):
    """The same vectors are pinned in the app's tests/tool-server-approval.test.ts."""
    from alysis_code.mcp.person_approval import server_identity

    assert server_identity(server) == expected
