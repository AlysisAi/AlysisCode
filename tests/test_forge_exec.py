from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code import forge as forge_mod
from alysis_code import workspace_binding as workspace_binding_mod
from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig
from alysis_code.conflict_auto_resolver import (
    AutoResolveOutcome,
    ConflictAutoResolveSettings,
)
from alysis_code.execution_shared import execution_private_sessions_dir
from alysis_code.forge import (
    add_task,
    create_plan_run,
    load_current_run_paths,
    load_plan,
    save_plan,
)
from alysis_code.git_ops import GitOpsError
from alysis_code.mcp.transport_stdio import live_stdio_transport_diagnostics
from alysis_code.mcp.untrusted_content import build_untrusted_mcp_text_block
from alysis_code.merge_conflict_reviewer import ConflictReviewOutcome
from alysis_code.review_gate import ReviewOutcome
from alysis_code.run_lock import write_run_mutation_lock_metadata
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult
from alysis_code.workspace_context import WORKSPACE_KIND_GIT_REPO, WorkspaceContext

_MCP_FIXTURE_SERVER = (
    Path(__file__).resolve().parent / "fixtures" / "mcp_servers" / "minimal_stdio_server.py"
)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_MAX_OUTPUT_TOKENS": "8192",
    }


def _fake_git_workspace_context(*, repo: Path, path: Path) -> WorkspaceContext:
    repo_root = repo.resolve()
    resolved = path.expanduser().resolve(strict=False)
    try:
        focus_relpath = resolved.relative_to(repo_root).as_posix()
    except ValueError:
        focus_relpath = "."
    if not focus_relpath:
        focus_relpath = "."
    return WorkspaceContext(
        input_path=resolved,
        focus_path=resolved,
        workspace_root=repo_root,
        git_root=repo_root,
        focus_relpath=focus_relpath,
        workspace_kind=WORKSPACE_KIND_GIT_REPO,
        has_head_commit=True,
        current_branch="main",
    )


def _patch_git_workspace_context(monkeypatch, *, repo: Path) -> None:
    def fake_resolve_workspace_context(
        path: Path,
        *,
        allow_subprocess: bool = True,
    ) -> WorkspaceContext:
        _ = allow_subprocess
        return _fake_git_workspace_context(repo=repo, path=path)

    monkeypatch.setattr(cli_mod, "resolve_workspace_context", fake_resolve_workspace_context)
    monkeypatch.setattr(forge_mod, "resolve_workspace_context", fake_resolve_workspace_context)
    monkeypatch.setattr(
        workspace_binding_mod,
        "resolve_workspace_context",
        fake_resolve_workspace_context,
    )


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_exec_reports_structured_timeout_when_same_run_stays_locked(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    write_run_mutation_lock_metadata(
        paths.run_dir / "active_execution.lock.json",
        {
            "schema_version": 1,
            "run_id": paths.run_id,
            "mode": "forge_swarm",
            "kind": "lock",
            "pid": os.getpid(),
            "hostname": socket.gethostname(),
            "acquired_at": "2026-03-26T00:00:00+00:00",
            "owner_token": "other-owner",
            "workspace_root": os.fspath(paths.root),
            "run_dir": os.fspath(paths.run_dir),
        },
    )

    monkeypatch.setattr(
        cli_mod,
        "run_agent",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("run_agent should not be called when the run is locked")
        ),
    )
    monkeypatch.setattr(cli_mod, "resolve_model_for_role", lambda **_kwargs: "test-model")

    result = runner.invoke(
        alysis_app,
        ["forge", "exec", "T01", "--path", os.fspath(repo)],
        env={**_env(tmp_path), "ALYSIS_FORGE_LOCK_WAIT_TIMEOUT_S": "0"},
    )

    assert result.exit_code == 2
    assert "already mutating this run" in result.output


def test_exec_releases_run_lock_when_setup_raises(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    monkeypatch.setattr(
        cli_mod,
        "run_agent",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("run_agent should not be called when setup fails")
        ),
    )
    monkeypatch.setattr(cli_mod, "resolve_model_for_role", lambda **_kwargs: "test-model")
    monkeypatch.setattr(
        cli_mod,
        "ensure_execution_dirs",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("boom-after-lock")),
    )

    result = runner.invoke(
        alysis_app,
        ["forge", "exec", "T01", "--path", os.fspath(repo)],
        env=_env(tmp_path),
        catch_exceptions=True,
    )

    assert result.exit_code == 1
    assert isinstance(result.exception, RuntimeError)
    assert str(result.exception) == "boom-after-lock"
    assert not (paths.run_dir / "active_execution.lock.json").exists()


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _latest_markdown_text(directory: Path) -> str:
    entries = sorted(directory.glob("*.md"))
    assert entries
    return entries[-1].read_text(encoding="utf-8")


def _cp(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _git_env() -> dict[str, str]:
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = ""
    env["SSH_ASKPASS"] = ""
    env["GCM_INTERACTIVE"] = "never"
    env["GIT_EDITOR"] = "true"
    env["GIT_MERGE_AUTOEDIT"] = "no"
    env["PAGER"] = "cat"
    return env


def _git_run(args: list[str], **kwargs):  # type: ignore[no-untyped-def]
    kwargs.setdefault("env", _git_env())
    kwargs.setdefault("timeout", 10)
    attempts = 2 if os.name == "nt" else 1
    for attempt in range(attempts):
        try:
            return subprocess.run(args, **kwargs)
        except KeyboardInterrupt:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.1)
    raise AssertionError("unreachable git retry state")


def _write_material_change(
    root: Path,
    *,
    rel_path: str = "src/file.py",
    content: str = "print('ok')\n",
) -> None:
    target = root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _write_tool_result_session_event(
    *,
    sessions_dir: Path,
    session_id: str,
    tool_name: str,
    result: dict,
) -> None:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    event_path = sessions_dir / f"{session_id}.jsonl"
    event = {
        "type": "tool_result",
        "session_id": session_id,
        "payload": {"name": tool_name, "result": result, "step": 1},
    }
    with event_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=True) + "\n")


def _run_agent_with_material_change(
    *,
    rel_path: str = "src/file.py",
    content: str = "print('ok')\n",
    exit_code: int = 0,
):
    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        _write_material_change(root, rel_path=rel_path, content=content)
        return exit_code

    return fake_run_agent


