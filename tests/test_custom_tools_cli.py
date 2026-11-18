from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import click
import pytest
from click.testing import CliRunner as ClickCliRunner
from typer.testing import CliRunner

from alysis_code import cli as cli_mod


def _init_git_repo(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    (root / "README.md").write_text("repo\n", encoding="utf-8")


def _write_tool(
    root: Path,
    rel_path: str,
    *,
    name: str,
    extra_manifest_lines: list[str] | None = None,
) -> None:
    extra_manifest_lines = list(extra_manifest_lines or [])
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest_lines = [
        "TOOL = {",
        f'    "name": "{name}",',
        '    "description": "CLI tool",',
        '    "input_schema": {"type": "object", "properties": {}, "required": []},',
    ]
    manifest_lines.extend(f"    {line}" for line in extra_manifest_lines)
    manifest_lines.append("}")
    path.write_text(
        "\n".join(
            [
                *manifest_lines,
                "",
                "def run(args):",
                "    return {'ok': True}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def test_tool_list_cli_shows_project_and_global_tools_and_path_handling(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    (workspace / "subdir").mkdir()
    _write_tool(workspace, ".alysis/tools/project_echo.py", name="project_echo")
    _write_tool(cfg_dir, "tools/global_echo.py", name="global_echo")

    env = {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}
    result = runner.invoke(
        cli_mod.app,
        ["tool", "list", "--path", str(workspace / "subdir")],
        env=env,
        terminal_width=200,
    )
    normalized_output = "".join(result.output.split())

    assert result.exit_code == 0
    assert "global_echo" in normalized_output
    assert "project_echo" in normalized_output
    assert "global_echo.py" in normalized_output
    assert "project_echo.py" in normalized_output
    assert "tools/global_echo.py" in normalized_output
    assert ".alysis/tools/project_echo.py" in normalized_output
    assert f"Projectroot:{workspace.as_posix()}/.alysis/tools" in normalized_output
    assert f"Globalroot:{cfg_dir.as_posix()}/tools" in normalized_output


def test_tool_list_cli_preserves_exact_name_and_filename_on_narrow_width(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    _write_tool(workspace, ".alysis/tools/project_echo.py", name="project_echo")
    _write_tool(cfg_dir, "tools/global_echo.py", name="global_echo")

    env = {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}
    result = runner.invoke(
        cli_mod.app,
        ["tool", "list", "--path", str(workspace)],
        env=env,
        terminal_width=100,
    )
    normalized_output = "".join(result.output.split())

    assert result.exit_code == 0
    assert "global_echo" in normalized_output
    assert "project_echo" in normalized_output
    assert "global_echo.py" in normalized_output
    assert "project_echo.py" in normalized_output
    assert "tools/global_echo.py" in normalized_output
    assert ".alysis/tools/project_echo.py" in normalized_output


def test_console_uses_click_terminal_width_when_available() -> None:
    @click.command()
    def probe() -> None:
        click.echo(f"width={cli_mod._console().width}")

    result = ClickCliRunner().invoke(probe, [], terminal_width=137)

    assert result.exit_code == 0
    assert result.output.strip() == "width=137"


def test_console_defaults_to_stable_width_in_non_interactive_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cli_mod, "get_current_context", lambda silent=True: None)
    monkeypatch.setattr(cli_mod.sys, "stdout", SimpleNamespace(isatty=lambda: False), raising=False)

    assert cli_mod._console().width == 120


def test_tool_info_cli_shows_manifest_details(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    _write_tool(workspace, ".alysis/tools/project_echo.py", name="project_echo")

    result = runner.invoke(
        cli_mod.app,
        ["tool", "info", "project_echo", "--path", str(workspace)],
        terminal_width=200,
    )
    normalized_output = "".join(result.output.split())

    assert result.exit_code == 0
    assert "description" in result.output
    assert "input_schema" in result.output
    assert "project_echo" in result.output
    assert "source_path" in result.output
    assert (workspace / ".alysis/tools/project_echo.py").as_posix() in normalized_output


def test_tool_list_cli_preserves_filename_contiguously_on_narrow_terminals(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    _write_tool(workspace, ".alysis/tools/project_echo.py", name="project_echo")
    _write_tool(cfg_dir, "tools/global_echo.py", name="global_echo")

    env = {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}
    result = runner.invoke(
        cli_mod.app,
        ["tool", "list", "--path", str(workspace)],
        env=env,
        terminal_width=60,
    )
    normalized_output = "".join(result.output.split())

    assert result.exit_code == 0
    assert "global_echo.py" in normalized_output
    assert "project_echo.py" in normalized_output


def test_tool_list_and_info_cli_do_not_mark_supported_runtime_specific_tools_disabled(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _init_git_repo(workspace)
    _write_tool(
        cfg_dir,
        "tools/one_shot_echo.py",
        name="one_shot_echo",
        extra_manifest_lines=['"enabled_in": ["one_shot"],'],
    )

    env = {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}
    list_result = runner.invoke(
        cli_mod.app,
        ["tool", "list", "--path", str(workspace)],
        env=env,
        terminal_width=200,
    )
    info_result = runner.invoke(
        cli_mod.app,
        ["tool", "info", "one_shot_echo", "--path", str(workspace)],
        env=env,
        terminal_width=200,
    )
    normalized_list_output = "".join(list_result.output.split())
    normalized_info_output = "".join(info_result.output.split())

    assert list_result.exit_code == 0
    assert "CustomTools(1)" in normalized_list_output
    assert "disabled" not in normalized_list_output
    assert info_result.exit_code == 0
    assert "one_shot_echo" in normalized_info_output
    assert "status" in normalized_info_output
    assert "available" in normalized_info_output


def test_tool_trust_and_untrust_cli_updates_project_tool_status(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    _write_tool(workspace, ".alysis/tools/project_echo.py", name="project_echo")
    env = {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}

    trust_result = runner.invoke(
        cli_mod.app,
        ["tool", "trust", "project_echo", "--path", str(workspace)],
        env=env,
    )
    assert trust_result.exit_code == 0
    assert "Trusted project custom tool" in trust_result.output

    list_result = runner.invoke(
        cli_mod.app,
        ["tool", "list", "--path", str(workspace)],
        env=env,
    )
    assert "persistent" in list_result.output

    untrust_result = runner.invoke(
        cli_mod.app,
        ["tool", "untrust", "project_echo", "--path", str(workspace)],
        env=env,
    )
    assert untrust_result.exit_code == 0
    assert "Untrusted project custom tool" in untrust_result.output


def test_tool_trust_rejects_global_tools(tmp_path: Path) -> None:
    runner = CliRunner()
    workspace = tmp_path / "workspace"
    cfg_dir = tmp_path / "config"
    workspace.mkdir(parents=True, exist_ok=True)
    _write_tool(cfg_dir, "tools/global_echo.py", name="global_echo")

    env = {"ALYSIS_CONFIG_DIR": os.fspath(cfg_dir)}
    result = runner.invoke(
        cli_mod.app,
        ["tool", "trust", "global_echo", "--path", str(workspace)],
        env=env,
    )

    assert result.exit_code == 2
    assert "only apply to project custom tools" in result.output
