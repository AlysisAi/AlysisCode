# ruff: noqa: F401,F403,F405,I001
# Legacy split module: dependencies are synced by cli_surface.py.
from __future__ import annotations

from contextvars import ContextVar

from .cli_common import *

_PICKER_NARROW_OVERRIDE = ContextVar("_PICKER_NARROW_OVERRIDE", default=None)


def _picker_row_styles(*, selected: bool) -> tuple[str, str]:
    if selected:
        return STYLE_SELECTED_LABEL, STYLE_SELECTED_DESC
    return STYLE_DESELECTED_LABEL, STYLE_DESELECTED_DESC


def _picker_hint_text() -> str:
    return "Up/Down navigate / Enter confirm / Esc cancel"


def _picker_label_text(label: str, *, selected: bool) -> str:
    text = str(label or "")
    if not selected:
        return text
    match = re.match(r"^(\s*\d+\))\s*(.+)$", text)
    if match:
        return f"{match.group(1)} ({match.group(2)})"
    return f"({text})"


def _fallback_picker_is_narrow_terminal() -> bool:
    return _terminal_width() < 88


def _picker_is_narrow_terminal() -> bool:
    default = globals().get("_is_narrow_terminal") or _fallback_picker_is_narrow_terminal
    return bool(_patchable("_is_narrow_terminal", default)())


def _forge_picker_row_renderables(*, label: str, desc: str, selected: bool) -> list[Any]:
    from rich.text import Text

    label_style, desc_style = _picker_row_styles(selected=selected)
    renderables: list[Any] = [
        Text.assemble(
            ("│ ", "bright_black"),
            (_picker_label_text(label, selected=selected), label_style),
        )
    ]
    if str(desc or "").strip():
        renderables.append(
            Text.assemble(
                ("│   ", "bright_black"),
                (str(desc), desc_style),
            )
        )
    return renderables


def _selectable_options_panel(
    *,
    title: str,
    rows: list[tuple[str, str, str]],
    selected_value: str | None = None,
    interactive: bool = False,
) -> Any:
    from rich.console import Group
    from rich.text import Text

    selected = (selected_value or "").strip().casefold()
    renderables: list[Any] = (
        [Text(str(title or ""), style=STYLE_EMPHASIS)] if str(title or "").strip() else []
    )
    narrow_override = _PICKER_NARROW_OVERRIDE.get()
    is_narrow = (
        bool(narrow_override) if narrow_override is not None else _picker_is_narrow_terminal()
    )
    if is_narrow:
        for value, label, desc in rows:
            row_selected = str(value).strip().casefold() == selected
            label_style, desc_style = _picker_row_styles(selected=row_selected)
            renderables.append(
                Text.assemble(
                    (_picker_label_text(label, selected=row_selected), label_style),
                )
            )
            if str(desc or "").strip():
                renderables.append(
                    Text.assemble(
                        ("   ", "bright_black"),
                        (str(desc), desc_style),
                    )
                )
        if interactive:
            renderables.append(Text(_picker_hint_text(), style="dim"))
        return Group(*renderables)

    table = _Table(show_header=False, box=None, expand=True, padding=(0, 1), collapse_padding=True)
    table.add_column("option", no_wrap=True, ratio=2)
    table.add_column("description", no_wrap=False, ratio=5, overflow="fold")
    for value, label, desc in rows:
        row_selected = str(value).strip().casefold() == selected
        label_style, desc_style = _picker_row_styles(selected=row_selected)
        table.add_row(
            Text(_picker_label_text(str(label), selected=row_selected), style=label_style),
            Text(str(desc), style=desc_style),
        )

    renderables.append(table)
    if interactive:
        renderables.append(Text(_picker_hint_text(), style="dim"))
    return Group(*renderables)


def _terminal_dimensions() -> tuple[int, int]:
    size = shutil.get_terminal_size(fallback=(80, 24))
    columns = int(getattr(size, "columns", 80) or 80)
    rows = int(getattr(size, "lines", 24) or 24)
    if columns < 1:
        columns = 1
    if rows < 1:
        rows = 1
    return columns, rows


def _terminal_too_small() -> bool:
    columns, rows = _patchable("_terminal_dimensions", _terminal_dimensions)()
    return columns < _MIN_TERMINAL_COLUMNS or rows < _MIN_TERMINAL_ROWS


