from __future__ import annotations

import io
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from rich.console import Console

import alysis_code.agent_loop as agent_loop_mod
from alysis_code.agent.errors import AgentRuntimeError
from alysis_code.agent_loop import _tool_event_metadata, build_tools
from alysis_code.config import AppConfig
from alysis_code.custom_tools.discovery import discover_custom_tools
from alysis_code.custom_tools.trust import trust_project_tool
from alysis_code.mcp.manager import (
    McpHostToolBinding,
    McpToolBinding,
    mcp_tool_approval_hints,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.session_store import SessionStore
from alysis_code.surface import ApprovalDecision, ApprovalRequest, NoopSurface


def _store(root: Path, *, enabled: bool = True) -> SessionStore:
    return SessionStore(
        enabled=enabled,
        sessions_dir=root / "sessions",
        session_id="tool-exposure-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _fake_git_repo(root: Path) -> None:
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("0" * 40 + "\n", encoding="utf-8")


class _FakeMcpClient:
    def __init__(self) -> None:
        self.called = False

    def call_tool(self, *, tool_name: str, arguments: dict[str, object]) -> SimpleNamespace:
        self.called = True
        return SimpleNamespace(
            is_error=False,
            content=[{"type": "text", "text": f"ok:{tool_name}:{arguments!r}"}],
            content_summary="ok",
            structured_content=None,
            extracted_text="ok",
        )


class _DummyMcpManager:
    def __init__(self, *bindings: McpToolBinding) -> None:
        self.tool_bindings = tuple(bindings)


class _RecordingSurface(NoopSurface):
    def __init__(self, *, allow: bool = True) -> None:
        super().__init__()
        self.allow = allow
        self.requests: list[ApprovalRequest] = []

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(allow=self.allow)


def _make_mcp_binding(
    *,
    session_mode: str | None = None,
    read_only_hint: bool = False,
    destructive_hint: bool = False,
) -> tuple[McpToolBinding, _FakeMcpClient]:
    client = _FakeMcpClient()
    binding = McpToolBinding(
        server_id="alpha",
        tool_name="echo",
        tool_alias="mcp__alpha__echo",
        description="Echo via MCP",
        parameters={"type": "object", "properties": {}, "required": []},
        client=client,
        session_mode=session_mode,
        read_only_hint=read_only_hint,
        destructive_hint=destructive_hint,
    )
    return binding, client


def _make_mcp_host_binding(
    *,
    session_mode: str | None = None,
) -> tuple[McpHostToolBinding, dict[str, bool]]:
    state = {"called": False}

    def _run_handler(_arguments: dict[str, object]) -> dict[str, object]:
        state["called"] = True
        return {"ok": True}

    binding = McpHostToolBinding(
        tool_name="mcp_resource_read",
        tool_alias="mcp_resource_read",
        description="Read resource via MCP",
        parameters={"type": "object", "properties": {}, "required": []},
        run_handler=_run_handler,
        session_mode=session_mode,
    )
    return binding, state


def _write_project_custom_tool(
    root: Path,
    *,
    name: str = "project_echo",
    body: str = "return {'ok': True}",
    extra_manifest_lines: list[str] | None = None,
) -> None:
    extra_manifest_lines = list(extra_manifest_lines or [])
    tool_path = root / ".alysis" / "tools" / f"{name}.py"
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_lines = [
        "TOOL = {",
        f'    "name": "{name}",',
        '    "description": "Project custom tool",',
        '    "input_schema": {"type": "object", "properties": {}, "required": []},',
    ]
    manifest_lines.extend(f"    {line}" for line in extra_manifest_lines)
    manifest_lines.append("}")
    tool_path.write_text(
        "\n".join(
            [
                *manifest_lines,
                "",
                "def run(args):",
                f"    {body}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _trust_project_custom_tool(root: Path) -> None:
    discovered = discover_custom_tools(
        workspace_root=root,
        built_in_tool_names=set(),
    )
    [spec] = discovered.project_tools
    trust_project_tool(spec)


def _build_tools(
    tmp_path: Path,
    *,
    mode: str,
    runtime_kind: RuntimeKind = RuntimeKind.INTERACTIVE_CHAT,
    deny_write_prefixes: list[str] | None = None,
    allow_write_globs: list[str] | None = None,
    mcp_manager: object | None = None,
    non_interactive: bool = True,
    surface: object | None = None,
    cfg: AppConfig | None = None,
    yes: bool = True,
    subagent_depth: int = 0,
    store: object | None = None,
    skills_enabled: bool = True,
    subagent_coordinator: object | None = None,
    subagent_coordinator_sink: object | None = None,
    child_scheduler_sink: object | None = None,
) -> dict[str, object]:
    _fake_git_repo(tmp_path)
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        surface=surface,
        store=store or _store(tmp_path, enabled=True),  # type: ignore[arg-type]
        mode=mode,
        yes=yes,
        cfg=cfg
        or AppConfig(
            model="test-model",
            base_url="https://api.openai.com/v1",
            web_search_mode="auto",
        ),
        api_key="main-key",
        non_interactive=non_interactive,
        deny_write_prefixes=deny_write_prefixes,
        allow_write_globs=allow_write_globs,
        verification_enabled=True,
        skills_enabled=skills_enabled,
        subagents_enabled=True,
        subagent_depth=subagent_depth,
        subagent_registry={},
        runtime_kind=runtime_kind,
        mcp_manager=mcp_manager,  # type: ignore[arg-type]
        subagent_coordinator=subagent_coordinator,  # type: ignore[arg-type]
        subagent_coordinator_sink=subagent_coordinator_sink,  # type: ignore[arg-type]
        child_scheduler_sink=child_scheduler_sink,  # type: ignore[arg-type]
    )


def test_build_tools_readonly_excludes_mutating_and_higher_risk_tools(tmp_path: Path) -> None:
    binding, _client = _make_mcp_binding()
    tools = _build_tools(
        tmp_path,
        mode="readonly",
        mcp_manager=_DummyMcpManager(binding),
    )

    for name in (
        "fs_write",
        "fs_edit",
        "fs_move",
        "fs_copy",
        "fs_delete",
        "fs_mkdir",
        "shell_run",
        "verify_run",
        "git_apply_patch",
        "mcp__alpha__echo",
    ):
        assert name not in tools

    for name in (
        "subagent_run",
        "subagent_spawn",
        "subagent_send",
        "subagent_resume",
        "subagent_status",
        "subagent_wait",
        "subagent_cancel",
    ):
        assert name in tools
    assert "subagent_apply" not in tools
    assert "subagent_discard" not in tools


def test_build_tools_nested_readonly_does_not_expose_delegation(
    tmp_path: Path,
) -> None:
    tools = _build_tools(tmp_path, mode="readonly", subagent_depth=1)

    assert not {name for name in tools if name.startswith("subagent_")}


def test_live_tool_rebuild_reuses_session_subagent_coordinator(
    tmp_path: Path,
) -> None:
    session_store = _store(tmp_path, enabled=True)
    coordinators: list[object] = []
    schedulers: list[object] = []
    first = _build_tools(
        tmp_path,
        mode="review",
        store=session_store,
        subagent_coordinator_sink=coordinators.append,
        child_scheduler_sink=schedulers.append,
    )
    coordinator = coordinators[0]
    scheduler = schedulers[0]
    first_status = first["subagent_status"]
    first_spawn = first["subagent_spawn"]

    second = _build_tools(
        tmp_path,
        mode="readonly",
        store=session_store,
        skills_enabled=False,
        subagent_coordinator=coordinator,
        subagent_coordinator_sink=coordinators.append,
        child_scheduler_sink=schedulers.append,
    )
    try:
        assert coordinators == [coordinator, coordinator]
        assert schedulers == [scheduler, scheduler]
        assert coordinator.registry is scheduler.registry  # type: ignore[attr-defined]
        assert scheduler._closed is False  # type: ignore[attr-defined]  # noqa: SLF001
        assert first_status.run({}) == second["subagent_status"].run({})
        stale_spawn_result = first_spawn.run(
            {"name": "missing-role", "task": "Exercise the captured tool closure."}
        )
        assert stale_spawn_result["error_code"] == "unknown_subagent"
        assert "closed" not in str(stale_spawn_result["error"]).casefold()
        assert coordinator.launcher.skills_enabled is False  # type: ignore[attr-defined]
    finally:
        coordinator.shutdown(cancel_pending=True)  # type: ignore[attr-defined]


def test_failed_tool_rebuild_does_not_rebind_session_subagent_coordinator(
    tmp_path: Path,
) -> None:
    class _FailingBinding:
        def bind_session_mode(self, _mode: str) -> object:
            raise RuntimeError("late MCP binding failure")

    session_store = _store(tmp_path, enabled=True)
    coordinators: list[object] = []
    first = _build_tools(
        tmp_path,
        mode="review",
        store=session_store,
        subagent_coordinator_sink=coordinators.append,
    )
    coordinator = coordinators[0]
    original_launcher = coordinator.launcher  # type: ignore[attr-defined]
    old_spawn = first["subagent_spawn"]
    failed_publish: list[object] = []
    try:
        with pytest.raises(RuntimeError, match="late MCP binding failure"):
            _build_tools(
                tmp_path,
                mode="auto",
                store=session_store,
                mcp_manager=_DummyMcpManager(_FailingBinding()),  # type: ignore[arg-type]
                subagent_coordinator=coordinator,
                subagent_coordinator_sink=failed_publish.append,
            )

        assert failed_publish == []
        assert coordinator.launcher is original_launcher  # type: ignore[attr-defined]
        stale_result = old_spawn.run(
            {"name": "missing-role", "task": "Use the last committed launcher."}
        )
        assert stale_result["error_code"] == "unknown_subagent"
        assert "closed" not in str(stale_result["error"]).casefold()
    finally:
        coordinator.shutdown(cancel_pending=True)  # type: ignore[attr-defined]


def test_failed_schema_conversion_does_not_rebind_session_subagent_coordinator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_store = _store(tmp_path, enabled=True)
    coordinators: list[object] = []
    first = _build_tools(
        tmp_path,
        mode="review",
        store=session_store,
        subagent_coordinator_sink=coordinators.append,
    )
    coordinator = coordinators[0]
    original_launcher = coordinator.launcher  # type: ignore[attr-defined]
    tool_type = type(first["subagent_spawn"])
    original_conversion = tool_type.as_openai_tool

    def _fail_late_schema_conversion(tool: object) -> dict[str, object]:
        if getattr(tool, "name", "") == "subagent_wait":
            raise RuntimeError("late schema conversion failure")
        return original_conversion(tool)

    monkeypatch.setattr(tool_type, "as_openai_tool", _fail_late_schema_conversion)
    failed_publish: list[object] = []
    try:
        with pytest.raises(RuntimeError, match="late schema conversion failure"):
            _build_tools(
                tmp_path,
                mode="auto",
                store=session_store,
                subagent_coordinator=coordinator,
                subagent_coordinator_sink=failed_publish.append,
            )

        assert failed_publish == []
        assert coordinator.launcher is original_launcher  # type: ignore[attr-defined]
    finally:
        coordinator.shutdown(cancel_pending=True)  # type: ignore[attr-defined]


def test_live_tool_rebuild_commits_background_cap_changes(tmp_path: Path) -> None:
    session_store = _store(tmp_path, enabled=True)
    coordinators: list[object] = []
    initial_cfg = AppConfig(model="test-model", web_search_mode="off")
    initial_cfg.subagent_orchestration.max_background_children = 1
    _build_tools(
        tmp_path,
        mode="review",
        store=session_store,
        cfg=initial_cfg,
        subagent_coordinator_sink=coordinators.append,
    )
    coordinator = coordinators[0]
    try:
        expanded_cfg = initial_cfg.model_copy(deep=True)
        expanded_cfg.subagent_orchestration.max_background_children = 5
        _build_tools(
            tmp_path,
            mode="review",
            store=session_store,
            cfg=expanded_cfg,
            subagent_coordinator=coordinator,
        )
        assert coordinator.max_background_children == 5  # type: ignore[attr-defined]
        assert coordinator._executor_worker_capacity >= 5  # type: ignore[attr-defined]  # noqa: SLF001

        reduced_cfg = expanded_cfg.model_copy(deep=True)
        reduced_cfg.subagent_orchestration.max_background_children = 2
        _build_tools(
            tmp_path,
            mode="review",
            store=session_store,
            cfg=reduced_cfg,
            subagent_coordinator=coordinator,
        )
        assert coordinator.max_background_children == 2  # type: ignore[attr-defined]
    finally:
        coordinator.shutdown(cancel_pending=True)  # type: ignore[attr-defined]


def test_live_tool_rebuild_can_enable_workspace_isolation(
    tmp_path: Path,
) -> None:
    session_store = _store(tmp_path, enabled=True)
    coordinators: list[object] = []
    disabled_cfg = AppConfig(model="test-model", web_search_mode="off")
    disabled_cfg.subagent_orchestration.workspace_isolation_enabled = False
    disabled_tools = _build_tools(
        tmp_path,
        mode="review",
        store=session_store,
        cfg=disabled_cfg,
        subagent_coordinator_sink=coordinators.append,
    )
    coordinator = coordinators[0]
    workspace_provider = coordinator.workspace_provider  # type: ignore[attr-defined]
    try:
        assert workspace_provider is not None
        assert "subagent_apply" not in disabled_tools
        assert "subagent_discard" not in disabled_tools

        enabled_cfg = disabled_cfg.model_copy(deep=True)
        enabled_cfg.subagent_orchestration.workspace_isolation_enabled = True
        enabled_tools = _build_tools(
            tmp_path,
            mode="review",
            store=session_store,
            cfg=enabled_cfg,
            subagent_coordinator=coordinator,
        )

        assert coordinator.workspace_provider is workspace_provider  # type: ignore[attr-defined]
        assert "subagent_apply" in enabled_tools
        assert "subagent_discard" in enabled_tools
    finally:
        coordinator.shutdown(cancel_pending=True)  # type: ignore[attr-defined]


def test_build_tools_readonly_keeps_expected_read_safe_inspection_tools(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path, mode="readonly")

    for name in (
        "fs_read",
        "fs_list",
        "search_rg",
        "symbol_search",
        "repo_map",
        "history_search",
        "session_artifact_read",
        "web_fetch",
        "web_search",
        "git_status",
        "git_diff",
        "git_history",
        "web_fetch",
        "web_search",
    ):
        assert name in tools

    assert "fs_read_lines" not in tools


def test_build_tools_readonly_does_not_construct_shell_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_build_shell_runner(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("readonly mode should not construct a shell runner")

    monkeypatch.setattr(agent_loop_mod, "build_shell_runner", fail_build_shell_runner)

    tools = _build_tools(tmp_path, mode="readonly")

    assert "shell_run" not in tools


@pytest.mark.parametrize("mode", ["review", "auto", "fullaccess"])
def test_build_tools_write_capable_modes_keep_expected_builtin_and_mcp_surface(
    tmp_path: Path,
    mode: str,
) -> None:
    binding, _client = _make_mcp_binding()
    tools = _build_tools(
        tmp_path,
        mode=mode,
        mcp_manager=_DummyMcpManager(binding),
    )

    for name in (
        "fs_write",
        "fs_edit",
        "fs_move",
        "fs_copy",
        "fs_delete",
        "fs_mkdir",
        "shell_run",
        "verify_run",
        "git_apply_patch",
        "subagent_run",
        "web_fetch",
        "web_search",
        "mcp__alpha__echo",
    ):
        assert name in tools


def test_auto_mode_fs_delete_requires_approval_when_not_yes(tmp_path: Path) -> None:
    target = tmp_path / "old.txt"
    target.write_text("old\n", encoding="utf-8")
    surface = _RecordingSurface(allow=True)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        non_interactive=False,
        surface=surface,
        yes=False,
    )

    result = tools["fs_delete"].run({"path": "old.txt"})

    assert result["deleted"] is True
    assert target.exists() is False
    assert [request.kind for request in surface.requests] == ["fs_delete"]
    assert "file deletion" in surface.requests[0].reason
    assert surface.requests[0].files == ["old.txt"]


def test_mcp_tool_binding_rejects_readonly_session_mode_before_server_call() -> None:
    binding, client = _make_mcp_binding(session_mode="readonly")

    with pytest.raises(RuntimeError, match="Blocked in readonly mode: MCP tool"):
        binding.run({})

    assert client.called is False


def test_mcp_host_tool_binding_rejects_readonly_session_mode_before_handler() -> None:
    binding, state = _make_mcp_host_binding(session_mode="readonly")

    with pytest.raises(RuntimeError, match="Blocked in readonly mode: MCP tool"):
        binding.run({})

    assert state["called"] is False


def test_build_tools_readonly_excludes_trusted_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="readonly",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )

    assert "project_echo" not in tools


@pytest.mark.parametrize(
    "runtime_kind",
    [
        RuntimeKind.INTERACTIVE_CHAT,
        RuntimeKind.ONE_SHOT,
        RuntimeKind.FORGE_EXEC,
    ],
)
def test_build_tools_top_level_write_runtimes_expose_trusted_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: RuntimeKind,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=runtime_kind,
    )

    assert "project_echo" in tools


def test_build_tools_exposes_custom_tool_capability_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(
        tmp_path,
        extra_manifest_lines=[
            '"capabilities": {',
            '    "read_only": True,',
            '    "network_access": "none",',
            '    "filesystem": {"read": "workspace", "write": "none"},',
            "},",
            '"output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},',
        ],
    )
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
    )

    metadata = tools["project_echo"].metadata
    assert metadata["tool_type"] == "custom_tool"
    custom_tool = metadata["custom_tool"]
    assert custom_tool["manifest_version"] == 1
    assert custom_tool["has_output_schema"] is True
    assert custom_tool["capabilities"]["read_only"] is True
    assert custom_tool["capabilities"]["filesystem_read_scope"] == "workspace"
    event_metadata = _tool_event_metadata(tools["project_echo"])
    assert event_metadata["tool_type"] == "custom_tool"
    assert event_metadata["custom_tool"]["capabilities"]["read_only"] is True
    assert "output_schema" not in event_metadata["custom_tool"]


def test_model_facing_custom_and_mcp_tool_schemas_strip_descriptive_bloat(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(
        tmp_path,
        extra_manifest_lines=[
            '"input_schema": {',
            '    "type": "object",',
            '    "description": "CUSTOM_ROOT_DESCRIPTION",',
            '    "properties": {',
            '        "mode": {',
            '            "type": "string",',
            '            "description": "CUSTOM_MODE_DESCRIPTION",',
            '            "markdownDescription": "CUSTOM_MARKDOWN_DESCRIPTION",',
            '            "title": "CUSTOM_TITLE",',
            '            "examples": ["CUSTOM_EXAMPLE"],',
            '            "default": "CUSTOM_DEFAULT",',
            '            "enum": ["fast", "safe"],',
            "        },",
            "    },",
            '    "required": ["mode"],',
            "},",
        ],
    )
    _trust_project_custom_tool(tmp_path)
    binding, _client = _make_mcp_binding()
    binding = McpToolBinding(
        server_id=binding.server_id,
        tool_name=binding.tool_name,
        tool_alias=binding.tool_alias,
        description="MCP echo " + ("detail " * 500),
        parameters={
            "type": "object",
            "description": "MCP_ROOT_DESCRIPTION",
            "properties": {
                "text": {
                    "type": "string",
                    "description": "MCP_TEXT_DESCRIPTION",
                    "default": "MCP_DEFAULT",
                }
            },
            "required": ["text"],
        },
        client=_client,
    )

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        mcp_manager=_DummyMcpManager(binding),
    )

    custom_tool = tools["project_echo"]
    custom_payload = custom_tool.as_openai_tool()  # type: ignore[attr-defined]
    custom_schema = custom_payload["function"]["parameters"]
    custom_serialized = json.dumps(custom_schema, ensure_ascii=True, sort_keys=True)
    assert "CUSTOM_ROOT_DESCRIPTION" not in custom_serialized
    assert "CUSTOM_MODE_DESCRIPTION" not in custom_serialized
    assert "CUSTOM_MARKDOWN_DESCRIPTION" not in custom_serialized
    assert "CUSTOM_TITLE" not in custom_serialized
    assert "CUSTOM_EXAMPLE" not in custom_serialized
    assert "CUSTOM_DEFAULT" not in custom_serialized
    assert custom_schema["properties"]["mode"]["enum"] == ["fast", "safe"]
    assert "CUSTOM_MODE_DESCRIPTION" in json.dumps(
        custom_tool.parameters,
        ensure_ascii=True,
        sort_keys=True,  # type: ignore[attr-defined]
    )
    assert custom_payload["function"]["description"] == "Project custom tool"

    mcp_tool = tools["mcp__alpha__echo"]
    mcp_payload = mcp_tool.as_openai_tool()  # type: ignore[attr-defined]
    mcp_schema = mcp_payload["function"]["parameters"]
    mcp_serialized = json.dumps(mcp_schema, ensure_ascii=True, sort_keys=True)
    assert "MCP_ROOT_DESCRIPTION" not in mcp_serialized
    assert "MCP_TEXT_DESCRIPTION" not in mcp_serialized
    assert "MCP_DEFAULT" not in mcp_serialized
    assert "MCP_TEXT_DESCRIPTION" in json.dumps(
        mcp_tool.parameters,
        ensure_ascii=True,
        sort_keys=True,  # type: ignore[attr-defined]
    )
    assert 800 < len(mcp_payload["function"]["description"]) <= 1000
    assert _tool_event_metadata(mcp_tool)["tool_type"] == "mcp"  # type: ignore[arg-type]


def test_model_facing_descriptions_honor_declared_family_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)
    binding, _client = _make_mcp_binding()
    binding = McpToolBinding(
        server_id=binding.server_id,
        tool_name=binding.tool_name,
        tool_alias=binding.tool_alias,
        description="MCP echo " + ("detail " * 500),
        parameters=binding.parameters,
        client=_client,
    )

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        mcp_manager=_DummyMcpManager(binding),
    )

    custom_tool = tools["project_echo"]
    assert custom_tool.metadata["model_description_max_chars"] == 1200  # type: ignore[attr-defined]
    # Manifest validation caps TOOL.description below 1200, so exercise the
    # declared limit on the built tool with a longer description swapped in.
    long_custom_tool = replace(
        custom_tool,  # type: ignore[type-var]
        description="Custom echo " + ("detail " * 500),
    )
    custom_payload = long_custom_tool.as_openai_tool()  # type: ignore[attr-defined]
    assert 1000 < len(custom_payload["function"]["description"]) <= 1200

    mcp_tool = tools["mcp__alpha__echo"]
    assert mcp_tool.metadata["model_description_max_chars"] == 1000  # type: ignore[attr-defined]
    mcp_payload = mcp_tool.as_openai_tool()  # type: ignore[attr-defined]
    assert 800 < len(mcp_payload["function"]["description"]) <= 1000


