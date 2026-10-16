"""End-to-end coverage for `alysis forge run`, the sequential execution path.

These tests drive a real git repository so the branch/commit/verify/merge cycle is
exercised for real, and they hard-fail if any worktree machinery is touched: the
whole point of the sequential path is that it stays in the main checkout.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code import conflict_auto_resolver as conflict_auto_resolver_mod
from alysis_code import git_worktrees as git_worktrees_mod
from alysis_code import swarm_orchestrator as swarm_orchestrator_mod
from alysis_code.cli import app as alysis_app
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.forge_events import parse_event_line
from alysis_code.verify_gate import VerifyCommandResult, VerifyRunResult

_WORKTREE_ENTRYPOINTS = ("ensure_task_worktree", "remove_task_worktree", "prune_worktrees")
_CANONICAL_HOST_CUTOVER_SKIP = (
    "public forge run now requires canonical Host authority; legacy public-run behavior "
    "is covered by the canonical Host CLI and sequential-engine suites"
)


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
        "ALYSIS_CONTEXT_WINDOW": "200000",
        "ALYSIS_MAX_OUTPUT_TOKENS": "8192",
    }


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout


def _init_repo(repo: Path) -> None:
    _git(repo, "init", "-q")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("# Demo\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")


def _forbid_worktrees(monkeypatch) -> None:
    """Make every worktree entry point explode.

    Patched on the defining module and on each importer, because a `from x import y`
    binding keeps its own reference and would otherwise sail past a patch on `x`.
    """

    def _boom(*_args, **_kwargs):
        raise AssertionError("the sequential path must not touch worktree machinery")

    modules = (git_worktrees_mod, swarm_orchestrator_mod, conflict_auto_resolver_mod, cli_mod)
    for module in modules:
        for name in _WORKTREE_ENTRYPOINTS:
            if hasattr(module, name):
                monkeypatch.setattr(module, name, _boom)
    for module in (swarm_orchestrator_mod, cli_mod):
        for name in ("run_task_worker", "attempt_auto_resolve_conflict", "run_integration_gate"):
            if hasattr(module, name):
                monkeypatch.setattr(module, name, _boom)
    if hasattr(cli_mod, "run_swarm"):
        monkeypatch.setattr(cli_mod, "run_swarm", _boom)


def _assert_single_worktree(repo: Path) -> None:
    entries = [line for line in _git(repo, "worktree", "list").splitlines() if line.strip()]
    assert len(entries) == 1, f"expected only the main checkout, got: {entries}"


def _three_task_plan(repo: Path) -> tuple[object, list[dict], dict[str, str]]:
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Build three dependent slices"
    plan["summary"] = "Build three dependent slices"

    specs = [
        ("Create alpha module", "src/alpha.py"),
        ("Create beta module", "src/beta.py"),
        ("Create gamma module", "src/gamma.py"),
    ]
    tasks: list[dict] = []
    for title, rel_path in specs:
        task = add_task(
            plan,
            title=title,
            description=f"Task created from planning chat: {title}",
            estimated_files=[rel_path],
        )
        task["write_scope"] = [rel_path]
        tasks.append(task)

    # A strict chain: nothing may start before its predecessor is done.
    tasks[1]["dependencies"] = [tasks[0]["id"]]
    tasks[2]["dependencies"] = [tasks[1]["id"]]
    save_plan(paths, plan)

    files = {task["id"]: rel for task, (_title, rel) in zip(tasks, specs, strict=True)}
    return paths, tasks, files


def _passing_verification(monkeypatch, *, command: str = "pytest -q") -> list[str]:
    verified: list[str] = []

    def fake_run_task_verification(*, artifact_path: Path, **_kwargs):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verification passed\n", encoding="utf-8")
        verified.append(artifact_path.stem)
        return VerifyRunResult(
            commands=[command],
            command_results=[
                VerifyCommandResult(
                    command=command,
                    effective_command=command,
                    exit_code=0,
                    output="ok\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(cli_mod, "run_task_verification", fake_run_task_verification)
    return verified


def _events(output: str) -> list[dict]:
    parsed = [parse_event_line(line) for line in output.splitlines()]
    return [event for event in parsed if event is not None]


@pytest.mark.skip(reason=_CANONICAL_HOST_CUTOVER_SKIP)
def test_forge_run_executes_dependent_tasks_in_order_without_worktrees(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    paths, tasks, files = _three_task_plan(repo)
    expected_order = [task["id"] for task in tasks]

    _forbid_worktrees(monkeypatch)
    verified = _passing_verification(monkeypatch)

    agent_calls: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        index = len(agent_calls)
        assert index < len(expected_order), "the agent ran more times than there are tasks"
        task_id = expected_order[index]
        # Dependency order is the contract, so the instruction must be for the task
        # the sequential scheduler was supposed to pick at this position.
        assert task_id in instruction, f"expected {task_id} at position {index}"
        agent_calls.append(task_id)
        target = root / files[task_id]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# generated by {task_id}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "run",
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
            "pytest -q",
            "--mode",
            "auto",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert agent_calls == expected_order
    assert len(verified) == 3

    # 1. Every task produced its file, and the work landed on the base branch.
    _assert_single_worktree(repo)
    for rel_path in files.values():
        assert (repo / rel_path).exists(), rel_path
    tracked = _git(repo, "ls-files").split()
    for rel_path in files.values():
        assert rel_path in tracked, rel_path
    assert _git(repo, "status", "--porcelain").strip() == ""

    # 2. Plan state: all three done.
    final_plan = load_plan(paths)
    assert [task["status"] for task in final_plan["tasks"]] == ["done", "done", "done"]

    # 3. Events: one terminal event, one start/complete pair and one verification
    #    result per task, and no failures.
    events = _events(result.output)
    names = [event["event"] for event in events]
    assert names.count("run_completed") == 1
    assert names.count("error") == 0
    assert names[-1] == "run_completed"
    assert names.count("task_started") == 3
    assert names.count("task_completed") == 3
    assert names.count("task_failed") == 0

    started = [e["data"]["task_id"] for e in events if e["event"] == "task_started"]
    completed = [e["data"]["task_id"] for e in events if e["event"] == "task_completed"]
    assert started == expected_order
    assert completed == expected_order

    verification_events = [e for e in events if e["event"] == "verification_result"]
    assert len(verification_events) == 3
    assert all(event["data"]["passed"] is True for event in verification_events)
    assert [event["data"]["task_id"] for event in verification_events] == expected_order

    terminal = events[-1]
    assert terminal["data"]["ok"] is True
    assert terminal["data"]["exit_code"] == 0
    outcome = terminal["data"]["outcome"]
    assert outcome["engine"] == "sequential"
    assert outcome["worktrees_used"] is False
    assert outcome["parallel"] == 1
    assert outcome["stopped_reason"] == "completed"
    assert outcome["counts"] == {"executed": 3, "succeeded": 3, "failed": 0}
    assert [item["task_id"] for item in outcome["executed"]] == expected_order
    assert outcome["remaining"] == []

    # 4. Artifacts: one linear run log plus the run summary.
    summary = json.loads(
        (paths.execution_dir / "sequential_summary.json").read_text(encoding="utf-8")
    )
    assert summary["counts"]["succeeded"] == 3

    log_path = paths.execution_dir / "sequential_run.jsonl"
    records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    assert [r["task_id"] for r in records if r["event"] == "task_started"] == expected_order
    assert [r["task_id"] for r in records if r["event"] == "task_finished"] == expected_order
    assert all(r["success"] for r in records if r["event"] == "task_finished")


@pytest.mark.skip(reason=_CANONICAL_HOST_CUTOVER_SKIP)
def test_forge_run_stops_at_first_failure_and_leaves_dependents_unstarted(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    paths, tasks, files = _three_task_plan(repo)
    expected_order = [task["id"] for task in tasks]

    _forbid_worktrees(monkeypatch)

    failing_command = "pytest -q"

    def fake_run_task_verification(*, artifact_path: Path, **_kwargs):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verification failed\n", encoding="utf-8")
        passed = artifact_path.stem != expected_order[1]
        return VerifyRunResult(
            commands=[failing_command],
            command_results=[
                VerifyCommandResult(
                    command=failing_command,
                    effective_command=failing_command,
                    exit_code=0 if passed else 1,
                    output="ok\n" if passed else "1 failed\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(cli_mod, "run_task_verification", fake_run_task_verification)

    agent_calls: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        # Repair is disabled below, so one agent run per task keeps the position
        # and the task in lockstep.
        index = len(agent_calls)
        assert index < len(expected_order), "the agent ran more times than there are tasks"
        task_id = expected_order[index]
        assert task_id in instruction, f"expected {task_id} at position {index}"
        agent_calls.append(task_id)
        target = root / files[task_id]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# generated by {task_id}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "run",
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
            failing_command,
            "--verify-repair-attempts",
            "0",
            "--mode",
            "auto",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1, result.output
    _assert_single_worktree(repo)

    final_plan = load_plan(paths)
    statuses = {task["id"]: task["status"] for task in final_plan["tasks"]}
    assert statuses[expected_order[0]] == "done"
    assert statuses[expected_order[1]] == "verify_failed"
    # The third task depends on the second, so the run must not have started it.
    assert statuses[expected_order[2]] == "planned"

    events = _events(result.output)
    names = [event["event"] for event in events]
    assert names.count("run_completed") == 1
    assert names[-1] == "run_completed"
    assert names.count("task_completed") == 1
    assert names.count("task_failed") == 1

    terminal = events[-1]
    assert terminal["data"]["ok"] is False
    assert terminal["data"]["exit_code"] == 1
    outcome = terminal["data"]["outcome"]
    assert outcome["stopped_reason"] == "task_failed"
    assert outcome["counts"] == {"executed": 2, "succeeded": 1, "failed": 1}
    assert sorted(outcome["remaining"]) == sorted(expected_order[1:])


@pytest.mark.skip(reason=_CANONICAL_HOST_CUTOVER_SKIP)
def test_forge_run_keep_going_branches_later_tasks_from_the_pinned_base(
    tmp_path: Path, monkeypatch
) -> None:
    """A failed task leaves the checkout on its own branch; the next task must not
    inherit that. The base branch is pinned once for the whole run, so the surviving
    task's commit reaches base without dragging the rejected one along."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    base = _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Two independent slices"
    plan["summary"] = "Two independent slices"

    specs = [("Create alpha module", "src/alpha.py"), ("Create beta module", "src/beta.py")]
    tasks = []
    for title, rel_path in specs:
        task = add_task(
            plan,
            title=title,
            description=f"Task created from planning chat: {title}",
            estimated_files=[rel_path],
        )
        task["write_scope"] = [rel_path]
        tasks.append(task)
    save_plan(paths, plan)
    expected_order = [task["id"] for task in tasks]
    files = {task["id"]: rel for task, (_t, rel) in zip(tasks, specs, strict=True)}

    _forbid_worktrees(monkeypatch)

    def fake_run_task_verification(*, artifact_path: Path, **_kwargs):  # type: ignore[no-untyped-def]
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_text("verification\n", encoding="utf-8")
        passed = artifact_path.stem != expected_order[0]
        return VerifyRunResult(
            commands=["pytest -q"],
            command_results=[
                VerifyCommandResult(
                    command="pytest -q",
                    effective_command="pytest -q",
                    exit_code=0 if passed else 1,
                    output="ok\n" if passed else "1 failed\n",
                    real_execution=True,
                )
            ],
            artifact_path=artifact_path,
        )

    monkeypatch.setattr(cli_mod, "run_task_verification", fake_run_task_verification)

    agent_calls: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        task_id = expected_order[len(agent_calls)]
        assert task_id in instruction
        agent_calls.append(task_id)
        target = root / files[task_id]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# generated by {task_id}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "run",
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
            "pytest -q",
            "--verify-repair-attempts",
            "0",
            "--keep-going",
            "--mode",
            "auto",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 1, result.output
    assert agent_calls == expected_order
    _assert_single_worktree(repo)

    final_plan = load_plan(paths)
    statuses = {task["id"]: task["status"] for task in final_plan["tasks"]}
    assert statuses[expected_order[0]] == "verify_failed"
    assert statuses[expected_order[1]] == "done"

    # The rejected task's file must not have reached the base branch, and the
    # accepted one must have.
    base_tree = _git(repo, "ls-tree", "-r", "--name-only", base).split()
    assert files[expected_order[0]] not in base_tree
    assert files[expected_order[1]] in base_tree


