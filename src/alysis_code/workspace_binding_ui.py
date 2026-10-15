from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from .workspace_binding import (
    WorkspaceAction,
    WorkspaceBinding,
    WorkspaceBindingError,
    WorkspaceCandidate,
    WorkspaceRiskLevel,
    discover_workspace_candidates,
    ensure_workspace_policy,
    resolve_workspace_binding,
    workspace_policy,
    workspace_policy_violation_message,
)

_ACTION_USE_CURRENT = "use_current"
_ACTION_CHOOSE_PROJECT = "choose_project"
_ACTION_CREATE_FOLDER = "create_folder"
_ACTION_ENTER_PATH = "enter_path"
_ACTION_CANCEL = "cancel"
_DEFAULT_ACTION = _ACTION_CHOOSE_PROJECT


def guarded_workspace_action_rows(
    *,
    binding: WorkspaceBinding,
    candidates: tuple[WorkspaceCandidate, ...],
    allow_use_current_action: bool = True,
) -> list[tuple[str, str, str]]:
    current_label = _display_path(binding.requested_path)
    rows: list[tuple[str, str, str]] = []
    if allow_use_current_action:
        rows.append(
            (
                _ACTION_USE_CURRENT,
                "1) use current directory anyway",
                f"Bind to {current_label} despite the guarded risk.",
            )
        )
        choose_label = "2) choose an existing project folder"
        create_label = "3) create a new folder here"
        enter_label = "4) enter another path"
        cancel_label = "5) cancel"
    else:
        choose_label = "1) choose an existing project folder"
        create_label = "2) create a new folder here"
        enter_label = "3) enter another path"
        cancel_label = "4) cancel"
    rows.extend(
        [
            (
                _ACTION_CHOOSE_PROJECT,
                choose_label,
                (
                    f"Pick from {len(candidates)} shallow project candidate(s)."
                    if candidates
                    else "Pick another existing folder manually."
                ),
            ),
            (
                _ACTION_CREATE_FOLDER,
                create_label,
                f"Create a new child directory under {current_label} and bind to it.",
            ),
            (
                _ACTION_ENTER_PATH,
                enter_label,
                "Type an explicit existing path or a relative child path.",
            ),
            (
                _ACTION_CANCEL,
                cancel_label,
                "Abort startup without creating a session.",
            ),
        ]
    )
    return rows


def workspace_candidate_rows(
    *,
    base_path: Path,
    candidates: tuple[WorkspaceCandidate, ...],
) -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for idx, candidate in enumerate(candidates, start=1):
        try:
            label_path = os.fspath(candidate.path.relative_to(base_path))
        except ValueError:
            label_path = _display_path(candidate.path)
        rows.append(
            (
                os.fspath(candidate.path),
                f"{idx}) {label_path}",
                candidate.summary,
            )
        )
    return rows


def resolve_startup_workspace_binding(
    *,
    requested_path: Path,
    interactive: bool,
    create_if_missing: bool = False,
    allow_broad_workspace: bool = False,
    source: str = "explicit_path",
    action: str = WorkspaceAction.CHAT,
    console: Any | None = None,
    select_action_interactive: Callable[..., tuple[str | None, bool]] | None = None,
    select_candidate_interactive: Callable[..., tuple[Path | None, bool]] | None = None,
    prompt_text: Callable[..., str] | None = None,
    allow_subprocess: bool = True,
) -> WorkspaceBinding:
    prompter = prompt_text or typer.prompt
    policy = workspace_policy(action)
    binding = resolve_workspace_binding(
        requested_path,
        create_if_missing=create_if_missing,
        allow_broad_workspace=allow_broad_workspace,
        source=source,
        allow_subprocess=allow_subprocess,
    )
    try:
        return ensure_workspace_policy(
            binding,
            action=policy.action,
            allow_broad_workspace=allow_broad_workspace,
        )
    except WorkspaceBindingError:
        pass
    if binding.risk_level == WorkspaceRiskLevel.BLOCKED:
        raise WorkspaceBindingError(
            workspace_policy_violation_message(binding, action=policy.action)
        )
    if not interactive or not policy.interactive_guarded_resolution:
        raise WorkspaceBindingError(
            workspace_policy_violation_message(binding, action=policy.action)
        )
    return _resolve_guarded_binding_interactively(
        binding=binding,
        policy_action=policy.action,
        allow_use_current_action=policy.interactive_allow_use_current,
        console=console,
        select_action_interactive=select_action_interactive,
        select_candidate_interactive=select_candidate_interactive,
        prompt_text=prompter,
    )