def test_build_tools_scope_restricted_session_excludes_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.FORGE_EXEC,
        allow_write_globs=["allowed.txt"],
    )

    assert "project_echo" not in tools


def test_build_tools_duplicate_default_protected_prefix_does_not_hide_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.FORGE_EXEC,
        deny_write_prefixes=[".alysis"],
    )

    assert "project_echo" in tools


def test_build_tools_extra_denied_prefixes_exclude_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        deny_write_prefixes=["blocked"],
    )

    assert "project_echo" not in tools


@pytest.mark.parametrize(
    "runtime_kind",
    [
        RuntimeKind.SWARM_WORKER,
        RuntimeKind.SUBAGENT,
        RuntimeKind.CONFLICT_AUTO_RESOLVE,
    ],
)
def test_build_tools_non_top_level_runtimes_exclude_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime_kind: RuntimeKind,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=runtime_kind,
    )

    assert "project_echo" not in tools


def test_review_mode_custom_tool_execution_requires_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)
    surface = _RecordingSurface()

    tools = _build_tools(
        tmp_path,
        mode="review",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        non_interactive=False,
        surface=surface,
    )

    result = tools["project_echo"].run({})

    assert result["success"] is True
    assert [request.kind for request in surface.requests] == ["custom_tool_run:project_echo"]
    assert (
        surface.requests[0].metadata["custom_tool"]["capabilities"]["network_access"]
        == "unspecified"
    )
    assert "capabilities:" in surface.requests[0].preview


