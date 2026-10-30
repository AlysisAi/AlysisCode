from __future__ import annotations

import io
import subprocess
from pathlib import Path

import pytest
from rich.console import Console

import alysis_code.agent_loop as agent_loop_mod
import alysis_code.cli_impl.chat as chat_impl_mod
from alysis_code import cli as cli_mod
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.session_store import read_session_events
from alysis_code.subagents import SubagentDefinition
from alysis_code.workspace_binding import resolve_workspace_binding


def _fake_git_repo(root: Path) -> None:
    git_dir = root / ".git"
    (git_dir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (git_dir / "refs" / "heads" / "main").write_text("0" * 40 + "\n", encoding="utf-8")


class _FakeShellRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def run(
        self, *, root: Path, cwd: Path, cmd: str, timeout_s: int
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append(
            {
                "root": root,
                "cwd": cwd,
                "cmd": cmd,
                "timeout_s": timeout_s,
            }
        )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="ok\n", stderr="")


def _binding_context_content(session: object) -> str:
    messages = getattr(session, "messages", [])
    if not isinstance(messages, list):
        return ""
    for message in messages:
        if str(message.get("role") or "") != "user":
            continue
        content = str(message.get("content") or "")
        if content.lstrip().startswith("<workspace_binding_context>"):
            return content
    return ""


def _create_chat_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    focus_path: Path | None = None,
    no_log: bool = True,
    active_workdir_relpath_override: str | None = None,
    subagents_enabled: bool = False,
    subagent_registry: dict[str, SubagentDefinition] | None = None,
) -> tuple[object, _FakeShellRunner, Path]:
    # Keep monkeypatched readers in this process so their directory arguments
    # can be inspected independently of the contained-worker transport.
    monkeypatch.setenv("ALYSIS_SHELL_SANDBOX_MODE", "off")
    repo = tmp_path / "repo"
    (repo / "packages" / "app").mkdir(parents=True, exist_ok=True)
    (repo / "packages" / "lib").mkdir(parents=True, exist_ok=True)
    (repo / "packages" / "b").mkdir(parents=True, exist_ok=True)
    (repo / "package.json").write_text('{"name":"repo-root"}\n', encoding="utf-8")
    (repo / "packages" / "app" / "package.json").write_text(
        '{"name":"packages-app"}\n',
        encoding="utf-8",
    )
    (repo / "packages" / "lib" / "package.json").write_text(
        '{"name":"packages-lib"}\n',
        encoding="utf-8",
    )
    _fake_git_repo(repo)
    target_path = focus_path if focus_path is not None else repo
    workspace_binding = resolve_workspace_binding(target_path, source="cwd")
    runner = _FakeShellRunner()
    monkeypatch.setattr(agent_loop_mod, "build_shell_runner", lambda **_kwargs: runner)
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            subagents_enabled=subagents_enabled,
        ),
        root=workspace_binding.workspace_context.workspace_root,
        mode="auto",
        runtime_kind="interactive_chat",
        yes=True,
        max_steps=1,
        no_log=no_log,
        api_key_override="override-key",
        console=Console(file=io.StringIO(), force_terminal=False, width=140),
        non_interactive=True,
        session_log_dir_override=tmp_path / "sessions",
        workspace_binding=workspace_binding,
        active_workdir_relpath_override=active_workdir_relpath_override,
        subagents_enabled=subagents_enabled,
        subagent_registry=subagent_registry,
    )
    return session, runner, repo


def _run_chat_command(session: object, command: str) -> tuple[object, str]:
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=4096)
    result = chat_impl_mod._handle_chat_command_impl(
        cli_mod,
        input_text=command,
        root=Path(getattr(session, "root", Path("."))),
        session=session,
        pending_images=[],
        console=console,
        forge_state=cli_mod._ForgeChatState(),
    )
    return result, buffer.getvalue()


def _workdir_subagent_registry() -> dict[str, SubagentDefinition]:
    return {
        "focused-worker": SubagentDefinition(
            name="focused-worker",
            description="Inspect the current package focus.",
            system_prompt="Inspect only the requested package and report grounded findings.",
            mode="auto",
            allow_tools=("fs_read", "search_rg", "shell_run"),
        )
    }


