from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .branding import env_get_from
from .command_text import command_text
from .config import AppConfig, ConfigError, clone_cfg
from .execution_shared import (
    build_task_execution_instruction_bundle,
    mirror_plan_into_worktree,
    mirror_selected_knowledge_into_worktree,
    prepare_task_execution_knowledge,
    resolve_managed_task_step_budget,
    safe_task_file_component,
    snapshot_runtime_tree,
    snapshot_workspace_tree,
)
from .forge import RunPaths, now_iso
from .git_ops import (
    DEFAULT_COMMIT_AUTHOR_EMAIL,
    DEFAULT_COMMIT_AUTHOR_NAME,
    GitOpsError,
    delete_branch,
    ensure_not_staged_prefixes,
    ensure_not_staged_runtime_artifacts,
    format_patch_stdout,
    merge_no_ff,
    stage_all,
    staged_files,
    unstage_staged_prefixes,
    unstage_staged_runtime_artifacts,
)
from .git_safe import build_git_cmd
from .git_worktrees import ensure_task_worktree, remove_task_worktree
from .knowledge_base import rebuild_knowledge_index, write_issue_entry, write_task_attempt_entry
from .knowledge_capture import (
    RecordingSurface,
    mark_knowledge_capture_promotion_skipped,
    persist_execution_knowledge_capture,
    promote_validated_knowledge_capture,
)
from .merge_conflict_reviewer import list_unmerged_files, try_abort_merge
from .model_router import (
    ROLE_CODING,
    ROLE_CONFLICT_RESOLVE,
    resolve_model_for_role,
)
from .runtime_artifacts import has_grounded_rust_target_runtime_artifacts
from .runtime_kind import RuntimeKind
from .surface import NoopSurface
from .surface.console import make_console
from .task_scope import list_changed_files_including_untracked
from .verify_gate import (
    ResolvedVerifyCommands,
    VerifyError,
    resolve_task_aware_verify_command_selection,
    resolve_verify_command_selection,
    resolve_verify_commands,
    run_task_verification,
    verify_run_status,
)

_VERIFY_MODES = {"off", "warn", "strict"}

CONFLICT_RESOLVER_SYSTEM_PROMPT = """You are CONFLICT_RESOLVER. You are an autonomous coding agent operating inside a dedicated conflict-resolution worktree.

Mission
- Resolve git merge conflicts safely and correctly.
- Resolve merge conflicts only. Do not implement unrelated features.
- Only modify files that are part of the merge conflict set / allowed scope.
- Ensure there are no conflict markers left and the repo is in a clean, merge-completable state.

Rules
- Start by inspecting git status and identifying unmerged files.
- Edit ONLY the conflicted files (and only within the allowed write scope).
- Remove all conflict markers (<<<<<<<, =======, >>>>>>>) and produce valid code.
- Prefer minimal changes: preserve intended behavior from both sides when possible.
- Do not refactor unrelated code.
- Do not modify .alysis/ or other denied prefixes unless explicitly instructed.

Tool usage
- Prefer search_rg plus fs_read line ranges for focused conflict inspection; use fs_read when you need broader file context.
- Prefer fs_edit for deterministic localized edits in one existing conflicted file.
- Prefer git_apply_patch for broader or context-heavy conflict edits where unified diff context matters.
- After edits, search for remaining conflict markers.
- Use git_status to ensure no unmerged paths remain.
- If verification tools/commands are available in this run, use the most relevant targeted checks to ensure the resolution is sound.

Docs and tests
- If conflict resolution changes user-facing behavior, update README/docs.
- If the conflict touches logic likely to break, ensure tests are present and propose/execute relevant verification.

Final response
- Summarize which files were resolved and any remaining risks/assumptions.
- Report verification commands run (or proposed) and results.
"""


@dataclass(frozen=True)
class ConflictAutoResolveSettings:
    enabled: bool = True
    verify_mode: str = "strict"
    max_attempts: int = 1