def _structured_capture_text(*, valid: bool = True) -> str:
    if not valid:
        return "Done.\n\n```knowledge_capture_json\nnot-json\n```"
    return "\n".join(
        [
            "Done.",
            "",
            "```knowledge_capture_json",
            json.dumps(
                {
                    "schema_version": 1,
                    "facts": [
                        {
                            "title": "Parser retries are bounded",
                            "summary": "Observed parser retry logic uses a bounded backoff.",
                            "paths": ["src/parser.py"],
                            "tags": ["parser", "retry"],
                        }
                    ],
                    "decisions": [
                        {
                            "decision_key": "parser-retry-backoff",
                            "title": "Keep bounded parser retry backoff",
                            "summary": "Use the bounded retry backoff for parser requests.",
                            "status": "active",
                            "paths": ["src/parser.py"],
                            "tags": ["parser", "retry"],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
        ]
    )


def _git_args(cmd: list[str] | str) -> list[str]:
    if isinstance(cmd, str):
        # Verification commands are executed with shell=True; emulate legacy
        # bash-style token shape so existing test dispatch remains stable.
        return ["bash", "-lc", cmd]
    if not cmd or cmd[0] != "git":
        return list(cmd)
    assert cmd[1] == "-C"
    args = cmd[3:]
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-c":
            i += 2
            continue
        cleaned.append(args[i])
        i += 1
    if cleaned in (["diff", "HEAD", "--binary", "-M"], ["diff", "--binary", "-M"]):
        return ["diff"]
    if cleaned[:2] == ["status", "--porcelain=v1"] and "-z" in cleaned:
        return ["status", "--porcelain"]
    return cleaned


def _workspace_context_git_cp(
    repo: Path, args: list[str]
) -> subprocess.CompletedProcess[str] | None:
    if args == ["rev-parse", "--show-toplevel"]:
        return _cp(stdout=os.fspath(repo) + "\n")
    if args == ["symbolic-ref", "--quiet", "--short", "HEAD"]:
        return _cp(stdout="main\n")
    if args == ["rev-parse", "--verify", "HEAD"]:
        return _cp(stdout="deadbeef\n")
    if args == ["rev-parse", "HEAD"]:
        return _cp(stdout="deadbeef\n")
    if args in (
        ["ls-files", "--cached", "-z"],
        ["ls-files", "--others", "--exclude-standard", "-z"],
        ["ls-files", "--cached", "--others", "--exclude-standard", "-z"],
    ):
        rels = [
            path.resolve().relative_to(repo.resolve()).as_posix()
            for path in sorted(repo.rglob("*"))
            if path.is_file() and ".git" not in path.parts and ".alysis" not in path.parts
        ]
        payload = b"".join(rel.encode("utf-8") + b"\0" for rel in rels)
        return subprocess.CompletedProcess(
            args=["git"],
            returncode=0,
            stdout=payload,
            stderr=b"",
        )
    if len(args) == 5 and args[:3] == ["diff", "--no-index", "--"]:
        rel_path = args[4]
        file_path = repo / rel_path
        if not file_path.exists():
            return _cp(returncode=0, stdout="")
        return _cp(
            returncode=1,
            stdout=(
                f"diff --git a/{rel_path} b/{rel_path}\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                f"+++ b/{rel_path}\n"
            ),
        )
    return None


def _init_git_repo_with_commit(repo: Path) -> None:
    _git_run(
        ["git", "-C", os.fspath(repo.parent), "init", os.fspath(repo.name)],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        ["git", "-C", os.fspath(repo), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        ["git", "-C", os.fspath(repo), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
        text=True,
    )
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git_run(
        ["git", "-C", os.fspath(repo), "add", "README.md"],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        [
            "git",
            "-C",
            os.fspath(repo),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "init",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _prepare_run_with_tasks(
    runner: CliRunner,
    repo: Path,
    tmp_path: Path,
    *,
    pointer_root: Path | None = None,
) -> tuple[Path, dict]:
    del runner, tmp_path, pointer_root
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Execute tasks safely"
    plan["summary"] = "Execute tasks safely"
    task = add_task(
        plan,
        title="Implement feature slice",
        description="Task created from planning chat: Implement feature slice",
        estimated_files=[
            "README.md",
            "implemented.txt",
            "new_file.py",
            "src/generated.py",
            "src/parser.py",
            "src/file.py",
        ],
    )
    task["write_scope"] = []
    save_plan(paths, plan)
    reloaded = load_plan(paths)
    assert task["id"] == reloaded["tasks"][0]["id"]
    return paths.plan_json_path, reloaded


def _prepare_run_with_task_titles(
    runner: CliRunner,
    repo: Path,
    tmp_path: Path,
    *,
    task_titles: list[str],
) -> tuple[Path, dict]:
    del runner, tmp_path
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Execute tasks safely"
    plan["summary"] = "Execute tasks safely"
    for title in task_titles:
        estimated_files = None
        title_key = title.casefold()
        if "file a" in title_key:
            estimated_files = ["a.txt"]
        elif "file b" in title_key:
            estimated_files = ["b.txt"]
        add_task(
            plan,
            title=title,
            description=f"Task created from planning chat: {title}",
            estimated_files=estimated_files,
        )
    save_plan(paths, plan)
    return paths.plan_json_path, load_plan(paths)


def _prepare_dependency_plan(
    repo: Path,
    *,
    task_specs: list[tuple[str, str]],
) -> tuple[Path, dict]:
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Check task dependencies"
    plan["summary"] = "Check task dependencies"
    for title, rel_path in task_specs:
        add_task(
            plan,
            title=title,
            description=f"Task created for dependency coverage: {title}",
            estimated_files=[rel_path],
        )
    save_plan(paths, plan)
    return paths.plan_json_path, load_plan(paths)


def _write_mcp_stdio_config(
    tmp_path: Path,
    fixture_payload: dict,
    *,
    server_overrides: dict[str, object] | None = None,
) -> None:
    fixture_path = tmp_path / "mcp-fixture.json"
    _write_json(fixture_path, fixture_payload)
    server_payload: dict[str, object] = {
        "transport": "stdio",
        "command": sys.executable,
        "args": [os.fspath(_MCP_FIXTURE_SERVER)],
        "env": {
            "ALYSIS_TEST_MCP_CONFIG": os.fspath(fixture_path),
        },
    }
    if server_overrides:
        server_payload.update(server_overrides)
    _write_json(
        tmp_path / "cfg" / "mcp.json",
        {
            "servers": {
                "alpha": server_payload,
            }
        },
    )


def _run_mcp_scoped_exec_case(
    *,
    case_tmp_path: Path,
    monkeypatch,
    fixture_payload: dict,
    task_mcp_scope: dict[str, object],
    fake_run_agent,
    server_overrides: dict[str, object] | None = None,
):
    case_tmp_path.mkdir(parents=True, exist_ok=True)
    runner = CliRunner()
    repo = case_tmp_path / "repo"
    repo.mkdir()
    _write_mcp_stdio_config(
        case_tmp_path,
        fixture_payload,
        server_overrides=server_overrides,
    )
    plan_path, plan = _prepare_run_with_tasks(runner, repo, case_tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    task["mcp_scope"] = task_mcp_scope
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(case_tmp_path),
    )
    return result, repo, task_id


def test_execution_private_sessions_dir_stays_outside_workspace_when_session_log_dir_is_under_repo(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    cfg = AppConfig(session_log_dir=os.fspath(repo / ".alysis" / "sessions"))

    runtime_dir = execution_private_sessions_dir(
        cfg=cfg,
        run_id="run-123",
        task_id="T01",
        workspace_root=repo,
    ).resolve()

    assert repo.resolve() not in runtime_dir.parents
    assert (repo / ".alysis").resolve() not in runtime_dir.parents


def test_exec_loads_current_run_from_git_subdir(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subdir = repo / "pkg" / "feature"
    subdir.mkdir(parents=True)
    _patch_git_workspace_context(monkeypatch, repo=repo)
    plan_path, _plan = _prepare_run_with_tasks(CliRunner(), subdir, tmp_path, pointer_root=repo)

    paths = load_current_run_paths(subdir)
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    assert paths.root == repo.resolve()
    assert paths.focus_path == subdir.resolve()
    assert paths.focus_relpath == "pkg/feature"
    assert paths.run_dir == (repo / pointer["run_path"]).resolve()
    assert plan_path.exists()


def test_exec_runs_for_bootstrapped_workspace(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "greenfield" / "repo"

    paths = create_plan_run(repo, create_if_missing=True)
    plan = load_plan(paths)
    plan["project_goal"] = "New workspace"
    plan["summary"] = "New workspace"
    task = add_task(
        plan,
        title="Implement task",
        description="Task created from planning chat: Implement task",
        estimated_files=["implemented.txt"],
    )
    save_plan(paths, plan)
    task_id = task["id"]
    captured: dict[str, Path] = {}
    assert paths.has_head_commit is False

    def fail_head_commit(*_args, **_kwargs) -> str:
        raise KeyboardInterrupt("plain bootstrapped workspace should not probe git HEAD")

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        captured["root"] = root
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "head_commit", fail_head_commit)
    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    exec_result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert exec_result.exit_code == 0, (
        f"exit={exec_result.exit_code} "
        f"exception={exec_result.exception!r} "
        f"output={exec_result.output!r} "
        f"captured={captured!r}"
    )
    assert captured["root"] == repo.resolve()


def test_exec_sets_in_progress_then_done_and_writes_outputs(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    seen: dict[str, bool] = {
        "in_progress_seen": False,
        "compaction_disabled": False,
        "tool_output_offload_enabled": False,
        "conversation_summarization_enabled": False,
        "chat_turn_step_budget_disabled": False,
        "subagents_disabled": False,
        "execution_compaction_profile": False,
    }
    captured_session_dir: dict[str, Path | None] = {"value": None}

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        current = _load_json(plan_path)
        current_task = current["tasks"][0]
        seen["in_progress_seen"] = current_task["status"] == "in_progress"
        seen["compaction_disabled"] = _kwargs.get("enable_compaction") is False
        seen["tool_output_offload_enabled"] = _kwargs.get("enable_tool_output_offload") is True
        seen["conversation_summarization_enabled"] = (
            _kwargs.get("enable_conversation_summarization") is True
        )
        seen["chat_turn_step_budget_disabled"] = (
            _kwargs.get("enable_chat_turn_step_budget") is False
        )
        seen["subagents_disabled"] = _kwargs.get("subagents_enabled") is False
        seen["execution_compaction_profile"] = _kwargs.get("compaction_profile") == "execution"
        captured = _kwargs.get("session_log_dir_override")
        captured_session_dir["value"] = captured if isinstance(captured, Path) else None
        # Touch a file so the agent appears to have acted.
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert seen["in_progress_seen"] is True
    assert seen["compaction_disabled"] is True
    assert seen["tool_output_offload_enabled"] is True
    assert seen["conversation_summarization_enabled"] is True
    assert seen["chat_turn_step_budget_disabled"] is True
    assert seen["subagents_disabled"] is True
    assert seen["execution_compaction_profile"] is True
    assert captured_session_dir["value"] is not None
    runtime_sessions_dir = captured_session_dir["value"].resolve()  # type: ignore[union-attr]
    assert repo.resolve() not in runtime_sessions_dir.parents
    assert (tmp_path / "data" / "sessions").resolve() not in runtime_sessions_dir.parents
    assert runtime_sessions_dir.name == task_id

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    report_path = run_dir / "execution" / "reports" / f"{task_id}.md"
    patch_path = run_dir / "execution" / "patches" / f"{task_id}.diff"
    context_path = run_dir / "execution" / "context" / f"{task_id}_context.md"
    budget_path = run_dir / "execution" / "budgets" / f"{task_id}.json"
    assert report_path.exists()
    assert patch_path.exists()
    assert context_path.exists()
    assert budget_path.exists()

    budget_payload = _load_json(budget_path)
    assert budget_payload["model"] == "test-model"
    assert budget_payload["pinned_prefix_token_estimate"] > 0
    assert budget_payload["tool_schema_token_estimate"] > 0
    assert budget_payload["requested_execution_response_reserve_tokens"] > 0
    assert budget_payload["effective_execution_response_reserve_tokens"] > 0
    assert (
        budget_payload["execution_response_reserve_tokens"]
        == budget_payload["effective_execution_response_reserve_tokens"]
    )
    assert budget_payload["requested_execution_headroom_reserve_tokens"] > 0
    assert budget_payload["effective_execution_headroom_reserve_tokens"] > 0
    assert budget_payload["execution_headroom_reserve_tokens"] > 0
    assert (
        budget_payload["execution_headroom_reserve_tokens"]
        == budget_payload["effective_execution_headroom_reserve_tokens"]
    )
    assert budget_payload["minimum_instruction_budget_tokens"] > 0
    assert "reserve_adjustment_applied" in budget_payload
    assert "reserve_adjustment_reason" in budget_payload
    assert budget_payload["truncation_strategy"].startswith("execution_priority")
    assert budget_payload["subagents_enabled"] is False
    assert budget_payload["step_budget"]["kind"] == "managed_task"
    assert budget_payload["step_budget"]["reason"] == "autonomous_unbounded"
    assert budget_payload["step_budget"]["resolved_max_steps"] is None
    assert budget_payload["image_count"] == 0
    assert budget_payload["image_budget_reserve_tokens"] == 0
    assert budget_payload["initial_request_token_estimate"] > 0
    assert budget_payload["compaction_budget_tokens"] > 0
    assert budget_payload["compaction_trigger_tokens"] > 0
    assert budget_payload["startup_target_tokens"] < budget_payload["compaction_trigger_tokens"]
    assert (
        budget_payload["startup_headroom_tokens"]
        == budget_payload["startup_target_tokens"]
        - budget_payload["initial_request_token_estimate"]
    )
    if budget_payload["startup_headroom_adjustment_applied"]:
        assert (
            budget_payload["initial_request_token_estimate_before_adjustment"]
            >= (budget_payload["initial_request_token_estimate"])
        )
        assert budget_payload["startup_headroom_adjustment_reason"] is not None
    else:
        assert "initial_request_token_estimate_before_adjustment" not in budget_payload
        if (
            budget_payload["initial_request_token_estimate"]
            > budget_payload["startup_target_tokens"]
        ):
            assert budget_payload["startup_headroom_adjustment_reason"] is not None
        else:
            assert budget_payload["startup_headroom_adjustment_reason"] is None
    assert (
        budget_payload["final_instruction_token_estimate"]
        <= budget_payload["final_instruction_budget"]
    )
    assert (
        f"Instruction budget (tokens): `{budget_payload['final_instruction_budget']}`"
        in context_path.read_text(encoding="utf-8")
    )


def test_exec_reports_untracked_only_changes_in_patch_report_attempt_and_issue(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        created = root / "src" / "generated.py"
        created.parent.mkdir(parents=True, exist_ok=True)
        created.write_text("print('generated')\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1

    paths = load_current_run_paths(repo)
    report_path = paths.execution_reports_dir / f"{task_id}.md"
    patch_path = paths.execution_patches_dir / f"{task_id}.diff"
    attempt_dir = paths.knowledge_task_attempts_dir / task_id
    issue_dir = paths.knowledge_issues_dir / task_id

    patch_text = patch_path.read_text(encoding="utf-8")
    assert "diff --git a/src/generated.py b/src/generated.py" in patch_text
    assert any(f"new file mode {mode}" in patch_text for mode in ("100644", "100755"))

    report_text = report_path.read_text(encoding="utf-8")
    assert "`src/generated.py`" in report_text
    assert "(none detected)" not in report_text

    attempt_text = _latest_markdown_text(attempt_dir)
    assert "`src/generated.py`" in attempt_text

    issue_text = _latest_markdown_text(issue_dir)
    assert "`src/generated.py`" in issue_text


def test_exec_reports_tracked_and_untracked_changes_together(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "README.md").write_text("repo\nupdated\n", encoding="utf-8")
        created = root / "src" / "generated.py"
        created.parent.mkdir(parents=True, exist_ok=True)
        created.write_text("print('generated')\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0

    paths = load_current_run_paths(repo)
    report_path = paths.execution_reports_dir / f"{task_id}.md"
    patch_path = paths.execution_patches_dir / f"{task_id}.diff"
    attempt_dir = paths.knowledge_task_attempts_dir / task_id

    patch_text = patch_path.read_text(encoding="utf-8")
    assert "diff --git a/README.md b/README.md" in patch_text
    assert "diff --git a/src/generated.py b/src/generated.py" in patch_text

    report_text = report_path.read_text(encoding="utf-8")
    assert report_text.index("`README.md`") < report_text.index("`src/generated.py`")
    assert "`README.md`" in report_text
    assert "`src/generated.py`" in report_text

    attempt_text = _latest_markdown_text(attempt_dir)
    assert "`README.md`" in attempt_text
    assert "`src/generated.py`" in attempt_text


def test_exec_non_pr_nonzero_with_material_changes_still_fails(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    paths = load_current_run_paths(repo)
    report_text = (paths.execution_reports_dir / f"{task_id}.md").read_text(encoding="utf-8")
    assert "`implemented.txt`" in report_text
    assert "PR flow salvaged a non-zero agent exit" not in report_text


def test_exec_uses_task_local_delta_when_repo_starts_dirty(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_commit(repo)
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo / "b.txt").write_text("bravo\n", encoding="utf-8")
    _git_run(
        ["git", "-C", os.fspath(repo), "add", "a.txt", "b.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        [
            "git",
            "-C",
            os.fspath(repo),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "seed",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["b.txt"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    (repo / "a.txt").write_text("alpha dirty before task\n", encoding="utf-8")

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "b.txt").write_text("bravo task change\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"

    paths = load_current_run_paths(repo)
    report_text = (paths.execution_reports_dir / f"{task_id}.md").read_text(encoding="utf-8")
    patch_text = (paths.execution_patches_dir / f"{task_id}.diff").read_text(encoding="utf-8")
    attempt_text = _latest_markdown_text(paths.knowledge_task_attempts_dir / task_id)
    issue_text = _latest_markdown_text(paths.knowledge_issues_dir / task_id)

    assert "`b.txt`" in report_text
    assert "`a.txt`" not in report_text
    assert "Task blocked due to strict scope isolation." not in report_text
    assert "diff --git a/b.txt b/b.txt" in patch_text
    assert "a.txt" not in patch_text
    assert "`b.txt`" in attempt_text
    assert "`a.txt`" not in attempt_text
    assert "`b.txt`" in issue_text
    assert "`a.txt`" not in issue_text


def test_exec_uses_task_local_delta_for_second_sequential_dirty_task(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_commit(repo)
    (repo / "a.txt").write_text("alpha\n", encoding="utf-8")
    (repo / "b.txt").write_text("bravo\n", encoding="utf-8")
    _git_run(
        ["git", "-C", os.fspath(repo), "add", "a.txt", "b.txt"],
        check=True,
        capture_output=True,
        text=True,
    )
    _git_run(
        [
            "git",
            "-C",
            os.fspath(repo),
            "-c",
            "commit.gpgsign=false",
            "commit",
            "--no-gpg-sign",
            "--no-verify",
            "-m",
            "seed",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    plan_path, plan = _prepare_run_with_task_titles(
        runner,
        repo,
        tmp_path,
        task_titles=["Leave file A dirty", "Update file B"],
    )
    task_one = plan["tasks"][0]
    task_two = plan["tasks"][1]
    task_one["estimated_files"] = ["a.txt"]
    task_two["estimated_files"] = ["b.txt"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    call_count = {"value": 0}

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        call_count["value"] += 1
        if call_count["value"] == 1:
            (root / "a.txt").write_text("alpha task one change\n", encoding="utf-8")
            return 1
        (root / "b.txt").write_text("bravo task two change\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result_one = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_one["id"],
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert result_one.exit_code == 1

    result_two = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_two["id"],
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert result_two.exit_code == 1

    paths = load_current_run_paths(repo)
    task_two_id = str(task_two["id"])
    report_text = (paths.execution_reports_dir / f"{task_two_id}.md").read_text(encoding="utf-8")
    patch_text = (paths.execution_patches_dir / f"{task_two_id}.diff").read_text(encoding="utf-8")
    attempt_text = _latest_markdown_text(paths.knowledge_task_attempts_dir / task_two_id)
    issue_text = _latest_markdown_text(paths.knowledge_issues_dir / task_two_id)

    assert "`b.txt`" in report_text
    assert "`a.txt`" not in report_text
    assert "Task blocked due to strict scope isolation." not in report_text
    assert "diff --git a/b.txt b/b.txt" in patch_text
    assert "a.txt" not in patch_text
    assert "`b.txt`" in attempt_text
    assert "`a.txt`" not in attempt_text
    assert "`b.txt`" in issue_text
    assert "`a.txt`" not in issue_text


def test_exec_uses_safe_external_runtime_sessions_dir_even_when_session_log_dir_is_under_repo(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]
    captured_runtime_dir: dict[str, Path | None] = {"value": None}

    original_load_config = cli_mod.load_config

    def fake_load_config() -> AppConfig:
        cfg = original_load_config()
        cfg.session_log_dir = os.fspath(repo / ".alysis" / "sessions")
        return cfg

    def fake_run_agent(*, root: Path, **kwargs) -> int:
        assert root == repo.resolve()
        captured = kwargs.get("session_log_dir_override")
        captured_runtime_dir["value"] = captured if isinstance(captured, Path) else None
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "load_config", fake_load_config)
    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured_runtime_dir["value"] is not None
    runtime_dir = captured_runtime_dir["value"].resolve()  # type: ignore[union-attr]
    assert repo.resolve() not in runtime_dir.parents
    assert (repo / ".alysis").resolve() not in runtime_dir.parents


def test_exec_verify_off_disables_verify_tool_exposure(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    captured: dict[str, object] = {}

    def fake_run_agent(*, root: Path, **kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["verification_enabled"] = kwargs.get("verification_enabled")
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["verification_enabled"] is False
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    budget_path = repo / pointer["run_path"] / "execution" / "budgets" / f"{task_id}.json"
    budget_payload = _load_json(budget_path)
    assert budget_payload["step_budget"]["signals_used"]["verification_enabled"] is False


def test_exec_passes_runtime_kind_to_run_agent(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]
    captured: dict[str, object] = {}

    def fake_run_agent(*, root: Path, **kwargs) -> int:
        assert root == repo.resolve()
        captured["runtime_kind"] = kwargs.get("runtime_kind")
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["runtime_kind"] == RuntimeKind.FORGE_EXEC


def test_exec_uses_autonomous_unbounded_managed_task_policy(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["attempts"] = 2
    task["acceptance_criteria"] = [f"criterion {idx}" for idx in range(1, 9)]
    task["estimated_files"] = [f"src/file_{idx}.py" for idx in range(1, 9)]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    original_load_config = cli_mod.load_config
    captured: dict[str, object] = {}

    def fake_load_config() -> AppConfig:
        cfg = original_load_config()
        cfg.step_budget_policy = "adaptive"
        cfg.max_steps = 77
        cfg.task_max_steps = 31
        return cfg

    def fake_run_agent(*, root: Path, **kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["max_steps"] = kwargs.get("max_steps")
        captured["enable_chat_turn_step_budget"] = kwargs.get("enable_chat_turn_step_budget")
        captured["subagents_enabled"] = kwargs.get("subagents_enabled")
        target = root / "src" / "file_1.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "load_config", fake_load_config)
    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["max_steps"] is None
    assert captured["enable_chat_turn_step_budget"] is False
    assert captured["subagents_enabled"] is False

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    budget_path = repo / pointer["run_path"] / "execution" / "budgets" / f"{task_id}.json"
    budget_payload = _load_json(budget_path)
    assert budget_payload["step_budget"]["hard_cap"] is None
    assert budget_payload["step_budget"]["resolved_max_steps"] is None
    assert budget_payload["step_budget"]["reason"] == "autonomous_unbounded"
    assert budget_payload["step_budget"]["unlimited"] is True
    assert budget_payload["step_budget"]["signals_used"]["attempt_count"] == 3


def test_exec_max_steps_cli_acts_as_explicit_execution_limit(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    original_load_config = cli_mod.load_config
    captured: dict[str, object] = {}

    def fake_load_config() -> AppConfig:
        cfg = original_load_config()
        cfg.step_budget_policy = "fixed"
        cfg.max_steps = 99
        cfg.task_max_steps = 40
        return cfg

    def fake_run_agent(*, root: Path, **kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["max_steps"] = kwargs.get("max_steps")
        captured["enable_chat_turn_step_budget"] = kwargs.get("enable_chat_turn_step_budget")
        captured["subagents_enabled"] = kwargs.get("subagents_enabled")
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "load_config", fake_load_config)
    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--max-steps",
            "7",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["max_steps"] == 7
    assert captured["enable_chat_turn_step_budget"] is False
    assert captured["subagents_enabled"] is False

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    budget_path = repo / pointer["run_path"] / "execution" / "budgets" / f"{task_id}.json"
    budget_payload = _load_json(budget_path)
    assert budget_payload["step_budget"]["resolved_max_steps"] == 7
    assert budget_payload["step_budget"]["reason"] == "explicit_limit"
    assert budget_payload["step_budget"]["override_applied"] is True
    assert budget_payload["step_budget"]["hard_cap"] == 7


def test_exec_propagates_authoritative_verify_commands_into_managed_session(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    captured: dict[str, object] = {}

    def fake_run_agent(*, cfg, root: Path, **kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["verification_enabled"] = kwargs.get("verification_enabled")
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["cfg_verify_commands"] = list(cfg.verify_commands)
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--verify",
            "strict",
            "--verify-cmd",
            "PYTHONPATH=src pytest -q",
            "--verify-cmd",
            "ruff check .",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["verification_enabled"] is True
    assert captured["authoritative_verification_commands"] == [
        "PYTHONPATH=src pytest -q",
        "ruff check .",
    ]
    assert captured["cfg_verify_commands"] == [
        "PYTHONPATH=src pytest -q",
        "ruff check .",
    ]


def test_exec_prefers_repo_inferred_verify_commands_over_generic_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "package.json").write_text(
        '{"scripts":{"test":"vitest run"}}\n',
        encoding="utf-8",
    )
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    captured: dict[str, object] = {}

    def fake_run_agent(*, cfg, root: Path, **kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["root"] = root
        captured["verification_enabled"] = kwargs.get("verification_enabled")
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["cfg_verify_commands"] = list(cfg.verify_commands)
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--verify",
            "strict",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["root"] == repo.resolve()
    assert captured["verification_enabled"] is True
    assert captured["authoritative_verification_commands"] == ["npm test"]
    assert captured["cfg_verify_commands"] == ["npm test"]


def test_exec_refines_generic_fallback_to_node_test_for_js_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task["estimated_files"] = ["test/app.test.js", "src/app.js"]
    task["write_scope"] = ["test/app.test.js", "src/app.js"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run_agent(*, cfg, root: Path, **kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["root"] = root
        captured["verification_enabled"] = kwargs.get("verification_enabled")
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["cfg_verify_commands"] = list(cfg.verify_commands)
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "app.js").write_text("export const ok = true;\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            str(task["id"]),
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--verify",
            "strict",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["root"] == repo.resolve()
    assert captured["verification_enabled"] is True
    assert captured["authoritative_verification_commands"] == ["node --test"]
    assert captured["cfg_verify_commands"] == ["node --test"]


def test_exec_uses_coding_role_model_from_env(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    captured: dict[str, str] = {}

    def fake_run_agent(*, cfg, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        captured["model"] = cfg.model
        (root / "implemented.txt").write_text("done\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    env = _env(tmp_path)
    env["ALYSIS_MODEL_CODING"] = "env-coding-model"
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--api-key",
            "k",
            "--no-log",
        ],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["model"] == "env-coding-model"

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"


def test_exec_fails_for_missing_task_id(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run_with_tasks(runner, repo, tmp_path)

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        raise AssertionError("run_agent should not be called for missing task id")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            "T99",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 2
    assert "Task not found" in result.output


def test_exec_enforces_dependencies(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    plan_path, plan = _prepare_dependency_plan(
        repo,
        task_specs=[
            ("Task one", "src/one.py"),
            ("Task two", "src/two.py"),
        ],
    )
    first_id = plan["tasks"][0]["id"]
    second_id = plan["tasks"][1]["id"]
    plan["tasks"][1]["dependencies"] = [first_id]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        raise AssertionError("run_agent should not be called when dependencies are blocked")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    exec_result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            second_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert exec_result.exit_code == 2
    assert "Dependencies are not done" in exec_result.output

    final_plan = _load_json(plan_path)
    second_task = next(t for t in final_plan["tasks"] if t["id"] == second_id)
    assert second_task["status"] == "planned"


def test_exec_blocks_inferred_ordered_dependency_even_if_plan_lacks_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    plan_path, plan = _prepare_dependency_plan(
        repo,
        task_specs=[
            ("Update src/calc.py helper behavior", "src/calc.py"),
            ("Update src/app.py caller after helper behavior", "src/app.py"),
        ],
    )
    first_id = plan["tasks"][0]["id"]
    second_id = plan["tasks"][1]["id"]
    plan["tasks"][1]["dependencies"] = []
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        raise AssertionError("run_agent should not be called when inferred dependency is blocked")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    exec_result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            second_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert exec_result.exit_code == 2
    assert f"{first_id} (planned, inferred)" in exec_result.output


def test_exec_marks_failed_when_agent_returns_nonzero(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        return 1

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"


def test_exec_writes_execution_log_artifacts(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    captured_runtime_dir: dict[str, Path | None] = {"value": None}

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        sessions_dir = _kwargs.get("session_log_dir_override")
        session_id = str(_kwargs.get("session_id_override") or "")
        assert isinstance(sessions_dir, Path)
        assert session_id == task_id
        captured_runtime_dir["value"] = sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{session_id}.jsonl").write_text(
            '{"type":"session_start"}\n',
            encoding="utf-8",
        )
        tool_outputs_dir = sessions_dir / session_id / "tool_outputs"
        tool_outputs_dir.mkdir(parents=True, exist_ok=True)
        (tool_outputs_dir / "step001_fs_read_tc.json").write_text(
            '{"tool_name":"fs_read","result":{"content":"big"}}\n',
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    log_copy = run_dir / "execution" / "logs" / f"{task_id}.jsonl"
    log_pointer = run_dir / "execution" / "logs" / f"{task_id}.log.json"
    report_path = run_dir / "execution" / "reports" / f"{task_id}.md"
    session_artifact = (
        run_dir
        / "execution"
        / "sessions"
        / f"{task_id}"
        / "tool_outputs"
        / "step001_fs_read_tc.json"
    )

    assert log_copy.exists()
    assert log_pointer.exists()
    assert report_path.exists()
    assert session_artifact.exists()
    assert captured_runtime_dir["value"] is not None
    assert not captured_runtime_dir["value"].exists()  # type: ignore[union-attr]
    assert not (tmp_path / "data" / "sessions" / f"{task_id}.jsonl").exists()

    pointer_data = _load_json(log_pointer)
    report_text = report_path.read_text(encoding="utf-8")
    run_path = Path(pointer["run_path"]).as_posix()
    assert pointer_data["task_id"] == task_id
    assert pointer_data["logging_enabled"] is True
    assert pointer_data["log_retained"] is True
    assert pointer_data["session_artifacts_retained"] is True
    assert pointer_data["session_id"] == task_id
    assert pointer_data["copied_log_path"] == f"{run_path}/execution/logs/{task_id}.jsonl"
    assert pointer_data["source_log_path"] == f"[redacted host path: {task_id}.jsonl]"
    assert pointer_data["session_artifact_dir"] == f"{run_path}/execution/sessions/{task_id}"
    assert os.fspath(tmp_path) not in json.dumps(pointer_data, sort_keys=True)
    assert f"- Source Log Path: `[redacted host path: {task_id}.jsonl]`" in report_text
    assert os.fspath(tmp_path) not in report_text


def test_exec_no_log_cleans_up_private_runtime_sessions_dir(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]
    captured_runtime_dir: dict[str, Path | None] = {"value": None}

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        sessions_dir = _kwargs.get("session_log_dir_override")
        session_id = str(_kwargs.get("session_id_override") or "")
        assert isinstance(sessions_dir, Path)
        captured_runtime_dir["value"] = sessions_dir
        sessions_dir.mkdir(parents=True, exist_ok=True)
        (sessions_dir / f"{session_id}.jsonl").write_text(
            '{"type":"session_start"}\n',
            encoding="utf-8",
        )
        tool_outputs_dir = sessions_dir / session_id / "tool_outputs"
        tool_outputs_dir.mkdir(parents=True, exist_ok=True)
        (tool_outputs_dir / "step001_shell_run_tc.json").write_text(
            '{"tool_name":"shell_run","result":{"stdout":"big"}}\n',
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured_runtime_dir["value"] is not None
    assert not captured_runtime_dir["value"].exists()  # type: ignore[union-attr]
    assert not (tmp_path / "data" / "sessions").exists()

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    assert not (run_dir / "execution" / "logs" / f"{task_id}.jsonl").exists()
    assert not (run_dir / "execution" / "sessions" / task_id).exists()
    log_pointer = run_dir / "execution" / "logs" / f"{task_id}.log.json"
    report_path = run_dir / "execution" / "reports" / f"{task_id}.md"
    assert log_pointer.exists()
    pointer_data = _load_json(log_pointer)
    assert pointer_data["logging_enabled"] is False
    assert pointer_data["log_retained"] is False
    assert pointer_data["copied_log_path"] is None
    assert pointer_data["session_artifacts_retained"] is False
    assert pointer_data["session_artifact_dir"] is None
    assert pointer_data["cleanup_note"] == "temporary runtime session artifacts will be cleaned up"
    report_text = report_path.read_text(encoding="utf-8")
    assert "- Session Logging: disabled (--no-log)" in report_text
    assert "- Execution Log: (not retained)" in report_text
    assert "- Session Artifacts: (none retained)" in report_text


def test_exec_instruction_includes_attached_assets(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    source = repo / "brief.txt"
    source.write_text("Asset text content\n", encoding="utf-8")
    attach = runner.invoke(
        alysis_app,
        ["forge", "attach", os.fspath(source), "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert attach.exit_code == 0

    captured: dict[str, str] = {}

    def fake_run_agent(*, instruction: str, **_kwargs) -> int:
        captured["instruction"] = instruction
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    plan_data = _load_json(plan_path)
    asset_index = _load_json(plan_path.parents[1] / "assets" / "index.json")
    asset = asset_index["assets"][0]
    instruction = captured["instruction"]
    assert plan_data["schema_version"] == 2
    assert plan_data["assets"] == []
    assert plan_data.get("legacy_assets_migrated_at")
    assert asset["original_filename"] == "brief.txt"
    assert asset["extracted_text_path"] == asset["stored_path"]
    assert asset["added_by"]["legacy_stored_path"].endswith("/brief.txt")
    assert "# Task Context Pack" in instruction
    assert "## Selected Assets" not in instruction
    assert asset["stored_path"] not in instruction
    assert "You may read attached plan assets as needed" in instruction


def test_exec_passes_task_images_when_opted_in_and_model_supports_vision(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    image_asset = repo / "diagram.png"
    image_asset.write_bytes(b"not-a-real-png-but-ok-for-tests")
    attach = runner.invoke(
        alysis_app,
        ["forge", "attach", os.fspath(image_asset), "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert attach.exit_code == 0

    captured: dict[str, list[str] | None] = {"image_paths": None}

    def fake_run_agent(*, image_paths: list[str] | None = None, **_kwargs) -> int:
        captured["image_paths"] = image_paths
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = _env(tmp_path)
    env["ALYSIS_TASK_IMAGES"] = "1"
    env["ALYSIS_SUPPORTS_VISION"] = "1"

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=env,
    )
    assert result.exit_code == 0
    image_paths = captured["image_paths"] or []
    assert len(image_paths) == 1
    assert str(image_paths[0]).endswith("diagram.png")

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    budget_path = repo / pointer["run_path"] / "execution" / "budgets" / f"{task_id}.json"
    budget_payload = _load_json(budget_path)
    assert budget_payload["subagents_enabled"] is False
    assert budget_payload["image_count"] == 1
    assert budget_payload["image_budget_reserve_tokens"] > 0
    assert budget_payload["step_budget"]["signals_used"]["image_count"] == 1


def test_exec_does_not_pass_task_images_without_vision_support(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    image_asset = repo / "diagram.png"
    image_asset.write_bytes(b"not-a-real-png-but-ok-for-tests")
    attach = runner.invoke(
        alysis_app,
        ["forge", "attach", os.fspath(image_asset), "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert attach.exit_code == 0

    captured: dict[str, list[str] | None] = {"image_paths": None}

    def fake_run_agent(*, image_paths: list[str] | None = None, **_kwargs) -> int:
        captured["image_paths"] = image_paths
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    env = _env(tmp_path)
    env["ALYSIS_TASK_IMAGES"] = "1"
    env["ALYSIS_SUPPORTS_VISION"] = "0"

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=env,
    )
    assert result.exit_code == 0
    assert captured["image_paths"] is None


def test_exec_defaults_scope_to_strict_and_passes_allow_write_globs_to_agent(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    captured: dict[str, list[str] | None] = {"allow": None}

    def fake_run_agent(*, root: Path, allow_write_globs, **_kwargs):  # type: ignore[no-untyped-def]
        captured["allow"] = allow_write_globs
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        cli_mod,
        "_build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(changed_files=("src/in_scope.py",), patch_text="PATCH\n"),
    )

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert captured["allow"] == ["src/in_scope.py"]


def test_exec_rejects_successful_agent_run_without_material_changes_for_write_scope_task(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["reports/tool_error_result.txt"]
    task["write_scope"] = ["reports/tool_error_result.txt"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    monkeypatch.setattr(cli_mod, "run_agent", lambda **_kwargs: 0)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    report_text = (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")
    assert "no material file changes were detected" in report_text
    assert not (repo / "reports" / "tool_error_result.txt").exists()


def test_exec_accepts_analysis_only_task_without_material_changes(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["title"] = "Investigate src/app.py and report findings only"
    task["description"] = "Read-only analysis only; report findings."
    task["acceptance_criteria"] = ["Findings documented."]
    task["estimated_files"] = ["src/app.py"]
    task["write_scope"] = ["src/app.py"]
    task["analysis_only"] = True
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    monkeypatch.setattr(cli_mod, "run_agent", lambda **_kwargs: 0)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    report_text = (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")
    assert "- Result: success" in report_text
    assert "- Result Kind: success_noop" in report_text
    assert "- No-Op Reason: analysis_only" in report_text


def test_exec_pr_accepts_analysis_only_task_without_material_changes(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_git_repo_with_commit(repo)
    base_branch = _git_run(
        ["git", "-C", os.fspath(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["title"] = "Investigate README.md and report findings only"
    task["description"] = "Read-only analysis only; report findings."
    task["acceptance_criteria"] = ["Findings documented."]
    task["estimated_files"] = ["README.md"]
    task["write_scope"] = ["README.md"]
    task["analysis_only"] = True
    task["branch"] = "feat/t01-analysis-only"
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    monkeypatch.setattr(cli_mod, "run_agent", lambda **_kwargs: 0)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    current_branch = _git_run(
        ["git", "-C", os.fspath(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert current_branch == base_branch
    deleted_branch = _git_run(
        ["git", "-C", os.fspath(repo), "branch", "--list", "feat/t01-analysis-only"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert deleted_branch == ""
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    report_text = (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")
    assert "- Result: success" in report_text
    assert "- Result Kind: success_noop" in report_text
    assert "- No-Op Reason: analysis_only" in report_text
    assert "- Commit: (none)" in report_text
    assert "- Merge Commit: (none)" in report_text
    assert "- Merge Result: no merge required (analysis-only no-op; branch deleted)" in report_text


def test_exec_strict_scope_surfaces_task_local_reporting_inspection_failures(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    monkeypatch.setattr(cli_mod, "run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        cli_mod,
        "_build_task_local_workspace_reporting_diff",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("src/in_scope.py",),
            patch_text="PATCH\n",
            inspection_error="scope inspection failed: boom",
        ),
    )

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    report_text = (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")
    assert "scope inspection failed: boom" in report_text


def test_exec_scope_warn_override_disables_allow_write_globs(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    captured: dict[str, list[str] | None] = {"allow": None}

    def fake_run_agent(*, root: Path, allow_write_globs, **_kwargs):  # type: ignore[no-untyped-def]
        captured["allow"] = allow_write_globs
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--scope",
            "warn",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["allow"] is None


def test_exec_scope_off_override_disables_allow_write_globs(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    captured: dict[str, list[str] | None] = {"allow": None}

    def fake_run_agent(*, root: Path, allow_write_globs, **_kwargs):  # type: ignore[no-untyped-def]
        captured["allow"] = allow_write_globs
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--scope",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["allow"] is None


def test_exec_without_task_mcp_scope_blocks_live_tools_and_resources(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_mcp_stdio_config(
        tmp_path,
        {
            "capabilities": {"tools": {}, "resources": {}},
            "tools_pages": [
                [
                    {
                        "name": "echo",
                        "description": "Echo tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
            "resources_pages": [
                [
                    {
                        "uri": "https://example.com/spec.json",
                        "name": "spec",
                        "description": "Spec resource",
                        "mimeType": "application/json",
                    }
                ]
            ],
        },
        server_overrides={"resources_mode": "listed_read_only"},
    )
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run_agent(*, root: Path, mcp_manager, **_kwargs):  # type: ignore[no-untyped-def]
        captured["tool_aliases"] = [binding.tool_alias for binding in mcp_manager.tool_bindings]
        captured["summary"] = mcp_manager.execution_context_summary()
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["tool_aliases"] == []
    summary = captured["summary"]
    assert isinstance(summary, dict)
    assert summary["task_scope"] == {
        "present": False,
        "allow_resources": False,
        "allowed_tools": [],
    }
    assert summary["active_server_ids"] == []
    assert summary["servers"] == []
    context_text = (
        load_current_run_paths(repo).execution_context_dir / f"{task_id}_context.md"
    ).read_text(encoding="utf-8")
    assert "## MCP Execution Context" in context_text
    assert "Task MCP Scope: disabled (default deny-by-default)" in context_text
    assert (
        "write_scope governs local file mutation. mcp_scope governs remote MCP actions."
        in context_text
    )
    assert "- Available MCP Servers: (none)" in context_text


def test_exec_without_task_mcp_scope_does_not_touch_broken_mcp_servers(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_json(
        tmp_path / "cfg" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "transport": "stdio",
                    "command": os.fspath(tmp_path / "missing-mcp-server"),
                    "resources_mode": "listed_read_only",
                }
            }
        },
    )
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    captured: dict[str, object] = {"agent_started": False}

    def fake_run_agent(*, root: Path, mcp_manager, **_kwargs):  # type: ignore[no-untyped-def]
        captured["agent_started"] = True
        captured["tool_aliases"] = [binding.tool_alias for binding in mcp_manager.tool_bindings]
        captured["summary"] = mcp_manager.execution_context_summary()
        captured["startup"] = mcp_manager.startup_metadata()
        captured["snapshot"] = mcp_manager.catalog_snapshot_metadata()
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["agent_started"] is True
    assert captured["tool_aliases"] == []
    assert captured["summary"] == {
        "active_server_ids": [],
        "servers": [],
        "task_scope": {
            "present": False,
            "allow_resources": False,
            "allowed_tools": [],
        },
    }
    startup = captured["startup"]
    assert isinstance(startup, dict)
    assert startup["config_present"] is False
    assert startup["active_server_ids"] == []
    assert startup["resolved_server_ids"] == []
    assert startup["forge_task_live_bootstrap_skipped"] is True
    snapshot = captured["snapshot"]
    assert isinstance(snapshot, dict)
    assert snapshot["active_server_ids"] == []
    assert snapshot["server_catalogs"] == []
    assert snapshot["forge_task_mcp_scope"]["forge_task_live_bootstrap_skipped"] is True
    context_text = (
        load_current_run_paths(repo).execution_context_dir / f"{task_id}_context.md"
    ).read_text(encoding="utf-8")
    assert "Task MCP Scope: disabled (default deny-by-default)" in context_text
    assert "- Available MCP Servers: (none)" in context_text


def test_exec_without_task_mcp_scope_ignores_malformed_mcp_config(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_json(
        tmp_path / "cfg" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "transport": "stdio",
                    "command": os.fspath(tmp_path / "missing-mcp-server"),
                    "resources_mode": 123,
                }
            }
        },
    )
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    captured: dict[str, object] = {"agent_started": False}

    def fake_run_agent(*, root: Path, mcp_manager, **_kwargs):  # type: ignore[no-untyped-def]
        captured["agent_started"] = True
        captured["tool_aliases"] = [binding.tool_alias for binding in mcp_manager.tool_bindings]
        captured["startup"] = mcp_manager.startup_metadata()
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["agent_started"] is True
    assert captured["tool_aliases"] == []
    startup = captured["startup"]
    assert isinstance(startup, dict)
    assert startup["config_present"] is False
    assert startup["resolved_server_ids"] == []
    assert startup["forge_task_live_bootstrap_skipped"] is True


def test_exec_rejects_superseded_task_mcp_scope_before_live_mcp_bootstrap(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_json(
        tmp_path / "cfg" / "mcp.json",
        {
            "servers": {
                "alpha": {
                    "transport": "stdio",
                    "command": os.fspath(tmp_path / "missing-mcp-server"),
                    "resources_mode": "listed_read_only",
                }
            }
        },
    )
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Implement obsolete TOML settings",
        description="Update src/settings.py for the obsolete TOML settings path.",
        estimated_files=["src/settings.py"],
        write_scope=["src/settings.py"],
        status="superseded",
        mcp_scope={"allow_resources": True},
    )
    save_plan(paths, plan)
    captured: dict[str, bool] = {"agent_started": False}

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["agent_started"] = True
        raise AssertionError("run_agent should not be called for superseded tasks")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            str(task["id"]),
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert captured["agent_started"] is False
    assert "Task is non-executable obsolete work (superseded)" in result.output
    assert "missing-mcp-server" not in result.output


def test_exec_task_mcp_scope_allow_resources_exposes_only_generic_resource_tools(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_mcp_stdio_config(
        tmp_path,
        {
            "capabilities": {"tools": {}, "resources": {}},
            "tools_pages": [
                [
                    {
                        "name": "echo",
                        "description": "Echo tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
            "resources_pages": [
                [
                    {
                        "uri": "https://example.com/spec.json",
                        "name": "spec",
                        "description": "Spec resource",
                        "mimeType": "application/json",
                    }
                ]
            ],
            "resource_read_results": {
                "https://example.com/spec.json": {
                    "contents": [
                        {
                            "uri": "https://example.com/spec.json",
                            "mimeType": "application/json",
                            "text": '{"ok":true}',
                        }
                    ]
                }
            },
        },
        server_overrides={"resources_mode": "listed_read_only"},
    )
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    task["mcp_scope"] = {"allow_resources": True}
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run_agent(*, root: Path, mcp_manager, **_kwargs):  # type: ignore[no-untyped-def]
        bindings = {binding.tool_alias: binding for binding in mcp_manager.tool_bindings}
        captured["tool_aliases"] = sorted(bindings)
        captured["listed"] = bindings["mcp_resources_list"].run({"limit": 5})
        captured["read"] = bindings["mcp_resource_read"].run(
            {
                "server_id": "alpha",
                "uri": "https://example.com/spec.json",
            }
        )
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["tool_aliases"] == ["mcp_resource_read", "mcp_resources_list"]
    listed = captured["listed"]
    assert isinstance(listed, dict)
    assert listed["returned_count"] == 1
    assert listed["resources"][0]["server_id"] == "alpha"
    assert listed["resources"][0]["uri"] == "https://example.com/spec.json"
    read = captured["read"]
    assert isinstance(read, dict)
    assert read["server_id"] == "alpha"
    assert read["uri"] == "https://example.com/spec.json"
    assert read["mime_type"] == "application/json"
    expected_text = build_untrusted_mcp_text_block(
        source_type="resource_read",
        server_id="alpha",
        source_name="https://example.com/spec.json",
        text='{"ok":true}',
        mime_type="application/json",
    )
    assert read["text"] == expected_text
    assert read["contents"][0]["text"] == expected_text
    assert "source_type: resource_read" in read["text"]
    assert "server_id: alpha" in read["text"]
    assert "source_name: https://example.com/spec.json" in read["text"]
    assert '{"ok":true}' in read["text"]


def test_exec_task_mcp_scope_allowed_tools_exposes_only_exact_live_tools(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_mcp_stdio_config(
        tmp_path,
        {
            "capabilities": {"tools": {}, "resources": {}},
            "tools_pages": [
                [
                    {
                        "name": "echo",
                        "description": "Echo tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    },
                    {
                        "name": "comment_pull_request",
                        "description": "Comment tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    },
                ]
            ],
            "tool_call_results": {
                "echo": {
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                },
                "comment_pull_request": {
                    "isError": False,
                    "content": [{"type": "text", "text": "commented"}],
                },
            },
            "resources_pages": [
                [
                    {
                        "uri": "https://example.com/spec.json",
                        "name": "spec",
                    }
                ]
            ],
        },
        server_overrides={"resources_mode": "listed_read_only"},
    )
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    task["mcp_scope"] = {
        "allowed_tools": [
            {"server_id": "alpha", "tool_name": "echo"},
        ]
    }
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_run_agent(*, root: Path, mcp_manager, **_kwargs):  # type: ignore[no-untyped-def]
        captured["mcp_manager"] = mcp_manager
        bindings = list(mcp_manager.tool_bindings)
        captured["tool_aliases"] = [binding.tool_alias for binding in bindings]
        captured["tool_names"] = [binding.tool_name for binding in bindings]
        captured["tool_result"] = bindings[0].run({})
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["tool_names"] == ["echo"]
    tool_aliases = captured["tool_aliases"]
    assert isinstance(tool_aliases, list)
    assert len(tool_aliases) == 1
    assert tool_aliases[0].startswith("mcp__alpha__echo")
    tool_result = captured["tool_result"]
    assert isinstance(tool_result, dict)
    assert tool_result["server_id"] == "alpha"
    assert tool_result["tool_name"] == "echo"
    assert tool_result["content_summary"] == "text(2 chars)"
    assert captured["mcp_manager"].closed is True
    assert not (load_current_run_paths(repo).run_dir / "active_execution.lock.json").exists()
    assert live_stdio_transport_diagnostics() == []


def test_exec_task_mcp_scope_unknown_tool_fails_clearly(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _write_mcp_stdio_config(
        tmp_path,
        {
            "tools_pages": [
                [
                    {
                        "name": "echo",
                        "description": "Echo tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ]
        },
    )
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    task["mcp_scope"] = {
        "allowed_tools": [
            {"server_id": "alpha", "tool_name": "missing_tool"},
        ]
    }
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def fake_run_agent(*, mcp_manager, **_kwargs):  # type: ignore[no-untyped-def]
        _ = mcp_manager.tool_bindings
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    report_text = (load_current_run_paths(repo).execution_reports_dir / f"{task_id}.md").read_text(
        encoding="utf-8"
    )
    assert "Forge task mcp_scope references MCP tools" in report_text
    assert "missing_tool" in report_text
    assert not (load_current_run_paths(repo).run_dir / "active_execution.lock.json").exists()
    assert live_stdio_transport_diagnostics() == []


def test_exec_task_mcp_scope_allowed_tool_then_resources_has_clean_mcp_lifecycle(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first_captured: dict[str, object] = {}

    def first_run_agent(*, root: Path, mcp_manager, **_kwargs):  # type: ignore[no-untyped-def]
        first_captured["mcp_manager"] = mcp_manager
        bindings = list(mcp_manager.tool_bindings)
        first_captured["tool_names"] = [binding.tool_name for binding in bindings]
        first_captured["tool_result"] = bindings[0].run({})
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    first_result, first_repo, _first_task_id = _run_mcp_scoped_exec_case(
        case_tmp_path=tmp_path / "first",
        monkeypatch=monkeypatch,
        fixture_payload={
            "capabilities": {"tools": {}, "resources": {}},
            "tools_pages": [
                [
                    {
                        "name": "echo",
                        "description": "Echo tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
            "tool_call_results": {
                "echo": {
                    "isError": False,
                    "content": [{"type": "text", "text": "ok"}],
                }
            },
            "resources_pages": [
                [
                    {
                        "uri": "https://example.com/spec.json",
                        "name": "spec",
                    }
                ]
            ],
        },
        task_mcp_scope={"allowed_tools": [{"server_id": "alpha", "tool_name": "echo"}]},
        fake_run_agent=first_run_agent,
        server_overrides={"resources_mode": "listed_read_only"},
    )

    assert first_result.exit_code == 0
    assert first_captured["tool_names"] == ["echo"]
    assert first_captured["mcp_manager"].closed is True
    assert not (load_current_run_paths(first_repo).run_dir / "active_execution.lock.json").exists()
    assert live_stdio_transport_diagnostics() == []

    second_captured: dict[str, object] = {}

    def second_run_agent(*, root: Path, mcp_manager, **_kwargs):  # type: ignore[no-untyped-def]
        bindings = {binding.tool_alias: binding for binding in mcp_manager.tool_bindings}
        second_captured["tool_aliases"] = sorted(bindings)
        second_captured["listed"] = bindings["mcp_resources_list"].run({"limit": 5})
        second_captured["read"] = bindings["mcp_resource_read"].run(
            {
                "server_id": "alpha",
                "uri": "https://example.com/spec.json",
            }
        )
        _write_material_change(root, rel_path="src/in_scope.py")
        return 0

    second_result, second_repo, _second_task_id = _run_mcp_scoped_exec_case(
        case_tmp_path=tmp_path / "second",
        monkeypatch=monkeypatch,
        fixture_payload={
            "capabilities": {"tools": {}, "resources": {}},
            "tools_pages": [
                [
                    {
                        "name": "echo",
                        "description": "Echo tool",
                        "inputSchema": {"type": "object", "properties": {}, "required": []},
                    }
                ]
            ],
            "resources_pages": [
                [
                    {
                        "uri": "https://example.com/spec.json",
                        "name": "spec",
                        "description": "Spec resource",
                        "mimeType": "application/json",
                    }
                ]
            ],
            "resource_read_results": {
                "https://example.com/spec.json": {
                    "contents": [
                        {
                            "uri": "https://example.com/spec.json",
                            "mimeType": "application/json",
                            "text": '{"ok":true}',
                        }
                    ]
                }
            },
        },
        task_mcp_scope={"allow_resources": True},
        fake_run_agent=second_run_agent,
        server_overrides={"resources_mode": "listed_read_only"},
    )

    assert second_result.exit_code == 0
    assert second_captured["tool_aliases"] == ["mcp_resource_read", "mcp_resources_list"]
    listed = second_captured["listed"]
    assert isinstance(listed, dict)
    assert listed["returned_count"] == 1
    read = second_captured["read"]
    assert isinstance(read, dict)
    assert '{"ok":true}' in read["text"]
    assert not (load_current_run_paths(second_repo).run_dir / "active_execution.lock.json").exists()
    assert live_stdio_transport_diagnostics() == []


def test_exec_strict_scope_fails_on_out_of_scope_changes_in_plain_dir(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "plain"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "README.md").write_text("# out of scope\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    report_text = (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")
    patch_text = (run_dir / "execution" / "patches" / f"{task_id}.diff").read_text(encoding="utf-8")
    assert "Task blocked due to strict scope isolation." in report_text
    assert "`README.md`" in report_text
    assert "added: README.md" in patch_text


def test_exec_uses_workspace_snapshot_diff_for_git_repo_without_head(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _git_run(["git", "-C", os.fspath(repo), "init", "-q"], check=True)
    _git_run(
        ["git", "-C", os.fspath(repo), "config", "user.name", "Test User"],
        check=True,
    )
    _git_run(
        ["git", "-C", os.fspath(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")

    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        target = root / "src" / "in_scope.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('ok')\n", encoding="utf-8")
        _git_run(["git", "-C", os.fspath(root), "add", "-A"], check=True)
        _git_run(
            [
                "git",
                "-C",
                os.fspath(root),
                "-c",
                "commit.gpgsign=false",
                "commit",
                "--no-gpg-sign",
                "--no-verify",
                "-m",
                "task",
                "-q",
            ],
            check=True,
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    report_text = (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")
    assert "`src/in_scope.py`" in report_text
    assert "`README.md`" not in report_text


def test_exec_pr_success_autofills_branch_and_writes_pr_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    assert task["branch"] == ""

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "new_file.py").write_text("print('ok')\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda cmd: "/usr/bin/git")

    patch_text = (
        "From deadbeef Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH] task update\n\n"
        " new file mode 100644\n"
        "---\n"
    )

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard", "-z"]:
            return subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=b"new_file.py\x00",
                stderr=b"",
            )
        if args == ["status", "--porcelain"]:
            return _cp(stdout="")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="new_file.py\n")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout=patch_text)
        if args == ["checkout", "main"]:
            return _cp(stdout="")
        if len(args) == 5 and args[0] == "merge" and args[1] == "--no-ff":
            return _cp(stdout="Merge made\n")
        if args == ["branch", "-d", "feat/t01-implement-feature-slice"]:
            return _cp(stdout="Deleted branch\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    final_plan = _load_json(plan_path)
    final_task = final_plan["tasks"][0]
    assert final_task["status"] == "done"
    assert final_task["branch"] == "feat/t01-implement-feature-slice"

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    patch_path = run_dir / "execution" / "patches" / f"{task_id}.diff"
    report_path = run_dir / "execution" / "reports" / f"{task_id}.md"

    assert patch_path.exists()
    assert report_path.exists()
    patch_text = patch_path.read_text(encoding="utf-8")
    assert any(f"new file mode {mode}" in patch_text for mode in ("100644", "100755"))

    report = report_path.read_text(encoding="utf-8")
    assert "Base Branch: main" in report
    assert "Task Branch: feat/t01-implement-feature-slice" in report
    assert "Commit: deadbeef" in report
    assert "Merge Commit: deadbeef" in report
    assert "Merge Result: merged into main" in report


def test_exec_pr_filters_untracked_packaging_metadata_by_exact_file_path(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr(cli_mod, "ensure_git_available", lambda: None)
    monkeypatch.setattr(cli_mod, "ensure_git_repo", lambda _root: None)
    monkeypatch.setattr(cli_mod, "ensure_clean_for_pr", lambda _root: None)
    monkeypatch.setattr(cli_mod, "current_branch", lambda _root: "main")
    monkeypatch.setattr(
        cli_mod,
        "checkout_branch",
        lambda _root, _branch, *, base_branch: None,
    )
    monkeypatch.setattr(
        cli_mod,
        "list_untracked_packaging_metadata_paths",
        lambda _root: ["src/calcbox.egg-info/SOURCES.txt"],
    )
    monkeypatch.setattr(cli_mod, "stage_all", lambda _root: None)
    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        cli_mod,
        "unstage_staged_prefixes",
        lambda _root, prefixes: captured.setdefault("prefixes", list(prefixes)) or [],
    )
    monkeypatch.setattr(
        cli_mod,
        "ensure_not_staged_prefixes",
        lambda _root, prefixes: captured.setdefault("ensure_prefixes", list(prefixes)),
    )
    monkeypatch.setattr(
        cli_mod,
        "unstage_staged_paths",
        lambda _root, paths: captured.setdefault("paths", list(paths)) or [],
    )
    monkeypatch.setattr(
        cli_mod,
        "ensure_not_staged_paths",
        lambda _root, paths: captured.setdefault("ensure_paths", list(paths)),
    )
    monkeypatch.setattr(cli_mod, "commit_all", lambda _root, *, message: "deadbeef")
    monkeypatch.setattr(cli_mod, "format_patch_stdout", lambda *_a, **_k: "From deadbeef\n")
    monkeypatch.setattr(cli_mod, "changed_files_between", lambda *_a, **_k: ["src/file.py"])
    monkeypatch.setattr(cli_mod, "merge_no_ff", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr(
        cli_mod,
        "run_task_verification",
        lambda **_k: VerifyRunResult(
            commands=[],
            statuses=[],
            all_passed=True,
            summary="verification disabled",
        ),
    )

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert captured["prefixes"] == [".alysis", ".alysis_images", "alysis-feedback"]
    assert captured["ensure_prefixes"] == [".alysis", ".alysis_images", "alysis-feedback"]
    assert captured["paths"] == ["src/calcbox.egg-info/SOURCES.txt"]
    assert captured["ensure_paths"] == ["src/calcbox.egg-info/SOURCES.txt"]


def test_exec_pr_dependency_enforcement_blocks_before_git(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    plan_path, plan = _prepare_dependency_plan(
        repo,
        task_specs=[
            ("Task one", "src/one.py"),
            ("Task two", "src/two.py"),
        ],
    )
    first_id = plan["tasks"][0]["id"]
    second_id = plan["tasks"][1]["id"]
    plan["tasks"][1]["dependencies"] = [first_id]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        raise AssertionError("run_agent should not be called when dependencies are blocked")

    def fake_subprocess_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("git should not run when dependencies are blocked")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda cmd: "/usr/bin/git")
    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    exec_result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            second_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
        ],
        env=_env(tmp_path),
    )
    assert exec_result.exit_code == 2
    assert "Dependencies are not done" in exec_result.output


def test_exec_pr_rejects_nonzero_agent_exit_with_untracked_material_change(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(*, root: Path, **_kwargs) -> int:  # type: ignore[no-untyped-def]
        (root / "new_file.py").write_text("print('ok')\n", encoding="utf-8")
        return 1

    calls: list[list[str]] = []
    patch_text = (
        "From deadbeef Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH] task update\n\n"
        " new file mode 100644\n"
        "---\n"
    )

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        calls.append(args)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["rev-parse", "--verify", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard", "-z"]:
            return subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=b"new_file.py\x00",
                stderr=b"",
            )
        if args == ["status", "--porcelain"]:
            return _cp(stdout="?? new_file.py\n")
        if args == ["status", "--porcelain=v1", "-z"]:
            return subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=b"?? new_file.py\x00",
                stderr=b"",
            )
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["diff", "HEAD", "--binary", "-M"]:
            return _cp(stdout="")
        if args == ["diff", "--no-index", "--", os.devnull, "new_file.py"]:
            return _cp(
                returncode=1,
                stdout=(
                    "diff --git a/new_file.py b/new_file.py\n"
                    "new file mode 100644\n"
                    "--- /dev/null\n"
                    "+++ b/new_file.py\n"
                    "@@ -0,0 +1 @@\n"
                    "+print('ok')\n"
                ),
            )
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout=patch_text)
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="new_file.py\n")
        if args == ["checkout", "main"]:
            return _cp(stdout="")
        if len(args) == 5 and args[0] == "merge" and args[1] == "--no-ff":
            return _cp(stdout="Merge made\n")
        if args == ["branch", "-d", "feat/t01-implement-feature-slice"]:
            return _cp(stdout="Deleted branch\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda cmd: "/usr/bin/git")
    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    patch_path = run_dir / "execution" / "patches" / f"{task_id}.diff"
    report_path = run_dir / "execution" / "reports" / f"{task_id}.md"
    patch_text = patch_path.read_text(encoding="utf-8")
    assert "new_file.py" in patch_text
    report_text = report_path.read_text(encoding="utf-8")
    assert "`new_file.py`" in report_text
    assert "agent exited non-zero (1)" in report_text
    assert "refusing to accept partial task result" in report_text
    assert "PR flow salvaged a non-zero agent exit" not in report_text
    assert not any(cmd and cmd[0] == "commit" for cmd in calls)
    assert not any(cmd and cmd[0] == "merge" for cmd in calls)
    assert not any(cmd[:2] == ["branch", "-d"] for cmd in calls)


def test_exec_pr_nonzero_without_material_changes_skips_pr_flow(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", lambda **_kwargs: 1)
    monkeypatch.setattr(
        cli_mod,
        "stage_all",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("stage_all should not run without material changes")
        ),
    )

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"


def test_exec_pr_nonzero_runtime_artifact_mutation_skips_pr_flow(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / ".alysis").mkdir(parents=True, exist_ok=True)
        (root / ".alysis" / "tamper.txt").write_text("bad\n", encoding="utf-8")
        (root / "new_file.py").write_text("print('ok')\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        cli_mod,
        "stage_all",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("stage_all should not run after runtime artifact mutation")
        ),
    )

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    paths = load_current_run_paths(repo)
    report_text = (paths.execution_reports_dir / f"{task_id}.md").read_text(encoding="utf-8")
    assert "modified files under .alysis/" in report_text


def test_exec_external_custom_tool_artifacts_do_not_fail_runtime_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(
        *,
        root: Path,
        session_log_dir_override: Path,
        session_id_override: str,
        **_kwargs,
    ) -> int:
        tool_log_dir = session_log_dir_override / session_id_override / "tool_logs"
        tool_log_dir.mkdir(parents=True, exist_ok=True)
        (tool_log_dir / "stream-tool.stdout.log").write_text(
            "CUSTOM_STDOUT_SENTINEL\n",
            encoding="utf-8",
        )
        _write_material_change(root)
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    paths = load_current_run_paths(repo)
    report_text = (paths.execution_reports_dir / f"{task_id}.md").read_text(encoding="utf-8")
    assert "Task execution completed successfully." in report_text
    assert "modified files under .alysis/" not in report_text


def test_exec_authorized_custom_tool_dir_side_effect_does_not_fail_runtime_snapshot(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(
        *,
        root: Path,
        session_log_dir_override: Path,
        session_id_override: str,
        **_kwargs,
    ) -> int:
        side_effect = root / ".alysis" / "tools" / "tool_state.txt"
        side_effect.parent.mkdir(parents=True, exist_ok=True)
        side_effect.write_text("tool-dir-ok\n", encoding="utf-8")
        _write_tool_result_session_event(
            sessions_dir=session_log_dir_override,
            session_id=session_id_override,
            tool_name="tool_dir_writer",
            result={
                "success": True,
                "result": {"state_file": os.fspath(side_effect)},
                "side_effects": {
                    "workspace_writes": [
                        {"path": ".alysis/tools/tool_state.txt", "scope": "tool_dir"}
                    ]
                },
            },
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    paths = load_current_run_paths(repo)
    report_text = (paths.execution_reports_dir / f"{task_id}.md").read_text(encoding="utf-8")
    assert "Task execution completed successfully." in report_text
    assert "modified files under .alysis/" not in report_text


def test_exec_workspace_scoped_custom_tool_alysis_side_effect_still_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(
        *,
        root: Path,
        session_log_dir_override: Path,
        session_id_override: str,
        **_kwargs,
    ) -> int:
        side_effect = root / ".alysis" / "workspace_scope_state.txt"
        side_effect.parent.mkdir(parents=True, exist_ok=True)
        side_effect.write_text("not-authorized-for-forge\n", encoding="utf-8")
        _write_tool_result_session_event(
            sessions_dir=session_log_dir_override,
            session_id=session_id_override,
            tool_name="workspace_writer",
            result={
                "success": True,
                "result": {"state_file": os.fspath(side_effect)},
                "side_effects": {
                    "workspace_writes": [
                        {
                            "path": ".alysis/workspace_scope_state.txt",
                            "scope": "workspace",
                        }
                    ]
                },
            },
        )
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    paths = load_current_run_paths(repo)
    report_text = (paths.execution_reports_dir / f"{task_id}.md").read_text(encoding="utf-8")
    assert "modified files under .alysis/" in report_text


def test_exec_pr_nonzero_strict_scope_violation_skips_pr_flow(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "README.md").write_text("# out of scope\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(
        cli_mod,
        "stage_all",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("stage_all should not run after strict scope blocking")
        ),
    )

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    paths = load_current_run_paths(repo)
    report_text = (paths.execution_reports_dir / f"{task_id}.md").read_text(encoding="utf-8")
    assert "Task blocked due to strict scope isolation." in report_text


def test_exec_pr_merge_conflict_writes_conflict_artifacts_and_sets_status(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr(cli_mod, "ensure_git_available", lambda: None)
    monkeypatch.setattr(cli_mod, "ensure_git_repo", lambda _root: None)
    monkeypatch.setattr(cli_mod, "ensure_clean_for_pr", lambda _root: None)
    monkeypatch.setattr(cli_mod, "current_branch", lambda _root: "main")
    monkeypatch.setattr(
        cli_mod,
        "checkout_branch",
        lambda _root, _branch, *, base_branch: None,
    )
    monkeypatch.setattr(cli_mod, "stage_all", lambda _root: None)
    monkeypatch.setattr(cli_mod, "unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(cli_mod, "ensure_not_staged_prefixes", lambda _root, _prefixes: None)
    monkeypatch.setattr(cli_mod, "commit_all", lambda _root, *, message: "deadbeef")
    monkeypatch.setattr(
        cli_mod,
        "format_patch_stdout",
        lambda _root, *, base_branch: "From deadbeef\n new file mode 100644\n",
    )
    monkeypatch.setattr(
        cli_mod,
        "changed_files_between",
        lambda _root, *, revspec: ["src/x.py"],
    )
    monkeypatch.setattr(
        cli_mod,
        "merge_no_ff",
        lambda *_a, **_k: (_ for _ in ()).throw(GitOpsError("merge conflict")),
    )
    monkeypatch.setattr(cli_mod, "list_unmerged_files", lambda _root: ["src/x.py"])
    monkeypatch.setattr(
        cli_mod,
        "capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )
    monkeypatch.setattr(
        cli_mod,
        "review_merge_conflict",
        lambda **_kwargs: ConflictReviewOutcome(
            review_json={
                "task_id": task_id,
                "confidence": "medium",
                "summary": "merge conflict",
                "root_cause": "same hunk changed",
                "recommended_strategy": "manual_merge",
                "per_file": [],
                "next_steps": ["resolve manually"],
            },
            review_markdown="# Merge conflict review\n",
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "try_abort_merge",
        lambda _root, *, base_branch: (
            True,
            f"$ git -C {_root} merge --abort\nbase={base_branch}\n",
        ),
    )

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1
    assert "Conflict Review:" in result.output

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "merge_conflict"

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    conflict_dir = run_dir / "execution" / "conflicts" / task_id
    assert (conflict_dir / "conflict_context.json").exists()
    assert (conflict_dir / "conflict_review.json").exists()
    assert (conflict_dir / "conflict_review.md").exists()
    assert (conflict_dir / "merge_cleanup.log").exists()


def test_exec_pr_merge_conflict_does_not_auto_resolve_without_opt_in(
    tmp_path: Path, monkeypatch
) -> None:
    """A conflict stops the sequential path instead of starting a resolver agent.

    Auto-resolution runs a second agent inside its own worktree; that is swarm
    machinery, so it only happens when the caller asks for it.
    """
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr(cli_mod, "ensure_git_available", lambda: None)
    monkeypatch.setattr(cli_mod, "ensure_git_repo", lambda _root: None)
    monkeypatch.setattr(cli_mod, "ensure_clean_for_pr", lambda _root: None)
    monkeypatch.setattr(cli_mod, "current_branch", lambda _root: "main")
    monkeypatch.setattr(
        cli_mod,
        "checkout_branch",
        lambda _root, _branch, *, base_branch: None,
    )
    monkeypatch.setattr(cli_mod, "stage_all", lambda _root: None)
    monkeypatch.setattr(cli_mod, "unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(cli_mod, "ensure_not_staged_prefixes", lambda _root, _prefixes: None)
    monkeypatch.setattr(cli_mod, "commit_all", lambda _root, *, message: "deadbeef")
    monkeypatch.setattr(
        cli_mod,
        "format_patch_stdout",
        lambda _root, *, base_branch: "From deadbeef\n new file mode 100644\n",
    )
    monkeypatch.setattr(
        cli_mod,
        "changed_files_between",
        lambda _root, *, revspec: ["src/x.py"],
    )
    monkeypatch.setattr(
        cli_mod,
        "merge_no_ff",
        lambda *_a, **_k: (_ for _ in ()).throw(GitOpsError("merge conflict")),
    )
    monkeypatch.setattr(cli_mod, "list_unmerged_files", lambda _root: ["src/x.py"])
    monkeypatch.setattr(
        cli_mod,
        "capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )
    monkeypatch.setattr(
        cli_mod,
        "review_merge_conflict",
        lambda **_kwargs: ConflictReviewOutcome(
            review_json={
                "task_id": task_id,
                "confidence": "medium",
                "summary": "merge conflict",
                "root_cause": "same hunk changed",
                "recommended_strategy": "manual_merge",
                "per_file": [],
                "next_steps": ["resolve manually"],
            },
            review_markdown="# Merge conflict review\n",
            skipped_reason=None,
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "try_abort_merge",
        lambda _root, *, base_branch: (True, "aborted\n"),
    )
    monkeypatch.setattr(
        cli_mod,
        "load_conflict_auto_resolve_settings",
        lambda **_kwargs: ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="off",
            max_attempts=1,
        ),
    )

    def _explode(**_kwargs):
        raise AssertionError("conflict auto-resolve must not run without --auto-resolve-conflicts")

    monkeypatch.setattr(cli_mod, "attempt_auto_resolve_conflict", _explode)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    task_record = final_plan["tasks"][0]
    assert task_record["status"] == "merge_conflict"
    # The settings allow an attempt; the flag is what withholds it, so the
    # attempt counter must stay untouched.
    assert int(task_record.get("conflict_attempts", 0)) == 0

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    report_text = (
        repo / pointer["run_path"] / "execution" / "reports" / f"{task_id}.md"
    ).read_text(encoding="utf-8")
    assert "started no resolver agent" in report_text
    assert "merge --no-ff" in report_text
    assert "--auto-resolve-conflicts" in report_text


def test_exec_pr_merge_conflict_auto_resolve_success_sets_done(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr(cli_mod, "ensure_git_available", lambda: None)
    monkeypatch.setattr(cli_mod, "ensure_git_repo", lambda _root: None)
    monkeypatch.setattr(cli_mod, "ensure_clean_for_pr", lambda _root: None)
    monkeypatch.setattr(cli_mod, "current_branch", lambda _root: "main")
    monkeypatch.setattr(
        cli_mod,
        "checkout_branch",
        lambda _root, _branch, *, base_branch: None,
    )
    monkeypatch.setattr(cli_mod, "stage_all", lambda _root: None)
    monkeypatch.setattr(cli_mod, "unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(cli_mod, "ensure_not_staged_prefixes", lambda _root, _prefixes: None)
    monkeypatch.setattr(cli_mod, "commit_all", lambda _root, *, message: "deadbeef")
    monkeypatch.setattr(
        cli_mod,
        "format_patch_stdout",
        lambda _root, *, base_branch: "From deadbeef\n new file mode 100644\n",
    )
    monkeypatch.setattr(
        cli_mod,
        "changed_files_between",
        lambda _root, *, revspec: ["src/x.py"],
    )
    monkeypatch.setattr(
        cli_mod,
        "merge_no_ff",
        lambda *_a, **_k: (_ for _ in ()).throw(GitOpsError("merge conflict")),
    )
    monkeypatch.setattr(cli_mod, "list_unmerged_files", lambda _root: ["src/x.py"])
    monkeypatch.setattr(
        cli_mod,
        "capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )
    monkeypatch.setattr(
        cli_mod,
        "review_merge_conflict",
        lambda **_kwargs: ConflictReviewOutcome(
            review_json=None,
            review_markdown="# Merge conflict review\n",
            skipped_reason="missing key",
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "try_abort_merge",
        lambda _root, *, base_branch: (
            True,
            f"$ git -C {_root} merge --abort\nbase={base_branch}\n",
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "load_conflict_auto_resolve_settings",
        lambda *, cfg: ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="strict",
            max_attempts=1,
        ),
    )

    def fake_auto_resolve(**kwargs) -> AutoResolveOutcome:  # type: ignore[no-untyped-def]
        paths = kwargs["paths"]
        conflict_dir = paths.execution_dir / "conflicts" / task_id
        conflict_dir.mkdir(parents=True, exist_ok=True)
        report_path = conflict_dir / "auto_resolve_report.md"
        report_path.write_text("# auto resolve\n", encoding="utf-8")
        patch_path = conflict_dir / "auto_resolve_patch.diff"
        patch_path.write_text("patch\n", encoding="utf-8")
        result_path = conflict_dir / "auto_resolve_result.json"
        result_path.write_text('{"success": true}\n', encoding="utf-8")
        return AutoResolveOutcome(
            success=True,
            task_id=task_id,
            conflict_branch=f"conflict/{task_id.lower()}",
            worktree_repo_path=paths.run_dir / "conflict_worktrees" / task_id / "repo",
            result_json_path=result_path,
            report_path=report_path,
            patch_path=patch_path,
            merge_commit_hash="automerge123",
            verify_summary="verification passed (1/1)",
            warnings=[],
            error=None,
        )

    monkeypatch.setattr(cli_mod, "attempt_auto_resolve_conflict", fake_auto_resolve)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
            "--auto-resolve-conflicts",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    assert int(final_plan["tasks"][0].get("conflict_attempts", 0)) == 1


def test_exec_pr_merge_conflict_auto_resolve_failure_keeps_merge_conflict_status(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr(cli_mod, "ensure_git_available", lambda: None)
    monkeypatch.setattr(cli_mod, "ensure_git_repo", lambda _root: None)
    monkeypatch.setattr(cli_mod, "ensure_clean_for_pr", lambda _root: None)
    monkeypatch.setattr(cli_mod, "current_branch", lambda _root: "main")
    monkeypatch.setattr(
        cli_mod,
        "checkout_branch",
        lambda _root, _branch, *, base_branch: None,
    )
    monkeypatch.setattr(cli_mod, "stage_all", lambda _root: None)
    monkeypatch.setattr(cli_mod, "unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(cli_mod, "ensure_not_staged_prefixes", lambda _root, _prefixes: None)
    monkeypatch.setattr(cli_mod, "commit_all", lambda _root, *, message: "deadbeef")
    monkeypatch.setattr(
        cli_mod,
        "format_patch_stdout",
        lambda _root, *, base_branch: "From deadbeef\n new file mode 100644\n",
    )
    monkeypatch.setattr(
        cli_mod,
        "changed_files_between",
        lambda _root, *, revspec: ["src/x.py"],
    )
    monkeypatch.setattr(
        cli_mod,
        "merge_no_ff",
        lambda *_a, **_k: (_ for _ in ()).throw(GitOpsError("merge conflict")),
    )
    monkeypatch.setattr(cli_mod, "list_unmerged_files", lambda _root: ["src/x.py"])
    monkeypatch.setattr(
        cli_mod,
        "capture_merge_conflict_context",
        lambda _root, *, base_branch, task_branch, merge_error: {
            "base_branch": base_branch,
            "task_branch": task_branch,
            "merge_error": merge_error,
            "git_status_porcelain": "UU src/x.py",
            "unmerged_files": ["src/x.py"],
            "files": [],
        },
    )
    monkeypatch.setattr(
        cli_mod,
        "review_merge_conflict",
        lambda **_kwargs: ConflictReviewOutcome(
            review_json=None,
            review_markdown="# Merge conflict review\n",
            skipped_reason="missing key",
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "try_abort_merge",
        lambda _root, *, base_branch: (
            True,
            f"$ git -C {_root} merge --abort\nbase={base_branch}\n",
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "load_conflict_auto_resolve_settings",
        lambda *, cfg: ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="strict",
            max_attempts=1,
        ),
    )

    def fake_auto_resolve(**kwargs) -> AutoResolveOutcome:  # type: ignore[no-untyped-def]
        paths = kwargs["paths"]
        conflict_dir = paths.execution_dir / "conflicts" / task_id
        conflict_dir.mkdir(parents=True, exist_ok=True)
        report_path = conflict_dir / "auto_resolve_report.md"
        report_path.write_text("# auto resolve\n", encoding="utf-8")
        patch_path = conflict_dir / "auto_resolve_patch.diff"
        patch_path.write_text("patch\n", encoding="utf-8")
        result_path = conflict_dir / "auto_resolve_result.json"
        result_path.write_text('{"success": false}\n', encoding="utf-8")
        return AutoResolveOutcome(
            success=False,
            task_id=task_id,
            conflict_branch=f"conflict/{task_id.lower()}",
            worktree_repo_path=paths.run_dir / "conflict_worktrees" / task_id / "repo",
            result_json_path=result_path,
            report_path=report_path,
            patch_path=patch_path,
            merge_commit_hash=None,
            verify_summary="verification failed (0/1)",
            warnings=[],
            error="strict conflict verify failed",
        )

    monkeypatch.setattr(cli_mod, "attempt_auto_resolve_conflict", fake_auto_resolve)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "off",
            "--auto-resolve-conflicts",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "merge_conflict"


def test_exec_pr_merge_success_branch_delete_failure_is_warning(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["status", "--porcelain"]:
            return _cp(stdout=" M src/file.py\n")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout="From deadbeef\n new file mode 100644\n")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="src/file.py\n")
        if args == ["checkout", "main"]:
            return _cp(stdout="")
        if len(args) == 5 and args[0] == "merge" and args[1] == "--no-ff":
            return _cp(stdout="Merge made\n")
        if args == ["branch", "-d", "feat/t01-implement-feature-slice"]:
            return _cp(returncode=1, stderr="cannot delete branch")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    report_path = run_dir / "execution" / "reports" / f"{task_id}.md"
    report = report_path.read_text(encoding="utf-8")
    assert "Result: success" in report
    assert "Branch cleanup warning" in report


def test_exec_pr_remote_sync_off_by_default_does_not_push(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr(
        cli_mod,
        "push_branch",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("push_branch should not run")
        ),
    )
    monkeypatch.setattr(
        cli_mod,
        "push_base",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("push_base should not run")),
    )
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["status", "--porcelain"]:
            return _cp(stdout="")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout="From deadbeef\n new file mode 100644\n")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="src/file.py\n")
        if args == ["checkout", "main"]:
            return _cp(stdout="")
        if len(args) == 5 and args[0] == "merge" and args[1] == "--no-ff":
            return _cp(stdout="Merge made\n")
        if args == ["branch", "-d", "feat/t01-implement-feature-slice"]:
            return _cp(stdout="Deleted branch\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0


def test_exec_pr_remote_warn_push_failure_does_not_block_merge(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr(cli_mod, "get_remote_url", lambda *_a, **_k: "git@github.com:org/repo.git")
    monkeypatch.setattr(cli_mod, "push_branch", lambda *_a, **_k: (False, "push failed"))
    monkeypatch.setattr(cli_mod, "push_base", lambda *_a, **_k: (False, "base push failed"))
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["status", "--porcelain"]:
            return _cp(stdout="")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout="From deadbeef\n new file mode 100644\n")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="src/file.py\n")
        if args == ["checkout", "main"]:
            return _cp(stdout="")
        if len(args) == 5 and args[0] == "merge" and args[1] == "--no-ff":
            return _cp(stdout="Merge made\n")
        if args == ["branch", "-d", "feat/t01-implement-feature-slice"]:
            return _cp(stdout="Deleted branch\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    env = _env(tmp_path)
    env["ALYSIS_REMOTE_SYNC"] = "warn"

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
        ],
        env=env,
    )
    assert result.exit_code == 0

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    remote_path = run_dir / "execution" / "remote" / f"{task_id}.json"
    assert remote_path.exists()
    remote_data = _load_json(remote_path)
    assert remote_data["pushed_branch"] is False
    assert remote_data["pushed_base"] is False


def test_exec_pr_remote_strict_push_failure_blocks_merge(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(cli_mod, "get_remote_url", lambda *_a, **_k: "git@github.com:org/repo.git")
    monkeypatch.setattr(cli_mod, "push_branch", lambda *_a, **_k: (False, "push failed"))
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")
    calls: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        calls.append(args)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["status", "--porcelain"]:
            return _cp(stdout="")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout="From deadbeef\n new file mode 100644\n")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="src/file.py\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    env = _env(tmp_path)
    env["ALYSIS_REMOTE_SYNC"] = "strict"

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
        ],
        env=env,
    )
    assert result.exit_code == 1
    assert not any(cmd and cmd[0] == "merge" for cmd in calls)

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"


def test_exec_pr_remote_strict_existing_pr_does_not_block_merge(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr(cli_mod, "get_remote_url", lambda *_a, **_k: "git@github.com:org/repo.git")
    monkeypatch.setattr(cli_mod, "push_branch", lambda *_a, **_k: (True, "pushed"))
    monkeypatch.setattr(cli_mod, "push_base", lambda *_a, **_k: (True, "pushed base"))
    monkeypatch.setattr(
        cli_mod,
        "ensure_pr_or_mr",
        lambda *_a, **_k: (
            True,
            "https://github.com/org/repo/pull/99",
            "99",
            "existing",
        ),
    )
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["status", "--porcelain"]:
            return _cp(stdout="")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout="From deadbeef\n new file mode 100644\n")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="src/file.py\n")
        if args == ["checkout", "main"]:
            return _cp(stdout="")
        if len(args) == 5 and args[0] == "merge" and args[1] == "--no-ff":
            return _cp(stdout="Merge made\n")
        if args == ["branch", "-d", "feat/t01-implement-feature-slice"]:
            return _cp(stdout="Deleted branch\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)
    env = _env(tmp_path)
    env["ALYSIS_REMOTE_SYNC"] = "strict"
    env["ALYSIS_REMOTE_CREATE_PR"] = "1"

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
        ],
        env=env,
    )
    assert result.exit_code == 0

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    assert final_plan["tasks"][0]["remote_pr_url"] == "https://github.com/org/repo/pull/99"
    assert final_plan["tasks"][0]["remote_provider"] == "github"


def test_exec_pr_review_rejection_blocks_merge_and_sets_changes_requested(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_review_task(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        json_path = run_dir / "execution" / "reviews" / f"{task_id}.json"
        md_path = run_dir / "execution" / "reviews" / f"{task_id}.md"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("{}", encoding="utf-8")
        md_path.write_text("# review\n", encoding="utf-8")
        return ReviewOutcome(
            task_id=task_id,
            approved=False,
            confidence="medium",
            summary="changes requested",
            blocking_issues_count=1,
            non_blocking_issues_count=0,
            json_path=json_path,
            markdown_path=md_path,
        )

    monkeypatch.setattr(cli_mod, "review_task", fake_review_task)

    def fake_run_task_verification(*, artifact_path: Path, **_kwargs):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verification passed\n", encoding="utf-8")
        return VerifyRunResult(
            commands=["go test ./..."],
            command_results=[
                VerifyCommandResult(
                    command="go test ./...",
                    effective_command="go test ./...",
                    exit_code=0,
                    output="ok\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(cli_mod, "run_task_verification", fake_run_task_verification)

    calls: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        calls.append(args)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["status", "--porcelain"]:
            return _cp(stdout="")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout="From deadbeef\n")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="src/file.py\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--review",
            "--verify-cmd",
            "go test ./...",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "changes_requested"
    assert not any(cmd and cmd[0] == "merge" for cmd in calls)


def test_exec_pr_review_receives_current_verification_payload_before_report_exists(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_run_task_verification(*, artifact_path: Path, **_kwargs):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verification passed\n", encoding="utf-8")
        return VerifyRunResult(
            commands=["go test ./..."],
            command_results=[
                VerifyCommandResult(
                    command="go test ./...",
                    effective_command="go test ./...",
                    exit_code=0,
                    output="ok\texample/pkg\t0.012s\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(cli_mod, "run_task_verification", fake_run_task_verification)

    def fake_review_task(*_args, **kwargs):  # type: ignore[no-untyped-def]
        report_path = run_dir / "execution" / "reports" / f"{task_id}.md"
        assert not report_path.exists()
        verification = kwargs.get("verification_payload_override")
        assert isinstance(verification, dict)
        assert verification["summary"] == "verification passed (1/1)"
        assert verification["all_passed"] is True
        command_results = verification["command_results"]
        assert isinstance(command_results, list)
        assert command_results[0]["command"] == "go test ./..."
        assert command_results[0]["exit_code"] == 0
        assert command_results[0]["ok"] is True

        json_path = run_dir / "execution" / "reviews" / f"{task_id}.json"
        md_path = run_dir / "execution" / "reviews" / f"{task_id}.md"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("{}", encoding="utf-8")
        md_path.write_text("# review\n", encoding="utf-8")
        return ReviewOutcome(
            task_id=task_id,
            approved=True,
            confidence="high",
            summary="approved",
            blocking_issues_count=0,
            non_blocking_issues_count=0,
            json_path=json_path,
            markdown_path=md_path,
        )

    monkeypatch.setattr(cli_mod, "review_task", fake_review_task)

    patch_text = (
        "From deadbeef Mon Sep 17 00:00:00 2001\n"
        "Subject: [PATCH] task update\n\n"
        " new file mode 100644\n"
        "---\n"
    )

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["status", "--porcelain"]:
            return _cp(stdout="")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="src/file.py\n")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout=patch_text)
        if args == ["checkout", "main"]:
            return _cp(stdout="")
        if len(args) == 5 and args[0] == "merge" and args[1] == "--no-ff":
            return _cp(stdout="Merge made\n")
        if args == ["branch", "-d", "feat/t01-implement-feature-slice"]:
            return _cp(stdout="Deleted branch\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--review",
            "--verify-cmd",
            "go test ./...",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    report_path = run_dir / "execution" / "reports" / f"{task_id}.md"
    report = report_path.read_text(encoding="utf-8")
    assert "## Verification Results" in report
    assert "go test ./..." in report


def test_exec_pr_verify_strict_blocks_merge_and_sets_verify_failed(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    verify_path = run_dir / "execution" / "verify" / f"{task_id}.txt"

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_verify(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        verify_path.parent.mkdir(parents=True, exist_ok=True)
        verify_path.write_text("verify failed\n", encoding="utf-8")
        return VerifyRunResult(
            commands=["pytest -q"],
            command_results=[VerifyCommandResult("pytest -q", 1, "failed")],
            artifact_path=verify_path,
        )

    monkeypatch.setattr(cli_mod, "run_task_verification", fake_verify)

    calls: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        calls.append(args)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["status", "--porcelain"]:
            return _cp(stdout="")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout="From deadbeef\n")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="src/file.py\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "strict",
            "--verify-cmd",
            "pytest -q",
        ],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "verify_failed"
    assert not any(cmd and cmd[0] == "merge" for cmd in calls)
    assert verify_path.exists()


def test_exec_pr_nonzero_salvage_strict_verify_failure_sets_verify_failed(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    verify_path = run_dir / "execution" / "verify" / f"{task_id}.txt"

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "new_file.py").write_text("print('ok')\n", encoding="utf-8")
        return 1

    def fake_verify(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        verify_path.parent.mkdir(parents=True, exist_ok=True)
        verify_path.write_text("verify failed\n", encoding="utf-8")
        return VerifyRunResult(
            commands=["pytest -q"],
            command_results=[VerifyCommandResult("pytest -q", 1, "failed")],
            artifact_path=verify_path,
        )

    calls: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        calls.append(args)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard", "-z"]:
            return subprocess.CompletedProcess(
                args=["git"],
                returncode=0,
                stdout=b"new_file.py\x00",
                stderr=b"",
            )
        if args == ["status", "--porcelain"]:
            return _cp(stdout="?? new_file.py\n")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout="From deadbeef\n")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="new_file.py\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli_mod, "run_task_verification", fake_verify)
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")
    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "strict",
            "--verify-cmd",
            "pytest -q",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "verify_failed"
    assert any(cmd and cmd[0] == "commit" for cmd in calls)
    assert not any(cmd and cmd[0] == "merge" for cmd in calls)
    report_text = (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")
    assert "PR flow attempted to salvage a non-zero agent exit" in report_text
    assert verify_path.exists()


def test_exec_pr_strict_scope_blocks_verification_time_out_of_scope_mutations(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task = plan["tasks"][0]
    task_id = task["id"]
    task["estimated_files"] = ["src/in_scope.py"]
    task["write_scope"] = ["src/in_scope.py"]
    plan_path.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    base_branch = subprocess.run(
        ["git", "-C", os.fspath(repo), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        target = root / "src" / "in_scope.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("print('ok')\n", encoding="utf-8")
        return 0

    def fake_verify(*, artifact_path: Path, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verification passed\n", encoding="utf-8")
        (root / "README.md").write_text("verify touched docs\n", encoding="utf-8")
        return VerifyRunResult(
            commands=["pytest -q"],
            command_results=[
                VerifyCommandResult(
                    command="pytest -q",
                    exit_code=0,
                    output="1 passed\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    monkeypatch.setattr(cli_mod, "run_task_verification", fake_verify)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "strict",
            "--verify-cmd",
            "pytest -q",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    patch_text = (run_dir / "execution" / "patches" / f"{task_id}.diff").read_text(encoding="utf-8")
    report_text = (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")
    main_head = subprocess.run(
        ["git", "-C", os.fspath(repo), "log", "--format=%s", "-1", base_branch],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert main_head == "init"
    assert "Post-verification workspace diff" in patch_text
    assert "README.md" in patch_text
    assert "Task blocked due to strict scope isolation." in report_text
    assert "Verification commands modified repository state after the task commit." in report_text


def test_exec_pr_verify_warn_failure_records_warning_and_merges(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    run_dir = repo / pointer["run_path"]
    verify_path = run_dir / "execution" / "verify" / f"{task_id}.txt"

    monkeypatch.setattr(cli_mod, "run_agent", _run_agent_with_material_change())
    monkeypatch.setattr("alysis_code.git_ops.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_verify(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        verify_path.parent.mkdir(parents=True, exist_ok=True)
        verify_path.write_text("verify warn\n", encoding="utf-8")
        return VerifyRunResult(
            commands=["pytest -q"],
            command_results=[VerifyCommandResult("pytest -q", 1, "failed")],
            artifact_path=verify_path,
        )

    monkeypatch.setattr(cli_mod, "run_task_verification", fake_verify)

    calls: list[list[str]] = []

    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        args = _git_args(cmd)
        calls.append(args)
        workspace_cp = _workspace_context_git_cp(repo, args)
        if workspace_cp is not None:
            return workspace_cp
        if args[:2] == ["bash", "-lc"]:
            return _cp(stdout="verify ok\n")
        if args == ["rev-parse", "--git-dir"]:
            return _cp(stdout=".git\n")
        if args == ["rev-parse", "--git-path", "info/exclude"]:
            return _cp(stdout=str(repo / ".git" / "info" / "exclude") + "\n")
        if args == ["diff", "--cached", "--name-only"]:
            return _cp(stdout="")
        if args == ["diff", "--name-only"]:
            return _cp(stdout="")
        if args == ["ls-files", "--others", "--exclude-standard"]:
            return _cp(stdout="")
        if args == ["status", "--porcelain"]:
            return _cp(stdout="")
        if args in (
            ["symbolic-ref", "--short", "HEAD"],
            ["rev-parse", "--abbrev-ref", "HEAD"],
        ):
            return _cp(stdout="main\n")
        if args == [
            "show-ref",
            "--verify",
            "--quiet",
            "refs/heads/feat/t01-implement-feature-slice",
        ]:
            return _cp(returncode=1)
        if args == ["checkout", "-b", "feat/t01-implement-feature-slice", "main"]:
            return _cp(stdout="")
        if args == ["diff"]:
            return _cp(stdout="")
        if args == ["add", "-A"]:
            return _cp(stdout="")
        if len(args) == 3 and args[0] == "commit" and args[1] == "-m":
            return _cp(stdout="[feat] task commit\n")
        if args == ["rev-parse", "HEAD"]:
            return _cp(stdout="deadbeef\n")
        if args == ["format-patch", "main..HEAD", "--stdout"]:
            return _cp(stdout="From deadbeef\n")
        if args == ["diff", "--name-only", "main..HEAD"]:
            return _cp(stdout="src/file.py\n")
        if args == ["checkout", "main"]:
            return _cp(stdout="")
        if len(args) == 5 and args[0] == "merge" and args[1] == "--no-ff":
            return _cp(stdout="Merge made\n")
        if args == ["branch", "-d", "feat/t01-implement-feature-slice"]:
            return _cp(stdout="Deleted branch\n")
        raise AssertionError(f"unexpected git args: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--pr",
            "--verify",
            "warn",
            "--verify-cmd",
            "pytest -q",
        ],
        env=_env(tmp_path),
    )
    # --verify warn is advisory: a failing verification is recorded as a warning
    # but does not block the merge (only --verify strict blocks).
    assert result.exit_code == 0
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    assert any(cmd and cmd[0] == "merge" for cmd in calls)
    assert verify_path.exists()
    report_text = (run_dir / "execution" / "reports" / f"{task_id}.md").read_text(encoding="utf-8")
    assert "Verification warning: verification failed (0/1); failed: pytest -q" in report_text


def test_exec_writes_knowledge_and_injects_relevant_knowledge_context(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _prepare_run_with_tasks(runner, repo, tmp_path)
    paths = load_current_run_paths(repo)
    plan = load_plan(paths)
    first_task = plan["tasks"][0]
    first_task["estimated_files"] = ["src/parser.py"]
    save_plan(paths, plan)

    def fake_first_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "parser.py").write_text("print('first')\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_first_run_agent)

    first_result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            str(first_task["id"]),
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )
    assert first_result.exit_code == 0

    plan = load_plan(paths)
    second_task = add_task(
        plan,
        title="Follow up parser retry",
        description="Use prior parser context to finish the follow-up change.",
        estimated_files=["src/parser.py"],
    )
    save_plan(paths, plan)
    captured: dict[str, str] = {}

    def fake_second_run_agent(*, root: Path, **kwargs) -> int:
        captured["instruction"] = str(kwargs["instruction"])
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "parser.py").write_text("print('second')\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_second_run_agent)

    second_result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            str(second_task["id"]),
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert second_result.exit_code == 0
    assert "## Relevant Knowledge" in captured["instruction"]
    assert "Selected Knowledge Files" in captured["instruction"]
    manifest_path = (
        paths.knowledge_selected_dir / str(second_task["id"]) / "execution" / "manifest.json"
    )
    assert manifest_path.exists()
    attempt_entries = list(
        (paths.knowledge_task_attempts_dir / str(second_task["id"])).glob("*.md")
    )
    assert attempt_entries


def test_exec_writes_structured_knowledge_capture_artifacts_and_promotes_on_success(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    _plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(*, root: Path, **kwargs) -> int:
        surface = kwargs["surface"]
        surface.on_assistant_message_done(_structured_capture_text(valid=True))
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "parser.py").write_text("print('ok')\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    paths = load_current_run_paths(repo)
    capture_dirs = list((paths.execution_knowledge_capture_dir / task_id).glob("*"))
    assert capture_dirs
    validation_path = capture_dirs[0] / "validation.json"
    assert validation_path.exists()
    validation_payload = _load_json(validation_path)
    assert validation_payload["valid"] is True
    assert validation_payload["promotable_fact_count"] == 1
    assert validation_payload["promotable_decision_count"] == 1
    promotion_payload = _load_json(capture_dirs[0] / "promotion.json")
    assert promotion_payload["promotion_attempted"] is True
    assert promotion_payload["promotion_succeeded"] is True
    assert promotion_payload["fact_entry_ids"]
    assert promotion_payload["decision_entry_ids"]
    assert list((paths.knowledge_facts_dir / task_id).glob("*.md"))
    assert list((paths.knowledge_decisions_dir / task_id).glob("*.md"))


def test_exec_invalid_structured_capture_is_non_fatal(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(*, root: Path, **kwargs) -> int:
        surface = kwargs["surface"]
        surface.on_assistant_message_done(_structured_capture_text(valid=False))
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "parser.py").write_text("print('ok')\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "done"
    paths = load_current_run_paths(repo)
    capture_dirs = list((paths.execution_knowledge_capture_dir / task_id).glob("*"))
    assert capture_dirs
    validation_payload = _load_json(capture_dirs[0] / "validation.json")
    assert validation_payload["valid"] is False
    promotion_payload = _load_json(capture_dirs[0] / "promotion.json")
    assert promotion_payload["promotion_succeeded"] is False
    assert promotion_payload["fact_entry_ids"] == []
    assert promotion_payload["decision_entry_ids"] == []
    assert not list((paths.knowledge_facts_dir / task_id).glob("*.md"))
    assert not list((paths.knowledge_decisions_dir / task_id).glob("*.md"))


def test_exec_failure_writes_capture_artifacts_without_canonical_promotion(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def fake_run_agent(*, root: Path, **kwargs) -> int:
        surface = kwargs["surface"]
        surface.on_assistant_message_done(_structured_capture_text(valid=True))
        (root / "src").mkdir(parents=True, exist_ok=True)
        (root / "src" / "parser.py").write_text("print('nope')\n", encoding="utf-8")
        return 1

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "exec",
            task_id,
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1
    final_plan = _load_json(plan_path)
    assert final_plan["tasks"][0]["status"] == "failed"
    paths = load_current_run_paths(repo)
    capture_dirs = list((paths.execution_knowledge_capture_dir / task_id).glob("*"))
    assert capture_dirs
    validation_payload = _load_json(capture_dirs[0] / "validation.json")
    assert validation_payload["valid"] is True
    promotion_payload = _load_json(capture_dirs[0] / "promotion.json")
    assert promotion_payload["promotion_attempted"] is False
    assert promotion_payload["promotion_succeeded"] is False
    assert (
        promotion_payload["promotion_skipped_reason"] == "task execution outcome was not accepted"
    )
    assert not list((paths.knowledge_facts_dir / task_id).glob("*.md"))
    assert not list((paths.knowledge_decisions_dir / task_id).glob("*.md"))


def test_exec_context_budget_rejection_marks_task_failed_and_releases_lock(tmp_path, monkeypatch):
    import pytest

    from alysis_code.execution_context import ExecutionContextBudgetError

    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    plan_path, plan = _prepare_run_with_tasks(runner, repo, tmp_path)
    task_id = plan["tasks"][0]["id"]

    def reject(**kwargs):
        raise ExecutionContextBudgetError(available_tokens=100, required_tokens=900)

    monkeypatch.setattr(cli_mod, "_build_forge_exec_instruction_bundle", reject)
    monkeypatch.setattr(
        cli_mod, "run_agent", lambda **_: pytest.fail("No dispatch after budget rejection")
    )
    result = runner.invoke(
        alysis_app,
        ["forge", "exec", task_id, "--path", os.fspath(repo), "--model", "test-model", "--no-log"],
        env=_env(tmp_path),
    )
    assert result.exit_code == 1
    assert _load_json(plan_path)["tasks"][0]["status"] == "failed"
    run_dir = plan_path.parent.parent
    assert not (run_dir / "active_execution.lock.json").exists()
    report = run_dir / "execution" / "reports" / f"{task_id}.md"
    assert "Increase the context allowance" in report.read_text()
    context = (run_dir / "execution" / "context" / f"{task_id}_context.md").read_text()
    assert "setup failed before the agent started" in context
    assert "No shortened execution instruction was produced" in context
