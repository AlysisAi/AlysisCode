# ruff: noqa: F821
# Dependencies are injected at runtime from alysis_code.cli to preserve monkeypatch surfaces.
from __future__ import annotations

from typing import Any

from ..runtime_kind import RuntimeKind
from ..step_budget import normalize_step_budget_policy, step_budget_is_autonomous
from ..surface.styles import STYLE_CONTENT, STYLE_EMPHASIS
from .commands import _materialize_cli_globals

_PROTECTED_GLOBAL_NAMES: set[str] = set()


def _sync_cli_globals(cli_mod: Any) -> None:
    _materialize_cli_globals(cli_mod)
    module_globals = globals()
    if not _PROTECTED_GLOBAL_NAMES:
        for local_name, local_value in module_globals.items():
            if callable(local_value):
                _PROTECTED_GLOBAL_NAMES.add(local_name)
    for name, value in cli_mod.__dict__.items():
        if name.startswith("__") or name in _PROTECTED_GLOBAL_NAMES:
            continue
        module_globals[name] = value


def _resume_positive_int(raw: object, *, default: int) -> int:
    try:
        value = int(raw) if raw is not None else default
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _resume_optional_positive_int(raw: object) -> int | None:
    try:
        value = int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
    if value is None or value <= 0:
        return None
    return value


