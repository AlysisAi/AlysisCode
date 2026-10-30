from __future__ import annotations

import json
import os
import subprocess
import warnings
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.config import AppConfig
from alysis_code.conflict_auto_resolver import (
    CONFLICT_RESOLVER_SYSTEM_PROMPT,
    ConflictAutoResolveSettings,
    attempt_auto_resolve_conflict,
    load_conflict_auto_resolve_settings,
)
from alysis_code.execution_context import ExecutionContextBudgetError
from alysis_code.execution_shared import build_task_execution_instruction_bundle
from alysis_code.forge import add_task, create_plan_run, load_plan, save_plan
from alysis_code.knowledge_base import write_task_attempt_entry
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.verify_gate import ResolvedVerifyCommands

_ORIGINAL_SUBPROCESS_RUN = subprocess.run


def _delegate_non_git_subprocess_calls(fake_run: Any) -> Any:
    def wrapped(cmd: Any, **kwargs: Any) -> Any:
        if isinstance(cmd, (list, tuple)) and cmd and cmd[0] == "git":
            return fake_run(cmd, **kwargs)
        return _ORIGINAL_SUBPROCESS_RUN(cmd, **kwargs)

    return wrapped


def test_load_conflict_auto_resolve_settings_defaults_to_enabled() -> None:
    settings = load_conflict_auto_resolve_settings(
        cfg=AppConfig(model="default-model"),
        env={},
    )

    assert settings.enabled is True
    assert settings.verify_mode == "strict"
    assert settings.max_attempts == 1


def test_load_conflict_auto_resolve_settings_env_overrides_config() -> None:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {
        "conflict_auto_resolve": {
            "enabled": True,
            "verify_mode": "warn",
            "max_attempts": 5,
        }
    }
    settings = load_conflict_auto_resolve_settings(
        cfg=cfg,
        env={
            "ALYSIS_CONFLICT_AUTO_RESOLVE": "0",
            "ALYSIS_CONFLICT_AUTO_RESOLVE_VERIFY": "strict",
            "ALYSIS_CONFLICT_AUTO_RESOLVE_MAX_ATTEMPTS": "2",
        },
    )
    assert settings.enabled is False
    assert settings.verify_mode == "strict"
    assert settings.max_attempts == 2


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


def _git_args(cmd: list[str]) -> tuple[str, list[str]]:
    assert cmd[0] == "git"
    assert cmd[1] == "-C"
    cwd = cmd[2]
    args = cmd[3:]
    cleaned: list[str] = []
    i = 0
    while i < len(args):
        if args[i] == "-c":
            i += 2
            continue
        cleaned.append(args[i])
        i += 1
    return cwd, cleaned


def _structured_capture_text() -> str:
    return "\n".join(
        [
            "Resolved the merge conflict cleanly.",
            "",
            "```knowledge_capture_json",
            json.dumps(
                {
                    "schema_version": 1,
                    "facts": [
                        {
                            "title": "Conflict resolution preserved parser behavior",
                            "summary": "The resolved merge kept the intended parser behavior in src/x.py.",
                            "paths": ["src/x.py"],
                            "tags": ["conflict", "parser"],
                        }
                    ],
                    "decisions": [
                        {
                            "decision_key": "conflict-parser-resolution",
                            "title": "Keep parser conflict resolution minimal",
                            "summary": "The merge conflict was resolved with minimal parser changes.",
                            "status": "active",
                            "paths": ["src/x.py"],
                            "tags": ["conflict", "parser"],
                        }
                    ],
                },
                indent=2,
                sort_keys=True,
            ),
            "```",
        ]
    )