def test_create_session_initializes_active_workdir_from_git_subdirectory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    focus_path = tmp_path / "repo" / "packages" / "app"
    session, _runner, repo = _create_chat_session(
        tmp_path,
        monkeypatch,
        focus_path=focus_path,
    )
    try:
        expected_relpath = "packages/app"

        assert session.focus_relpath == expected_relpath
        assert session.active_workdir_relpath == expected_relpath
        assert agent_loop_mod.resolve_session_active_workdir_path(session) == focus_path.resolve()

        binding_context = _binding_context_content(session)
        assert f"workspace_root: {repo.resolve()}" in binding_context
        assert f"focus_dir: {focus_path.resolve()}" in binding_context
        assert f"active_workdir: {focus_path.resolve()}" in binding_context
        assert f"active_workdir_relpath: {expected_relpath}" in binding_context
    finally:
        session.close()


def test_create_session_active_workdir_override_initializes_session_start(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _runner, repo = _create_chat_session(
        tmp_path,
        monkeypatch,
        no_log=False,
        active_workdir_relpath_override="packages/app",
    )
    try:
        expected_path = repo / "packages" / "app"

        assert session.active_workdir_relpath == "packages/app"
        assert session.store.cwd == str(expected_path)
        events = list(read_session_events(session.store.path))
        start_event = next(event for event in events if event.get("type") == "session_start")
        assert start_event["payload"]["active_workdir_relpath"] == "packages/app"
        assert start_event["payload"]["active_workdir"] == str(expected_path)
        binding_context = _binding_context_content(session)
        assert f"active_workdir: {expected_path.resolve()}" in binding_context
        assert "active_workdir_relpath: packages/app" in binding_context
    finally:
        session.close()


def test_create_session_active_workdir_override_fails_before_log_creation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(agent_loop_mod.SessionWorkdirError):
        _create_chat_session(
            tmp_path,
            monkeypatch,
            no_log=False,
            active_workdir_relpath_override="missing-package",
        )

    sessions_dir = tmp_path / "sessions"
    assert not list(sessions_dir.glob("*.jsonl"))


def test_shell_run_without_cwd_uses_active_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    focus_path = tmp_path / "repo" / "packages" / "app"
    session, runner, _repo = _create_chat_session(
        tmp_path,
        monkeypatch,
        focus_path=focus_path,
    )
    try:
        session.tools["shell_run"].run({"cmd": "pwd"})

        assert runner.calls
        assert runner.calls[0]["cwd"] == focus_path.resolve()
    finally:
        session.close()


def test_natural_language_navigation_changes_active_workdir_and_tool_defaults(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, str]] = []

    def fake_fs_list(*, root: Path, root_path: str, globs=None, ignore=None) -> dict[str, object]:
        _ = root, globs, ignore
        captured.append(("fs_list", root_path))
        return {"root_path": root_path, "entries": []}

    def fake_search_rg(
        *, root: Path, pattern: str, root_path: str, globs=None, **kwargs: object
    ) -> dict[str, object]:
        _ = root, pattern, globs, kwargs
        captured.append(("search_rg", root_path))
        return {"root_path": root_path, "matches": []}

    def fake_symbol_search(
        *,
        root: Path,
        query: str,
        kind=None,
        root_path: str,
        globs=None,
        max_results=100,
        exact=False,
        **kwargs: object,
    ) -> dict[str, object]:
        _ = root, query, kind, globs, max_results, exact, kwargs
        captured.append(("symbol_search", root_path))
        return {"root_path": root_path, "matches": []}

    monkeypatch.setattr(agent_loop_mod, "fs_list", fake_fs_list)
    monkeypatch.setattr(agent_loop_mod, "search_rg", fake_search_rg)
    monkeypatch.setattr(agent_loop_mod, "symbol_search", fake_symbol_search)

    session, runner, repo = _create_chat_session(tmp_path, monkeypatch)
    try:
        result, output = _run_chat_command(session, "go to packages/app")
        expected_relpath = "packages/app"

        assert result == "handled"
        assert "Active workdir:" in output
        assert session.active_workdir_relpath == expected_relpath

        session.tools["shell_run"].run({"cmd": "pwd"})
        session.tools["fs_list"].run({})
        session.tools["search_rg"].run({"pattern": "TODO"})
        session.tools["symbol_search"].run({"query": "App"})

        assert runner.calls[-1]["cwd"] == (repo / "packages" / "app").resolve()
        assert captured == [
            ("fs_list", expected_relpath),
            ("search_rg", expected_relpath),
            ("symbol_search", expected_relpath),
        ]
    finally:
        session.close()