def _terminal_too_small_panel() -> Panel:
    from rich.align import Align
    from rich.text import Text

    columns, rows = _patchable("_terminal_dimensions", _terminal_dimensions)()
    message = Text("Terminal too small - please resize", style="bold yellow")
    details = Text(
        f"Current: {columns}x{rows}  Minimum: {_MIN_TERMINAL_COLUMNS}x{_MIN_TERMINAL_ROWS}",
        style="dim",
    )
    content = _table_grid(expand=True)
    content.add_row("")
    content.add_row(Align.center(message))
    content.add_row("")
    content.add_row(Align.center(details))
    content.add_row("")
    return _Panel(content, title="Resize Required", border_style="yellow")


def _clear_terminal_screen(*, console: Console) -> None:
    stream = console.file if getattr(console, "file", None) is not None else sys.stdout
    try:
        stream.write("\x1b[2J\x1b[H")
        stream.flush()
    except Exception:  # noqa: BLE001
        pass


@contextmanager
def _watch_terminal_resize() -> Any:
    if not hasattr(signal, "SIGWINCH"):
        yield lambda: False
        return

    resized = threading.Event()
    try:
        previous_handler = signal.getsignal(signal.SIGWINCH)
    except Exception:  # noqa: BLE001
        yield lambda: False
        return

    def _on_resize(_signum: int, _frame: Any) -> None:
        resized.set()

    installed = False
    try:
        signal.signal(signal.SIGWINCH, _on_resize)
        installed = True
    except Exception:  # noqa: BLE001
        installed = False

    def _consume_resize() -> bool:
        if resized.is_set():
            resized.clear()
            return True
        return False

    try:
        yield _consume_resize
    finally:
        if installed:
            try:
                signal.signal(signal.SIGWINCH, previous_handler)
            except Exception:  # noqa: BLE001
                pass


def _read_input_keys_with_timeout(*, input_reader: Any, timeout_s: float) -> list[Any]:
    fileno_fn = getattr(input_reader, "fileno", None)
    if callable(fileno_fn):
        try:
            fd = int(fileno_fn())
        except Exception:  # noqa: BLE001
            fd = -1
        if fd >= 0:
            try:
                ready, _w, _x = select.select([fd], [], [], timeout_s)
            except Exception:  # noqa: BLE001
                ready = [fd]
            if not ready:
                return []
    try:
        keys = input_reader.read_keys()
    except Exception:  # noqa: BLE001
        return []
    if isinstance(keys, list):
        return keys
    return []


def _run_inline_option_selector(
    *,
    console: Console,
    rows: list[tuple[str, str, str]],
    current_value: str,
    panel_builder: Callable[[str | None, bool], Any],
    unavailable_label: str,
    use_alt_screen: bool = True,
    confirm_on_digit: bool = False,
) -> tuple[str | None, bool]:
    if _patchable("_is_non_interactive_terminal", _is_non_interactive_terminal)():
        return None, False
    try:
        from prompt_toolkit.input import create_input
        from prompt_toolkit.keys import Keys
    except Exception:
        return None, False

    if not rows:
        return None, True

    selected_index = next(
        (idx for idx, (value, _label, _desc) in enumerate(rows) if value == current_value),
        0,
    )
    if selected_index < 0 or selected_index >= len(rows):
        selected_index = 0

    def _render_panel() -> Any:
        if _terminal_too_small():
            return _terminal_too_small_panel()
        return panel_builder(rows[selected_index][0], True)

    narrow_token = _PICKER_NARROW_OVERRIDE.set(_picker_is_narrow_terminal())
    input_reader = None
    try:
        input_reader = create_input()
        with _watch_terminal_resize() as consume_resize:
            with _Live(
                _render_panel(),
                console=console,
                auto_refresh=False,
                transient=not use_alt_screen,
                screen=use_alt_screen,
            ) as live:
                with input_reader.raw_mode():
                    while True:
                        resized = consume_resize()
                        if resized:
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
                            raw_data = key_press.data
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

                            if key == Keys.Up or key == "up":
                                selected_index = (selected_index - 1) % len(rows)
                                panel_updated = True
                                continue
                            if key == Keys.Down or key == "down":
                                selected_index = (selected_index + 1) % len(rows)
                                panel_updated = True
                                continue
                            digit_key: str | None = None
                            if isinstance(key, str) and key.isdigit():
                                digit_key = key
                            elif isinstance(raw_data, str) and raw_data.isdigit():
                                digit_key = raw_data
                            if digit_key is not None:
                                idx = int(digit_key) - 1
                                if 0 <= idx < len(rows):
                                    selected_index = idx
                                    if confirm_on_digit:
                                        return rows[selected_index][0], True
                                    panel_updated = True
                                    continue
                            if (
                                key == Keys.Enter
                                or key == Keys.ControlM
                                or raw_data in {chr(13), chr(10)}
                            ):
                                return rows[selected_index][0], True
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
                        if panel_updated:
                            live.update(_render_panel(), refresh=True)
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]{unavailable_label} unavailable:[/yellow] {e}")
        return None, False
    finally:
        _PICKER_NARROW_OVERRIDE.reset(narrow_token)
        close = getattr(input_reader, "close", None)
        if callable(close):
            close()
    return None, True


