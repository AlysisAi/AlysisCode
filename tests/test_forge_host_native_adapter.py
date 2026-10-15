from __future__ import annotations

import copy
import hashlib
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from alysis_code.forge import create_plan_run, load_plan
from alysis_code.forge_host_native_adapter import (
    ForgeHostPreflightError,
    NativeAdapterRequest,
    ProviderBinding,
    build_native_plan_admission,
    revalidate_native_plan_admission,
)
from alysis_code.forge_host_protocol.canonical import (
    canonical_fingerprint,
    execution_admission_fingerprint,
    plan_fingerprint,
    verification_contract_fingerprint,
)
from alysis_code.forge_host_protocol.models import (
    Identity,
    PlannerGoal,
    PlannerInput,
)
from alysis_code.forge_host_service import (
    ForgeHostPlannerAdapter,
    ForgeHostPlannerAdapterSettings,
    HostPlannerRequest,
)
from alysis_code.forge_planner_engine import (
    ForgePlannerDependencies,
    ForgePlannerState,
)
from alysis_code.plan_assistant import PlannerTurnResult
from alysis_code.plan_reconciliation import PlanReconciliationResult


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repo(tmp_path: Path) -> tuple[Path, object, dict[str, object]]:
    root = tmp_path / "repo"
    trusted_pytest = tmp_path / "bin" / "pytest"
    trusted_pytest.parent.mkdir()
    trusted_pytest.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    trusted_pytest.chmod(0o755)
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "base.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_base.py").write_text(
        "from src.base import VALUE\n\ndef test_value():\n    assert VALUE == 1\n",
        encoding="utf-8",
    )
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    _git(root, "add", "src", "tests")
    _git(root, "commit", "-m", "initial")
    paths = create_plan_run(root)
    plan = load_plan(paths)
    plan.update(
        {
            "project_goal": "Build a feature module.",
            "summary": "Add and verify one scoped module.",
            "requirements": ["Focused tests pass."],
            "planning_constraints": {
                "schema_version": 1,
                "target_roots": [
                    {
                        "path": "src",
                        "kind": "target_root",
                        "reason_code": "user_target_root",
                        "evidence": "The user explicitly selected src.",
                    }
                ],
                "forbidden_roots": [],
                "decoy_roots": [],
                "unrelated_roots": [],
            },
            "tasks": [
                {
                    "id": "T01",
                    "title": "Implement feature",
                    "description": "Create the scoped feature module.",
                    "acceptance_criteria": ["Focused tests pass."],
                    "dependencies": [],
                    "estimated_files": ["src/feature.py"],
                    "write_scope": ["src/feature.py"],
                    "branch": "",
                    "status": "planned",
                    "attempts": 0,
                    "task_kind": "implementation",
                    "task_kind_reason": "derived_from_mutating_language",
                }
            ],
        }
    )
    return root, paths, plan


def _request(paths: object, plan: dict[str, object]) -> NativeAdapterRequest:
    return NativeAdapterRequest(
        paths=paths,  # type: ignore[arg-type]
        plan=plan,
        planner_input="Build the scoped feature module in src/feature.py.",
        workspace_ref="workspace_abcdefgh",
        provider_binding=ProviderBinding(
            provider_binding_ref="provider_abcdefgh",
            provider="synthetic",
            model="scripted-v1",
        ),
        per_managed_task_max_steps=12,
        run_timeout_ms=60_000,
        verification_commands=["pytest -q"],
        trusted_pytest=paths.root.parent / "bin" / "pytest",  # type: ignore[attr-defined]
    )


def test_adapter_builds_lossless_authority_without_creating_git_ref(tmp_path: Path) -> None:
    root, paths, plan = _repo(tmp_path)
    refs_before = _git(root, "for-each-ref", "--format=%(refname)", "refs/heads")

    admission = build_native_plan_admission(_request(paths, plan))

    refs_after = _git(root, "for-each-ref", "--format=%(refname)", "refs/heads")
    assert refs_after == refs_before
    assert plan["tasks"][0]["branch"] == "feat/t01-implement-feature"  # type: ignore[index]
    assert load_plan(paths)["tasks"][0]["branch"] == "feat/t01-implement-feature"
    ready = admission.plan_ready
    assert plan_fingerprint(ready.plan_definition) == ready.host_plan_fingerprint
    assert (
        canonical_fingerprint(ready.native_approval_definition) == ready.native_approval_fingerprint
    )
    assert (
        verification_contract_fingerprint(ready.verification_contract)
        == ready.verification_contract.contract_fingerprint
    )
    assert (
        execution_admission_fingerprint(ready.execution_admission)
        == ready.execution_admission.fingerprint
    )
    assert admission.native_run_id == paths.run_id
    protocol_command_ids = tuple(
        command.command_id for command in ready.verification_contract.commands
    )
    native_spec_ids = tuple(spec.command_id for spec in admission.verification_specs)
    assert protocol_command_ids == native_spec_ids == ("command.pytest",)
    assert admission.verification_specs[0].provenance.value == "HOST_AUTHORITATIVE"
    assert ready.verification_contract.commands[0].provenance == "HOST_AUTHORITATIVE"
    assert admission.diagnostics[0].task_kind_reason == "derived_from_mutating_language"
    assert "task_kind_reason" not in ready.native_approval_definition.model_dump_json()