@pytest.mark.parametrize("mode", ["auto", "fullaccess"])
def test_trusted_custom_tools_follow_mode_authority_without_a_reviewer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)
    surface = _RecordingSurface()

    tools = _build_tools(
        tmp_path,
        mode=mode,
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        non_interactive=False,
        surface=surface,
    )

    result = tools["project_echo"].run({})

    assert result["success"] is True
    assert len(surface.requests) == (1 if mode == "auto" else 0)


def test_build_tools_custom_tools_disabled_by_config_excludes_custom_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(cfg_dir))
    _write_project_custom_tool(tmp_path)
    _trust_project_custom_tool(tmp_path)

    tools = _build_tools(
        tmp_path,
        mode="auto",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        cfg=AppConfig(
            model="test-model",
            base_url="https://api.openai.com/v1",
            web_search_mode="auto",
            custom_tools_enabled=False,
        ),
    )

    assert "project_echo" not in tools


def test_review_mode_asks_before_an_mcp_tool_call_with_exact_identity(
    tmp_path: Path,
) -> None:
    binding, client = _make_mcp_binding()
    surface = _RecordingSurface(allow=True)
    tools = _build_tools(
        tmp_path,
        mode="review",
        mcp_manager=_DummyMcpManager(binding),
        non_interactive=False,
        surface=surface,
    )

    tools["mcp__alpha__echo"].run({"text": "hi"})

    assert client.called is True
    [request] = surface.requests
    assert request.kind == "mcp"
    assert request.command == "mcp__alpha__echo"
    assert request.metadata["mcp_server_id"] == "alpha"
    assert request.metadata["mcp_tool_id"] == "echo"
    assert request.metadata["access"] == "write"
    assert '"text": "hi"' in request.preview
    assert request.allow_for_session_scope is None