def _select_chat_resume_interactive(
    *,
    current_session_id: str,
    sessions: list[SessionInfo],
    console: Console,
) -> tuple[str | None, bool]:
    if _is_non_interactive_terminal():
        return None, False
    if not sessions:
        return None, True
    try:
        from prompt_toolkit.input import create_input
        from prompt_toolkit.keys import Keys
    except Exception:
        return None, False

    selected_index = 0
    scroll_offset = 0
    rename_mode = False
    rename_buffer = ""
    status_message = ""
    display_rows = _chat_resume_display_rows(sessions=sessions)

    def _selected_session_id() -> str | None:
        if not sessions:
            return None
        if selected_index < 0 or selected_index >= len(sessions):
            return None
        return sessions[selected_index].session_id

    def _page_size() -> int:
        return max(1, _resume_visible_session_rows(has_status_message=bool(status_message)))

    def _sync_viewport() -> None:
        nonlocal scroll_offset
        if not sessions:
            scroll_offset = 0
            return
        if selected_index < 0:
            selected_index_local = 0
        elif selected_index >= len(sessions):
            selected_index_local = len(sessions) - 1
        else:
            selected_index_local = selected_index
        scroll_offset = _clamp_resume_scroll_offset(
            total_rows=len(sessions),
            selected_index=selected_index_local,
            scroll_offset=scroll_offset,
            visible_session_rows=_page_size(),
        )

    def _render_panel() -> Panel:
        _sync_viewport()
        selected_id = _selected_session_id()
        return _chat_resume_panel(
            current_session_id=current_session_id,
            sessions=sessions,
            selected_session_id=selected_id,
            interactive=True,
            display_rows=display_rows,
            rename_session_id=selected_id if rename_mode else None,
            rename_buffer=rename_buffer,
            status_message=status_message or None,
            scroll_offset=scroll_offset,
            visible_session_rows=_page_size(),
        )

    input_reader = create_input()
    try:
        with _watch_terminal_resize() as consume_resize:
            with _Live(
                _render_panel(),
                console=console,
                auto_refresh=False,
                transient=False,
                screen=True,
            ) as live:
                with input_reader.raw_mode():
                    while True:
                        resized = consume_resize()
                        if resized:
                            _sync_viewport()
                            _clear_terminal_screen(console=console)
                            live.update(_render_panel(), refresh=True)

                        key_presses = _read_input_keys_with_timeout(
                            input_reader=input_reader,
                            timeout_s=0.12,
                        )
                        if not key_presses:
                            continue
                        panel_updated = False
                        for key_press in key_presses:
                            key = key_press.key
                            key_char = _resume_picker_key_char(key_press)

                            if _terminal_too_small():
                                if (
                                    key == Keys.Escape
                                    or key == Keys.ControlC
                                    or key
                                    in {
                                        "escape",
                                        "c-c",
                                    }
                                ):
                                    return None, True
                                continue

                            if rename_mode:
                                if key == Keys.Enter or key in {"enter", "\r", "\n"}:
                                    if sessions and 0 <= selected_index < len(sessions):
                                        renamed, status_message = (
                                            _rename_resume_session_custom_title(
                                                info=sessions[selected_index],
                                                new_title=rename_buffer,
                                            )
                                        )
                                        if renamed:
                                            display_rows = _chat_resume_display_rows(
                                                sessions=sessions
                                            )
                                    else:
                                        status_message = "Rename canceled."
                                    rename_mode = False
                                    _sync_viewport()
                                    panel_updated = True
                                    continue
                                if (
                                    key == Keys.Escape
                                    or key == Keys.ControlC
                                    or key
                                    in {
                                        "escape",
                                        "c-c",
                                    }
                                ):
                                    rename_mode = False
                                    status_message = "Rename canceled."
                                    _sync_viewport()
                                    panel_updated = True
                                    continue
                                if key == Keys.Backspace or key in {"backspace", "c-h"}:
                                    if rename_buffer:
                                        rename_buffer = rename_buffer[:-1]
                                        panel_updated = True
                                    continue
                                if key_char and key_char in {"\x7f", "\b"}:
                                    if rename_buffer:
                                        rename_buffer = rename_buffer[:-1]
                                        panel_updated = True
                                    continue
                                if key_char and key_char >= " ":
                                    rename_buffer += key_char
                                    panel_updated = True
                                continue

                            if key == Keys.Up or key == "up":
                                if selected_index > 0:
                                    selected_index -= 1
                                status_message = ""
                                _sync_viewport()
                                panel_updated = True
                                continue
                            if key == Keys.Down or key == "down":
                                if selected_index < len(sessions) - 1:
                                    selected_index += 1
                                status_message = ""
                                _sync_viewport()
                                panel_updated = True
                                continue
                            if key == Keys.PageUp or key in {"pageup", "s-pageup"}:
                                selected_index = max(0, selected_index - _page_size())
                                status_message = ""
                                _sync_viewport()
                                panel_updated = True
                                continue
                            if key == Keys.PageDown or key in {"pagedown", "s-pagedown"}:
                                selected_index = min(
                                    len(sessions) - 1, selected_index + _page_size()
                                )
                                status_message = ""
                                _sync_viewport()
                                panel_updated = True
                                continue
                            if key == Keys.Home or key == "home":
                                selected_index = 0
                                status_message = ""
                                _sync_viewport()
                                panel_updated = True
                                continue
                            if key == Keys.End or key == "end":
                                selected_index = len(sessions) - 1
                                status_message = ""
                                _sync_viewport()
                                panel_updated = True
                                continue
                            if isinstance(key, str) and key.isdigit():
                                idx = int(key) - 1
                                if 0 <= idx < len(sessions):
                                    selected_index = idx
                                    status_message = ""
                                    _sync_viewport()
                                    panel_updated = True
                                    continue
                            if key == Keys.Enter or key in {"enter", "\r", "\n"}:
                                if not sessions:
                                    return None, True
                                return sessions[selected_index].session_id, True
                            if (
                                key == Keys.Escape
                                or key == Keys.ControlC
                                or key
                                in {
                                    "escape",
                                    "c-c",
                                }
                            ):
                                return None, True
                            if key_char and key_char.lower() == "r":
                                selected_id = _selected_session_id()
                                if selected_id is None:
                                    continue
                                current_row = next(
                                    (
                                        item
                                        for item in display_rows
                                        if item.session_id == selected_id
                                    ),
                                    None,
                                )
                                rename_buffer = (
                                    current_row.preview if current_row is not None else ""
                                )
                                rename_mode = True
                                status_message = ""
                                _sync_viewport()
                                panel_updated = True
                                continue
                            if key_char and key_char.lower() == "d":
                                if not sessions:
                                    continue
                                deleted, status_message = _delete_resume_session(
                                    info=sessions[selected_index]
                                )
                                if deleted:
                                    sessions.pop(selected_index)
                                    if not sessions:
                                        return None, True
                                    selected_index = min(selected_index, len(sessions) - 1)
                                    display_rows = _chat_resume_display_rows(sessions=sessions)
                                _sync_viewport()
                                panel_updated = True
                                continue
                        if panel_updated:
                            live.update(_render_panel(), refresh=True)
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]Resume picker unavailable:[/yellow] {e}")
        return None, False
    finally:
        close = getattr(input_reader, "close", None)
        if callable(close):
            close()
    return None, True