def test_natural_language_navigation_rejects_paths_that_leave_workspace_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _runner, _repo = _create_chat_session(tmp_path, monkeypatch)
    try:
        result, output = _run_chat_command(session, "go to ..")

        assert result == "handled"
        assert "escapes the bound workspace_root" in output
        assert session.active_workdir_relpath == "."
    finally:
        session.close()


def test_status_and_pwd_show_workspace_focus_and_active_workdir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    focus_path = tmp_path / "repo" / "packages" / "app"
    session, _runner, repo = _create_chat_session(
        tmp_path,
        monkeypatch,
        focus_path=focus_path,
    )
    try:
        _status_result, status_output = _run_chat_command(session, "/status")
        _pwd_result, pwd_output = _run_chat_command(session, "/pwd")
        expected_relpath = "packages/app"

        assert "workspace_root" in status_output
        assert str(repo.resolve()) in status_output
        assert "focus_dir" in status_output
        assert "active_workdir" in status_output
        assert expected_relpath in status_output
        assert focus_path.name in status_output

        assert f"active_workdir: {focus_path.resolve()}" in pwd_output
        assert f"active_workdir_relpath: {expected_relpath}" in pwd_output
        assert f"focus_dir: {focus_path.resolve()}" in pwd_output
        assert f"workspace_root: {repo.resolve()}" in pwd_output
    finally:
        session.close()


def test_session_set_workdir_tool_refreshes_binding_context_and_logging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, runner, repo = _create_chat_session(tmp_path, monkeypatch, no_log=False)
    try:
        result = session.tools["session_set_workdir"].run({"path": "packages/app"})
        session.tools["shell_run"].run({"cmd": "pwd"})
        expected_relpath = "packages/app"

        assert result["active_workdir_relpath"] == expected_relpath
        assert session.active_workdir_relpath == expected_relpath
        assert runner.calls[-1]["cwd"] == (repo / "packages" / "app").resolve()

        binding_context = _binding_context_content(session)
        assert f"active_workdir_relpath: {expected_relpath}" in binding_context
        assert f"active_workdir: {(repo / 'packages' / 'app').resolve()}" in binding_context

        events = list(read_session_events(session.store.path))
        workdir_event = next(
            event for event in events if event.get("type") == "session_workdir_changed"
        )
        cmd_event = next(event for event in events if event.get("type") == "cmd")
        assert workdir_event["payload"]["active_workdir_relpath"] == expected_relpath
        assert cmd_event["cwd"] == str((repo / "packages" / "app").resolve())
        assert cmd_event["active_workdir_relpath"] == expected_relpath
    finally:
        session.close()


