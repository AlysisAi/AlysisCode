# ruff: noqa: F401,F403,F405,I001
# Legacy split module: dependencies are synced by cli_surface.py.
from __future__ import annotations

from ...task_dependencies import infer_ordered_predecessor_dependency
from .cli_common import *
from ...command_text import command_text


def _snapshot_workspace_tree(root: Path) -> dict[str, str]:
    return _shared_snapshot_workspace_tree(root)


def _capture_task_local_workspace_baseline(
    root: Path,
    *,
    before_commit: str | None,
) -> Any:
    return _shared_capture_task_local_workspace_baseline(
        root,
        before_commit=before_commit,
    )


def _cleanup_task_local_workspace_baseline(baseline: Any) -> None:
    _shared_cleanup_task_local_workspace_baseline(baseline)


def _git_diff_text(root: Path) -> str | None:
    if shutil.which("git") is None:
        return None
    proc = subprocess.run(
        ["git", "-C", os.fspath(root), "diff"],
        capture_output=True,
        **command_text(),
        check=False,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _git_changed_files(root: Path) -> list[str]:
    return _shared_git_changed_files(root)


def _build_execution_reporting_diff(root: Path) -> Any:
    return _shared_build_execution_reporting_diff(root)


def _build_execution_reporting_diff_with_commit_range(
    root: Path,
    *,
    before_commit: str | None,
    after_commit: str | None,
) -> Any:
    return _shared_build_execution_reporting_diff_with_commit_range(
        root,
        before_commit=before_commit,
        after_commit=after_commit,
    )


def _build_workspace_snapshot_reporting_diff(
    root: Path,
    *,
    before_snapshot: dict[str, str],
    after_snapshot: dict[str, str],
) -> Any:
    return _shared_build_workspace_snapshot_reporting_diff(
        root,
        before_snapshot=before_snapshot,
        after_snapshot=after_snapshot,
    )


def _build_task_local_workspace_reporting_diff(
    root: Path,
    *,
    baseline: Any,
    after_commit: str | None,
) -> Any:
    return _shared_build_task_local_workspace_reporting_diff(
        root,
        baseline=baseline,
        after_commit=after_commit,
    )


def _snapshot_session_logs(cfg: AppConfig) -> set[Path]:
    return _shared_snapshot_session_logs(cfg)


def _execution_private_sessions_dir(
    *,
    cfg: AppConfig,
    run_id: str,
    task_id: str,
    workspace_root: Path,
) -> Path:
    return _shared_execution_private_sessions_dir(
        cfg=cfg,
        run_id=run_id,
        task_id=task_id,
        workspace_root=workspace_root,
    )


def _cleanup_execution_private_sessions_dir(path: Path) -> None:
    _shared_cleanup_execution_private_sessions_dir(path)


def _write_exec_log_artifacts(
    *,
    paths: Any,
    task_id: str,
    cfg: AppConfig,
    no_log: bool,
    before_logs: set[Path] | None,
    sessions_dir: Path | None = None,
    expected_session_id: str | None = None,
) -> Any:
    return _shared_write_exec_log_artifacts(
        paths=paths,
        task_id=task_id,
        cfg=cfg,
        no_log=no_log,
        before_logs=before_logs,
        sessions_dir=sessions_dir,
        expected_session_id=expected_session_id,
    )


def _write_execution_context_artifact(
    *,
    paths: Any,
    task_id: str,
    context_text: str,
) -> Path:
    return _shared_write_execution_context_artifact(
        run_paths=paths,
        task_id=task_id,
        context_text=context_text,
    )


def _write_execution_budget_artifact(
    *,
    paths: Any,
    task_id: str,
    payload: dict[str, Any],
) -> Path:
    return _shared_write_execution_budget_artifact(
        run_paths=paths,
        task_id=task_id,
        payload=payload,
    )


def _resolve_managed_task_step_budget(
    *,
    cfg: AppConfig,
    plan: dict[str, Any],
    task: dict[str, Any],
    kind: str = "managed_task",
    mode: str | None = None,
    verification_enabled: bool,
    max_steps_override: int | None = None,
    attempt_count: int | None = None,
    image_count: int = 0,
    conflict_file_count: int = 0,
):
    return _shared_resolve_managed_task_step_budget(
        cfg=cfg,
        plan=plan,
        task=task,
        kind=kind,
        mode=mode,
        verification_enabled=verification_enabled,
        max_steps_override=max_steps_override,
        attempt_count=attempt_count,
        image_count=image_count,
        conflict_file_count=conflict_file_count,
    )


def _prepare_task_execution_knowledge(
    *,
    run_paths: RunPaths,
    task: dict[str, Any],
    selection_label: str,
    extra_paths: list[str] | None = None,
    limit: int = 4,
) -> Any:
    return _shared_prepare_task_execution_knowledge(
        run_paths=run_paths,
        task=task,
        selection_label=selection_label,
        extra_paths=extra_paths,
        limit=limit,
    )


def _mirror_selected_knowledge_into_worktree(
    *,
    materialized: Any,
    run_paths: RunPaths,
    worktree_repo_path: Path,
) -> None:
    _shared_mirror_selected_knowledge_into_worktree(
        materialized=materialized,
        run_paths=run_paths,
        worktree_repo_path=worktree_repo_path,
    )


def _task_dependency_blockers(plan: dict[str, Any], task: dict[str, Any]) -> list[str]:
    blockers: list[str] = []
    deps = task.get("dependencies") or []
    for dep_id in deps:
        dep_task = find_task(plan, str(dep_id))
        if dep_task is None:
            blockers.append(f"{dep_id} (missing)")
            continue
        dep_status = str(dep_task.get("status") or "")
        if dep_status != "done":
            blockers.append(f"{dep_id} ({dep_status or 'unknown'})")
    tasks = [item for item in plan.get("tasks") or [] if isinstance(item, dict)]
    inferred = infer_ordered_predecessor_dependency(tasks=tasks, task=task)
    if inferred is not None and inferred.depends_on not in {str(dep).strip() for dep in deps}:
        dep_task = find_task(plan, inferred.depends_on)
        if dep_task is None:
            blockers.append(f"{inferred.depends_on} (missing, inferred)")
        else:
            dep_status = str(dep_task.get("status") or "")
            if dep_status != "done":
                blockers.append(f"{inferred.depends_on} ({dep_status or 'unknown'}, inferred)")
    return blockers


def _safe_task_file_component(task_id: str) -> str:
    return _shared_safe_task_file_component(task_id)


def _normalize_scope_mode(scope: str) -> str:
    value = scope.strip().lower()
    if value not in _SCOPE_MODES:
        raise ForgeError("Invalid --scope. Use one of: off, warn, strict.")
    return value


def _normalize_verify_mode(value: str) -> str:
    try:
        return normalize_verify_mode(value)
    except VerifyError as e:
        raise ForgeError(str(e)) from e


def _remote_report_lines(record: dict[str, Any] | None) -> list[str]:
    if not record:
        return []
    lines: list[str] = [
        f"remote={record.get('remote') or '-'} provider={record.get('provider') or 'unknown'}",
        (
            f"pushed_branch={record.get('pushed_branch')} "
            f"pushed_base={record.get('pushed_base')} "
            f"created_pr={record.get('created_pr')}"
        ),
    ]
    pr_url = str(record.get("pr_url") or "").strip()
    if pr_url:
        lines.append(f"pr_url={pr_url}")
    pr_id = str(record.get("pr_number_or_iid") or "").strip()
    if pr_id:
        lines.append(f"pr_number_or_iid={pr_id}")

    branch_push_output = str(record.get("branch_push_output") or "").strip()
    if branch_push_output:
        lines.append(f"branch_push_output={truncate_output(branch_push_output, max_chars=300)}")
    base_push_output = str(record.get("base_push_output") or "").strip()
    if base_push_output:
        lines.append(f"base_push_output={truncate_output(base_push_output, max_chars=300)}")
    pr_output = str(record.get("pr_output") or "").strip()
    if pr_output:
        lines.append(f"pr_output={truncate_output(pr_output, max_chars=300)}")

    raw_errors = record.get("errors")
    if isinstance(raw_errors, list):
        for err in raw_errors:
            msg = str(err).strip()
            if msg:
                lines.append(f"error={truncate_output(msg, max_chars=300)}")
    return lines


def _print_usage_summary_from_logs(
    *,
    console: Console,
    title: str,
    log_paths: list[Path],
) -> None:
    summary = aggregate_usage_from_session_logs(log_paths)
    rows = summary.by_model_rows()
    if not rows:
        return
    table = _Table(title=title)
    table.add_column("model")
    table.add_column("prompt_tokens", justify="right")
    table.add_column("completion_tokens", justify="right")
    table.add_column("total_tokens", justify="right")
    table.add_column("cost_usd", justify="right")
    table.add_column("unknown_pricing", justify="right")
    table.add_column("usage_source(api/est)", justify="right")
    for row in rows:
        unknown_count = int(row.get("unknown_cost_count") or 0)
        cost_display = _format_cost_with_unknown(
            known_cost=_known_cost_value(row),
            unknown_calls=unknown_count,
            style="table",
        )
        table.add_row(
            str(row.get("model") or "-"),
            str(int(row.get("prompt_tokens") or 0)),
            str(int(row.get("completion_tokens") or 0)),
            str(int(row.get("total_tokens") or 0)),
            cost_display,
            str(unknown_count),
            (f"{int(row.get('api_usage_calls') or 0)}/{int(row.get('estimate_usage_calls') or 0)}"),
        )
    totals = summary.totals()
    total_cost = _format_cost_with_unknown(
        known_cost=_known_cost_value(totals),
        unknown_calls=int(totals.get("unknown_cost_calls") or 0),
        style="table",
    )
    table.add_row(
        "TOTAL",
        str(int(totals.get("prompt_tokens") or 0)),
        str(int(totals.get("completion_tokens") or 0)),
        str(int(totals.get("total_tokens") or 0)),
        total_cost,
        str(int(totals.get("unknown_cost_calls") or 0)),
        (
            f"{int(totals.get('api_usage_calls') or 0)}/"
            f"{int(totals.get('estimate_usage_calls') or 0)}"
        ),
    )
    console.print(table)


__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