def _guarded_workspace_panel(
    *,
    binding: WorkspaceBinding,
    candidates: tuple[WorkspaceCandidate, ...],
    allow_use_current_action: bool = True,
    selected_action: str | None = None,
    interactive: bool = False,
) -> Panel:
    path_label = os.fspath(binding.requested_path)
    title = f"Guarded Workspace: {path_label}"
    return _selectable_options_panel(
        title=title,
        rows=guarded_workspace_action_rows(
            binding=binding,
            candidates=candidates,
            allow_use_current_action=allow_use_current_action,
        ),
        selected_value=selected_action,
        interactive=interactive,
    )


def _select_guarded_workspace_action_interactive(
    *,
    binding: WorkspaceBinding,
    candidates: tuple[WorkspaceCandidate, ...],
    allow_use_current_action: bool = True,
    console: Console,
) -> tuple[str | None, bool]:
    rows = guarded_workspace_action_rows(
        binding=binding,
        candidates=candidates,
        allow_use_current_action=allow_use_current_action,
    )
    default_value = rows[1][0] if allow_use_current_action and len(rows) > 1 else rows[0][0]
    return _patchable("_run_inline_option_selector", _run_inline_option_selector)(
        console=console,
        rows=rows,
        current_value=default_value,
        panel_builder=lambda selected, interactive: _guarded_workspace_panel(
            binding=binding,
            candidates=candidates,
            allow_use_current_action=allow_use_current_action,
            selected_action=selected,
            interactive=interactive,
        ),
        unavailable_label="Workspace chooser",
        use_alt_screen=False,
        confirm_on_digit=True,
    )


def _workspace_candidate_panel(
    *,
    base_path: Path,
    candidates: tuple[WorkspaceCandidate, ...],
    selected_path: Path | None = None,
    interactive: bool = False,
) -> Panel:
    title = f"Project Folders Under {os.fspath(base_path)}"
    selected_value = os.fspath(selected_path) if selected_path is not None else None
    return _selectable_options_panel(
        title=title,
        rows=workspace_candidate_rows(base_path=base_path, candidates=candidates),
        selected_value=selected_value,
        interactive=interactive,
    )


def _select_workspace_candidate_interactive(
    *,
    base_path: Path,
    candidates: tuple[WorkspaceCandidate, ...],
    console: Console,
) -> tuple[Path | None, bool]:
    rows = workspace_candidate_rows(base_path=base_path, candidates=candidates)
    if not rows:
        return None, True
    selected_value, interactive_available = _patchable(
        "_run_inline_option_selector", _run_inline_option_selector
    )(
        console=console,
        rows=rows,
        current_value=rows[0][0],
        panel_builder=lambda selected, interactive: _workspace_candidate_panel(
            base_path=base_path,
            candidates=candidates,
            selected_path=(Path(selected) if selected is not None else None),
            interactive=interactive,
        ),
        unavailable_label="Workspace candidate picker",
        use_alt_screen=False,
        confirm_on_digit=True,
    )
    if selected_value is None:
        return None, interactive_available
    return Path(selected_value), interactive_available


def _guarded_workspace_prompt_text(text: str, *, default: str | None = None) -> str:
    return _patchable("_prompt_text_with_escape", _prompt_text_with_escape)(
        text,
        default=default,
        escape_hint="Esc to go back",
    )


def _prompt_text_with_escape(
    text: str,
    *,
    default: str | None = None,
    escape_hint: str = "Esc to go back",
) -> str:
    if _patchable("_is_non_interactive_terminal", _is_non_interactive_terminal)():
        return typer.prompt(text, default=default)
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.key_binding import KeyBindings
    except Exception:
        return typer.prompt(text, default=default)

    kb = KeyBindings()

    @kb.add("escape")
    def _go_back(event: Any) -> None:
        event.app.exit(exception=KeyboardInterrupt())

    prompt_session = PromptSession(key_bindings=kb)
    prompt_label = f"{text} ({escape_hint})" if escape_hint else text
    return prompt_session.prompt(
        f"{prompt_label}: ",
        default=default if default is not None else "",
    )