def test_subagent_inherits_active_workdir_in_prompt_store_and_tool_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _workdir_subagent_registry()
    session, runner, repo = _create_chat_session(
        tmp_path,
        monkeypatch,
        no_log=False,
        subagents_enabled=True,
        subagent_registry=registry,
    )
    (repo / "README.md").write_text("root readme\n", encoding="utf-8")
    package_dir = repo / "packages" / "b"
    (package_dir / "README.md").write_text("package b readme\n", encoding="utf-8")
    search_calls: list[dict[str, object]] = []
    child_evidence: dict[str, object] = {}
    real_create_session = agent_loop_mod.create_session

    def _fake_search_rg(
        *, root: Path, pattern: str, root_path: str, **kwargs: object
    ) -> dict[str, object]:
        search_calls.append(
            {
                "root": root,
                "pattern": pattern,
                "root_path": root_path,
                **kwargs,
            }
        )
        return {"root_path": root_path, "matches": []}

    def _create_child(**kwargs: object) -> object:
        child = real_create_session(**kwargs)
        child_evidence["session"] = child

        def _run_child(_task: str) -> int:
            child_evidence["active_workdir_relpath"] = child.active_workdir_relpath
            child_evidence["store_cwd"] = child.store.cwd
            child_evidence["prompt"] = _binding_context_content(child)
            child_evidence["session_start"] = next(
                event
                for event in child.store.events_snapshot()
                if event.get("type") == "session_start"
            )
            child.tools["shell_run"].run({"cmd": "pwd"})
            child.tools["search_rg"].run({"pattern": "needle"})
            child_evidence["focused_read"] = child.tools["fs_read"].run({"path": "README.md"})[
                "content"
            ]
            child_evidence["root_read"] = child.tools["fs_read"].run(
                {"path": "README.md", "path_base": "workspace_root"}
            )["content"]
            final_text = "Focused child inspected package b without changing files."
            child.messages.append({"role": "assistant", "content": final_text})
            child.store.append("final", {"content": final_text})
            return 0

        child.run_turn = _run_child
        return child

    try:
        session.tools["session_set_workdir"].run({"path": "packages/b"})
        monkeypatch.setattr(agent_loop_mod, "search_rg", _fake_search_rg)
        monkeypatch.setattr(agent_loop_mod, "create_session", _create_child)

        result = session.tools["subagent_run"].run(
            {"name": "focused-worker", "task": "Inspect README.md in the current package."}
        )

        expected_path = package_dir.resolve()
        assert result["result"] == "Focused child inspected package b without changing files."
        assert child_evidence["active_workdir_relpath"] == "packages/b"
        assert child_evidence["store_cwd"] == str(expected_path)
        child_start = child_evidence["session_start"]
        assert isinstance(child_start, dict)
        assert child_start["payload"]["active_workdir"] == str(expected_path)
        assert child_start["payload"]["active_workdir_relpath"] == "packages/b"
        child_prompt = str(child_evidence["prompt"])
        assert f"active_workdir: {expected_path}" in child_prompt
        assert "active_workdir_relpath: packages/b" in child_prompt

        assert runner.calls[-1]["root"] == repo.resolve()
        assert runner.calls[-1]["cwd"] == expected_path
        assert search_calls[-1]["root"] == repo.resolve()
        assert search_calls[-1]["root_path"] == "packages/b"
        assert str(child_evidence["focused_read"]).replace("\r\n", "\n") == "package b readme\n"
        assert str(child_evidence["root_read"]).replace("\r\n", "\n") == "root readme\n"

        parent_events = list(read_session_events(session.store.path))
        starts = [event for event in parent_events if event.get("type") == "subagent_start"]
        ends = [event for event in parent_events if event.get("type") == "subagent_end"]
        assert len(starts) == len(ends) == 1
        child_session_id = starts[0]["payload"]["subagent_session_id"]
        assert child_session_id
        assert ends[0]["payload"]["subagent_session_id"] == child_session_id
    finally:
        session.close()


def test_subagent_missing_inherited_workdir_fails_before_client_or_tool_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = _workdir_subagent_registry()
    session, _runner, repo = _create_chat_session(
        tmp_path,
        monkeypatch,
        no_log=False,
        subagents_enabled=True,
        subagent_registry=registry,
    )
    client_construction_calls: list[dict[str, object]] = []

    def _unexpected_client(**kwargs: object) -> object:
        client_construction_calls.append(dict(kwargs))
        raise AssertionError("model client must not be constructed for an invalid child workdir")

    try:
        session.tools["session_set_workdir"].run({"path": "packages/b"})
        (repo / "packages" / "b").rmdir()
        monkeypatch.setattr(agent_loop_mod, "OpenAICompatClient", _unexpected_client)

        result = session.tools["subagent_run"].run(
            {"name": "focused-worker", "task": "Inspect the current package."}
        )

        assert "Failed to initialize subagent session" in result["error"]
        assert "Directory does not exist" in result["error"]
        assert client_construction_calls == []
        parent_events = list(read_session_events(session.store.path))
        starts = [event for event in parent_events if event.get("type") == "subagent_start"]
        ends = [event for event in parent_events if event.get("type") == "subagent_end"]
        assert len(starts) == len(ends) == 1
        assert starts[0]["payload"]["subagent_session_id"] is None
        assert ends[0]["payload"]["subagent_session_id"] is None
        assert ends[0]["payload"]["status"] == "failed"
        assert len(list((tmp_path / "sessions").glob("*.jsonl"))) == 1
    finally:
        session.close()