def _resolve_guarded_binding_interactively(
    *,
    binding: WorkspaceBinding,
    policy_action: str,
    allow_use_current_action: bool,
    console: Any | None,
    select_action_interactive: Callable[..., tuple[str | None, bool]] | None,
    select_candidate_interactive: Callable[..., tuple[Path | None, bool]] | None,
    prompt_text: Callable[..., str],
) -> WorkspaceBinding:
    while True:
        candidates = discover_workspace_candidates(binding.workspace_context.workspace_root)
        selected_action = _choose_guarded_action(
            binding=binding,
            candidates=candidates,
            allow_use_current_action=allow_use_current_action,
            console=console,
            select_action_interactive=select_action_interactive,
            prompt_text=prompt_text,
        )
        if selected_action == _ACTION_CANCEL:
            raise WorkspaceBindingError("Workspace binding cancelled.")
        if selected_action == _ACTION_USE_CURRENT:
            if not allow_use_current_action:
                raise WorkspaceBindingError(
                    "This command requires a narrower workspace. "
                    "Pass --allow-broad-workspace to continue."
                )
            return resolve_workspace_binding(
                binding.requested_path,
                allow_broad_workspace=True,
                source=binding.binding_source,
            )
        if selected_action == _ACTION_CHOOSE_PROJECT:
            selected_path = _choose_workspace_candidate(
                base_path=binding.workspace_context.workspace_root,
                candidates=candidates,
                console=console,
                select_candidate_interactive=select_candidate_interactive,
                prompt_text=prompt_text,
            )
            if selected_path is None:
                continue
            return resolve_startup_workspace_binding(
                requested_path=selected_path,
                interactive=True,
                create_if_missing=False,
                allow_broad_workspace=False,
                source="startup_candidate",
                action=policy_action,
                console=console,
                select_action_interactive=select_action_interactive,
                select_candidate_interactive=select_candidate_interactive,
                prompt_text=prompt_text,
            )
        if selected_action == _ACTION_CREATE_FOLDER:
            created_path = _prompt_new_child_path(
                base_path=binding.workspace_context.workspace_root,
                prompt_text=prompt_text,
            )
            if created_path is None:
                continue
            return resolve_startup_workspace_binding(
                requested_path=created_path,
                interactive=True,
                create_if_missing=True,
                allow_broad_workspace=False,
                source="startup_create_path",
                action=policy_action,
                console=console,
                select_action_interactive=select_action_interactive,
                select_candidate_interactive=select_candidate_interactive,
                prompt_text=prompt_text,
            )
        if selected_action == _ACTION_ENTER_PATH:
            entered_path = _prompt_workspace_path(
                base_path=binding.workspace_context.workspace_root,
                prompt_text=prompt_text,
            )
            if entered_path is None:
                continue
            return resolve_startup_workspace_binding(
                requested_path=entered_path,
                interactive=True,
                create_if_missing=False,
                allow_broad_workspace=False,
                source="startup_entered_path",
                action=policy_action,
                console=console,
                select_action_interactive=select_action_interactive,
                select_candidate_interactive=select_candidate_interactive,
                prompt_text=prompt_text,
            )


def _choose_guarded_action(
    *,
    binding: WorkspaceBinding,
    candidates: tuple[WorkspaceCandidate, ...],
    allow_use_current_action: bool,
    console: Any | None,
    select_action_interactive: Callable[..., tuple[str | None, bool]] | None,
    prompt_text: Callable[..., str],
) -> str:
    if callable(select_action_interactive):
        selected, interactive_available = select_action_interactive(
            binding=binding,
            candidates=candidates,
            allow_use_current_action=allow_use_current_action,
            console=console,
        )
        if interactive_available and selected is not None:
            return selected
    if allow_use_current_action:
        prompt = "Workspace action [1=use current|2=choose project|3=create folder|4=enter path|5=cancel]"
        default = "2" if candidates else "3"
    else:
        prompt = "Workspace action [1=choose project|2=create folder|3=enter path|4=cancel]"
        default = "1" if candidates else "2"
    try:
        raw = prompt_text(
            prompt,
            default=default,
        )
    except (EOFError, KeyboardInterrupt):
        return _ACTION_CANCEL
    return _normalize_guarded_action(raw, allow_use_current_action=allow_use_current_action)


