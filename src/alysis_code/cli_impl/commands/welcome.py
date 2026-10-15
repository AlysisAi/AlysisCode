# ruff: noqa: F401,F403,F405,I001
# Legacy split module: dependencies are synced by cli_surface.py.
from __future__ import annotations

from ...surface.theme import (
    _hex_to_8bit,
    _load_jsonc_file,
    _strip_json_comments,
    _theme_from_colorfgbg,
    _theme_from_hex_background,
    _theme_from_osc11,
    _theme_from_windows_terminal_settings,
    _windows_terminal_builtin_scheme_theme,
    _windows_terminal_scheme_name,
    _windows_terminal_settings_paths,
    detect_terminal_theme as _detect_owl_theme,
)
from .cli_common import *
from ...branding import env_get
from ...tools.availability import tool_availability_snapshot as copy_tool_availability_snapshot
from ..chat.forge_visibility import command_visible, visible_commands


def stripAnsi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


def visibleLength(value: str) -> int:
    return len(stripAnsi(value))


def padLine(content: str, width: int) -> str:
    current = visibleLength(content)
    if current >= width:
        return content
    return content + (" " * (width - current))


def _clip_visible_line(content: str, width: int) -> str:
    if width <= 0:
        return ""
    if visibleLength(content) <= width:
        return content
    cells = _ansi_visible_cells(content)
    clipped = "".join(cells[:width])
    if "\x1b[" in clipped:
        clipped += "\x1b[0m"
    return clipped


def _welcome_stream(console: Console | None) -> Any | None:
    if console is None:
        return None
    return console.file if getattr(console, "file", None) is not None else sys.stdout


def _welcome_stream_is_tty(stream: Any | None) -> bool:
    isatty = getattr(stream, "isatty", None)
    if not callable(isatty):
        return False
    try:
        return bool(isatty())
    except Exception:
        return False