def _resolve_startup_workspace_binding(
    *,
    requested_path: Path,
    console: Console,
    interactive: bool,
    create_if_missing: bool = False,
    allow_broad_workspace: bool = False,
    source: str = "explicit_path",
    action: str = WorkspaceAction.CHAT,
) -> WorkspaceBinding:
    default_select_action = _select_guarded_workspace_action_interactive
    default_select_candidate = _select_workspace_candidate_interactive
    default_prompt = _guarded_workspace_prompt_text
    select_action = _patchable(
        "_select_guarded_workspace_action_interactive",
        default_select_action,
    )
    select_candidate = _patchable(
        "_select_workspace_candidate_interactive",
        default_select_candidate,
    )
    prompt = _patchable("_guarded_workspace_prompt_text", default_prompt)
    # When the full-screen TUI is active, render the guarded-workspace steps with
    # the TUI's own picker/prompt chrome instead of the classic inline ones, so the
    # startup screen matches the rest of the experience. The binding *logic* is
    # unchanged — only these display callbacks swap. Any import/terminal failure
    # falls back to the classic selectors above.
    if interactive:
        try:
            from ..tui import is_tui_enabled as _tui_enabled

            if _tui_enabled():
                from ..tui.workspace_guard import (
                    select_guarded_workspace_action as _tui_select_action,
                    select_workspace_candidate as _tui_select_candidate,
                    workspace_guard_prompt_text as _tui_prompt,
                )

                if select_action is default_select_action:
                    select_action = _tui_select_action
                if select_candidate is default_select_candidate:
                    select_candidate = _tui_select_candidate
                if prompt is default_prompt:
                    prompt = _tui_prompt
        except Exception:
            pass
    return _resolve_startup_workspace_binding_impl(
        requested_path=requested_path,
        interactive=interactive,
        create_if_missing=create_if_missing,
        allow_broad_workspace=allow_broad_workspace,
        source=source,
        action=action,
        console=console,
        select_action_interactive=select_action,
        select_candidate_interactive=select_candidate,
        prompt_text=prompt,
    )


def _chat_mode_rows() -> list[tuple[str, str, str]]:
    return [
        (
            "review",
            "1) safe (review)",
            "Ask for approval: asks before file writes and commands; read-only commands run.",
        ),
        (
            "auto",
            "2) fast (auto)",
            "Approve for me: runs normal work; a reviewer model decides sensitive actions "
            "and asks you when unsure.",
        ),
        (
            "readonly",
            "3) read (readonly)",
            "Read-only analysis with inspection-only tools; no writes, shell, or verification.",
        ),
        (
            "fullaccess",
            "4) full (fullaccess)",
            "No sandbox or prompts; still asks before deleting the project or home folder.",
        ),
    ]


def _chat_persona_rows(custom: Any = None) -> list[tuple[str, str, str]]:
    rows = [
        ("code", "1) code", "Implementation work; keeps your execution mode."),
        ("architect", "2) architect", "Plans and designs; writes markdown documents only."),
        ("ask", "3) ask", "Read-only questions with inspection tools."),
        ("debug", "4) debug", "Reproduce-before-fix; keeps your execution mode."),
    ]
    if custom:
        index = len(rows)
        for name in sorted(custom):
            definition = custom[name]
            index += 1
            rows.append(
                (
                    str(name),
                    f"{index}) {name} (custom)",
                    str(getattr(definition, "description", "") or "Custom persona."),
                )
            )
    return rows


def _chat_persona_panel(
    *,
    current_persona: str,
    selected_persona: str | None = None,
    interactive: bool = False,
    custom: Any = None,
) -> Panel:
    current = current_persona.strip().lower() or "code"
    return _selectable_options_panel(
        title=f"Persona Options (current: {current})",
        rows=_chat_persona_rows(custom=custom),
        selected_value=selected_persona,
        interactive=interactive,
    )


def _select_chat_persona_interactive(
    *,
    current_persona: str,
    console: Console,
    custom: Any = None,
) -> tuple[str | None, bool]:
    normalized_current = current_persona.strip().lower() or "code"
    return _patchable("_run_inline_option_selector", _run_inline_option_selector)(
        console=console,
        rows=_chat_persona_rows(custom=custom),
        current_value=normalized_current,
        panel_builder=lambda selected, interactive: _chat_persona_panel(
            current_persona=current_persona,
            selected_persona=selected,
            interactive=interactive,
        ),
        unavailable_label="Persona picker",
    )