def test_review_mode_declined_mcp_tool_call_never_reaches_the_server(tmp_path: Path) -> None:
    binding, client = _make_mcp_binding()
    surface = _RecordingSurface(allow=False)
    tools = _build_tools(
        tmp_path,
        mode="review",
        mcp_manager=_DummyMcpManager(binding),
        non_interactive=False,
        surface=surface,
    )

    with pytest.raises(AgentRuntimeError, match="MCP action was not approved") as caught:
        tools["mcp__alpha__echo"].run({})

    assert client.called is False
    assert caught.value.result_payload["dispatch_state"] == "not_dispatched"


def test_review_mode_runs_a_tool_its_server_marks_read_only_without_asking(
    tmp_path: Path,
) -> None:
    binding, client = _make_mcp_binding(read_only_hint=True)
    surface = _RecordingSurface(allow=False)
    tools = _build_tools(
        tmp_path,
        mode="review",
        mcp_manager=_DummyMcpManager(binding),
        non_interactive=False,
        surface=surface,
    )

    tools["mcp__alpha__echo"].run({})

    assert client.called is True
    assert surface.requests == []


def test_review_mode_mcp_tool_call_fails_closed_when_nobody_can_be_asked(
    tmp_path: Path,
) -> None:
    binding, client = _make_mcp_binding()
    tools = _build_tools(
        tmp_path,
        mode="review",
        mcp_manager=_DummyMcpManager(binding),
        non_interactive=True,
        surface=_RecordingSurface(allow=True),
    )

    with pytest.raises(AgentRuntimeError, match="Nobody can be asked"):
        tools["mcp__alpha__echo"].run({})

    assert client.called is False


