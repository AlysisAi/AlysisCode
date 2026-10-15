from __future__ import annotations

import os
from pathlib import Path

from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code.cli import app as alysis_app
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.review_gate import ReviewOutcome


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }


def _prepare_run_with_task(repo: Path) -> tuple[str, Path]:
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Review test"
    plan["summary"] = "Review test"
    task = add_task(
        plan,
        title="Implement feature",
        description="Task created for review command coverage.",
        estimated_files=["src/feature.py"],
    )
    save_plan(paths, plan)
    return str(task["id"]), paths.run_dir


def test_review_command_exit_zero_when_approved(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, run_dir = _prepare_run_with_task(repo)

    def fake_review_task(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        json_path = run_dir / "execution" / "reviews" / f"{task_id}.json"
        md_path = run_dir / "execution" / "reviews" / f"{task_id}.md"
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text("{}", encoding="utf-8")
        md_path.write_text("# review\n", encoding="utf-8")
        return ReviewOutcome(
            task_id=task_id,
            approved=True,
            confidence="high",
            summary="ok",
            blocking_issues_count=0,
            non_blocking_issues_count=0,
            json_path=json_path,
            markdown_path=md_path,
        )

    monkeypatch.setattr(cli_mod, "review_task", fake_review_task)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "review",
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
    assert "Approved: yes" in result.output


def test_review_command_exit_zero_when_not_approved(tmp_path: Path, monkeypatch) -> None:
    """A completed review that rejects work is not a failed command."""
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    task_id, run_dir = _prepare_run_with_task(repo)

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
            summary="changes needed",
            blocking_issues_count=1,
            non_blocking_issues_count=0,
            json_path=json_path,
            markdown_path=md_path,
        )

    monkeypatch.setattr(cli_mod, "review_task", fake_review_task)
    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "review",
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
    assert "Approved: no" in result.output