def _select_chat_mode_interactive(
    *,
    current_mode: str,
    console: Console,
) -> tuple[str | None, bool]:
    normalized_current = current_mode.strip().lower()
    if normalized_current not in _CHAT_MODES:
        normalized_current = "review"

    rows = _chat_mode_rows()
    return _patchable("_run_inline_option_selector", _run_inline_option_selector)(
        console=console,
        rows=rows,
        current_value=normalized_current,
        panel_builder=lambda selected, interactive: _chat_mode_panel(
            current_mode=current_mode,
            selected_mode=selected,
            interactive=interactive,
        ),
        unavailable_label="Mode picker",
    )


def _chat_mode_display(mode: str) -> str:
    normalized = mode.strip().lower()
    if normalized == "review":
        return "safe (review)"
    if normalized == "auto":
        return "fast (auto)"
    if normalized == "readonly":
        return "read (readonly)"
    if normalized == "fullaccess":
        return "full (fullaccess)"
    return normalized or "unknown"


def _print_fullaccess_mode_warning(*, console: Console) -> None:
    console.print(
        "[yellow]Warning:[/yellow] full (fullaccess) disables write/shell safety guards "
        "and approval prompts."
    )


def _chat_mode_panel(
    *,
    current_mode: str,
    selected_mode: str | None = None,
    interactive: bool = False,
) -> Panel:
    current = current_mode.strip().lower()
    title = f"Permissions (current: {_chat_mode_display(current)})"
    return _selectable_options_panel(
        title=title,
        rows=_chat_mode_rows(),
        selected_value=selected_mode,
        interactive=interactive,
    )


def _chat_trace_rows() -> list[tuple[str, str, str]]:
    return [
        ("off", "1) off", "Hide reasoning summaries and routine activity."),
        (
            "compact",
            "2) compact",
            "Collapsed provider summaries and key activity (recommended).",
        ),
        (
            "full",
            "3) full",
            "Expanded provider summaries and detailed tool activity; never raw thinking.",
        ),
    ]


def _chat_trace_panel(
    *,
    current_level: str,
    selected_level: str | None = None,
    interactive: bool = False,
) -> Panel:
    current = _resolve_trace_level(current_level) or "compact"
    title = f"Trace Options (current: {current})"
    return _selectable_options_panel(
        title=title,
        rows=_chat_trace_rows(),
        selected_value=selected_level,
        interactive=interactive,
    )


def _select_chat_trace_interactive(
    *,
    current_level: str,
    console: Console,
) -> tuple[str | None, bool]:
    normalized_current = _resolve_trace_level(current_level) or "compact"
    rows = _chat_trace_rows()
    return _patchable("_run_inline_option_selector", _run_inline_option_selector)(
        console=console,
        rows=rows,
        current_value=normalized_current,
        panel_builder=lambda selected, interactive: _chat_trace_panel(
            current_level=normalized_current,
            selected_level=selected,
            interactive=interactive,
        ),
        unavailable_label="Trace picker",
    )


def _chat_usage_hud_rows() -> list[tuple[str, str, str]]:
    return [
        ("on", "1) on", "Show usage summary continuously in the toolbar."),
        ("off", "2) off", "Hide usage summary from the toolbar."),
        ("status", "3) status", "Show current HUD setting without changing it."),
    ]


def _chat_usage_hud_panel(
    *,
    current_enabled: bool,
    selected_action: str | None = None,
    interactive: bool = False,
) -> Panel:
    current = "on" if current_enabled else "off"
    title = f"Usage HUD Options (current: {current})"
    return _selectable_options_panel(
        title=title,
        rows=_chat_usage_hud_rows(),
        selected_value=selected_action,
        interactive=interactive,
    )


def _select_chat_usage_hud_interactive(
    *,
    current_enabled: bool,
    console: Console,
) -> tuple[str | None, bool]:
    rows = _chat_usage_hud_rows()
    return _patchable("_run_inline_option_selector", _run_inline_option_selector)(
        console=console,
        rows=rows,
        current_value="on" if current_enabled else "off",
        panel_builder=lambda selected, interactive: _chat_usage_hud_panel(
            current_enabled=current_enabled,
            selected_action=selected,
            interactive=interactive,
        ),
        unavailable_label="Usage HUD picker",
    )