def _sequence_returner(values: list[Any]):  # type: ignore[no-untyped-def]
    state = {"index": 0}

    def _fn(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        idx = state["index"]
        state["index"] += 1
        value = values[idx] if idx < len(values) else values[-1]
        if isinstance(value, dict):
            return dict(value)
        if isinstance(value, list):
            return list(value)
        return value

    return _fn


def _path_exists(path: Path) -> bool:
    if os.name != "nt":
        return path.exists()
    raw = os.path.abspath(os.fspath(path))
    if not raw.startswith("\\\\?\\"):
        raw = "\\\\?\\UNC\\" + raw.lstrip("\\") if raw.startswith("\\\\") else "\\\\?\\" + raw
    return os.path.exists(raw)


def _stub_clean_conflict_run_state(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_runtime_tree",
        _sequence_returner([{}, {}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_workspace_tree",
        _sequence_returner([{}, {}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_changed_files_including_untracked",
        lambda _root: [],
    )


def _stub_conflict_knowledge_mirror(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.prepare_task_execution_knowledge",
        lambda **_kwargs: SimpleNamespace(prompt_section=""),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_selected_knowledge_into_worktree",
        lambda **_kwargs: None,
    )


def test_attempt_auto_resolve_conflict_success_writes_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    task["conflict_attempts"] = 3
    prior_task = add_task(
        plan,
        title="Prior parser work",
        estimated_files=["src/x.py"],
        branch="feat/t00",
    )
    plan["role_models"] = {"conflict_resolve": "resolver-model"}
    save_plan(paths, plan)
    write_task_attempt_entry(
        paths=paths,
        task=prior_task,
        source="forge_exec",
        result="success",
        summary="Previous parser work touched src/x.py.",
        changed_files=["src/x.py"],
        verify_summary="pytest passed",
        report_path=None,
        patch_path=None,
        verify_artifact_path=None,
        budget_artifact_path=None,
        session_artifact_dir=None,
    )

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"
    captured: dict[str, object] = {}
    cfg = AppConfig(model="default-model", max_steps=77, task_max_steps=30)

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )

    list_calls = {"n": 0}

    def fake_list_unmerged(_root: Path) -> list[str]:
        list_calls["n"] += 1
        return ["src/x.py"] if list_calls["n"] == 1 else []

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        fake_list_unmerged,
    )
    _stub_clean_conflict_run_state(monkeypatch)
    real_build_bundle = build_task_execution_instruction_bundle

    def capture_build_bundle(**kwargs):  # type: ignore[no-untyped-def]
        captured["bundle_trusted_system_prompt_override"] = kwargs.get(
            "trusted_system_prompt_override"
        )
        return real_build_bundle(**kwargs)

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.build_task_execution_instruction_bundle",
        capture_build_bundle,
    )

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["allow_write_globs"] = kwargs["allow_write_globs"]
        captured["instruction"] = kwargs["instruction"]
        captured["max_steps"] = kwargs["max_steps"]
        captured["runtime_kind"] = kwargs.get("runtime_kind")
        captured["trusted_system_prompt_override"] = kwargs.get("trusted_system_prompt_override")
        captured["verification_enabled"] = kwargs.get("verification_enabled")
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["enable_tool_output_offload"] = kwargs.get("enable_tool_output_offload")
        captured["enable_conversation_summarization"] = kwargs.get(
            "enable_conversation_summarization"
        )
        captured["compaction_profile"] = kwargs.get("compaction_profile")
        captured["enable_chat_turn_step_budget"] = kwargs.get("enable_chat_turn_step_budget")
        captured["subagents_enabled"] = kwargs.get("subagents_enabled")
        captured["session_log_dir_override"] = kwargs.get("session_log_dir_override")
        cfg = kwargs["cfg"]
        captured["model"] = cfg.model
        captured["cfg_verify_commands"] = list(cfg.verify_commands)
        kwargs["surface"].on_assistant_message_done(_structured_capture_text())
        return 0

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", lambda _root: None)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.format_patch_stdout",
        lambda *_a, **_k: "From deadbeef\nnew file mode 100644\n",
    )

    class _VerifyPass:
        all_passed = True
        summary = "verification passed (1/1)"

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_task_verification",
        lambda **_kwargs: _VerifyPass(),
    )

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        if cwd == str(worktree_repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="resolve123\n")
        if cwd == str(repo) and args == ["checkout", "main"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["merge", "--ff-only", "conflict/t01"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="merge123\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=cfg,
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="strict",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is True
    assert outcome.merge_commit_hash == "merge123"
    assert captured["allow_write_globs"] == ["src/x.py"]
    assert captured["model"] == "resolver-model"
    assert captured["verification_enabled"] is True
    assert captured["authoritative_verification_commands"] == ["pytest -q"]
    assert captured["cfg_verify_commands"] == ["pytest -q"]
    assert captured["bundle_trusted_system_prompt_override"] == CONFLICT_RESOLVER_SYSTEM_PROMPT
    assert captured["enable_tool_output_offload"] is True
    assert captured["enable_conversation_summarization"] is True
    assert captured["compaction_profile"] == "execution"
    assert captured["enable_chat_turn_step_budget"] is False
    assert captured["subagents_enabled"] is False
    assert captured["runtime_kind"] == RuntimeKind.CONFLICT_AUTO_RESOLVE
    assert captured["session_log_dir_override"] == outcome.result_json_path.parent / "sessions"
    instruction = str(captured["instruction"])
    assert "## Conflict Resolution Scope" in instruction
    assert "## Relevant Knowledge" in instruction
    assert "Resolve only the merge conflict." in instruction
    assert "- `src/x.py`" in instruction
    assert "## Task Specification" in instruction
    assert "## Selected Assets" not in instruction
    assert "## Execution Rules" in instruction
    system_prompt = str(captured["trusted_system_prompt_override"])
    assert "You are CONFLICT_RESOLVER." in system_prompt
    assert "Resolve merge conflicts only." in system_prompt
    assert outcome.result_json_path.exists()
    assert outcome.report_path.exists()
    assert outcome.patch_path.exists()
    assert (outcome.result_json_path.parent / "auto_resolve_context.md").exists()
    assert (outcome.result_json_path.parent / "auto_resolve_budget.json").exists()
    mirrored_manifest = (
        worktree_repo
        / ".alysis"
        / "runs"
        / paths.run_id
        / "knowledge"
        / "selected"
        / str(task["id"])
        / "conflict_auto_resolve"
        / "manifest.json"
    )
    assert _path_exists(mirrored_manifest)
    attempt_entries = list((paths.knowledge_task_attempts_dir / str(task["id"])).glob("*.md"))
    assert attempt_entries
    capture_dirs = list((outcome.result_json_path.parent / "knowledge_capture").glob("*"))
    assert capture_dirs
    validation_payload = json.loads(
        (capture_dirs[0] / "validation.json").read_text(encoding="utf-8")
    )
    assert validation_payload["valid"] is True
    assert validation_payload["promotable_fact_count"] == 1
    assert validation_payload["promotable_decision_count"] == 1
    promotion_payload = json.loads((capture_dirs[0] / "promotion.json").read_text(encoding="utf-8"))
    assert promotion_payload["promotion_attempted"] is True
    assert promotion_payload["promotion_succeeded"] is True
    assert promotion_payload["fact_entry_ids"]
    assert promotion_payload["decision_entry_ids"]
    assert list((paths.knowledge_facts_dir / str(task["id"])).glob("*.md"))
    assert list((paths.knowledge_decisions_dir / str(task["id"])).glob("*.md"))

    payload = json.loads(outcome.result_json_path.read_text(encoding="utf-8"))
    assert payload["success"] is True
    assert payload["agent_exit_code"] == 0
    assert payload["salvaged_nonzero_exit"] is False
    assert payload["verify_summary"] == "verification passed (1/1)"
    assert Path(payload["context_artifact_path"]).name == "auto_resolve_context.md"
    assert Path(payload["budget_artifact_path"]).name == "auto_resolve_budget.json"
    budget_payload = json.loads(
        (outcome.result_json_path.parent / "auto_resolve_budget.json").read_text(encoding="utf-8")
    )
    assert budget_payload["trusted_system_prompt_override_applied"] is True
    assert budget_payload["step_budget"]["kind"] == "conflict_resolution"
    assert budget_payload["step_budget"]["reason"] == "autonomous_unbounded"
    assert budget_payload["step_budget"]["resolved_max_steps"] is None


def test_attempt_auto_resolve_conflict_preserves_warning_visibility_for_recording_surface(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )
    _stub_conflict_knowledge_mirror(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        _sequence_returner([["src/x.py"], []]),
    )
    _stub_clean_conflict_run_state(monkeypatch)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        kwargs["surface"].on_warning("Model metadata warning for unknown-model-xyz")
        kwargs["surface"].on_assistant_message_done("Resolved the merge conflict.")
        return 0

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", lambda _root: None)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.format_patch_stdout",
        lambda *_a, **_k: "From deadbeef\nnew file mode 100644\n",
    )

    class _VerifyPass:
        all_passed = True
        summary = "verification passed (1/1)"

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_task_verification",
        lambda **_kwargs: _VerifyPass(),
    )

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        if cwd == str(worktree_repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="resolve123\n")
        if cwd == str(repo) and args == ["checkout", "main"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["merge", "--ff-only", "conflict/t01"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="merge123\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    with warnings.catch_warnings(record=True) as seen:
        warnings.simplefilter("always")
        outcome = attempt_auto_resolve_conflict(
            paths=paths,
            plan=load_plan(paths),
            task=task,
            cfg=AppConfig(model="default-model"),
            api_key_override="k",
            base_branch="main",
            task_branch="feat/t01",
            keep_worktrees=True,
            settings=ConflictAutoResolveSettings(
                enabled=True,
                verify_mode="strict",
                max_attempts=1,
            ),
            verify_commands=["pytest -q"],
        )

    assert outcome.success is True
    assert outcome.error is None
    assert any("Model metadata warning for unknown-model-xyz" in str(item.message) for item in seen)


def test_attempt_auto_resolve_conflict_rejects_nonzero_exit_with_material_changes(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )
    _stub_conflict_knowledge_mirror(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        _sequence_returner([["src/x.py"], []]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_runtime_tree",
        _sequence_returner([{}, {}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_workspace_tree",
        _sequence_returner(
            [
                {"src/x.py": "before"},
                {
                    "src/x.py": "after",
                    "notes/conflict-resolution.md": "new",
                },
            ]
        ),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_changed_files_including_untracked",
        lambda _root: ["src/x.py", "notes/conflict-resolution.md"],
    )

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        kwargs["surface"].on_assistant_message_done(_structured_capture_text())
        return 1

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.stage_all",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("stage_all should not run after a non-zero agent exit")
        ),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.format_patch_stdout",
        lambda *_a, **_k: "From deadbeef\nnew file mode 100644\n",
    )

    def fail_verify(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_task_verification should not be called when verify_mode=off")

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_task_verification", fail_verify)

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        if cwd == str(worktree_repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="resolve123\n")
        if cwd == str(repo) and args == ["checkout", "main"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["merge", "--ff-only", "conflict/t01"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="merge123\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="off",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is False
    assert outcome.salvaged_nonzero_exit is False
    assert outcome.agent_exit_code == 1
    expected_warning = (
        "Conflict auto-resolve accepted a salvaged non-zero agent exit after material "
        "conflict-resolution changes were detected."
    )
    assert expected_warning not in outcome.warnings
    assert "failed with exit code 1 after producing material conflict-resolution changes" in (
        outcome.error or ""
    )
    assert "refusing to accept partial conflict auto-resolve result" in (outcome.error or "")
    payload = json.loads(outcome.result_json_path.read_text(encoding="utf-8"))
    assert payload["success"] is False
    assert payload["agent_exit_code"] == 1
    assert payload["salvaged_nonzero_exit"] is False
    assert expected_warning not in payload["warnings"]
    report_text = outcome.report_path.read_text(encoding="utf-8")
    assert "- Salvaged Non-Zero Exit: no" in report_text
    assert expected_warning not in report_text


def test_attempt_auto_resolve_conflict_rejects_nonzero_exit_before_strict_verify(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"
    commit_calls = {"count": 0}

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )
    _stub_conflict_knowledge_mirror(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        _sequence_returner([["src/x.py"], []]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_runtime_tree",
        _sequence_returner([{}, {}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_workspace_tree",
        _sequence_returner([{"src/x.py": "before"}, {"src/x.py": "after"}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_changed_files_including_untracked",
        lambda _root: ["src/x.py"],
    )

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        kwargs["surface"].on_assistant_message_done(_structured_capture_text())
        return 1

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_agent", fake_run_agent)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.stage_all",
        lambda _root: (_ for _ in ()).throw(
            AssertionError("stage_all should not run after a non-zero agent exit")
        ),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_task_verification",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("verification should not run after a non-zero agent exit")
        ),
    )

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            commit_calls["count"] += 1
            raise AssertionError("commit should not run after a non-zero agent exit")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=False,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="strict",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is False
    assert outcome.salvaged_nonzero_exit is False
    assert outcome.agent_exit_code == 1
    assert "failed with exit code 1 after producing material conflict-resolution changes" in (
        outcome.error or ""
    )
    assert worktree_repo.exists() is True
    assert commit_calls["count"] == 0
    payload = json.loads(outcome.result_json_path.read_text(encoding="utf-8"))
    assert payload["success"] is False
    assert payload["agent_exit_code"] == 1
    assert payload["salvaged_nonzero_exit"] is False
    assert not any(
        "accepted a salvaged non-zero agent exit" in item for item in payload["warnings"]
    )


def test_attempt_auto_resolve_conflict_refines_generic_fallback_to_node_test(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        estimated_files=["test/app.test.js", "src/app.js"],
        branch="feat/t01",
    )
    task["write_scope"] = ["test/app.test.js", "src/app.js"]
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"
    captured: dict[str, object] = {}

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )

    list_calls = {"n": 0}

    def fake_list_unmerged(_root: Path) -> list[str]:
        list_calls["n"] += 1
        return ["test/app.test.js"] if list_calls["n"] == 1 else []

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        fake_list_unmerged,
    )
    _stub_clean_conflict_run_state(monkeypatch)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        return 0

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", lambda _root: None)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.format_patch_stdout",
        lambda *_a, **_k: "From deadbeef\n",
    )

    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        captured["verify_commands"] = list(kwargs["commands"])

        class _VerifyPass:
            all_passed = True
            summary = "verification passed (1/1)"

        return _VerifyPass()

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_task_verification", fake_verify)

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        if cwd == str(worktree_repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="resolve123\n")
        if cwd == str(repo) and args == ["checkout", "main"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["merge", "--ff-only", "conflict/t01"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="merge123\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="warn",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
        verify_command_selection=ResolvedVerifyCommands(
            commands=("pytest -q",),
            source="config.verify_commands_fallback",
        ),
    )

    assert outcome.success is True
    assert captured["authoritative_verification_commands"] == ["node --test"]
    assert captured["cfg_verify_commands"] == ["node --test"]
    assert captured["verify_commands"] == ["node --test"]


def test_attempt_auto_resolve_conflict_uses_structured_node_text_refinement(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        estimated_files=["src/app.js"],
        branch="feat/t01",
    )
    task["write_scope"] = ["src/app.js"]
    task["acceptance_criteria"] = ["Use node --test for test verification."]
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"
    captured: dict[str, object] = {}

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )

    list_calls = {"n": 0}

    def fake_list_unmerged(_root: Path) -> list[str]:
        list_calls["n"] += 1
        return ["src/app.js"] if list_calls["n"] == 1 else []

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        fake_list_unmerged,
    )
    _stub_clean_conflict_run_state(monkeypatch)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        captured["cfg_verify_commands"] = list(kwargs["cfg"].verify_commands)
        return 0

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", lambda _root: None)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.format_patch_stdout",
        lambda *_a, **_k: "From deadbeef\n",
    )

    def fake_verify(**kwargs):  # type: ignore[no-untyped-def]
        captured["verify_commands"] = list(kwargs["commands"])

        class _VerifyPass:
            all_passed = True
            summary = "verification passed (1/1)"

        return _VerifyPass()

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_task_verification", fake_verify)

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        if cwd == str(worktree_repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="resolve123\n")
        if cwd == str(repo) and args == ["checkout", "main"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["merge", "--ff-only", "conflict/t01"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="merge123\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="warn",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
        verify_command_selection=ResolvedVerifyCommands(
            commands=("pytest -q",),
            source="config.verify_commands_fallback",
        ),
    )

    assert outcome.success is True
    assert captured["authoritative_verification_commands"] == ["node --test"]
    assert captured["cfg_verify_commands"] == ["node --test"]
    assert captured["verify_commands"] == ["node --test"]


@pytest.mark.parametrize("context_tokens", [9948, 13_500])
def test_conflict_instruction_bundle_preserves_scope_and_unmerged_files_under_tight_budget(
    tmp_path: Path,
    context_tokens: int,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    plan = {
        "project_goal": "Resolve merge conflicts safely",
        "summary": "Large plan summary " * 80,
        "requirements": [f"requirement {i:03d}" for i in range(120)],
        "tasks": [
            {
                "id": f"T{i:02d}",
                "title": f"Task {i}",
                "dependencies": [f"T{i - 1:02d}"] if i > 1 else [],
            }
            for i in range(1, 80)
        ],
        "assets": [
            {
                "stored_path": f".alysis/runs/r/plan/assets/spec_{i:03d}.md",
                "text_copy_path": f".alysis/runs/r/plan/assets_text/spec_{i:03d}.txt",
            }
            for i in range(90)
        ],
    }
    task = {
        "id": "T81",
        "title": "Resolve spec_042 merge conflict",
        "description": "Need the selected asset and merge-conflict scope to survive trimming. "
        * 60,
        "acceptance_criteria": [f"criterion {i}" for i in range(20)],
        "dependencies": ["T10"],
        "estimated_files": ["src/parser.py"],
        "write_scope": ["src/parser.py"],
        "branch": "feat/conflict",
        "status": "merge_conflict",
    }
    cfg = AppConfig(model="resolver-model")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "resolver-model": {
                    # The smaller budget must reject the full required task;
                    # the fitting case may reduce only supplementary plan context.
                    "context_window_tokens": context_tokens,
                    "max_output_tokens": 2048,
                    "supports_vision": False,
                }
            }
        }
    }
    unmerged_files = [f"src/conflict_{i:02d}.py" for i in range(18)] + ["tests/test_parser.py"]

    bundle_args = dict(
        plan=plan,
        task=task,
        root=repo,
        cfg=cfg,
        role_model="resolver-model",
        mode="auto",
        yes=True,
        deny_write_prefixes=[".alysis"],
        allow_write_globs=["src/parser.py", "tests/test_parser.py"],
        non_interactive=True,
        verification_enabled=True,
        authoritative_verification_commands=["pytest -q"],
        subagents_enabled=False,
        leading_sections=[
            "\n".join(
                [
                    "## Conflict Resolution Scope",
                    "",
                    "- Resolve only the merge conflict.",
                    "- Do not expand scope beyond the conflict resolution itself.",
                    "",
                    "### Unmerged Files",
                    "",
                    *[f"- `{path}`" for path in unmerged_files],
                ]
            )
        ],
    )

    if context_tokens == 9948:
        with pytest.raises(ExecutionContextBudgetError) as caught:
            build_task_execution_instruction_bundle(**bundle_args)
        assert caught.value.required_tokens > caught.value.available_tokens
        return
    bundle = build_task_execution_instruction_bundle(**bundle_args)
    assert task["description"].strip() in bundle.instruction
    assert all(item in bundle.instruction for item in task["acceptance_criteria"])
    assert all(path in bundle.instruction for path in unmerged_files)
    assert bundle.truncation_strategy.startswith("execution_priority")
    assert "## Conflict Resolution Scope" in bundle.instruction
    assert "- `src/conflict_00.py`" in bundle.instruction
    assert "- `tests/test_parser.py`" in bundle.instruction
    assert "## Task Specification" in bundle.instruction
    assert "## Execution Rules" in bundle.instruction
    assert ".alysis/runs/r/plan/assets/spec_042.md" in bundle.instruction
    assert ".alysis/runs/r/plan/assets_text/spec_042.txt" in bundle.instruction
    assert (
        "additional tasks omitted" in bundle.instruction
        or "earlier requirements omitted" in bundle.instruction
        or bundle.truncation_strategy == "execution_priority_minimal_plan"
    )


def test_attempt_auto_resolve_conflict_invalid_attempt_count_falls_back_to_one(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    task["conflict_attempts"] = "oops"
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )

    list_calls = {"n": 0}

    def fake_list_unmerged(_root: Path) -> list[str]:
        list_calls["n"] += 1
        return ["src/x.py"] if list_calls["n"] == 1 else []

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        fake_list_unmerged,
    )
    _stub_clean_conflict_run_state(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_agent",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", lambda _root: None)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.format_patch_stdout",
        lambda *_a, **_k: "From deadbeef\nnew file mode 100644\n",
    )

    def fail_verify(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_task_verification should not be called when verify_mode=off")

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_task_verification", fail_verify)

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        if cwd == str(worktree_repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="resolve123\n")
        if cwd == str(repo) and args == ["checkout", "main"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["merge", "--ff-only", "conflict/t01"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="merge123\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model", task_max_steps=40),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="off",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is True
    budget_payload = json.loads(
        (outcome.result_json_path.parent / "auto_resolve_budget.json").read_text(encoding="utf-8")
    )
    assert budget_payload["step_budget"]["kind"] == "conflict_resolution"
    assert budget_payload["step_budget"]["signals_used"]["attempt_count"] == 1
    assert budget_payload["step_budget"]["signals_used"]["verification_enabled"] is False


def test_attempt_auto_resolve_conflict_verify_off_disables_tool_exposure_and_outer_verify(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"
    captured: dict[str, object] = {}

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )

    list_calls = {"n": 0}

    def fake_list_unmerged(_root: Path) -> list[str]:
        list_calls["n"] += 1
        return ["src/x.py"] if list_calls["n"] == 1 else []

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        fake_list_unmerged,
    )
    _stub_clean_conflict_run_state(monkeypatch)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        captured["verification_enabled"] = kwargs.get("verification_enabled")
        captured["authoritative_verification_commands"] = kwargs.get(
            "authoritative_verification_commands"
        )
        return 0

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", lambda _root: None)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.format_patch_stdout",
        lambda *_a, **_k: "From deadbeef\nnew file mode 100644\n",
    )

    def fail_verify(**_kwargs):  # type: ignore[no-untyped-def]
        raise AssertionError("run_task_verification should not be called when verify_mode=off")

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_task_verification", fail_verify)

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        if cwd == str(worktree_repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="resolve123\n")
        if cwd == str(repo) and args == ["checkout", "main"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["merge", "--ff-only", "conflict/t01"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="merge123\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="off",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is True
    assert captured["verification_enabled"] is False
    assert captured["authoritative_verification_commands"] is None
    assert outcome.verify_summary == "verification disabled (conflict auto-resolve verify=off)"
    payload = json.loads(outcome.result_json_path.read_text(encoding="utf-8"))
    assert payload["verify_mode"] == "off"
    assert payload["verify_artifact_path"] is None


def test_attempt_auto_resolve_conflict_nonzero_exit_without_material_changes_fails(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )
    _stub_conflict_knowledge_mirror(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        _sequence_returner([["src/x.py"]]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_runtime_tree",
        _sequence_returner([{}, {}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_workspace_tree",
        _sequence_returner([{"src/x.py": "same"}, {"src/x.py": "same"}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_changed_files_including_untracked",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_agent",
        lambda **_kwargs: 1,
    )

    def fail_stage_all(_root: Path) -> None:
        raise AssertionError("stage_all should not run when non-zero salvage is blocked")

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", fail_stage_all)

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="off",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is False
    assert outcome.salvaged_nonzero_exit is False
    assert outcome.agent_exit_code == 1
    assert "produced no material conflict-resolution changes" in (outcome.error or "")


def test_attempt_auto_resolve_conflict_runtime_artifact_drift_blocks_nonzero_salvage(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )
    _stub_conflict_knowledge_mirror(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        _sequence_returner([["src/x.py"]]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_runtime_tree",
        _sequence_returner([{}, {".alysis/state.json": "sha256:changed"}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_workspace_tree",
        _sequence_returner([{"src/x.py": "before"}, {"src/x.py": "after"}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_changed_files_including_untracked",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_agent",
        lambda **_kwargs: 1,
    )

    def fail_stage_all(_root: Path) -> None:
        raise AssertionError("stage_all should not run when runtime drift blocks salvage")

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", fail_stage_all)

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="off",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is False
    assert outcome.salvaged_nonzero_exit is False
    assert outcome.agent_exit_code == 1
    assert "protected runtime artifacts under .alysis changed" in (outcome.error or "")


def test_attempt_auto_resolve_conflict_nonzero_exit_with_unresolved_markers_stays_failure(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        conflict_file = wt / "src/x.py"
        conflict_file.parent.mkdir(parents=True, exist_ok=True)
        conflict_file.write_text(
            "<<<<<<< HEAD\nold\n=======\nnew\n>>>>>>> branch\n",
            encoding="utf-8",
        )
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )
    _stub_conflict_knowledge_mirror(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        _sequence_returner([["src/x.py"], ["src/x.py"]]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_runtime_tree",
        _sequence_returner([{}, {}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_workspace_tree",
        _sequence_returner([{"src/x.py": "before"}, {"src/x.py": "after"}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_changed_files_including_untracked",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_agent",
        lambda **_kwargs: 1,
    )

    def fail_stage_all(_root: Path) -> None:
        raise AssertionError("stage_all should not run while conflicts remain unresolved")

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", fail_stage_all)

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="off",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is False
    assert outcome.salvaged_nonzero_exit is False
    assert outcome.agent_exit_code == 1
    assert "failed with exit code 1 after producing material conflict-resolution changes" in (
        outcome.error or ""
    )


def test_attempt_auto_resolve_conflict_stages_resolved_unmerged_paths_before_checking_status(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"
    calls: list[str] = []

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        resolved = wt / "src/x.py"
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text("resolved = True\n", encoding="utf-8")
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        _sequence_returner([["src/x.py"], []]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_runtime_tree",
        _sequence_returner([{}, {}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_workspace_tree",
        _sequence_returner([{"src/x.py": "before"}, {"src/x.py": "after"}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_changed_files_including_untracked",
        lambda _root: ["src/x.py"],
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_agent",
        lambda **_kwargs: 0,
    )

    def fake_stage_all(_root: Path) -> None:
        calls.append("stage_all")

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", fake_stage_all)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.format_patch_stdout",
        lambda *_a, **_k: "From deadbeef\n",
    )

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        if cwd == str(worktree_repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="resolve123\n")
        if cwd == str(repo) and args == ["checkout", "main"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["merge", "--ff-only", "conflict/t01"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="merge123\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="off",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is True
    assert calls == ["stage_all"]


def test_attempt_auto_resolve_conflict_run_agent_exception_is_not_salvaged(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        _sequence_returner([["src/x.py"]]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_runtime_tree",
        _sequence_returner([{}, {}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.snapshot_workspace_tree",
        _sequence_returner([{"src/x.py": "before"}, {"src/x.py": "after"}]),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_changed_files_including_untracked",
        lambda _root: ["src/x.py"],
    )

    def fake_run_agent(**_kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("transport failed")

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_agent", fake_run_agent)

    def fail_stage_all(_root: Path) -> None:
        raise AssertionError("stage_all should not run after a wrapper exception")

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", fail_stage_all)

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="off",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is False
    assert outcome.salvaged_nonzero_exit is False
    assert outcome.agent_exit_code is None
    assert "raised before returning normally" in (outcome.error or "")


def test_attempt_auto_resolve_conflict_strict_verify_failure_keeps_worktree(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    plan["role_models"] = {"coding": "coding-model"}
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"
    captured: dict[str, object] = {}

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )

    list_calls = {"n": 0}

    def fake_list_unmerged(_root: Path) -> list[str]:
        list_calls["n"] += 1
        return ["src/x.py"] if list_calls["n"] == 1 else []

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        fake_list_unmerged,
    )
    _stub_clean_conflict_run_state(monkeypatch)

    def fake_run_agent(**kwargs) -> int:  # type: ignore[no-untyped-def]
        cfg = kwargs["cfg"]
        captured["model"] = cfg.model
        kwargs["surface"].on_assistant_message_done(_structured_capture_text())
        return 0

    monkeypatch.setattr("alysis_code.conflict_auto_resolver.run_agent", fake_run_agent)
    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", lambda _root: None)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )

    class _VerifyFail:
        all_passed = False
        summary = "verification failed (0/1)"

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_task_verification",
        lambda **_kwargs: _VerifyFail(),
    )

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=False,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="strict",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is False
    assert "strict conflict verify failed" in (outcome.error or "")
    assert worktree_repo.exists() is True
    assert captured["model"] == "coding-model"
    payload = json.loads(outcome.result_json_path.read_text(encoding="utf-8"))
    assert payload["agent_exit_code"] == 0
    assert payload["salvaged_nonzero_exit"] is False
    assert payload["worktree_kept"] is True
    issue_entries = list((paths.knowledge_issues_dir / str(task["id"])).glob("*.md"))
    assert issue_entries
    capture_dirs = list((outcome.result_json_path.parent / "knowledge_capture").glob("*"))
    assert capture_dirs
    validation_payload = json.loads(
        (capture_dirs[0] / "validation.json").read_text(encoding="utf-8")
    )
    assert validation_payload["valid"] is True
    promotion_payload = json.loads((capture_dirs[0] / "promotion.json").read_text(encoding="utf-8"))
    assert promotion_payload["promotion_attempted"] is False
    assert promotion_payload["promotion_succeeded"] is False
    assert (
        promotion_payload["promotion_skipped_reason"]
        == "conflict auto-resolve outcome was not accepted"
    )
    assert list((paths.knowledge_facts_dir / str(task["id"])).glob("*.md")) == []
    assert list((paths.knowledge_decisions_dir / str(task["id"])).glob("*.md")) == []


def test_attempt_auto_resolve_conflict_warn_verify_failure_is_warning(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan,
        title="Resolve merge conflict",
        branch="feat/t01",
        _allow_execution_unready=True,
    )
    save_plan(paths, plan)

    task_id = str(task["id"])
    worktree_repo = paths.run_dir / "conflict_worktrees" / task_id / "repo"

    def fake_ensure_task_worktree(**kwargs):  # type: ignore[no-untyped-def]
        wt = Path(kwargs["worktree_repo_path"])
        wt.mkdir(parents=True, exist_ok=True)
        return None

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        fake_ensure_task_worktree,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree",
        lambda **_kwargs: None,
    )

    list_calls = {"n": 0}

    def fake_list_unmerged(_root: Path) -> list[str]:
        list_calls["n"] += 1
        return ["src/x.py"] if list_calls["n"] == 1 else []

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files",
        fake_list_unmerged,
    )
    _stub_clean_conflict_run_state(monkeypatch)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_agent",
        lambda **_kwargs: 0,
    )
    monkeypatch.setattr("alysis_code.conflict_auto_resolver.stage_all", lambda _root: None)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.unstage_staged_prefixes", lambda *_a, **_k: []
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_not_staged_prefixes",
        lambda *_a, **_k: None,
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.format_patch_stdout",
        lambda *_a, **_k: "From deadbeef\nnew file mode 100644\n",
    )

    class _VerifyFail:
        all_passed = False
        summary = "verification failed (0/1)"

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_task_verification",
        lambda **_kwargs: _VerifyFail(),
    )

    @_delegate_non_git_subprocess_calls
    def fake_subprocess_run(cmd, **_kwargs):  # type: ignore[no-untyped-def]
        cwd, args = _git_args(cmd)
        if cwd == str(worktree_repo) and args == [
            "merge",
            "--no-ff",
            "feat/t01",
            "-m",
            "Resolve T01",
        ]:
            return _cp(returncode=1, stderr="CONFLICT")
        if cwd == str(worktree_repo) and args == ["commit", "--no-edit"]:
            return _cp(stdout="[conflict] merge commit\n")
        if cwd == str(worktree_repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="resolve123\n")
        if cwd == str(repo) and args == ["checkout", "main"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["merge", "--ff-only", "conflict/t01"]:
            return _cp(stdout="")
        if cwd == str(repo) and args == ["rev-parse", "HEAD"]:
            return _cp(stdout="merge123\n")
        raise AssertionError(f"unexpected git args for {cwd}: {args}")

    monkeypatch.setattr(subprocess, "run", fake_subprocess_run)

    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=load_plan(paths),
        task=task,
        cfg=AppConfig(model="default-model"),
        api_key_override="k",
        base_branch="main",
        task_branch="feat/t01",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(
            enabled=True,
            verify_mode="warn",
            max_attempts=1,
        ),
        verify_commands=["pytest -q"],
    )

    assert outcome.success is True
    assert outcome.verify_summary == "verification failed (0/1)"
    assert "Conflict verify warning: verification failed (0/1)" in outcome.warnings


def test_conflict_resolver_prompt_matches_current_tool_policy() -> None:
    assert "Prefer search_rg plus fs_read line ranges for focused conflict inspection" in (
        CONFLICT_RESOLVER_SYSTEM_PROMPT
    )
    assert "Prefer fs_edit for deterministic localized edits in one existing conflicted file." in (
        CONFLICT_RESOLVER_SYSTEM_PROMPT
    )
    assert "Prefer git_apply_patch for broader or context-heavy conflict edits" in (
        CONFLICT_RESOLVER_SYSTEM_PROMPT
    )
    assert "If verification tools/commands are available in this run" in (
        CONFLICT_RESOLVER_SYSTEM_PROMPT
    )


def test_conflict_budget_rejection_is_reported_without_agent_dispatch(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    paths = create_plan_run(repo)
    plan = load_plan(paths)
    task = add_task(
        plan, title="Resolve bounded conflict", branch="feat/task", estimated_files=["src/owned.py"]
    )
    save_plan(paths, plan)
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.ensure_task_worktree",
        lambda **kwargs: kwargs["worktree_repo_path"].mkdir(parents=True),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.mirror_plan_into_worktree", lambda **_: None
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver._run_git",
        lambda *args: subprocess.CompletedProcess(args, 1, "conflict", ""),
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.list_unmerged_files", lambda _: ["src/owned.py"]
    )
    _stub_conflict_knowledge_mirror(monkeypatch)

    def reject(**kwargs):
        raise ExecutionContextBudgetError(available_tokens=100, required_tokens=900)

    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.build_task_execution_instruction_bundle", reject
    )
    monkeypatch.setattr(
        "alysis_code.conflict_auto_resolver.run_agent",
        lambda **_: pytest.fail("Budget rejection must precede dispatch"),
    )
    outcome = attempt_auto_resolve_conflict(
        paths=paths,
        plan=plan,
        task=task,
        cfg=AppConfig(model="test-model"),
        api_key_override=None,
        base_branch="main",
        task_branch="feat/task",
        keep_worktrees=True,
        settings=ConflictAutoResolveSettings(True, "off", 1),
    )
    assert outcome.success is False
    assert outcome.agent_exit_code is None
    assert "Increase the context allowance" in outcome.error
    payload = json.loads(outcome.result_json_path.read_text())
    assert payload["success"] is False
    assert payload["context_artifact_path"] is None
    assert "900 estimated tokens" in outcome.report_path.read_text()
