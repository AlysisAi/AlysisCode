from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from alysis_code import forge as forge_mod
from alysis_code import workspace_binding as workspace_binding_mod
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl import forge as forge_cli
from alysis_code.cli_impl.commands.forge_helpers import _workspace_context_payload_for_paths
from alysis_code.forge import (
    ForgeError,
    add_task,
    append_planner_chat,
    append_planner_summary,
    append_transcript_note,
    create_plan_run,
    finalize_plan,
    load_current_run_paths,
    load_plan,
    make_run_paths,
    render_plan_markdown,
    save_plan,
)


def test_make_run_paths_marks_greenfield_from_workspace_metadata(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    plain.mkdir()
    plain_paths = make_run_paths(root=plain, run_id="run_plain")
    assert plain_paths.greenfield is True

    mature = tmp_path / "mature"
    mature.mkdir()
    (mature / "README.md").write_text("existing project\n", encoding="utf-8")
    mature_paths = make_run_paths(
        root=mature,
        run_id="run_mature",
        workspace_kind="git_repo",
        has_head_commit=True,
    )
    assert mature_paths.greenfield is False


def test_workspace_context_payload_threads_greenfield_signal(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = make_run_paths(root=repo, run_id="run_greenfield")

    payload = _workspace_context_payload_for_paths(paths=paths)

    assert payload is not None
    assert payload["greenfield"] is True
    assert payload["workspace_kind"] == "plain_dir"


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": os.fspath(tmp_path / "cfg"),
        "ALYSIS_DATA_DIR": os.fspath(tmp_path / "data"),
    }


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", os.fspath(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _init_git_repo_with_commit(repo: Path) -> None:
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("repo\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def test_add_task_does_not_bypass_mutating_scope_readiness_policy() -> None:
    plan = {"tasks": []}

    with pytest.raises(ForgeError, match="lacks runnable file scope"):
        add_task(
            plan,
            title="Fix login bug",
            description="Task created from planning chat: Fix login bug",
        )

    assert plan["tasks"] == []


@pytest.mark.parametrize(
    "title",
    [
        "Improve login flow",
        "Build dashboard",
        "Migrate settings",
        "Configure timeout handling",
        "Enable dark mode",
        "Task A",
        "Do the work",
        "Make app better",
    ],
)
def test_add_task_rejects_scope_empty_task(title: str) -> None:
    plan = {"tasks": []}

    with pytest.raises(ForgeError, match="lacks runnable file scope"):
        add_task(
            plan,
            title=title,
            description=f"Manual planning chat task: {title}",
        )

    assert plan["tasks"] == []


@pytest.mark.parametrize(
    ("title", "expected_path"),
    [
        ("Improve src/auth.py", "src/auth.py"),
        ("Build packages/web/src/Dashboard.tsx", "packages/web/src/Dashboard.tsx"),
    ],
)
def test_explicit_path_hint_still_recovers_scope_for_likely_runnable_task(
    title: str,
    expected_path: str,
) -> None:
    plan = {"tasks": []}

    task = add_task(
        plan,
        title=title,
        description=f"Manual planning chat task: {title}",
    )

    assert task["estimated_files"] == [expected_path]
    assert task["write_scope"] == [expected_path]


def test_manual_task_infers_ordered_dependency_from_predecessor_cue() -> None:
    plan = {"tasks": []}

    first = add_task(
        plan,
        title="Update src/calc.py helper behavior",
        description="Manual planning chat task: Update src/calc.py helper behavior",
    )
    second = add_task(
        plan,
        title="Update src/app.py caller after helper behavior",
        description="Manual planning chat task: Update src/app.py caller after helper behavior",
    )

    assert second["dependencies"] == [first["id"]]


@pytest.mark.parametrize(
    ("title", "description", "acceptance"),
    [
        (
            "Analyze the auth flow",
            "Read-only analysis only; report findings.",
            ["Findings documented."],
        ),
        (
            "Investigate login bug",
            "Investigate and report findings without file changes.",
            ["Findings documented."],
        ),
        (
            "Review the current architecture",
            "Review and report only.",
            ["Summarize findings for the team."],
        ),
        (
            "Compare build options",
            "Report findings only.",
            ["Findings documented."],
        ),
        (
            "Review support options",
            "Read-only; report findings.",
            ["Findings documented."],
        ),
        (
            "Audit upgrade path",
            "No code changes; report findings.",
            ["Findings documented."],
        ),
        (
            "Summarize the issue",
            "No code changes; report findings.",
            ["Summary documented."],
        ),
    ],
)
def test_manual_task_allows_analysis_only_scope_free_entry(
    title: str,
    description: str,
    acceptance: list[str],
) -> None:
    plan = {"tasks": []}

    task = add_task(
        plan,
        title=title,
        description=description,
        acceptance_criteria=acceptance,
    )

    assert task["estimated_files"] == []
    assert task["write_scope"] == []
    assert task["analysis_only"] is True


def test_manual_task_rejects_report_only_text_with_mutating_action_clause() -> None:
    plan = {"tasks": []}

    with pytest.raises(ForgeError, match="lacks runnable file scope"):
        add_task(
            plan,
            title="Review login flow, implement token refresh fix",
            description="Report findings only.",
            acceptance_criteria=["Findings documented."],
        )

    assert plan["tasks"] == []


def test_render_plan_markdown_reports_superseded_work() -> None:
    plan = {
        "run_id": "run_1",
        "created_at": "2026-02-20T00:00:00+00:00",
        "updated_at": "2026-02-20T00:00:00+00:00",
        "project_goal": "Use env vars",
        "summary": "TOML work was superseded.",
        "requirements": ["Use APP_TIMEOUT_SECONDS"],
        "superseded_requirements": [
            {
                "text": "Support TOML config",
                "status": "superseded",
                "reason": "latest user direction changed",
            }
        ],
        "tasks": [
            {
                "id": "T01",
                "title": "Implement TOML settings loader",
                "description": "Obsolete branch.",
                "acceptance_criteria": [],
                "dependencies": [],
                "estimated_files": [],
                "write_scope": [],
                "status": "superseded",
                "attempts": 0,
            }
        ],
        "assets": [],
    }

    rendered = render_plan_markdown(plan)

    assert "## Superseded Requirements" in rendered
    assert "Support TOML config (latest user direction changed)" in rendered
    assert "Status counts: superseded: 1" in rendered


def test_finalize_plan_does_not_insert_synthetic_clarify_task() -> None:
    plan = {
        "schema_version": 1,
        "run_id": "run_1",
        "created_at": "2026-02-20T00:00:00+00:00",
        "updated_at": "2026-02-20T00:00:00+00:00",
        "project_goal": "",
        "summary": "",
        "requirements": [],
        "tasks": [],
        "assets": [],
    }

    finalized = finalize_plan(plan)

    assert finalized["tasks"] == []
    assert finalized["project_goal"] == "Define the project goal and implementation scope."
    assert finalized["summary"] == "Initial planning scaffold created."


def test_plan_semantic_fingerprint_ignores_updated_at_only() -> None:
    base_plan = {
        "schema_version": 1,
        "run_id": "run_1",
        "created_at": "2026-02-20T00:00:00+00:00",
        "updated_at": "2026-02-20T00:00:00+00:00",
        "project_goal": "Ship parser hardening",
        "summary": "Track parser work.",
        "requirements": ["Bound retries"],
        "tasks": [],
        "assets": [],
    }
    same_plan = dict(base_plan, updated_at="2026-02-21T00:00:00+00:00")
    changed_plan = dict(base_plan, project_goal="Ship parser rewrite")

    assert forge_mod._plan_semantic_fingerprint(base_plan) == forge_mod._plan_semantic_fingerprint(
        same_plan
    )
    assert forge_mod._plan_semantic_fingerprint(base_plan) != forge_mod._plan_semantic_fingerprint(
        changed_plan
    )


def test_save_plan_is_idempotent_when_only_updated_at_would_change(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    add_task(plan, title="Task A", estimated_files=["src/a.py"], branch="feat/t01-a")
    save_plan(paths, plan)

    plan_json_before = paths.plan_json_path.read_text(encoding="utf-8")
    plan_md_before = paths.plan_md_path.read_text(encoding="utf-8")
    persisted_updated_at = json.loads(plan_json_before)["updated_at"]

    monkeypatch.setattr(forge_mod, "now_iso", lambda: "2035-01-01T00:00:00+00:00")

    save_plan(paths, plan)

    assert plan["updated_at"] == persisted_updated_at
    assert paths.plan_json_path.read_text(encoding="utf-8") == plan_json_before
    assert paths.plan_md_path.read_text(encoding="utf-8") == plan_md_before


def test_forge_render_planner_reply_uses_monochrome_labels() -> None:
    console_file = io.StringIO()
    console = Console(file=console_file, force_terminal=False, color_system=None)

    forge_cli._render_planner_reply(
        console=console,
        message="Draft ready.",
        questions=["Need auth?", "Need tests?"],
    )

    rendered = console_file.getvalue()
    assert "Planner:" in rendered
    assert "Planner questions" in rendered
    assert "Draft ready." in rendered
    assert "Need auth?" in rendered
    assert "Need tests?" in rendered


def test_forge_attach_copies_and_updates_plan_json(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    save_plan(paths, plan)

    source = repo / "brief.txt"
    source.write_text("This is a small text asset.\n", encoding="utf-8")

    attach_1 = runner.invoke(
        alysis_app,
        ["forge", "attach", os.fspath(source), "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert attach_1.exit_code == 0

    attach_2 = runner.invoke(
        alysis_app,
        ["forge", "attach", os.fspath(source), "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert attach_2.exit_code == 0

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    plan_json_path = repo / pointer["run_path"] / "plan" / "plan.json"
    plan = _load_json(plan_json_path)

    assets = plan["assets"]
    assert len(assets) == 2
    assert assets[0]["original_path"] == os.fspath(source.resolve())
    assert assets[0]["stored_path"] != assets[1]["stored_path"]

    first_stored = repo / assets[0]["stored_path"]
    second_stored = repo / assets[1]["stored_path"]
    assert first_stored.exists()
    assert second_stored.exists()

    assert "text_copy_path" in assets[0]
    assert (repo / assets[0]["text_copy_path"]).exists()


def test_load_current_run_paths_loads_current_pointer_from_nested_path(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    project_root = repo / "project"
    nested = project_root / "nested"
    nested.mkdir(parents=True)

    paths = make_run_paths(root=project_root, run_id="current_run")
    paths.run_dir.mkdir(parents=True, exist_ok=True)
    pointer = {
        "run_id": paths.run_id,
        "run_path": os.fspath(Path(".alysis") / "runs" / paths.run_id),
        "updated_at": "2026-03-07T00:00:00+00:00",
    }
    (project_root / ".alysis").mkdir(parents=True, exist_ok=True)
    (project_root / ".alysis" / "current_run.json").write_text(
        json.dumps(pointer, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    loaded = load_current_run_paths(nested)

    assert loaded.root == project_root.resolve()
    assert loaded.run_id == "current_run"
    assert loaded.focus_relpath == "."
    assert loaded.focus_path == project_root.resolve()
    assert loaded.git_root == repo.resolve()
    assert loaded.workspace_kind == "git_repo"
    assert loaded.binding_requested_path == project_root.resolve()
    assert loaded.binding_source == "current_run_pointer"
    assert loaded.binding_risk_level == "healthy"


def test_create_plan_run_bootstraps_missing_directory_when_requested(tmp_path: Path) -> None:
    missing = tmp_path / "new" / "repo"

    paths = create_plan_run(missing, create_if_missing=True)

    assert missing.exists()
    assert paths.root == missing.resolve()
    assert paths.binding_requested_path == missing.resolve()
    assert paths.workspace_created_at_startup is True

    pointer = _load_json(missing / ".alysis" / "current_run.json")
    assert pointer["binding_requested_path"] == os.fspath(missing.resolve())
    assert pointer["workspace_created_at_startup"] is True
    assert pointer["binding_source"] == "explicit_path"
    assert pointer["binding_risk_level"] == "healthy"


def test_create_plan_run_rejects_guarded_workspace_without_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    with pytest.raises(ForgeError, match="allow_broad_workspace=True"):
        create_plan_run(home)

    paths = create_plan_run(home, allow_broad_workspace=True)
    pointer = _load_json(home / ".alysis" / "current_run.json")

    assert paths.binding_risk_level == "guarded"
    assert pointer["binding_risk_level"] == "guarded"
    assert "home directory" in pointer["binding_risk_reasons"][0]
    assert pointer["binding_broad_workspace_override_used"] is True


def test_forge_show_prints_summary(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    paths = create_plan_run(repo)
    plan = load_plan(paths)
    plan["project_goal"] = "Deliver plan phase"
    add_task(
        plan,
        title="Create docs/initial.md artifact",
        estimated_files=["docs/initial.md"],
    )
    save_plan(paths, plan)

    source = repo / "requirements.txt"
    source.write_text("Requirement A\n", encoding="utf-8")
    attach_result = runner.invoke(
        alysis_app,
        ["forge", "attach", os.fspath(source), "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert attach_result.exit_code == 0

    show_result = runner.invoke(
        alysis_app,
        ["forge", "show", "--path", os.fspath(repo)],
        env=_env(tmp_path),
    )
    assert show_result.exit_code == 0
    assert "Project goal: Deliver plan phase" in show_result.output
    assert "Create docs/initial.md artifact" in show_result.output
    assert "Assets" in show_result.output
    assert "requirements" in show_result.output


def test_forge_show_loads_current_run_from_git_subdir(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    subdir = repo / "pkg"
    subdir.mkdir()

    paths = create_plan_run(subdir)
    plan = load_plan(paths)
    plan["project_goal"] = "Show from subdir"
    add_task(
        plan,
        title="Keep pointer canonical",
        estimated_files=["README.md"],
    )
    save_plan(paths, plan)

    show_result = runner.invoke(
        alysis_app,
        ["forge", "show", "--path", os.fspath(subdir)],
        env=_env(tmp_path),
    )
    assert show_result.exit_code == 0
    assert "Project goal: Show from subdir" in show_result.output
    assert "Keep pointer canonical" in show_result.output


def test_plan_note_writes_handle_invalid_surrogates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = make_run_paths(root=repo, run_id="run_surrogate")

    append_transcript_note(paths, role="user", message="hello\udcceworld")
    append_planner_chat(paths, role="assistant", message="reply\udcffdone")
    append_planner_summary(paths, "summary\udce2line")

    notes_text = paths.notes_path.read_text(encoding="utf-8")
    chat_text = paths.planner_chat_path.read_text(encoding="utf-8")
    summary_text = paths.planner_summary_path.read_text(encoding="utf-8")

    assert "hello?world" in notes_text
    assert "reply?done" in chat_text
    assert "summary?line" in summary_text
