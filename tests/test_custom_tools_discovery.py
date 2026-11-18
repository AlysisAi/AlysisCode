from __future__ import annotations

import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.agent_loop import _custom_tool_capability_summary
from alysis_code.custom_tools.discovery import (
    discover_custom_tools,
)
from alysis_code.custom_tools.session import build_custom_tool_session_state
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.tools.registry import iter_builtin_tool_metadata


def _built_in_tool_names() -> set[str]:
    return {spec.name.casefold() for spec in iter_builtin_tool_metadata()}


def _write_tool(
    root: Path,
    rel_path: str,
    *,
    name: str,
    description: str = "Custom tool",
    input_schema: str = '{"type": "object", "properties": {}, "required": []}',
    body: str = "return {'ok': True}",
    extra_manifest_lines: list[str] | None = None,
) -> Path:
    extra_manifest_lines = list(extra_manifest_lines or [])
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_lines = [
        "TOOL = {",
        f'    "name": "{name}",',
        f'    "description": "{description}",',
        f'    "input_schema": {input_schema},',
    ]
    manifest_lines.extend(f"    {line}" for line in extra_manifest_lines)
    manifest_lines.append("}")
    path.write_text(
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
    return path


def _symlink_to_or_skip(
    link_path: Path,
    target: Path,
    *,
    target_is_directory: bool = False,
) -> None:
    try:
        link_path.symlink_to(target, target_is_directory=target_is_directory)
    except NotImplementedError:
        pytest.skip("symlink creation is not supported in this test environment")
    except OSError as exc:
        if getattr(exc, "winerror", None) == 1314:
            pytest.skip("symlink creation requires Windows Developer Mode or elevated privilege")
        raise


def test_discovery_finds_valid_global_tool(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "config"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_tool(cfg_dir, "tools/global_echo.py", name="global_echo")

    result = discover_custom_tools(
        workspace_root=tmp_path / "workspace",
        built_in_tool_names=_built_in_tool_names(),
    )

    assert [tool.name for tool in result.global_tools] == ["global_echo"]
    assert not result.project_tools
    assert [tool.name for tool in result.effective_tools] == ["global_echo"]
    assert not result.issues


def test_discovery_finds_valid_project_tool(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(workspace, ".alysis/tools/project_echo.py", name="project_echo")

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert [tool.name for tool in result.project_tools] == ["project_echo"]
    assert [tool.relative_tool_path for tool in result.project_tools] == [
        ".alysis/tools/project_echo.py"
    ]


def test_discovery_parses_manifest_version_capabilities_and_output_schema(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/jira_lookup.py",
        name="jira_lookup",
        extra_manifest_lines=[
            '"manifest_version": 1,',
            '"required_env": ["JIRA_TOKEN"],',
            '"capabilities": {',
            '    "read_only": True,',
            '    "network_access": "restricted",',
            '    "network_hosts": ["api.example.com"],',
            '    "filesystem": {"read": "workspace", "write": "none"},',
            '    "secret_refs": ["EXTRA_SECRET"],',
            "},",
            '"output_schema": {"type": "object", "properties": {"ok": {"type": "boolean"}}},',
        ],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
        env={"JIRA_TOKEN": "token"},
    )

    [tool] = result.project_tools
    assert tool.manifest_version == 1
    assert tool.output_schema == {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
    }
    assert tool.capabilities.read_only is True
    assert tool.capabilities.destructive is False
    assert tool.capabilities.network_access == "restricted"
    assert tool.capabilities.network_hosts == ("api.example.com",)
    assert tool.capabilities.filesystem_read_scope == "workspace"
    assert tool.capabilities.filesystem_write_scope == "none"
    assert tool.capabilities.process_spawn == "unspecified"
    assert tool.capabilities.secret_refs == ("EXTRA_SECRET", "JIRA_TOKEN")


def test_discovery_keeps_legacy_manifest_backward_compatible(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(workspace, ".alysis/tools/legacy.py", name="legacy_tool")

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    [tool] = result.project_tools
    assert tool.manifest_version == 1
    assert tool.output_schema is None
    assert tool.capabilities.to_dict() == {
        "read_only": False,
        "destructive": False,
        "network_access": "unspecified",
        "network_hosts": [],
        "filesystem_read_scope": "unspecified",
        "filesystem_write_scope": "unspecified",
        "process_spawn": "unspecified",
        "secret_refs": [],
    }


def test_discovery_defaults_isolation_to_subprocess(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(workspace, ".alysis/tools/default_isolation.py", name="default_isolation")

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    [tool] = result.project_tools
    assert tool.isolation == "subprocess"


def test_discovery_rejects_explicit_inprocess_isolation(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/inprocess.py",
        name="inprocess_tool",
        extra_manifest_lines=['"isolation": "inprocess",'],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.effective_tools
    assert any("TOOL.isolation must be subprocess" in issue.message for issue in result.issues)


def test_discovery_skips_retired_runtimes_and_rejects_retired_only_scopes(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/mixed_scope.py",
        name="mixed_scope",
        extra_manifest_lines=['"enabled_in": ["interactive_chat", "forge_exec"],'],
    )
    _write_tool(
        workspace,
        ".alysis/tools/retired_scope.py",
        name="retired_scope",
        extra_manifest_lines=['"enabled_in": ["swarm_worker"],'],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    [tool] = result.effective_tools
    assert tool.name == "mixed_scope"
    assert tool.enabled_in == ("interactive_chat",)
    # A tool scoped only to runtimes that no longer exist is reported, never widened.
    assert any("no longer exist" in issue.message for issue in result.issues)


def test_docs_workspace_manifest_example_is_discoverable(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[1]
    example = repo_root / "docs" / "examples" / "custom_tools" / "workspace_manifest.py"
    workspace = tmp_path / "workspace"
    tool_path = workspace / ".alysis" / "tools" / "workspace_manifest.py"
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(example, tool_path)

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    example_issues = [
        issue
        for issue in result.issues
        if issue.relative_tool_path.endswith("workspace_manifest.py")
    ]
    assert not example_issues
    project_names = {tool.name for tool in result.project_tools}
    effective_names = {tool.name for tool in result.effective_tools}
    assert "workspace_manifest" in project_names
    assert "workspace_manifest" in effective_names
    spec = next(tool for tool in result.project_tools if tool.name == "workspace_manifest")
    assert spec.isolation == "subprocess"
    assert spec.capabilities.read_only is True
    assert spec.capabilities.filesystem_write_scope == "none"


def test_discovery_rejects_unsupported_manifest_version(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/future.py",
        name="future_tool",
        extra_manifest_lines=['"manifest_version": 2,'],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.project_tools
    assert any("unsupported TOOL.manifest_version" in issue.message for issue in result.issues)


def test_discovery_rejects_conflicting_read_only_destructive_capabilities(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/conflict.py",
        name="conflict_tool",
        extra_manifest_lines=[
            '"capabilities": {"read_only": True, "destructive": True},',
        ],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.project_tools
    assert any("both read_only and destructive" in issue.message for issue in result.issues)


def test_discovery_accepts_capability_process_spawn_unrestricted(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/spawn.py",
        name="spawn_tool",
        extra_manifest_lines=[
            '"capabilities": {"process_spawn": "unrestricted"},',
        ],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.issues
    [spec] = result.project_tools
    assert spec.capabilities.process_spawn == "unrestricted"
    assert spec.capabilities.to_dict()["process_spawn"] == "unrestricted"


def test_discovery_rejects_invalid_process_spawn_capability(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/spawn.py",
        name="spawn_tool",
        extra_manifest_lines=[
            '"capabilities": {"process_spawn": "sometimes"},',
        ],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.project_tools
    assert any("TOOL.capabilities.process_spawn" in issue.message for issue in result.issues)


def test_discovery_rejects_restricted_network_without_hosts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/network.py",
        name="network_tool",
        extra_manifest_lines=[
            '"capabilities": {"network_access": "restricted"},',
        ],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.project_tools
    assert any("network_hosts" in issue.message for issue in result.issues)


def test_discovery_accepts_restricted_network_with_exact_hosts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/network.py",
        name="network_tool",
        extra_manifest_lines=[
            '"capabilities": {',
            '    "network_access": "restricted",',
            '    "network_hosts": ["api.example.com", "127.0.0.1"],',
            "},",
        ],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.issues
    [spec] = result.project_tools
    assert spec.capabilities.network_access == "restricted"
    assert spec.capabilities.network_hosts == ("api.example.com", "127.0.0.1")


def test_custom_tool_capability_summary_includes_enforcement_fields() -> None:
    spec = SimpleNamespace(
        capabilities=SimpleNamespace(
            read_only=False,
            destructive=False,
            network_access="restricted",
            network_hosts=("api.example.com",),
            filesystem_read_scope="none",
            filesystem_write_scope="workspace",
            process_spawn="unrestricted",
            secret_refs=("API_TOKEN",),
        )
    )

    summary = _custom_tool_capability_summary(spec)

    assert "network=restricted" in summary
    assert "network_hosts=api.example.com" in summary
    assert "process_spawn=unrestricted" in summary
    assert "secrets=API_TOKEN" in summary


def test_discovery_rejects_network_hosts_with_non_restricted_network(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/network.py",
        name="network_tool",
        extra_manifest_lines=[
            '"capabilities": {',
            '    "network_access": "none",',
            '    "network_hosts": ["api.example.com"],',
            "},",
        ],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.project_tools
    assert any(
        "network_hosts is only valid with restricted network access" in issue.message
        for issue in result.issues
    )


def test_discovery_rejects_wildcard_network_hosts(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/network.py",
        name="network_tool",
        extra_manifest_lines=[
            '"capabilities": {',
            '    "network_access": "restricted",',
            '    "network_hosts": ["*.example.com"],',
            "},",
        ],
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.project_tools
    assert any("wildcards" in issue.message for issue in result.issues)


def test_project_tool_overrides_global_tool_on_same_valid_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg_dir = tmp_path / "config"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_tool(cfg_dir, "tools/echo.py", name="echo", description="Global echo")
    _write_tool(
        workspace,
        ".alysis/tools/echo.py",
        name="echo",
        description="Project echo",
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert [tool.description for tool in result.effective_tools] == ["Project echo"]
    assert [tool.description for tool in result.shadowed_tools] == ["Global echo"]


def test_invalid_project_tool_does_not_shadow_valid_global_tool(
    tmp_path: Path,
    monkeypatch,
) -> None:
    cfg_dir = tmp_path / "config"
    workspace = tmp_path / "workspace"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_tool(cfg_dir, "tools/echo.py", name="echo", description="Global echo")
    _write_tool(
        workspace,
        ".alysis/tools/echo.py",
        name="echo",
        input_schema='{"type": "string"}',
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert [tool.description for tool in result.effective_tools] == ["Global echo"]
    assert any(issue.tool_name == "echo" for issue in result.issues)


def test_builtin_name_collision_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(workspace, ".alysis/tools/fs_read.py", name="fs_read")

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.effective_tools
    assert any("built-in tool name" in issue.message for issue in result.issues)


def test_reserved_prefix_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(workspace, ".alysis/tools/mcp_tool.py", name="mcp__alpha__echo")

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.effective_tools
    assert any("reserved prefix" in issue.message for issue in result.issues)


@pytest.mark.parametrize("tool_name", ["mcp_resources_list", "mcp_resource_read"])
def test_reserved_host_tool_name_is_rejected(tmp_path: Path, tool_name: str) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(workspace, f".alysis/tools/{tool_name}.py", name=tool_name)

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert not result.effective_tools
    assert any("reserved host tool name" in issue.message for issue in result.issues)


def test_syntax_error_is_reported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    path = workspace / ".alysis/tools/bad.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("TOOL = {\n", encoding="utf-8")

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert any(issue.code == "syntax_error" for issue in result.issues)


def test_invalid_utf8_is_reported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    path = workspace / ".alysis/tools/bad.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"TOOL = {}\n\xff\xfe")

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert any(issue.code == "invalid_utf8" for issue in result.issues)


def test_symlink_is_rejected(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    source = workspace / "real.py"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("print('x')\n", encoding="utf-8")
    tool_path = workspace / ".alysis/tools/link.py"
    tool_path.parent.mkdir(parents=True, exist_ok=True)
    _symlink_to_or_skip(tool_path, source)

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert any(issue.code == "symlink_rejected" for issue in result.issues)


def test_discovery_rejects_project_tool_root_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_tools = tmp_path / "external-project-tools"
    _write_tool(external_tools, "escaped_project.py", name="escaped_project")
    escaped_root = workspace / ".alysis" / "tools"
    escaped_root.parent.mkdir(parents=True, exist_ok=True)
    _symlink_to_or_skip(escaped_root, external_tools, target_is_directory=True)

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert result.project_tools == ()
    assert all(tool.name != "escaped_project" for tool in result.effective_tools)
    assert any(
        issue.code == "path_escape"
        and issue.source_scope == "project"
        and "outside the workspace-owned custom tools scope" in issue.message
        for issue in result.issues
    )


def test_discovery_rejects_project_tool_root_broken_symlink_escape(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    escaped_root = workspace / ".alysis" / "tools"
    escaped_root.parent.mkdir(parents=True, exist_ok=True)
    _symlink_to_or_skip(
        escaped_root,
        tmp_path / "missing-external-tools",
        target_is_directory=True,
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert result.project_tools == ()
    assert any(
        issue.code == "path_escape"
        and issue.source_scope == "project"
        and issue.relative_tool_path == ".alysis/tools"
        for issue in result.issues
    )


def test_discovery_rejects_global_tool_root_symlink_escape(tmp_path: Path, monkeypatch) -> None:
    cfg_dir = tmp_path / "config"
    external_tools = tmp_path / "external-global-tools"
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", os.fspath(cfg_dir))
    _write_tool(external_tools, "escaped_global.py", name="escaped_global")
    escaped_root = cfg_dir / "tools"
    escaped_root.parent.mkdir(parents=True, exist_ok=True)
    _symlink_to_or_skip(escaped_root, external_tools, target_is_directory=True)

    result = discover_custom_tools(
        workspace_root=tmp_path / "workspace",
        built_in_tool_names=_built_in_tool_names(),
    )

    assert result.global_tools == ()
    assert any(
        issue.code == "path_escape" and issue.source_scope == "global" for issue in result.issues
    )


def test_discovery_does_not_fallback_to_basename_for_escaped_root_issue(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    external_tools = tmp_path / "external-project-tools"
    _write_tool(external_tools, "escaped_project.py", name="escaped_project")
    escaped_root = workspace / ".alysis" / "tools"
    escaped_root.parent.mkdir(parents=True, exist_ok=True)
    _symlink_to_or_skip(escaped_root, external_tools, target_is_directory=True)

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    issue = next(
        issue
        for issue in result.issues
        if issue.code == "path_escape" and issue.source_scope == "project"
    )
    assert issue.relative_tool_path != "escaped_project.py"
    assert issue.relative_tool_path == ".alysis/tools"


def test_symlink_helper_skips_on_windows_privilege_error(tmp_path: Path, monkeypatch) -> None:
    link_path = tmp_path / "link"
    target = tmp_path / "target"

    def raise_winerror(*args, **kwargs) -> None:
        exc = OSError("privilege not held")
        exc.winerror = 1314
        raise exc

    monkeypatch.setattr(Path, "symlink_to", raise_winerror)

    with pytest.raises(pytest.skip.Exception):
        _symlink_to_or_skip(link_path, target)


def test_symlink_helper_reraises_unexpected_symlink_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    link_path = tmp_path / "link"
    target = tmp_path / "target"

    def raise_unexpected(*args, **kwargs) -> None:
        raise OSError("unexpected symlink failure")

    monkeypatch.setattr(Path, "symlink_to", raise_unexpected)

    with pytest.raises(OSError, match="unexpected symlink failure"):
        _symlink_to_or_skip(link_path, target)


def test_symlink_helper_skips_when_symlinks_are_not_supported(
    tmp_path: Path,
    monkeypatch,
) -> None:
    link_path = tmp_path / "link"
    target = tmp_path / "target"

    def raise_not_supported(*args, **kwargs) -> None:
        raise NotImplementedError

    monkeypatch.setattr(Path, "symlink_to", raise_not_supported)

    with pytest.raises(pytest.skip.Exception):
        _symlink_to_or_skip(link_path, target)


def test_missing_run_is_reported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    path = workspace / ".alysis/tools/no_run.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        'TOOL = {"name": "no_run", "description": "x", "input_schema": {"type": "object", "properties": {}, "required": []}}\n',
        encoding="utf-8",
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert any(issue.code == "missing_run" for issue in result.issues)


def test_invalid_schema_is_reported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/bad_schema.py",
        name="bad_schema",
        input_schema='{"type": "array"}',
    )

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert any("root type must be 'object'" in issue.message for issue in result.issues)


def test_duplicate_names_in_same_scope_are_reported(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(workspace, ".alysis/tools/one.py", name="dup_tool")
    _write_tool(workspace, ".alysis/tools/two.py", name="Dup_Tool")

    result = discover_custom_tools(
        workspace_root=workspace,
        built_in_tool_names=_built_in_tool_names(),
    )

    assert len(result.project_tools) == 1
    assert any(issue.code == "duplicate_name" for issue in result.issues)


def test_missing_required_env_is_visible_but_not_exposed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = tmp_path / "workspace"
    monkeypatch.delenv("MISSING_TOKEN", raising=False)
    _write_tool(
        workspace,
        ".alysis/tools/needs_env.py",
        name="needs_env",
        extra_manifest_lines=['"required_env": ["MISSING_TOKEN"],'],
    )

    state = build_custom_tool_session_state(
        workspace_root=workspace,
        custom_tools_enabled=True,
        mode="review",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        built_in_tool_names=_built_in_tool_names(),
    )

    [entry] = [entry for entry in state.catalog_entries if entry.name == "needs_env"]
    assert entry.status == "missing-env"
    assert "MISSING_TOKEN" in entry.detail
    assert "needs_env" not in state.exposed_tools_by_name


def test_session_catalog_entries_expose_capability_metadata(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    _write_tool(
        workspace,
        ".alysis/tools/reader.py",
        name="reader_tool",
        extra_manifest_lines=[
            '"capabilities": {',
            '    "read_only": True,',
            '    "network_access": "none",',
            '    "filesystem": {"read": "workspace", "write": "none"},',
            "},",
        ],
    )

    state = build_custom_tool_session_state(
        workspace_root=workspace,
        custom_tools_enabled=True,
        mode="review",
        runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
        built_in_tool_names=_built_in_tool_names(),
        catalog_view=True,
    )

    [entry] = [entry for entry in state.catalog_entries if entry.name == "reader_tool"]
    assert entry.manifest_version == 1
    assert entry.capabilities is not None
    assert entry.capabilities.read_only is True
    assert entry.capabilities.filesystem_read_scope == "workspace"
