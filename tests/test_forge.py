from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from rich.console import Console
from typer.testing import CliRunner

from alysis_code import cli as cli_mod
from alysis_code import forge as forge_mod
from alysis_code import workspace_binding as workspace_binding_mod
from alysis_code.cli import app as alysis_app
from alysis_code.cli_impl import forge as forge_cli
from alysis_code.cli_impl.commands.forge_helpers import _workspace_context_payload_for_paths
from alysis_code.config import AppConfig
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
from alysis_code.llm.openai_compat import LLMError
from alysis_code.plan_assistant import PlannerTurnResult

_MCP_FIXTURE_SERVER = (
    Path(__file__).resolve().parent / "fixtures" / "mcp_servers" / "minimal_stdio_server.py"
)
_RETIRED_LEGACY_PLANNER_REASON = (
    "Retired legacy slash-command planner and public assistant flow; "
    "canonical planning is covered by the Forge Host contract suites."
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


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


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


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_creates_run_and_pointer(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=(
            "/goal Build a local planning workflow\n"
            "/task Bootstrap docs/run-workspace.md\n"
            "Need safe run state\n"
            "/done\n"
        ),
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    pointer = repo / ".alysis" / "current_run.json"
    assert pointer.exists()
    pointer_data = _load_json(pointer)
    assert pointer_data["run_id"]
    assert not Path(pointer_data["run_path"]).is_absolute()
    assert pointer_data["binding_requested_path"] == os.fspath(repo.resolve())
    assert pointer_data["binding_source"] == "explicit_path"
    assert pointer_data["workspace_created_at_startup"] is False
    assert pointer_data["binding_risk_level"] == "healthy"
    assert pointer_data["binding_risk_reasons"] == []
    assert pointer_data["binding_broad_workspace_override_used"] is False

    run_dir = repo / pointer_data["run_path"]
    plan_dir = run_dir / "plan"
    assert run_dir.exists()
    assert (plan_dir / "PLAN.md").exists()
    assert (plan_dir / "plan.json").exists()
    assert (plan_dir / "DECISIONS.md").exists()
    assert (plan_dir / "RISKS.md").exists()
    assert (plan_dir / "assets").exists()
    assert (plan_dir / "context" / "workspace_context.json").exists()
    assert (plan_dir / "context" / "workspace_summary.md").exists()
    assert (plan_dir / "notes" / "user_notes.md").exists()
    assert (plan_dir / "notes" / "plan_validation.md").exists()

    plan = _load_json(plan_dir / "plan.json")
    workspace_context = _load_json(plan_dir / "context" / "workspace_context.json")
    assert plan["project_goal"] == "Build a local planning workflow"
    assert workspace_context["workspace_root"] == os.fspath(repo.resolve())
    assert workspace_context["focus_relpath"] == "."
    assert len(plan["tasks"]) >= 1
    task = plan["tasks"][0]
    for key in [
        "id",
        "title",
        "description",
        "acceptance_criteria",
        "dependencies",
        "estimated_files",
        "write_scope",
        "branch",
        "status",
    ]:
        assert key in task


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
    ],
)
def test_add_task_rejects_likely_runnable_scope_empty_task(title: str) -> None:
    plan = {"tasks": []}

    with pytest.raises(ForgeError, match="lacks runnable file scope"):
        add_task(
            plan,
            title=title,
            description=f"Manual planning chat task: {title}",
        )

    assert plan["tasks"] == []


