from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
from rich.console import Console

from alysis_code.compaction.tool_output_offload import ToolOutputOffloader
from alysis_code.config import AppConfig
from alysis_code.failure_category import FailureCategory
from alysis_code.forge import (
    add_task,
    attach_asset,
    create_plan_run,
    load_plan,
    save_plan,
)
from alysis_code.git_ops import GitOpsError
from alysis_code.knowledge_base import load_knowledge_entry, write_task_attempt_entry
from alysis_code.knowledge_capture import RecordingSurface
from alysis_code.llm.openai_compat import LLMError
from alysis_code.session_artifacts import SessionArtifactLayout
from alysis_code.surface.types import ToolEndEvent, ToolOutputEvent, ToolStartEvent
from alysis_code.swarm_trace import (
    SwarmTraceEvent,
    SwarmWorkerTraceSurface,
    format_swarm_trace_message,
)
from alysis_code.swarm_worker import (
    _baseline_improved_failure_comparison,
    resolve_worker_verify_contract,
    run_task_worker,
)
from alysis_code.verify_gate import (
    ResolvedVerifyCommands,
    VerifyCommandResult,
    VerifyRunResult,
)


def _console() -> Console:
    return Console(file=io.StringIO())


def _init_git_repo(repo: Path) -> str:
    subprocess.run(["git", "-C", os.fspath(repo), "init", "-q"], check=True)
    subprocess.run(["git", "-C", os.fspath(repo), "checkout", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    subprocess.run(["git", "-C", os.fspath(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "init", "-q"],
        check=True,
    )
    return "main"


def _structured_capture_text(*, valid: bool = True) -> str:
    if not valid:
        return "Worker summary.\n\n```knowledge_capture_json\nnot-json\n```"
    return "\n".join(
        [
            "Worker summary.",
            "",
            "```knowledge_capture_json",
            json.dumps(
                {
                    "schema_version": 1,
                    "facts": [
                        {
                            "title": "Parser worker touched bounded retry logic",
                            "summary": "Worker observed bounded retry handling in src/parser.py.",
                            "paths": ["src/parser.py"],
                            "tags": ["parser", "retry"],
                        }
                    ],
                    "decisions": [
                        {
                            "decision_key": "parser-worker-retry-backoff",
                            "title": "Keep worker parser retry backoff",
                            "summary": "Worker chose the bounded parser retry behavior.",
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


def _build_verify_run_result(
    *,
    artifact_path: Path,
    commands: list[str],
    exit_codes: list[int] | None = None,
    real_executions: list[bool | None] | None = None,
    outputs: list[str] | None = None,
    failure_category: FailureCategory | str | None = None,
) -> VerifyRunResult:
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text("verification artifact\n", encoding="utf-8")

    effective_exit_codes = exit_codes or [0] * len(commands)
    effective_real_executions = real_executions or [True] * len(commands)
    effective_outputs = outputs or ["ok\n"] * len(commands)
    command_results = [
        VerifyCommandResult(
            command=command,
            effective_command=command,
            exit_code=effective_exit_codes[idx],
            output=effective_outputs[idx],
            real_execution=effective_real_executions[idx],
        )
        for idx, command in enumerate(commands)
    ]
    return VerifyRunResult(
        commands=list(commands),
        command_results=command_results,
        artifact_path=artifact_path,
        failure_category=failure_category,
    )


def test_worker_verify_contract_keeps_explicit_cli_java_verify_command(
    tmp_path: Path,
) -> None:
    task = {
        "title": "Fix Java ConfigLoader",
        "description": "Patch ConfigLoader and do not substitute pytest.",
        "acceptance_criteria": ["The explicit javac/java verification command passes."],
        "estimated_files": ["src/main/java/example/ConfigLoader.java"],
        "write_scope": ["src/main/java/example/ConfigLoader.java"],
    }

    command = "javac src/main/java/example/ConfigLoader.java && java example.ConfigLoader"
    contract = resolve_worker_verify_contract(
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q"]),
        verify_mode="warn",
        verify_commands=[command],
        verify_command_selection=None,
        task=task,
        root=tmp_path,
    )

    assert contract.commands == (command,)
    assert contract.selection is not None
    assert contract.selection.source == "cli.verify_cmd"
    assert contract.cfg.verify_commands == [command]


def _prepare_zero_diff_worker_case(tmp_path: Path) -> tuple[Path, object, dict[str, object], Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Zero diff task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-zero-diff",
    )
    save_plan(paths, plan)
    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    return repo, paths, task, worktree_repo


def _configure_zero_diff_worker(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(changed_files=(), patch_text=""),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: [],
    )


class _ListTraceSink:
    def __init__(self) -> None:
        self.events: list[SwarmTraceEvent] = []

    def emit(self, event: SwarmTraceEvent) -> None:
        self.events.append(event)

    def close(self) -> None:
        return None


def test_run_task_worker_mirrors_plan_assets_into_worktree(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Use attached brief",
        estimated_files=["src/impl.py"],
        branch="feat/t01-use-brief",
    )
    save_plan(paths, plan)

    source = repo / "brief.txt"
    source.write_text("asset body\n", encoding="utf-8")
    _, metadata = attach_asset(repo, source)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        session_log_dir = kwargs["session_log_dir_override"]
        session_id = kwargs["session_id_override"]
        (session_log_dir / f"{session_id}.jsonl").write_text(
            '{"type":"session_start"}\n',
            encoding="utf-8",
        )
        offloader = ToolOutputOffloader(
            artifact_layout=SessionArtifactLayout(
                filesystem_root=session_log_dir / str(session_id)
            ),
            workspace_root=worktree_repo,
            threshold_chars=20,
            preview_chars=20,
            workspace_artifacts_enabled=False,
        )
        offload_result = offloader.maybe_offload(
            tool_name="fs_read",
            tool_call_id="tc",
            step=1,
            result={"content": "asset body " * 20},
            content_json=json.dumps({"content": "asset body " * 20}, ensure_ascii=True),
        )
        stub = json.loads(offload_result.content_for_message)
        assert stub["artifact_locator"] == "session_artifacts/tool_outputs/step1_fs_read_tc.json"
        assert "artifact_path" not in stub
        assert stub["artifact_readable_via_fs"] is False
        assert "fs_read_path" not in stub
        assert "session_artifact_read with that locator" in stub["full_output"]
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/impl.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/impl.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/impl.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=False,
        console=_console(),
        scope_mode="warn",
    )
    assert result.success is True

    mirrored_asset = worktree_repo / str(metadata["stored_path"])
    assert mirrored_asset.exists()
    text_copy = metadata.get("text_copy_path")
    assert text_copy is not None
    assert (worktree_repo / str(text_copy)).exists()
    context_path = paths.execution_dir / "context" / f"{task['id']}_context.md"
    budget_path = paths.execution_dir / "budgets" / f"{task['id']}.json"
    log_copy_path = paths.execution_dir / "logs" / f"{task['id']}.jsonl"
    session_artifact_path = (
        paths.execution_dir
        / "sessions"
        / f"{task['id']}"
        / "tool_outputs"
        / "step1_fs_read_tc.json"
    )
    assert context_path.exists()
    assert budget_path.exists()
    assert log_copy_path.exists()
    assert session_artifact_path.exists()
    assert "# Task Context Pack" in context_path.read_text(encoding="utf-8")
    budget_payload = json.loads(budget_path.read_text(encoding="utf-8"))
    assert budget_payload["model"] == "test-model"
    assert budget_payload["pinned_prefix_token_estimate"] > 0
    assert budget_payload["tool_schema_token_estimate"] > 0
    assert budget_payload["requested_execution_response_reserve_tokens"] > 0
    assert budget_payload["effective_execution_response_reserve_tokens"] > 0
    assert budget_payload["requested_execution_headroom_reserve_tokens"] > 0
    assert budget_payload["effective_execution_headroom_reserve_tokens"] > 0
    assert budget_payload["minimum_instruction_budget_tokens"] > 0
    assert budget_payload["truncation_strategy"].startswith("execution_priority")
    assert budget_payload["subagents_enabled"] is False
    assert budget_payload["image_count"] == 0
    assert budget_payload["image_budget_reserve_tokens"] == 0
    assert (
        budget_payload["final_instruction_token_estimate"]
        <= budget_payload["final_instruction_budget"]
    )


def test_run_task_worker_moves_known_root_scratch_files_to_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Update implementation",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-scratch",
    )
    save_plan(paths, plan)
    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    _init_git_repo(worktree_repo)

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        (worktree_repo / "src").mkdir(parents=True, exist_ok=True)
        (worktree_repo / "src" / "in_scope.py").write_text("print('ok')\n", encoding="utf-8")
        (worktree_repo / "_pytest_out.txt").write_text("debug output\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("src/in_scope.py", "_pytest_out.txt"),
            patch_text="added: src/in_scope.py\nadded: _pytest_out.txt\n",
        ),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    scratch_artifact = paths.execution_dir / "scratch" / str(task["id"]) / "_pytest_out.txt"
    assert result.success is True
    assert "_pytest_out.txt" not in result.changed_files
    assert not (worktree_repo / "_pytest_out.txt").exists()
    assert scratch_artifact.read_text(encoding="utf-8") == "debug output\n"
    assert result.scope_diagnostics is not None
    assert result.scope_diagnostics[0]["classification"] == "scratch_diagnostic_artifact"


def test_run_task_worker_no_log_reports_retained_session_artifacts_truthfully(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Retain offload artifacts without JSONL logs",
        estimated_files=["src/impl.py"],
        branch="feat/t01-retain-offload",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        session_log_dir = kwargs["session_log_dir_override"]
        session_id = kwargs["session_id_override"]
        tool_outputs_dir = session_log_dir / str(session_id) / "tool_outputs"
        tool_outputs_dir.mkdir(parents=True, exist_ok=True)
        (tool_outputs_dir / "step001_shell_run_tc.json").write_text(
            '{"tool_name":"shell_run","result":{"stdout":"big"}}\n',
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/impl.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/impl.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/impl.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="warn",
    )

    assert result.success is True
    pointer_path = paths.root / result.log_pointer_path
    report_path = paths.root / result.report_path
    session_artifact_path = (
        paths.execution_dir
        / "sessions"
        / f"{task['id']}"
        / "tool_outputs"
        / "step001_shell_run_tc.json"
    )
    assert session_artifact_path.exists()
    pointer_data = json.loads(pointer_path.read_text(encoding="utf-8"))
    assert pointer_data["logging_enabled"] is False
    assert pointer_data["log_retained"] is False
    assert pointer_data["copied_log_path"] is None
    assert pointer_data["session_artifacts_retained"] is True
    assert (
        pointer_data["session_artifact_dir"]
        .replace("\\", "/")
        .endswith(f"/execution/sessions/{task['id']}")
    )
    report_text = report_path.read_text(encoding="utf-8")
    normalized_report_text = report_text.replace("\\", "/")
    assert "- Session Logging: disabled (--no-log)" in report_text
    assert "- Execution Log: (not retained)" in report_text
    assert (
        f"- Session Artifacts: `.alysis/runs/{paths.run_id}/execution/sessions/{task['id']}`"
        in normalized_report_text
    )


def test_run_task_worker_materializes_and_mirrors_relevant_knowledge(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    prior_task = add_task(
        plan,
        title="Implement parser retry",
        estimated_files=["src/parser.py"],
        branch="feat/t00-parser",
    )
    task = add_task(
        plan,
        title="Refine parser retry behavior",
        estimated_files=["src/parser.py"],
        branch="feat/t01-parser-follow-up",
    )
    save_plan(paths, plan)
    write_task_attempt_entry(
        paths=paths,
        task=prior_task,
        source="x",
        result="success",
        summary="Prior parser retry work completed.",
        changed_files=["src/parser.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
        created_at="1",
    )

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, str] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["instruction"] = str(kwargs["instruction"])
        (kwargs["session_log_dir_override"] / f"{kwargs['session_id_override']}.jsonl").write_text(
            '{"type":"session_start"}\n',
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/parser.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/parser.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/parser.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=False,
        console=_console(),
        scope_mode="warn",
    )

    assert result.success is True
    assert "## Relevant Knowledge" in captured["instruction"]
    assert "Selected Knowledge Files" in captured["instruction"]
    mirrored_manifest = (
        worktree_repo
        / ".alysis"
        / "runs"
        / paths.run_id
        / "knowledge"
        / "selected"
        / str(task["id"])
        / "execution"
        / "manifest.json"
    )
    assert mirrored_manifest.exists()
    knowledge_entries = list((paths.knowledge_task_attempts_dir / str(task["id"])).glob("*.md"))
    assert knowledge_entries
    attempt_entry = load_knowledge_entry(knowledge_entries[0])
    assert attempt_entry.result == "success"
    assert attempt_entry.status == "pending"


def test_run_task_worker_writes_structured_knowledge_capture_artifacts_without_promoting_entries(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Capture worker parser knowledge",
        estimated_files=["src/parser.py"],
        branch="feat/t01-parser",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        kwargs["surface"].on_assistant_message_done(_structured_capture_text(valid=True))
        (kwargs["session_log_dir_override"] / f"{kwargs['session_id_override']}.jsonl").write_text(
            '{"type":"session_start"}\n',
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/parser.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/parser.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/parser.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=False,
        console=_console(),
        scope_mode="warn",
    )

    assert result.success is True
    assert result.knowledge_capture_artifact_dir is not None
    capture_dirs = list((paths.execution_knowledge_capture_dir / str(task["id"])).glob("*"))
    assert capture_dirs
    validation_payload = json.loads(
        (capture_dirs[0] / "validation.json").read_text(encoding="utf-8")
    )
    assert validation_payload["valid"] is True
    assert validation_payload["promotable_fact_count"] == 1
    assert validation_payload["promotable_decision_count"] == 1
    promotion_payload = json.loads((capture_dirs[0] / "promotion.json").read_text(encoding="utf-8"))
    assert promotion_payload["promotion_attempted"] is False
    assert promotion_payload["promotion_succeeded"] is False
    assert promotion_payload["promotion_skipped_reason"] is None
    assert list((paths.knowledge_facts_dir / str(task["id"])).glob("*.md")) == []
    assert list((paths.knowledge_decisions_dir / str(task["id"])).glob("*.md")) == []


def test_run_task_worker_invalid_structured_capture_is_non_fatal(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Capture invalid worker parser knowledge",
        estimated_files=["src/parser.py"],
        branch="feat/t01-parser-invalid",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        kwargs["surface"].on_assistant_message_done(_structured_capture_text(valid=False))
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/parser.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/parser.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/parser.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="warn",
    )

    assert result.success is True
    capture_dirs = list((paths.execution_knowledge_capture_dir / str(task["id"])).glob("*"))
    assert capture_dirs
    validation_payload = json.loads(
        (capture_dirs[0] / "validation.json").read_text(encoding="utf-8")
    )
    assert validation_payload["valid"] is False
    promotion_payload = json.loads((capture_dirs[0] / "promotion.json").read_text(encoding="utf-8"))
    assert promotion_payload["promotion_attempted"] is False
    assert promotion_payload["promotion_succeeded"] is False
    assert promotion_payload["fact_entry_ids"] == []
    assert promotion_payload["decision_entry_ids"] == []
    assert list((paths.knowledge_facts_dir / str(task["id"])).glob("*.md")) == []
    assert list((paths.knowledge_decisions_dir / str(task["id"])).glob("*.md")) == []


def test_run_task_worker_failure_without_material_changes_writes_capture_artifacts_and_marks_promotion_skipped(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Capture failed worker parser knowledge",
        estimated_files=["src/parser.py"],
        branch="feat/t01-parser-fail",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        kwargs["surface"].on_assistant_message_done(_structured_capture_text(valid=True))
        return 1

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    called_stage = {"value": False}

    def fail_if_called(_root: Path) -> None:
        called_stage["value"] = True
        raise AssertionError("stage_all should not be called when no material changes exist")

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fail_if_called)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: [],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="warn",
    )

    assert called_stage["value"] is False
    assert result.success is False
    assert result.agent_exit_code == 1
    assert result.salvaged_nonzero_exit is False
    assert result.knowledge_capture_artifact_dir is not None
    capture_dir = paths.root / result.knowledge_capture_artifact_dir
    assert capture_dir.exists()
    validation_payload = json.loads((capture_dir / "validation.json").read_text(encoding="utf-8"))
    assert validation_payload["valid"] is True
    promotion_payload = json.loads((capture_dir / "promotion.json").read_text(encoding="utf-8"))
    assert promotion_payload["promotion_attempted"] is False
    assert promotion_payload["promotion_succeeded"] is False
    assert (
        promotion_payload["promotion_skipped_reason"] == "worker execution outcome was not accepted"
    )
    assert list((paths.knowledge_facts_dir / str(task["id"])).glob("*.md")) == []
    assert list((paths.knowledge_decisions_dir / str(task["id"])).glob("*.md")) == []


def test_run_task_worker_rejects_nonzero_exit_with_material_changes_and_verify_off(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Reject non-zero worker result",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-salvage",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    commit_calls = {"stage": 0, "commit": 0}

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 1)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )

    def fake_stage_all(_root: Path) -> None:
        commit_calls["stage"] += 1

    def fake_commit_all(*_args, **_kwargs) -> str:
        commit_calls["commit"] += 1
        return "deadbeef"

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fake_stage_all)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", fake_commit_all)
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run when verify_mode=off")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert commit_calls["stage"] == 0
    assert commit_calls["commit"] == 0
    assert result.success is False
    assert result.verify_failed is False
    assert result.commit_hash is None
    assert result.agent_exit_code == 1
    assert result.salvaged_nonzero_exit is False
    assert result.salvaged_agent_exception is False
    assert result.verify_summary is None
    assert "agent exited non-zero (1)" in (result.error or "")
    assert "refusing to accept partial worker result" in result.summary
    payload = result.to_json()
    assert payload["agent_exit_code"] == 1
    assert payload["salvaged_nonzero_exit"] is False
    assert payload["salvaged_agent_exception"] is False


def test_run_task_worker_rejects_agent_exception_with_material_changes_and_verify_off(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Reject agent exception",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-salvage-exc",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    commit_calls = {"stage": 0, "commit": 0}

    def fake_run_agent(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider timeout")

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )

    def fake_stage_all(_root: Path) -> None:
        commit_calls["stage"] += 1

    def fake_commit_all(*_args, **_kwargs) -> str:
        commit_calls["commit"] += 1
        return "deadbeef"

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fake_stage_all)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", fake_commit_all)
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert commit_calls["stage"] == 0
    assert commit_calls["commit"] == 0
    assert result.success is False
    assert result.commit_hash is None
    assert result.agent_exit_code == 1
    assert result.salvaged_nonzero_exit is False
    assert result.salvaged_agent_exception is False
    assert result.agent_exception_summary == "RuntimeError: provider timeout"
    assert result.error == "agent raised: RuntimeError: provider timeout"
    assert "Worker failed. Error: agent raised: RuntimeError: provider timeout" in result.summary
    payload = result.to_json()
    assert payload["salvaged_agent_exception"] is False
    assert payload["agent_exception_summary"] == "RuntimeError: provider timeout"
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Salvaged Non-Zero Exit: no" in report
    assert "- Salvaged Agent Exception: no" in report
    assert "- Agent Exception Summary: RuntimeError: provider timeout" in report


def test_run_task_worker_redacts_and_truncates_agent_exception_summary(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Sanitize agent exception",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-sanitize-exc",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    secret_token = "sk-" + ("abc12345" * 4)
    secret_bearer = "Bearer " + ("tok12345" * 5)
    long_tail = "tail-" + ("0123456789" * 40)

    def fake_run_agent(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError(f"provider timeout {secret_token} {secret_bearer} {long_tail}")

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.commit_all",
        lambda *_args, **_kwargs: "deadbeef",
    )
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is False
    assert result.salvaged_agent_exception is False
    assert result.agent_exception_summary is not None
    assert secret_token not in result.agent_exception_summary
    assert secret_bearer not in result.agent_exception_summary
    assert "[REDACTED]" in result.agent_exception_summary
    assert result.agent_exception_summary.endswith("...(truncated)")
    assert len(result.agent_exception_summary) <= 320
    assert result.error is not None
    assert secret_token not in result.error
    assert secret_bearer not in result.error
    payload = result.to_json()
    assert payload["success"] is False
    assert payload["salvaged_agent_exception"] is False
    assert payload["agent_exception_summary"] == result.agent_exception_summary
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert secret_token not in report
    assert secret_bearer not in report
    assert "[REDACTED]" in report


def test_run_task_worker_accepts_verified_zero_diff_as_noop_success(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **kwargs: _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=["pytest -q"],
            outputs=["1 passed\n"],
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is True
    assert result.commit_hash is None
    assert result.changed_files == []
    assert result.verify_failed is False
    assert result.verify_summary == "verification passed (1/1)"
    assert result.effective_result_kind == "success_noop"
    assert result.noop_success is True
    assert result.noop_reason == "already_satisfied"
    assert "already satisfied" in result.summary
    payload = result.to_json()
    assert payload["result_kind"] == "success_noop"
    assert payload["noop_success"] is True
    assert payload["noop_reason"] == "already_satisfied"
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Result Kind: success_noop" in report
    assert "- No-Op Reason: already_satisfied" in report
    assert "- Merge Result: no merge required (already-satisfied no-op)" in report


def test_run_task_worker_suppresses_generic_pytest_for_static_zero_diff(
    tmp_path: Path, monkeypatch
) -> None:
    # A generic ``pytest -q`` preset is only trusted once a real test surface is
    # confirmed. For a static-file task in a worktree with no discoverable Python
    # test surface, verification is suppressed to the first-class, non-failing
    # ``task_refinement.no_authoritative_commands`` selection: run_task_verification
    # is never invoked and the zero-diff no-op still succeeds cleanly.
    _repo, paths, _task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    plan = load_plan(paths)
    task = plan["tasks"][0]
    task["title"] = "Create static site files"
    task["estimated_files"] = ["index.html", "style.css"]
    task["write_scope"] = ["index.html", "style.css"]
    save_plan(paths, plan)
    _configure_zero_diff_worker(monkeypatch)
    verify_calls: list[dict[str, object]] = []

    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        verify_calls.append(dict(kwargs))
        raise AssertionError("verification must be suppressed for a scope-irrelevant task")

    monkeypatch.setattr("alysis_code.swarm_worker.run_task_verification", fake_verify)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q"]),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
    )

    assert result.success is True
    assert result.effective_result_kind == "success_noop"
    assert result.noop_reason == "already_satisfied"
    assert result.verify_summary == "verification skipped: no changes needed"
    assert result.verify_command_source == "task_refinement.no_authoritative_commands"
    assert result.verify_payload is None
    assert verify_calls == []
    assert "zero-diff" not in result.summary


def test_run_task_worker_runs_trusted_pytest_and_accepts_no_tests(
    tmp_path: Path, monkeypatch
) -> None:
    # When a real Python test surface is present the configured ``pytest -q`` is
    # trusted and executed; a pytest exit code 5 (no tests collected) is threaded
    # through as a benign, non-failing "nothing to verify" skip rather than a
    # verification failure.
    _repo, paths, _task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    (worktree_repo / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.0.0'\n", encoding="utf-8"
    )
    (worktree_repo / "tests").mkdir(parents=True, exist_ok=True)
    (worktree_repo / "tests" / "test_smoke.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )
    plan = load_plan(paths)
    task = plan["tasks"][0]
    save_plan(paths, plan)
    _configure_zero_diff_worker(monkeypatch)
    captured: dict[str, object] = {}

    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        captured["commands"] = list(kwargs["commands"])
        artifact_path = kwargs["artifact_path"]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("no tests collected\n", encoding="utf-8")
        return VerifyRunResult(
            commands=list(kwargs["commands"]),
            command_results=[
                VerifyCommandResult(
                    command="pytest -q",
                    effective_command="pytest -q",
                    exit_code=5,
                    output="collected 0 items\n\nno tests ran\n",
                    real_execution=False,
                    non_execution_reason="pytest_no_tests_collected",
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.swarm_worker.run_task_verification", fake_verify)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q"]),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
    )

    assert result.success is True
    assert result.effective_result_kind == "success_noop"
    assert result.noop_reason == "already_satisfied"
    assert captured["commands"] == ["pytest -q"]
    assert result.verify_command_source == "repo_scan.likely_test_commands"
    assert result.verify_summary == "verification skipped: nothing to verify (1/1)"
    assert result.verify_payload is not None
    assert result.verify_payload["all_passed"] is False
    assert result.verify_payload["status"] == "not_run"
    assert result.verify_payload["command_results"][0]["status"] == "skipped"
    assert "zero-diff" not in result.summary


def test_run_task_worker_accepts_diagnostic_zero_diff_without_verification(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    plan = load_plan(paths)
    task = plan["tasks"][0]
    task["title"] = "Investigate current slug handling of spaces"
    task["description"] = "Inspect the current behavior before implementation tasks run."
    save_plan(paths, plan)
    _configure_zero_diff_worker(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("diagnostic no-op should not require verification")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is True
    assert result.commit_hash is None
    assert result.changed_files == []
    assert result.effective_result_kind == "success_noop"
    assert result.noop_reason == "diagnostic_analysis_only"
    assert result.verify_summary == (
        "verification skipped: diagnostic analysis-only task made no changes"
    )
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- No-Op Reason: diagnostic_analysis_only" in report
    assert "- Merge Result: no merge required (analysis-only no-op)" in report


def test_run_task_worker_accepts_read_scope_zero_diff_without_baseline_verification(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    plan = load_plan(paths)
    task = plan["tasks"][0]
    task["title"] = "Read current config_loader.py and test_config_loader.py"
    task["description"] = "Inspect current files before downstream implementation tasks run."
    task["estimated_files"] = ["config_loader.py", "test_config_loader.py"]
    task["write_scope"] = ["config_loader.py", "test_config_loader.py"]
    save_plan(paths, plan)
    _configure_zero_diff_worker(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("read-only diagnostic no-op should not require verification")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is True
    assert result.effective_result_kind == "success_noop"
    assert result.noop_reason == "diagnostic_analysis_only"
    assert result.task_kind == "analysis_only"
    assert result.to_json()["task_kind"] == "analysis_only"


def test_run_task_worker_accepts_diagnostic_rust_build_metadata_side_effects(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    plan = load_plan(paths)
    task = plan["tasks"][0]
    task["title"] = "Locate duration parser implementation and tests"
    task["description"] = "Inspect the current Rust parser layout before implementation tasks run."
    task["estimated_files"] = []
    task["write_scope"] = []
    save_plan(paths, plan)
    (worktree_repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("Cargo.lock", "target/debug/demo"),
            patch_text="PATCH\n",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["Cargo.lock", "target/debug/demo"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("diagnostic side-effect no-op should not require verification")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["cargo test"],
    )

    assert result.success is True
    assert result.commit_hash is None
    assert result.changed_files == []
    assert result.effective_result_kind == "success_noop"
    assert result.noop_reason == "diagnostic_analysis_only"
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "generated Rust build metadata" in report


def test_run_task_worker_accepts_report_only_zero_diff_without_title_keyword(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    plan = load_plan(paths)
    task = plan["tasks"][0]
    task["title"] = "Compare build options"
    task["description"] = "Read-only analysis only; report findings."
    task["acceptance_criteria"] = ["Findings documented."]
    save_plan(paths, plan)
    _configure_zero_diff_worker(monkeypatch)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is True
    assert result.commit_hash is None
    assert result.effective_result_kind == "success_noop"
    assert result.noop_reason == "diagnostic_analysis_only"


def test_run_task_worker_rejects_locate_diagnostic_noop_after_nonzero_exit(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    plan = load_plan(paths)
    task = plan["tasks"][0]
    task["title"] = "Locate duration parser implementation and tests"
    task["description"] = "Inspect the repository before implementation tasks run."
    save_plan(paths, plan)

    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured["verification_enabled"] = kwargs["verification_enabled"]
        captured["authoritative_verification_commands"] = kwargs[
            "authoritative_verification_commands"
        ]
        return 1

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(changed_files=(), patch_text=""),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: [],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("diagnostic no-op should not require verification")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert captured["verification_enabled"] is False
    assert captured["authoritative_verification_commands"] is None
    assert result.success is False
    assert result.agent_exit_code == 1
    assert result.effective_result_kind == "failure"
    assert result.noop_reason is None
    assert "agent exited non-zero (1)" in (result.error or "")
    assert "refusing to accept partial worker result" in result.summary


def test_run_task_worker_accepts_conditional_zero_diff_without_verification(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    plan = load_plan(paths)
    task = plan["tasks"][0]
    task["title"] = "Update services/api README when present"
    task["description"] = "If services/api/README.md exists, document APP_REGION behavior."
    task["acceptance_criteria"] = ["Docs are updated when applicable."]
    save_plan(paths, plan)
    _configure_zero_diff_worker(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("conditional no-op should not require verification")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is True
    assert result.commit_hash is None
    assert result.changed_files == []
    assert result.effective_result_kind == "success_noop"
    assert result.noop_reason == "conditional_noop"
    assert result.verify_summary == "verification skipped: conditional task made no changes"
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- No-Op Reason: conditional_noop" in report
    assert "- Merge Result: no merge required (conditional no-op)" in report


def test_run_task_worker_refines_generic_pytest_fallback_to_node_test_for_js_noop_task(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Node test task",
        estimated_files=["test/app.test.js", "src/app.js"],
        branch="feat/t01-node-test",
    )
    task["write_scope"] = ["test/app.test.js", "src/app.js"]
    task["acceptance_criteria"] = ["Keep the JS test green."]
    save_plan(paths, plan)
    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    _configure_zero_diff_worker(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        return 0

    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        captured["verification_commands"] = list(kwargs["commands"])
        return _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=kwargs["commands"],
            outputs=["1 passed\n"],
        )

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.swarm_worker.run_task_verification", fake_verify)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model", verify_commands=["pytest -q", "ruff check ."]),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
    )

    assert result.success is True
    assert result.noop_success is True
    assert result.verify_command_source == "task_refinement.node_test"
    assert captured["authoritative_verification_commands"] == ["node --test"]
    assert captured["cfg_verify_commands"] == ["node --test"]
    assert captured["verification_commands"] == ["node --test"]
    assert result.verify_payload is not None
    assert result.verify_payload["commands"] == ["node --test"]
    payload = result.to_json()
    assert payload["verify_command_source"] == "task_refinement.node_test"
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Verify Command Source: task_refinement.node_test" in report
    assert "- `node --test`" in report
    assert "pytest -q" not in report


def test_run_task_worker_refines_generic_pytest_fallback_from_structured_node_text(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Node source task",
        estimated_files=["src/index.js"],
        branch="feat/t01-node-text",
    )
    task["write_scope"] = ["src/index.js"]
    task["acceptance_criteria"] = ["Use node --test for test verification."]
    save_plan(paths, plan)
    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    _configure_zero_diff_worker(monkeypatch)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        return 0

    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        captured["verification_commands"] = list(kwargs["commands"])
        return _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=kwargs["commands"],
            outputs=["1 passed\n"],
        )

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.swarm_worker.run_task_verification", fake_verify)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
        verify_command_selection=ResolvedVerifyCommands(
            commands=("pytest -q",),
            source="config.verify_commands_fallback",
        ),
    )

    assert result.success is True
    assert result.noop_success is True
    assert result.verify_command_source == "task_refinement.node_test"
    assert captured["authoritative_verification_commands"] == ["node --test"]
    assert captured["cfg_verify_commands"] == ["node --test"]
    assert captured["verification_commands"] == ["node --test"]
    assert result.verify_payload is not None
    assert result.verify_payload["commands"] == ["node --test"]
    payload = result.to_json()
    assert payload["verify_command_source"] == "task_refinement.node_test"
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Verify Command Source: task_refinement.node_test" in report
    assert "- `node --test`" in report
    assert "pytest -q" not in report


def test_run_task_worker_rejects_nonzero_exit_as_zero_diff_noop(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)
    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 1)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run after a non-zero agent exit")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is False
    assert result.commit_hash is None
    assert result.changed_files == []
    assert result.agent_exit_code == 1
    assert result.salvaged_nonzero_exit is False
    assert result.salvaged_agent_exception is False
    assert result.effective_result_kind == "failure"
    assert result.noop_success is False
    assert "agent exited non-zero (1)" in (result.error or "")
    assert "refusing to accept partial worker result" in result.summary
    payload = result.to_json()
    assert payload["result_kind"] == "failure"
    assert payload["salvaged_nonzero_exit"] is False
    assert payload["salvaged_agent_exception"] is False
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Salvaged Non-Zero Exit: no" in report
    assert "- Salvaged Agent Exception: no" in report


def test_run_task_worker_suppresses_wrong_pytest_fallback_for_js_bootstrap_commit_task(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Bootstrap package",
        estimated_files=["package.json", "src/index.js"],
        branch="feat/t01-bootstrap",
    )
    task["write_scope"] = ["package.json", "src/index.js"]
    task["acceptance_criteria"] = ["Bootstrap the package files."]
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["package.json", "src/index.js"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(changed_files=(), patch_text=""),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["package.json", "src/index.js"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["package.json", "src/index.js"],
    )

    def fail_verify(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("authoritative verification should be skipped when no commands exist")

    monkeypatch.setattr("alysis_code.swarm_worker.run_task_verification", fail_verify)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
        verify_command_selection=ResolvedVerifyCommands(
            commands=("pytest -q",),
            source="config.verify_commands_fallback",
        ),
    )

    assert result.success is True
    assert result.commit_hash == "deadbeef"
    assert result.verify_summary == "verification skipped: no authoritative commands available"
    assert result.verify_command_source == "task_refinement.no_authoritative_commands"
    assert captured["authoritative_verification_commands"] is None
    assert captured["cfg_verify_commands"] == []
    payload = result.to_json()
    assert payload["verify_command_source"] == "task_refinement.no_authoritative_commands"
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Verify Command Source: task_refinement.no_authoritative_commands" in report
    assert "No authoritative verification commands were available for this task yet." in report
    assert "pytest -q" not in report


def test_worker_reports_no_authoritative_commands_for_docs_only_task_under_default_fallback(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Update docs",
        estimated_files=["README.md", "docs/usage.md"],
        branch="feat/t01-docs",
    )
    task["write_scope"] = ["README.md", "docs/usage.md"]
    task["acceptance_criteria"] = ["Refresh operator documentation."]
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["README.md", "docs/usage.md"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(changed_files=(), patch_text=""),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["README.md", "docs/usage.md"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["README.md", "docs/usage.md"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("docs-only generic preset should not run verification")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
    )

    assert result.success is True
    assert result.commit_hash == "deadbeef"
    assert result.verify_summary == "verification skipped: no authoritative commands available"
    assert result.verify_command_source == "task_refinement.no_authoritative_commands"
    assert captured["authoritative_verification_commands"] is None
    assert captured["cfg_verify_commands"] == []
    payload = result.to_json()
    assert payload["verify_command_source"] == "task_refinement.no_authoritative_commands"
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Verify Command Source: task_refinement.no_authoritative_commands" in report
    assert "No authoritative verification commands were available for this task yet." in report
    assert "pytest -q" not in report


def test_run_task_worker_refreshes_verify_commands_after_creating_test_surface(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Implement calc command",
        estimated_files=["calc.py", "test_calc.py"],
        branch="feat/t01-calc",
    )
    task["write_scope"] = ["calc.py", "test_calc.py"]
    task["acceptance_criteria"] = ["Add calc behavior and pytest coverage."]
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["initial_authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["initial_cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        (worktree_repo / "calc.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        (worktree_repo / "test_calc.py").write_text(
            "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n",
            encoding="utf-8",
        )
        return 0

    def fake_run_task_verification(**kwargs):  # type: ignore[no-untyped-def]
        commands = list(kwargs["commands"])
        captured["post_change_verify_commands"] = commands
        artifact_path = Path(kwargs["artifact_path"])
        artifact_path.write_text("pytest passed\n", encoding="utf-8")
        return VerifyRunResult(
            commands=commands,
            command_results=[
                VerifyCommandResult(
                    command=commands[0],
                    exit_code=0,
                    output="pytest passed",
                    stdout="pytest passed",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["calc.py", "test_calc.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(changed_files=(), patch_text=""),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["calc.py", "test_calc.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["calc.py", "test_calc.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        fake_run_task_verification,
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
        verify_command_selection=ResolvedVerifyCommands(
            commands=("pytest -q",),
            source="config.verify_commands_fallback",
        ),
    )

    assert result.success is True
    assert result.verify_summary == "verification passed (1/1)"
    assert result.verify_command_source == "repo_scan.likely_test_commands"
    assert captured["initial_authoritative_verification_commands"] is None
    assert captured["initial_cfg_verify_commands"] == []
    assert captured["post_change_verify_commands"] == ["pytest -q"]


def test_run_task_worker_strict_verify_keeps_commit_unverified_when_no_commands_exist(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Bootstrap package",
        estimated_files=["package.json", "src/index.js"],
        branch="feat/t01-bootstrap",
    )
    task["write_scope"] = ["package.json", "src/index.js"]
    task["acceptance_criteria"] = ["Bootstrap the package files."]
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["package.json", "src/index.js"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(changed_files=(), patch_text=""),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["package.json", "src/index.js"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["package.json", "src/index.js"],
    )

    def fail_verify(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("strict mode should fail before verification runs")

    monkeypatch.setattr("alysis_code.swarm_worker.run_task_verification", fail_verify)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="strict",
        verify_commands=["pytest -q"],
        verify_command_selection=ResolvedVerifyCommands(
            commands=("pytest -q",),
            source="config.verify_commands_fallback",
        ),
    )

    # Missing tooling is not a defect in the work: the commit is kept and reported as
    # an unverified completion instead of a failure that would discard it.
    assert result.success is True
    assert result.verification_unavailable is True
    assert result.verify_failed is False
    assert result.failure_reason is None
    assert result.commit_hash == "deadbeef"
    assert "completed_unverified" in (result.verify_summary or "")
    assert result.verify_command_source == "task_refinement.no_authoritative_commands"
    assert captured["authoritative_verification_commands"] is None
    assert result.error is None
    assert any("nothing checked it" in item for item in result.warnings)


def test_run_task_worker_rejects_agent_exception_as_zero_diff_noop(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)

    def fake_run_agent(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider timeout")

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run after an agent exception")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is False
    assert result.commit_hash is None
    assert result.changed_files == []
    assert result.salvaged_agent_exception is False
    assert result.agent_exception_summary == "RuntimeError: provider timeout"
    assert result.effective_result_kind == "failure"
    assert result.noop_success is False
    assert result.error == "agent raised: RuntimeError: provider timeout"
    assert "Worker failed. Error: agent raised: RuntimeError: provider timeout" in result.summary
    payload = result.to_json()
    assert payload["result_kind"] == "failure"
    assert payload["salvaged_agent_exception"] is False
    assert payload["agent_exception_summary"] == "RuntimeError: provider timeout"
    report = (paths.execution_reports_dir / f"{task['id']}.md").read_text(encoding="utf-8")
    assert "- Salvaged Agent Exception: no" in report
    assert "- Agent Exception Summary: RuntimeError: provider timeout" in report


def test_run_task_worker_rejects_verified_zero_diff_after_nonzero_exit(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)
    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 1)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run after a non-zero agent exit")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is False
    assert result.agent_exit_code != 0
    assert result.salvaged_nonzero_exit is False
    assert result.salvaged_agent_exception is False
    assert "agent exited non-zero (1)" in (result.error or "")


def test_run_task_worker_accepts_zero_diff_when_verification_is_off(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run when verify_mode=off")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is True
    assert result.commit_hash is None
    assert result.verify_failed is False
    assert result.verify_summary == "verification skipped: no changes needed"
    assert result.effective_result_kind == "success_noop"
    assert result.noop_success is True
    assert result.noop_reason == "already_satisfied"
    assert "already satisfied" in result.summary
    assert "zero-diff" not in result.summary
    payload = result.to_json()
    assert payload["result_kind"] == "success_noop"
    assert payload["noop_success"] is True
    assert payload["noop_reason"] == "already_satisfied"


def test_run_task_worker_rejects_zero_diff_agent_exception_when_verification_is_off(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)

    def fake_run_agent(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider timeout")

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run when verify_mode=off")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is False
    assert result.salvaged_agent_exception is False
    assert result.agent_exception_summary == "RuntimeError: provider timeout"
    assert result.verify_summary is None
    assert "agent raised: RuntimeError: provider timeout" in result.summary


def test_run_task_worker_executor_429_exception_is_provider_throttled(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)

    def fake_run_agent(**_kwargs):  # type: ignore[no-untyped-def]
        request = httpx.Request("POST", "https://provider.invalid/v1/responses")
        response = httpx.Response(429, request=request)
        raise LLMError("Provider request failed") from httpx.HTTPStatusError(
            "Request rejected", request=request, response=response
        )

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run when verify_mode=off")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is False
    assert result.failure_category == FailureCategory.PROVIDER_THROTTLED
    assert result.to_json()["failure_category"] == FailureCategory.PROVIDER_THROTTLED.value


def test_run_task_worker_executor_timeout_exception_is_provider_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)

    def fake_run_agent(**_kwargs):  # type: ignore[no-untyped-def]
        raise LLMError("LLM request failed") from httpx.ReadTimeout("Fixture deadline")

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run when verify_mode=off")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is False
    assert result.failure_category == FailureCategory.PROVIDER_UNAVAILABLE
    assert result.to_json()["failure_category"] == FailureCategory.PROVIDER_UNAVAILABLE.value


def test_run_task_worker_rejects_zero_diff_when_verification_fails(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **kwargs: _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=["pytest -q"],
            exit_codes=[1],
            real_executions=[True],
            outputs=["1 failed\n"],
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is False
    assert result.commit_hash is None
    assert result.verify_failed is False
    assert result.failure_reason == "noop_verification_failed"
    assert result.effective_result_kind == "failure"
    assert result.noop_success is False
    assert result.verify_payload is not None
    assert result.verify_payload["all_passed"] is False
    assert "already-satisfied verification failed" in result.summary


def test_run_task_worker_classifies_zero_diff_infra_verification_failure(
    tmp_path: Path, monkeypatch
) -> None:
    _repo, paths, task, worktree_repo = _prepare_zero_diff_worker_case(tmp_path)
    _configure_zero_diff_worker(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **kwargs: _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=["pytest -q"],
            exit_codes=[1],
            real_executions=[None],
            outputs=["failed to connect to the docker API at unix:///var/run/docker.sock\n"],
            failure_category=FailureCategory.INFRA_UNAVAILABLE,
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is False
    assert result.failure_reason == "verification_infra_unavailable"
    assert result.failure_category == FailureCategory.INFRA_UNAVAILABLE.value
    assert "verification infrastructure unavailable" in result.summary


def test_run_task_worker_warn_accepts_material_change_when_verification_infra_unavailable(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_branch = _init_git_repo(repo)
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Build static page",
        estimated_files=["index.html"],
        branch="feat/t01-static-page",
    )
    save_plan(paths, plan)

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "index.html").write_text("<!doctype html><title>Demo</title>\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **kwargs: _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=["pytest -q"],
            exit_codes=[1],
            real_executions=[None],
            outputs=["verify sandbox unavailable: no usable backend\n"],
            failure_category=FailureCategory.INFRA_UNAVAILABLE,
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=repo,
        base_branch=base_branch,
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is True
    assert result.commit_hash is not None
    assert result.verify_failed is False
    assert result.failure_reason is None
    assert result.effective_result_kind == "success_commit"
    assert result.verify_payload is not None
    assert result.verify_payload["all_passed"] is False
    assert any("Verification infrastructure warning" in warning for warning in result.warnings)


def test_run_task_worker_setup_failure_is_not_salvaged(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Setup failure",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-setup-fail",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    (worktree_repo / ".git").mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_runtime_artifact_excludes",
        lambda _root: (_ for _ in ()).throw(GitOpsError("index dirty")),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_agent",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("run_agent should not be called")),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )

    called_stage = {"value": False}

    def fail_if_called(_root: Path) -> None:
        called_stage["value"] = True
        raise AssertionError("stage_all should not run when setup fails")

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fail_if_called)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert called_stage["value"] is False
    assert result.success is False
    assert result.salvaged_nonzero_exit is False
    assert result.salvaged_agent_exception is False
    assert result.agent_exception_summary is None
    assert "worker setup failed" in result.summary


def test_run_task_worker_agent_exception_strict_scope_violation_does_not_salvage(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Scope failure",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-scope-fail",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    def fake_run_agent(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("provider timeout")

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["README.md"],
    )

    called_stage = {"value": False}

    def fail_if_called(_root: Path) -> None:
        called_stage["value"] = True
        raise AssertionError("stage_all should not run when strict scope blocks exception salvage")

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fail_if_called)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert called_stage["value"] is False
    assert result.success is False
    assert result.failure_reason == "scope_violation"
    assert result.salvaged_agent_exception is False
    assert result.agent_exception_summary == "RuntimeError: provider timeout"
    assert "Task was blocked due to strict scope isolation." in result.summary


def test_run_task_worker_nonzero_exit_with_runtime_artifact_changes_does_not_salvage(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Reject protected runtime drift",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-runtime-artifacts",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    snapshots = [{}, {"execution/sessions/t01": "changed"}]
    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 1)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.snapshot_runtime_tree",
        lambda _root: snapshots.pop(0),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )

    called_stage = {"value": False}

    def fail_if_called(_root: Path) -> None:
        called_stage["value"] = True
        raise AssertionError("stage_all should not run when runtime artifacts changed")

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fail_if_called)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert called_stage["value"] is False
    assert result.success is False
    assert result.commit_hash is None
    assert result.agent_exit_code == 1
    assert result.salvaged_nonzero_exit is False
    assert "attempted protected .alysis modifications" in result.summary


def test_run_task_worker_nonzero_exit_strict_scope_violation_does_not_salvage(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Reject out-of-scope salvage",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-scope-salvage",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 1)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py", "README.md"],
    )

    called_stage = {"value": False}

    def fail_if_called(_root: Path) -> None:
        called_stage["value"] = True
        raise AssertionError("stage_all should not run when strict scope blocks salvage")

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fail_if_called)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert called_stage["value"] is False
    assert result.success is False
    assert result.commit_hash is None
    assert result.failure_reason == "scope_violation"
    assert result.scope_violation_files == ["README.md"]
    assert result.agent_exit_code == 1
    assert result.salvaged_nonzero_exit is False


def test_run_task_worker_strict_scope_fails_on_out_of_scope_changes(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Scoped task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-scoped",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py", "README.md"],
    )

    called_stage = {"value": False}

    def fail_if_called(_root: Path) -> None:
        called_stage["value"] = True
        raise AssertionError("stage_all should not be called when strict scope fails")

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fail_if_called)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
    )
    assert called_stage["value"] is False
    assert result.success is False
    assert result.commit_hash is None
    assert result.failure_reason == "scope_violation"
    assert result.scope_violation_files == ["README.md"]
    report_text = (paths.root / result.report_path).read_text(encoding="utf-8")
    assert "Out-of-scope file changes detected" in result.summary
    assert "Task was blocked due to strict scope isolation." in result.summary
    assert "Allowed scope: ['src/in_scope.py']" in result.summary
    assert "README.md" in report_text


def test_run_task_worker_defaults_scope_to_strict(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Scoped task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-default-strict",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured["allow_write_globs"] = kwargs["allow_write_globs"]
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py", "README.md"],
    )

    called_stage = {"value": False}

    def fail_if_called(_root: Path) -> None:
        called_stage["value"] = True
        raise AssertionError("stage_all should not be called when default strict scope fails")

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fail_if_called)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        verify_mode="off",
    )

    assert captured["allow_write_globs"] == ["src/in_scope.py"]
    assert called_stage["value"] is False
    assert result.success is False
    assert result.failure_reason == "scope_violation"
    assert result.scope_violation_files == ["README.md"]


def test_run_task_worker_strict_scope_rejects_agent_committed_out_of_scope_changes(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", os.fspath(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.name", "Temp User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.email", "temp@example.com"],
        check=True,
    )
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / "src" / "in_scope.py").write_text("print('base')\n", encoding="utf-8")
    subprocess.run(["git", "-C", os.fspath(repo), "add", "src/in_scope.py"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "Initial commit", "-q"], check=True
    )
    base_branch = subprocess.run(
        ["git", "-C", os.fspath(repo), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", os.fspath(repo), "checkout", "-b", "feat/t01-scoped"],
        check=True,
        capture_output=True,
        text=True,
    )

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Scoped task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-scoped",
    )
    save_plan(paths, plan)

    def fake_run_agent(*, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        (root / "README.md").write_text("# out of scope\n", encoding="utf-8")
        subprocess.run(["git", "-C", os.fspath(root), "add", "README.md"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(root),
                "-c",
                "user.name=Temp User",
                "-c",
                "user.email=temp@example.com",
                "commit",
                "-m",
                "T01: out-of-scope",
                "-q",
            ],
            check=True,
        )
        return 0

    called_stage = {"value": False}

    def fail_if_called(_root: Path) -> None:
        called_stage["value"] = True
        raise AssertionError("stage_all should not be called when strict scope fails")

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fail_if_called)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=repo,
        base_branch=base_branch,
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert called_stage["value"] is False
    assert result.success is False
    assert result.commit_hash is None
    assert result.failure_reason == "scope_violation"
    assert result.scope_violation_files == ["README.md"]
    assert result.changed_files == ["README.md"]
    assert "Task was blocked due to strict scope isolation." in result.summary
    patch_text = (paths.root / result.patch_path).read_text(encoding="utf-8")
    assert "README.md" in patch_text


def test_run_task_worker_warn_scope_ignores_python_support_file_drift(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Add tests",
        estimated_files=["tests/test_team_labels.py"],
        branch="feat/t01-tests",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: [
            "tests/test_team_labels.py",
            "tests/__init__.py",
            "tests/conftest.py",
        ],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: [
            "tests/test_team_labels.py",
            "tests/__init__.py",
            "tests/conftest.py",
        ],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: [
            "tests/test_team_labels.py",
            "tests/__init__.py",
            "tests/conftest.py",
        ],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="warn",
    )
    assert result.success is True
    assert not any("Out-of-scope file changes detected" in warning for warning in result.warnings)


def test_run_task_worker_warn_scope_allows_out_of_scope_changes_with_warning(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Scoped task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-scoped",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("src/in_scope.py", "README.md"),
            patch_text="PATCH\n",
        ),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py", "README.md"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py", "README.md"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="warn",
        verify_mode="off",
    )

    assert result.success is True
    assert result.commit_hash == "deadbeef"
    assert result.failure_reason is None
    assert any("Out-of-scope file changes detected" in warning for warning in result.warnings)


def test_run_task_worker_off_scope_disables_write_scope_guardrails(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Scoped task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-scoped",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["allow_write_globs"] = kwargs.get("allow_write_globs")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("src/in_scope.py", "README.md"),
            patch_text="PATCH\n",
        ),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py", "README.md"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py", "README.md"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="off",
        verify_mode="off",
    )

    assert result.success is True
    assert result.warnings == []
    assert captured["allow_write_globs"] is None


def test_run_task_worker_strict_scope_expands_python_support_file_writes(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Add package module",
        estimated_files=["src/pkg/module.py"],
        branch="feat/t01-pkg",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["allow_write_globs"] = kwargs.get("allow_write_globs")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/pkg/module.py", "src/pkg/__init__.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/pkg/module.py", "src/pkg/__init__.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/pkg/module.py", "src/pkg/__init__.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
    )
    assert result.success is True
    allow_write_globs = captured["allow_write_globs"]
    assert allow_write_globs == ["src/pkg/module.py", "src/pkg/__init__.py"]


def test_run_task_worker_strict_scope_allows_related_rust_lockfile_and_target_outputs(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Add Rust entrypoint",
        estimated_files=["src/"],
        branch="feat/t01-rust",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    (worktree_repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["allow_write_globs"] = kwargs.get("allow_write_globs")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("src/main.rs", "Cargo.lock", "target/debug/demo"),
            patch_text="PATCH\n",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/main.rs", "Cargo.lock", "target/debug/demo"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/main.rs", "Cargo.lock"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/main.rs", "Cargo.lock"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is True
    assert result.failure_reason is None
    assert result.changed_files == ["src/main.rs", "Cargo.lock"]
    assert captured["allow_write_globs"] == ["src/**", "Cargo.lock"]


def test_run_task_worker_strict_scope_allows_related_rust_lockfile_for_nested_directory_scope(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Add Rust utility module",
        estimated_files=["src/utils/"],
        branch="feat/t01-rust-utils",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    (worktree_repo / "src" / "utils").mkdir(parents=True, exist_ok=True)
    (worktree_repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["allow_write_globs"] = kwargs.get("allow_write_globs")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("src/utils/mod.rs", "Cargo.lock", "target/debug/demo"),
            patch_text="PATCH\n",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/utils/mod.rs", "Cargo.lock", "target/debug/demo"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/utils/mod.rs", "Cargo.lock"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/utils/mod.rs", "Cargo.lock"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is True
    assert result.failure_reason is None
    assert result.changed_files == ["src/utils/mod.rs", "Cargo.lock"]
    assert captured["allow_write_globs"] == ["src/utils/**", "Cargo.lock"]


def test_run_task_worker_strict_scope_still_flags_unrelated_rust_source_changes(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Add Rust entrypoint",
        estimated_files=["src/"],
        branch="feat/t01-rust",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    (worktree_repo / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("src/main.rs", "Cargo.lock", "target/debug/demo", "examples/other.rs"),
            patch_text="PATCH\n",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/main.rs", "Cargo.lock", "target/debug/demo", "examples/other.rs"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is False
    assert result.failure_reason == "scope_violation"
    assert result.scope_violation_files == ["examples/other.rs"]


def test_run_task_worker_strict_scope_allows_bootstrap_python_package_files_and_filters_untracked_egg_info(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_branch = _init_git_repo(repo)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "checkout", "-b", "feat/t01-calcbox"],
        check=True,
    )
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Bootstrap package",
        estimated_files=[
            "pyproject.toml",
            "src/calcbox/__init__.py",
            "src/calcbox/core.py",
        ],
        branch="feat/t01-calcbox",
    )
    save_plan(paths, plan)

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "pyproject.toml").write_text("[project]\nname='calcbox'\n", encoding="utf-8")
        (root / "src" / "calcbox").mkdir(parents=True, exist_ok=True)
        (root / "src" / "calcbox" / "__init__.py").write_text("", encoding="utf-8")
        (root / "src" / "calcbox" / "core.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        egg_info = root / "src" / "calcbox.egg-info"
        egg_info.mkdir(parents=True, exist_ok=True)
        (egg_info / "PKG-INFO").write_text("Metadata-Version: 2.4\n", encoding="utf-8")
        (egg_info / "SOURCES.txt").write_text("src/calcbox/core.py\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=repo,
        base_branch=base_branch,
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is True
    assert result.failure_reason is None
    assert result.changed_files == [
        "pyproject.toml",
        "src/calcbox/__init__.py",
        "src/calcbox/core.py",
    ]
    assert all(".egg-info/" not in path for path in result.changed_files)
    assert "egg-info" not in (paths.root / result.patch_path).read_text(encoding="utf-8")

    committed_files = subprocess.run(
        ["git", "-C", os.fspath(repo), "show", "--name-only", "--pretty=format:", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "src/calcbox.egg-info/PKG-INFO" not in committed_files
    assert "src/calcbox.egg-info/SOURCES.txt" not in committed_files


def test_run_task_worker_keeps_tracked_egg_info_edit_while_filtering_untracked_side_effect(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_branch = _init_git_repo(repo)
    tracked_egg = repo / "src" / "calcbox.egg-info" / "PKG-INFO"
    tracked_egg.parent.mkdir(parents=True, exist_ok=True)
    tracked_egg.write_text("Metadata-Version: 2.3\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", os.fspath(repo), "add", tracked_egg.relative_to(repo).as_posix()],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "track egg-info", "-q"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "checkout", "-b", "feat/t01-egg-info"],
        check=True,
    )
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Update packaging metadata",
        estimated_files=["src/calcbox.egg-info/PKG-INFO"],
        branch="feat/t01-egg-info",
    )
    save_plan(paths, plan)

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        egg_info = root / "src" / "calcbox.egg-info"
        (egg_info / "PKG-INFO").write_text("Metadata-Version: 2.4\n", encoding="utf-8")
        (egg_info / "SOURCES.txt").write_text("src/calcbox/core.py\n", encoding="utf-8")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=repo,
        base_branch=base_branch,
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is True
    assert result.changed_files == ["src/calcbox.egg-info/PKG-INFO"]
    assert "SOURCES.txt" not in (paths.root / result.patch_path).read_text(encoding="utf-8")

    committed_files = subprocess.run(
        ["git", "-C", os.fspath(repo), "show", "--name-only", "--pretty=format:", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "src/calcbox.egg-info/PKG-INFO" in committed_files
    assert "src/calcbox.egg-info/SOURCES.txt" not in committed_files


def test_run_task_worker_sanitizes_agent_created_commit_with_untracked_egg_info_side_effects(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_branch = _init_git_repo(repo)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "checkout", "-b", "feat/t01-agent-commit"],
        check=True,
    )
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Bootstrap package",
        estimated_files=["pyproject.toml", "src/calcbox/core.py"],
        branch="feat/t01-agent-commit",
    )
    save_plan(paths, plan)
    captured: dict[str, str] = {}

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "pyproject.toml").write_text("[project]\nname='calcbox'\n", encoding="utf-8")
        (root / "src" / "calcbox").mkdir(parents=True, exist_ok=True)
        (root / "src" / "calcbox" / "core.py").write_text(
            "def add(a, b):\n    return a + b\n",
            encoding="utf-8",
        )
        egg_info = root / "src" / "calcbox.egg-info"
        egg_info.mkdir(parents=True, exist_ok=True)
        (egg_info / "PKG-INFO").write_text("Metadata-Version: 2.4\n", encoding="utf-8")
        subprocess.run(["git", "-C", os.fspath(root), "add", "-A"], check=True)
        subprocess.run(
            ["git", "-C", os.fspath(root), "commit", "-m", "agent commit", "-q"],
            check=True,
        )
        captured["agent_commit_hash"] = subprocess.run(
            ["git", "-C", os.fspath(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=repo,
        base_branch=base_branch,
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is True
    assert result.commit_hash is not None
    assert result.commit_hash != captured["agent_commit_hash"]
    assert result.changed_files == ["pyproject.toml", "src/calcbox/core.py"]
    assert "egg-info" not in (paths.root / result.patch_path).read_text(encoding="utf-8")

    committed_files = subprocess.run(
        ["git", "-C", os.fspath(repo), "show", "--name-only", "--pretty=format:", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "pyproject.toml" in committed_files
    assert "src/calcbox/core.py" in committed_files
    assert "src/calcbox.egg-info/PKG-INFO" not in committed_files


def test_run_task_worker_strict_scope_allows_bootstrap_test_file_without_directory_placeholder_failure(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    base_branch = _init_git_repo(repo)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "checkout", "-b", "feat/t01-tests"],
        check=True,
    )
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Add JS tests",
        estimated_files=["test/slugify.test.js"],
        branch="feat/t01-tests",
    )
    save_plan(paths, plan)

    def fake_run_agent(*, root: Path, **_kwargs) -> int:
        (root / "test").mkdir(parents=True, exist_ok=True)
        (root / "test" / "slugify.test.js").write_text(
            "import test from 'node:test';\n",
            encoding="utf-8",
        )
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=repo,
        base_branch=base_branch,
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert result.success is True
    assert result.changed_files == ["test/slugify.test.js"]


def test_run_task_worker_strict_scope_still_blocks_real_out_of_scope_source_file(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Scoped task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-source-scope",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("src/in_scope.py", "src/other.py"),
            patch_text="PATCH\n",
        ),
    )

    called_stage = {"value": False}

    def fail_if_called(_root: Path) -> None:
        called_stage["value"] = True
        raise AssertionError("stage_all should not be called when strict scope fails")

    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", fail_if_called)

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )

    assert called_stage["value"] is False
    assert result.success is False
    assert result.failure_reason == "scope_violation"
    assert result.scope_violation_files == ["src/other.py"]


def test_run_task_worker_nonzero_exit_strict_verify_rejects_before_verification(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Verify strict task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-verify",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 1)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.stage_all",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("stage_all should not run after a non-zero agent exit")
        ),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.commit_all",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("commit_all should not run after a non-zero agent exit")
        ),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run after a non-zero agent exit")
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="strict",
        verify_commands=["pytest -q"],
    )
    assert result.success is False
    assert result.verify_failed is False
    assert result.commit_hash is None
    assert result.agent_exit_code == 1
    assert result.salvaged_nonzero_exit is False
    assert result.salvaged_agent_exception is False
    assert "agent exited non-zero (1)" in (result.error or "")
    assert result.verify_payload is None


def test_run_task_worker_strict_scope_blocks_verification_time_out_of_scope_mutations(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Verify mutation scope task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-verify-scope",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_execution_reporting_diff_with_commit_range",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("src/in_scope.py",),
            patch_text="PATCH\n",
        ),
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )
    mutation_snapshots = [{}, {"README.md": "meta:1:2:3"}]
    monkeypatch.setattr(
        "alysis_code.swarm_worker.snapshot_workspace_tree",
        lambda _root: mutation_snapshots.pop(0),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.build_workspace_snapshot_reporting_diff",
        lambda *_a, **_k: SimpleNamespace(
            changed_files=("README.md",),
            patch_text="# Workspace snapshot diff\n\nadded: README.md\n",
        ),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **kwargs: _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=["pytest -q"],
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="strict",
        verify_commands=["pytest -q"],
    )

    assert result.success is False
    assert result.verify_failed is False
    assert result.commit_hash is None
    assert result.failure_reason == "scope_violation"
    assert result.scope_violation_files == ["README.md"]
    assert result.changed_files == ["src/in_scope.py", "README.md"]
    assert "Worker blocked due to strict scope isolation." in result.summary
    assert (
        "Verification commands modified repository state after the task commit." in result.summary
    )
    patch_text = (paths.root / result.patch_path).read_text(encoding="utf-8")
    assert "Post-verification workspace diff" in patch_text
    assert "README.md" in patch_text


def test_run_task_worker_warn_verify_failure_rejects_material_result(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Verify warn task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-verify-warn",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", lambda **_kwargs: 0)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **kwargs: _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=["pytest -q"],
            exit_codes=[1],
            real_executions=[True],
            outputs=["1 failed in 0.10s\n"],
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )
    assert result.success is False
    assert result.verify_failed is True
    assert result.failure_reason == "verification_failed"
    assert "verification failed" in result.summary
    assert result.verify_payload is not None
    assert result.verify_payload["summary"].startswith("verification failed")
    command_results = result.verify_payload["command_results"]
    assert isinstance(command_results, list)
    assert command_results[0]["ok"] is False


def test_run_task_worker_warn_accepts_material_result_when_verification_improves_baseline(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Implement percent style",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-percent",
    )
    save_plan(paths, plan)

    worktree_repo = tmp_path / "worktree"
    worktree_repo.mkdir()
    _init_git_repo(worktree_repo)
    (worktree_repo / "src").mkdir()
    (worktree_repo / "src/in_scope.py").write_text("value = 'old'\n", encoding="utf-8")
    subprocess.run(["git", "-C", os.fspath(worktree_repo), "add", "src/in_scope.py"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(worktree_repo), "commit", "-m", "seed source", "-q"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(worktree_repo), "checkout", "-b", "feat/t01-percent"], check=True
    )

    def fake_run_agent(**kwargs):  # type: ignore[no-untyped-def]
        root = kwargs["root"]
        (root / "src/in_scope.py").write_text("value = 'new'\n", encoding="utf-8")
        return 0

    baseline_output = "\n".join(
        [
            "FAILED tests/test_formatting.py::test_percent_style - ValueError",
            "FAILED tests/test_formatting.py::test_currency_style - ValueError",
        ]
    )
    current_output = "FAILED tests/test_formatting.py::test_currency_style - ValueError\n"

    def fake_run_task_verification(**kwargs):  # type: ignore[no-untyped-def]
        return _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=["pytest -q"],
            exit_codes=[1],
            real_executions=[True],
            outputs=[current_output],
        )

    def fake_baseline_verification(**kwargs):  # type: ignore[no-untyped-def]
        return _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=kwargs["commands"],
            exit_codes=[1],
            real_executions=[True],
            outputs=[baseline_output],
        )

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        fake_run_task_verification,
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker._run_baseline_verification_snapshot",
        fake_baseline_verification,
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="warn",
        verify_commands=["pytest -q"],
    )

    assert result.success is True
    assert result.verify_failed is False
    assert result.failure_reason is None
    assert result.verify_payload is not None
    assert result.verify_payload["all_passed"] is False
    assert result.verify_payload["baseline_comparison"]["accepted"] is True
    assert result.verify_payload["baseline_comparison"]["baseline_failure_count"] == 2
    assert result.verify_payload["baseline_comparison"]["current_failure_count"] == 1
    assert "reduced pre-existing baseline failures (2 -> 1)" in result.summary


def test_baseline_improved_rejects_unchanged_remaining_failed_command(tmp_path: Path) -> None:
    commands = [
        "python -m doctest README.md",
        "pytest --doctest-glob=README.md -q README.md",
    ]
    pytest_failure = "\n".join(
        [
            "FAILED README.md::README.md",
            "_____________________________ [doctest] README.md ______________________________",
            "ModuleNotFoundError: No module named 'mathlet'",
        ]
    )
    baseline = _build_verify_run_result(
        artifact_path=tmp_path / "baseline.txt",
        commands=commands,
        exit_codes=[1, 1],
        real_executions=[True, True],
        outputs=["Failed example:\nExpected: 7\nGot: 6\n", pytest_failure],
    )
    current = _build_verify_run_result(
        artifact_path=tmp_path / "current.txt",
        commands=commands,
        exit_codes=[0, 1],
        real_executions=[True, True],
        outputs=["", pytest_failure],
    )

    assert _baseline_improved_failure_comparison(baseline=baseline, current=current) is None
    assert (
        _baseline_improved_failure_comparison(
            baseline=baseline,
            current=current,
            task={
                "title": "Fix README.md doctest example",
                "description": "Update README.md so doctest examples match mathlet.double.",
                "acceptance_criteria": ["pytest doctest for README.md passes"],
            },
        )
        is None
    )


def test_baseline_improved_accepts_unchanged_unrelated_residual_failure(
    tmp_path: Path,
) -> None:
    commands = ["pytest -q"]
    percent_failure = "\n".join(
        [
            "FAILED tests/test_formatting.py::test_percent_style - ValueError",
            "ValueError: unknown style: percent",
        ]
    )
    baseline = _build_verify_run_result(
        artifact_path=tmp_path / "baseline.txt",
        commands=commands,
        exit_codes=[1],
        real_executions=[True],
        outputs=[percent_failure],
    )
    current = _build_verify_run_result(
        artifact_path=tmp_path / "current.txt",
        commands=commands,
        exit_codes=[1],
        real_executions=[True],
        outputs=[percent_failure],
    )

    comparison = _baseline_improved_failure_comparison(
        baseline=baseline,
        current=current,
        task={
            "title": "Add focused tests for currency formatting",
            "description": "Add currency-specific tests for render_value.",
            "acceptance_criteria": ["pytest -q -k currency passes"],
        },
    )

    assert comparison is not None
    assert comparison["accepted"] is True
    assert comparison["resolved_failure_count"] == 0
    assert comparison["unchanged_unrelated_failures"]


def test_run_task_worker_accepts_agent_created_commit_when_only_alysis_untracked_remains(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", os.fspath(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.name", "Temp User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.email", "temp@example.com"],
        check=True,
    )
    (repo / "README.md").write_text("# temp\n", encoding="utf-8")
    subprocess.run(["git", "-C", os.fspath(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "Initial commit", "-q"], check=True
    )
    base_branch = subprocess.run(
        ["git", "-C", os.fspath(repo), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", os.fspath(repo), "checkout", "-b", "feat/t01-script"],
        check=True,
        capture_output=True,
        text=True,
    )

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Add verification script",
        estimated_files=["tests/verify_site.sh"],
        branch="feat/t01-script",
    )
    save_plan(paths, plan)

    def fake_run_agent(*, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        tests_dir = root / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        script_path = tests_dir / "verify_site.sh"
        script_path.write_text("#!/usr/bin/env bash\necho ok\n", encoding="utf-8")
        script_path.chmod(0o755)
        subprocess.run(["git", "-C", os.fspath(root), "add", "tests/verify_site.sh"], check=True)
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(root),
                "-c",
                "user.name=Temp User",
                "-c",
                "user.email=temp@example.com",
                "commit",
                "-m",
                "T01: Add verification script",
                "-q",
            ],
            check=True,
        )
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: [],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=repo,
        base_branch=base_branch,
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="warn",
        verify_mode="off",
    )

    expected_commit = subprocess.run(
        ["git", "-C", os.fspath(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert result.success is True
    assert result.commit_hash == expected_commit
    assert "produced a task commit" in result.summary
    assert result.changed_files == ["tests/verify_site.sh"]


def test_run_task_worker_ignores_alysis_artifacts_for_agent_git_add_all(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "-C", os.fspath(repo), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.name", "Temp User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", os.fspath(repo), "config", "user.email", "temp@example.com"],
        check=True,
    )
    (repo / "README.md").write_text("# temp\n", encoding="utf-8")
    subprocess.run(["git", "-C", os.fspath(repo), "add", "README.md"], check=True)
    subprocess.run(
        ["git", "-C", os.fspath(repo), "commit", "-m", "Initial commit", "-q"], check=True
    )
    base_branch = subprocess.run(
        ["git", "-C", os.fspath(repo), "symbolic-ref", "--short", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", os.fspath(repo), "checkout", "-b", "feat/t01-index"],
        check=True,
        capture_output=True,
        text=True,
    )

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Add index page",
        estimated_files=["index.html"],
        branch="feat/t01-index",
    )
    save_plan(paths, plan)

    def fake_run_agent(*, root: Path, **_kwargs):  # type: ignore[no-untyped-def]
        (root / "index.html").write_text("<h1>Hello</h1>\n", encoding="utf-8")
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(root),
                "-c",
                "user.name=Temp User",
                "-c",
                "user.email=temp@example.com",
                "add",
                "-A",
            ],
            check=True,
        )
        subprocess.run(
            [
                "git",
                "-C",
                os.fspath(root),
                "-c",
                "user.name=Temp User",
                "-c",
                "user.email=temp@example.com",
                "commit",
                "-m",
                "T01: Add index page",
                "-q",
            ],
            check=True,
        )
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["index.html"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=repo,
        base_branch=base_branch,
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="warn",
        verify_mode="off",
    )

    committed_files = subprocess.run(
        ["git", "-C", os.fspath(repo), "show", "--name-only", "--format=", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert result.success is True
    assert result.commit_hash is not None
    assert "index.html" in committed_files
    assert not any(path.startswith(".alysis/") for path in committed_files)


def test_run_task_worker_passes_worker_trace_surface_to_run_agent(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Trace surface task",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-trace-surface",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs):  # type: ignore[no-untyped-def]
        captured["surface"] = kwargs.get("surface")
        captured["one_shot_execution"] = kwargs.get("one_shot_execution")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        trace_sink=_ListTraceSink(),
        trace_level="compact",
    )
    assert result.success is True
    assert captured["one_shot_execution"] is True
    assert isinstance(captured["surface"], RecordingSurface)
    assert isinstance(captured["surface"]._delegate, SwarmWorkerTraceSurface)


def test_run_task_worker_verify_off_disables_verify_tool_and_uses_execution_sessions_dir(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Verify off policy",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-verify-off",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["verification_enabled"] = kwargs.get("verification_enabled")
        captured["session_log_dir_override"] = kwargs.get("session_log_dir_override")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="off",
    )
    assert result.success is True
    assert captured["verification_enabled"] is False
    assert captured["session_log_dir_override"] == paths.execution_sessions_dir


def test_run_task_worker_propagates_authoritative_verify_commands(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Verify propagation",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-verify-prop",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["verification_enabled"] = kwargs.get("verification_enabled")
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        cfg = kwargs["cfg"]
        captured["cfg_verify_commands"] = list(cfg.verify_commands)
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    monkeypatch.setattr(
        "alysis_code.swarm_worker.run_task_verification",
        lambda **kwargs: _build_verify_run_result(
            artifact_path=kwargs["artifact_path"],
            commands=["PYTHONPATH=src pytest -q", "ruff check ."],
            outputs=["2 passed\n", "All checks passed!\n"],
        ),
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
        verify_mode="strict",
        verify_commands=["PYTHONPATH=src pytest -q", "ruff check ."],
    )
    assert result.success is True
    assert captured["verification_enabled"] is True
    assert captured["authoritative_verification_commands"] == [
        "PYTHONPATH=src pytest -q",
        "ruff check .",
    ]
    assert captured["cfg_verify_commands"] == [
        "PYTHONPATH=src pytest -q",
        "ruff check .",
    ]
    context_path = paths.execution_dir / "context" / f"{task['id']}_context.md"
    context_text = context_path.read_text(encoding="utf-8")
    assert "## Authoritative Verification" in context_text
    assert "every listed command passes" in context_text
    assert "Do not leave temporary command-output files in the repository root." in context_text
    assert "- `PYTHONPATH=src pytest -q`" in context_text
    assert "- `ruff check .`" in context_text
    assert result.verify_payload is not None
    assert result.verify_payload["all_passed"] is True
    assert result.verify_payload["commands"] == [
        "PYTHONPATH=src pytest -q",
        "ruff check .",
    ]
    command_results = result.verify_payload["command_results"]
    assert isinstance(command_results, list)
    assert [item["command"] for item in command_results] == [
        "PYTHONPATH=src pytest -q",
        "ruff check .",
    ]


def test_run_task_worker_installs_runtime_artifact_excludes_for_git_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Install runtime excludes",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-runtime-excludes",
    )
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    (worktree_repo / ".git").mkdir()
    captured: dict[str, object] = {}

    def fake_run_agent(**_kwargs) -> int:  # type: ignore[no-untyped-def]
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_runtime_artifact_excludes",
        lambda root: captured.setdefault("exclude_root", root),
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="test-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
    )
    assert result.success is True
    assert captured["exclude_root"] == worktree_repo


def test_swarm_worker_trace_surface_compact_prefixes_and_summarizes() -> None:
    sink = _ListTraceSink()
    surface = SwarmWorkerTraceSurface(
        run_id="run-1",
        task_id="T01",
        trace_sink=sink,
        trace_level="compact",
    )

    surface.on_progress_update("Understanding the task.")
    surface.on_assistant_token("hello")
    surface.on_tool_start(
        ToolStartEvent(tool_call_id="tool-1", name="fs_read", args={"path": "src/app.py"}, step=1)
    )
    surface.on_tool_output(
        ToolOutputEvent(
            tool_call_id="tool-1",
            name="fs_read",
            chunk='{"path":"src/app.py","content":"print(1)","truncated":false}',
        )
    )
    surface.on_tool_end(
        ToolEndEvent(tool_call_id="tool-1", name="fs_read", status="done", elapsed_ms=125)
    )
    surface.on_assistant_message_done("Implemented the change.")

    rendered = [format_swarm_trace_message(event) for event in sink.events]
    assert rendered[0] == "[T01] Understanding the task."
    assert any("[T01] Receiving worker output..." == line for line in rendered)
    assert any("[T01] Step 1: Read File" == line for line in rendered)
    assert any('Loaded "src/app.py"' in line for line in rendered)
    assert any("Worker response ready" in line for line in rendered)
    assert not any("Worker response preview" in line for line in rendered)


def test_swarm_worker_trace_surface_names_remote_site_outcomes_without_failure() -> None:
    sink = _ListTraceSink()
    surface = SwarmWorkerTraceSurface(
        run_id="run-1",
        task_id="T03",
        trace_sink=sink,
        trace_level="compact",
    )

    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="tool-1",
            name="web_fetch",
            status="failed",
            elapsed_ms=900,
            meta={
                "error": "HTTP error 403 while fetching '…': remote site declined automated access.",
                "blocked_by_remote_site": True,
                "remote_site_reason": "site doesn't allow automated access",
            },
        )
    )
    surface.on_tool_end(
        ToolEndEvent(
            tool_call_id="tool-2",
            name="web_fetch",
            status="done",
            elapsed_ms=5,
            meta={"tool_unavailable": True, "unavailable_reason": "Network is unreachable"},
        )
    )

    rendered = [format_swarm_trace_message(event) for event in sink.events]
    assert any("site doesn't allow automated access" in line for line in rendered)
    assert any("unavailable" in line for line in rendered)
    assert not any("failed" in line for line in rendered)


def test_swarm_worker_trace_surface_full_emits_richer_detail() -> None:
    sink = _ListTraceSink()
    surface = SwarmWorkerTraceSurface(
        run_id="run-1",
        task_id="T02",
        trace_sink=sink,
        trace_level="full",
    )

    surface.on_assistant_token("x" * 400)
    surface.on_tool_start(
        ToolStartEvent(tool_call_id="tool-1", name="fs_read", args={"path": "src/app.py"}, step=2)
    )
    surface.on_tool_end(
        ToolEndEvent(tool_call_id="tool-1", name="fs_read", status="failed", elapsed_ms=50)
    )
    surface.on_assistant_message_done("Completed implementation details.")

    messages = [event.message for event in sink.events]
    assert any("Worker output progress" in msg for msg in messages)
    assert any(msg.startswith("Input:") for msg in messages)
    assert any("Read File failed (50ms)" in msg for msg in messages)
    assert not any(msg.startswith("Goal:") for msg in messages)
    assert not any(msg.startswith("Action:") for msg in messages)
    assert not any(msg.startswith("Fallback:") for msg in messages)
    assert not any(msg.startswith("Decision:") for msg in messages)
    assert any("Worker response preview" in msg for msg in messages)


def test_swarm_worker_trace_surface_emits_warning_phase() -> None:
    sink = _ListTraceSink()
    surface = SwarmWorkerTraceSurface(
        run_id="run-1",
        task_id="T03",
        trace_sink=sink,
        trace_level="compact",
    )

    surface.on_warning(
        "Model metadata warning for unknown-model-xyz (roles: coding): fallback capacity metadata in context_window_tokens, max_output_tokens."
    )

    assert [event.phase for event in sink.events] == ["worker.warning"]
    assert sink.events[0].message.startswith(
        "Worker warning: Model metadata warning for unknown-model-xyz"
    )


def test_run_task_worker_uses_coding_role_model(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Coding model routing",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-model",
    )
    plan["role_models"] = {"coding": "plan-coding-model"}
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        cfg = kwargs["cfg"]
        captured["model"] = cfg.model
        captured["enable_compaction"] = kwargs.get("enable_compaction")
        captured["enable_tool_output_offload"] = kwargs.get("enable_tool_output_offload")
        captured["enable_conversation_summarization"] = kwargs.get(
            "enable_conversation_summarization"
        )
        captured["compaction_profile"] = kwargs.get("compaction_profile")
        captured["subagents_enabled"] = kwargs.get("subagents_enabled")
        captured["enforce_explicit_subagent_requests"] = kwargs.get(
            "enforce_explicit_subagent_requests"
        )
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    result = run_task_worker(
        task=task,
        plan=load_plan(paths),
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="default-model", subagents_enabled=True),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
    )
    assert result.success is True
    assert captured["model"] == "plan-coding-model"
    assert captured["enable_compaction"] is False
    assert captured["enable_tool_output_offload"] is True
    assert captured["enable_conversation_summarization"] is True
    assert captured["compaction_profile"] == "execution"
    assert captured["subagents_enabled"] is False
    assert captured["enforce_explicit_subagent_requests"] is False


def test_run_task_worker_passes_images_when_opted_in_and_vision_supported(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Use diagram image",
        description="Read diagram.png before coding",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-images",
    )
    save_plan(paths, plan)

    image_asset = repo / "diagram.png"
    image_asset.write_bytes(b"image-bytes")
    attach_asset(repo, image_asset)
    plan = load_plan(paths)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, list[str] | None] = {"image_paths": None}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["image_paths"] = kwargs.get("image_paths")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )
    monkeypatch.setenv("ALYSIS_TASK_IMAGES", "1")
    monkeypatch.setenv("ALYSIS_SUPPORTS_VISION", "1")

    result = run_task_worker(
        task=next(t for t in plan["tasks"] if t["id"] == task["id"]),
        plan=plan,
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(model="default-model"),
        mode="auto",
        yes=True,
        max_steps=5,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
    )
    assert result.success is True
    image_paths = captured["image_paths"] or []
    assert len(image_paths) == 1
    assert str(image_paths[0]).endswith("diagram.png")
    budget_path = paths.execution_dir / "budgets" / f"{task['id']}.json"
    budget_payload = json.loads(budget_path.read_text(encoding="utf-8"))
    assert budget_payload["subagents_enabled"] is False
    assert budget_payload["image_count"] == 1
    assert budget_payload["image_budget_reserve_tokens"] > 0


def test_run_task_worker_uses_adaptive_managed_task_budget_without_override(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Adaptive worker budget",
        description="Use diagram image before coding",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-adaptive-budget",
    )
    save_plan(paths, plan)

    image_asset = repo / "diagram.png"
    image_asset.write_bytes(b"image-bytes")
    attach_asset(repo, image_asset)
    plan = load_plan(paths)
    task = next(t for t in plan["tasks"] if t["id"] == task["id"])
    task["attempts"] = 3
    save_plan(paths, plan)

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["max_steps"] = kwargs.get("max_steps")
        captured["enable_chat_turn_step_budget"] = kwargs.get("enable_chat_turn_step_budget")
        captured["subagents_enabled"] = kwargs.get("subagents_enabled")
        captured["image_paths"] = kwargs.get("image_paths")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )
    monkeypatch.setenv("ALYSIS_TASK_IMAGES", "1")
    monkeypatch.setenv("ALYSIS_SUPPORTS_VISION", "1")

    result = run_task_worker(
        task=task,
        plan=plan,
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(
            model="default-model",
            step_budget_policy="adaptive",
            task_max_steps=31,
            subagents_enabled=True,
        ),
        mode="auto",
        yes=True,
        max_steps=None,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
    )

    assert result.success is True
    assert captured["enable_chat_turn_step_budget"] is False
    assert captured["subagents_enabled"] is False
    image_paths = captured["image_paths"] or []
    assert len(image_paths) == 1

    budget_path = paths.execution_dir / "budgets" / f"{task['id']}.json"
    budget_payload = json.loads(budget_path.read_text(encoding="utf-8"))
    step_budget = budget_payload["step_budget"]
    assert step_budget["kind"] == "managed_task"
    assert step_budget["reason"] == "autonomous_unbounded"
    assert step_budget["hard_cap"] is None
    assert step_budget["resolved_max_steps"] is None
    assert step_budget["unlimited"] is True
    assert step_budget["override_applied"] is False
    assert step_budget["signals_used"]["attempt_count"] == 3
    assert step_budget["signals_used"]["image_count"] == 1
    assert captured["max_steps"] == step_budget["resolved_max_steps"]


def test_run_task_worker_uses_fixed_override_for_explicit_max_steps(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Fixed worker budget override",
        estimated_files=["src/in_scope.py"],
        branch="feat/t01-fixed-budget",
    )
    save_plan(paths, plan)
    task = next(t for t in plan["tasks"] if t["id"] == task["id"])
    task["attempts"] = 2

    worktree_repo = paths.run_dir / "worktrees" / str(task["id"]) / "repo"
    worktree_repo.mkdir(parents=True, exist_ok=True)
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["max_steps"] = kwargs.get("max_steps")
        captured["enable_chat_turn_step_budget"] = kwargs.get("enable_chat_turn_step_budget")
        captured["subagents_enabled"] = kwargs.get("subagents_enabled")
        return 0

    monkeypatch.setattr("alysis_code.swarm_worker.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.swarm_worker.list_changed_files_including_untracked",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.stage_all", lambda _root: None)
    monkeypatch.setattr("alysis_code.swarm_worker.unstage_staged_prefixes", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "alysis_code.swarm_worker.ensure_not_staged_prefixes", lambda *_a, **_k: None
    )
    monkeypatch.setattr(
        "alysis_code.swarm_worker.staged_files",
        lambda _root: ["src/in_scope.py"],
    )
    monkeypatch.setattr("alysis_code.swarm_worker.commit_all", lambda *_a, **_k: "deadbeef")
    monkeypatch.setattr("alysis_code.swarm_worker.format_patch_stdout", lambda *_a, **_k: "PATCH\n")
    monkeypatch.setattr(
        "alysis_code.swarm_worker.changed_files_between",
        lambda *_a, **_k: ["src/in_scope.py"],
    )

    result = run_task_worker(
        task=task,
        plan=plan,
        worktree_repo_path=worktree_repo,
        base_branch="main",
        run_paths=paths,
        cfg=AppConfig(
            model="default-model",
            step_budget_policy="adaptive",
            task_max_steps=40,
            subagents_enabled=True,
        ),
        mode="auto",
        yes=True,
        max_steps=7,
        api_key_override="k",
        no_log=True,
        console=_console(),
        scope_mode="strict",
    )

    assert result.success is True
    assert captured["enable_chat_turn_step_budget"] is False
    assert captured["subagents_enabled"] is False
    assert captured["max_steps"] == 7

    budget_path = paths.execution_dir / "budgets" / f"{task['id']}.json"
    budget_payload = json.loads(budget_path.read_text(encoding="utf-8"))
    step_budget = budget_payload["step_budget"]
    assert step_budget["resolved_max_steps"] == 7
    assert step_budget["hard_cap"] == 7
    assert step_budget["reason"] == "explicit_limit"
    assert step_budget["override_applied"] is True
