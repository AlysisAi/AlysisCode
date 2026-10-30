# ruff: noqa: F821
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

import typer
from rich.text import Text

from ...agent.llm_calls import _request_messages_with_volatile_suffix
from ...config import AppConfig, ConfigError
from ...forge_completion import build_forge_completion_report
from ...llm.base import effective_tools_for_client
from ...personas import is_persona_name, normalize_persona, persona_modes_enabled
from ...plan_repair import apply_plan_status
from ...plan_validation import PlannerFailedError, raise_for_execution_ready_plan
from ...surface.styles import (
    STYLE_CHROME,
    STYLE_EMPHASIS,
    STYLE_SUCCESS,
    STYLE_WARN,
)
from ...usage_tracker import UsageRecord, aggregate_usage_from_session_logs
from .forge_visibility import command_visible
from .state import _ChatExecutionRequest, _ForgeChatState

_PROTECTED_COMMAND_GLOBAL_NAMES: set[str] = set()
_SYNCED_COMMAND_GLOBAL_VALUES: dict[str, Any] = {}
_COMMAND_SYNC_MISSING = object()
_PATCHABLE_COMMAND_GLOBAL_NAMES: set[str] = {
    "_apply_config_menu_changes_to_session",
    "_is_non_interactive_terminal",
    "load_config",
}
_LOGGER = logging.getLogger("alysis_code.cli_impl.chat.commands")
_TERMINALS_DISPLAY_LINE_LIMIT = 200
_TERMINALS_USAGE_LINES = (
    "Usage: /terminals",
    "       /terminals list",
    "       /terminals show <process_id>",
    "       /terminals kill <process_id>",
    "       /terminals help",
)


def acquire_swarm_mutation_guard(*args: Any, **kwargs: Any) -> Any:
    from ...swarm_orchestrator import acquire_swarm_mutation_guard as acquire

    return acquire(*args, **kwargs)


def _record_chat_local_command(*, session: Any, command_token: str, has_argument: bool) -> None:
    """Persist a privacy-safe marker for a locally dispatched chat command."""

    normalized = str(command_token or "").strip().lower()
    if normalized in {"exit", "/exit"}:
        normalized = "/exit"
    elif normalized in {"quit", "/quit"}:
        normalized = "/quit"
    elif not normalized.startswith("/"):
        return

    store = getattr(session, "store", None)
    append = getattr(store, "append", None)
    if callable(append):
        append(
            "chat_local_command",
            {
                "command": normalized,
                "has_argument": bool(has_argument),
            },
        )


def _handle_unknown_chat_command(*, command: str, console: Console, ui_mode: str) -> str:
    suggestion = _suggest_chat_command(command, ui_mode=ui_mode)
    if suggestion:
        console.print(
            f"[yellow]Unknown command:[/yellow] {command}. Did you mean {suggestion}? Try /help."
        )
        return "handled"
    console.print(f"[yellow]Unknown command:[/yellow] {command}. Try /help.")
    return "handled"


def _merge_usage_payloads_into_session(
    *, session: Any, payloads: list[dict[str, Any]] | tuple[dict[str, Any], ...]
) -> int:
    summary = getattr(session, "usage_summary", None)
    add_event_payload = getattr(summary, "add_event_payload", None)
    store = getattr(session, "store", None)
    append = getattr(store, "append", None)
    merged = 0
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if str(payload.get("event_type") or "") != "llm_usage":
            continue
        if callable(add_event_payload):
            try:
                add_event_payload(payload)
            except Exception:
                continue
        if callable(append):
            try:
                append("llm_usage", dict(payload))
            except Exception:
                pass
        merged += 1
    return merged


def _merge_usage_records_into_session(
    *, session: Any, records: list[UsageRecord] | tuple[UsageRecord, ...]
) -> int:
    summary = getattr(session, "usage_summary", None)
    add_record = getattr(summary, "add_record", None)
    store = getattr(session, "store", None)
    append = getattr(store, "append", None)
    merged = 0
    for record in records:
        if not isinstance(record, UsageRecord):
            continue
        if callable(add_record):
            try:
                add_record(record)
            except Exception:
                continue
        if callable(append):
            try:
                append("llm_usage", record.to_payload())
            except Exception:
                pass
        merged += 1
    return merged


def _merge_forge_execution_usage_into_session(*, session: Any, paths: Any) -> int:
    logs_dir = getattr(paths, "execution_logs_dir", None)
    if logs_dir is None:
        return 0
    try:
        imported = set(getattr(session, "_alysis_imported_forge_usage_logs", set()) or set())
        log_paths = sorted(Path(logs_dir).glob("*.jsonl"))
    except Exception:
        return 0
    selected: list[Path] = []
    selected_keys: list[str] = []
    for path in log_paths:
        try:
            key = os.fspath(path.resolve())
        except Exception:
            key = os.fspath(path)
        if key in imported:
            continue
        selected.append(path)
        selected_keys.append(key)
    if not selected:
        return 0
    try:
        summary = aggregate_usage_from_session_logs(selected)
        records = summary.records()
    except Exception:
        return 0
    merged = _merge_usage_records_into_session(session=session, records=records)
    if merged:
        imported.update(selected_keys)
        try:
            session._alysis_imported_forge_usage_logs = imported
        except Exception:
            pass
    return merged


def _print_alysis_trial_models(console: Console, session: Any, *, current: str) -> None:
    """List the models the hosted Alysis account serves, when on that profile.

    Best-effort and silent otherwise: only runs when the active profile is the
    hosted `alysis` account, discovers the live allowlist via the proxy, and
    never raises into the chat loop.
    """
    cfg = getattr(session, "cfg", None)
    if cfg is None:
        return
    try:
        from ...alysis_cloud import PROFILE_KEY

        active = str((getattr(cfg, "extra_fields", {}) or {}).get("active_profile") or "").strip()
        if active != PROFILE_KEY:
            return
        from ... import account_login

        models = account_login.list_trial_models(cfg)
    except Exception:  # noqa: BLE001 - listing is best-effort
        return
    if not models:
        return
    from ...model_reasoning import hosted_model_capabilities

    console.print("Models on your Alysis account:")
    for model_id in models:
        marker = "  (current)" if model_id == current else ""
        capabilities = hosted_model_capabilities(model_id)
        efforts = ", ".join(item["label"] for item in capabilities["reasoning_efforts"])
        detail = f"Auto, {efforts}" if efforts else "Auto (provider default)"
        console.print(f"  • {model_id}{marker} · effort: {detail}", markup=False)
    console.print("[dim]Switch with: /model <name>[/dim]")


def _print_forge_lock_wait_notice(console: Console, info: dict[str, Any]) -> None:
    diagnostic = str(info.get("diagnostic") or "").strip()
    warnings = ["Another Forge execution is mutating this workspace; queued and waiting."]
    if diagnostic:
        warnings.append(diagnostic)
    _print_forge_warning_messages(
        console=console, label="Forge execution queued", warnings=warnings
    )


def _sync_command_globals(source_globals: dict[str, Any]) -> None:
    module_globals = globals()
    if not _PROTECTED_COMMAND_GLOBAL_NAMES:
        for local_name, local_value in module_globals.items():
            if callable(local_value):
                _PROTECTED_COMMAND_GLOBAL_NAMES.add(local_name)
    for name, value in source_globals.items():
        if name.startswith("__"):
            continue
        if name in _PROTECTED_COMMAND_GLOBAL_NAMES and name not in _PATCHABLE_COMMAND_GLOBAL_NAMES:
            continue
        previous_synced_value = _SYNCED_COMMAND_GLOBAL_VALUES.get(name, _COMMAND_SYNC_MISSING)
        if (
            previous_synced_value is not _COMMAND_SYNC_MISSING
            and module_globals.get(name, _COMMAND_SYNC_MISSING) is not previous_synced_value
        ):
            # Preserve an explicit override applied at the command-module
            # compatibility surface.  Without tracking the last value injected
            # by the facade, the next command dispatch silently replaced a
            # monkeypatch after the first command of the process.  That made
            # identical classic-chat calls behave differently depending on
            # which command test ran first.
            continue
        module_globals[name] = value
        _SYNCED_COMMAND_GLOBAL_VALUES[name] = value


def _handle_idle_skill_invocation(
    *,
    input_text: str,
    root: Path,
    session: Any,
    pending_images: list[str],
    console: Console,
    forge_state: _ForgeChatState,
    forge_planner_reply_sink: Any | None = None,
    forge_execution_report_sink: Any | None = None,
) -> str | _ChatExecutionRequest | None:
    """Route an idle ``$name [task]`` submission through the existing skill flow."""

    trimmed = str(input_text or "").strip()
    if not trimmed.startswith("$"):
        return None
    parts = trimmed.split(maxsplit=1)
    skill_name = parts[0][1:].strip()
    if not skill_name:
        return None
    ordered_skills = tuple(getattr(session, "skills_ordered", ()) or ())
    registry_obj = getattr(session, "skill_registry", None)
    registry = registry_obj if isinstance(registry_obj, dict) else {}
    if not registry and ordered_skills:
        registry = {str(getattr(skill, "name", "")).casefold(): skill for skill in ordered_skills}
    if registry.get(skill_name.casefold()) is None:
        return None
    skill_arg = skill_name
    if len(parts) > 1 and parts[1].strip():
        skill_arg = f"{skill_arg} {parts[1].strip()}"
    return _handle_chat_command(
        input_text=f"/skill {skill_arg}",
        root=root,
        session=session,
        pending_images=pending_images,
        console=console,
        forge_state=forge_state,
        forge_planner_reply_sink=forge_planner_reply_sink,
        forge_execution_report_sink=forge_execution_report_sink,
    )