def _choose_workspace_candidate(
    *,
    base_path: Path,
    candidates: tuple[WorkspaceCandidate, ...],
    console: Any | None,
    select_candidate_interactive: Callable[..., tuple[Path | None, bool]] | None,
    prompt_text: Callable[..., str],
) -> Path | None:
    if not candidates:
        typed_path = _prompt_workspace_path(base_path=base_path, prompt_text=prompt_text)
        return typed_path
    if callable(select_candidate_interactive):
        selected, interactive_available = select_candidate_interactive(
            base_path=base_path,
            candidates=candidates,
            console=console,
        )
        if interactive_available:
            return selected

    rows = workspace_candidate_rows(base_path=base_path, candidates=candidates)
    try:
        raw = prompt_text(
            f"Project folder [1-{len(rows)} or blank to cancel]",
            default="1",
        )
    except (EOFError, KeyboardInterrupt):
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    if text.isdigit():
        index = int(text)
        if 1 <= index <= len(rows):
            return Path(rows[index - 1][0])
    raise WorkspaceBindingError("Invalid project selection.")


def _normalize_guarded_action(raw: str, *, allow_use_current_action: bool = True) -> str:
    value = str(raw or "").strip().casefold()
    if allow_use_current_action:
        aliases = {
            "1": _ACTION_USE_CURRENT,
            "use": _ACTION_USE_CURRENT,
            "use_current": _ACTION_USE_CURRENT,
            "current": _ACTION_USE_CURRENT,
            "2": _ACTION_CHOOSE_PROJECT,
            "choose": _ACTION_CHOOSE_PROJECT,
            "project": _ACTION_CHOOSE_PROJECT,
            "choose_project": _ACTION_CHOOSE_PROJECT,
            "3": _ACTION_CREATE_FOLDER,
            "create": _ACTION_CREATE_FOLDER,
            "new": _ACTION_CREATE_FOLDER,
            "create_folder": _ACTION_CREATE_FOLDER,
            "4": _ACTION_ENTER_PATH,
            "enter": _ACTION_ENTER_PATH,
            "path": _ACTION_ENTER_PATH,
            "enter_path": _ACTION_ENTER_PATH,
            "5": _ACTION_CANCEL,
            "cancel": _ACTION_CANCEL,
            "quit": _ACTION_CANCEL,
        }
    else:
        aliases = {
            "1": _ACTION_CHOOSE_PROJECT,
            "choose": _ACTION_CHOOSE_PROJECT,
            "project": _ACTION_CHOOSE_PROJECT,
            "choose_project": _ACTION_CHOOSE_PROJECT,
            "2": _ACTION_CREATE_FOLDER,
            "create": _ACTION_CREATE_FOLDER,
            "new": _ACTION_CREATE_FOLDER,
            "create_folder": _ACTION_CREATE_FOLDER,
            "3": _ACTION_ENTER_PATH,
            "enter": _ACTION_ENTER_PATH,
            "path": _ACTION_ENTER_PATH,
            "enter_path": _ACTION_ENTER_PATH,
            "4": _ACTION_CANCEL,
            "cancel": _ACTION_CANCEL,
            "quit": _ACTION_CANCEL,
        }
    return aliases.get(value, _DEFAULT_ACTION)


def _prompt_new_child_path(*, base_path: Path, prompt_text: Callable[..., str]) -> Path | None:
    try:
        raw = prompt_text("New folder name", default="new-project")
    except (EOFError, KeyboardInterrupt):
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    rel = Path(text)
    if rel.is_absolute():
        raise WorkspaceBindingError("New folder name must be relative to the current directory.")
    target = (base_path / rel).expanduser().resolve(strict=False)
    _ensure_within_base(base_path=base_path, candidate=target)
    return target


def _prompt_workspace_path(*, base_path: Path, prompt_text: Callable[..., str]) -> Path | None:
    try:
        raw = prompt_text("Workspace path")
    except (EOFError, KeyboardInterrupt):
        return None
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        candidate = base_path / candidate
    return candidate.resolve(strict=False)


def _ensure_within_base(*, base_path: Path, candidate: Path) -> None:
    try:
        candidate.relative_to(base_path.resolve())
    except ValueError as e:
        raise WorkspaceBindingError(
            f"Path must stay under {base_path.resolve()}: {candidate}"
        ) from e


def _display_path(path: Path) -> str:
    return os.fspath(path.expanduser().resolve(strict=False))