def _chat_resume_panel(
    *,
    current_session_id: str,
    sessions: list[SessionInfo],
    selected_session_id: str | None = None,
    interactive: bool = False,
    display_rows: list[_ResumeSessionRow] | None = None,
    rename_session_id: str | None = None,
    rename_buffer: str = "",
    status_message: str | None = None,
    scroll_offset: int = 0,
    visible_session_rows: int | None = None,
) -> Panel:
    from rich.align import Align
    from rich.text import Text

    if _terminal_too_small():
        return _terminal_too_small_panel()

    _ = interactive
    current = current_session_id.strip() or "-"
    selected = (selected_session_id or "").strip().casefold()
    rename_target = (rename_session_id or "").strip().casefold()
    rows = (
        display_rows if display_rows is not None else _chat_resume_display_rows(sessions=sessions)
    )
    selected_index = _resolve_resume_selected_index(
        rows=rows, selected_session_id=selected_session_id
    )
    visible_count = (
        int(visible_session_rows)
        if visible_session_rows is not None
        else _resume_visible_session_rows(has_status_message=bool(status_message))
    )
    normalized_offset = _clamp_resume_scroll_offset(
        total_rows=len(rows),
        selected_index=selected_index,
        scroll_offset=scroll_offset,
        visible_session_rows=visible_count,
    )
    start = normalized_offset
    end = min(len(rows), start + max(1, visible_count))
    window_rows = rows[start:end]
    hidden_above = start
    hidden_below = max(0, len(rows) - end)
    columns, _ = _terminal_dimensions()

    header = _Panel(
        Align.center(Text("Resume Session", style="bold cyan")),
        border_style="cyan",
        padding=(0, 1),
    )

    current_info = Text()
    current_info.append("Current Session: ", style=STYLE_EMPHASIS)
    current_info.append("* ", style="bold yellow")
    current_info.append(current, style=STYLE_EMPHASIS)
    current_panel = _Panel(current_info, border_style="bright_blue", padding=(0, 1))

    idx_width = 3 if columns < 72 else 4
    when_width = 15
    if columns >= 90:
        when_width = 20
    if columns >= 130:
        when_width = 24

    table = _Table(show_header=False, box=None, expand=True, padding=(0, 1), collapse_padding=True)
    table.add_column("idx", justify="right", no_wrap=True, width=idx_width)
    table.add_column("session", style=STYLE_CONTENT, no_wrap=False, ratio=6, overflow="ellipsis")
    table.add_column("when", justify="right", no_wrap=True, width=when_width)

    if hidden_above > 0:
        suffix = "" if hidden_above == 1 else "s"
        table.add_row("", Text(f"... {hidden_above} more session{suffix} above", style="dim"), "")

    last_group = rows[start - 1].group_label if start > 0 else ""
    if window_rows:
        first_group = window_rows[0].group_label
        if start > 0 and rows[start - 1].group_label == first_group:
            table.add_row("", Text(first_group, style="bold magenta"), "")
            last_group = first_group

    for item in window_rows:
        if item.group_label != last_group:
            table.add_row("", Text(item.group_label, style="bold magenta"), "")
            last_group = item.group_label

        is_selected = bool(selected) and item.session_id.casefold() == selected
        is_renaming = bool(rename_target) and item.session_id.casefold() == rename_target
        if is_renaming:
            row_style = "bold black on bright_yellow"
        elif is_selected:
            row_style = "bold black on bright_cyan"
        else:
            row_style = item.recency_style
        marker = ">" if is_selected else " "
        idx_text = Text(f"{item.index:>2}", style=row_style)
        if is_renaming:
            live_name = _truncate_preview(rename_buffer, max_chars=72)
            label_text = Text(f"{marker} Rename: {live_name}", style=row_style)
        else:
            label_text = Text(f"{marker} {item.preview}", style=row_style)
        when_style = (
            row_style if is_selected else ("dim" if item.recency_style == "dim" else STYLE_CONTENT)
        )
        when_text = Text(item.when_label, style=when_style)
        table.add_row(idx_text, label_text, when_text)

    if hidden_below > 0:
        suffix = "" if hidden_below == 1 else "s"
        table.add_row("", Text(f"... {hidden_below} more session{suffix} below", style="dim"), "")

    footer_text = (
        "/ Navigate   PgUp/PgDn Page   Home/End Jump   "
        "Enter Select   r Rename   d Delete   Esc Cancel"
    )
    if rename_target:
        footer_text = "Type title   Enter Save   Esc Cancel"

    footer = _Panel(
        Align.center(
            Text(
                footer_text,
                style="dim",
            )
        ),
        border_style="bright_black",
        padding=(0, 1),
    )

    content = _table_grid(expand=True)
    content.add_row(header)
    content.add_row("")
    content.add_row(current_panel)
    content.add_row("")
    content.add_row(table)
    if status_message:
        content.add_row("")
        content.add_row(Text(status_message, style="dim"))
    content.add_row("")
    content.add_row(footer)

    return _Panel(content, border_style="cyan")