def test_adapter_does_not_promote_scan_suggestion_into_host_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _root, paths, plan = _repo(tmp_path)
    monkeypatch.setattr(
        "alysis_code.forge_host_native_adapter.scan_workspace",
        lambda **_kwargs: SimpleNamespace(likely_test_commands=["pytest -q"]),
    )
    request = replace(_request(paths, plan), verification_commands=None)
    with pytest.raises(ForgeHostPreflightError, match="not assertive authority") as rejected:
        build_native_plan_admission(request)
    assert rejected.value.code == "invalid_verification_command"


@pytest.mark.parametrize("command", ["pytest --collect-only", "pytest -q || true", "echo passed"])
def test_host_selected_verifier_still_requires_canonical_assertive_pytest(
    tmp_path: Path, command: str
) -> None:
    _root, paths, plan = _repo(tmp_path)
    with pytest.raises(ForgeHostPreflightError, match="repo-native pytest"):
        build_native_plan_admission(replace(_request(paths, plan), verification_commands=[command]))


def test_adapter_binds_long_app_planner_input_into_bounded_intent_summary(
    tmp_path: Path,
) -> None:
    _root, paths, plan = _repo(tmp_path)
    user_goal = "Build the scoped feature module in src/feature.py."
    app_contract = "\n".join(
        [
            "[Alysis Desktop execution contract]",
            *("Preserve exact scoped execution authority and evidence." for _ in range(32)),
        ]
    )
    planner_input = f"{user_goal}\n\n{app_contract}"
    assert 1000 < len(planner_input) < 4096

    admission = build_native_plan_admission(
        replace(_request(paths, plan), planner_input=planner_input)
    )
    intent = admission.plan_ready.plan_definition.intent
    digest = hashlib.sha256(planner_input.encode("utf-8")).hexdigest()

    assert intent.summary.startswith(user_goal)
    assert intent.summary.endswith(f"[full-input-sha256:{digest}]")
    assert len(intent.summary) <= 1000
    assert admission.plan_ready.native_approval_definition.intent_record.summary == intent.summary

    changed = build_native_plan_admission(
        replace(_request(paths, plan), planner_input=planner_input + "\nChanged authority.")
    )
    assert changed.plan_ready.host_plan_fingerprint != admission.plan_ready.host_plan_fingerprint
    assert (
        changed.plan_ready.native_approval_fingerprint
        != admission.plan_ready.native_approval_fingerprint
    )


def test_adapter_revalidation_detects_native_plan_or_head_change(tmp_path: Path) -> None:
    root, paths, plan = _repo(tmp_path)
    admission = build_native_plan_admission(_request(paths, plan))
    assert revalidate_native_plan_admission(admission)["run_id"] == paths.run_id

    changed = copy.deepcopy(load_plan(paths))
    changed["summary"] = "Changed after approval."
    paths.plan_json_path.write_text(__import__("json").dumps(changed), encoding="utf-8")
    with pytest.raises(ForgeHostPreflightError, match="changed after approval"):
        revalidate_native_plan_admission(admission)


def test_adapter_revalidation_rejects_branch_created_after_approval(tmp_path: Path) -> None:
    root, paths, plan = _repo(tmp_path)
    admission = build_native_plan_admission(_request(paths, plan))
    _git(root, "branch", "feat/t01-implement-feature")

    with pytest.raises(ForgeHostPreflightError, match="collides"):
        revalidate_native_plan_admission(admission)


def test_adapter_rejects_existing_ignored_workspace_material(tmp_path: Path) -> None:
    root, paths, plan = _repo(tmp_path)
    (root / ".gitignore").write_text("src/feature.py\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-m", "ignore generated feature")
    ignored_target = root / "src" / "feature.py"
    ignored_target.write_text("USER_DATA = 'must not be overwritten'\n", encoding="utf-8")
    assert _git(root, "status", "--porcelain", "--ignored", "src/feature.py").startswith("!!")

    with pytest.raises(ForgeHostPreflightError, match="non-runtime changes"):
        build_native_plan_admission(_request(paths, plan))


def test_adapter_revalidation_rejects_new_ignored_workspace_material(tmp_path: Path) -> None:
    root, paths, plan = _repo(tmp_path)
    (root / ".gitignore").write_text("src/feature.py\n", encoding="utf-8")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-m", "ignore generated feature")
    admission = build_native_plan_admission(_request(paths, plan))

    (root / "src" / "feature.py").write_text("SURPRISE = True\n", encoding="utf-8")
    with pytest.raises(ForgeHostPreflightError, match="non-runtime changes"):
        revalidate_native_plan_admission(admission)