@dataclass(frozen=True)
class AutoResolveOutcome:
    success: bool
    task_id: str
    conflict_branch: str
    worktree_repo_path: Path
    result_json_path: Path
    report_path: Path
    patch_path: Path
    merge_commit_hash: str | None
    verify_summary: str | None
    warnings: list[str]
    error: str | None
    agent_exit_code: int | None = None
    salvaged_nonzero_exit: bool = False


def run_agent(*args: Any, **kwargs: Any) -> Any:
    # Conflict resolution is optional; loading its settings should stay lightweight.
    from .agent_loop import run_agent as run

    return run(*args, **kwargs)


def _parse_bool(raw: object, *, default: bool) -> bool:
    if raw is None:
        return default
    value = str(raw).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _parse_verify_mode(raw: object, *, default: str) -> str:
    value = str(raw).strip().lower() if raw is not None else default
    if value in _VERIFY_MODES:
        return value
    return default


def _parse_positive_int(raw: object, *, default: int) -> int:
    try:
        parsed = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if parsed <= 0:
        return default
    return parsed


def load_conflict_auto_resolve_settings(
    *,
    cfg: AppConfig,
    env: Mapping[str, str] | None = None,
) -> ConflictAutoResolveSettings:
    source = env if env is not None else os.environ
    enabled = True
    verify_mode = "strict"
    max_attempts = 1

    cfg_raw = cfg.extra_fields.get("conflict_auto_resolve")
    if isinstance(cfg_raw, dict):
        enabled = _parse_bool(cfg_raw.get("enabled"), default=enabled)
        verify_mode = _parse_verify_mode(cfg_raw.get("verify_mode"), default=verify_mode)
        max_attempts = _parse_positive_int(cfg_raw.get("max_attempts"), default=max_attempts)

    enabled = _parse_bool(
        env_get_from(source, "ALYSIS_CONFLICT_AUTO_RESOLVE"),
        default=enabled,
    )
    verify_mode = _parse_verify_mode(
        env_get_from(source, "ALYSIS_CONFLICT_AUTO_RESOLVE_VERIFY"),
        default=verify_mode,
    )
    max_attempts = _parse_positive_int(
        env_get_from(source, "ALYSIS_CONFLICT_AUTO_RESOLVE_MAX_ATTEMPTS"),
        default=max_attempts,
    )
    return ConflictAutoResolveSettings(
        enabled=enabled,
        verify_mode=verify_mode,
        max_attempts=max_attempts,
    )


def task_conflict_attempts(task: dict[str, Any]) -> int:
    raw = task.get("conflict_attempts")
    try:
        attempts = int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        return 0
    return max(0, attempts)


def can_attempt_conflict_auto_resolve(
    *,
    task: dict[str, Any],
    settings: ConflictAutoResolveSettings,
) -> bool:
    return settings.enabled and task_conflict_attempts(task) < settings.max_attempts


def bump_conflict_attempt(task: dict[str, Any]) -> int:
    next_attempt = task_conflict_attempts(task) + 1
    task["conflict_attempts"] = next_attempt
    return next_attempt


