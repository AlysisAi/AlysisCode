from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from alysis_code.forge_planner_engine import (
    ForgePlannerDependencies,
    ForgePlannerFinalizeRequest,
    ForgePlannerState,
    ForgePlannerTurnRequest,
    finalize_forge_planner_plan,
    run_forge_planner_turn,
)
from alysis_code.plan_assistant import PlanApplyResult, PlannerTurnResult
from alysis_code.plan_reconciliation import PlanReconciliationResult


class _Knowledge:
    def render_prompt_section(self, *, workspace_root: Any) -> str:
        return f"knowledge:{workspace_root}"


def _paths(tmp_path: Any) -> Any:
    notes = tmp_path / "notes"
    notes.mkdir()
    return SimpleNamespace(root=tmp_path, notes_dir=notes, greenfield=False)


def test_planner_engine_applies_reconciles_and_persists_without_ui(tmp_path: Any) -> None:
    paths = _paths(tmp_path)
    plan: dict[str, Any] = {"tasks": []}
    state = ForgePlannerState(cfg=SimpleNamespace())
    calls: list[tuple[str, Any]] = []

    def turn_runner(**kwargs: Any) -> PlannerTurnResult:
        assert kwargs["relevant_knowledge_section"] == f"knowledge:{tmp_path}"
        assert kwargs["user_text"] == "Add src/feature.py"
        assert kwargs["planning_profile"] == "deep"
        return PlannerTurnResult(
            assistant_message="I drafted the task.",
            questions=[],
            plan_update={"tasks_add": [{"title": "Feature"}]},
            usage_events=[{"kind": "usage"}],
        )

    def guarded_apply(plan: dict[str, Any], _update: Any, **_kwargs: Any) -> PlanApplyResult:
        plan["tasks"] = [{"id": "T01"}]
        return PlanApplyResult(
            changed=True,
            warnings=["normalized scope"],
            added_task_ids=["T01"],
            removed_task_ids=[],
            updated_task_ids=[],
            requirements_added=0,
            goal_updated=False,
            summary_updated=False,
        )

    def reconcile(**kwargs: Any) -> tuple[PlanReconciliationResult, dict[str, Any]]:
        assert kwargs["target_task_ids"] == ["T01"]
        return (
            PlanReconciliationResult(
                changed=True,
                warnings=["reconciled scope"],
                task_updates={"T01": {"write_scope": ["src/feature.py"]}},
            ),
            {"workspace": "fresh"},
        )

    deps = ForgePlannerDependencies(
        turn_runner=turn_runner,
        knowledge_preparer=lambda **_kwargs: _Knowledge(),
        knowledge_root_resolver=lambda _paths: tmp_path,
        workspace_context_loader=lambda **_kwargs: {"workspace": "initial"},
        plan_reconciler=reconcile,
        guarded_apply=guarded_apply,
        update_summarizer=lambda _result: "added T01",
        validator=lambda _plan: [],
        plan_saver=lambda _paths, _plan: calls.append(("save", len(_plan["tasks"]))),
        transcript_appender=lambda _paths, **kwargs: calls.append(
            (f"transcript:{kwargs['role']}", kwargs["message"])
        ),
        planner_chat_appender=lambda _paths, **kwargs: calls.append(
            (f"chat:{kwargs['role']}", kwargs["message"])
        ),
        planner_summary_appender=lambda _paths, message: calls.append(("summary", message)),
        repair_recorder=lambda _plan, _repair: calls.append(("repair", True)),
        status_applier=lambda _plan, **_kwargs: calls.append(("status", True)),
    )

    outcome = run_forge_planner_turn(
        ForgePlannerTurnRequest(
            paths=paths,
            plan=plan,
            state=state,
            user_text="Add src/feature.py",
            cfg_loader=lambda: SimpleNamespace(),
            unavailable_message_builder=str,
        ),
        dependencies=deps,
    )

    assert outcome.status == "applied"
    assert outcome.usage_events == ({"kind": "usage"},)
    assert [warning.label for warning in outcome.warnings] == [
        "Planner",
        "Plan reconciliation",
    ]
    assert state.workspace_context == {"workspace": "fresh"}
    assert state.transcript == [
        {"role": "user", "content": "Add src/feature.py"},
        {"role": "assistant", "content": "I drafted the task."},
    ]
    assert ("save", 1) in calls
    assert ("summary", "added T01") in calls


