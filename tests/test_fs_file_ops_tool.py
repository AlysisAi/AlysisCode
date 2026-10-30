from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from rich.console import Console

from alysis_code.agent_loop import AgentRuntimeError, build_tools
from alysis_code.config import AppConfig
from alysis_code.session_store import SessionStore
from alysis_code.tools.fs import FsError


def _store(root: Path) -> SessionStore:
    return SessionStore(
        enabled=False,
        sessions_dir=root / "sessions",
        session_id="fs-file-ops-test",
        cwd=str(root),
        repo_root=str(root),
    )


def _build_tools(
    tmp_path: Path,
    *,
    mode: str = "auto",
    yes: bool = True,
    non_interactive: bool = True,
):
    return build_tools(
        root=tmp_path,
        console=Console(file=io.StringIO(), force_terminal=False),
        store=_store(tmp_path),
        mode=mode,
        yes=yes,
        cfg=AppConfig(model="test-model"),
        non_interactive=non_interactive,
    )


def test_build_tools_registers_fs_mkdir_move_copy_delete(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    assert "fs_mkdir" in tools
    assert "fs_move" in tools
    assert "fs_copy" in tools
    assert "fs_delete" in tools

    mkdir_schema = tools["fs_mkdir"].as_openai_tool()["function"]["parameters"]
    move_schema = tools["fs_move"].as_openai_tool()["function"]["parameters"]
    copy_schema = tools["fs_copy"].as_openai_tool()["function"]["parameters"]
    delete_schema = tools["fs_delete"].as_openai_tool()["function"]["parameters"]

    assert mkdir_schema["required"] == ["path"]
    assert mkdir_schema["properties"]["parents"]["default"] is True
    assert mkdir_schema["properties"]["exist_ok"]["default"] is True
    assert move_schema["required"] == ["source_path", "destination_path"]
    assert copy_schema["required"] == ["source_path", "destination_path"]
    # fs_delete gained a batch ``paths`` form, so ``path`` alone is no
    # longer required by the schema (the executor enforces exactly one of the
    # two at runtime).
    assert "required" not in delete_schema
    assert "paths" in delete_schema["properties"]


def test_fs_mkdir_success(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    result = tools["fs_mkdir"].run({"path": "scaffold"})

    assert result == {
        "path": "scaffold",
        "created": True,
        "already_exists": False,
        "parents": True,
        "exist_ok": True,
    }
    assert (tmp_path / "scaffold").is_dir()


def test_fs_mkdir_nested_creation_with_parents_true(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    result = tools["fs_mkdir"].run({"path": "a/b/c", "parents": True})

    assert result == {
        "path": str(Path("a/b/c")),
        "created": True,
        "already_exists": False,
        "parents": True,
        "exist_ok": True,
    }
    assert (tmp_path / "a" / "b" / "c").is_dir()


def test_fs_mkdir_existing_directory_succeeds_with_exist_ok_true(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    tools = _build_tools(tmp_path)

    result = tools["fs_mkdir"].run({"path": "existing", "exist_ok": True})

    assert result == {
        "path": "existing",
        "created": False,
        "already_exists": True,
        "parents": True,
        "exist_ok": True,
    }


def test_fs_mkdir_existing_directory_fails_with_exist_ok_false(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.mkdir()
    tools = _build_tools(tmp_path)

    with pytest.raises(FsError, match="Directory already exists and exist_ok is false: existing"):
        tools["fs_mkdir"].run({"path": "existing", "exist_ok": False})


def test_fs_mkdir_existing_file_fails_clearly(tmp_path: Path) -> None:
    target = tmp_path / "existing"
    target.write_text("alpha\n", encoding="utf-8")
    tools = _build_tools(tmp_path)

    with pytest.raises(FsError, match="Target exists as a file: existing"):
        tools["fs_mkdir"].run({"path": "existing"})


def test_fs_move_success(tmp_path: Path) -> None:
    source = tmp_path / "old.txt"
    source.write_text("alpha\n", encoding="utf-8")
    expected_size = len(source.read_bytes())
    tools = _build_tools(tmp_path)

    result = tools["fs_move"].run(
        {
            "source_path": "old.txt",
            "destination_path": "nested/new.txt",
        }
    )

    assert result == {
        "source_path": "old.txt",
        "destination_path": str(Path("nested/new.txt")),
        "moved": True,
        "overwritten": False,
        "bytes": expected_size,
    }
    assert source.exists() is False
    assert (tmp_path / "nested" / "new.txt").read_text(encoding="utf-8") == "alpha\n"


def test_fs_copy_success(tmp_path: Path) -> None:
    source = tmp_path / "old.txt"
    source.write_text("alpha\n", encoding="utf-8")
    expected_size = len(source.read_bytes())
    tools = _build_tools(tmp_path)

    result = tools["fs_copy"].run(
        {
            "source_path": "old.txt",
            "destination_path": "nested/new.txt",
        }
    )

    assert result == {
        "source_path": "old.txt",
        "destination_path": str(Path("nested/new.txt")),
        "copied": True,
        "overwritten": False,
        "bytes": expected_size,
    }
    assert source.read_text(encoding="utf-8") == "alpha\n"
    assert (tmp_path / "nested" / "new.txt").read_text(encoding="utf-8") == "alpha\n"


def test_fs_delete_success(tmp_path: Path) -> None:
    target = tmp_path / "old.txt"
    target.write_text("alpha\n", encoding="utf-8")
    expected_size = len(target.read_bytes())
    tools = _build_tools(tmp_path)

    result = tools["fs_delete"].run({"path": "old.txt"})

    assert result == {
        "path": "old.txt",
        "deleted": True,
        "bytes": expected_size,
    }
    assert target.exists() is False


def test_fs_move_missing_source(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    with pytest.raises(FsError, match="Not found: missing.txt"):
        tools["fs_move"].run(
            {
                "source_path": "missing.txt",
                "destination_path": "new.txt",
            }
        )


def test_fs_copy_missing_source(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    with pytest.raises(FsError, match="Not found: missing.txt"):
        tools["fs_copy"].run(
            {
                "source_path": "missing.txt",
                "destination_path": "new.txt",
            }
        )


def test_fs_delete_missing_source(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    with pytest.raises(FsError, match="Not found: missing.txt"):
        tools["fs_delete"].run({"path": "missing.txt"})


def test_fs_delete_in_use_permission_error_preserves_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "in-use.txt"
    target.write_text("busy\n", encoding="utf-8")
    tools = _build_tools(tmp_path)
    import alysis_code.tools.fs as fs_module

    original_replace = fs_module.rename_host_file_no_replace if os.name == "nt" else os.replace

    def locked_replace(source: object, destination: object) -> None:
        if Path(source).resolve() == target.resolve():
            raise PermissionError("file is in use")
        original_replace(source, destination)

    if os.name == "nt":
        monkeypatch.setattr(fs_module, "rename_host_file_no_replace", locked_replace)
    else:
        monkeypatch.setattr(os, "replace", locked_replace)

    with pytest.raises(PermissionError, match="file is in use"):
        tools["fs_delete"].run({"path": "in-use.txt"})
    assert target.read_text(encoding="utf-8") == "busy\n"


def test_fs_move_overwrite_false_blocks_existing_destination(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")
    tools = _build_tools(tmp_path)

    with pytest.raises(FsError, match="Destination exists and overwrite is false: new.txt"):
        tools["fs_move"].run(
            {
                "source_path": "old.txt",
                "destination_path": "new.txt",
            }
        )


def test_fs_copy_overwrite_false_blocks_existing_destination(tmp_path: Path) -> None:
    (tmp_path / "old.txt").write_text("old\n", encoding="utf-8")
    (tmp_path / "new.txt").write_text("new\n", encoding="utf-8")
    tools = _build_tools(tmp_path)

    with pytest.raises(FsError, match="Destination exists and overwrite is false: new.txt"):
        tools["fs_copy"].run(
            {
                "source_path": "old.txt",
                "destination_path": "new.txt",
            }
        )


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("fs_mkdir", {"path": "../outside"}),
        ("fs_move", {"source_path": "old.txt", "destination_path": "../outside.txt"}),
        ("fs_copy", {"source_path": "../outside.txt", "destination_path": "copy.txt"}),
        ("fs_delete", {"path": "../outside.txt"}),
    ],
)
def test_file_ops_reject_root_escape(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, str],
) -> None:
    tools = _build_tools(tmp_path)

    with pytest.raises(AgentRuntimeError, match="Path escapes root"):
        tools[tool_name].run(arguments)


def test_agent_runtime_error_preserves_runtime_error_constructor_shapes() -> None:
    empty_error = AgentRuntimeError()
    multi_arg_error = AgentRuntimeError("first", "second")
    payload_error = AgentRuntimeError("blocked", result_payload={"error_code": "example"})

    assert empty_error.args == ()
    assert multi_arg_error.args == ("first", "second")
    assert payload_error.args == ("blocked",)
    assert payload_error.result_payload == {"error_code": "example"}


def test_fs_write_root_escape_carries_structured_recovery_payload(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    with pytest.raises(AgentRuntimeError, match="Path escapes root") as exc_info:
        tools["fs_write"].run({"path": "/git/server/hooks/post-receive", "content": "#!/bin/sh\n"})

    payload = exc_info.value.result_payload
    assert payload is not None
    assert payload["error_code"] == "path_escapes_workspace"
    assert payload["attempted_path"] == "/git/server/hooks/post-receive"
    assert payload["tool_name"] == "fs_write"
    assert payload["workspace_root"] == os.fspath(tmp_path.resolve())
    actions = {action["action"] for action in payload["suggested_next_actions"]}
    assert "use_workspace_relative_path" in actions
    assert "use_shell_run_if_policy_allows" in actions


def test_fs_read_root_escape_carries_read_recovery_payload(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    with pytest.raises(AgentRuntimeError, match="Path escapes root") as exc_info:
        tools["fs_read"].run({"path": "/etc/passwd"})

    payload = exc_info.value.result_payload
    assert payload is not None
    assert payload["error_code"] == "path_escapes_workspace"
    assert payload["tool_name"] == "fs_read"
    assert payload["requires_user_confirmation"] is False
    assert {action["action"] for action in payload["suggested_next_actions"]} == {
        "use_workspace_relative_path",
        "ask_user_for_accessible_input",
        "explain_boundary",
    }


def test_shell_run_cwd_escape_carries_cwd_recovery_payload(tmp_path: Path) -> None:
    tools = _build_tools(tmp_path)

    with pytest.raises(AgentRuntimeError, match="Path escapes root") as exc_info:
        tools["shell_run"].run({"cmd": "pwd", "cwd": "/etc"})

    payload = exc_info.value.result_payload
    assert payload is not None
    assert payload["error_code"] == "path_escapes_workspace"
    assert payload["tool_name"] == "shell_run"
    assert {action["action"] for action in payload["suggested_next_actions"]} == {
        "use_workspace_relative_cwd",
        "pass_external_path_as_argument_if_policy_allows",
        "ask_or_explain_boundary",
    }


@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("fs_mkdir", {"path": "new-dir"}),
        ("fs_move", {"source_path": "old.txt", "destination_path": "new.txt"}),
        ("fs_copy", {"source_path": "old.txt", "destination_path": "new.txt"}),
        ("fs_delete", {"path": "old.txt"}),
    ],
)
def test_file_ops_block_in_readonly_mode(
    tmp_path: Path,
    tool_name: str,
    arguments: dict[str, str],
) -> None:
    tools = _build_tools(tmp_path, mode="readonly")

    _ = arguments

    assert tool_name not in tools


@pytest.fixture(autouse=True)
def file_semantics_without_os_backend(monkeypatch):
    # These unit tests isolate approval, stale-file and ledger behavior.
    # Native/WSL containment is exercised by the installed-runtime qualifier.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