def test_adapter_fails_closed_for_ambiguous_intent_and_ref_collision(tmp_path: Path) -> None:
    root, paths, plan = _repo(tmp_path)
    request = _request(paths, plan)
    ambiguous = NativeAdapterRequest(
        **{**request.__dict__, "planner_input": "Please handle src/feature.py."}
    )
    with pytest.raises(ForgeHostPreflightError, match="intent"):
        build_native_plan_admission(ambiguous)

    collision_plan = copy.deepcopy(plan)
    _git(root, "branch", "feat/t01-implement-feature")
    with pytest.raises(ForgeHostPreflightError, match="collides"):
        build_native_plan_admission(_request(paths, collision_plan))


def test_adapter_revalidation_rejects_replaced_pytest_executable(tmp_path: Path) -> None:
    _root, paths, plan = _repo(tmp_path)
    admission = build_native_plan_admission(_request(paths, plan))
    executable = admission.pytest_executable.resolved_path
    executable.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    executable.chmod(0o755)

    with pytest.raises(ForgeHostPreflightError, match="changed after approval"):
        revalidate_native_plan_admission(admission)


def test_concrete_host_planner_uses_real_engine_finalize_and_native_adapter(
    tmp_path: Path,
) -> None:
    _root, paths, plan = _repo(tmp_path)
    plan["project_goal"] = ""
    plan["summary"] = ""
    plan["requirements"] = []
    plan["tasks"] = []
    provider_calls = 0

    class _Knowledge:
        def render_prompt_section(self, *, workspace_root: Path) -> str:
            return f"knowledge:{workspace_root.name}"

    def synthetic_planner(**_kwargs: Any) -> PlannerTurnResult:
        nonlocal provider_calls
        provider_calls += 1
        return PlannerTurnResult(
            assistant_message="The implementation task is ready.",
            questions=[],
            plan_update={
                "project_goal": "Build a feature module.",
                "summary": "Add and verify one scoped module.",
                "requirements_append": ["Focused tests pass."],
                "tasks_add": [
                    {
                        "title": "Implement feature",
                        "description": "Create the scoped feature module.",
                        "acceptance_criteria": ["Focused tests pass."],
                        "dependencies": [],
                        "estimated_files": ["src/feature.py"],
                        "write_scope": ["src/feature.py"],
                    }
                ],
            },
        )

    planner_dependencies = ForgePlannerDependencies(
        turn_runner=synthetic_planner,
        knowledge_preparer=lambda **_kwargs: _Knowledge(),
        knowledge_root_resolver=lambda run_paths: run_paths.root,
        plan_reconciler=lambda **_kwargs: (
            PlanReconciliationResult(changed=False, warnings=[], task_updates={}),
            {},
        ),
    )
    declared_scope_kinds = {"src/feature.py": "file"}
    external_state = ForgePlannerState(cfg=SimpleNamespace())
    adapter = ForgeHostPlannerAdapter(
        paths=paths,
        plan=plan,
        state=external_state,
        settings=ForgeHostPlannerAdapterSettings(
            workspace_ref="workspace_abcdefgh",
            provider_binding=ProviderBinding(
                provider_binding_ref="provider_abcdefgh",
                provider="synthetic",
                model="planner-v1",
            ),
            per_managed_task_max_steps=12,
            run_timeout_ms=60_000,
            trusted_pytest=paths.root.parent / "bin" / "pytest",
            verification_commands=("pytest -q",),
            declared_scope_kinds=declared_scope_kinds,  # type: ignore[arg-type]
        ),
        cfg_loader=lambda: SimpleNamespace(),  # type: ignore[arg-type]
        planner_dependencies=planner_dependencies,
    )
    # Caller-owned nested inputs are detached at construction, and getters do
    # not return an authority-bearing state object.
    plan["assets"] = [{"attacker": True}]
    declared_scope_kinds["src/feature.py"] = "directory"
    external_state.transcript.append({"role": "system", "content": "attacker"})
    exposed_state = adapter.state
    exposed_state.pending_questions.append("attacker")
    identity = Identity(
        session_id="ses_abcdefgh",
        generation_id="gen_abcdefgh",
        run_id=None,
        revision=0,
    )

    outcome = adapter(
        HostPlannerRequest(
            planner_input=PlannerInput(
                type="planner.input",
                identity=identity,
                expected_revision=0,
                input=PlannerGoal(
                    kind="goal",
                    text="Build the scoped feature module in src/feature.py.",
                ),
            ),
            operation_id="operation_planner01",
            identity=identity,
        )
    )

    assert provider_calls == 1
    assert outcome.admission is not None
    assert outcome.admission.plan_ready.plan_definition.tasks[0].task_id == "T01"
    assert outcome.admission.plan_ready.plan_definition.intent.kind == "build"
    assert not adapter.state.pending_questions
    assert all(item.get("content") != "attacker" for item in adapter.state.transcript)
    assert (paths.notes_dir / "plan_validation.md").is_file()
