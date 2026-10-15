# ruff: noqa: F401,F403,F405,I001
# Legacy split module: dependencies are synced by cli_surface.py.
from __future__ import annotations

from ...forge_planner_engine import (
    ForgePlannerDependencies,
    ForgePlannerFinalizeRequest,
    ForgePlannerTurnRequest,
    finalize_forge_planner_plan,
    run_forge_planner_turn,
)
from .cli_common import *
from .forge_asset_view import forge_asset_view_count


def _planning_help_panel() -> Panel:
    rows = [
        ("/help", "show planning commands"),
        ("/goal <text>", "set project goal"),
        (
            "/task <title>",
            "add a task; ambiguous/mutating work must name repo-relative file paths",
        ),
        ("/done", "save + validate the plan, then return to chat"),
    ]
    if _patchable("_is_narrow_terminal", _is_narrow_terminal)():
        lines = [f"{cmd} - {desc}" for cmd, desc in rows]
        lines.append("")
        for footer_line in _forge_help_footer_lines():
            lines.append(f"[dim]{footer_line}[/dim]")
        text = "\n".join(lines)
        return _Panel(text, title="Planning Help", border_style="bright_black")
    table = _Table(
        show_header=False,
        box=None,
        expand=True,
        padding=(0, 1),
        collapse_padding=True,
    )
    table.add_column("command", style=STYLE_EMPHASIS, no_wrap=True, ratio=2)
    table.add_column("description", style="dim", no_wrap=False, ratio=5, overflow="fold")
    for cmd, desc in rows:
        table.add_row(cmd, desc)
    content = _table_grid(expand=True)
    content.add_row(table)
    content.add_row("")
    for footer_line in _forge_help_footer_lines():
        content.add_row(_forge_bar_text(text=footer_line, style="dim"))
    return _Panel(content, title="Planning Help", border_style="bright_black")


def _forge_help_panel() -> Panel:
    return _chat_commands_panel(ui_mode="forge")


def _forge_enter_panel(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    entry_kind: str,
    assistant_enabled: bool,
    workspace_summary_lines: list[str] | None = None,
    model: str | None = None,
    mode: str | None = None,
) -> Any:
    from rich.console import Group

    _ = workspace_summary_lines
    task_count = len(plan.get("tasks") or [])
    task_label = "task" if task_count == 1 else "tasks"
    assistant_label = "assistant on" if assistant_enabled else "assistant off"
    lines = [
        _forge_bar_text(
            text=f"Forge ready · run {paths.run_id} · {task_count} {task_label} · {assistant_label}",
            style=STYLE_EMPHASIS,
        ),
    ]
    context_bits = []
    if str(model or "").strip():
        context_bits.append(f"model {str(model).strip()}")
    if str(mode or "").strip():
        context_bits.append(f"mode {str(mode).strip()}")
    if context_bits:
        lines.append(_forge_bar_text(text=" · ".join(context_bits), style="dim"))
    lines.append(
        _forge_bar_text(
            text=_forge_entry_status_text(entry_kind=entry_kind),
            style="dim",
        )
    )
    lines.append(
        _forge_bar_text(
            text="/show for summary · /execute plan when ready · /help for commands",
            style="dim",
        )
    )
    return Group(*lines)


def _forge_requirement_text(item: Any) -> str:
    return requirement_text(item)


def _forge_has_nonempty_requirement(plan: dict[str, Any]) -> bool:
    requirements = plan.get("requirements") or []
    if not isinstance(requirements, list):
        return False
    return any(bool(_forge_requirement_text(req)) for req in requirements)


def _forge_has_usable_plan_input(plan: dict[str, Any]) -> bool:
    tasks = plan.get("tasks") or []
    has_tasks = isinstance(tasks, list) and len(tasks) > 0
    return has_tasks or _forge_has_nonempty_requirement(plan)


def _forge_no_execution_ready_tasks_message(plan: dict[str, Any]) -> str | None:
    tasks = plan.get("tasks") or []
    if isinstance(tasks, list) and tasks:
        return None
    requirements = plan.get("requirements") or []
    if not isinstance(requirements, list):
        return None
    if not any(_forge_requirement_text(req) for req in requirements):
        return None
    if any(requirement_is_execution_ready(req) for req in requirements):
        return None
    return (
        "Execution blocked: planner fallback captured requirements, but no execution-ready "
        "tasks exist. Re-run the planner after the model/API is available, or add scoped "
        "tasks with repo-relative file paths before executing."
    )


