"""CLI routing for `forge swarm --parallel 1`.

The default single-worker route uses sequential execution without worktrees.
Swarm-only flags retain the swarm engine. Both checks use real Git repositories.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

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