def test_auto_mode_without_reviewer_asks_for_writes_and_retains_destructive_hint(
    tmp_path: Path,
) -> None:
    plain, plain_client = _make_mcp_binding()
    surface = _RecordingSurface(allow=True)
    tools = _build_tools(
        tmp_path,
        mode="auto",
        mcp_manager=_DummyMcpManager(plain),
        non_interactive=False,
        surface=surface,
        yes=False,
    )
    tools["mcp__alpha__echo"].run({})
    assert plain_client.called is True
    assert len(surface.requests) == 1
    surface.requests.clear()

    destructive, destructive_client = _make_mcp_binding(destructive_hint=True)
    tools = _build_tools(
        tmp_path,
        mode="auto",
        mcp_manager=_DummyMcpManager(destructive),
        non_interactive=False,
        surface=surface,
        yes=False,
    )
    tools["mcp__alpha__echo"].run({})
    assert destructive_client.called is True
    [request] = surface.requests
    assert request.kind == "mcp"
    assert request.metadata["destructive"] is True
    assert request.metadata["access"] == "write"
    # A destructive tool asks every time: no "allow for this chat".
    assert request.allow_for_session_scope is None


@pytest.mark.parametrize(("mode", "yes"), [("auto", True), ("fullaccess", False)])
def test_yes_does_not_override_mcp_authority_but_fullaccess_does(
    tmp_path: Path, mode: str, yes: bool
) -> None:
    binding, client = _make_mcp_binding(destructive_hint=True)
    surface = _RecordingSurface(allow=True)
    tools = _build_tools(
        tmp_path,
        mode=mode,
        mcp_manager=_DummyMcpManager(binding),
        non_interactive=False,
        surface=surface,
        yes=yes,
    )

    tools["mcp__alpha__echo"].run({})

    assert client.called is True
    assert len(surface.requests) == (1 if mode == "auto" else 0)


@pytest.mark.parametrize(
    ("annotations", "expected"),
    [
        (None, (False, False)),
        ({}, (False, False)),
        ({"readOnlyHint": True}, (True, False)),
        ({"readOnlyHint": True, "destructiveHint": False}, (True, False)),
        ({"destructiveHint": True}, (False, True)),
        ({"readOnlyHint": True, "destructiveHint": True}, (False, True)),
        ({"readOnlyHint": "true"}, (False, False)),
        ("not-an-object", (False, False)),
    ],
)
def test_mcp_tool_approval_hints_trust_only_explicit_booleans(
    annotations: object, expected: tuple[bool, bool]
) -> None:
    raw_tool: dict[str, object] = {"name": "echo", "inputSchema": {"type": "object"}}
    if annotations is not None:
        raw_tool["annotations"] = annotations
    assert mcp_tool_approval_hints(raw_tool) == expected