def _validate_forge_plan_shape(candidate: Any) -> str | None:
    if not isinstance(candidate, dict):
        return "Plan JSON must be an object."
    for key in ("tasks", "assets", "requirements"):
        if key in candidate and not isinstance(candidate.get(key), list):
            return f"Invalid plan JSON: '{key}' must be an array."
    return None


def _validate_forge_plan_for_paths(paths: RunPaths, plan: dict[str, Any]) -> list[str]:
    warnings = _patchable("validate_plan", validate_plan)(plan)
    if not hasattr(paths, "assets_index_path"):
        return warnings
    try:
        cfg = load_config()
        max_primary = int(cfg.assets.planner.max_primary_per_task)
    except Exception as e:  # noqa: BLE001
        warnings.append(f"Asset validation config unavailable; using default limit: {e}")
        max_primary = 8
    try:
        warnings.extend(
            validate_plan_against_assets(
                plan,
                AssetIndex(paths),
                max_primary_per_task=max_primary,
            )
        )
    except Exception as e:  # noqa: BLE001
        warnings.append(f"Asset validation skipped: {e}")
        return warnings
    return warnings


def _edit_forge_plan_json(
    *,
    console: Console,
    paths: RunPaths,
    forge_state: _ForgeChatState,
) -> None:
    current_plan = forge_state.plan
    if current_plan is None:
        _print_forge_warning_messages(
            console=console,
            label="Plan edit",
            warnings=["No Forge plan loaded."],
        )
        return

    save_plan(paths, current_plan)
    backup_path = paths.plan_json_path.with_suffix(".json.bak")
    try:
        backup_path.write_text(paths.plan_json_path.read_text(encoding="utf-8"), encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _print_forge_warning_messages(
            console=console,
            label="Plan edit",
            warnings=[f"Backup warning: {e}"],
        )

    try:
        edited_text = typer.edit(filename=os.fspath(paths.plan_json_path), extension=".json")
    except TypeError:
        edited_text = typer.edit(os.fspath(paths.plan_json_path), extension=".json")
    except Exception as e:  # noqa: BLE001
        _print_forge_warning_messages(
            console=console,
            label="Plan edit",
            warnings=[f"Editor unavailable: {e}"],
        )
        _print_forge_meta(console=console, message=f"Edit directly · {paths.plan_json_path}")
        return

    if edited_text is None:
        _print_forge_meta(console=console, message="Plan edit canceled.")
        return

    try:
        edited_raw = paths.plan_json_path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _print_forge_error(console=console, message=f"Failed to read updated plan file: {e}")
        return

    try:
        candidate = json.loads(edited_raw)
    except json.JSONDecodeError as e:
        _print_forge_error(console=console, message=f"Invalid JSON: {e}")
        _print_forge_meta(console=console, message="Current in-memory plan was not changed.")
        try:
            if backup_path.exists():
                paths.plan_json_path.write_text(
                    backup_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
        except Exception as restore_error:  # noqa: BLE001
            _print_forge_warning_messages(
                console=console,
                label="Plan edit",
                warnings=[f"Failed to restore previous plan JSON: {restore_error}"],
            )
        return

    shape_error = _validate_forge_plan_shape(candidate)
    if shape_error:
        _print_forge_error(console=console, message=shape_error)
        _print_forge_meta(console=console, message="Current in-memory plan was not changed.")
        try:
            if backup_path.exists():
                paths.plan_json_path.write_text(
                    backup_path.read_text(encoding="utf-8"), encoding="utf-8"
                )
        except Exception as restore_error:  # noqa: BLE001
            _print_forge_warning_messages(
                console=console,
                label="Plan edit",
                warnings=[f"Failed to restore previous plan JSON: {restore_error}"],
            )
        return

    assert isinstance(candidate, dict)
    validation_warnings = _validate_forge_plan_for_paths(paths, candidate)
    save_plan(paths, candidate)
    forge_state.plan = candidate
    _print_forge_meta(console=console, message=f"Updated plan · {paths.plan_json_path}")
    if validation_warnings:
        _print_forge_warning_messages(
            console=console,
            label="Plan validation",
            warnings=validation_warnings,
        )


def _show_forge_plan_summary(*, console: Console, paths: RunPaths, plan: dict[str, Any]) -> None:
    goal = str(plan.get("project_goal") or "").strip() or "(not set)"
    summary = str(plan.get("summary") or "").strip() or "(not set)"
    tasks = plan.get("tasks") or []
    task_count = len(tasks) if isinstance(tasks, list) else 0
    asset_count = forge_asset_view_count(paths, plan)
    _print_forge_meta(
        console=console,
        message=f"Run {paths.run_id} · {task_count} tasks · {asset_count} assets",
        style=STYLE_EMPHASIS,
    )
    _print_forge_meta(console=console, message=f"Goal · {goal}", style=STYLE_CONTENT)
    _print_forge_meta(console=console, message=f"Summary · {summary}", style=STYLE_CONTENT)
    requirements = plan.get("requirements") or []
    req_count = len(requirements) if isinstance(requirements, list) else 0
    _print_forge_meta(console=console, message=f"Requirements · {req_count}", style="dim")
    if req_count:
        for idx, requirement in enumerate(requirements[:8], start=1):
            req_text = _forge_requirement_text(requirement)
            if not req_text:
                continue
            if len(req_text) > 120:
                req_text = req_text[:117].rstrip() + "..."
            _print_forge_meta(console=console, message=f"{idx}. {req_text}", style=STYLE_CONTENT)
        if req_count > 8:
            _print_forge_meta(
                console=console,
                message=f"... ({req_count - 8} more requirements)",
                style="dim",
            )

    table = _forge_task_table()
    table.add_column("id", style="dim", no_wrap=True, ratio=1)
    table.add_column("status", style="dim", no_wrap=True, ratio=1)
    table.add_column("title", style=STYLE_CONTENT, no_wrap=False, ratio=4, overflow="fold")
    table.add_column("dependencies", style="dim", no_wrap=False, ratio=2, overflow="fold")
    table.add_column("mcp", style="dim", no_wrap=False, ratio=2, overflow="fold")
    if tasks:
        for task in tasks:
            deps = task.get("dependencies") or []
            deps_label = ", ".join(str(dep) for dep in deps) if deps else "-"
            table.add_row(
                str(task.get("id") or "-"),
                _forge_task_status_markup(str(task.get("status") or "planned")),
                str(task.get("title") or "-"),
                deps_label,
                _forge_task_mcp_summary_label(task if isinstance(task, dict) else {}),
            )
    else:
        table.add_row("-", "-", "(no tasks yet)", "-", "off")
    console.print(table)
    _print_forge_meta(console=console, message=f"Assets · {asset_count}", style="dim")


def _show_forge_plan_markdown(
    *,
    console: Console,
    paths: RunPaths,
    plan: dict[str, Any],
) -> None:
    # Ensure PLAN.md reflects in-memory plan changes before showing it.
    save_plan(paths, plan)
    plan_md_path = paths.plan_md_path
    try:
        markdown_text = plan_md_path.read_text(encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        _print_forge_error(console=console, message=f"Failed to read PLAN.md: {e}")
        return

    lines = markdown_text.splitlines()
    preview_limit = 200
    console.print(f"PLAN.md: {plan_md_path}")

    if _patchable("_is_interactive_terminal", _is_interactive_terminal)():
        try:
            # Use plain-text pager output for maximum terminal compatibility.
            pager_text = f"Press q to exit this view.\n\n{markdown_text}"
            with console.pager(styles=False):
                console.print(pager_text, markup=False, highlight=False)
            return
        except KeyboardInterrupt:
            console.print("")
            _print_forge_meta(console=console, message="Closed PLAN.md pager.")
            return
        except Exception as e:  # noqa: BLE001
            _print_forge_warning_messages(
                console=console,
                label="PLAN.md",
                warnings=[f"Pager unavailable: {e}", "Showing preview instead."],
            )

    preview = "\n".join(lines[:preview_limit]).rstrip()
    if preview:
        console.print(preview, markup=False, highlight=False)
    else:
        console.print("(PLAN.md is empty)")
    if len(lines) > preview_limit:
        console.print(
            f"[dim]... ({len(lines) - preview_limit} more lines in {plan_md_path.name})[/dim]"
        )


def _forge_enrich_plan_enabled() -> bool:
    raw = env_get("ALYSIS_FORGE_ENRICH_PLAN")
    if raw is not None:
        parsed = _parse_bool_text(raw)
        return bool(parsed) if parsed is not None else False
    return not _is_non_interactive_terminal()


def _forge_should_try_enrichment(validation_warnings: list[str]) -> bool:
    target_tokens = (
        "missing acceptance_criteria",
        "missing estimated_files",
        EXECUTION_UNREADY_SCOPE_WARNING.casefold(),
    )
    lowered = [str(item).casefold() for item in validation_warnings]
    return any(any(token in warning for token in target_tokens) for warning in lowered)


def _set_forge_planner_follow_up_state(
    *,
    planner_state: _ForgePlannerSessionState,
    questions: list[str] | None,
    awaiting_clarification: bool,
) -> None:
    clean_questions = [
        str(question).strip() for question in questions or [] if str(question).strip()
    ]
    planner_state.pending_questions = clean_questions
    planner_state.awaiting_clarification = bool(awaiting_clarification and clean_questions)


def _sync_forge_planner_follow_up_state_from_result(
    *,
    planner_state: _ForgePlannerSessionState,
    planner_result: Any,
    goal_key: str | None = None,
) -> None:
    questions = getattr(planner_result, "questions", None)
    plan_update = getattr(planner_result, "plan_update", None)
    error = str(getattr(planner_result, "error", "") or "").strip()
    awaiting = bool(not error and not plan_update)
    _set_forge_planner_follow_up_state(
        planner_state=planner_state,
        questions=questions if isinstance(questions, list) else list(questions or []),
        awaiting_clarification=awaiting,
    )
    if goal_key is None:
        return
    tracker = getattr(planner_state, "clarification_loop", None)
    if tracker is None:
        return
    # A turn that produced a plan update resets the streak; only a genuine
    # question-and-nothing-else turn counts toward the cap.
    tracker.record(
        goal_key=goal_key,
        awaiting_clarification=awaiting and bool(planner_state.awaiting_clarification),
    )


def _workspace_context_payload_for_paths(
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
    return _augment_workspace_context_with_mcp_execution_context(
        workspace_context=payload,
        workspace_root=paths.root,
    )


def _reconcile_plan_for_paths(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    refresh_if_stale: bool = False,
    user_text: str | None = None,
    transcript_tail: list[dict[str, Any]] | None = None,
    target_task_ids: list[str] | tuple[str, ...] | None = None,
) -> tuple[Any, dict[str, Any] | None]:
    workspace_context = _workspace_context_payload_for_paths(
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


@dataclass(frozen=True)
class _ForgePlannerTurnControllerResult:
    status: str
    planner_result: Any | None = None
    summary_line: str | None = None


@dataclass(frozen=True)
class _ForgePlannerUpdateSanitizationResult:
    plan_update: dict[str, Any] | None
    warnings: list[str]


def _sanitize_forge_enrichment_plan_update(
    *,
    plan: dict[str, Any],
    plan_update: dict[str, Any] | None,
) -> _ForgePlannerUpdateSanitizationResult:
    if not isinstance(plan_update, dict):
        return _ForgePlannerUpdateSanitizationResult(plan_update=None, warnings=[])

    warnings: list[str] = []
    allowed_top_level = {"tasks_update"}
    dropped_top_level = sorted(set(plan_update) - allowed_top_level)
    if dropped_top_level:
        warnings.append(
            "Ignored plan enrichment fields outside narrow execution-readiness scope: "
            + ", ".join(dropped_top_level)
            + "."
        )

    tasks_by_id = {
        str(task.get("id") or "").strip(): task
        for task in plan.get("tasks") or []
        if isinstance(task, dict) and str(task.get("id") or "").strip()
    }
    raw_updates = plan_update.get("tasks_update")
    if not isinstance(raw_updates, list):
        return _ForgePlannerUpdateSanitizationResult(plan_update=None, warnings=warnings)

    sanitized_updates: list[dict[str, Any]] = []
    for raw_patch in raw_updates:
        if not isinstance(raw_patch, dict):
            warnings.append("Ignored non-object plan enrichment tasks_update entry.")
            continue
        task_id = str(raw_patch.get("id") or "").strip()
        if not task_id:
            warnings.append("Ignored plan enrichment tasks_update entry without an id.")
            continue
        current_task = tasks_by_id.get(task_id)
        sanitized_patch: dict[str, Any] = {"id": task_id}

        if "acceptance_criteria" in raw_patch:
            current_acceptance = (
                list(current_task.get("acceptance_criteria") or [])
                if isinstance(current_task, dict)
                else []
            )
            new_acceptance = list(raw_patch.get("acceptance_criteria") or [])
            if current_acceptance:
                warnings.append(
                    f"Ignored plan enrichment acceptance_criteria update for {task_id} "
                    "because that field is already populated."
                )
            elif new_acceptance:
                sanitized_patch["acceptance_criteria"] = new_acceptance
            else:
                warnings.append(
                    f"Ignored empty plan enrichment acceptance_criteria update for {task_id}."
                )

        if "estimated_files" in raw_patch:
            current_estimated_files = (
                list(current_task.get("estimated_files") or [])
                if isinstance(current_task, dict)
                else []
            )
            new_estimated_files = list(raw_patch.get("estimated_files") or [])
            if current_estimated_files:
                warnings.append(
                    f"Ignored plan enrichment estimated_files update for {task_id} "
                    "because that field is already populated."
                )
            elif new_estimated_files:
                sanitized_patch["estimated_files"] = new_estimated_files
            else:
                warnings.append(
                    f"Ignored empty plan enrichment estimated_files update for {task_id}."
                )

        dropped_patch_fields = sorted(
            set(raw_patch)
            - {
                "id",
                "acceptance_criteria",
                "estimated_files",
            }
        )
        if dropped_patch_fields:
            warnings.append(
                f"Ignored plan enrichment tasks_update fields for {task_id}: "
                + ", ".join(dropped_patch_fields)
                + "."
            )

        if len(sanitized_patch) > 1:
            sanitized_updates.append(sanitized_patch)
        else:
            warnings.append(
                f"Ignored plan enrichment tasks_update for {task_id} because it did not add "
                "missing execution-readiness fields."
            )

    sanitized_update = {"tasks_update": sanitized_updates} if sanitized_updates else None
    return _ForgePlannerUpdateSanitizationResult(
        plan_update=sanitized_update,
        warnings=list(dict.fromkeys(warnings)),
    )


def _run_forge_planner_turn_controller(
    *,
    console: Console,
    paths: RunPaths,
    plan: dict[str, Any],
    planner_state: _ForgePlannerSessionState,
    user_text: str,
    cfg_loader: Callable[[], AppConfig],
    unavailable_message_builder: Callable[[Exception], str],
    emit_meta: Callable[[str], None],
    emit_warning_group: Callable[[str, list[str]], None],
    api_key_override: str | None,
    render_reply: Callable[[str, list[str] | None], None],
    selection_label: str = "planner",
    planning_relevant: bool = True,
    refresh_workspace_context: bool = False,
    trace_callback: Callable[[str, bool], None] | None = None,
    stream: bool = False,
    on_text_delta: Callable[[str], None] | None = None,
    on_usage_events: Callable[[list[dict[str, Any]]], None] | None = None,
    error_fallback: Callable[[], None] | None = None,
) -> _ForgePlannerTurnControllerResult:
    dependencies = ForgePlannerDependencies(
        turn_runner=_patchable("run_planner_turn", run_planner_turn),
        knowledge_preparer=_patchable(
            "prepare_planner_knowledge",
            prepare_planner_knowledge,
        ),
        knowledge_root_resolver=resolve_knowledge_workspace_root,
        workspace_context_loader=_workspace_context_payload_for_paths,
        plan_reconciler=lambda **kwargs: _patchable(
            "_reconcile_plan_for_paths",
            _reconcile_plan_for_paths,
        )(**kwargs),
        guarded_apply=apply_guarded_planner_plan_update,
        update_summarizer=summarize_plan_update,
        validator=_patchable("validate_plan", validate_plan),
        plan_saver=save_plan,
        plan_finalizer=finalize_plan,
        transcript_appender=append_transcript_note,
        planner_chat_appender=append_planner_chat,
        planner_summary_appender=append_planner_summary,
        repair_recorder=record_plan_repair,
        status_applier=apply_plan_status,
    )
    outcome = run_forge_planner_turn(
        ForgePlannerTurnRequest(
            paths=paths,
            plan=plan,
            state=planner_state,
            user_text=user_text,
            cfg_loader=cfg_loader,
            unavailable_message_builder=unavailable_message_builder,
            api_key_override=api_key_override,
            selection_label=selection_label,
            planning_relevant=planning_relevant,
            refresh_workspace_context=refresh_workspace_context,
            stream=stream,
            on_text_delta=on_text_delta,
        ),
        dependencies=dependencies,
    )

    usage_events = list(outcome.usage_events)
    if usage_events and on_usage_events is not None:
        try:
            on_usage_events(usage_events)
        except Exception:
            pass
    if outcome.planner_result is not None:
        render_reply(outcome.assistant_message, list(outcome.questions))
    for message in outcome.meta_messages:
        emit_meta(message)
    for warning_group in outcome.warnings:
        emit_warning_group(warning_group.label, list(warning_group.messages))
    if trace_callback is not None:
        for trace in outcome.traces:
            trace_callback(trace.message, trace.warning)
    if outcome.fallback_recommended and error_fallback is not None:
        error_fallback()
    return _ForgePlannerTurnControllerResult(
        status=outcome.status,
        planner_result=outcome.planner_result,
        summary_line=outcome.summary_line,
    )


def _write_plan_validation_artifact(
    *,
    paths: RunPaths,
    reconciliation_result: Any,
    validation_warnings: list[str],
) -> None:
    validation_path = paths.notes_dir / "plan_validation.md"
    lines: list[str] = [
        "# Plan Validation",
        "",
        "## Reconciliation",
        "",
    ]

    changed = bool(getattr(reconciliation_result, "changed", False))
    updated_task_ids = list(getattr(reconciliation_result, "updated_task_ids", []) or [])
    reconciliation_warnings = list(getattr(reconciliation_result, "warnings", []) or [])
    lines.append(f"- Changed: `{'yes' if changed else 'no'}`")
    if updated_task_ids:
        lines.append(f"- Updated Tasks: {', '.join(updated_task_ids)}")
    else:
        lines.append("- Updated Tasks: (none)")
    if reconciliation_warnings:
        lines.append("- Warnings:")
        for warning in reconciliation_warnings:
            lines.append(f"  - {warning}")
    else:
        lines.append("- No reconciliation warnings.")

    lines.extend(["", "## Validation", ""])
    if validation_warnings:
        for warning in validation_warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- No validation warnings.")

    validation_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")


def _capture_forge_requirement_from_planner_fallback(
    *,
    plan: dict[str, Any],
    paths: RunPaths,
    console: Console,
    user_text: str,
    planning_relevant: bool | None = None,
) -> bool:
    if planning_relevant is False:
        append_transcript_note(
            paths,
            role="system",
            message="Skipped fallback requirement capture for non-planning message.",
        )
        _print_forge_meta(
            console=console,
            message="Planner skipped this message because it looked off-topic; plan unchanged.",
        )
        return False

    if _set_forge_project_goal_if_empty(
        plan=plan,
        paths=paths,
        console=console,
        goal=user_text,
        transcript_message="Captured project goal (planner produced no structured update).",
        display_message="Captured project goal because the planner produced no structured update.",
    ):
        return True

    add_requirement(
        plan,
        user_text,
        execution_ready=False,
        source="planner_error_fallback",
    )
    save_plan(paths, plan)
    append_transcript_note(
        paths,
        role="system",
        message="Captured requirement note (planner produced no structured update).",
    )
    _print_forge_meta(
        console=console,
        message="Captured requirement note because the planner produced no structured update.",
    )
    return True


def _set_forge_project_goal_if_empty(
    *,
    plan: dict[str, Any],
    paths: RunPaths,
    console: Console,
    goal: str,
    transcript_message: str = "Updated project goal.",
    display_message: str = "Project goal set.",
) -> bool:
    normalized_goal = str(goal or "").strip()
    if not normalized_goal or str(plan.get("project_goal") or "").strip():
        return False
    plan["project_goal"] = normalized_goal
    if not str(plan.get("summary") or "").strip():
        plan["summary"] = normalized_goal
    save_plan(paths, plan)
    append_transcript_note(paths, role="system", message=transcript_message)
    _print_forge_meta(console=console, message=display_message)
    return True


def _finalize_forge_plan(
    *,
    console: Console,
    paths: RunPaths,
    plan: dict[str, Any],
    transcript_tail: list[dict[str, Any]] | None = None,
) -> None:
    dependencies = ForgePlannerDependencies(
        plan_reconciler=lambda **kwargs: _patchable(
            "_reconcile_plan_for_paths",
            _reconcile_plan_for_paths,
        )(**kwargs),
        validator=_patchable("validate_plan", validate_plan),
        plan_saver=save_plan,
        plan_finalizer=finalize_plan,
        transcript_appender=append_transcript_note,
    )

    def _legacy_artifact_writer(**kwargs: Any) -> Path:
        _patchable(
            "_write_plan_validation_artifact",
            _write_plan_validation_artifact,
        )(**kwargs)
        return paths.notes_dir / "plan_validation.md"

    outcome = finalize_forge_planner_plan(
        ForgePlannerFinalizeRequest(
            paths=paths,
            plan=plan,
            transcript_tail=transcript_tail,
        ),
        dependencies=dependencies,
        path_validator=_validate_forge_plan_for_paths,
        artifact_writer=_legacy_artifact_writer,
    )
    reconciliation_warnings = list(getattr(outcome.reconciliation_result, "warnings", ()) or ())
    if reconciliation_warnings:
        _print_forge_warning_messages(
            console=console,
            label="Plan reconciliation",
            warnings=reconciliation_warnings,
        )
    if outcome.validation_warnings:
        _print_forge_warning_messages(
            console=console,
            label="Plan validation",
            warnings=list(outcome.validation_warnings),
        )
    _print_forge_meta(console=console, message=f"Plan saved · {paths.plan_md_path}")
    _print_forge_meta(console=console, message=f"Structured plan · {paths.plan_json_path}")


def _enter_forge_mode(
    *,
    root: Path,
    console: Console,
    forge_state: _ForgeChatState,
    model: str | None = None,
    mode: str | None = None,
    planner_assistant_default: bool | None = None,
) -> bool:
    forge_binding: WorkspaceBinding | None = None
    try:
        forge_binding = resolve_workspace_binding(
            root,
            create_if_missing=False,
            allow_broad_workspace=False,
            source="forge_entry",
        )
        ensure_workspace_policy(
            forge_binding,
            action=WorkspaceAction.FORGE,
            allow_broad_workspace=False,
        )
    except WorkspaceBindingError as e:
        if forge_binding is not None:
            console.print("[yellow]Forge requires a narrower workspace before planning.[/yellow]")
            console.print(f"- requested path: {forge_binding.requested_path}")
            console.print(f"- workspace root: {forge_binding.workspace_context.workspace_root}")
            console.print(f"- focus dir: {forge_binding.workspace_context.focus_path}")
            console.print(f"- risk level: {forge_binding.risk_level}")
            for reason in forge_binding.risk_reasons:
                console.print(f"- reason: {reason}")
            console.print(
                workspace_policy_violation_message(forge_binding, action=WorkspaceAction.FORGE)
            )
        else:
            _print_forge_error(console=console, message=f"Forge error: {e}")
        return False

    workspace_scan = None
    try:
        entry_selection = _select_forge_chat_entry(
            forge_state=forge_state,
            workspace_binding=forge_binding,
        )
    except ForgeError as e:
        _print_forge_error(console=console, message=f"Forge error: {e}")
        return False
    paths = entry_selection.paths
    plan = entry_selection.plan
    forge_state.paths = paths
    forge_state.plan = plan
    entry_goal = str(getattr(forge_state, "entry_goal", "") or "").strip()
    forge_state.entry_goal = ""
    try:
        workspace_scan = refresh_workspace_context_artifacts(paths)
    except ForgeError as e:
        _print_forge_error(console=console, message=f"Forge error: {e}")
        return False
    if entry_selection.entry_kind in {"session_local_resume", "session_local_resume_rebound"}:
        refresh_current_run_pointer_if_tracking_same_run(paths)

    forge_state.ui_mode = "forge"
    if planner_assistant_default is None:
        forge_state.assistant_enabled = _patchable(
            "_prompt_forge_entry_plan_assistant",
            _prompt_forge_entry_plan_assistant,
        )(console=console)
    else:
        forge_state.assistant_enabled = bool(planner_assistant_default)
    if entry_goal:
        _set_forge_project_goal_if_empty(
            plan=plan,
            paths=paths,
            console=console,
            goal=entry_goal,
            transcript_message="Set project goal from /forge shortcut.",
            display_message="Project goal set from /forge.",
        )
    forge_state.planner_session = _ForgePlannerSessionState(
        workspace_context=_augment_workspace_context_with_mcp_execution_context(
            workspace_context={
                **workspace_scan.to_dict(),
                "greenfield": bool(getattr(paths, "greenfield", False)),
            },
            workspace_root=paths.root,
        )
    )
    console.print(
        _forge_enter_panel(
            paths=paths,
            plan=plan,
            entry_kind=entry_selection.entry_kind,
            assistant_enabled=forge_state.assistant_enabled,
            workspace_summary_lines=format_workspace_context_summary_lines(workspace_scan),
            model=model,
            mode=mode,
        )
    )
    if (
        not forge_state.assistant_enabled
        and planner_assistant_default is None
        and _is_non_interactive_terminal()
        and env_get("ALYSIS_FORGE_PLAN_ASSISTANT") is None
    ):
        _print_forge_meta(
            console=console,
            message=(
                "Plan Assistant defaulted off in non-interactive mode; set "
                "ALYSIS_FORGE_PLAN_ASSISTANT=1 to enable it."
            ),
        )
    return True


def _is_forge_enter_command(*, cmd: str, arg: str) -> bool:
    parsed = _parse_forge_enter_command(cmd=cmd, arg=arg)
    return parsed is not None and parsed.usage_error is None


def _handle_forge_chat_command(
    *,
    input_text: str,
    forge_state: _ForgeChatState,
    session: Any,
    console: Console,
) -> str:
    from ..chat import _handle_forge_chat_command_impl

    return _handle_forge_chat_command_impl(
        _cli_module_for_legacy_impl(),
        input_text=input_text,
        forge_state=forge_state,
        session=session,
        console=console,
    )


def _build_forge_exec_instruction(
    *,
    plan: dict[str, Any],
    task: dict[str, Any],
    cfg: AppConfig,
    role_model: str,
) -> str:
    return _shared_build_task_execution_instruction(
        plan=plan,
        task=task,
        cfg=cfg,
        role_model=role_model,
    )


def _build_forge_exec_instruction_bundle(
    *,
    plan: dict[str, Any],
    task: dict[str, Any],
    root: Path,
    cfg: AppConfig,
    role_model: str,
    mode: str,
    yes: bool,
    deny_write_prefixes: list[str] | None = None,
    allow_write_globs: list[str] | None = None,
    non_interactive: bool = False,
    verification_enabled: bool = True,
    authoritative_verification_commands: list[str] | None = None,
    api_key: str | None = None,
    subagents_enabled: bool = False,
    leading_sections: list[str] | None = None,
    relevant_assets_section: str | None = None,
) -> Any:
    return _shared_build_task_execution_instruction_bundle(
        plan=plan,
        task=task,
        root=root,
        cfg=cfg,
        role_model=role_model,
        mode=mode,
        yes=yes,
        deny_write_prefixes=deny_write_prefixes,
        allow_write_globs=allow_write_globs,
        non_interactive=non_interactive,
        verification_enabled=verification_enabled,
        authoritative_verification_commands=authoritative_verification_commands,
        api_key=api_key,
        subagents_enabled=subagents_enabled,
        leading_sections=leading_sections,
        relevant_assets_section=relevant_assets_section,
        managed_execution_startup_headroom=True,
    )


def _snapshot_runtime_tree(root: Path) -> dict[str, str]:
    return _shared_snapshot_runtime_tree(root)


__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