def _run_git(
    root: Path,
    args: list[str],
    *,
    extra_config: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    cmd = build_git_cmd(root, args, extra_config=extra_config)
    return subprocess.run(
        cmd,
        check=False,
        capture_output=True,
        **command_text(),
    )


def _run_git_checked(
    root: Path,
    args: list[str],
    *,
    extra_config: dict[str, str] | None = None,
    error_message: str,
) -> subprocess.CompletedProcess[str]:
    cp = _run_git(root, args, extra_config=extra_config)
    if cp.returncode != 0:
        detail = (cp.stderr or cp.stdout).strip()
        raise GitOpsError(f"{error_message}: {detail or 'unknown error'}")
    return cp


def _git_head(root: Path) -> str:
    cp = _run_git_checked(
        root,
        ["rev-parse", "HEAD"],
        error_message="failed to resolve commit hash",
    )
    return cp.stdout.strip()


def _truncate(text: str, *, max_chars: int = 4000) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "...(truncated)"


def _format_agent_exception_summary(exc: Exception) -> str:
    name = exc.__class__.__name__
    message = str(exc).strip()
    return f"{name}: {message}" if message else name


def _merge_changed_files(*path_groups: list[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for group in path_groups:
        for raw in group:
            value = str(raw).strip().replace("\\", "/")
            while value.startswith("./"):
                value = value[2:]
            if not value or value in seen:
                continue
            seen.add(value)
            merged.append(value)
    return merged


_CONFLICT_MARKER_PREFIXES = ("<<<<<<<", "=======", ">>>>>>>")


def _paths_with_conflict_markers(root: Path, paths: list[str]) -> list[str]:
    marker_paths: list[str] = []
    seen: set[str] = set()
    for raw_path in paths:
        rel_path = str(raw_path or "").strip()
        if not rel_path or rel_path in seen:
            continue
        seen.add(rel_path)
        target = root / rel_path
        if not target.is_file():
            continue
        try:
            lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if any(line.startswith(_CONFLICT_MARKER_PREFIXES) for line in lines):
            marker_paths.append(rel_path)
    return marker_paths


def _build_conflict_instruction(
    *,
    instruction_bundle: Any,
) -> str:
    return str(instruction_bundle.instruction)


def _conflict_instruction_sections(*, unmerged_files: list[str]) -> list[str]:
    files_block = (
        "\n".join(f"- `{path}`" for path in unmerged_files) if unmerged_files else "- (none)"
    )
    return [
        "\n".join(
            [
                "## Conflict Resolution Scope",
                "",
                "- Resolve only the merge conflict.",
                "- Do not expand scope beyond the conflict resolution itself.",
                "- Edit only the currently unmerged files listed below.",
                "- Preserve the task intent, but do not treat plan context as permission to broaden scope.",
                "",
                "### Unmerged Files",
                "",
                files_block,
            ]
        )
    ]


def _write_auto_resolve_artifacts(
    *,
    conflict_dir: Path,
    result_payload: dict[str, Any],
    report_text: str,
    patch_text: str,
    context_text: str | None = None,
    budget_payload: dict[str, Any] | None = None,
) -> tuple[Path, Path, Path]:
    conflict_dir.mkdir(parents=True, exist_ok=True)
    result_path = conflict_dir / "auto_resolve_result.json"
    report_path = conflict_dir / "auto_resolve_report.md"
    patch_path = conflict_dir / "auto_resolve_patch.diff"
    result_path.write_text(
        json.dumps(result_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_path.write_text(report_text.rstrip() + "\n", encoding="utf-8")
    patch_path.write_text(patch_text if patch_text else "(no patch generated)\n", encoding="utf-8")
    if context_text is not None:
        (conflict_dir / "auto_resolve_context.md").write_text(
            context_text.rstrip() + "\n",
            encoding="utf-8",
        )
    if budget_payload is not None:
        (conflict_dir / "auto_resolve_budget.json").write_text(
            json.dumps(budget_payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return result_path, report_path, patch_path


def attempt_auto_resolve_conflict(
    *,
    paths: RunPaths,
    plan: dict[str, Any],
    task: dict[str, Any],
    cfg: AppConfig,
    api_key_override: str | None,
    base_branch: str,
    task_branch: str,
    keep_worktrees: bool,
    settings: ConflictAutoResolveSettings | None = None,
    verify_commands: list[str] | None = None,
    verify_command_selection: ResolvedVerifyCommands | None = None,
) -> AutoResolveOutcome:
    effective_settings = settings or load_conflict_auto_resolve_settings(cfg=cfg)
    task_id = str(task.get("id") or "").strip()
    safe_task = safe_task_file_component(task_id)
    conflict_branch = f"conflict/{safe_task.lower()}"
    worktree_repo_path = paths.run_dir / "conflict_worktrees" / safe_task / "repo"
    conflict_dir = paths.execution_dir / "conflicts" / safe_task
    verify_artifact_path = conflict_dir / "auto_resolve_verify.txt"

    started_at = now_iso()
    warnings: list[str] = []
    errors: list[str] = []
    verify_summary: str | None = None
    merge_commit_hash: str | None = None
    patch_text = "(no patch generated)\n"
    agent_model = ""
    unresolved_after: list[str] = []
    worktree_kept = True
    conflict_instruction_text: str | None = None
    conflict_budget_payload: dict[str, Any] | None = None
    conflict_paths: list[str] = []
    recording_surface = RecordingSurface(NoopSurface())
    resolved_verify_selection: ResolvedVerifyCommands | None = None
    resolved_verify_commands: list[str] = []
    prompt_verification_enabled = False
    agent_exit_code: int | None = None
    salvaged_nonzero_exit = False
    runtime_artifacts_changed = False
    material_changed_files: list[str] = []
    if effective_settings.verify_mode != "off":
        if verify_command_selection is not None:
            resolved_verify_selection = verify_command_selection
            if verify_commands is not None:
                normalized_commands = tuple(
                    resolve_verify_commands(
                        cfg=cfg,
                        verify_cmd=verify_commands,
                        root=worktree_repo_path,
                    )
                )
                if normalized_commands != resolved_verify_selection.commands:
                    resolved_verify_selection = resolve_verify_command_selection(
                        cfg=cfg,
                        verify_cmd=verify_commands,
                        root=worktree_repo_path,
                    )
        resolved_verify_selection = resolve_task_aware_verify_command_selection(
            cfg=cfg,
            verify_cmd=(verify_commands if resolved_verify_selection is None else None),
            task=task,
            root=worktree_repo_path,
            selection=resolved_verify_selection,
            plan_requirements=[
                str(item).strip() for item in (plan.get("requirements") or []) if str(item).strip()
            ]
            if isinstance(plan, dict)
            else None,
        )
        resolved_verify_commands = list(resolved_verify_selection.commands)
        prompt_verification_enabled = bool(resolved_verify_commands)

    try:
        ensure_task_worktree(
            root=paths.root,
            worktree_repo_path=worktree_repo_path,
            branch=conflict_branch,
            base_branch=base_branch,
        )
        mirror_plan_into_worktree(run_paths=paths, worktree_repo_path=worktree_repo_path)

        merge_cp = _run_git(
            worktree_repo_path,
            ["merge", "--no-ff", task_branch, "-m", f"Resolve {task_id}"],
        )
        unmerged_files = list_unmerged_files(worktree_repo_path)
        if merge_cp.returncode != 0 and not unmerged_files:
            detail = (merge_cp.stderr or merge_cp.stdout).strip()
            raise GitOpsError(
                f"failed to initialize conflict resolution merge: {detail or 'unknown error'}"
            )

        if unmerged_files:
            conflict_paths = list(unmerged_files)
            run_cfg = clone_cfg(cfg)
            if effective_settings.verify_mode != "off":
                run_cfg.verify_commands = list(resolved_verify_commands)
            try:
                run_cfg.model = resolve_model_for_role(
                    cfg=cfg,
                    role=ROLE_CONFLICT_RESOLVE,
                    plan=plan,
                    fallback_to_default=False,
                    prefer_context="forge",
                )
            except ConfigError:
                run_cfg.model = resolve_model_for_role(
                    cfg=cfg,
                    role=ROLE_CODING,
                    plan=plan,
                    prefer_context="forge",
                )
            agent_model = run_cfg.model
            prepared_knowledge = prepare_task_execution_knowledge(
                run_paths=paths,
                task=task,
                selection_label="conflict_auto_resolve",
                extra_paths=unmerged_files,
            )
            mirror_selected_knowledge_into_worktree(
                materialized=prepared_knowledge,
                run_paths=paths,
                worktree_repo_path=worktree_repo_path,
            )
            instruction_bundle = build_task_execution_instruction_bundle(
                plan=plan,
                task=task,
                root=worktree_repo_path,
                cfg=run_cfg,
                role_model=run_cfg.model,
                mode="auto",
                yes=True,
                deny_write_prefixes=[".alysis"],
                allow_write_globs=unmerged_files,
                non_interactive=True,
                verification_enabled=prompt_verification_enabled,
                authoritative_verification_commands=(
                    resolved_verify_commands if prompt_verification_enabled else None
                ),
                trusted_system_prompt_override=CONFLICT_RESOLVER_SYSTEM_PROMPT,
                api_key=api_key_override,
                subagents_enabled=False,
                leading_sections=[
                    *_conflict_instruction_sections(unmerged_files=unmerged_files),
                    prepared_knowledge.prompt_section,
                ],
            )
            conflict_instruction_text = instruction_bundle.instruction
            task_image_paths = list(instruction_bundle.image_paths) or None
            conflict_attempt_count = task_conflict_attempts(task)
            if conflict_attempt_count <= 0:
                conflict_attempt_count = 1
            conflict_file_count = len(unmerged_files)
            conflict_step_budget = resolve_managed_task_step_budget(
                cfg=run_cfg,
                plan=plan,
                task=task,
                kind="conflict_resolution",
                mode="auto",
                verification_enabled=effective_settings.verify_mode != "off",
                max_steps_override=None,
                attempt_count=conflict_attempt_count,
                image_count=len(task_image_paths or []),
                conflict_file_count=conflict_file_count,
            )
            conflict_budget_payload = instruction_bundle.to_budget_artifact_payload()
            conflict_budget_payload["step_budget"] = conflict_step_budget.to_payload()
            instruction = _build_conflict_instruction(
                instruction_bundle=instruction_bundle,
            )
            before_runtime_snapshot = snapshot_runtime_tree(worktree_repo_path)
            before_workspace_snapshot = snapshot_workspace_tree(worktree_repo_path)
            run_agent_exception_summary: str | None = None
            try:
                agent_exit_code = run_agent(
                    cfg=run_cfg,
                    root=worktree_repo_path,
                    instruction=instruction,
                    acceptance_instruction=(
                        "Resolve the merge conflicts without expanding the task scope.\n"
                        + "\n".join(f"Resolve conflict in `{path}`." for path in unmerged_files)
                    ),
                    mode="auto",
                    runtime_kind=RuntimeKind.CONFLICT_AUTO_RESOLVE,
                    yes=True,
                    max_steps=conflict_step_budget.resolved_max_steps,
                    no_log=False,
                    api_key_override=api_key_override,
                    console=make_console(),
                    surface=recording_surface,
                    deny_write_prefixes=[".alysis"],
                    allow_write_globs=unmerged_files,
                    non_interactive=True,
                    session_log_dir_override=conflict_dir / "sessions",
                    session_id_override=f"{safe_task}-conflict",
                    usage_role=f"conflict_resolve:{task_id}",
                    enable_compaction=False,
                    enable_tool_output_offload=True,
                    enable_conversation_summarization=True,
                    compaction_profile="execution",
                    enable_chat_turn_step_budget=False,
                    one_shot_execution=True,
                    trusted_system_prompt_override=CONFLICT_RESOLVER_SYSTEM_PROMPT,
                    verification_enabled=prompt_verification_enabled,
                    authoritative_verification_commands=(
                        resolved_verify_commands if prompt_verification_enabled else None
                    ),
                    subagents_enabled=False,
                )
            except Exception as e:  # noqa: BLE001
                run_agent_exception_summary = _format_agent_exception_summary(e)
                agent_exit_code = None

            after_runtime_snapshot = snapshot_runtime_tree(worktree_repo_path)
            after_workspace_snapshot = snapshot_workspace_tree(worktree_repo_path)
            runtime_artifacts_changed = before_runtime_snapshot != after_runtime_snapshot
            workspace_delta_files = {
                path
                for path in (set(before_workspace_snapshot) | set(after_workspace_snapshot))
                if before_workspace_snapshot.get(path) != after_workspace_snapshot.get(path)
            }
            current_changed_files = list_changed_files_including_untracked(worktree_repo_path)
            material_changed_files = [
                path for path in current_changed_files if path in workspace_delta_files
            ]
            material_changed_files = _merge_changed_files(material_changed_files)
            material_changes_detected = bool(material_changed_files)
            nonzero_agent_exit = (
                agent_exit_code is not None
                and agent_exit_code != 0
                and run_agent_exception_summary is None
            )
            if run_agent_exception_summary is not None:
                raise RuntimeError(
                    "conflict resolver agent raised before returning normally: "
                    f"{run_agent_exception_summary}"
                )
            if agent_exit_code is None:
                raise RuntimeError("conflict resolver agent did not return an exit code")
            if nonzero_agent_exit:
                if runtime_artifacts_changed:
                    raise RuntimeError(
                        "conflict resolver agent failed with exit code "
                        f"{agent_exit_code}; protected runtime "
                        "artifacts under .alysis changed"
                    )
                if material_changes_detected:
                    raise RuntimeError(
                        "conflict resolver agent failed with exit code "
                        f"{agent_exit_code} after producing material conflict-resolution changes; "
                        "refusing to accept partial conflict auto-resolve result"
                    )
                raise RuntimeError(
                    "conflict resolver agent failed with exit code "
                    f"{agent_exit_code} and produced no material conflict-resolution changes"
                )

            marker_paths = _paths_with_conflict_markers(worktree_repo_path, conflict_paths)
            if marker_paths:
                raise RuntimeError(
                    "conflict markers remain unresolved in: " + ", ".join(marker_paths[:20])
                )

            stage_all(worktree_repo_path)
            unstage_staged_prefixes(
                worktree_repo_path,
                [".alysis", ".alysis_images", "alysis-feedback"],
            )
            ensure_not_staged_prefixes(
                worktree_repo_path,
                [".alysis", ".alysis_images", "alysis-feedback"],
            )
            if has_grounded_rust_target_runtime_artifacts(worktree_repo_path):
                staged_now = staged_files(worktree_repo_path)
                unstage_staged_runtime_artifacts(
                    worktree_repo_path,
                    current_paths=staged_now,
                )
                staged_now = staged_files(worktree_repo_path)
                ensure_not_staged_runtime_artifacts(
                    worktree_repo_path,
                    current_paths=staged_now,
                )
            unresolved_after = list_unmerged_files(worktree_repo_path)
            if unresolved_after:
                raise RuntimeError(
                    "conflict paths remain unmerged after staging resolved files: "
                    + ", ".join(unresolved_after[:20])
                )
            _run_git_checked(
                worktree_repo_path,
                ["commit", "--no-edit"],
                extra_config={
                    "user.name": DEFAULT_COMMIT_AUTHOR_NAME,
                    "user.email": DEFAULT_COMMIT_AUTHOR_EMAIL,
                },
                error_message="failed to commit conflict resolution",
            )

        if effective_settings.verify_mode != "off" and resolved_verify_commands:
            verify_result = run_task_verification(
                root=worktree_repo_path,
                commands=resolved_verify_commands,
                artifact_path=verify_artifact_path,
                cfg=cfg,
            )
            verify_summary = verify_result.summary
            if verify_run_status(verify_result) == "not_run":
                # Tri-state: nothing executed - warn, do not fail the merge.
                warnings.append(f"Conflict verify did not execute: {verify_result.summary}")
            elif not verify_result.all_passed:
                if effective_settings.verify_mode == "strict":
                    raise RuntimeError(f"strict conflict verify failed: {verify_result.summary}")
                warnings.append(f"Conflict verify warning: {verify_result.summary}")
        elif effective_settings.verify_mode == "strict":
            verify_summary = "verification skipped: no authoritative commands available"
            raise RuntimeError(
                "strict conflict verify could not run: no authoritative commands available"
            )
        elif effective_settings.verify_mode != "off":
            verify_summary = "verification skipped: no authoritative commands available"
        else:
            verify_summary = "verification disabled (conflict auto-resolve verify=off)"

        merge_commit_hash = _git_head(worktree_repo_path)
        patch_text = format_patch_stdout(worktree_repo_path, base_branch=base_branch)

        _run_git_checked(
            paths.root,
            ["checkout", base_branch],
            error_message=f"failed to checkout base branch {base_branch}",
        )
        ff_cp = _run_git(paths.root, ["merge", "--ff-only", conflict_branch])
        if ff_cp.returncode != 0:
            try:
                merge_commit_hash = merge_no_ff(
                    paths.root,
                    base_branch=base_branch,
                    task_branch=conflict_branch,
                    message=f"Merge {task_id}: auto-resolved conflict",
                )
            except GitOpsError as e:
                cleanup_ok, cleanup_log = try_abort_merge(paths.root, base_branch=base_branch)
                errors.append(f"failed to merge conflict branch into base: {e}")
                if not cleanup_ok:
                    errors.append(
                        "failed to cleanup repository after merge error; "
                        f"cleanup log: {_truncate(cleanup_log, max_chars=600)}"
                    )
                raise
        else:
            merge_commit_hash = _git_head(paths.root)

        if not keep_worktrees:
            try:
                remove_task_worktree(
                    root=paths.root,
                    worktree_repo_path=worktree_repo_path,
                    force=True,
                )
                worktree_kept = False
            except Exception as e:  # noqa: BLE001
                warnings.append(f"conflict worktree cleanup failed: {e}")
            try:
                delete_branch(paths.root, conflict_branch)
            except Exception as e:  # noqa: BLE001
                warnings.append(f"conflict branch cleanup failed: {e}")
        else:
            worktree_kept = True

        success = True
        error_message: str | None = None
    except (GitOpsError, RuntimeError, VerifyError, ConfigError) as e:
        success = False
        error_message = str(e)
        errors.append(str(e))

    finished_at = now_iso()
    result_payload = {
        "task_id": task_id,
        "base_branch": base_branch,
        "task_branch": task_branch,
        "conflict_branch": conflict_branch,
        "worktree_repo_path": os.fspath(worktree_repo_path),
        "worktree_kept": worktree_kept,
        "success": success,
        "merge_commit_hash": merge_commit_hash,
        "verify_mode": effective_settings.verify_mode,
        "verify_summary": verify_summary,
        "verify_artifact_path": (
            os.fspath(verify_artifact_path) if verify_artifact_path.exists() else None
        ),
        "context_artifact_path": (
            os.fspath(conflict_dir / "auto_resolve_context.md")
            if conflict_instruction_text is not None
            else None
        ),
        "budget_artifact_path": (
            os.fspath(conflict_dir / "auto_resolve_budget.json")
            if conflict_budget_payload is not None
            else None
        ),
        "agent_model": agent_model,
        "agent_exit_code": agent_exit_code,
        "salvaged_nonzero_exit": salvaged_nonzero_exit,
        "warnings": warnings,
        "errors": errors,
        "unresolved_after": unresolved_after,
        "started_at": started_at,
        "finished_at": finished_at,
    }
    report_lines = [
        f"# Conflict Auto-Resolve: {task_id}",
        "",
        f"- Success: {'yes' if success else 'no'}",
        f"- Base Branch: `{base_branch}`",
        f"- Task Branch: `{task_branch}`",
        f"- Conflict Branch: `{conflict_branch}`",
        f"- Worktree: `{worktree_repo_path}`",
        f"- Verify Mode: `{effective_settings.verify_mode}`",
        f"- Verify Summary: {verify_summary or '(none)'}",
        f"- Merge Commit: `{merge_commit_hash or '-'}`",
        f"- Agent Exit Code: `{agent_exit_code if agent_exit_code is not None else '-'}`",
        f"- Salvaged Non-Zero Exit: {'yes' if salvaged_nonzero_exit else 'no'}",
        "",
    ]
    if warnings:
        report_lines.append("## Warnings")
        report_lines.append("")
        report_lines.extend(f"- {item}" for item in warnings)
        report_lines.append("")
    if errors:
        report_lines.append("## Errors")
        report_lines.append("")
        report_lines.extend(f"- {item}" for item in errors)
        report_lines.append("")
    if not warnings and not errors:
        report_lines.append("No warnings or errors recorded.")
        report_lines.append("")
    report_text = "\n".join(report_lines).rstrip() + "\n"

    result_path, report_path, patch_path = _write_auto_resolve_artifacts(
        conflict_dir=conflict_dir,
        result_payload=result_payload,
        report_text=report_text,
        patch_text=patch_text,
        context_text=conflict_instruction_text,
        budget_payload=conflict_budget_payload,
    )
    persisted_capture = persist_execution_knowledge_capture(
        paths=paths,
        task=task,
        source="conflict_auto_resolve",
        assistant_message=recording_surface.final_assistant_message,
        artifact_dir=conflict_dir / "knowledge_capture" / safe_task_file_component(started_at),
        report_path=report_path,
        patch_path=patch_path,
        verify_artifact_path=verify_artifact_path if verify_artifact_path.exists() else None,
        budget_artifact_path=(conflict_dir / "auto_resolve_budget.json")
        if conflict_budget_payload is not None
        else None,
        session_artifact_dir=(conflict_dir / "sessions")
        if (conflict_dir / "sessions").exists()
        else None,
    )
    if success:
        promote_validated_knowledge_capture(
            paths=paths,
            task=task,
            artifact_dir=persisted_capture.artifact_dir,
        )
    else:
        mark_knowledge_capture_promotion_skipped(
            artifact_dir=persisted_capture.artifact_dir,
            reason="conflict auto-resolve outcome was not accepted",
        )
    if success:
        attempt_summary = "Conflict auto-resolve succeeded."
    else:
        attempt_summary = f"Conflict auto-resolve failed: {error_message or 'unknown error'}"
    reported_changed_files = material_changed_files or conflict_paths or unresolved_after
    write_task_attempt_entry(
        paths=paths,
        task=task,
        source="conflict_auto_resolve",
        result="success" if success else "failure",
        summary=attempt_summary,
        changed_files=reported_changed_files,
        verify_summary=verify_summary,
        report_path=report_path,
        patch_path=patch_path,
        verify_artifact_path=verify_artifact_path if verify_artifact_path.exists() else None,
        budget_artifact_path=(conflict_dir / "auto_resolve_budget.json")
        if conflict_budget_payload is not None
        else None,
        session_artifact_dir=(conflict_dir / "sessions")
        if (conflict_dir / "sessions").exists()
        else None,
        acceptance_state="accepted" if success else "rejected",
        extra_tags=[
            "conflict_auto_resolve",
        ],
    )
    if not success or unresolved_after:
        write_issue_entry(
            paths=paths,
            task=task,
            source="conflict_auto_resolve",
            title=f"{task_id}: conflict auto-resolve failed",
            summary=error_message or "Conflict auto-resolve did not finish cleanly.",
            paths_in_scope=unresolved_after or reported_changed_files,
            report_path=report_path,
            patch_path=patch_path,
            verify_artifact_path=verify_artifact_path if verify_artifact_path.exists() else None,
            budget_artifact_path=(conflict_dir / "auto_resolve_budget.json")
            if conflict_budget_payload is not None
            else None,
            session_artifact_dir=(conflict_dir / "sessions")
            if (conflict_dir / "sessions").exists()
            else None,
            tags=["conflict_auto_resolve", "merge_conflict"],
        )
    rebuild_knowledge_index(paths)
    return AutoResolveOutcome(
        success=success,
        task_id=task_id,
        conflict_branch=conflict_branch,
        worktree_repo_path=worktree_repo_path,
        result_json_path=result_path,
        report_path=report_path,
        patch_path=patch_path,
        merge_commit_hash=merge_commit_hash,
        verify_summary=verify_summary,
        warnings=warnings,
        error=error_message,
        agent_exit_code=agent_exit_code,
        salvaged_nonzero_exit=salvaged_nonzero_exit,
    )
