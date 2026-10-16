"""UI-neutral orchestration for Forge planner turns and finalization.

The planner model call itself lives in :mod:`plan_assistant`.  This module owns
the stateful, result-returning seam around that call: transcript persistence,
guarded application, reconciliation, plan status, and final validation.  CLI
and Host adapters translate the structured outcome into their own presentation
without making planner decisions themselves.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import AppConfig
from .forge import (
    ForgeError,
    RunPaths,
    append_planner_chat,
    append_planner_summary,
    append_transcript_note,
    ensure_workspace_context_artifacts,
    finalize_plan,
    save_plan,
)
from .forge_planning_profile import (
    PlanningProfile,
    clarification_round_limit,
    normalize_planning_profile,
    planning_profile_policy,
)
from .knowledge_librarian import prepare_planner_knowledge, resolve_knowledge_workspace_root
from .plan_assistant import (
    PlanApplyResult,
    PlannerTurnResult,
    apply_guarded_planner_plan_update,
    run_planner_turn,
    summarize_plan_update,
)
from .plan_reconciliation import PlanReconciliationResult, reconcile_plan_with_workspace
from .plan_repair import (
    ClarificationLoopTracker,
    PlannerRepairReport,
    apply_plan_status,
    clarification_goal_key,
    record_plan_repair,
    resolve_plan_repair_policy,
)
from .plan_validation import validate_plan


@dataclass
class ForgePlannerState:
    """State shared by every presentation adapter for one native Forge run."""

    transcript: list[dict[str, str]] = field(default_factory=list)
    cfg: AppConfig | None = None
    workspace_context: dict[str, Any] | None = None
    awaiting_clarification: bool = False
    pending_questions: list[str] = field(default_factory=list)
    clarification_loop: ClarificationLoopTracker = field(default_factory=ClarificationLoopTracker)


@dataclass(frozen=True)
class ForgePlannerWarning:
    label: str
    messages: tuple[str, ...]


@dataclass(frozen=True)
class ForgePlannerTrace:
    message: str
    warning: bool = False


@dataclass(frozen=True)
class ForgePlannerTurnRequest:
    paths: RunPaths
    plan: dict[str, Any]
    state: ForgePlannerState
    user_text: str
    cfg_loader: Callable[[], AppConfig]
    unavailable_message_builder: Callable[[Exception], str]
    api_key_override: str | None = None
    selection_label: str = "planner"
    planning_relevant: bool = True
    refresh_workspace_context: bool = False
    planning_profile: PlanningProfile = "deep"
    stream: bool = False
    on_text_delta: Callable[[str], None] | None = None


@dataclass(frozen=True)
class ForgePlannerTurnOutcome:
    status: str
    planner_result: PlannerTurnResult | Any | None = None
    summary_line: str | None = None
    assistant_message: str = ""
    questions: tuple[str, ...] = ()
    usage_events: tuple[dict[str, Any], ...] = ()
    meta_messages: tuple[str, ...] = ()
    warnings: tuple[ForgePlannerWarning, ...] = ()
    traces: tuple[ForgePlannerTrace, ...] = ()
    apply_result: PlanApplyResult | None = None
    reconciliation_result: PlanReconciliationResult | Any | None = None
    fallback_recommended: bool = False


@dataclass(frozen=True)
class ForgePlannerFinalizeRequest:
    paths: RunPaths
    plan: dict[str, Any]
    transcript_tail: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class ForgePlannerFinalizeOutcome:
    reconciliation_result: PlanReconciliationResult | Any
    validation_warnings: tuple[str, ...]
    validation_artifact_path: Path


WorkspaceContextLoader = Callable[..., dict[str, Any] | None]
PlanReconciler = Callable[..., tuple[Any, dict[str, Any] | None]]


@dataclass(frozen=True)
class ForgePlannerDependencies:
    """Injectable seams used by legacy monkeypatch adapters and focused tests."""

    turn_runner: Callable[..., PlannerTurnResult | Any] = run_planner_turn
    knowledge_preparer: Callable[..., Any] = prepare_planner_knowledge
    knowledge_root_resolver: Callable[[RunPaths], Path] = resolve_knowledge_workspace_root
    workspace_context_loader: WorkspaceContextLoader | None = None
    plan_reconciler: PlanReconciler | None = None
    guarded_apply: Callable[..., PlanApplyResult] = apply_guarded_planner_plan_update
    update_summarizer: Callable[[PlanApplyResult], str] = summarize_plan_update
    validator: Callable[[dict[str, Any]], list[str]] = validate_plan
    plan_saver: Callable[[RunPaths, dict[str, Any]], None] = save_plan
    plan_finalizer: Callable[[dict[str, Any]], dict[str, Any]] = finalize_plan
    transcript_appender: Callable[..., None] = append_transcript_note
    planner_chat_appender: Callable[..., None] = append_planner_chat
    planner_summary_appender: Callable[[RunPaths, str], None] = append_planner_summary
    repair_recorder: Callable[[dict[str, Any], PlannerRepairReport], Any] = record_plan_repair
    status_applier: Callable[..., Any] = apply_plan_status


def _default_workspace_context_loader(
    *,
    paths: RunPaths,
    refresh_if_stale: bool = False,
) -> dict[str, Any] | None:
    try:
        scan = ensure_workspace_context_artifacts(paths, refresh_if_stale=refresh_if_stale)
    except ForgeError:
        return None
    payload = scan.to_dict()
    payload["greenfield"] = bool(getattr(paths, "greenfield", False))
    return payload


def _default_plan_reconciler(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    refresh_if_stale: bool = False,
    user_text: str | None = None,
    transcript_tail: list[dict[str, Any]] | None = None,
    target_task_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[PlanReconciliationResult, dict[str, Any] | None]:
    workspace_context = _default_workspace_context_loader(
        paths=paths,
        refresh_if_stale=refresh_if_stale,
    )
    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=paths.root,
        workspace_context=workspace_context,
        user_text=user_text,
        transcript_tail=transcript_tail,
        target_task_ids=(
            {str(task_id).strip() for task_id in target_task_ids if str(task_id).strip()}
            if target_task_ids is not None
            else None
        ),
    )
    return result, workspace_context


def _clean_questions(value: Sequence[Any] | None) -> list[str]:
    return [str(question).strip() for question in value or () if str(question).strip()]


def _set_follow_up_state(
    *,
    state: ForgePlannerState,
    questions: Sequence[Any] | None,
    awaiting_clarification: bool,
) -> None:
    clean_questions = _clean_questions(questions)
    state.pending_questions = clean_questions
    state.awaiting_clarification = bool(awaiting_clarification and clean_questions)


def _sync_follow_up_state(
    *,
    state: ForgePlannerState,
    planner_result: Any,
    goal_key: str,
) -> None:
    questions = getattr(planner_result, "questions", None)
    plan_update = getattr(planner_result, "plan_update", None)
    error = str(getattr(planner_result, "error", "") or "").strip()
    awaiting = bool(not error and not plan_update)
    _set_follow_up_state(
        state=state,
        questions=questions if isinstance(questions, list) else list(questions or []),
        awaiting_clarification=awaiting,
    )
    state.clarification_loop.record(
        goal_key=goal_key,
        awaiting_clarification=awaiting and state.awaiting_clarification,
    )


def run_forge_planner_turn(
    request: ForgePlannerTurnRequest,
    *,
    dependencies: ForgePlannerDependencies | None = None,
) -> ForgePlannerTurnOutcome:
    """Run and persist one planner turn without rendering UI."""

    deps = dependencies or ForgePlannerDependencies()
    state = request.state
    plan = request.plan
    paths = request.paths
    workspace_loader = deps.workspace_context_loader or _default_workspace_context_loader
    reconciler = deps.plan_reconciler or _default_plan_reconciler
    planning_profile = normalize_planning_profile(request.planning_profile, default="deep")
    profile_policy = planning_profile_policy(planning_profile)

    state.transcript.append({"role": "user", "content": request.user_text})
    deps.planner_chat_appender(paths, role="user", message=request.user_text)

    if state.cfg is None:
        try:
            state.cfg = request.cfg_loader()
        except Exception as exc:  # noqa: BLE001 - the configured adapter owns its message
            assistant_message = request.unavailable_message_builder(exc)
            deps.transcript_appender(paths, role="assistant", message=assistant_message)
            deps.planner_chat_appender(paths, role="assistant", message=assistant_message)
            state.transcript.append({"role": "assistant", "content": assistant_message})
            deps.planner_summary_appender(paths, "no plan_update proposed")
            deps.transcript_appender(
                paths,
                role="system",
                message="Planner proposed no plan update.",
            )
            _set_follow_up_state(state=state, questions=(), awaiting_clarification=False)
            return ForgePlannerTurnOutcome(
                status="unavailable",
                assistant_message=assistant_message,
                meta_messages=(assistant_message,),
            )

    if (
        request.refresh_workspace_context
        or profile_policy.refresh_workspace_context
        or state.workspace_context is None
    ):
        state.workspace_context = workspace_loader(
            paths=paths,
            refresh_if_stale=request.refresh_workspace_context,
        )

    planner_knowledge = deps.knowledge_preparer(
        paths=paths,
        plan=plan,
        user_text=request.user_text,
        selection_label=request.selection_label,
    )
    planner_workspace_root = deps.knowledge_root_resolver(paths)
    goal_key = clarification_goal_key(plan=plan, user_text=request.user_text)
    state.clarification_loop.max_rounds = clarification_round_limit(
        resolve_plan_repair_policy(state.cfg).max_clarification_rounds,
        planning_profile,
    )
    clarification_rounds = state.clarification_loop.rounds_for(goal_key)
    planner_result = deps.turn_runner(
        cfg=state.cfg,
        api_key_override=request.api_key_override,
        plan=plan,
        transcript_tail=state.transcript,
        workspace_context=state.workspace_context,
        user_text=request.user_text,
        stream=request.stream,
        on_text_delta=request.on_text_delta if request.stream else None,
        relevant_knowledge_section=planner_knowledge.render_prompt_section(
            workspace_root=planner_workspace_root
        ),
        prefer_context="forge",
        awaiting_clarification=state.awaiting_clarification,
        pending_questions=state.pending_questions,
        clarification_rounds=clarification_rounds,
        planning_profile=planning_profile,
        run_paths=paths,
    )

    usage_events = tuple(getattr(planner_result, "usage_events", ()) or ())
    assistant_message = str(getattr(planner_result, "assistant_message", "") or "")
    questions = tuple(_clean_questions(getattr(planner_result, "questions", None)))
    deps.transcript_appender(paths, role="assistant", message=assistant_message)
    deps.planner_chat_appender(paths, role="assistant", message=assistant_message)
    state.transcript.append({"role": "assistant", "content": assistant_message})

    request_retry_count = int(getattr(planner_result, "request_retry_count", 0) or 0)
    planner_error = str(getattr(planner_result, "error", "") or "").strip()
    _sync_follow_up_state(state=state, planner_result=planner_result, goal_key=goal_key)

    traces: list[ForgePlannerTrace] = []
    warning_groups: list[ForgePlannerWarning] = []
    if planner_error:
        retry_word = "retry" if request_retry_count == 1 else "retries"
        traces.append(
            ForgePlannerTrace(
                "Planner returned an error after "
                f"{request_retry_count} transient {retry_word}; using fallback handling."
                if request_retry_count > 0
                else "Planner returned an error; using fallback handling."
            )
        )
    else:
        traces.append(ForgePlannerTrace("Planner response ready."))

    if request_retry_count > 0 and not planner_error:
        retry_word = "retry" if request_retry_count == 1 else "retries"
        retry_notice = (
            f"Planner request recovered after {request_retry_count} transient {retry_word}."
        )
        warning_groups.append(ForgePlannerWarning("Planner", (retry_notice,)))
        deps.transcript_appender(
            paths,
            role="system",
            message=f"Planner warning: {retry_notice}",
        )
        traces.append(ForgePlannerTrace(retry_notice, warning=True))

    if questions:
        deps.transcript_appender(
            paths,
            role="system",
            message=f"Planner questions: {'; '.join(questions)}",
        )

    if planner_error:
        retry_word = "retry" if request_retry_count == 1 else "retries"
        summary_line = (
            f"planner error after {request_retry_count} transient {retry_word}: {planner_error}"
            if request_retry_count > 0
            else f"planner error: {planner_error}"
        )
        note = (
            f"Planner error after {request_retry_count} transient {retry_word}: {planner_error}"
            if request_retry_count > 0
            else f"Planner error: {planner_error}"
        )
        warnings = (
            [f"Planner request failed after {request_retry_count} transient {retry_word}."]
            if request_retry_count > 0
            else []
        )
        warnings.append(f"Final planner error: {planner_error}")
        warning_groups.append(ForgePlannerWarning("Planner", tuple(warnings)))
        deps.planner_summary_appender(paths, summary_line)
        deps.transcript_appender(paths, role="system", message=note)
        return ForgePlannerTurnOutcome(
            status="error",
            planner_result=planner_result,
            summary_line=summary_line,
            assistant_message=assistant_message,
            questions=questions,
            usage_events=usage_events,
            warnings=tuple(warning_groups),
            traces=tuple(traces),
            fallback_recommended=True,
        )

    raw_plan_update = getattr(planner_result, "plan_update", None)
    if raw_plan_update:
        if not request.planning_relevant:
            deps.planner_summary_appender(
                paths,
                "plan_update ignored: message not planning-relevant",
            )
            deps.transcript_appender(
                paths,
                role="system",
                message=(
                    "Planner update ignored because the user message was not planning-relevant."
                ),
            )
            return ForgePlannerTurnOutcome(
                status="ignored",
                planner_result=planner_result,
                assistant_message=assistant_message,
                questions=questions,
                usage_events=usage_events,
                meta_messages=(
                    "Planner update ignored because this message did not look "
                    "planning-related; plan unchanged.",
                ),
                traces=tuple(traces),
            )

        apply_result = deps.guarded_apply(
            plan,
            raw_plan_update,
            latest_user_text=request.user_text,
            workspace_context=state.workspace_context,
        )
        summary_line = deps.update_summarizer(apply_result)
        reconciliation_result: Any | None = None
        if apply_result.changed:
            target_ids = list(
                dict.fromkeys([*apply_result.added_task_ids, *apply_result.updated_task_ids])
            )
            reconciliation_kwargs: dict[str, Any] = {
                "paths": paths,
                "plan": plan,
                "refresh_if_stale": False,
                "user_text": request.user_text,
                "transcript_tail": state.transcript,
            }
            if target_ids:
                reconciliation_kwargs["target_task_ids"] = target_ids
            reconciliation_result, state.workspace_context = reconciler(**reconciliation_kwargs)

        deps.repair_recorder(
            plan,
            getattr(planner_result, "repair", None) or PlannerRepairReport(),
        )
        deps.status_applier(plan, validation_warnings=deps.validator(plan))
        deps.plan_saver(paths, plan)

        meta = (
            "Applied planner update to plan."
            if apply_result.changed
            else "Planner update contained no applicable changes."
        )
        traces.append(
            ForgePlannerTrace(
                "Applied planner update to the Forge plan."
                if apply_result.changed
                else "Planner update was a no-op.",
                warning=not apply_result.changed,
            )
        )
        if apply_result.warnings:
            warning_groups.append(
                ForgePlannerWarning("Planner", tuple(str(item) for item in apply_result.warnings))
            )
        reconciliation_warnings = tuple(
            str(item) for item in getattr(reconciliation_result, "warnings", ()) or ()
        )
        if reconciliation_warnings:
            warning_groups.append(
                ForgePlannerWarning("Plan reconciliation", reconciliation_warnings)
            )
        deps.planner_summary_appender(paths, summary_line)
        deps.transcript_appender(
            paths,
            role="system",
            message=f"Planner update: {summary_line}",
        )
        for warning in apply_result.warnings:
            deps.transcript_appender(
                paths,
                role="system",
                message=f"Planner warning: {warning}",
            )
        for warning in reconciliation_warnings:
            deps.transcript_appender(
                paths,
                role="system",
                message=f"Plan reconciliation warning: {warning}",
            )
        return ForgePlannerTurnOutcome(
            status="applied" if apply_result.changed else "noop",
            planner_result=planner_result,
            summary_line=summary_line,
            assistant_message=assistant_message,
            questions=questions,
            usage_events=usage_events,
            meta_messages=(meta,),
            warnings=tuple(warning_groups),
            traces=tuple(traces),
            apply_result=apply_result,
            reconciliation_result=reconciliation_result,
        )

    deps.planner_summary_appender(paths, "no plan_update proposed")
    deps.transcript_appender(
        paths,
        role="system",
        message="Planner proposed no plan update.",
    )
    return ForgePlannerTurnOutcome(
        status="no_update",
        planner_result=planner_result,
        assistant_message=assistant_message,
        questions=questions,
        usage_events=usage_events,
        traces=tuple(traces),
    )


def write_plan_validation_artifact(
    *,
    paths: RunPaths,
    reconciliation_result: Any,
    validation_warnings: Sequence[str],
) -> Path:
    """Write the stable planner validation artifact and return its path."""

    validation_path = paths.notes_dir / "plan_validation.md"
    lines: list[str] = ["# Plan Validation", "", "## Reconciliation", ""]
    changed = bool(getattr(reconciliation_result, "changed", False))
    updated_task_ids = list(getattr(reconciliation_result, "updated_task_ids", ()) or ())
    reconciliation_warnings = list(getattr(reconciliation_result, "warnings", ()) or ())
    lines.append(f"- Changed: `{'yes' if changed else 'no'}`")
    lines.append(
        f"- Updated Tasks: {', '.join(updated_task_ids)}"
        if updated_task_ids
        else "- Updated Tasks: (none)"
    )
    if reconciliation_warnings:
        lines.append("- Warnings:")
        lines.extend(f"  - {warning}" for warning in reconciliation_warnings)
    else:
        lines.append("- No reconciliation warnings.")
    lines.extend(["", "## Validation", ""])
    if validation_warnings:
        lines.extend(f"- {warning}" for warning in validation_warnings)
    else:
        lines.append("- No validation warnings.")
    validation_path.parent.mkdir(parents=True, exist_ok=True)
    validation_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return validation_path


def finalize_forge_planner_plan(
    request: ForgePlannerFinalizeRequest,
    *,
    dependencies: ForgePlannerDependencies | None = None,
    path_validator: Callable[[RunPaths, dict[str, Any]], list[str]] | None = None,
    artifact_writer: Callable[..., Path] = write_plan_validation_artifact,
) -> ForgePlannerFinalizeOutcome:
    """Finalize, reconcile, validate, and persist a Forge plan without UI work."""

    deps = dependencies or ForgePlannerDependencies()
    reconciler = deps.plan_reconciler or _default_plan_reconciler
    deps.plan_finalizer(request.plan)
    reconciliation_result, _ = reconciler(
        paths=request.paths,
        plan=request.plan,
        refresh_if_stale=True,
        transcript_tail=request.transcript_tail,
    )
    deps.plan_saver(request.paths, request.plan)
    validation_warnings = (
        path_validator(request.paths, request.plan)
        if path_validator is not None
        else deps.validator(request.plan)
    )
    for warning in getattr(reconciliation_result, "warnings", ()) or ():
        deps.transcript_appender(
            request.paths,
            role="system",
            message=f"Plan reconciliation warning: {warning}",
        )
    for warning in validation_warnings:
        deps.transcript_appender(
            request.paths,
            role="system",
            message=f"Plan validation warning: {warning}",
        )
    artifact_path = artifact_writer(
        paths=request.paths,
        reconciliation_result=reconciliation_result,
        validation_warnings=validation_warnings,
    )
    return ForgePlannerFinalizeOutcome(
        reconciliation_result=reconciliation_result,
        validation_warnings=tuple(validation_warnings),
        validation_artifact_path=artifact_path,
    )


__all__ = [
    "ForgePlannerDependencies",
    "ForgePlannerFinalizeOutcome",
    "ForgePlannerFinalizeRequest",
    "ForgePlannerState",
    "ForgePlannerTrace",
    "ForgePlannerTurnOutcome",
    "ForgePlannerTurnRequest",
    "ForgePlannerWarning",
    "finalize_forge_planner_plan",
    "run_forge_planner_turn",
    "write_plan_validation_artifact",
]
