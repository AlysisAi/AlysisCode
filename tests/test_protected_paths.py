from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.agent_loop import AgentRuntimeError, build_tools
from alysis_code.session_store import SessionStore


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="protected-paths-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _tools(root: Path, *, mode: str = "auto"):
    return build_tools(
        root=root,
        console=Console(file=io.StringIO()),
        store=_store(root),
        mode=mode,
        yes=True,
        non_interactive=True,
    )


def test_fs_write_blocks_git_dir_by_default(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
        tools["fs_write"].run(
            {
                "path": ".git/hooks/pre-commit",
                "content": "#!/bin/sh\necho blocked\n",
            }
        )


def test_fs_edit_blocks_git_dir_by_default(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
        tools["fs_edit"].run(
            {
                "path": ".git/hooks/pre-commit",
                "edits": [{"op": "append", "content": "echo blocked\n"}],
            }
        )


def test_fs_move_blocks_git_dir_by_default(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
        tools["fs_move"].run(
            {
                "source_path": ".git/config",
                "destination_path": "backup/config",
            }
        )


def test_fs_copy_blocks_git_dir_destination_by_default(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
        tools["fs_copy"].run(
            {
                "source_path": "README.md",
                "destination_path": ".git/config",
            }
        )


def test_fs_delete_blocks_git_dir_by_default(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
        tools["fs_delete"].run({"path": ".git/config"})


def test_fs_write_blocks_git_dir_case_insensitive(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
        tools["fs_write"].run(
            {
                "path": ".GIT/hooks/pre-commit",
                "content": "#!/bin/sh\necho blocked\n",
            }
        )


@pytest.mark.parametrize(
    "path",
    [
        ".Alysis/runs/x",
        ".AlYsIs_ImAgEs/x.png",
    ],
)
def test_fs_write_blocks_all_protected_prefixes_case_insensitive(
    tmp_path: Path,
    path: str,
) -> None:
    tools = _tools(tmp_path)
    with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
        tools["fs_write"].run(
            {
                "path": path,
                "content": "blocked\n",
            }
        )


def test_git_apply_patch_blocks_git_dir_by_default(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    patch = (
        "diff --git a/.git/hooks/pre-commit b/.git/hooks/pre-commit\n"
        "new file mode 100755\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/.git/hooks/pre-commit\n"
        "@@ -0,0 +1 @@\n"
        "+echo blocked\n"
    )
    with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
        tools["git_apply_patch"].run({"patch": patch})


def test_git_apply_patch_blocks_git_dir_case_insensitive(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    patch = (
        "diff --git a/.GIT/hooks/pre-commit b/.GIT/hooks/pre-commit\n"
        "new file mode 100755\n"
        "index 0000000..1111111\n"
        "--- /dev/null\n"
        "+++ b/.GIT/hooks/pre-commit\n"
        "@@ -0,0 +1 @@\n"
        "+echo blocked\n"
    )
    with pytest.raises(AgentRuntimeError, match="Blocked write to protected path"):
        tools["git_apply_patch"].run({"patch": patch})


def test_fs_write_fullaccess_authorizes_sensitive_path_without_another_prompt(
    tmp_path: Path,
) -> None:
    tools = _tools(tmp_path, mode="fullaccess")
    content = "#!/bin/sh\necho authorized\n"
    tools["fs_write"].run({"path": ".git/hooks/pre-commit", "content": content})
    assert (tmp_path / ".git" / "hooks" / "pre-commit").read_text() == content


def test_fs_write_allows_gitignore(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    out = tools["fs_write"].run({"path": ".gitignore", "content": "*.pyc\n"})
    assert out["bytes"] > 0
