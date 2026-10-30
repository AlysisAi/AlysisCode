from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.agent_loop import AgentRuntimeError, build_tools
from alysis_code.session_store import SessionStore


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="test",
        cwd=str(root),
        repo_root=str(root),
    )


def _git_spaced_file_patch(repo: Path) -> str:
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    target = repo / "foo bar.txt"
    target.write_text("old\n", encoding="utf-8")
    subprocess.run(
        ["git", "add", "foo bar.txt"], cwd=repo, check=True, capture_output=True, text=True
    )
    subprocess.run(
        ["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True, text=True
    )
    target.write_text("new\n", encoding="utf-8")
    cp = subprocess.run(
        ["git", "diff", "--", "foo bar.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "checkout", "--", "foo bar.txt"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return cp.stdout


def test_non_interactive_auto_blocks_sensitive_shell_confirmation(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=False,
        non_interactive=True,
    )
    with pytest.raises(
        AgentRuntimeError, match="Git remote writes require a separate person approval"
    ):
        tools["shell_run"].run({"cmd": "git push origin main"})


def test_fullaccess_still_requires_person_for_unresolved_shell_effects(tmp_path: Path) -> None:
    class _Runner:
        def run(self, *, root: Path, cwd: Path, cmd: str, timeout_s: int):
            pytest.fail("An unresolved command must not dispatch without person approval")

    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="fullaccess",
        yes=False,
        non_interactive=True,
        shell_runner=_Runner(),
    )

    with pytest.raises(
        AgentRuntimeError, match="Unresolved command effects require a separate person"
    ):
        tools["shell_run"].run({"cmd": "rm -rf /definitely-not-a-real-path-for-tests"})


def test_git_apply_patch_strict_scope_blocks_out_of_scope_patch(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        allow_write_globs=["src/**"],
        non_interactive=True,
    )
    patch = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        "+new\n"
    )
    with pytest.raises(AgentRuntimeError, match="outside allowed scope"):
        tools["git_apply_patch"].run({"patch": patch})


def test_git_apply_patch_scope_allows_spaced_filename(tmp_path: Path) -> None:
    patch = _git_spaced_file_patch(tmp_path)
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        allow_write_globs=["foo bar.txt"],
        non_interactive=True,
    )
    result = tools["git_apply_patch"].run({"patch": patch})
    assert result["applied"] is True


def test_fs_write_scope_allows_globstar_direct_child_path(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        allow_write_globs=["tests/**/*.py"],
        non_interactive=True,
    )

    result = tools["fs_write"].run(
        {"path": "tests/test_coupon.py", "content": "def test_ok():\n    pass\n"}
    )

    assert Path(result["path"]).as_posix() == "tests/test_coupon.py"
    assert (tmp_path / "tests" / "test_coupon.py").is_file()


def test_fs_write_scope_allows_existing_directory_scope_descendant(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        allow_write_globs=["tests"],
        non_interactive=True,
    )

    result = tools["fs_write"].run(
        {"path": "tests/test_settings.py", "content": "SETTING = True\n"}
    )

    assert Path(result["path"]).as_posix() == "tests/test_settings.py"
    assert (tmp_path / "tests" / "test_settings.py").read_text(encoding="utf-8") == (
        "SETTING = True\n"
    )


def test_fs_write_readme_alias_updates_existing_readme_md(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Old\n", encoding="utf-8")
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        allow_write_globs=["README"],
        non_interactive=True,
    )

    result = tools["fs_write"].run({"path": "README", "content": "# New\n"})

    assert result["path"] == "README.md"
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "# New\n"
    assert not (tmp_path / "README").exists()


def test_fs_mkdir_allows_ancestor_directory_for_explicit_file_scope(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        allow_write_globs=["src/pkg/module.py"],
        non_interactive=True,
    )

    result_root = tools["fs_mkdir"].run({"path": "src"})
    result_pkg = tools["fs_mkdir"].run({"path": "src/pkg"})

    assert result_root["path"] == "src"
    assert Path(result_pkg["path"]).as_posix() == "src/pkg"
    assert (tmp_path / "src").is_dir()
    assert (tmp_path / "src" / "pkg").is_dir()


def test_fs_mkdir_still_blocks_unrelated_directory_for_explicit_file_scope(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        allow_write_globs=["src/pkg/module.py"],
        non_interactive=True,
    )

    with pytest.raises(AgentRuntimeError, match="outside allowed scope"):
        tools["fs_mkdir"].run({"path": "src/other"})


def test_fs_delete_allows_root_scratch_output_cleanup_outside_strict_scope(
    tmp_path: Path,
) -> None:
    scratch = tmp_path / "pytest_results.txt"
    scratch.write_text("temporary output\n", encoding="utf-8")
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        allow_write_globs=["src/main.py"],
        non_interactive=True,
    )

    result = tools["fs_delete"].run({"path": "pytest_results.txt"})

    assert result["deleted"] is True
    assert not scratch.exists()


def test_fs_delete_still_blocks_material_files_outside_strict_scope(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Notes\n", encoding="utf-8")
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        allow_write_globs=["src/main.py"],
        non_interactive=True,
    )

    with pytest.raises(AgentRuntimeError, match="outside allowed scope"):
        tools["fs_delete"].run({"path": "README.md"})


def test_build_tools_registers_one_reader_with_optional_line_window(tmp_path: Path) -> None:
    tools = build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO()),
        store=_store(tmp_path),
        mode="auto",
        yes=True,
        non_interactive=True,
    )

    assert "fs_read_lines" not in tools
    assert "fs_read" in tools
    schema = tools["fs_read"].as_openai_tool()["function"]["parameters"]
    assert schema["required"] == ["path"]
    assert schema["properties"]["max_lines"]["minimum"] == 1
    assert schema["properties"]["include_line_numbers"]["type"] == "boolean"