@pytest.mark.skip(reason=_CANONICAL_HOST_CUTOVER_SKIP)
def test_forge_run_human_output_renders_order_and_summary(tmp_path: Path, monkeypatch) -> None:
    """The Rich path is a real code path: `--machine` discards it, so exercise it too."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _paths, tasks, files = _three_task_plan(repo)
    expected_order = [task["id"] for task in tasks]

    _forbid_worktrees(monkeypatch)
    _passing_verification(monkeypatch)

    agent_calls: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        task_id = expected_order[len(agent_calls)]
        agent_calls.append(task_id)
        target = root / files[task_id]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# generated by {task_id}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "run",
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
            "pytest -q",
            "--mode",
            "auto",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert "Execution order" in result.output
    assert "Executed: 3" in result.output
    assert "succeeded: 3" in result.output
    assert "failed: 0" in result.output
    assert "stopped: completed" in result.output
    for task_id in expected_order:
        assert task_id in result.output


def test_forge_swarm_parallel_one_delegates_to_the_sequential_engine(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    paths, tasks, files = _three_task_plan(repo)
    expected_order = [task["id"] for task in tasks]

    _forbid_worktrees(monkeypatch)
    _passing_verification(monkeypatch)

    agent_calls: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        index = len(agent_calls)
        assert index < len(expected_order), "the agent ran more times than there are tasks"
        task_id = expected_order[index]
        assert task_id in instruction, f"expected {task_id} at position {index}"
        agent_calls.append(task_id)
        target = root / files[task_id]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# generated by {task_id}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--parallel",
            "1",
            "--verify",
            "strict",
            "--verify-cmd",
            "pytest -q",
            "--mode",
            "auto",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert agent_calls == expected_order
    _assert_single_worktree(repo)
    # `run_swarm` is patched to explode by _forbid_worktrees, so reaching the end
    # proves the swarm engine never ran.
    assert not (paths.execution_dir / "swarm_summary.json").exists()
    assert (paths.execution_dir / "sequential_summary.json").exists()

    terminal = _events(result.output)[-1]
    assert terminal["event"] == "run_completed"
    # The terminal event still names the command the user typed.
    assert terminal["data"]["command"] == "forge.swarm"
    assert terminal["data"]["outcome"]["engine"] == "sequential"


def test_forge_swarm_parallel_one_keeps_swarm_when_a_swarm_only_flag_is_used(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _paths, tasks, _files = _three_task_plan(repo)

    def _no_agent(**_kwargs):
        raise AssertionError("--dry-run must not run the agent")

    monkeypatch.setattr(cli_mod, "run_agent", _no_agent)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "swarm",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--parallel",
            "1",
            "--dry-run",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    # --dry-run reports the swarm schedule, which the sequential engine cannot
    # produce, so the swarm engine has to stay.
    assert "forge swarm (dry-run)" in result.output
    assert "kept the swarm engine" in result.output
    assert tasks[0]["id"] in result.output


@pytest.mark.skip(reason=_CANONICAL_HOST_CUTOVER_SKIP)
def test_forge_run_degrades_to_no_pr_without_a_git_head(tmp_path: Path, monkeypatch) -> None:
    """A workspace with no git HEAD cannot do git flow; say so rather than crashing."""
    repo = tmp_path / "repo"
    repo.mkdir()  # deliberately not a git repository
    paths, tasks, files = _three_task_plan(repo)
    expected_order = [task["id"] for task in tasks]

    _forbid_worktrees(monkeypatch)

    agent_calls: list[str] = []

    def fake_run_agent(*, root: Path, instruction: str, **_kwargs) -> int:
        task_id = expected_order[len(agent_calls)]
        agent_calls.append(task_id)
        target = root / files[task_id]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"# generated by {task_id}\n", encoding="utf-8")
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "run",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--mode",
            "auto",
            "--yes",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    assert agent_calls == expected_order
    for rel_path in files.values():
        assert (repo / rel_path).exists(), rel_path

    events = _events(result.output)
    degraded = [
        event
        for event in events
        if event["event"] == "verification_unavailable" and event["data"].get("scope") == "run"
    ]
    assert len(degraded) == 1
    assert "no git HEAD" in degraded[0]["data"]["reason"]

    terminal = events[-1]
    assert terminal["event"] == "run_completed"
    assert terminal["data"]["outcome"]["git_flow"] is False


@pytest.mark.skip(reason=_CANONICAL_HOST_CUTOVER_SKIP)
def test_forge_run_rejects_explicit_pr_without_a_git_head(tmp_path: Path, monkeypatch) -> None:
    """An explicit flag is never silently ignored -- only an unstated default degrades."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _three_task_plan(repo)

    _forbid_worktrees(monkeypatch)
    monkeypatch.setattr(
        cli_mod,
        "run_agent",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "run",
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

    assert result.exit_code == 2, result.output
    terminal = _events(result.output)[-1]
    assert terminal["event"] == "error"
    assert "--pr needs a git repository" in terminal["data"]["message"]


@pytest.mark.skip(reason=_CANONICAL_HOST_CUTOVER_SKIP)
def test_forge_run_dry_run_reports_dependency_order_and_runs_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _paths, tasks, _files = _three_task_plan(repo)
    expected_order = [task["id"] for task in tasks]

    _forbid_worktrees(monkeypatch)

    def _no_agent(**_kwargs):
        raise AssertionError("--dry-run must not run the agent")

    monkeypatch.setattr(cli_mod, "run_agent", _no_agent)

    result = CliRunner().invoke(
        alysis_app,
        [
            "forge",
            "--machine",
            "run",
            "--path",
            os.fspath(repo),
            "--model",
            "test-model",
            "--api-key",
            "k",
            "--no-log",
            "--dry-run",
        ],
        env=_env(tmp_path),
    )

    assert result.exit_code == 0, result.output
    events = _events(result.output)
    terminal = events[-1]
    assert terminal["event"] == "run_completed"
    assert terminal["data"]["dry_run"] is True
    assert terminal["data"]["order"] == expected_order
    # Nothing was executed: no task files, no branches, still one commit.
    assert not (repo / "src").exists()
    assert _git(repo, "rev-list", "--count", "HEAD").strip() == "1"
    assert _git(repo, "branch", "--format=%(refname:short)").split() == [
        _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()
    ]