def test_mixed_natural_language_navigation_turn_returns_follow_up_instruction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _runner, repo = _create_chat_session(tmp_path, monkeypatch)
    try:
        result, output = _run_chat_command(
            session, "switch to packages/app and inspect package.json"
        )

        assert isinstance(result, chat_impl_mod._ChatExecutionRequest)
        assert result.instruction == "inspect package.json"
        assert "Active workdir:" in output
        assert session.active_workdir_relpath == "packages/app"
        assert session.tools["fs_read"].run({"path": "package.json"})["content"].replace(
            "\r\n", "\n"
        ) == (repo / "packages" / "app" / "package.json").read_text(encoding="utf-8")
    finally:
        session.close()


def test_fs_read_defaults_to_active_workdir_after_navigation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _runner, _repo = _create_chat_session(tmp_path, monkeypatch)
    try:
        _run_chat_command(session, "work in packages/app")

        content = session.tools["fs_read"].run({"path": "package.json"})["content"]

        assert content.replace("\r\n", "\n") == '{"name":"packages-app"}\n'
    finally:
        session.close()


def test_fs_write_defaults_to_active_workdir_after_workdir_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _runner, repo = _create_chat_session(tmp_path, monkeypatch)
    try:
        session.tools["session_set_workdir"].run({"path": "packages/app"})
        session.tools["fs_write"].run({"path": "note.txt", "content": "hello\n"})

        assert not (repo / "note.txt").exists()
        assert (repo / "packages" / "app" / "note.txt").read_text(encoding="utf-8").replace(
            "\r\n", "\n"
        ) == "hello\n"
        assert (
            session.tools["fs_read"].run({"path": "note.txt"})["content"].replace("\r\n", "\n")
            == "hello\n"
        )
    finally:
        session.close()


def test_workspace_root_override_still_works_after_workdir_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _runner, repo = _create_chat_session(tmp_path, monkeypatch)
    try:
        session.tools["session_set_workdir"].run({"path": "packages/app"})
        session.tools["fs_write"].run(
            {
                "path": "note.txt",
                "path_base": "workspace_root",
                "content": "root-level\n",
            }
        )

        assert (repo / "note.txt").read_text(encoding="utf-8").replace(
            "\r\n", "\n"
        ) == "root-level\n"
        assert not (repo / "packages" / "app" / "note.txt").exists()
        assert (
            session.tools["fs_read"]
            .run(
                {
                    "path": "package.json",
                    "path_base": "workspace_root",
                }
            )["content"]
            .replace("\r\n", "\n")
            == '{"name":"repo-root"}\n'
        )
    finally:
        session.close()