@pytest.mark.parametrize("title", ["Task A", "Do the work", "Make app better"])
def test_add_task_rejects_ambiguous_scope_empty_task(title: str) -> None:
    plan = {"tasks": []}

    with pytest.raises(ForgeError, match="lacks runnable file scope"):
        add_task(
            plan,
            title=title,
            description=f"Manual planning chat task: {title}",
        )

    assert plan["tasks"] == []


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_manual_task_recovers_explicit_path_hint_into_estimated_files_and_write_scope(
    tmp_path: Path,
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input="/goal Auth fix\n/task Fix src/auth.py login bug\n/done\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    plan = _load_json(repo / pointer["run_path"] / "plan" / "plan.json")
    task = plan["tasks"][0]
    assert task["estimated_files"] == ["src/auth.py"]
    assert task["write_scope"] == ["src/auth.py"]


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


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_manual_task_rejects_mutating_title_without_runnable_scope(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input="/goal Auth fix\n/task Fix login bug\n/done\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Task rejected" in result.output
    assert "repo-relative file paths" in result.output
    pointer = _load_json(repo / ".alysis" / "current_run.json")
    plan = _load_json(repo / pointer["run_path"] / "plan" / "plan.json")
    assert plan["tasks"] == []


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_does_not_launch_mcp_servers_for_planner_context(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    request_log = tmp_path / "mcp-requests.jsonl"
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
            "record_client_requests_path": os.fspath(request_log),
        },
        server_overrides={
            "allowed_tools": ["echo"],
            "resources_mode": "listed_read_only",
        },
    )

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input="/done\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    if request_log.exists():
        assert request_log.read_text(encoding="utf-8").strip() == ""


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


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_from_git_subdir_writes_workspace_root_pointer(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    _init_git_repo_with_commit(repo)
    subdir = repo / "src" / "feature"
    subdir.mkdir(parents=True)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(subdir)],
        input="/goal Plan from subdir\n/done\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    pointer_path = repo / ".alysis" / "current_run.json"
    assert pointer_path.exists()
    assert not (subdir / ".alysis" / "current_run.json").exists()

    pointer = _load_json(pointer_path)
    assert pointer["workspace_root"] == os.fspath(repo.resolve())
    assert pointer["focus_path"] == os.fspath(subdir.resolve())
    assert pointer["focus_relpath"] == "src/feature"
    assert pointer["workspace_kind"] == "git_repo"
    assert pointer["binding_requested_path"] == os.fspath(subdir.resolve())
    assert pointer["binding_source"] == "explicit_path"
    assert pointer["binding_risk_level"] == "healthy"

    run_dir = repo / pointer["run_path"]
    assert run_dir.exists()
    workspace_context = _load_json(run_dir / "plan" / "context" / "workspace_context.json")
    assert workspace_context["focus_relpath"] == "src/feature"
    assert workspace_context["workspace_root"] == os.fspath(repo.resolve())

    paths = load_current_run_paths(subdir)
    assert paths.root == repo.resolve()
    assert paths.run_id == pointer["run_id"]
    assert paths.focus_path == subdir.resolve()
    assert paths.focus_relpath == "src/feature"


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


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_cli_bootstraps_missing_directory(tmp_path: Path) -> None:
    runner = CliRunner()
    missing = tmp_path / "new" / "repo"

    result = runner.invoke(
        alysis_app,
        [
            "forge",
            "plan",
            "--path",
            os.fspath(missing),
            "--create-path",
        ],
        input="/done\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert missing.exists()
    pointer = _load_json(missing / ".alysis" / "current_run.json")
    assert pointer["workspace_created_at_startup"] is True


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_cli_guarded_workspace_requires_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(workspace_binding_mod, "_home_directory", lambda: home.resolve())

    blocked = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(home)],
        input="/done\n",
        env=_env(tmp_path),
    )
    assert blocked.exit_code == 2
    assert "--allow-broad-workspace" in blocked.output

    allowed = runner.invoke(
        alysis_app,
        [
            "forge",
            "plan",
            "--path",
            os.fspath(home),
            "--allow-broad-workspace",
        ],
        input="/done\n",
        env=_env(tmp_path),
    )
    assert allowed.exit_code == 0


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_cli_rejects_blocked_workspace(tmp_path: Path) -> None:
    runner = CliRunner()

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(Path(os.path.abspath(os.sep)))],
        input="/done\n",
        env=_env(tmp_path),
    )

    assert result.exit_code == 2
    assert "filesystem root '/'" in result.output


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


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_updates_plan_and_notes(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    captured: dict[str, object] = {}

    def fake_planner_turn(**kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        return PlannerTurnResult(
            assistant_message="I will add a task and requirement.",
            questions=[],
            plan_update={
                "requirements_append": ["Need assistant-backed planning updates"],
                "tasks_add": [
                    {
                        "title": "Wire planner turn",
                        "description": "Integrate planner call in loop",
                        "acceptance_criteria": ["Assistant can be toggled"],
                        "dependencies": [],
                        "estimated_files": ["src/alysis_code/cli.py"],
                        "write_scope": [".alysis/runs"],
                    }
                ],
            },
        )

    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease update the plan\n/done\n"),
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "Planner assistant: ON" in result.output
    assert "Planner:" in result.output
    assert "I will add a task and requirement." in result.output
    assert "Applied planner update to plan." in result.output

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    plan_dir = repo / pointer["run_path"] / "plan"
    plan = _load_json(plan_dir / "plan.json")
    assert plan["requirements"] == ["Need assistant-backed planning updates"]
    assert len(plan["tasks"]) == 1
    assert plan["tasks"][0]["title"] == "Wire planner turn"

    plan_md = (plan_dir / "PLAN.md").read_text(encoding="utf-8")
    assert "Wire planner turn" in plan_md

    planner_chat = (plan_dir / "notes" / "planner_chat.md").read_text(encoding="utf-8")
    assert "Please update the plan" in planner_chat
    assert "I will add a task and requirement." in planner_chat

    planner_summary = (plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "added requirements: 1" in planner_summary
    assert "added tasks: T01" in planner_summary

    plan_validation = (plan_dir / "notes" / "plan_validation.md").read_text(encoding="utf-8")
    assert "No validation warnings." in plan_validation
    workspace_context = captured.get("workspace_context")
    assert isinstance(workspace_context, dict)
    assert workspace_context.get("focus_relpath") == "."


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_planner_turn_policy_matches_chat_forge(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    cli_repo = tmp_path / "cli-repo"
    cli_repo.mkdir()
    chat_repo = tmp_path / "chat-repo"
    chat_repo.mkdir()
    chat_paths = create_plan_run(chat_repo)

    class _DummyStore:
        session_id = "sid"
        enabled = False

    class _DummyClient:
        model = "test-model"
        temperature = 1.0
        api_key = "k"

    class _DummySession:
        store = _DummyStore()
        client = _DummyClient()
        stream = False
        mode = "review"
        cfg = AppConfig(model="session-model", base_url="https://example.test/v1")

        @staticmethod
        def run_turn(_instruction: str, *, image_paths: list[str] | None = None) -> int:
            _ = image_paths
            return 0

        @staticmethod
        def close() -> None:
            return None

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="I will add the same task in both Forge entrypoints.",
            questions=[],
            plan_update={
                "requirements_append": ["Need planner parity coverage"],
                "tasks_add": [
                    {
                        "title": "Keep planner controller behavior aligned",
                        "description": "Use the same apply/save/reconcile flow in chat and CLI Forge.",
                        "acceptance_criteria": ["Both entrypoints apply the same update"],
                        "dependencies": [],
                        "estimated_files": ["src/alysis_code/cli.py"],
                        "write_scope": ["src/alysis_code/cli.py"],
                    }
                ],
            },
        )

    def fake_enter_forge_mode(*, root: Path, console: Console, forge_state, **_kwargs) -> bool:
        _ = root, console
        forge_state.ui_mode = "forge"
        forge_state.paths = chat_paths
        forge_state.plan = load_plan(chat_paths)
        return True

    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)
    monkeypatch.setattr(
        cli_mod,
        "load_config",
        lambda: AppConfig(model="planner-model", base_url="https://example.test/v1"),
    )
    monkeypatch.setattr(cli_mod, "create_session", lambda **_kwargs: _DummySession())
    monkeypatch.setattr(cli_mod, "_enter_forge_mode", fake_enter_forge_mode)

    cli_result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(cli_repo)],
        input=("/assistant on\nPlease update the plan\n/done\n"),
        env=_env(tmp_path),
    )
    chat_result = runner.invoke(
        alysis_app,
        ["chat", "--model", "test-model", "--api-key", "k", "--no-log"],
        input="/forge\n/assistant on\nPlease update the plan\n/back\nexit\n",
        env=_env(tmp_path),
    )

    assert cli_result.exit_code == 0
    assert chat_result.exit_code == 0

    cli_pointer = _load_json(cli_repo / ".alysis" / "current_run.json")
    cli_plan_dir = cli_repo / cli_pointer["run_path"] / "plan"
    cli_plan = _load_json(cli_plan_dir / "plan.json")
    chat_plan = _load_json(chat_paths.plan_json_path)

    assert cli_plan["requirements"] == chat_plan["requirements"] == ["Need planner parity coverage"]
    assert cli_plan["tasks"] == chat_plan["tasks"]
    cli_summary = (cli_plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    chat_summary = chat_paths.planner_summary_path.read_text(encoding="utf-8")
    cli_summary_line = [line for line in cli_summary.splitlines() if line.startswith("- [")][-1]
    chat_summary_line = [line for line in chat_summary.splitlines() if line.startswith("- [")][-1]
    assert cli_summary_line.split("] ", 1)[1] == chat_summary_line.split("] ", 1)[1]
    assert "Planner update: added requirements: 1" in (
        cli_plan_dir / "notes" / "user_notes.md"
    ).read_text(encoding="utf-8")
    assert "Planner update: added requirements: 1" in chat_paths.notes_path.read_text(
        encoding="utf-8"
    )


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_recovers_after_transient_request_retry(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    calls = {"planner": 0}

    class FakePlannerClient:
        def __init__(self, **_kwargs) -> None:  # type: ignore[no-untyped-def]
            pass

        def chat(self, **kwargs):  # type: ignore[no-untyped-def]
            calls["planner"] += 1
            if calls["planner"] == 1:
                raise LLMError("LLM request failed: ReadTimeout")
            payload = {
                "assistant_message": "Recovered planner response.",
                "questions": [],
                "plan_update": {
                    "tasks_add": [
                        {
                            "title": "Add recovered planner task",
                            "description": "Apply the recovered planner update once.",
                            "acceptance_criteria": ["Task exists once"],
                            "dependencies": [],
                            "estimated_files": ["src/example.py"],
                            "write_scope": ["src/example.py"],
                        }
                    ]
                },
            }
            return type("Resp", (), {"content": json.dumps(payload)})()

    monkeypatch.setattr(
        "alysis_code.plan_assistant.OpenAICompatClient",
        FakePlannerClient,
    )
    monkeypatch.setattr(
        cli_mod,
        "load_config",
        lambda: AppConfig(model="planner-model", base_url="https://example.com/v1"),
    )

    env = _env(tmp_path)
    env["ALYSIS_API_KEY"] = "k"
    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease update the plan\n/done\n"),
        env=env,
    )

    assert result.exit_code == 0
    assert "Recovered planner response." in result.output
    assert "Applied planner update to plan." in result.output
    assert "Planner request recovered after 1 transient retry." in result.output
    assert calls == {"planner": 3}

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    plan_dir = repo / pointer["run_path"] / "plan"
    plan = _load_json(plan_dir / "plan.json")
    assert len(plan["tasks"]) == 1
    assert plan["tasks"][0]["title"] == "Add recovered planner task"
    planner_summary = (plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert planner_summary.count("added tasks: T01") == 1
    notes_text = (plan_dir / "notes" / "user_notes.md").read_text(encoding="utf-8")
    assert (
        notes_text.count("Planner warning: Planner request recovered after 1 transient retry.") == 1
    )


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_surfaces_planner_error_with_retry_context(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="Planner assistant returned no safe structured update.",
            questions=[],
            plan_update=None,
            error="empty_response",
            request_retry_count=1,
        )

    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease update the plan\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Planner assistant returned no safe structured update." in result.output
    assert "Planner: Planner request failed after 1 transient retry." in result.output
    assert "Planner: Final planner error: empty_response" in result.output
    assert "Planner proposed no plan update." not in result.output

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    plan_dir = repo / pointer["run_path"] / "plan"
    plan = _load_json(plan_dir / "plan.json")
    assert plan["tasks"] == []
    assert plan["requirements"] == []

    planner_summary = (plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "planner error after 1 transient retry: empty_response" in planner_summary
    assert "no plan_update proposed" not in planner_summary

    notes_text = (plan_dir / "notes" / "user_notes.md").read_text(encoding="utf-8")
    assert "Planner error after 1 transient retry: empty_response" in notes_text
    assert "Planner proposed no plan update." not in notes_text


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_keeps_protected_history_immutable(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship login",
        description="Released in src/login.py.",
        estimated_files=[],
    )
    completed["status"] = "done"
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="I will add a follow-up task without rewriting shipped history.",
            questions=[],
            plan_update={
                "tasks_update": [{"id": str(completed["id"]), "title": "Rewrite shipped task"}],
                "tasks_add": [
                    {
                        "title": "Add login follow-up",
                        "description": "Track the post-release adjustment as a new task.",
                        "acceptance_criteria": ["Follow-up task exists"],
                        "dependencies": [str(completed["id"])],
                        "estimated_files": ["src/login.py", "tests/test_login.py"],
                        "write_scope": ["src/login.py", "tests/test_login.py"],
                    }
                ],
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a login follow-up task\n/done\n"),
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    assert "protected non-planned task history" in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert updated_plan["tasks"][0]["title"] == "Ship login"
    assert updated_plan["tasks"][0]["status"] == "done"
    assert updated_plan["tasks"][0]["estimated_files"] == ["src/login.py"]
    assert updated_plan["tasks"][0]["write_scope"] == ["src/login.py"]
    assert updated_plan["tasks"][1]["title"] == "Add login follow-up"


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_synthesizes_follow_up_from_protected_update_only(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship src/slugify.js",
        description="Released in src/slugify.js.",
        estimated_files=["src/slugify.js"],
    )
    completed["status"] = "done"
    completed["acceptance_criteria"] = ["Slugify shipped"]
    completed["write_scope"] = ["src/slugify.js"]
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="I will track the slugify follow-up separately.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(completed["id"]),
                        "title": "Add slugify follow-up tests",
                        "description": "Track post-release slugify tests separately.",
                        "acceptance_criteria": ["Slugify follow-up tests exist"],
                        "dependencies": [str(completed["id"])],
                        "estimated_files": ["test/slugify.test.js"],
                        "write_scope": ["test/slugify.test.js"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a slugify follow-up task\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    assert "protected non-planned task history" in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert updated_plan["tasks"][0]["title"] == "Ship src/slugify.js"
    assert updated_plan["tasks"][0]["status"] == "done"
    assert updated_plan["tasks"][1]["id"] == "T02"
    assert updated_plan["tasks"][1]["status"] == "planned"
    assert updated_plan["tasks"][1]["title"] == "Add slugify follow-up tests"
    notes_text = (paths.plan_dir / "notes" / "user_notes.md").read_text(encoding="utf-8")
    assert "Planner update:" in notes_text
    assert "synthesized follow-up tasks: T02" in notes_text

    notes_text = paths.notes_path.read_text(encoding="utf-8")
    assert "Planner warning:" in notes_text
    assert "follow-up work as new tasks instead" in notes_text


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_synthesizes_same_file_follow_up_from_protected_update(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship slugify",
        description="Released in src/slugify.js.",
        estimated_files=["src/slugify.js"],
    )
    completed["status"] = "done"
    completed["acceptance_criteria"] = ["Slugify shipped"]
    completed["write_scope"] = ["src/slugify.js"]
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="I will track the lowercase option as same-file follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(completed["id"]),
                        "title": "Add lowercase option follow-up",
                        "description": "Update src/slugify.js to add lowercase: bool = True behavior.",
                        "acceptance_criteria": ["src/slugify.js supports lowercase option"],
                        "dependencies": [str(completed["id"])],
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a slugify lowercase follow-up task\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    assert "protected non-planned task history" in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert updated_plan["tasks"][0]["title"] == "Ship slugify"
    assert updated_plan["tasks"][1]["title"] == "Add lowercase option follow-up"
    assert updated_plan["tasks"][1]["estimated_files"] == ["src/slugify.js"]
    assert updated_plan["tasks"][1]["write_scope"] == ["src/slugify.js"]
    notes_text = (paths.plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in notes_text
    assert "missing new runnable delta beyond protected history" not in notes_text


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_synthesizes_title_only_same_file_follow_up(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship slugify",
        description="Released in src/slugify.js.",
        estimated_files=["src/slugify.js"],
    )
    completed["status"] = "done"
    completed["acceptance_criteria"] = ["Slugify shipped"]
    completed["write_scope"] = ["src/slugify.js"]
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="I will track the lowercase option as same-file follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(completed["id"]),
                        "title": "Add lowercase option follow-up",
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a slugify lowercase follow-up task\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert updated_plan["tasks"][1]["title"] == "Add lowercase option follow-up"
    assert updated_plan["tasks"][1]["estimated_files"] == ["src/slugify.js"]
    assert updated_plan["tasks"][1]["write_scope"] == ["src/slugify.js"]
    notes_text = (paths.plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in notes_text
    assert "missing new runnable delta beyond protected history" not in notes_text


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_synthesizes_title_only_new_path_follow_up(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship slugify",
        description="Released in src/slugify.js.",
        estimated_files=["src/slugify.js"],
    )
    completed["status"] = "done"
    completed["acceptance_criteria"] = ["Slugify shipped"]
    completed["write_scope"] = ["src/slugify.js"]
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="I will track the docs follow-up as new work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(completed["id"]),
                        "title": "Add docs follow-up",
                        "estimated_files": ["docs/slugify.md"],
                        "write_scope": ["docs/slugify.md"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a slugify docs follow-up task\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert updated_plan["tasks"][1]["title"] == "Add docs follow-up"
    assert updated_plan["tasks"][1]["estimated_files"] == ["docs/slugify.md"]
    assert updated_plan["tasks"][1]["write_scope"] == ["docs/slugify.md"]
    notes_text = (paths.plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in notes_text
    assert "missing new runnable delta beyond protected history" not in notes_text


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_refuses_weak_generic_title_with_scope(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship slugify",
        description="Released in src/slugify.js.",
        estimated_files=["src/slugify.js"],
    )
    completed["status"] = "done"
    completed["acceptance_criteria"] = ["Slugify shipped"]
    completed["write_scope"] = ["src/slugify.js"]
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="That protected update does not describe runnable new follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(completed["id"]),
                        "title": "Refactor src/slugify.js",
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a slugify follow-up task\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Planner update contained no applicable changes." in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert len(updated_plan["tasks"]) == 1
    planner_summary = (paths.plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "synthesized follow-up tasks" not in planner_summary
    assert "protected history preserved" in planner_summary
    assert (
        "synthesis refused: T01 missing new runnable delta beyond protected history"
        in planner_summary
    )


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_synthesizes_same_file_follow_up_from_description_delta(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship slugify",
        description="Released in src/slugify.js.",
        estimated_files=["src/slugify.js"],
    )
    completed["status"] = "done"
    completed["acceptance_criteria"] = ["Slugify shipped"]
    completed["write_scope"] = ["src/slugify.js"]
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="I will track the lowercase option as same-file follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(completed["id"]),
                        "description": "Add lowercase option behavior with default True.",
                        "dependencies": [str(completed["id"])],
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a slugify lowercase follow-up task\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Applied planner update to plan." in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert updated_plan["tasks"][1]["title"] == "Ship slugify follow-up"
    assert (
        updated_plan["tasks"][1]["description"]
        == "Add lowercase option behavior with default True."
    )
    assert updated_plan["tasks"][1]["estimated_files"] == ["src/slugify.js"]
    assert updated_plan["tasks"][1]["write_scope"] == ["src/slugify.js"]
    notes_text = (paths.plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "synthesized follow-up tasks: T02" in notes_text
    assert "missing new runnable delta beyond protected history" not in notes_text


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_refuses_trivial_protected_update_without_new_path_signal(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship src/slugify.js",
        description="Released in src/slugify.js.",
        estimated_files=["src/slugify.js"],
    )
    completed["status"] = "done"
    completed["acceptance_criteria"] = ["Slugify shipped"]
    completed["write_scope"] = ["src/slugify.js"]
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="That protected update does not describe runnable new follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(completed["id"]),
                        "title": "Ship src/slugify.js",
                        "description": "Minor wording tweak only.",
                        "estimated_files": ["src/slugify.js"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a slugify follow-up task\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Planner update contained no applicable changes." in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert len(updated_plan["tasks"]) == 1
    assert updated_plan["tasks"][0]["title"] == "Ship src/slugify.js"
    notes_text = (paths.plan_dir / "notes" / "user_notes.md").read_text(encoding="utf-8")
    assert "protected non-planned task history" in notes_text
    assert (
        "could not be synthesized into runnable follow-up work: missing new runnable delta beyond protected history"
        in notes_text
    )
    planner_summary = (paths.plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "synthesized follow-up tasks" not in planner_summary
    assert "protected history preserved" in planner_summary
    assert (
        "synthesis refused: T01 missing new runnable delta beyond protected history"
        in planner_summary
    )


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_refuses_punctuation_only_same_file_delta(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship slugify",
        description="Released in src/slugify.js.",
        estimated_files=["src/slugify.js"],
    )
    completed["status"] = "done"
    completed["acceptance_criteria"] = ["Slugify shipped"]
    completed["write_scope"] = ["src/slugify.js"]
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="That protected update does not describe runnable new follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(completed["id"]),
                        "description": "Released in src/slugify.js!",
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a slugify follow-up task\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Planner update contained no applicable changes." in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert len(updated_plan["tasks"]) == 1
    planner_summary = (paths.plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "synthesized follow-up tasks" not in planner_summary
    assert "protected history preserved" in planner_summary
    assert (
        "synthesis refused: T01 missing new runnable delta beyond protected history"
        in planner_summary
    )


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_refuses_formatting_only_same_file_delta(
    tmp_path: Path, monkeypatch
) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    completed = add_task(
        plan,
        title="Ship slugify",
        description="Released in src/slugify.js.",
        estimated_files=["src/slugify.js"],
    )
    completed["status"] = "done"
    completed["acceptance_criteria"] = ["Slugify shipped"]
    completed["write_scope"] = ["src/slugify.js"]
    save_plan(paths, plan)

    def fake_create_plan_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        return paths

    def fake_planner_turn(**_kwargs) -> PlannerTurnResult:  # type: ignore[no-untyped-def]
        return PlannerTurnResult(
            assistant_message="That protected update does not describe runnable new follow-up work.",
            questions=[],
            plan_update={
                "tasks_update": [
                    {
                        "id": str(completed["id"]),
                        "description": "Formatting only in src/slugify.js.",
                        "estimated_files": ["src/slugify.js"],
                        "write_scope": ["src/slugify.js"],
                    }
                ]
            },
        )

    monkeypatch.setattr(cli_mod, "create_plan_run", fake_create_plan_run)
    monkeypatch.setattr(cli_mod, "run_planner_turn", fake_planner_turn)

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant on\nPlease add a slugify follow-up task\n/done\n"),
        env=_env(tmp_path),
    )

    assert result.exit_code == 0
    assert "Planner update contained no applicable changes." in result.output
    updated_plan = _load_json(paths.plan_json_path)
    assert len(updated_plan["tasks"]) == 1
    planner_summary = (paths.plan_dir / "notes" / "planner_summary.md").read_text(encoding="utf-8")
    assert "synthesized follow-up tasks" not in planner_summary
    assert "protected history preserved" in planner_summary
    assert (
        "synthesis refused: T01 missing new runnable delta beyond protected history"
        in planner_summary
    )


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_finalization_applies_reconciliation_additively(tmp_path: Path) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("project docs\n", encoding="utf-8")

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input="/task Update README.md for release notes\n/done\n",
        env=_env(tmp_path),
    )
    assert result.exit_code == 0

    pointer = _load_json(repo / ".alysis" / "current_run.json")
    plan_dir = repo / pointer["run_path"] / "plan"
    plan = _load_json(plan_dir / "plan.json")
    task = plan["tasks"][0]
    assert task["estimated_files"] == ["README.md"]
    assert task["write_scope"] == ["README.md"]

    plan_validation = (plan_dir / "notes" / "plan_validation.md").read_text(encoding="utf-8")
    assert "## Reconciliation" in plan_validation
    assert "Updated Tasks: (none)" in plan_validation
    assert "missing estimated_files" not in plan_validation


@pytest.mark.skip(reason=_RETIRED_LEGACY_PLANNER_REASON)
def test_forge_plan_assistant_without_arg_uses_picker(tmp_path: Path, monkeypatch) -> None:
    runner = CliRunner()
    repo = tmp_path / "repo"
    repo.mkdir()

    monkeypatch.setattr(
        cli_mod,
        "_select_forge_assistant_interactive",
        lambda **_kwargs: ("on", True),
    )

    result = runner.invoke(
        alysis_app,
        ["forge", "plan", "--path", os.fspath(repo)],
        input=("/assistant\n/done\n"),
        env=_env(tmp_path),
    )
    assert result.exit_code == 0
    assert "Planner assistant: ON" in result.output


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