def test_planner_engine_propagates_quick_profile_and_caps_clarification(tmp_path: Any) -> None:
    paths = _paths(tmp_path)
    state = ForgePlannerState(cfg=SimpleNamespace())
    observed: dict[str, Any] = {}

    def turn_runner(**kwargs: Any) -> PlannerTurnResult:
        observed.update(kwargs)
        return PlannerTurnResult(
            assistant_message="One blocker remains.",
            questions=["Which API version?"],
            plan_update=None,
        )

    outcome = run_forge_planner_turn(
        ForgePlannerTurnRequest(
            paths=paths,
            plan={"tasks": []},
            state=state,
            user_text="Build the API",
            cfg_loader=lambda: SimpleNamespace(),
            unavailable_message_builder=str,
            planning_profile="quick",
        ),
        dependencies=ForgePlannerDependencies(
            turn_runner=turn_runner,
            knowledge_preparer=lambda **_kwargs: _Knowledge(),
            knowledge_root_resolver=lambda _paths: tmp_path,
            workspace_context_loader=lambda **_kwargs: {},
            transcript_appender=lambda *_args, **_kwargs: None,
            planner_chat_appender=lambda *_args, **_kwargs: None,
            planner_summary_appender=lambda *_args, **_kwargs: None,
        ),
    )

    assert outcome.questions == ("Which API version?",)
    assert observed["planning_profile"] == "quick"
    assert state.clarification_loop.max_rounds <= 1


def test_planner_engine_returns_structured_error_and_fallback_signal(tmp_path: Any) -> None:
    paths = _paths(tmp_path)
    state = ForgePlannerState(cfg=SimpleNamespace())
    notes: list[str] = []
    deps = ForgePlannerDependencies(
        turn_runner=lambda **_kwargs: PlannerTurnResult(
            assistant_message="Planner unavailable.",
            questions=[],
            plan_update=None,
            error="provider timeout",
            request_retry_count=2,
        ),
        knowledge_preparer=lambda **_kwargs: _Knowledge(),
        knowledge_root_resolver=lambda _paths: tmp_path,
        workspace_context_loader=lambda **_kwargs: {},
        transcript_appender=lambda _paths, **kwargs: notes.append(kwargs["message"]),
        planner_chat_appender=lambda *_args, **_kwargs: None,
        planner_summary_appender=lambda _paths, message: notes.append(message),
    )

    outcome = run_forge_planner_turn(
        ForgePlannerTurnRequest(
            paths=paths,
            plan={"tasks": []},
            state=state,
            user_text="Build feature",
            cfg_loader=lambda: SimpleNamespace(),
            unavailable_message_builder=str,
        ),
        dependencies=deps,
    )

    assert outcome.status == "error"
    assert outcome.fallback_recommended is True
    assert outcome.summary_line == "planner error after 2 transient retries: provider timeout"
    assert outcome.warnings[0].messages[-1] == "Final planner error: provider timeout"
    assert any("provider timeout" in note for note in notes)


def test_finalize_engine_writes_validation_artifact_and_records_warnings(tmp_path: Any) -> None:
    paths = _paths(tmp_path)
    plan: dict[str, Any] = {"tasks": [{"id": "T01"}]}
    notes: list[str] = []
    saved: list[bool] = []
    reconciliation = PlanReconciliationResult(
        changed=False,
        warnings=["scope diagnostic"],
        task_updates={},
    )
    deps = ForgePlannerDependencies(
        plan_reconciler=lambda **_kwargs: (reconciliation, {}),
        plan_finalizer=lambda value: value.update({"finalized": True}) or value,
        plan_saver=lambda _paths, _plan: saved.append(True),
        transcript_appender=lambda _paths, **kwargs: notes.append(kwargs["message"]),
    )

    outcome = finalize_forge_planner_plan(
        ForgePlannerFinalizeRequest(paths=paths, plan=plan),
        dependencies=deps,
        path_validator=lambda _paths, _plan: ["missing criterion"],
    )

    assert plan["finalized"] is True
    assert saved == [True]
    assert outcome.validation_warnings == ("missing criterion",)
    assert outcome.validation_artifact_path.read_text(encoding="utf-8").startswith(
        "# Plan Validation\n"
    )
    assert notes == [
        "Plan reconciliation warning: scope diagnostic",
        "Plan validation warning: missing criterion",
    ]