def _truthy_env(name: str) -> bool:
    return str(env_get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _owl_assets_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "assets" / "owl"


def _should_animate_owl_logo(stream: Any | None) -> bool:
    if _truthy_env("ALYSIS_NO_OWL") or env_get("ALYSIS_NO_INTRO"):
        return False
    if os.environ.get("CI") or _truthy_env("ALYSIS_CI"):
        return False
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return False
    if not _welcome_stream_is_tty(stream):
        return False
    stdout = getattr(sys, "stdout", None)
    if stream is not stdout:
        return False
    if not _welcome_stream_is_tty(getattr(sys, "stdin", None)):
        return False
    return _welcome_stream_is_tty(stdout)


def _load_owl_logo_frames(
    *,
    stream: Any | None,
    color_enabled: bool,
    theme: str | None = None,
) -> list[list[str]]:
    if _truthy_env("ALYSIS_NO_OWL"):
        return []
    theme = theme or _detect_owl_theme(stream)
    frame_dir_name = "ascii"
    frame_dir = _owl_assets_dir() / frame_dir_name
    try:
        frames = [
            path.read_text(encoding="utf-8", errors="replace").splitlines()
            for path in sorted(frame_dir.glob("f-*.txt"))
        ]
    except Exception:
        frames = []
    if not frames:
        return []
    if not color_enabled or theme == "neutral":
        # With no reliable background signal, use the terminal's default
        # foreground. Terminal profiles are expected to keep that readable on
        # their own background; retaining the asset's fixed gray ramp would
        # instead make the owl disappear on one side of the light/dark divide.
        return [[stripAnsi(line) for line in frame] for frame in frames]
    return frames


def _paint_owl_light_panel(frames: list[list[str]]) -> list[list[str]]:
    if not frames:
        return frames
    bg = "\x1b[48;5;231m"
    reset = "\x1b[0m"
    horizontal_padding = 2
    # Runtime-only contrast remap for light owl grays on the white panel.
    panel_gray_map = {
        "238": "16",
        "239": "16",
        "240": "16",
        "241": "234",
        "242": "234",
        "243": "234",
        "244": "236",
        "245": "236",
        "246": "236",
        "247": "238",
        "248": "238",
    }

    def remap_panel_grays(row: str) -> str:
        return re.sub(
            r"\x1b\[38;5;(238|239|240|241|242|243|244|245|246|247|248)m",
            lambda match: f"\x1b[38;5;{panel_gray_map[match.group(1)]}m",
            row,
        )

    width = max((visibleLength(row) for frame in frames for row in frame), default=0)
    if width <= 0:
        return frames
    side_pad = " " * horizontal_padding
    blank = f"{bg}{' ' * (width + (horizontal_padding * 2))}{reset}"
    painted_frames: list[list[str]] = []
    for frame in frames:
        painted = [blank]
        for row in frame:
            contrast_row = remap_panel_grays(row)
            safe_row = contrast_row.replace(reset, f"{reset}{bg}")
            painted.append(f"{bg}{side_pad}{padLine(safe_row, width)}{side_pad}{reset}")
        painted.append(blank)
        painted_frames.append(painted)
    return painted_frames


def _ansi_visible_cells(line: str) -> list[str]:
    cells: list[str] = []
    active_style = ""
    index = 0
    for match in re.finditer(r"\x1b\[[0-9;]*m", line):
        for char in line[index : match.start()]:
            cells.append(f"{active_style}{char}" if active_style else char)
        token = match.group(0)
        code = match.group(0)[2:-1]
        active_style = "" if code in {"0", "39"} else token
        index = match.end()
    for char in line[index:]:
        cells.append(f"{active_style}{char}" if active_style else char)
    return cells


def _crop_owl_logo_frames(frames: list[list[str]]) -> list[list[str]]:
    if not frames:
        return frames
    plain_frames = [[stripAnsi(line) for line in frame] for frame in frames]
    height = max((len(frame) for frame in plain_frames), default=0)
    width = max((len(line) for frame in plain_frames for line in frame), default=0)
    if height <= 0 or width <= 0:
        return frames

    occupied: list[tuple[int, int]] = []
    for frame in plain_frames:
        for row_index in range(height):
            line = frame[row_index] if row_index < len(frame) else ""
            for col_index, char in enumerate(line.ljust(width)):
                if char != " ":
                    occupied.append((row_index, col_index))
    if not occupied:
        return frames

    top = min(row for row, _ in occupied)
    bottom = max(row for row, _ in occupied) + 1
    left = min(col for _, col in occupied)
    right = max(col for _, col in occupied) + 1

    cropped_frames: list[list[str]] = []
    for frame in frames:
        cropped_frame: list[str] = []
        for row_index in range(top, bottom):
            line = frame[row_index] if row_index < len(frame) else ""
            cells = _ansi_visible_cells(line)
            cells.extend(" " for _ in range(max(width - len(cells), 0)))
            row = "".join(cells[left:right])
            if "\x1b[" in row:
                row += "\x1b[0m"
            cropped_frame.append(row)
        cropped_frames.append(cropped_frame)
    return cropped_frames


def _welcome_style(value: str, code: str, *, enabled: bool) -> str:
    if not enabled or not code:
        return value
    return f"\x1b[{code}m{value}\x1b[0m"


def _welcome_palette(theme: str) -> dict[str, str]:
    if theme == "dark":
        return {
            "primary": "1;97",
            "muted": "97",
            "label": "97",
            "value": "97",
            "rule": "97",
        }
    if theme == "light":
        return {
            "primary": "1;30",
            "muted": "30",
            "label": "30",
            "value": "30",
            "rule": "30",
        }
    return {
        "primary": "1",
        "muted": "",
        "label": "",
        "value": "",
        "rule": "",
    }


def _welcome_workspace_value(workspace: str | os.PathLike[str] | None) -> str:
    if workspace is None:
        return ""
    raw = os.fspath(workspace).strip()
    if not raw:
        return ""
    try:
        resolved = Path(raw).expanduser().resolve()
        home = Path.home().resolve()
        try:
            rel_home = resolved.relative_to(home)
            display = "~" if os.fspath(rel_home) == "." else f"~/{rel_home.as_posix()}"
        except ValueError:
            display = os.fspath(resolved)
    except Exception:
        display = raw
    if len(display) > 30:
        basename = Path(display).name
        if basename:
            return basename
    return display


def _welcome_model_value(model: str | None) -> str:
    clean = str(model or "").strip()
    if not clean:
        return ""
    if "/" in clean:
        clean = clean.rsplit("/", 1)[-1].strip()
    return clean


def _welcome_context_line(
    *,
    workspace: str | os.PathLike[str] | None,
    model: str | None,
    version: str | None,
    color_enabled: bool,
    palette: dict[str, str],
) -> str:
    def label_text(value: str) -> str:
        return _welcome_style(value, palette["label"], enabled=color_enabled)

    def value_text(value: str) -> str:
        return _welcome_style(value, palette["value"], enabled=color_enabled)

    parts: list[str] = []
    workspace_value = _welcome_workspace_value(workspace)
    model_value = _welcome_model_value(model)
    version_value = str(version or "").strip()
    if workspace_value:
        parts.append(f"{label_text('workspace')} {value_text(workspace_value)}")
    if model_value:
        parts.append(f"{label_text('model')} {value_text(model_value)}")
    if version_value:
        parts.append(f"{label_text('version')} {value_text(version_value)}")
    return "   ".join(parts)


def _welcome_context_lines(
    *,
    workspace: str | os.PathLike[str] | None,
    model: str | None,
    version: str | None,
    color_enabled: bool,
    palette: dict[str, str],
    max_width: int,
) -> list[str]:
    line = _welcome_context_line(
        workspace=workspace,
        model=model,
        version=version,
        color_enabled=color_enabled,
        palette=palette,
    )
    if not line:
        return []
    if visibleLength(line) <= max_width:
        return [line]

    workspace_line = _welcome_context_line(
        workspace=workspace,
        model=None,
        version=None,
        color_enabled=color_enabled,
        palette=palette,
    )
    model_version_line = _welcome_context_line(
        workspace=None,
        model=model,
        version=version,
        color_enabled=color_enabled,
        palette=palette,
    )
    if model_version_line and visibleLength(model_version_line) > max_width:
        model_line = _welcome_context_line(
            workspace=None,
            model=model,
            version=None,
            color_enabled=color_enabled,
            palette=palette,
        )
        version_line = _welcome_context_line(
            workspace=None,
            model=None,
            version=version,
            color_enabled=color_enabled,
            palette=palette,
        )
        return [line for line in (workspace_line, model_line, version_line) if line]
    return [line for line in (workspace_line, model_version_line) if line]


def printWelcome(
    console: Console | None = None,
    *,
    workspace: str | os.PathLike[str] | None = None,
    model: str | None = None,
    version: str | None = __version__,
) -> str:
    stream = _welcome_stream(console)
    terminal_width = shutil.get_terminal_size((80, 20)).columns
    safe_line_width = max(1, terminal_width - 1)
    no_color = bool(os.environ.get("NO_COLOR"))
    color_enabled = not no_color and (stream is None or _welcome_stream_is_tty(stream))
    indent = "  "
    gap = "   "
    welcome_theme = _detect_owl_theme(stream)
    palette = _welcome_palette(welcome_theme)
    owl_frames = _load_owl_logo_frames(
        stream=stream,
        color_enabled=color_enabled,
        theme=welcome_theme,
    )
    owl_frames = _crop_owl_logo_frames(owl_frames)
    if welcome_theme == "dark" and color_enabled:
        owl_frames = _paint_owl_light_panel(owl_frames)
    fallback_icon_frames = [
        [
            "    ◇     ",
            "   ╱ ╲    ",
            "  ◇  ◇   ",
            "   ╲ ╱    ",
            "    ◇     ",
        ]
    ]
    logo_frames = owl_frames or fallback_icon_frames
    logo_height = max((len(frame) for frame in logo_frames), default=0)
    logo_width = max(
        (visibleLength(row) for frame in logo_frames for row in frame),
        default=0,
    )
    logo_frames = [
        [padLine(row, logo_width) for row in frame]
        + [(" " * logo_width) for _ in range(max(logo_height - len(frame), 0))]
        for frame in logo_frames
    ]
    logo = logo_frames[0]
    if terminal_width <= logo_width + visibleLength(indent):
        indent = ""
    narrow = safe_line_width < visibleLength(indent) + logo_width + visibleLength(gap) + 31

    def primary_text(value: str) -> str:
        return _welcome_style(value, palette["primary"], enabled=color_enabled)

    def rule_text(value: str) -> str:
        return _welcome_style(value, palette["rule"], enabled=color_enabled)

    def body_text(value: str) -> str:
        return _welcome_style(value, palette["muted"], enabled=color_enabled)

    brand = f"{primary_text('Alysis Code')}{rule_text('  ·  ')}{primary_text('AlysisAI')}"
    tagline = body_text("The autonomous coding agent")

    def welcome_detail_lines(max_width: int) -> list[str]:
        context_lines = _welcome_context_lines(
            workspace=workspace,
            model=model,
            version=version,
            color_enabled=color_enabled,
            palette=palette,
            max_width=max_width,
        )
        detail = [
            brand,
            tagline,
            "",
            *context_lines,
            rule_text("─" * min(max(max_width, 1), 64)),
            "",
            f"{primary_text('/forge')}     {body_text('begin an autonomous run')}",
            f"{primary_text('/status')}    {body_text('view run state and usage')}",
            f"{primary_text('/help')}      {body_text('show all commands')}",
        ]
        return detail

    def welcome_lines_for_logo(logo_frame: list[str]) -> list[str]:
        if narrow:
            return [
                *(f"{indent}{row.rstrip()}" for row in logo_frame),
                "",
                f"{indent}{brand}",
                f"{indent}{tagline}",
                "",
                f"{indent}{primary_text('/forge')}  {primary_text('/status')}  {primary_text('/help')}",
            ]

        if owl_frames:
            right_width = max(
                safe_line_width - visibleLength(indent) - logo_width - visibleLength(gap),
                1,
            )
            detail_lines = welcome_detail_lines(right_width)
            required_side_by_side_width = visibleLength(indent) + logo_width + visibleLength(gap)
            required_side_by_side_width += max(
                (visibleLength(line) for line in detail_lines),
                default=0,
            )
            if safe_line_width >= required_side_by_side_width:
                right_start = max((len(logo_frame) - len(detail_lines)) // 2, 0)
                rendered_lines = []
                total_rows = max(len(logo_frame), right_start + len(detail_lines))
                for row_index in range(total_rows):
                    row = logo_frame[row_index] if row_index < len(logo_frame) else ""
                    line = f"{indent}{padLine(row, logo_width)}"
                    right_index = row_index - right_start
                    if 0 <= right_index < len(detail_lines):
                        line = f"{line}{gap}{detail_lines[right_index]}"
                    rendered_lines.append(_clip_visible_line(line.rstrip(), safe_line_width))
                return rendered_lines
            else:
                rendered_lines = []
                rendered_lines.extend(
                    _clip_visible_line(f"{indent}{row.rstrip()}".rstrip(), safe_line_width)
                    for row in logo_frame
                )
                rendered_lines.append("")
                full_detail_width = max(safe_line_width - visibleLength(indent), 1)
                full_detail_lines = welcome_detail_lines(full_detail_width)
                rendered_lines.extend(
                    _clip_visible_line(f"{indent}{line}".rstrip(), safe_line_width)
                    for line in full_detail_lines
                )
                return rendered_lines

        context_line = _welcome_context_line(
            workspace=workspace,
            model=model,
            version=version,
            color_enabled=color_enabled,
            palette=palette,
        )
        rendered_lines = [
            f"{indent}{logo_frame[0]}{gap}{brand}",
            f"{indent}{logo_frame[1].rstrip()}",
            f"{indent}{logo_frame[2]}{gap}{tagline}",
            f"{indent}{logo_frame[3].rstrip()}",
            f"{indent}{logo_frame[4].rstrip()}",
            "",
        ]
        if context_line:
            rendered_lines.append(f"{indent}{context_line}")
        rendered_lines.extend(
            [
                f"{indent}{rule_text('─' * min(max(terminal_width - 4, 1), 64))}",
                "",
                f"{indent}{primary_text('/forge')}   {body_text('begin an autonomous run')}",
                f"{indent}{primary_text('/status')}    {body_text('view run state and usage')}",
                f"{indent}{primary_text('/help')}      {body_text('show all commands')}",
            ]
        )
        return rendered_lines

    lines = welcome_lines_for_logo(logo)
    lines = [_clip_visible_line(line, safe_line_width) for line in lines]

    display_lines = ["", *lines, ""]
    banner = "\n".join(display_lines)
    if console is not None and stream is not None:
        try:
            should_animate = (
                bool(owl_frames)
                and _welcome_stream_is_tty(stream)
                and not no_color
                and _patchable("_should_animate_owl_logo", _should_animate_owl_logo)(stream)
            )
            if should_animate:
                animation_frames = [
                    welcome_lines_for_logo(frame)
                    for frame in (logo_frames[0], *logo_frames[1:], logo_frames[0])
                ]
                block_height = max(
                    (len(frame_lines) for frame_lines in animation_frames), default=0
                )
                block_height += 1

                def write_animation_block(frame_lines: list[str]) -> None:
                    for row_index in range(block_height - 1):
                        line = frame_lines[row_index] if row_index < len(frame_lines) else ""
                        line = _clip_visible_line(line, safe_line_width)
                        stream.write(f"\r\x1b[2K{line}\n")
                    stream.write("\r\x1b[2K\n")

                stream.write("\n")
                write_animation_block(animation_frames[0])
                stream.flush()
                for frame_lines in animation_frames[1:]:
                    time.sleep(0.10)
                    stream.write(f"\x1b[{block_height}A\r")
                    write_animation_block(frame_lines)
                    stream.flush()
                stream.flush()
            else:
                stream.write(banner + "\n")
                stream.flush()
        except Exception:
            console.print(banner, markup=False, highlight=False, soft_wrap=True)
    return banner


def _chat_session_info_line(*, workspace: Path, model: str, mode: str) -> str:
    workspace_name = workspace.name or os.fspath(workspace)
    parts = [workspace_name, str(model or "?").strip() or "?", str(mode or "?").strip() or "?"]
    return " · ".join(parts)


def _render_command_sections_panel(
    *,
    title: str,
    sections: list[tuple[str, list[tuple[str, str]]]],
) -> Panel:
    if _patchable("_is_narrow_terminal", _is_narrow_terminal)():
        lines: list[str] = []
        for section_name, rows in sections:
            lines.append(f"\n[bold]{section_name}[/bold]")
            for cmd, desc in rows:
                lines.append(f"  {cmd}  {desc}")
        return _Panel("\n".join(lines).strip(), title=title, border_style="bright_black")

    table = _Table(show_header=False, box=None, expand=True, padding=(0, 1), collapse_padding=True)
    table.add_column("command", style=STYLE_EMPHASIS, no_wrap=True, ratio=2)
    table.add_column("description", style="dim", no_wrap=False, ratio=5, overflow="fold")
    for idx, (section_name, rows) in enumerate(sections):
        table.add_row(f"[bold]{section_name}[/bold]", "")
        for cmd, desc in rows:
            table.add_row(cmd, desc)
        if idx < len(sections) - 1:
            table.add_row("", "")
    return _Panel(table, title=title, border_style="bright_black")


def _forge_help_footer_lines() -> list[str]:
    return [
        "Plain /forge starts a fresh run for a new chat session.",
        "/forge resume explicitly attaches to the current run pointer.",
        "After /back, plain /forge resumes this chat session's run only in the same workspace.",
        "Same-workspace re-entry keeps the same run id and tracks the chat's current focus.",
        "Changing workspaces starts a fresh run instead of resuming prior session-local state.",
        "Type freely to add requirements or talk to the planner.",
        "/plan tasks|markdown|edit views or edits the plan; /back returns to chat.",
    ]


def _is_forge_ui_mode(ui_mode: str) -> bool:
    return str(ui_mode or "").strip().lower() == "forge"


def _forge_commands_panel() -> Panel:
    sections = _chat_command_sections(ui_mode="forge")
    footer_lines = _forge_help_footer_lines()
    if _patchable("_is_narrow_terminal", _is_narrow_terminal)():
        lines: list[str] = []
        for section_name, rows in sections:
            lines.append(f"\n[bold]{section_name}[/bold]")
            for cmd, desc in rows:
                lines.append(f"  {cmd}  {desc}")
        lines.append("")
        for line in footer_lines:
            lines.append(f"[dim]{line}[/dim]")
        return _Panel("\n".join(lines).strip(), title="Forge Commands", border_style="bright_black")

    table = _Table(show_header=False, box=None, expand=True, padding=(0, 1), collapse_padding=True)
    table.add_column("command", style=STYLE_EMPHASIS, no_wrap=True, ratio=2)
    table.add_column("description", style="dim", no_wrap=False, ratio=5, overflow="fold")
    for idx, (section_name, rows) in enumerate(sections):
        table.add_row(f"[bold]{section_name}[/bold]", "")
        for cmd, desc in rows:
            table.add_row(cmd, desc)
        if idx < len(sections) - 1:
            table.add_row("", "")

    content = _table_grid(expand=True)
    content.add_row(table)
    content.add_row("")
    for line in footer_lines:
        content.add_row(_forge_bar_text(text=line, style="dim"))
    return _Panel(content, title="Forge Commands", border_style="bright_black")


def _chat_command_sections(*, ui_mode: str = "chat") -> list[tuple[str, list[tuple[str, str]]]]:
    if _is_forge_ui_mode(ui_mode):
        return [
            (
                "Forge",
                [
                    ("/help", "show Forge & chat commands"),
                    ("/show", "show the current plan summary"),
                    ("/plan markdown|md", "preview PLAN.md for the current run"),
                    ("/plan edit", "edit plan.json and reload Forge state"),
                    ("/execute plan", "save, validate, and run scoped planned tasks"),
                    ("/goal <text>", "set project goal"),
                    (
                        "/task <title>",
                        "add a task; ambiguous/mutating work must name repo-relative file paths",
                    ),
                    ("/done", "save + validate the plan, then return to chat"),
                    ("/back", "return to chat without that final save/validate step"),
                ],
            ),
            (
                "Session",
                [
                    ("/status", "session details"),
                    ("/subagents", "open an active subagent run"),
                    ("/terminals", "list/read/kill background processes"),
                    ("/pwd", "show active workdir, focus dir, and workspace root"),
                    ("/usage", "token count & cost; /usage hud on|off toggles HUD"),
                    ("/permissions", "change execution mode"),
                    ("/persona", "switch persona (code, architect, ask, debug)"),
                    ("/stream", "toggle streaming"),
                    ("/trace", "reasoning detail (off/compact/full)"),
                ],
            ),
            (
                "Context",
                [
                    ("/context", "context window left"),
                    ("/compact [focus]", "force compaction"),
                    ("/resume [id]", "continue previous session"),
                    ("/report [text]", "create feedback bundle + issue draft"),
                    ("/feedback [text]", "alias for /report"),
                ],
            ),
            (
                "Tools & Config",
                [
                    ("/login", "choose Alysis Code or an AI subscription connection"),
                    (
                        "/skill",
                        "no args lists; <name> shows info; <name> <task> attaches",
                    ),
                    ("/image [path]", "attach an image or file (drag & drop, or Ctrl+V to paste)"),
                    ("/assets", "open assets for the active Forge run"),
                    ("/config", "inline config menu; /config show|set|clear for model metadata"),
                    ("/toolbar", "customize toolbar items"),
                    ("/exit", "quit"),
                ],
            ),
        ]

    sections = [
        (
            "Getting Started",
            [
                ("/help", "commands & config"),
                ("/status", "session details"),
                ("/subagents", "open an active subagent run"),
                ("/terminals", "list/read/kill background processes"),
                ("/pwd", "show active workdir, focus dir, and workspace root"),
                ("/usage", "token count & cost; /usage hud on|off toggles HUD"),
            ],
        ),
        (
            "Execution",
            [
                ("/permissions", "change execution mode"),
                ("/persona", "switch persona (code, architect, ask, debug)"),
                ("/ask <question>", "one read-only turn; mode restored afterwards"),
                ("/trace", "reasoning detail (off/compact/full)"),
            ],
        ),
        (
            "Context",
            [
                ("/context", "context window left"),
                ("/compact [focus]", "force compaction"),
                ("/clear", "wipe conversation (keeps session id + log; Ctrl+L clears terminal)"),
                ("/resume [id]", "continue previous session"),
                ("/report [text]", "create feedback bundle + issue draft"),
                ("/feedback [text]", "alias for /report"),
            ],
        ),
        (
            "Tools & Subagents",
            [
                (
                    "/skill",
                    "no args lists; <name> shows info; <name> <task> attaches",
                ),
                ("$<name> [task]", "show skill info or attach it for one turn"),
                ("/image [path]", "attach an image or file (drag & drop, or Ctrl+V to paste)"),
                ("/assets", "open assets for the current Forge run pointer"),
                (
                    "/forge [resume]",
                    "fresh run by default; same-workspace re-entry resumes session-local state and tracks the current focus; resume explicitly loads the current pointer",
                ),
            ],
        ),
        (
            "Configuration",
            [
                ("/login", "choose Alysis Code or an AI subscription connection"),
                ("/config", "inline config menu; /config show|set|clear for model metadata"),
                ("/toolbar", "customize toolbar items"),
                ("/exit", "quit"),
            ],
        ),
    ]
    return [
        (
            section_name,
            [row for row in rows if command_visible(row[0], ui_mode=ui_mode)],
        )
        for section_name, rows in sections
    ]


def _chat_commands_panel(*, ui_mode: str = "chat") -> Panel:
    if _is_forge_ui_mode(ui_mode):
        return _forge_commands_panel()
    title = "Forge Commands" if _is_forge_ui_mode(ui_mode) else "Commands"
    return _render_command_sections_panel(
        title=title,
        sections=_chat_command_sections(ui_mode=ui_mode),
    )


def _chat_quick_commands_panel(*, ui_mode: str = "chat") -> Panel:
    return _chat_commands_panel(ui_mode=ui_mode)


def _chat_visible_commands(*, ui_mode: str = "chat") -> list[str]:
    if _is_forge_ui_mode(ui_mode):
        return visible_commands(_FORGE_SUGGESTION_COMMANDS, ui_mode=ui_mode)
    return visible_commands(_CHAT_GLOBAL_VISIBLE_COMMANDS, ui_mode=ui_mode)


def _chat_completer_commands(*, ui_mode: str = "chat") -> list[str]:
    if _is_forge_ui_mode(ui_mode):
        return visible_commands(_FORGE_COMPLETER_COMMANDS, ui_mode=ui_mode)
    commands = _ordered_unique_strings(
        _CHAT_GLOBAL_VISIBLE_COMMANDS
        + [
            "/forge resume",
            "/usage hud",
            "/usage hud on",
            "/usage hud off",
            "/usage hud status",
            "/terminals list",
            "/terminals show",
            "/terminals kill",
            "/terminals help",
        ]
    )
    return visible_commands(commands, ui_mode=ui_mode)


def _suggest_chat_command(raw_command: str, *, ui_mode: str = "chat") -> str | None:
    candidate = raw_command.strip().lower()
    if not candidate.startswith("/"):
        return None
    if candidate in _CHAT_RETIRED_COMMANDS:
        return None
    if candidate in _CHAT_COMMANDS:
        return None
    matches = get_close_matches(candidate, _chat_visible_commands(ui_mode=ui_mode), n=1, cutoff=0.6)
    return matches[0] if matches else None


def _rebuild_session_tools_for_mode(*, session: Any, mode: str) -> None:
    coordinator_holder: dict[str, Any] = {}
    scheduler_holder: dict[str, Any] = {}
    availability_holder: dict[str, Any] = {}
    build_kwargs = _session_build_tools_kwargs(session=session, mode=mode)
    build_kwargs["subagent_coordinator_sink"] = lambda coordinator: coordinator_holder.__setitem__(
        "coordinator", coordinator
    )
    build_kwargs["child_scheduler_sink"] = lambda scheduler: scheduler_holder.__setitem__(
        "scheduler", scheduler
    )
    build_kwargs["tool_availability_sink"] = lambda snapshot: availability_holder.__setitem__(
        "snapshot", copy_tool_availability_snapshot(snapshot)
    )
    tools = build_tools(**build_kwargs)
    tool_list = [tool.as_openai_tool() for tool in tools.values()]
    previous_scheduler = getattr(session, "child_scheduler", None)
    replacement_coordinator = coordinator_holder.get(
        "coordinator",
        getattr(session, "subagent_coordinator", None),
    )
    replacement_scheduler = scheduler_holder.get("scheduler", previous_scheduler)
    replacement_availability = availability_holder["snapshot"]
    session.tools = tools
    session.tool_list = tool_list
    session.tool_availability_snapshot = replacement_availability
    session.subagent_coordinator = replacement_coordinator
    session.child_scheduler = replacement_scheduler


def _session_build_tools_kwargs(*, session: Any, mode: str) -> dict[str, Any]:
    cfg = getattr(session, "cfg", None)
    max_steps = int(
        getattr(session, "max_steps", getattr(cfg, "max_steps", DEFAULT_CHAT_MAX_STEPS))
        or DEFAULT_CHAT_MAX_STEPS
    )
    authoritative_verification_commands = getattr(
        session,
        "authoritative_verification_commands",
        None,
    )
    verification_selection = _agent_loop_module()._session_verify_command_selection(session)
    effective_verification_commands = getattr(
        session,
        "effective_verification_commands",
        None,
    )
    subagent_coordinator = getattr(session, "subagent_coordinator", None)
    legacy_scheduler = getattr(session, "child_scheduler", None)
    if subagent_coordinator is None and callable(getattr(legacy_scheduler, "bind_launcher", None)):
        # ChildScheduler is a compatibility name for the same stable engine.
        # Adopt it explicitly so older live sessions also rebuild in place.
        subagent_coordinator = legacy_scheduler
    return {
        "root": Path(getattr(session, "root", Path("."))),
        "console": getattr(session, "console", None),
        "surface": getattr(session, "surface", None),
        "store": session.store,
        # Carried across tool rebuilds (/permissions and plan-mode transitions):
        # dropping it would silently stop tracking the verifier's process groups
        # for the rest of the session.
        "process_group_registry": getattr(session, "process_group_registry", None),
        "mode": mode,
        "yes": bool(getattr(session, "yes", False)),
        "cfg": cfg,
        # Carried across tool rebuilds so an active switch_mode coordination
        # cell (and the tool itself) survives /permissions and persona transitions.
        "persona_switch_state": getattr(session, "persona_switch_state", None),
        "api_key": str(getattr(session, "api_key", "") or "") or None,
        "max_steps": max_steps,
        "no_log": bool(getattr(session, "no_log", False)),
        "usage_role": str(getattr(session, "usage_role", "main") or "main"),
        "usage_summary": getattr(session, "usage_summary", None),
        "model_registry": getattr(session, "model_registry", None),
        "deny_write_prefixes": getattr(session, "deny_write_prefixes", None),
        "allow_write_globs": getattr(session, "allow_write_globs", None),
        "persona_allow_write_globs": getattr(session, "persona_allow_write_globs", None),
        "non_interactive": bool(getattr(session, "non_interactive", False)),
        "shell_runner": getattr(session, "shell_runner", None),
        "shell_environment": getattr(session, "shell_environment", None),
        "terminal_manager": getattr(session, "terminal_manager", None),
        # Carried across tool rebuilds for the same reason as the process-group
        # registry: the rewrite guard is per-run and fires once per file, so
        # dropping it here would silently stop warning for the rest of the
        # session and would forget which files it had already warned about.
        "edit_discipline": getattr(session, "edit_discipline", None),
        # Carried for the same reason again: the registry is what remembers
        # which durable services this run started, so dropping it would leave
        # the finalization liveness check blind to every service started before
        # the rebuild and silently stop reporting a dead one.
        "persistent_service_registry": getattr(session, "persistent_service_registry", None),
        "verification_enabled": bool(getattr(session, "verification_enabled", True)),
        "authoritative_verification_commands": (
            list(authoritative_verification_commands)
            if authoritative_verification_commands is not None
            else None
        ),
        "effective_verification_commands": list(effective_verification_commands or []),
        "verify_command_selection": verification_selection,
        "get_verify_command_selection": lambda: (
            _agent_loop_module()._session_verify_command_selection(session)
        ),
        "one_shot_execution": bool(getattr(session, "one_shot_execution", False)),
        "skills_enabled": bool(getattr(session, "skills_enabled", True)),
        "skill_registry": getattr(session, "skill_registry", None),
        "subagents_enabled": bool(getattr(session, "subagents_enabled", False)),
        "subagent_depth": int(getattr(session, "subagent_depth", 0) or 0),
        "subagent_registry": getattr(session, "subagent_registry", None),
        "subagent_coordinator": subagent_coordinator,
        "parent_steer_inbox": getattr(session, "steer_inbox", None),
        "session_log_dir_override": getattr(session, "session_log_dir_override", None),
        "step_budget_runtime": getattr(session, "step_budget_runtime", None),
        "runtime_kind": getattr(session, "runtime_kind", "interactive_chat"),
        "mcp_manager": getattr(session, "mcp_manager", None),
        "custom_tool_session_state": getattr(session, "custom_tool_session_state", None),
        "managed_browser_service": getattr(session, "managed_browser_service", None),
        "managed_browser_owner_id": getattr(session, "managed_browser_owner_id", None),
        "managed_browser_cancel_check": getattr(session, "managed_browser_cancel_check", None),
        # Carried across rebuilds so "Approve for me" keeps its reviewer and denial counts
        # through mode, model and persona switches.
        "approval_reviewer": getattr(session, "approval_reviewer", None),
        "get_active_workdir_relpath": lambda: resolve_session_active_workdir_relpath(session),
        "set_active_workdir_callback": (
            lambda raw_path, source: set_session_active_workdir(
                session,
                raw_path,
                source=source,
            )
        ),
    }


def _chat_help_panel(*, ui_mode: str = "chat") -> Panel:
    return _chat_commands_panel(ui_mode=ui_mode)


__all__ = [name for name in globals() if (not name.startswith("__") or name == "__version__")]