def _resume_chat_session(
    *,
    session: Any,
    target_session_id: str,
) -> tuple[bool, str, list[dict[str, Any]]]:
    raw_requested_id = str(target_session_id or "").strip()
    if not raw_requested_id:
        return False, "[red]Usage:[/red] /resume <index|session_id>", []
    requested_id = _normalize_chat_resume_session_id(raw_requested_id)
    if requested_id is None:
        return False, f"[red]Invalid session id:[/red] {raw_requested_id}", []

    cfg = getattr(session, "cfg", None)
    if not isinstance(cfg, AppConfig):
        return False, "[red]Resume unavailable:[/red] missing session config.", []

    store = getattr(session, "store", None)
    sessions_dir_raw = getattr(store, "sessions_dir", None)
    sessions_dir = (
        Path(sessions_dir_raw) if sessions_dir_raw is not None else resolve_sessions_dir(cfg)
    )

    target_path = _resolve_chat_resume_session_path(
        sessions_dir=sessions_dir,
        session_id=requested_id,
    )
    if target_path is None:
        return False, f"[red]Session not found:[/red] {requested_id}", []

    current_session_id = str(getattr(store, "session_id", "") or "")
    if current_session_id == requested_id:
        return True, f"Session already active: {requested_id}", []

    synthesized_tool_results: list[str] = []
    history_messages = _load_chat_resume_messages(
        target_path,
        synthesized_tool_results=synthesized_tool_results,
    )
    resume_context_message = _build_chat_resume_context_message(target_path)
    start_payload = _load_chat_resume_session_start(target_path)
    runtime_settings = _load_chat_resume_runtime_settings(target_path)
    active_workdir_relpath = _load_chat_resume_active_workdir_relpath(target_path)

    current_mode = str(getattr(session, "mode", "review") or "review").strip().lower()
    start_mode = str(start_payload.get("mode") or "").strip().lower()
    mode = start_mode if start_mode in _CHAT_MODES else current_mode
    if mode not in _CHAT_MODES:
        mode = "review"

    raw_yes = start_payload.get("yes")
    if isinstance(raw_yes, bool):
        yes = raw_yes
    else:
        yes = bool(getattr(session, "yes", False))

    resume_cfg = clone_cfg(cfg)

    start_step_budget_policy = str(start_payload.get("step_budget_policy") or "").strip().lower()
    if start_step_budget_policy in {"autonomous", "limited", "adaptive", "fixed"}:
        resume_cfg.step_budget_policy = normalize_step_budget_policy(start_step_budget_policy)
    else:
        resume_cfg.step_budget_policy = normalize_step_budget_policy(
            getattr(resume_cfg, "step_budget_policy", "autonomous")
        )
    stored_task_max_steps = _resume_optional_positive_int(start_payload.get("task_max_steps"))
    if stored_task_max_steps is not None:
        resume_cfg.task_max_steps = stored_task_max_steps
    stored_subagent_max_steps = _resume_optional_positive_int(
        start_payload.get("subagent_max_steps")
    )
    if stored_subagent_max_steps is not None:
        resume_cfg.subagent_max_steps = stored_subagent_max_steps

    default_max_steps = _resume_positive_int(
        getattr(resume_cfg, "max_steps", DEFAULT_CHAT_MAX_STEPS),
        default=DEFAULT_CHAT_MAX_STEPS,
    )
    max_steps = _resume_positive_int(start_payload.get("max_steps"), default=default_max_steps)
    resume_cfg.max_steps = max_steps

    raw_enable_chat_turn_step_budget = start_payload.get("enable_chat_turn_step_budget")
    enable_chat_turn_step_budget = (
        True
        if step_budget_is_autonomous(resume_cfg.step_budget_policy)
        else (
            raw_enable_chat_turn_step_budget
            if isinstance(raw_enable_chat_turn_step_budget, bool)
            else True
        )
    )
    chat_turn_fixed_override = _resume_optional_positive_int(
        start_payload.get("chat_turn_fixed_override")
    )

    api_key_override = str(getattr(getattr(session, "client", None), "api_key", "") or "").strip()
    if not api_key_override:
        api_key_override = None

    root = Path(getattr(session, "root", Path(".")))
    if active_workdir_relpath:
        try:
            _resolve_chat_resume_active_workdir_path(
                workspace_root=root,
                active_workdir_relpath=active_workdir_relpath,
            )
        except Exception as e:  # noqa: BLE001
            return (
                False,
                "[red]Failed to resume session:[/red] "
                f"could not restore active workdir {active_workdir_relpath!r}: {e}",
                [],
            )
    no_log = not bool(getattr(store, "enabled", False))
    usage_role = str(getattr(session, "usage_role", "main") or "main")
    usage_hud_enabled = _chat_usage_hud_enabled(session)
    # These bindings belong to the live UI, not to the persisted chat being
    # resumed.  The session object is mutated in place below so callers keep a
    # stable reference; preserve the same runtime hooks across that state swap.
    runtime_bindings = {
        name: getattr(session, name)
        for name in (
            "_alysis_tui_interactive",
            "apply_staged_operation",
            "pending_operation_labels",
            "on_child_scheduler_replaced",
        )
        if hasattr(session, name)
    }
    previous_scheduler = getattr(session, "child_scheduler", None)
    lifecycle_listener = getattr(previous_scheduler, "lifecycle_listener", None)

    start_model = str(start_payload.get("model") or "").strip()
    model_restore_reason = "no historical model recorded"
    historical_model_restored = False
    try:
        from ..llm.metadata import endpoint_descriptor_matches
        from ..profiles import get_active_profile, resolve_effective_base_url

        current_profile = get_active_profile(resume_cfg)
        current_provider_base_url = resolve_effective_base_url(
            cfg=resume_cfg,
            profile=current_profile,
        ).rstrip("/")
        connection_identity_keys = (
            "profile_name",
            "protocol",
            "auth_provider",
            "reasoning_trace_adapter",
        )
        has_connection_identity = all(key in start_payload for key in connection_identity_keys)
        if "provider_base_url_descriptor" in start_payload:
            endpoint_matches = endpoint_descriptor_matches(
                current_provider_base_url,
                start_payload.get("provider_base_url_descriptor"),
            )
            has_connection_identity = has_connection_identity and isinstance(
                start_payload.get("provider_base_url_descriptor"), dict
            )
        else:
            # Legacy sessions only have a raw URL. It may contain credentials
            # and is not a trustworthy resume identity, so model restoration
            # and provider continuation both fail closed.
            has_connection_identity = False
            endpoint_matches = False
        connection_matches = has_connection_identity and (
            str(start_payload.get("profile_name") or "") == current_profile.name
            and str(start_payload.get("protocol") or "") == current_profile.protocol
            and endpoint_matches
            and str(start_payload.get("auth_provider") or "")
            == str(current_profile.auth_provider or "")
            and str(start_payload.get("reasoning_trace_adapter") or "auto")
            == current_profile.reasoning_trace_adapter
        )
    except Exception:  # noqa: BLE001 - legacy/corrupt identity must fail closed
        has_connection_identity = False
        connection_matches = False

    if start_model and connection_matches:
        resume_cfg.model = start_model
        historical_model_restored = True
        model_restore_reason = "stored provider connection matches the active connection"
    elif start_model and has_connection_identity:
        model_restore_reason = (
            "stored provider connection differs from the active connection; using the current model"
        )
    elif start_model:
        model_restore_reason = (
            "legacy session has no verified provider identity; using the current model"
        )

    raw_temp = start_payload.get("temperature")
    if raw_temp is not None:
        try:
            temp_value = float(raw_temp)
        except (TypeError, ValueError):
            temp_value = None
        if temp_value is not None and temp_value >= 0:
            _apply_temperature_override(resume_cfg, temp_value)

    for key in (
        "coding_temperature",
        "review_temperature",
        "planner_temperature",
        "conflict_review_temperature",
        "compactor_temperature",
        "chat_temperature",
    ):
        raw_value = start_payload.get(key)
        if raw_value is None:
            continue
        try:
            parsed_value = float(raw_value)
        except (TypeError, ValueError):
            continue
        if parsed_value < 0:
            continue
        setattr(resume_cfg, key, parsed_value)

    raw_stream = runtime_settings.get("stream")
    if isinstance(raw_stream, bool):
        resume_cfg.stream = raw_stream

    start_routing_mode = str(start_payload.get("routing_mode") or "").strip().lower()
    if start_routing_mode in {"auto", "code_only"}:
        resume_cfg.routing_mode = start_routing_mode

    start_compaction_enabled = start_payload.get("compaction_enabled")
    resume_enable_compaction = (
        bool(start_compaction_enabled)
        if isinstance(start_compaction_enabled, bool)
        else (
            getattr(session, "conversation_compactor", None) is not None
            or getattr(session, "tool_output_offloader", None) is not None
        )
    )
    start_tool_output_offload = start_payload.get("tool_output_offload_enabled")
    resume_enable_tool_output_offload = (
        bool(start_tool_output_offload) if isinstance(start_tool_output_offload, bool) else None
    )
    start_conversation_summarization = start_payload.get("conversation_summarization_enabled")
    resume_enable_conversation_summarization = (
        bool(start_conversation_summarization)
        if isinstance(start_conversation_summarization, bool)
        else None
    )

    try:
        new_session = create_session(
            cfg=resume_cfg,
            root=root,
            mode=mode,
            runtime_kind=RuntimeKind.INTERACTIVE_CHAT,
            yes=yes,
            max_steps=max_steps,
            no_log=no_log,
            api_key_override=api_key_override,
            console=getattr(session, "console", None),
            surface=getattr(session, "surface", None),
            non_interactive=_is_non_interactive_terminal(),
            enable_chat_turn_step_budget=enable_chat_turn_step_budget,
            chat_turn_fixed_override=chat_turn_fixed_override,
            session_log_dir_override=sessions_dir,
            session_id_override=requested_id,
            usage_role=usage_role,
            enable_compaction=resume_enable_compaction,
            enable_tool_output_offload=resume_enable_tool_output_offload,
            enable_conversation_summarization=resume_enable_conversation_summarization,
            active_workdir_relpath_override=active_workdir_relpath,
            session_source="resume",
            session_source_metadata={
                "from_session_id": current_session_id,
                "resumed_session_id": requested_id,
                "loaded_message_count": len(history_messages),
                "active_workdir_relpath": active_workdir_relpath,
            },
        )
    except Exception as e:  # noqa: BLE001
        return False, f"[red]Failed to resume session:[/red] {e}", []

    # Provider-owned continuation state is only valid on the exact route that
    # produced it.  Require both the session-level connection/model identity
    # above and the per-message route stamp to match the newly created client.
    # Legacy sessions, clients without a route identity, and any mismatch fail
    # closed while retaining public message content and tool calls for ordinary
    # full-history replay.
    from ..llm.metadata import (
        ProviderRouteIdentity,
        gate_messages_for_provider_route,
        strip_provider_metadata_from_messages,
    )

    active_route_identity = getattr(getattr(new_session, "client", None), "route_identity", None)
    if historical_model_restored and isinstance(active_route_identity, ProviderRouteIdentity):
        history_messages = gate_messages_for_provider_route(
            history_messages,
            active_route_identity,
            preserve_internal_artifacts=True,
        )
    else:
        history_messages = strip_provider_metadata_from_messages(
            history_messages,
            preserve_internal_artifacts=True,
        )

    restored_trace_level = str(runtime_settings.get("trace_level") or "compact")
    trace_setter = getattr(getattr(new_session, "surface", None), "set_trace_level", None)
    if callable(trace_setter):
        try:
            trace_setter(restored_trace_level)
        except Exception:
            pass

    combined_resume_context = resume_context_message or ""
    resume_context_loaded = _insert_chat_resume_context_message(
        new_session,
        combined_resume_context,
    )

    if history_messages:
        new_session.messages.extend(history_messages)

    from ..provider_usage import restore_provider_usage

    restore_provider_usage(new_session, target_path)

    # Restore workspace verification provenance without pinning an objective.
    _restore_chat_resume_evidence(
        new_session,
        path=target_path,
        source_session_id=requested_id,
    )

    # Restored compaction state (summary/pins) only becomes model-visible via
    # _upsert_context_messages, which otherwise runs on the next compaction.
    # Re-inject here, after the pinned resume context bumped pinned_prefix_len,
    # so the memory message lands after the pinned prefix.
    compaction_memory_reinjected = False
    resume_compactor = getattr(new_session, "conversation_compactor", None)
    if resume_compactor is not None:
        new_session.messages[:] = resume_compactor.reinject_context_messages(new_session.messages)
        compaction_memory_reinjected = (
            getattr(getattr(resume_compactor, "state", None), "memory_message_index", None)
            is not None
        )

    new_session.store.append(
        "system_note",
        {
            "message": "chat_resume",
            "from_session_id": current_session_id,
            "resumed_session_id": requested_id,
            "loaded_messages": len(history_messages),
            "synthesized_tool_results": len(synthesized_tool_results),
            "synthesized_tool_result_ids": list(synthesized_tool_results),
            "resume_context_loaded": resume_context_loaded,
            "resume_context_chars": len(combined_resume_context),
            "compaction_memory_reinjected": compaction_memory_reinjected,
            "historical_model_restored": historical_model_restored,
            "model_restore_reason": model_restore_reason,
        },
    )

    close_fn = getattr(session, "close", None)
    close_warning = ""
    if callable(close_fn):
        try:
            close_fn()
        except Exception as e:  # noqa: BLE001
            close_warning = f"previous session close failed during resume: {e}"
            new_session.store.append(
                "system_note",
                {
                    "message": "chat_resume_warning",
                    "warning": close_warning,
                },
            )

    session.__dict__.clear()
    session.__dict__.update(new_session.__dict__)
    for name, value in runtime_bindings.items():
        setattr(session, name, value)
    replacement_scheduler = getattr(session, "child_scheduler", None)
    set_lifecycle_listener = getattr(replacement_scheduler, "set_lifecycle_listener", None)
    if callable(set_lifecycle_listener) and callable(lifecycle_listener):
        set_lifecycle_listener(lifecycle_listener)
    _set_chat_usage_hud_enabled(session, usage_hud_enabled)
    _refresh_chat_hud_context_cache(session)
    _ensure_session_summary_metadata(session=session, allow_model_summary=False)

    turn_count = sum(1 for msg in history_messages if msg.get("role") == "user")
    suffix = "" if turn_count == 1 else "s"
    message = f"Resumed session: {requested_id} ({turn_count} turn{suffix} loaded)."
    if start_model and not historical_model_restored:
        message += " The current provider and model were kept for safety."
    if close_warning:
        message += f" [yellow]Warning:[/yellow] {close_warning}."
    return (True, message, history_messages)


def _select_chat_resume_interactive_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return _select_chat_resume_interactive(*args, **kwargs)


def _chat_resume_panel_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return _chat_resume_panel(*args, **kwargs)


def _resume_chat_session_impl(cli_mod: Any, *args: Any, **kwargs: Any) -> Any:
    _sync_cli_globals(cli_mod)
    return _resume_chat_session(*args, **kwargs)