def test_tool_roots_align_on_active_workdir_with_workspace_root_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[tuple[str, str]] = []

    def fake_fs_list(*, root: Path, root_path: str, globs=None, ignore=None) -> dict[str, object]:
        _ = root, globs, ignore
        captured.append(("fs_list", root_path))
        return {"root_path": root_path, "entries": []}

    def fake_search_rg(
        *, root: Path, pattern: str, root_path: str, globs=None, **kwargs: object
    ) -> dict[str, object]:
        _ = root, pattern, globs, kwargs
        captured.append(("search_rg", root_path))
        return {"root_path": root_path, "matches": []}

    def fake_symbol_search(
        *,
        root: Path,
        query: str,
        kind=None,
        root_path: str,
        globs=None,
        max_results=100,
        exact=False,
        **kwargs: object,
    ) -> dict[str, object]:
        _ = root, query, kind, globs, max_results, exact, kwargs
        captured.append(("symbol_search", root_path))
        return {"root_path": root_path, "matches": []}

    monkeypatch.setattr(agent_loop_mod, "fs_list", fake_fs_list)
    monkeypatch.setattr(agent_loop_mod, "search_rg", fake_search_rg)
    monkeypatch.setattr(agent_loop_mod, "symbol_search", fake_symbol_search)

    session, runner, repo = _create_chat_session(tmp_path, monkeypatch)
    try:
        session.tools["session_set_workdir"].run({"path": "packages/app"})
        expected_relpath = "packages/app"

        session.tools["fs_list"].run({})
        session.tools["search_rg"].run({"pattern": "TODO"})
        session.tools["symbol_search"].run({"query": "App"})
        session.tools["shell_run"].run({"cmd": "pwd"})

        session.tools["fs_list"].run({"path_base": "workspace_root"})
        session.tools["search_rg"].run({"pattern": "TODO", "path_base": "workspace_root"})
        session.tools["symbol_search"].run({"query": "App", "path_base": "workspace_root"})
        session.tools["shell_run"].run({"cmd": "pwd", "cwd_base": "workspace_root"})

        assert runner.calls[0]["cwd"] == (repo / "packages" / "app").resolve()
        assert runner.calls[1]["cwd"] == repo.resolve()
        assert captured == [
            ("fs_list", expected_relpath),
            ("search_rg", expected_relpath),
            ("symbol_search", expected_relpath),
            ("fs_list", "."),
            ("search_rg", "."),
            ("symbol_search", "."),
        ]
    finally:
        session.close()


def test_navigation_false_positive_go_to_definition_is_not_host_handled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _runner, _repo = _create_chat_session(tmp_path, monkeypatch)
    try:
        result, output = _run_chat_command(session, "go to definition")

        assert result == "send"
        assert output == ""
        assert session.active_workdir_relpath == "."
    finally:
        session.close()


def test_navigation_false_positive_directory_name_phrase_is_not_host_handled(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _runner, _repo = _create_chat_session(tmp_path, monkeypatch)
    try:
        result, output = _run_chat_command(session, "change the directory name in README")

        assert result == "send"
        assert output == ""
        assert session.active_workdir_relpath == "."
    finally:
        session.close()


def test_natural_language_navigation_logs_active_workdir_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session, _runner, _repo = _create_chat_session(tmp_path, monkeypatch, no_log=False)
    try:
        result, _output = _run_chat_command(session, "go to packages/app")
        expected_relpath = "packages/app"

        assert result == "handled"
        events = list(read_session_events(session.store.path))
        workdir_event = next(
            event for event in events if event.get("type") == "session_workdir_changed"
        )
        assert workdir_event["payload"]["active_workdir_relpath"] == expected_relpath
        assert (
            cli_mod._load_chat_resume_active_workdir_relpath(session.store.path) == expected_relpath
        )
    finally:
        session.close()


def test_session_keeps_coding_reasoning_without_a_skill_selector(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Automatic skill choice uses the coding client's configured reasoning.
    repo = tmp_path / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    _fake_git_repo(repo)
    workspace_binding = resolve_workspace_binding(repo, source="cwd")
    monkeypatch.setattr(agent_loop_mod, "build_shell_runner", lambda **_kwargs: _FakeShellRunner())
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            web_search_mode="off",
            llm_enable_thinking=True,
            llm_reasoning_effort="high",
        ),
        root=workspace_binding.workspace_context.workspace_root,
        mode="auto",
        runtime_kind="interactive_chat",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        console=Console(file=io.StringIO(), force_terminal=False, width=140),
        non_interactive=True,
        session_log_dir_override=tmp_path / "sessions",
        workspace_binding=workspace_binding,
    )
    try:
        assert session.client.enable_thinking is True
        assert session.client.reasoning_effort == "high"
        assert session.router_client is None
        assert session.skills_auto_invoke is True
        assert "skill_read" in session.tools
    finally:
        session.close()