def _forge_assistant_rows() -> list[tuple[str, str, str]]:
    return [
        ("on", "1) on", "Enable planner assistant suggestions."),
        ("off", "2) off", "Disable planner assistant suggestions."),
        ("status", "3) status", "Show assistant status without changing it."),
    ]


def _forge_assistant_panel(
    *,
    enabled: bool,
    selected_action: str | None = None,
    interactive: bool = False,
) -> Panel:
    current = "ON" if enabled else "OFF"
    title = f"Planner Assistant (current: {current})"
    return _selectable_options_panel(
        title=title,
        rows=_forge_assistant_rows(),
        selected_value=selected_action,
        interactive=interactive,
    )


def _select_forge_assistant_interactive(
    *,
    enabled: bool,
    console: Console,
) -> tuple[str | None, bool]:
    rows = _forge_assistant_rows()
    return _patchable("_run_inline_option_selector", _run_inline_option_selector)(
        console=console,
        rows=rows,
        current_value="on" if enabled else "off",
        panel_builder=lambda selected, interactive: _forge_assistant_panel(
            enabled=enabled,
            selected_action=selected,
            interactive=interactive,
        ),
        unavailable_label="Assistant picker",
    )


def _forge_entry_plan_assistant_rows() -> list[tuple[str, str, str]]:
    return [
        ("yes", "1) Yes", "Enable planner assistant now."),
        ("no", "2) No", "Keep assistant off and provide plan text manually."),
    ]


def _forge_entry_plan_assistant_panel(
    *,
    selected_action: str | None = None,
    interactive: bool = False,
) -> Panel:
    return _selectable_options_panel(
        title="Do you want Plan Assistant?",
        rows=_forge_entry_plan_assistant_rows(),
        selected_value=selected_action,
        interactive=interactive,
    )


def _select_forge_entry_plan_assistant_interactive(*, console: Console) -> tuple[str | None, bool]:
    rows = _forge_entry_plan_assistant_rows()
    return _patchable("_run_inline_option_selector", _run_inline_option_selector)(
        console=console,
        rows=rows,
        current_value="yes",
        panel_builder=lambda selected, interactive: _forge_entry_plan_assistant_panel(
            selected_action=selected,
            interactive=interactive,
        ),
        unavailable_label="Plan assistant chooser",
    )


def _prompt_forge_entry_plan_assistant(*, console: Console) -> bool:
    env_override = env_get("ALYSIS_FORGE_PLAN_ASSISTANT")
    if env_override is not None:
        parsed_override = _parse_bool_text(env_override)
        if parsed_override is None:
            console.print(
                "[yellow]Invalid ALYSIS_FORGE_PLAN_ASSISTANT value; defaulting to OFF.[/yellow]"
            )
            return False
        return parsed_override

    if _patchable("_is_non_interactive_terminal", _is_non_interactive_terminal)():
        return False

    action, picker_available = _select_forge_entry_plan_assistant_interactive(console=console)
    if picker_available:
        return action == "yes"

    try:
        fallback = typer.prompt("Do you want Plan Assistant? [y/N]", default="n").strip()
    except (EOFError, KeyboardInterrupt):
        console.print("")
        return False
    parsed = _parse_bool_text(fallback)
    if parsed is None:
        console.print("[yellow]Invalid selection; defaulting to No.[/yellow]")
        return False
    return parsed


def _forge_plan_command_rows() -> list[tuple[str, str, str]]:
    return [
        ("tasks", "1) show summary", "Show the current plan summary and task list."),
        ("markdown", "2) markdown", "Render PLAN.md for the current run."),
        ("edit", "3) edit", "Edit plan.json and reload it into Forge."),
    ]


def _forge_plan_command_panel(
    *,
    selected_action: str | None = None,
    interactive: bool = False,
) -> Panel:
    return _selectable_options_panel(
        title="Forge Plan",
        rows=_forge_plan_command_rows(),
        selected_value=selected_action,
        interactive=interactive,
    )


def _select_forge_plan_command_interactive(*, console: Console) -> tuple[str | None, bool]:
    rows = _forge_plan_command_rows()
    return _patchable("_run_inline_option_selector", _run_inline_option_selector)(
        console=console,
        rows=rows,
        current_value="tasks",
        panel_builder=lambda selected, interactive: _forge_plan_command_panel(
            selected_action=selected,
            interactive=interactive,
        ),
        unavailable_label="Forge plan command picker",
    )


__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