def _handle_chat_command(
    *,
    input_text: str,
    root: Path,
    session: Any,
    pending_images: list[str],
    console: Console,
    forge_state: _ForgeChatState,
    forge_planner_reply_sink: Any | None = None,
    forge_execution_report_sink: Any | None = None,
    record_local_command: bool = True,
) -> str | _ChatExecutionRequest:
    trimmed = input_text.strip()
    if not trimmed:
        return "handled"

    parts = trimmed.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if record_local_command:
        _record_chat_local_command(
            session=session,
            command_token=cmd,
            has_argument=bool(arg),
        )

    if not command_visible(cmd, ui_mode=forge_state.ui_mode):
        return _handle_unknown_chat_command(
            command=parts[0],
            console=console,
            ui_mode=forge_state.ui_mode,
        )

    if _is_forge_ui_mode(forge_state.ui_mode):
        forge_action = _handle_forge_chat_command(
            input_text=input_text,
            forge_state=forge_state,
            session=session,
            console=console,
            forge_planner_reply_sink=forge_planner_reply_sink,
            forge_execution_report_sink=forge_execution_report_sink,
        )
        if forge_action != "unhandled":
            return forge_action

    parsed_forge_enter = _parse_forge_enter_command(cmd=cmd, arg=arg)

    if cmd in {"exit", "quit", "/exit", "/quit"}:
        return "exit"
    if parsed_forge_enter is not None:
        if parsed_forge_enter.usage_error is not None:
            console.print(f"[red]{parsed_forge_enter.usage_error}[/red]")
            for line in _forge_enter_usage_lines():
                console.print(line)
            return "handled"
        if _is_forge_ui_mode(forge_state.ui_mode):
            console.print("Forge is already active.")
            return "handled"
        forge_state.entry_request_mode = parsed_forge_enter.entry_mode
        forge_state.entry_goal = parsed_forge_enter.initial_goal
        try:
            forge_root = _resolve_forge_entry_root(session=session, fallback_root=root)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Failed to enter Forge:[/red] {e}")
            return "handled"
        forge_model = getattr(getattr(session, "client", None), "model", None)
        forge_mode = getattr(session, "mode", None)
        enter_kwargs = {
            "root": forge_root,
            "console": console,
            "forge_state": forge_state,
            "model": str(forge_model) if forge_model else None,
            "mode": str(forge_mode) if forge_mode else None,
        }
        if parsed_forge_enter.planner_assistant_default is not None:
            # Explicit choice from the TUI intro popup's "Use the planner?" prompt
            # (or a direct ``/forge --planner`` / ``/forge --no-planner``) wins.
            enter_kwargs["planner_assistant_default"] = parsed_forge_enter.planner_assistant_default
        elif bool(getattr(session, "_alysis_tui_interactive", False)):
            # Bare/goal/resume entry in the alt-screen TUI can't run the classic
            # stdin plan-assistant prompt, so default it on (unchanged behavior).
            enter_kwargs["planner_assistant_default"] = True
        _enter_forge_mode(**enter_kwargs)
        return "handled"
    if cmd == "/":
        console.print(_chat_quick_commands_panel(ui_mode=forge_state.ui_mode))
        return "handled"
    if cmd == "/help":
        console.print(_chat_help_panel(ui_mode=forge_state.ui_mode))
        return "handled"
    if cmd in {"/status"}:
        _print_chat_status(console=console, session=session, pending_images=pending_images)
        return "handled"
    if cmd == "/subagents":
        if arg.strip():
            console.print("[yellow]Usage:[/yellow] /subagents")
            return "handled"
        scheduler = getattr(session, "child_scheduler", None)
        if scheduler is None:
            console.print("no active subagents")
            return "handled"
        try:
            status = scheduler.status()
        except Exception as exc:  # noqa: BLE001 - inspection is best-effort
            console.print(f"[red]/subagents failed:[/red] {exc}")
            return "handled"
        raw_children = status.get("children") if isinstance(status, dict) else None
        active_children = [
            child
            for child in (raw_children if isinstance(raw_children, list) else [])
            if isinstance(child, dict)
            and str(child.get("state") or "").strip().lower()
            in {"spawned", "queued", "waiting", "running"}
        ]
        if not active_children:
            console.print("no active subagents")
            return "handled"
        lines: list[str] = []
        for child in active_children:
            lines.append(
                " | ".join(
                    (
                        str(child.get("subagent") or "subagent"),
                        str(child.get("state") or "unknown"),
                        str(child.get("workspace_view") or "shared"),
                        f"{int(child.get('steps_completed') or 0)} steps",
                        str(child.get("activity") or "working"),
                    )
                )
            )
        console.print("\n".join(lines))
        return "handled"
    if cmd == "/ask":
        remainder = arg.strip()
        if not remainder:
            console.print("[yellow]Usage:[/yellow] /ask <question> — one read-only turn")
            return "handled"
        current_mode = str(getattr(session, "mode", "review") or "review").strip().lower()
        # Always describe the one-turn read-only scope explicitly. Even when the
        # current mode is already readonly, a staged Permissions selection may
        # activate at this message boundary before the overlay is applied.
        return _ChatExecutionRequest(
            instruction=remainder,
            mode_override="readonly",
            restore_mode_after=current_mode,
        )
    if cmd == "/chat":
        # Retired in favor of the Ask persona; the chat_only plumbing stays
        # accepted-and-ignored for one release (no producer).
        console.print(
            "[yellow]/chat is retired.[/yellow] Use /persona ask for a persistent "
            "read-only Q&A persona, or /ask <question> for one read-only turn."
        )
        return "handled"
    if cmd in {"/terminals"}:
        _handle_terminals_command(arg=arg, session=session, console=console)
        return "handled"
    if cmd in {"/pwd"}:
        _print_chat_pwd(console=console, session=session)
        return "handled"
    if cmd in {"/cd"}:
        requested_path = arg.strip()
        if not requested_path:
            console.print("[yellow]Usage:[/yellow] /cd <path>")
            return "handled"
        try:
            result = set_session_active_workdir(
                session,
                requested_path,
                source="chat_command",
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Failed to change active workdir:[/red] {e}")
            return "handled"
        console.print(
            f"Active workdir: {result['active_workdir']} ({result['active_workdir_relpath']})"
        )
        return "handled"
    if cmd in {"/resume"}:
        cfg = getattr(session, "cfg", None)
        if not isinstance(cfg, AppConfig):
            console.print("[red]Resume unavailable:[/red] missing session config.")
            return "handled"

        store = getattr(session, "store", None)
        sessions_dir_raw = getattr(store, "sessions_dir", None)
        sessions_dir = (
            Path(sessions_dir_raw) if sessions_dir_raw is not None else resolve_sessions_dir(cfg)
        )
        current_session_id = str(getattr(store, "session_id", "") or "")

        candidates = _collect_chat_resume_candidates(
            sessions_dir=sessions_dir,
            current_session_id=current_session_id,
            workspace_root=getattr(store, "workspace_root", None),
            git_root=getattr(store, "git_root", None),
        )
        target_session_id: str | None = None
        raw_arg = arg.strip()
        # Only the bare-picker form needs candidates; an explicit
        # "/resume <id>" must still reach the direct-id escape hatch below even
        # when owner/workspace scoping filtered the list down to nothing.
        if not candidates and not raw_arg:
            console.print("No previous sessions available to resume.")
            return "handled"

        if not raw_arg:
            target_session_id, picker_available = _select_chat_resume_interactive(
                current_session_id=current_session_id,
                sessions=candidates,
                console=console,
            )
            if not picker_available:
                console.print(
                    _chat_resume_panel(
                        current_session_id=current_session_id,
                        sessions=candidates,
                    )
                )
                if _is_non_interactive_terminal():
                    console.print("Use /resume <index|session_id> to continue a previous session.")
                    return "handled"
                try:
                    fallback_choice = typer.prompt(
                        "Resume session (index/session_id, Enter to cancel)",
                        default="",
                    ).strip()
                except (EOFError, KeyboardInterrupt):
                    console.print("")
                    return "handled"
                if not fallback_choice:
                    return "handled"
                target_session_id = _resolve_chat_resume_target(
                    raw_value=fallback_choice,
                    sessions=candidates,
                )
                if target_session_id is None and not fallback_choice.isdigit():
                    target_session_id = _resolve_chat_resume_direct_session_id(
                        raw_value=fallback_choice,
                        sessions_dir=sessions_dir,
                    )
                if target_session_id is None:
                    console.print(
                        "[red]Invalid session.[/red] "
                        "Use /resume for picker or /resume <index|session_id>."
                    )
                    return "handled"
            elif target_session_id is None:
                return "handled"
        else:
            target_session_id = _resolve_chat_resume_target(
                raw_value=raw_arg,
                sessions=candidates,
            )
            if target_session_id is None and not raw_arg.isdigit():
                target_session_id = _resolve_chat_resume_direct_session_id(
                    raw_value=raw_arg,
                    sessions_dir=sessions_dir,
                )
            if target_session_id is None:
                console.print(
                    "[red]Invalid session.[/red] "
                    "Use /resume for picker or /resume <index|session_id>."
                )
                return "handled"

        pending_permissions = _pending_chat_permissions(session)
        resumed, message, history_messages = _resume_chat_session(
            session=session,
            target_session_id=target_session_id,
        )
        console.print(message)
        if resumed:
            pending_images.clear()
            # session_start restored the base mode; the persona (and with it
            # the narrowed mode, write scope, and model role) rides on top.
            _reapply_resumed_persona(session=session, console=console)
            # A next-message choice belongs to the live UI interaction, not the
            # history being resumed. Restore the historical persona first so
            # staging can distinguish its temporary narrowing from the user's
            # selected base Permissions.
            if pending_permissions is not None:
                _stage_chat_permissions(
                    session=session,
                    next_mode=pending_permissions,
                )
            _render_chat_resume_history(
                session=session,
                messages=history_messages,
            )
        return "handled"

    if cmd == "/usage":
        raw_usage_arg = arg.strip()
        if not raw_usage_arg:
            _print_chat_usage(console=console, session=session)
            return "handled"
        parts_usage = raw_usage_arg.split(maxsplit=1)
        if parts_usage[0].strip().lower() != "hud":
            console.print("Usage: /usage  (no args = print) or /usage hud on|off|status")
            return "handled"
        usage_arg = parts_usage[1].strip().lower() if len(parts_usage) > 1 else ""
        if not usage_arg:
            usage_arg, picker_available = _select_chat_usage_hud_interactive(
                current_enabled=_chat_usage_hud_enabled(session),
                console=console,
            )
            if not picker_available:
                state = "on" if _chat_usage_hud_enabled(session) else "off"
                console.print(f"Usage HUD: {state}")
                console.print("[dim]Usage: /usage hud on|off|status[/dim]")
                return "handled"
            if usage_arg is None:
                return "handled"
            usage_arg = str(usage_arg).strip().lower()
        if usage_arg == "status":
            state = "on" if _chat_usage_hud_enabled(session) else "off"
            console.print(f"Usage HUD: {state}")
            return "handled"
        parsed = _parse_bool_text(usage_arg)
        if parsed is None:
            console.print("[yellow]Usage:[/yellow] /usage hud on|off|status")
            return "handled"
        _set_chat_usage_hud_enabled(session, parsed)
        console.print(f"Usage HUD set for this session: {'on' if parsed else 'off'}")
        return "handled"
    if cmd in {"/toolbar"}:
        cfg_obj = getattr(session, "cfg", None)
        raw_items = getattr(cfg_obj, "toolbar_items", None) if cfg_obj is not None else None
        active_items: list[str] = []
        seen_items: set[str] = set()
        for raw_item in list(raw_items or list(_DEFAULT_TOOLBAR_ITEMS)):
            item = str(raw_item).strip().lower()
            if not item or item in seen_items or item not in _VALID_TOOLBAR_ITEMS:
                continue
            seen_items.add(item)
            active_items.append(item)
        active_set = set(active_items)
        available_items = [item for item in _CHAT_TOOLBAR_ITEM_ORDER if item not in active_set]
        valid_items_display = ", ".join(_CHAT_TOOLBAR_ITEM_ORDER)

        toolbar_arg = arg.strip()
        if not toolbar_arg:
            active_display = ", ".join(active_items) if active_items else "(none)"
            available_display = ", ".join(available_items) if available_items else "(none)"
            lines = [
                f"Active: {active_display}",
                f"Available: {available_display}",
                "",
                "/toolbar add <item>    add an item",
                "/toolbar remove <item> remove an item",
                "/toolbar reset         restore defaults",
                "/toolbar save          persist to config",
            ]
            console.print(
                _Panel("\n".join(lines), title="Toolbar Items", border_style="bright_black")
            )
            return "handled"

        toolbar_parts = toolbar_arg.split(maxsplit=1)
        action = toolbar_parts[0].lower()
        item_arg = toolbar_parts[1].strip().lower() if len(toolbar_parts) > 1 else ""

        if action == "add":
            if not item_arg:
                console.print("[yellow]Usage:[/yellow] /toolbar add <item>")
                return "handled"
            if item_arg not in _VALID_TOOLBAR_ITEMS:
                console.print(
                    f"[red]Invalid toolbar item:[/red] {item_arg}. "
                    f"Valid items: {valid_items_display}"
                )
                return "handled"
            if item_arg in active_set:
                console.print(f"Toolbar item already active: {item_arg}")
                return "handled"
            if not isinstance(cfg_obj, AppConfig):
                console.print("[red]Toolbar settings unavailable:[/red] missing session config.")
                return "handled"
            cfg_obj.toolbar_items = [*active_items, item_arg]
            console.print(f"Toolbar item added for this session: {item_arg}")
            return "handled"

        if action == "remove":
            if not item_arg:
                console.print("[yellow]Usage:[/yellow] /toolbar remove <item>")
                return "handled"
            if item_arg not in _VALID_TOOLBAR_ITEMS:
                console.print(
                    f"[red]Invalid toolbar item:[/red] {item_arg}. "
                    f"Valid items: {valid_items_display}"
                )
                return "handled"
            if item_arg not in active_set:
                console.print(f"Toolbar item is not active: {item_arg}")
                return "handled"
            if not isinstance(cfg_obj, AppConfig):
                console.print("[red]Toolbar settings unavailable:[/red] missing session config.")
                return "handled"
            cfg_obj.toolbar_items = [item for item in active_items if item != item_arg]
            console.print(f"Toolbar item removed for this session: {item_arg}")
            return "handled"

        if action == "reset":
            if not isinstance(cfg_obj, AppConfig):
                console.print("[red]Toolbar settings unavailable:[/red] missing session config.")
                return "handled"
            cfg_obj.toolbar_items = list(_DEFAULT_TOOLBAR_ITEMS)
            console.print(
                "Toolbar items reset for this session: "
                + ", ".join(str(item) for item in cfg_obj.toolbar_items)
            )
            return "handled"

        if action == "save":
            if not isinstance(cfg_obj, AppConfig):
                console.print("[red]Toolbar settings unavailable:[/red] missing session config.")
                return "handled"
            save_config(cfg_obj)
            console.print(f"Saved toolbar settings: {config_path()}")
            return "handled"

        console.print("[yellow]Usage:[/yellow] /toolbar [add|remove|reset|save]")
        return "handled"
    if cmd == "/skill":
        if _is_forge_ui_mode(forge_state.ui_mode):
            console.print("Skills are unavailable in Forge.")
            return "handled"
        raw_skill_arg = arg.strip()
        enabled, ordered_skills, issues = _session_skill_listing(session)
        if not raw_skill_arg:
            if ordered_skills:
                console.print(
                    _skills_table(skills=ordered_skills, title=f"Skills ({len(ordered_skills)})")
                )
            elif enabled:
                console.print("No skills discovered in the supported skill roots.")
            else:
                console.print("Skills are disabled for this session config.")
            for issue in issues:
                source_path = getattr(issue, "source_path", None)
                message = str(getattr(issue, "message", "") or "").strip()
                if source_path is not None and message:
                    console.print(f"[yellow]Skipped skill:[/yellow] {source_path} ({message})")
            return "handled"
        registry_obj = getattr(session, "skill_registry", None)
        registry = registry_obj if isinstance(registry_obj, dict) else {}
        if not registry and ordered_skills:
            registry = {
                str(getattr(skill, "name", "")).casefold(): skill for skill in ordered_skills
            }
        parts_skill = raw_skill_arg.split(maxsplit=1)
        skill_name = parts_skill[0].strip()
        skill = resolve_skill_by_name(registry, skill_name)
        catalog_entries_obj = getattr(session, "skill_catalog_entries", ())
        catalog_entries = (
            tuple(item for item in catalog_entries_obj if item is not None)
            if isinstance(catalog_entries_obj, tuple)
            else tuple(item for item in (catalog_entries_obj or []) if item is not None)
        )
        catalog_entry = resolve_skill_catalog_entry(entries=catalog_entries, raw_name=skill_name)
        if skill is None:
            console.print(f"[red]Skill not found:[/red] {skill_name}")
            if ordered_skills:
                console.print(
                    "Available skills: "
                    + ", ".join(str(getattr(skill_obj, "name", "")) for skill_obj in ordered_skills)
                )
            return "handled"
        if len(parts_skill) == 1 or not parts_skill[1].strip():
            console.print(render_skill_info_text(skill, catalog_entry=catalog_entry))
            return "handled"
        if not enabled:
            console.print("Skills are disabled for this session config.")
            return "handled"
        skill_task = parts_skill[1].strip()
        return _ChatExecutionRequest(
            instruction=skill_task,
            ephemeral_user_messages=(
                build_explicit_skill_context_message(skill=skill, task_text=skill_task),
            ),
        )
    if cmd in {"/context", "/ctx"}:
        _print_chat_context(console=console, session=session)
        return "handled"
    if cmd in {"/report", "/feedback"}:
        cfg = (
            getattr(session, "cfg", None)
            if isinstance(getattr(session, "cfg", None), AppConfig)
            else None
        )
        try:
            result = create_feedback_bundle(
                workspace_root=root,
                feedback_text=arg or None,
                cfg=cfg,
                active_session=session,
                active_run_paths=(
                    forge_state.paths if _is_forge_ui_mode(forge_state.ui_mode) else None
                ),
                pending_images=list(pending_images),
            )
        except (ConfigError, FeedbackReportError) as exc:
            console.print(f"[red]Feedback report failed:[/red] {exc}")
            return "handled"
        console.print(f"Feedback bundle directory: {result.bundle_dir}")
        console.print(f"Feedback bundle archive: {result.zip_path}")
        try:
            issue_result = create_feedback_github_issue_draft(
                bundle_result=result,
                feedback_text=arg or None,
                cfg=cfg,
            )
        except Exception as exc:  # noqa: BLE001 - GitHub issue drafting is best-effort.
            console.print(f"[yellow]GitHub issue draft skipped:[/yellow] {exc}")
        else:
            for line in feedback_github_issue_status_lines(issue_result):
                console.print(line)
        return "handled"
    if cmd in {"/compact"}:
        if _is_forge_ui_mode(forge_state.ui_mode):
            console.print("Compaction is disabled in Forge.")
            return "handled"
        compactor = getattr(session, "conversation_compactor", None)
        if compactor is None:
            console.print("Compaction unavailable for this session (disabled or not supported).")
            return "handled"
        focus = arg.strip() or None
        messages_obj = getattr(session, "messages", [])
        messages = messages_obj if isinstance(messages_obj, list) else []
        tool_list_obj = getattr(session, "tool_list", None)
        tool_list = tool_list_obj if isinstance(tool_list_obj, list) else None
        tool_list = effective_tools_for_client(getattr(session, "client", None), tool_list)
        tokens_before = _estimate_request_tokens(messages, tool_list)
        state = getattr(compactor, "state", None)
        chunks_before = int(getattr(state, "history_chunk_index", 0) or 0)
        pins_before = len(getattr(state, "pins", []) or [])
        compact_fn = getattr(compactor, "compact_now", None)
        if not callable(compact_fn):
            console.print("Compaction unavailable for this session (missing compact_now).")
            return "handled"
        refresh_calibration = getattr(session, "refresh_compactor_calibration_filters", None)
        if callable(refresh_calibration):
            refresh_calibration()
        new_messages, changed = compact_fn(
            messages=messages,
            tool_list=tool_list,
            main_model=str(getattr(getattr(session, "client", None), "model", "") or ""),
            cache_policy=getattr(
                getattr(session, "client", None),
                "prompt_cache_policy_metadata",
                None,
            ),
            focus=focus,
            request_messages_builder=lambda history: _request_messages_with_volatile_suffix(
                messages=history
            ),
        )
        if isinstance(new_messages, list):
            session.messages = new_messages
            if changed:
                read_ledger = getattr(session, "read_ledger", None)
                if read_ledger is not None:
                    read_ledger.reset()
        if changed:
            invalidate_request_context = getattr(session, "invalidate_request_context", None)
            if callable(invalidate_request_context):
                invalidate_request_context(reason="manual_compaction")
            elif hasattr(session, "request_context_measurement"):
                session.request_context_measurement = None
        tokens_after = _estimate_request_tokens(getattr(session, "messages", []), tool_list)
        chunks_after = int(
            getattr(getattr(compactor, "state", None), "history_chunk_index", 0) or 0
        )
        pins_after = len(getattr(getattr(compactor, "state", None), "pins", []) or [])

        root = Path(getattr(session, "root", Path(".")))
        store_obj = getattr(session, "store", None)
        session_id = str(getattr(store_obj, "session_id", "") or "")
        history_artifact_base = None
        if hasattr(compactor, "history_dir"):
            history_dir_obj = getattr(compactor, "history_dir", None)
            if isinstance(history_dir_obj, Path):
                history_artifact_base = history_dir_obj.parent
        if history_artifact_base is None and session_id:
            history_artifact_base = root / ".alysis" / "sessions" / _safe_component(session_id)
        tool_output_artifact_base = getattr(store_obj, "session_artifact_root", None)

        table = _Table(title="Compaction")
        table.add_column("field")
        table.add_column("value")
        table.add_row("focus", focus or "-")
        table.add_row("main_model", str(getattr(getattr(session, "client", None), "model", "-")))
        table.add_row(
            "compactor_model",
            str(getattr(getattr(compactor, "compactor_client", None), "model", "-")),
        )
        table.add_row("compaction_profile", str(getattr(compactor, "profile_name", "-")))
        table.add_row("tokens_before", str(tokens_before))
        table.add_row("tokens_after", str(tokens_after))
        table.add_row("tokens_delta", str(tokens_after - tokens_before))
        table.add_row("chunks_created", str(max(0, chunks_after - chunks_before)))
        table.add_row("pins", f"{pins_after} (delta {pins_after - pins_before:+d})")
        table.add_row(
            "history_artifacts_dir",
            _artifact_display_ref(
                root=root,
                store_obj=store_obj,
                artifact_path=history_artifact_base,
            ),
        )
        table.add_row(
            "tool_output_artifacts_dir",
            _artifact_display_ref(
                root=root,
                store_obj=store_obj,
                artifact_path=(
                    tool_output_artifact_base / "tool_outputs"
                    if isinstance(tool_output_artifact_base, Path)
                    else None
                ),
            ),
        )
        console.print(table)
        _refresh_chat_hud_context_cache(session)
        if not changed:
            console.print("Nothing to compact (no eligible history beyond recent window).")
        return "handled"
    if cmd in {"/model-info"}:
        model_name = arg or str(getattr(getattr(session, "client", None), "model", "")).strip()
        if not model_name:
            console.print("Model info unavailable (missing model name).")
            return "handled"
        registry = getattr(session, "model_registry", None)
        if registry is None:
            console.print("Model info unavailable for this session.")
            return "handled"
        meta = registry.get(model_name)
        table = _Table(title=f"Model Info ({model_name})")
        table.add_column("field")
        table.add_column("value")
        field_sources = getattr(meta, "field_sources", {}) or {}
        table.add_row("resolved_model", meta.model_name)
        table.add_row("source", meta.source)
        table.add_row(
            "context_window_tokens",
            (
                f"{meta.context_window_tokens} "
                f"({field_sources.get('context_window_tokens', 'unknown')})"
            ),
        )
        table.add_row(
            "max_output_tokens",
            f"{meta.max_output_tokens} ({field_sources.get('max_output_tokens', 'unknown')})",
        )
        table.add_row(
            "supports_vision",
            f"{meta.supports_vision} ({field_sources.get('supports_vision', 'unknown')})",
        )
        table.add_row(
            "input_cost_per_token",
            f"{meta.input_cost_per_token} ({field_sources.get('input_cost_per_token', 'unknown')})",
        )
        table.add_row(
            "output_cost_per_token",
            (
                f"{meta.output_cost_per_token} "
                f"({field_sources.get('output_cost_per_token', 'unknown')})"
            ),
        )
        table.add_row("registry_last_error", str(getattr(registry, "last_error", None)))
        table.add_row("warnings", "; ".join(getattr(meta, "warnings", ())[:5]) or "-")
        for key, value in _bundled_catalog_provenance_rows(meta=meta, registry=registry):
            table.add_row(key, value)
        raw_keys = sorted(meta.raw_metadata.keys())
        preview_keys = ", ".join(raw_keys[:20]) if raw_keys else "(none)"
        if len(raw_keys) > 20:
            preview_keys += ", ..."
        table.add_row("raw_metadata_keys", preview_keys)
        console.print(table)
        return "handled"
    if cmd in {"/config"}:
        if not arg:
            if _is_non_interactive_terminal():
                console.print(_chat_config_panel(session=session))
                return "handled"
            from ..config_menu import run_config_menu
            from .loop import _ConfigReloadRequiresRestart

            result = run_config_menu()
            if not result.saved:
                console.print("Closed without saving.")
                return "handled"
            change_count = len(result.changes) + (1 if result.api_key_changed else 0)
            previous_cfg = getattr(session, "cfg", None)
            try:
                from ...profiles import connection_fingerprint

                reloaded = load_config()
                if previous_cfg is None or connection_fingerprint(
                    reloaded
                ) != connection_fingerprint(previous_cfg):
                    console.print(
                        "[yellow]Model access changed. This session is closing so the old and "
                        "new connection states are never mixed. Start `alysis chat` again.[/yellow]"
                    )
                    return "exit"
                _apply_config_menu_changes_to_session(session=session, cfg=reloaded)
            except _ConfigReloadRequiresRestart as e:
                console.print(
                    f"[yellow]Config saved. {e} This session is closing; start "
                    "`alysis chat` again.[/yellow]"
                )
                return "exit"
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]Config saved, but session reload failed:[/red] {e}")
                return "handled"
            if change_count > 0:
                change_word = "change" if change_count == 1 else "changes"
                console.print(
                    f"Saved {change_count} {change_word}. New settings apply on the next user turn."
                )
            else:
                console.print("Config saved (no changes).")
            return "handled"
        if arg.lower() in {"show", "list", "help"}:
            console.print(_chat_config_panel(session=session))
            return "handled"
        parts_config = arg.split()
        action = parts_config[0].lower()
        if action == "set":
            config_key = parts_config[1].lower() if len(parts_config) >= 2 else ""
            if config_key in {"model", "base_url"} and len(parts_config) == 3:
                from ... import config as config_mod

                try:
                    cfg_to_save = config_mod.load_config()
                    from ...profiles import connection_fingerprint

                    config_mod.ensure_subscription_menu_managed_key(cfg_to_save, config_key)
                    previous_fingerprint = connection_fingerprint(cfg_to_save)
                    config_mod.set_config_value(cfg_to_save, config_key, parts_config[2])
                    config_mod.save_config(cfg_to_save)
                    if connection_fingerprint(cfg_to_save) != previous_fingerprint:
                        console.print(
                            "[yellow]Model access changed. This session is closing so the old "
                            "and new connection states are never mixed. Start `alysis chat` "
                            "again.[/yellow]"
                        )
                        return "exit"
                    _apply_config_menu_changes_to_session(session=session, cfg=cfg_to_save)
                except Exception as e:  # noqa: BLE001
                    console.print(f"[red]Config update failed:[/red] {e}")
                    return "handled"
                console.print(f"Saved {config_key}. New setting applies on the next user turn.")
                return "handled"
            if len(parts_config) < 4 or len(parts_config) > 7:
                _print_chat_config_usage(console=console)
                return "handled"
            model_name, model_error = _resolve_chat_model_ref(
                session=session,
                raw_model_ref=parts_config[1],
            )
            if model_error is not None or model_name is None:
                console.print(f"[red]{model_error or 'Invalid model reference.'}[/red]")
                return "handled"

            context_window_tokens = parse_positive_int(parts_config[2])
            if context_window_tokens is None:
                console.print("[red]Invalid context_window_tokens.[/red] Use a positive integer.")
                return "handled"

            max_output_tokens = parse_positive_int(parts_config[3])
            if max_output_tokens is None:
                console.print("[red]Invalid max_output_tokens.[/red] Use a positive integer.")
                return "handled"

            updates: dict[str, Any] = {
                "context_window_tokens": context_window_tokens,
                "max_output_tokens": max_output_tokens,
            }

            if len(parts_config) >= 5:
                supports_vision = _parse_bool_text(parts_config[4])
                if supports_vision is None:
                    console.print("[red]Invalid supports_vision.[/red] Use true/false.")
                    return "handled"
                updates["supports_vision"] = supports_vision

            if len(parts_config) >= 6:
                input_cost = parse_non_negative_float(parts_config[5])
                if input_cost is None:
                    console.print(
                        "[red]Invalid input_cost_per_token.[/red] Use a non-negative number."
                    )
                    return "handled"
                updates["input_cost_per_token"] = input_cost

            if len(parts_config) >= 7:
                output_cost = parse_non_negative_float(parts_config[6])
                if output_cost is None:
                    console.print(
                        "[red]Invalid output_cost_per_token.[/red] Use a non-negative number."
                    )
                    return "handled"
                updates["output_cost_per_token"] = output_cost

            try:
                saved_path, merged = _save_chat_model_metadata_override(
                    session=session,
                    model_name=model_name,
                    fields=updates,
                )
            except ConfigError as e:
                console.print(f"[red]Config error:[/red] {e}")
                return "handled"
            _refresh_chat_hud_context_cache(session)
            console.print(f"Saved model metadata override for {model_name}.")
            console.print(
                "context_window_tokens="
                f"{merged.get('context_window_tokens')} | max_output_tokens="
                f"{merged.get('max_output_tokens')} | supports_vision="
                f"{merged.get('supports_vision', 'unset')}"
            )
            console.print(f"Config: {saved_path}")
            return "handled"

        if action in {"clear", "rm", "delete"}:
            if len(parts_config) != 2:
                _print_chat_config_usage(console=console)
                return "handled"
            model_name, model_error = _resolve_chat_model_ref(
                session=session,
                raw_model_ref=parts_config[1],
            )
            if model_error is not None or model_name is None:
                console.print(f"[red]{model_error or 'Invalid model reference.'}[/red]")
                return "handled"
            try:
                saved_path, cleared = _clear_chat_model_metadata_override(
                    session=session,
                    model_name=model_name,
                )
            except ConfigError as e:
                console.print(f"[red]Config error:[/red] {e}")
                return "handled"
            if not cleared:
                console.print(f"No stored override found for {model_name}.")
                return "handled"
            _refresh_chat_hud_context_cache(session)
            console.print(f"Cleared model metadata override for {model_name}.")
            console.print(f"Config: {saved_path}")
            return "handled"

        console.print(
            "[yellow]Unknown /config action.[/yellow] Use /config, /config set, /config clear."
        )
        return "handled"
    if cmd in {"/assets"}:
        try:
            run_paths = load_current_run_paths(Path(resolve_session_active_workdir_path(session)))
        except ForgeError:
            console.print(
                "No forge run is active for this workspace. Use /forge plan to start one."
            )
            return "handled"
        _open_assets_modal(session=session, console=console, run_paths=run_paths)
        return "handled"
    if cmd in {"/permissions"}:
        active_mode = str(getattr(session, "mode", "review")).strip().lower()
        pending_mode = _pending_chat_permissions(session)
        selected_mode = pending_mode or active_mode
        next_mode: str | None
        if not arg:
            next_mode, picker_available = _select_chat_mode_interactive(
                current_mode=selected_mode,
                console=console,
            )
            if not picker_available:
                console.print(_chat_mode_panel(current_mode=selected_mode))
                if pending_mode is not None:
                    console.print(
                        "Active Permissions: "
                        f"{_chat_mode_display(active_mode)}; next message: "
                        f"{_chat_mode_display(pending_mode)}"
                    )
                return "handled"
            if next_mode is None:
                return "handled"
        else:
            if persona_modes_enabled(getattr(session, "cfg", None)) and is_persona_name(
                arg.strip().lower(), getattr(session, "persona_registry", None)
            ):
                # Personas have their own command; keep the two vocabularies
                # from blurring back together.
                console.print(f"Personas have their own command: /persona {arg.strip().lower()}")
                return "handled"
            next_mode = _resolve_chat_mode_alias(arg)
        if next_mode is None or next_mode not in _CHAT_MODES:
            console.print(
                "[red]Invalid mode.[/red] Try: /permissions 1, /permissions 2, "
                "/permissions 3, /permissions 4 "
                "(or safe, fast, read, full)."
            )
            return "handled"
        persona_is_narrowing = getattr(session, "persona_restore_mode", None) is not None
        if next_mode == selected_mode and not (pending_mode is None and persona_is_narrowing):
            if pending_mode is not None:
                console.print(
                    "Permissions already selected for the next message: "
                    f"{_chat_mode_display(next_mode)}"
                )
            else:
                console.print(f"Permissions already active: {_chat_mode_display(next_mode)}")
            if next_mode == "fullaccess":
                _print_fullaccess_mode_warning(console=console)
            return "handled"
        try:
            staged = _stage_chat_permissions(
                session=session,
                next_mode=next_mode,
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Failed to select Permissions:[/red] {e}")
            return "handled"
        if staged is None:
            console.print(
                "Pending Permissions cleared; active Permissions remain "
                f"{_chat_mode_display(active_mode)}"
            )
        else:
            console.print(
                f"Permissions for the next message: {_chat_mode_display(next_mode)} "
                f"(currently {_chat_mode_display(active_mode)})"
            )
        if next_mode == "fullaccess":
            _print_fullaccess_mode_warning(console=console)
        return "handled"
    if cmd in {"/persona"}:
        if not persona_modes_enabled(getattr(session, "cfg", None)):
            console.print(
                "Persona modes are disabled (persona_modes_enabled=false or "
                "ALYSIS_PERSONA_MODES=off)."
            )
            return "handled"
        persona_registry = getattr(session, "persona_registry", None)
        current_persona = normalize_persona(getattr(session, "persona", "code"), persona_registry)
        persona_selection: str | None
        if not arg:
            persona_selection, picker_available = _select_chat_persona_interactive(
                current_persona=current_persona,
                console=console,
                custom=persona_registry,
            )
            if not picker_available:
                console.print(
                    _chat_persona_panel(current_persona=current_persona, custom=persona_registry)
                )
                return "handled"
            if persona_selection is None:
                return "handled"
        else:
            persona_selection = arg.strip().lower()
        if not is_persona_name(persona_selection, persona_registry):
            console.print(
                "[red]Invalid persona.[/red] Try: /persona code, architect, ask, debug"
                " — or a custom persona from .alysis_personas."
            )
            return "handled"
        # A persona is a convention layered on the gate: the clamp inside
        # _apply_chat_persona can only keep or lower the execution mode, so
        # this path needs no fullaccess warning and never persists
        # default_mode.
        persona_name = normalize_persona(persona_selection, persona_registry)
        if persona_name == current_persona:
            console.print(f"Persona already set: {persona_name}")
            return "handled"
        try:
            _apply_chat_persona(
                session=session,
                persona=persona_name,
                source="user",
            )
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Failed to change persona:[/red] {e}")
            return "handled"
        console.print(f"Persona set for this session: {persona_name}")
        return "handled"
    if cmd in {"/stream"}:
        stream_arg = arg.strip().lower()
        if not stream_arg or stream_arg == "status":
            enabled = bool(getattr(session, "stream", True))
            console.print(
                f"Streaming is {'on' if enabled else 'off'}. "
                "Use /stream on|off or bare /stream in the TUI."
            )
            return "handled"
        stream_value = _parse_bool_text(stream_arg)
        if stream_value is None:
            console.print(
                "[red]Invalid stream value.[/red] Try: /stream on, /stream off, or /stream status."
            )
            return "handled"
        _set_chat_stream_enabled(session=session, enabled=stream_value)
        console.print(f"Streaming set for this session: {'on' if stream_value else 'off'}")
        return "handled"
    if cmd in {"/trace"}:
        trace_arg = arg.strip()
        if not trace_arg:
            next_level, picker_available = _select_chat_trace_interactive(
                current_level=_chat_trace_level(session),
                console=console,
            )
            if not picker_available:
                level = _chat_trace_level(session)
                console.print(
                    f"Reasoning trace: {level}. Usage: /trace off, /trace compact, or /trace full"
                )
                return "handled"
            if next_level is None:
                return "handled"
        else:
            next_level = _resolve_trace_level(trace_arg)
        if next_level is None:
            console.print(
                "[red]Invalid trace level.[/red] Try: /trace off, /trace compact, or /trace full."
            )
            return "handled"
        applied = _set_chat_trace_level(session=session, level=next_level)
        console.print(f"Reasoning trace set for this session: {applied}")
        return "handled"
    if cmd in {"/clear"}:
        if _is_forge_ui_mode(forge_state.ui_mode):
            console.print("Cannot /clear inside Forge. Use /back or /done first.")
            return "handled"
        try:
            _clear_chat_conversation(session=session, pending_images=pending_images)
        except Exception as e:  # noqa: BLE001
            console.print(f"[red]Failed to clear conversation:[/red] {e}")
            return "handled"
        console.print("Conversation cleared.")
        return "handled"
    if cmd in {"/image"}:
        if not arg:
            try:
                saved = paste_clipboard_image(root=root, output_path=None)
            except ClipboardError as e:
                console.print(f"[red]Clipboard error:[/red] {e}")
                return "handled"
            pending_images.append(os.fspath(saved))
            console.print(f"Pasted clipboard image: {saved}")
            return "handled"
        pending_images.append(arg)
        console.print(f"Queued image: {arg}")
        return "handled"
    if cmd in {"/paste-image"}:
        out_path = arg or None
        try:
            saved = paste_clipboard_image(root=root, output_path=out_path)
        except ClipboardError as e:
            console.print(f"[red]Clipboard error:[/red] {e}")
            return "handled"
        pending_images.append(os.fspath(saved))
        console.print(f"Pasted clipboard image: {saved}")
        return "handled"
    if cmd in {"/images"}:
        if not pending_images:
            console.print("No queued images.")
            return "handled"
        for idx, img in enumerate(pending_images, start=1):
            console.print(f"{idx}. {img}")
        return "handled"
    if cmd in {"/clear-images"}:
        pending_images.clear()
        console.print("Cleared queued images.")
        return "handled"
    if cmd in {"/model"}:
        subscription_profile = False
        try:
            from ...profiles import get_active_profile

            cfg = getattr(session, "cfg", None)
            profile = get_active_profile(cfg) if cfg is not None else None
            subscription_profile = bool(profile is not None and profile.auth_provider)
        except Exception:
            subscription_profile = False
        if not arg:
            model = getattr(getattr(session, "client", None), "model", "")
            console.print(f"Current model: {model}")
            if subscription_profile:
                console.print(
                    "[dim]Open /config → Default Model to choose from your live "
                    "subscription models and supported reasoning efforts.[/dim]"
                )
            else:
                _print_alysis_trial_models(console, session, current=str(model))
            return "handled"
        if subscription_profile:
            console.print(
                "[yellow]Subscription model changes are managed in /config so the model and "
                "reasoning effort stay compatible and persist together.[/yellow]"
            )
            return "handled"
        previous_cfg = getattr(session, "cfg", None)
        copy_cfg = getattr(previous_cfg, "model_copy", None)
        if previous_cfg is None or not callable(copy_cfg):
            console.print(
                "[yellow]Model was not changed because this session cannot safely refresh "
                "its provider route. Restart chat with the desired model.[/yellow]"
            )
            return "handled"
        client = getattr(session, "client", None)
        previous_client_model = getattr(client, "model", None)
        previous_route_identity = getattr(client, "route_identity", None)
        candidate_cfg = copy_cfg(update={"model": arg}, deep=True)
        try:
            from . import loop as _model_loop

            _model_loop._apply_config_menu_changes_to_session(
                session=session,
                cfg=candidate_cfg,
            )
        except Exception as exc:  # noqa: BLE001 - restore the verified route atomically
            session.cfg = previous_cfg
            if client is not None:
                client.model = previous_client_model
                if previous_route_identity is not None or hasattr(client, "route_identity"):
                    client.route_identity = previous_route_identity
            console.print(
                f"[yellow]Model was not changed because provider capabilities could not be "
                f"refreshed: {exc}[/yellow]"
            )
            return "handled"
        _refresh_chat_hud_context_cache(session)
        console.print(f"Model set for this session: {arg}")
        current_client_model = getattr(getattr(session, "client", None), "model", None)
        if bool(getattr(session, "_alysis_applying_deferred_command", False)) and str(
            current_client_model or ""
        ) != str(previous_client_model or ""):
            console.print("provider cache resets - first call re-reads the prefix.")
        return "handled"
    if cmd in {"/login"}:
        from ..commands.auth import login_connection_interactively, login_connection_rows

        selected = str(arg or "").strip()
        if not selected:
            from ..config_menu import _run_config_picker

            rows = login_connection_rows()
            selected = str(
                _run_config_picker(
                    console=console,
                    title="Log in",
                    subtitle="Choose how Alysis Code should connect.",
                    rows=rows,
                    current_value=rows[0][0],
                )
                or ""
            ).strip()
        if not selected:
            return "handled"
        previous_cfg = getattr(session, "cfg", None)
        try:
            login_connection_interactively(selected, console=console)
        except typer.Exit:
            return "handled"
        try:
            from ... import config as _login_config
            from ...profiles import connection_fingerprint
            from . import loop as _login_loop

            reloaded_cfg = _login_config.load_config()
            if previous_cfg is None or connection_fingerprint(
                previous_cfg
            ) != connection_fingerprint(reloaded_cfg):
                console.print(
                    "[yellow]Connection saved. This session is closing so the provider "
                    "transport, authentication, and trace capability can be rebuilt safely. "
                    "Start `alysis chat` again.[/yellow]"
                )
                return "exit"
            _login_loop._apply_config_menu_changes_to_session(
                session=session,
                cfg=reloaded_cfg,
            )
            console.print("[green]The selected connection is active for this session.[/green]")
        except Exception as exc:  # noqa: BLE001 - never keep a possibly stale transport alive
            console.print(
                f"[yellow]Connection saved, but live reload failed: {exc}. This session is "
                "closing; start `alysis chat` again.[/yellow]"
            )
            return "exit"
        return "handled"
    if cmd in {"/logout"}:
        from ...account_login import logout as _alysis_logout

        cfg = getattr(session, "cfg", None)
        if cfg is not None and _alysis_logout(cfg):
            console.print(
                "[green]Logged out.[/green] Your Alysis Code key was revoked; "
                "hosted models will refuse further requests until you /login again."
            )
        else:
            console.print("You're not logged in to an Alysis Code account.")
        return "handled"

    if cmd[:1] in "/:":
        return _handle_unknown_chat_command(
            command=parts[0],
            console=console,
            ui_mode=forge_state.ui_mode,
        )

    if not _is_forge_ui_mode(forge_state.ui_mode):
        navigation_request = _parse_chat_workdir_navigation_request(
            input_text=input_text,
            session=session,
        )
        if navigation_request is not None:
            if len(navigation_request) == 3:
                _requested_path, _trailing_instruction, error_message = navigation_request
                console.print(f"[red]Failed to change active workdir:[/red] {error_message}")
                return "handled"

            requested_path, trailing_instruction = navigation_request
            try:
                result = set_session_active_workdir(
                    session,
                    requested_path,
                    source="natural_language_chat",
                )
            except Exception as e:  # noqa: BLE001
                console.print(f"[red]Failed to change active workdir:[/red] {e}")
                return "handled"
            console.print(
                f"Active workdir: {result['active_workdir']} ({result['active_workdir_relpath']})"
            )
            if not trailing_instruction:
                return "handled"
            return _ChatExecutionRequest(instruction=trailing_instruction)

    return "send"


def _handle_terminals_command(*, arg: str, session: Any, console: Console) -> None:
    parts = arg.split()
    subcommand = parts[0].lower() if parts else "list"

    if subcommand == "help":
        _print_terminals_usage(console=console)
        return

    terminal_manager = getattr(session, "terminal_manager", None)
    if terminal_manager is None:
        console.print("Background terminals are unavailable in this session.")
        return

    try:
        if subcommand == "list":
            if len(parts) > 1:
                console.print("Usage: /terminals list")
                return
            _print_terminals_list(console=console, terminal_manager=terminal_manager)
            return

        if subcommand == "show":
            if len(parts) != 2:
                console.print("Usage: /terminals show <process_id>")
                return
            _print_terminals_show(
                console=console,
                terminal_manager=terminal_manager,
                process_id=parts[1],
            )
            return

        if subcommand == "kill":
            if len(parts) != 2:
                console.print("Usage: /terminals kill <process_id>")
                return
            mode = str(getattr(session, "mode", "") or "").strip().lower()
            if mode == "readonly":
                console.print("Cannot kill processes in readonly mode.")
                return
            _print_terminals_kill(
                console=console,
                terminal_manager=terminal_manager,
                process_id=parts[1],
            )
            return

        _print_terminals_usage(console=console)
    except Exception as exc:  # noqa: BLE001
        # Slash commands should report failures without unwinding into the chat loop.
        _LOGGER.exception("Terminal slash command failed")
        console.print(f"[red]Background terminal command failed:[/red] {exc}")


def _print_terminals_usage(*, console: Console) -> None:
    for line in _TERMINALS_USAGE_LINES:
        console.print(line)


def _print_terminals_list(*, console: Console, terminal_manager: Any) -> None:
    summaries = terminal_manager.list()
    if not summaries:
        console.print("No background processes.")
        return

    table = _Table(title="Background Terminals")
    table.add_column("process_id", no_wrap=True)
    table.add_column("cmd", overflow="fold")
    table.add_column("status", no_wrap=True)
    table.add_column("exit_code", no_wrap=True)
    table.add_column("runtime", no_wrap=True)
    for summary in summaries:
        table.add_row(
            Text(str(summary.process_id)),
            Text(str(summary.cmd)),
            Text(str(summary.status)),
            Text("-" if summary.exit_code is None else str(summary.exit_code)),
            Text(_format_terminal_runtime_s(float(summary.runtime_s))),
        )
    console.print(table)


def _print_terminals_show(
    *,
    console: Console,
    terminal_manager: Any,
    process_id: str,
) -> None:
    try:
        snapshot = terminal_manager.read(process_id, since=0)
    except KeyError:
        console.print(_terminal_text("No such background process: ", process_id))
        return

    console.print(_terminal_text("process_id: ", snapshot.process_id))
    console.print(_terminal_text("status: ", snapshot.status))
    console.print(
        _terminal_text("exit_code: ", "-" if snapshot.exit_code is None else snapshot.exit_code)
    )
    if snapshot.failure_reason is not None:
        console.print(_terminal_text("failure_reason: ", snapshot.failure_reason))
    console.print(
        _terminal_text("runtime: ", _format_terminal_runtime_s(float(snapshot.runtime_s)))
    )
    console.print(_terminal_text("dropped_lines: ", snapshot.dropped_lines))

    lines = list(snapshot.lines)
    if len(lines) > _TERMINALS_DISPLAY_LINE_LIMIT:
        console.print("… (older lines truncated)")
        lines = lines[-_TERMINALS_DISPLAY_LINE_LIMIT:]
    if not lines:
        console.print("No output.")
        return
    for line in lines:
        text = str(line.text).rstrip("\r\n")
        console.print(_terminal_text(f"{line.seq} {line.stream}: ", text))


def _print_terminals_kill(
    *,
    console: Console,
    terminal_manager: Any,
    process_id: str,
) -> None:
    try:
        snapshot = terminal_manager.kill(process_id)
    except KeyError:
        console.print(_terminal_text("No such background process: ", process_id))
        return
    code = "-" if snapshot.exit_code is None else str(snapshot.exit_code)
    console.print(
        Text.assemble(
            ("Killed ",),
            (process_id,),
            (" (status=",),
            (snapshot.status,),
            (", exit_code=",),
            (code,),
            (")",),
        )
    )


def _terminal_text(prefix: str, value: object) -> Text:
    return Text.assemble((prefix,), (str(value),))


def _format_terminal_runtime_s(runtime_s: float) -> str:
    if runtime_s < 0:
        runtime_s = 0.0
    if runtime_s < 60:
        return f"{runtime_s:.1f}s"
    minutes = int(runtime_s // 60)
    seconds = int(runtime_s % 60)
    return f"{minutes}m{seconds:02d}s"


def _handle_forge_chat_command(
    *,
    input_text: str,
    forge_state: _ForgeChatState,
    session: Any,
    console: Console,
    forge_planner_reply_sink: Any | None = None,
    forge_execution_report_sink: Any | None = None,
) -> str:
    paths = forge_state.paths
    plan = forge_state.plan
    if paths is None or plan is None:
        _print_forge_warning_messages(
            console=console,
            label="Forge",
            warnings=["Forge state missing; returning to chat mode."],
        )
        forge_state.ui_mode = "chat"
        return "handled"

    trimmed = input_text.strip()
    if not trimmed:
        return "handled"

    parts = trimmed.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd in {"/back"}:
        forge_state.ui_mode = "chat"
        append_transcript_note(paths, role="system", message="Exited Forge with /back.")
        _print_forge_meta(
            console=console,
            message="Left Forge without the final save/validate step. Back to chat.",
        )
        return "handled"

    if cmd in {"/done", "done"}:
        append_transcript_note(paths, role="user", message=trimmed)
        append_transcript_note(paths, role="system", message="Forge finalized with /done.")
        _finalize_forge_plan(
            console=console,
            paths=paths,
            plan=plan,
            transcript_tail=forge_state.planner_transcript,
        )
        forge_state.ui_mode = "chat"
        _print_forge_meta(
            console=console,
            message="Saved and validated the plan. Back to chat.",
        )
        return "handled"

    if cmd in {"/help"}:
        console.print(_chat_help_panel(ui_mode="forge"))
        return "handled"

    if cmd in {"/show"}:
        _show_forge_plan_summary(console=console, paths=paths, plan=plan)
        return "handled"

    if cmd in {"/assets"}:
        _open_assets_modal(session=session, console=console, run_paths=paths)
        return "handled"

    if cmd in {"/plan"}:
        append_transcript_note(paths, role="user", message=trimmed)
        plan_cmd = arg.strip().lower()
        if not plan_cmd:
            plan_cmd, picker_available = _select_forge_plan_command_interactive(console=console)
            if not picker_available:
                _print_forge_warning_messages(
                    console=console,
                    label="Usage",
                    warnings=["/plan tasks|table|markdown|md|edit"],
                )
                return "handled"
            if plan_cmd is None:
                return "handled"

        if plan_cmd in {"view", "tasks", "table"}:
            _show_forge_plan_summary(console=console, paths=paths, plan=plan)
            return "handled"
        if plan_cmd in {"markdown", "md"}:
            _show_forge_plan_markdown(console=console, paths=paths, plan=plan)
            return "handled"
        if plan_cmd in {"edit", "edit-json"}:
            _edit_forge_plan_json(console=console, paths=paths, forge_state=forge_state)
            updated_plan = forge_state.plan
            if isinstance(updated_plan, dict):
                plan = updated_plan
            return "handled"
        append_transcript_note(
            paths,
            role="system",
            message="Rejected invalid Forge /plan usage.",
        )
        _print_forge_plan_command_guidance(console=console)
        return "handled"

    if cmd in {"/execute"}:
        forge_state.swarm_run_attempted = False
        if arg.strip().lower() != "plan":
            append_transcript_note(paths, role="user", message=trimmed)
            append_transcript_note(
                paths,
                role="system",
                message="Rejected invalid /execute usage.",
            )
            _print_forge_warning_messages(
                console=console,
                label="Usage",
                warnings=["/execute plan"],
            )
            return "handled"

        if not _forge_has_usable_plan_input(plan):
            _print_forge_warning_messages(
                console=console,
                label="Forge",
                warnings=[
                    "Plan is empty. Add requirements or tasks first (paste text, /task, or /plan edit)."
                ],
            )
            append_transcript_note(paths, role="user", message=trimmed)
            append_transcript_note(
                paths,
                role="system",
                message="Rejected /execute plan because plan had no requirements/tasks.",
            )
            return "handled"

        try:
            run_mutation_guard = acquire_swarm_mutation_guard(
                paths,
                mode="forge_swarm:chat",
                on_wait=lambda info: _print_forge_lock_wait_notice(console, info),
            )
        except ForgeError as e:
            _print_forge_error(
                console=console,
                message=f"Forge execution failed: {e}",
            )
            return "handled"

        try:
            append_transcript_note(paths, role="user", message=trimmed)

            if bool(getattr(run_mutation_guard, "acquired_after_wait", False)):
                plan.clear()
                plan.update(load_plan(paths))
            workspace_context = _workspace_context_payload_for_paths(
                paths=paths,
                refresh_if_stale=True,
            )
            finalize_plan(plan)
            reconciliation_result, workspace_context = _reconcile_plan_for_paths(
                paths=paths,
                plan=plan,
                refresh_if_stale=False,
                transcript_tail=forge_state.planner_transcript,
            )
            save_plan(paths, plan)
            validation_warnings = _validate_forge_plan_for_paths(paths, plan)
            if reconciliation_result.warnings:
                _print_forge_warning_messages(
                    console=console,
                    label="Plan reconciliation",
                    warnings=list(reconciliation_result.warnings),
                )
                for warning in reconciliation_result.warnings:
                    append_transcript_note(
                        paths,
                        role="system",
                        message=f"Plan reconciliation warning: {warning}",
                    )
            _write_plan_validation_artifact(
                paths=paths,
                reconciliation_result=reconciliation_result,
                validation_warnings=validation_warnings,
            )
            no_execution_ready_tasks_message = _forge_no_execution_ready_tasks_message(plan)
            if no_execution_ready_tasks_message is not None:
                _print_forge_error(
                    console=console,
                    message=no_execution_ready_tasks_message,
                )
                append_transcript_note(
                    paths,
                    role="system",
                    message=f"Rejected /execute plan: {no_execution_ready_tasks_message}",
                )
                return "handled"

            enrich_attempted = False
            enrich_applied = False
            enrich_summary = "enrichment not needed"
            show_enrichment_details = _chat_trace_level(session) == "full"
            if _forge_should_try_enrichment(validation_warnings):
                if not _forge_enrich_plan_enabled():
                    enrich_summary = "enrichment skipped: disabled"
                else:
                    planner_api_key = str(
                        getattr(getattr(session, "client", None), "api_key", "") or ""
                    ).strip()
                    session_cfg = getattr(session, "cfg", None)
                    if planner_api_key and isinstance(session_cfg, AppConfig):
                        enrich_attempted = True
                        enrichment_user_text = (
                            "Enrich the existing plan for execution: do not ask questions; only fill "
                            "missing acceptance_criteria and estimated_files via tasks_update; do not "
                            "change project_goal, summary, requirements, tasks_add, or tasks_remove."
                        )
                        planner_knowledge = prepare_planner_knowledge(
                            paths=paths,
                            plan=plan,
                            user_text=enrichment_user_text,
                            selection_label="planner_enrichment",
                        )
                        planner_workspace_root = resolve_knowledge_workspace_root(paths)
                        planner_result = run_planner_turn(
                            cfg=clone_cfg(session_cfg),
                            api_key_override=planner_api_key,
                            plan=plan,
                            transcript_tail=forge_state.planner_transcript,
                            workspace_context=workspace_context,
                            user_text=enrichment_user_text,
                            relevant_knowledge_section=planner_knowledge.render_prompt_section(
                                workspace_root=planner_workspace_root
                            ),
                            run_paths=paths,
                        )
                        _merge_usage_payloads_into_session(
                            session=session,
                            payloads=list(getattr(planner_result, "usage_events", []) or []),
                        )
                        request_retry_count = int(
                            getattr(planner_result, "request_retry_count", 0) or 0
                        )
                        if request_retry_count > 0 and not planner_result.error:
                            retry_word = "retry" if request_retry_count == 1 else "retries"
                            retry_notice = (
                                f"Planner request recovered after {request_retry_count} transient "
                                f"{retry_word}."
                            )
                            if show_enrichment_details:
                                _print_forge_warning_messages(
                                    console=console,
                                    label="Plan enrichment",
                                    warnings=[retry_notice],
                                )
                            if show_enrichment_details:
                                append_transcript_note(
                                    paths,
                                    role="system",
                                    message=f"Plan enrichment warning: {retry_notice}",
                                )
                        if planner_result.error:
                            retry_word = "retry" if request_retry_count == 1 else "retries"
                            enrichment_error_summary = (
                                f"enrichment skipped after {request_retry_count} transient "
                                f"{retry_word}: {planner_result.error}"
                                if request_retry_count > 0
                                else f"enrichment skipped: {planner_result.error}"
                            )
                            enrichment_error_warnings = (
                                [
                                    f"Planner request failed after {request_retry_count} transient "
                                    f"{retry_word}."
                                ]
                                if request_retry_count > 0
                                else []
                            )
                            enrichment_error_warnings.append(
                                f"Final planner error: {planner_result.error}"
                            )
                            _print_forge_warning_messages(
                                console=console,
                                label="Plan enrichment",
                                warnings=enrichment_error_warnings,
                            )
                            append_transcript_note(
                                paths,
                                role="system",
                                message=(
                                    f"Plan enrichment error after {request_retry_count} transient "
                                    f"{retry_word}: {planner_result.error}"
                                    if request_retry_count > 0
                                    else f"Plan enrichment error: {planner_result.error}"
                                ),
                            )
                            enrich_summary = enrichment_error_summary
                        elif planner_result.plan_update:
                            sanitized_enrichment = _sanitize_forge_enrichment_plan_update(
                                plan=plan,
                                plan_update=planner_result.plan_update,
                            )
                            if sanitized_enrichment.warnings:
                                if show_enrichment_details:
                                    _print_forge_warning_messages(
                                        console=console,
                                        label="Plan enrichment",
                                        warnings=list(sanitized_enrichment.warnings),
                                    )
                                if show_enrichment_details:
                                    for warning in sanitized_enrichment.warnings:
                                        append_transcript_note(
                                            paths,
                                            role="system",
                                            message=f"Plan enrichment warning: {warning}",
                                        )
                            if sanitized_enrichment.plan_update is None:
                                enrich_summary = "enrichment produced no applicable changes"
                            else:
                                apply_result = apply_guarded_planner_plan_update(
                                    plan,
                                    sanitized_enrichment.plan_update,
                                    latest_user_text=enrichment_user_text,
                                    workspace_context=workspace_context,
                                )
                                reconciliation_target_ids = list(
                                    dict.fromkeys(
                                        [
                                            *apply_result.added_task_ids,
                                            *apply_result.updated_task_ids,
                                        ]
                                    )
                                )
                                if apply_result.warnings:
                                    if show_enrichment_details:
                                        _print_forge_warning_messages(
                                            console=console,
                                            label="Plan enrichment",
                                            warnings=list(apply_result.warnings),
                                        )
                                    if show_enrichment_details:
                                        for warning in apply_result.warnings:
                                            append_transcript_note(
                                                paths,
                                                role="system",
                                                message=f"Plan enrichment warning: {warning}",
                                            )
                                if apply_result.changed:
                                    enrich_applied = True
                                    reconciliation_result, workspace_context = (
                                        _reconcile_plan_for_paths(
                                            paths=paths,
                                            plan=plan,
                                            refresh_if_stale=False,
                                            transcript_tail=forge_state.planner_transcript,
                                            target_task_ids=reconciliation_target_ids,
                                        )
                                    )
                                    save_plan(paths, plan)
                                    validation_warnings = _validate_forge_plan_for_paths(
                                        paths, plan
                                    )
                                    _write_plan_validation_artifact(
                                        paths=paths,
                                        reconciliation_result=reconciliation_result,
                                        validation_warnings=validation_warnings,
                                    )
                                    enrich_summary = (
                                        f"enrichment applied: {summarize_plan_update(apply_result)}"
                                    )
                                else:
                                    enrich_summary = "enrichment produced no applicable changes"
                        else:
                            enrich_summary = "enrichment produced no plan_update"
                    else:
                        enrich_summary = "enrichment skipped: missing planner api key/config"

            append_planner_summary(paths, enrich_summary)
            if enrich_applied:
                _print_forge_meta(console=console, message="Refined plan.")

            if validation_warnings:
                _print_forge_warning_messages(
                    console=console,
                    label="Plan validation",
                    warnings=validation_warnings,
                )
                for warning in validation_warnings:
                    append_transcript_note(
                        paths,
                        role="system",
                        message=f"Plan validation warning: {warning}",
                    )
            elif (
                enrich_attempted
                and enrich_applied
                and not validation_warnings
                and show_enrichment_details
            ):
                append_transcript_note(
                    paths,
                    role="system",
                    message="Plan enrichment cleared the remaining validation warnings.",
                )

            # Stamp draft/execution_ready before the gate runs, so the plan on disk
            # already says what the gate is about to decide.
            status_assessment = apply_plan_status(plan, validation_warnings=validation_warnings)
            save_plan(paths, plan)

            try:
                raise_for_execution_ready_plan(plan)
            except PlannerFailedError as e:
                block_message = str(e)
                _print_forge_error(
                    console=console,
                    message=block_message,
                )
                _print_forge_meta(
                    console=console,
                    message=(
                        "This plan is saved as a draft, not an execution-ready plan. "
                        "Refine it with the planner or edit it, then run /execute plan again."
                    ),
                )
                append_transcript_note(
                    paths,
                    role="system",
                    message=(
                        f"Rejected /execute plan (plan_status={status_assessment.status}): "
                        f"{block_message}"
                    ),
                )
                return "handled"

            session_cfg = getattr(session, "cfg", None)
            if not isinstance(session_cfg, AppConfig):
                _print_forge_error(
                    console=console,
                    message="Forge execution failed: missing session config.",
                )
                return "handled"
            swarm_cfg = clone_cfg(session_cfg)
            client_obj = getattr(session, "client", None)
            api_key_override = str(getattr(client_obj, "api_key", "") or "").strip() or None
            store_obj = getattr(session, "store", None)
            no_log = not bool(getattr(store_obj, "enabled", True))
            yes_value = bool(getattr(session, "yes", False))
            swarm_trace_level = _chat_trace_level(session)
            swarm_trace_sink = _make_forge_swarm_trace_sink(
                session=session,
                paths=paths,
                console=console,
            )

            # Swarm knobs: the TUI launch gate may have chosen non-default values
            # (stored on ``forge_state.swarm_knobs``); the classic/non-TUI path
            # leaves it empty so the historical defaults (parallel 2 · scope strict
            # · verify warn · no review) apply unchanged. Every value is validated
            # and clamped here so a malformed entry can never reach ``run_swarm``.
            _knobs = getattr(forge_state, "swarm_knobs", None) or {}
            try:
                _parallel = int(_knobs.get("parallel", 2))
            except (TypeError, ValueError):
                _parallel = 2
            _parallel = max(1, min(_parallel, 8))
            _scope_mode = str(_knobs.get("scope_mode", "strict")).strip().lower()
            if _scope_mode not in {"strict", "warn"}:
                _scope_mode = "strict"
            _verify_mode = str(_knobs.get("verify_mode", "warn")).strip().lower()
            if _verify_mode not in {"warn", "strict", "off"}:
                _verify_mode = "warn"
            _review = bool(_knobs.get("review", False))

            forge_state.swarm_run_attempted = True
            try:
                code = run_swarm(
                    paths=paths,
                    plan=plan,
                    cfg=swarm_cfg,
                    mode="auto",
                    yes=yes_value,
                    max_steps=None,
                    api_key_override=api_key_override,
                    no_log=no_log,
                    parallel=_parallel,
                    base_branch=None,
                    max_tasks=None,
                    max_attempts=None,
                    dry_run=False,
                    keep_worktrees=False,
                    retry_failed=False,
                    retry_changes_requested=False,
                    only=None,
                    retry_merge_conflicts=False,
                    scope_mode=_scope_mode,
                    verify_mode=_verify_mode,
                    integration_mode=None,
                    verify_cmd=None,
                    review=_review,
                    console=console,
                    trace_level=swarm_trace_level,
                    trace_sink=swarm_trace_sink,
                    run_mutation_guard=run_mutation_guard,
                )
            except (ConfigError, ForgeError, GitOpsError) as e:
                _print_forge_error(
                    console=console,
                    message=f"Forge execution failed: {e}",
                )
                append_transcript_note(
                    paths,
                    role="system",
                    message=f"Forge /execute plan failed: {e}",
                )
                return "handled"
            except Exception as e:  # noqa: BLE001
                _print_forge_error(
                    console=console,
                    message=f"Forge execution failed: {e}",
                )
                append_transcript_note(
                    paths,
                    role="system",
                    message=f"Forge /execute plan failed: {e}",
                )
                return "handled"

            _merge_forge_execution_usage_into_session(session=session, paths=paths)
            summary_path = paths.execution_dir / "swarm_summary.md"
            summary_json_path = paths.execution_dir / "swarm_summary.json"
            run_status = ""
            run_clean: bool | None = None
            if summary_json_path.exists():
                try:
                    summary_payload = json.loads(summary_json_path.read_text(encoding="utf-8"))
                    run_status = str(summary_payload.get("status") or "").strip()
                    raw_clean = summary_payload.get("clean")
                    if isinstance(raw_clean, bool):
                        run_clean = raw_clean
                except Exception:  # noqa: BLE001
                    run_status = ""
            try:
                plan_after_execution = load_plan(paths)
            except Exception:  # noqa: BLE001
                plan_after_execution = plan
            total_tasks = len(plan_after_execution.get("tasks") or [])
            done_tasks, failed_tasks, remaining_tasks = _forge_task_status_counts(
                plan_after_execution
            )
            if total_tasks <= 0:
                headline = "Execution finished."
            elif run_clean is False and run_status:
                headline = (
                    f"Execution finished with verification status `{run_status}` · "
                    f"{done_tasks} done · {failed_tasks} failed · {remaining_tasks} remaining."
                )
            elif failed_tasks == 0 and remaining_tasks == 0 and code == 0:
                headline = f"Execution complete · {total_tasks} tasks finished."
            elif failed_tasks == 0:
                headline = (
                    f"Execution finished · {done_tasks} done · {remaining_tasks} still in progress."
                )
            elif done_tasks > 0:
                headline = (
                    f"Execution finished with issues · {done_tasks} done · "
                    f"{failed_tasks} failed · {remaining_tasks} remaining."
                )
            else:
                headline = (
                    f"Execution finished with issues · {failed_tasks} failed · "
                    f"{remaining_tasks} remaining."
                )
            done_ok = total_tasks > 0 and failed_tasks == 0 and remaining_tasks == 0 and code == 0
            console.print(
                _forge_phase_rule(console=console, label="DONE"),
                highlight=False,
            )
            dot = "●" if _forge_supports_unicode_glyphs(console) else "*"
            console.print(
                Text.assemble(
                    ("│ ", STYLE_CHROME),
                    (f"{dot} ", STYLE_SUCCESS if done_ok else STYLE_WARN),
                    (headline, STYLE_EMPHASIS),
                ),
                highlight=False,
            )
            _print_forge_meta(
                console=console,
                message=(
                    f"Tasks · {total_tasks} total · {done_tasks} done · "
                    f"{failed_tasks} failed · {remaining_tasks} remaining"
                ),
            )
            _print_forge_meta(console=console, message=f"Summary · {summary_path}")

            # The run's actual answer: what each task did, where the files
            # landed, whether the output is verified, and how to try it. Built
            # deterministically from run artifacts (no model call). The TUI
            # passes a sink so this renders as a real assistant block instead
            # of joining the dim captured console text; classic chat prints it
            # inline as markdown.
            completion_report = ""
            try:
                completion_report = build_forge_completion_report(
                    paths=paths,
                    plan=plan_after_execution,
                    run_status=run_status,
                    run_clean=run_clean,
                    exit_code=code,
                )
            except Exception:  # noqa: BLE001
                completion_report = ""
            if completion_report:
                try:
                    (paths.execution_dir / "completion_report.md").write_text(
                        completion_report + "\n", encoding="utf-8"
                    )
                except Exception:  # noqa: BLE001
                    pass
                if forge_execution_report_sink is not None:
                    try:
                        forge_execution_report_sink(completion_report)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    from rich.markdown import Markdown

                    console.print()
                    console.print(Markdown(completion_report), highlight=False)

            # The run just rewrote the workspace; drop the cached pre-execution
            # scans so follow-up planner questions ("how do I run it?") are
            # answered against what now actually exists on disk.
            forge_state.workspace_context = None
            if getattr(session, "planner_workspace_context", None) is not None:
                try:
                    session.planner_workspace_context = None
                except Exception:  # noqa: BLE001
                    pass

            append_transcript_note(
                paths,
                role="system",
                message=(
                    "Forge /execute plan completed. "
                    f"Tasks: total={total_tasks}, done={done_tasks}, "
                    f"failed={failed_tasks}, remaining={remaining_tasks}."
                ),
            )
            return "handled"
        finally:
            run_mutation_guard.release()

    if cmd in {"/assistant"}:
        assistant_cmd = arg.lower()
        if not assistant_cmd:
            assistant_cmd, picker_available = _select_forge_assistant_interactive(
                enabled=forge_state.assistant_enabled,
                console=console,
            )
            if not picker_available:
                append_transcript_note(paths, role="user", message=trimmed)
                append_transcript_note(
                    paths,
                    role="system",
                    message="Rejected invalid /assistant usage.",
                )
                _print_forge_warning_messages(
                    console=console,
                    label="Usage",
                    warnings=["/assistant on|off|status"],
                )
                return "handled"
            if assistant_cmd is None:
                return "handled"
        if assistant_cmd == "on":
            forge_state.assistant_enabled = True
            append_transcript_note(paths, role="user", message=trimmed)
            append_transcript_note(paths, role="system", message="Planner assistant enabled.")
            _print_forge_meta(console=console, message="Planner assistant: ON")
            return "handled"
        if assistant_cmd == "off":
            forge_state.assistant_enabled = False
            forge_state.planner_awaiting_clarification = False
            forge_state.planner_pending_questions = []
            append_transcript_note(paths, role="user", message=trimmed)
            append_transcript_note(paths, role="system", message="Planner assistant disabled.")
            _print_forge_meta(console=console, message="Planner assistant: OFF")
            return "handled"
        if assistant_cmd == "status":
            state = "ON" if forge_state.assistant_enabled else "OFF"
            append_transcript_note(paths, role="user", message=trimmed)
            append_transcript_note(
                paths,
                role="system",
                message=f"Planner assistant status requested ({state}).",
            )
            _print_forge_meta(console=console, message=f"Planner assistant: {state}")
            return "handled"
        append_transcript_note(paths, role="user", message=trimmed)
        append_transcript_note(paths, role="system", message="Rejected invalid /assistant usage.")
        _print_forge_warning_messages(
            console=console,
            label="Usage",
            warnings=["/assistant on|off|status"],
        )
        return "handled"

    if cmd == "/goal":
        append_transcript_note(paths, role="user", message=trimmed)
        if not arg:
            append_transcript_note(paths, role="system", message="Rejected empty /goal.")
            _print_forge_warning_messages(
                console=console,
                label="Usage",
                warnings=["/goal <text>"],
            )
            return "handled"
        goal = arg
        plan["project_goal"] = goal
        if not str(plan.get("summary") or "").strip():
            plan["summary"] = goal
        save_plan(paths, plan)
        append_transcript_note(paths, role="system", message="Updated project goal.")
        _print_forge_meta(console=console, message="Project goal updated.")
        return "handled"

    if cmd == "/task":
        append_transcript_note(paths, role="user", message=trimmed)
        if not arg:
            append_transcript_note(paths, role="system", message="Rejected empty /task.")
            _print_forge_warning_messages(
                console=console,
                label="Usage",
                warnings=["/task <title>"],
            )
            return "handled"
        title = arg
        try:
            task = add_task(
                plan,
                title=title,
                description=f"Manual planning chat task: {title}",
            )
        except ForgeError as e:
            append_transcript_note(
                paths,
                role="system",
                message=f"Rejected /task because it lacked runnable file scope: {e}",
            )
            _print_forge_warning_messages(
                console=console,
                label="Task rejected",
                warnings=[str(e)],
            )
            return "handled"
        save_plan(paths, plan)
        append_transcript_note(paths, role="system", message=f"Added task {task['id']}.")
        _print_forge_meta(
            console=console,
            message=f"Added task {task['id']} · {task['title']}",
        )
        return "handled"

    if cmd[:1] in "/:":
        return "unhandled"

    # In the TUI the user line is already echoed (and the reply is routed to the
    # assistant-markdown renderer via the sink), so skip the captured "You:" label
    # that would otherwise double the echo as flat text.
    if forge_planner_reply_sink is None:
        _render_labeled_chat_message(console=console, label="You", message=trimmed)

    append_transcript_note(paths, role="user", message=trimmed)

    if forge_state.assistant_enabled:
        planning_relevant = True
        session_cfg = getattr(session, "cfg", None)
        planner_api_key = str(
            getattr(getattr(session, "client", None), "api_key", "") or ""
        ).strip()
        api_key_override = planner_api_key or None
        session_stream = bool(getattr(session, "stream", False))
        # With a reply sink (TUI) the reply is rendered as a single assistant block
        # on completion, so suppress the token-delta trace that would otherwise
        # spill the streaming text as separate trace lines.
        on_text_delta = (
            _make_forge_planner_delta_trace_callback(session=session)
            if (session_stream and forge_planner_reply_sink is None)
            else None
        )
        stream_planner = session_stream and on_text_delta is not None
        _emit_forge_planner_trace(
            session=session,
            message="Planner assistant is analyzing your request.",
        )
        if stream_planner:
            _emit_forge_planner_trace(
                session=session,
                message="Streaming planner response...",
                full_only=True,
            )
        _run_forge_planner_turn_controller(
            console=console,
            paths=paths,
            plan=plan,
            planner_state=forge_state.planner_session,
            user_text=trimmed,
            cfg_loader=(
                lambda: (
                    clone_cfg(session_cfg)
                    if isinstance(session_cfg, AppConfig)
                    else (_ for _ in ()).throw(ConfigError("session config missing"))
                )
            ),
            unavailable_message_builder=lambda _error: (
                "Planner assistant is unavailable because session config is missing."
            ),
            emit_meta=lambda message: _print_forge_meta(console=console, message=message),
            emit_warning_group=lambda label, warnings: _print_forge_warning_messages(
                console=console,
                label=label,
                warnings=warnings,
            ),
            api_key_override=api_key_override,
            render_reply=(
                forge_planner_reply_sink
                if forge_planner_reply_sink is not None
                else (
                    lambda message, questions: _render_planner_reply(
                        console=console,
                        message=message,
                        questions=questions,
                    )
                )
            ),
            selection_label="planner",
            planning_relevant=planning_relevant,
            refresh_workspace_context=(
                forge_state.workspace_context is None
                and hasattr(paths, "root")
                and hasattr(paths, "workspace_context_json_path")
            ),
            trace_callback=lambda message, full_only=False: _emit_forge_planner_trace(
                session=session,
                message=message,
                full_only=full_only,
            ),
            stream=stream_planner,
            on_text_delta=on_text_delta,
            on_usage_events=lambda payloads: _merge_usage_payloads_into_session(
                session=session,
                payloads=payloads,
            ),
            error_fallback=lambda: _capture_forge_requirement_from_planner_fallback(
                plan=plan,
                paths=paths,
                console=console,
                user_text=trimmed,
                planning_relevant=planning_relevant,
            ),
        )
        return "handled"

    goal_was_set = _set_forge_project_goal_if_empty(
        plan=plan,
        paths=paths,
        console=console,
        goal=trimmed,
    )
    if goal_was_set:
        add_requirement(plan, trimmed)
        save_plan(paths, plan)
        append_transcript_note(
            paths,
            role="system",
            message="Captured project goal as planning input.",
        )
    else:
        add_requirement(plan, trimmed)
        save_plan(paths, plan)
        append_transcript_note(paths, role="system", message="Captured requirement note.")
        _print_forge_meta(console=console, message="Captured requirement note.")
    return "handled"
