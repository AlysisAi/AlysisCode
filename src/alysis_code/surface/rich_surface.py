from __future__ import annotations

import math
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.prompt import Prompt
from rich.text import Text

from ..approval_scope import (
    approval_dir_grant_candidate,
    approval_session_scope_for_request,
    request_matches_dir_grants,
)
from ..interactive_input_guard import interactive_prompt_guard
from ..llm_error_display import classify_llm_error_display, friendly_llm_error_message
from ..subagent_labels import subagent_identity
from ..tools.registry import (
    summarize_tool_output_chunk as _summarize_tool_output,
)
from ..tools.registry import (
    tool_display_name as _tool_display_name,
)
from ..tools.registry import (
    tool_input_preview as _tool_input_preview,
)
from .console import (
    ascii_sanitize,
    console_glyph,
    console_uses_ascii,
    make_console,
    safe_plain_error,
    safe_write,
)
from .events import (
    ApprovalReviewed,
    ConfigFormRequest,
    ErrorRaised,
    Event,
    InfoEmitted,
    MessageDelta,
    MessageEnd,
    ModeChanged,
    PersonaChanged,
    PlanNodeUpdated,
    PromptForInput,
    ReviewGateDecision,
    StatusUpdate,
    SwarmWorkerStateChanged,
    ToolCallCompleted,
    ToolCallProgress,
    ToolCallStarted,
    VerifyGateResult,
    WarningEmitted,
)
from .styles import (
    STYLE_CHROME,
    STYLE_CONTENT,
    STYLE_DIM,
    STYLE_EMPHASIS,
    STYLE_ERROR,
    STYLE_SUBAGENT,
    STYLE_SUCCESS,
    STYLE_WARN,
)
from .types import (
    ApprovalDecision,
    ApprovalRequest,
    PatchEvent,
    StandingApproval,
    StatusEvent,
    SubagentEndEvent,
    SubagentStartEvent,
    ToolEndEvent,
    ToolOutputEvent,
    ToolStartEvent,
    approval_review_line,
    remote_site_outcome,
)

_SENSITIVE_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]{8,}", re.IGNORECASE),
    re.compile(r"(Authorization\s*:\s*)(.+)", re.IGNORECASE),
]
_TRACE_LEVELS = {"off", "compact", "full"}
_MAX_ERROR_CHARS = 520
_MAX_REASONING_SUMMARY_PREVIEW_CHARS = 180
_STYLE_EMPHASIS = STYLE_EMPHASIS
_STYLE_CONTENT = STYLE_CONTENT
_STYLE_META = STYLE_DIM
_STYLE_CHROME = STYLE_CHROME
_STYLE_SUBAGENT = STYLE_SUBAGENT
_STYLE_SUCCESS = STYLE_SUCCESS
_STYLE_FAILURE = STYLE_ERROR
_STYLE_WARNING = STYLE_WARN
_THINKING_SPINNER_FRAMES = ("⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏")
_TERMINAL_PROGRESS_MESSAGES = {
    "Applied planner update to the Forge plan.",
    "Planner response ready.",
    "Planner update was a no-op.",
}
_TERMINAL_PROGRESS_PREFIXES = (
    "Planner request recovered after",
    "Planner returned an error",
    "Swarm aborted:",
    "Swarm completed with exit code",
)


def _strip_progress_scope_prefix(message: str) -> str:
    clean = str(message or "").strip()
    if not clean.startswith("["):
        return clean
    closing = clean.find("]")
    if closing <= 1:
        return clean
    return clean[closing + 1 :].strip()


def _redact(text: str) -> str:
    out = text
    for pattern in _SENSITIVE_PATTERNS:
        if pattern.pattern.lower().startswith("(authorization"):
            out = pattern.sub(r"\1[REDACTED]", out)
            continue
        if pattern.pattern.lower().startswith("(bearer"):
            out = pattern.sub(r"\1[REDACTED]", out)
            continue
        out = pattern.sub("[REDACTED]", out)
    return out


def _truncate_inline(text: str, *, max_chars: int = 96) -> str:
    normalized = " ".join(text.split())
    if len(normalized) <= max_chars:
        return normalized
    if max_chars <= 3:
        return normalized[:max_chars]
    return normalized[: max_chars - 3] + "..."


def _split_lines(text: str) -> list[str]:
    lines = str(text).splitlines()
    return lines or [str(text)]


def _looks_like_markdown(text: str) -> bool:
    clean = str(text or "")
    if not clean.strip():
        return False
    if "```" in clean:
        return True
    has_multiple_lines = "\n" in clean
    for line in clean.splitlines():
        stripped = line.lstrip()
        if not stripped:
            continue
        if stripped.startswith(("#", ">", "-", "*")):
            return True
        if re.match(r"\d+\.\s", stripped):
            return True
        if has_multiple_lines and stripped.count("|") >= 2:
            return True
    return False


def _prefixed_text(
    *,
    prefix: str,
    prefix_style: str,
    text: str,
    text_style: str,
) -> Text:
    return Text.assemble((prefix, prefix_style), (text, text_style))


def _bar_prefix(console: Console) -> str:
    return console_glyph(console, "│ ", "| ")


def _subagent_top_prefix(console: Console, *, indent: str) -> str:
    return indent + console_glyph(console, "╭─ ", "+- ")


def _subagent_bottom_prefix(console: Console, *, indent: str) -> str:
    return indent + console_glyph(console, "╰─ ", "+- ")


def _subagent_bar_prefix(console: Console, *, indent: str) -> str:
    return indent + console_glyph(console, "│ ", "| ")


def _separator(console: Console) -> str:
    return console_glyph(console, " · ", " - ")


def _surface_text(console: Console, text: str) -> str:
    if not console_uses_ascii(console):
        return str(text)
    return ascii_sanitize(str(text), stream=getattr(console, "file", None))


def _error_renderable(err: str) -> Group:
    clean = _redact(friendly_llm_error_message(err).strip())
    if not clean:
        clean = "No additional error details."
    if len(clean) > _MAX_ERROR_CHARS:
        clean = clean[: _MAX_ERROR_CHARS - 15].rstrip() + "...(truncated)"

    display = classify_llm_error_display(clean)
    renderables: list[Text] = [
        _prefixed_text(
            prefix="│ ",
            prefix_style="red",
            text=clean,
            text_style="red",
        )
    ]
    renderables.extend(
        _prefixed_text(
            prefix="  ",
            prefix_style=_STYLE_CONTENT,
            text=line,
            text_style=_STYLE_META,
        )
        for line in display.guidance_lines
    )
    return Group(*renderables)


def _warning_renderable(warning: str) -> Group:
    clean = _redact(str(warning).strip())
    if not clean:
        clean = "No additional warning details."
    if len(clean) > _MAX_ERROR_CHARS:
        clean = clean[: _MAX_ERROR_CHARS - 15].rstrip() + "...(truncated)"
    return Group(
        _prefixed_text(
            prefix="│ ",
            prefix_style=_STYLE_WARNING,
            text=f"Warning: {clean}",
            text_style=_STYLE_WARNING,
        )
    )


def _normalize_trace_level(value: str | None, *, fallback: str = "compact") -> str:
    raw = (value or "").strip().lower()
    if raw in _TRACE_LEVELS:
        return raw
    return fallback


def _format_duration_ms(elapsed_ms: int) -> str:
    if elapsed_ms < 1000:
        return f"{elapsed_ms}ms"
    seconds = elapsed_ms / 1000.0
    return f"{seconds:.1f}s"


def _format_step_count(steps_completed: int) -> str:
    step_word = "step" if steps_completed == 1 else "steps"
    return f"{steps_completed} {step_word}"


def _format_turn_elapsed(elapsed_s: float) -> str | None:
    if elapsed_s < 1.0:
        return None
    total_seconds = max(int(elapsed_s), 1)
    minutes, seconds = divmod(total_seconds, 60)
    if minutes <= 0:
        return f"{seconds}s"
    if seconds <= 0:
        return f"{minutes}m"
    return f"{minutes}m {seconds}s"


def _progress_message_completes_activity(message: str) -> bool:
    clean = _strip_progress_scope_prefix(message)
    if clean in _TERMINAL_PROGRESS_MESSAGES:
        return True
    return any(clean.startswith(prefix) for prefix in _TERMINAL_PROGRESS_PREFIXES)


class _ThinkingSpinnerRenderable:
    def __init__(self, surface: RichSurface) -> None:
        self._surface = surface

    def __rich__(self) -> Text:
        return self._surface._make_thinking_renderable()


class RichSurface:
    def __init__(
        self,
        *,
        console: Console | None = None,
        show_status_line: bool = True,
    ) -> None:
        self.console = console or make_console()
        self.renders_error_panel = True
        self._show_status_line = show_status_line
        self._assistant_stream_open = False
        self._allow_for_session: set[str] = set()
        # Folder-wide session grants: (operation, directory) pairs the
        # user granted via "always for this folder" on a delete approval.
        self._session_dir_grants: set[tuple[str, str]] = set()
        self._approval_dir_candidate: str | None = None
        # Cumulative ms blocked on approval prompts (see TuiSurface).
        self.approval_wait_ms_total = 0
        self._thinking_open = False
        self._last_trace_line_key: str = ""
        self._tool_output_summary: dict[str, str] = {}
        self._tool_start_info: dict[str, tuple[int, str]] = {}
        self._trace_level = "compact"
        self._turn_started = False
        self._working_banner_shown = False
        self._turn_start_time: float | None = None
        self._thinking_live: Live | None = None
        self._thinking_start: float = 0.0
        self._spinner_frame: int = 0
        self._spinner_label: str = "Thinking..."
        self._spinner_active = False
        self._stream_buffer: list[str] = []
        self._reasoning_summary_buffer: list[str] = []
        self._reasoning_summary_live: Live | None = None
        self._reasoning_summary_block_id: str | None = None
        self._event_tool_names: dict[str, str] = {}

    @property
    def trace_level(self) -> str:
        return self._trace_level

    @property
    def reasoning_trace_enabled(self) -> bool:
        """Return whether safe provider-generated summaries are visible."""

        return self._trace_level != "off"

    def set_trace_level(self, level: str) -> str:
        self._trace_level = _normalize_trace_level(level, fallback=self._trace_level)
        if self._trace_level == "off":
            self._stop_reasoning_summary_live()
            self._reasoning_summary_buffer.clear()
            self._reasoning_summary_block_id = None
        return self._trace_level

    def _emit_trace_line(
        self,
        message: str,
        *,
        style: str,
        prefix: str,
        prefix_style: str,
        dedupe_key: str | None = None,
    ) -> None:
        self._stop_thinking_spinner()
        self._stop_reasoning_summary_live()
        clean = _redact(message.strip())
        if not clean:
            return
        prefix = _surface_text(self.console, prefix)
        clean = _surface_text(self.console, clean)
        line_key = dedupe_key or f"{prefix}\0{clean}"
        if line_key == self._last_trace_line_key:
            return
        self._last_trace_line_key = line_key
        self._thinking_open = True
        self.console.print(
            Text.assemble((prefix, prefix_style), (clean, style)),
            highlight=False,
        )

    def _emit_thinking(self, message: str, *, style: str = "dim") -> None:
        self._emit_trace_line(
            message,
            style=style,
            prefix=console_glyph(self.console, "• ", "* "),
            prefix_style=_STYLE_CHROME,
        )

    def _reset_thinking(self) -> None:
        self._stop_thinking_spinner()
        self._stop_reasoning_summary_live()
        self._thinking_open = False
        self._last_trace_line_key = ""
        self._stream_buffer.clear()
        self._reasoning_summary_buffer.clear()
        self._reasoning_summary_block_id = None

    def _flush_reasoning_summary(self) -> None:
        self._stop_reasoning_summary_live()
        if not self._reasoning_summary_buffer:
            return
        summary = _redact("".join(self._reasoning_summary_buffer)).strip()
        self._reasoning_summary_buffer.clear()
        self._reasoning_summary_block_id = None
        if self._trace_level == "off" or not summary:
            return
        if self._trace_level == "compact":
            preview = _truncate_inline(
                summary,
                max_chars=_MAX_REASONING_SUMMARY_PREVIEW_CHARS,
            )
            self._emit_thinking(f"Reasoning summary: {preview}", style=_STYLE_META)
            return
        self._emit_thinking("Reasoning summary:", style=_STYLE_CHROME)
        for line in _split_lines(summary):
            self._emit_thinking(line, style=_STYLE_META)

    def _reasoning_summary_renderable(self) -> Text:
        summary = _redact("".join(self._reasoning_summary_buffer)).strip()
        if self._trace_level == "compact":
            summary = _truncate_inline(
                summary,
                max_chars=_MAX_REASONING_SUMMARY_PREVIEW_CHARS,
            )
        label = "Reasoning summary… " if summary else "Reasoning summary…"
        return Text.assemble(
            (console_glyph(self.console, "• ", "* "), _STYLE_CHROME),
            (label, _STYLE_CHROME),
            (_surface_text(self.console, summary), _STYLE_META),
        )

    def _update_reasoning_summary_live(self) -> None:
        if not bool(getattr(self.console, "is_terminal", False)):
            return
        self._stop_thinking_spinner()
        renderable = self._reasoning_summary_renderable()
        if self._reasoning_summary_live is None:
            self._reasoning_summary_live = Live(
                renderable,
                console=self.console,
                auto_refresh=False,
                transient=True,
            )
            self._reasoning_summary_live.start(refresh=True)
            return
        self._reasoning_summary_live.update(renderable, refresh=True)

    def _stop_reasoning_summary_live(self) -> None:
        live = self._reasoning_summary_live
        self._reasoning_summary_live = None
        if live is not None:
            live.stop()

    def _supports_live_thinking_spinner(self) -> bool:
        if console_uses_ascii(self.console):
            return False
        return bool(getattr(self.console, "is_terminal", False))

    def _make_thinking_renderable(self) -> Text:
        if not self._spinner_active:
            return Text("")
        elapsed = max(int(time.monotonic() - self._thinking_start), 0)
        spinner_char = _THINKING_SPINNER_FRAMES[self._spinner_frame % len(_THINKING_SPINNER_FRAMES)]
        spinner_char = console_glyph(self.console, spinner_char, "*")
        self._spinner_frame = (self._spinner_frame + 1) % len(_THINKING_SPINNER_FRAMES)
        style = _STYLE_WARNING if elapsed >= 30 else _STYLE_META
        return Text(f"{spinner_char} {self._spinner_label} {elapsed}s", style=style)

    def _stop_thinking_spinner(self) -> None:
        live = self._thinking_live
        self._spinner_active = False
        self._thinking_live = None
        self._thinking_start = 0.0
        self._spinner_frame = 0
        self._spinner_label = "Thinking..."
        if live is not None:
            live.stop()

    def _start_thinking_spinner(self, *, label: str) -> None:
        if self._assistant_stream_open:
            return
        self._stop_thinking_spinner()
        self._spinner_label = label
        self._spinner_frame = 0
        if not self._supports_live_thinking_spinner():
            if self._trace_level == "off" and label == "Thinking...":
                self._emit_trace_line(
                    label,
                    style=_STYLE_META,
                    prefix=console_glyph(self.console, "• ", "* "),
                    prefix_style=_STYLE_CHROME,
                    dedupe_key=f"thinking-fallback:{label}",
                )
            return
        self._thinking_start = time.monotonic()
        self._spinner_active = True
        self._thinking_live = Live(
            _ThinkingSpinnerRenderable(self),
            console=self.console,
            auto_refresh=True,
            refresh_per_second=4,
            transient=True,
        )
        self._thinking_live.start(refresh=True)

    def _assistant_stream(self) -> object:
        return self.console.file if self.console.file is not None else sys.stdout

    def _count_terminal_lines(self, text: str) -> int:
        width = max(int(getattr(self.console, "width", 0) or 0), 1)
        total = 0
        for logical_line in str(text).split("\n"):
            if not logical_line:
                total += 1
                continue
            total += max(1, math.ceil(len(logical_line) / width))
        return total

    def _should_skip_stream_markdown_rerender(self, text: str) -> bool:
        return not _looks_like_markdown(text)

    def _render_markdown(self, text: str) -> None:
        self.console.print(Markdown(text, code_theme="monokai"))

    def _erase_streamed_output_lines(self, line_count: int) -> None:
        if line_count <= 0:
            return
        stream = self._assistant_stream()
        safe_write(stream, "\r\033[2K")
        for _ in range(max(0, line_count - 1)):
            safe_write(stream, "\033[A\033[2K")
        safe_write(stream, "\r")

    def _subagent_indent(self, *, nesting_depth: int = 1) -> str:
        indent = "  " * max(nesting_depth, 1)
        return indent

    def _emit_subagent_border_top(
        self,
        *,
        subagent_name: str,
        subagent_mode: str,
        nesting_depth: int = 1,
    ) -> None:
        indent = self._subagent_indent(nesting_depth=nesting_depth)
        self._emit_trace_line(
            f"{subagent_name} · {subagent_mode}",
            style=f"bold {_STYLE_SUBAGENT}",
            prefix=_subagent_top_prefix(self.console, indent=indent),
            prefix_style=_STYLE_SUBAGENT,
            dedupe_key=f"subagent-top:{subagent_name}:{subagent_mode}:{nesting_depth}",
        )

    def _emit_subagent_border_bottom(
        self,
        *,
        subagent_name: str,
        status_label: str,
        status_style: str,
        steps_completed: int,
        elapsed_ms: int,
        nesting_depth: int = 1,
    ) -> None:
        self._stop_thinking_spinner()
        indent = self._subagent_indent(nesting_depth=nesting_depth)
        step_text = _format_step_count(steps_completed)
        elapsed = _format_duration_ms(elapsed_ms)
        self._thinking_open = True
        self._last_trace_line_key = f"subagent-bottom:{subagent_name}:{status_label}:{steps_completed}:{elapsed_ms}:{nesting_depth}"
        self.console.print(
            Text.assemble(
                (_subagent_bottom_prefix(self.console, indent=indent), _STYLE_SUBAGENT),
                (status_label, status_style),
                (_separator(self.console), _STYLE_SUBAGENT),
                (step_text, _STYLE_SUBAGENT),
                (_separator(self.console), _STYLE_SUBAGENT),
                (elapsed, _STYLE_SUBAGENT),
            ),
            highlight=False,
        )

    def _emit_subagent_trace(
        self,
        *,
        subagent_name: str,
        message: str,
        style: str,
        nesting_depth: int = 1,
    ) -> None:
        indent = self._subagent_indent(nesting_depth=nesting_depth)
        self._emit_trace_line(
            message,
            style=style,
            prefix=_subagent_bar_prefix(self.console, indent=indent),
            prefix_style=_STYLE_SUBAGENT,
            dedupe_key=f"subagent:{subagent_name}:{nesting_depth}:{message}",
        )

    def _print_left_bar_block(
        self,
        lines: list[str],
        *,
        bar_style: str,
        text_style: str,
    ) -> None:
        self._stop_thinking_spinner()
        for line in lines:
            self.console.print(
                _prefixed_text(
                    prefix=_bar_prefix(self.console),
                    prefix_style=bar_style,
                    text=_surface_text(self.console, line),
                    text_style=text_style,
                ),
                highlight=False,
            )

    def _open_answer_section(self) -> None:
        self._stop_thinking_spinner()
        if self._thinking_open:
            self.console.print("")
        self._reset_thinking()
        elapsed = self._consume_turn_elapsed_label()
        if elapsed is None:
            self.console.rule(style=_STYLE_CHROME)
            return
        self.console.rule(Text(elapsed, style=_STYLE_META), style=_STYLE_CHROME, align="right")

    def _consume_turn_elapsed_label(self) -> str | None:
        start = self._turn_start_time
        self._turn_start_time = None
        if start is None:
            return None
        return _format_turn_elapsed(time.monotonic() - start)

    def on_status_update(self, status: StatusEvent) -> None:
        if not self._show_status_line:
            return
        cwd = Path(status.workspace).name or status.workspace
        parts = [cwd, status.model, status.mode]
        if status.task and status.task != "-":
            parts.append(status.task)
        if status.dirty:
            parts.append("dirty")
        self.console.print(" · ".join(parts), style=_STYLE_META, highlight=False)

    def on_user_message(self, text: str) -> None:
        self._reset_thinking()
        self._turn_started = True
        self._working_banner_shown = False
        self._turn_start_time = time.monotonic()
        clean = _redact(text.strip())
        self._print_left_bar_block(
            _split_lines(clean or "(empty message)"),
            bar_style=_STYLE_EMPHASIS,
            text_style=_STYLE_EMPHASIS,
        )
        self._start_thinking_spinner(label="Thinking...")

    def on_assistant_token(self, delta: str) -> None:
        self._turn_started = False
        self._working_banner_shown = False
        self._flush_reasoning_summary()
        self._stop_thinking_spinner()
        if not self._assistant_stream_open:
            self._open_answer_section()
            self._assistant_stream_open = True
        clean_delta = _redact(delta)
        stream = self._assistant_stream()
        safe_write(stream, clean_delta)
        self._stream_buffer.append(clean_delta)

    def on_reasoning_start(self, block_id: str) -> None:
        """Open a provider-call-scoped reasoning block."""

        if self._trace_level == "off":
            return
        normalized = str(block_id or "").strip()
        if not normalized or normalized == self._reasoning_summary_block_id:
            return
        if self._reasoning_summary_buffer:
            self._flush_reasoning_summary()
        self._reasoning_summary_block_id = normalized

    def on_reasoning_token(self, delta: str) -> None:
        """Buffer a provider-returned reasoning text for display."""

        if self._trace_level == "off" or not delta:
            return
        if self._reasoning_summary_block_id is None:
            self._reasoning_summary_block_id = "legacy"
        self._reasoning_summary_buffer.append(delta)
        # Streaming clients call this incrementally; buffered clients call it
        # only after the provider response completes, so live rendering follows
        # `/stream` timing without a second surface-level mode switch.
        self._update_reasoning_summary_live()

    def on_reasoning_end(self, block_id: str) -> None:
        normalized = str(block_id or "").strip()
        if normalized and normalized != self._reasoning_summary_block_id:
            return
        self._flush_reasoning_summary()

    def on_assistant_message_done(self, text: str) -> None:
        self._turn_started = False
        self._working_banner_shown = False
        self._flush_reasoning_summary()
        self._stop_thinking_spinner()
        clean = _redact(text.strip())
        if self._assistant_stream_open:
            full_text = "".join(self._stream_buffer)
            should_rerender = (
                bool(full_text.strip())
                and not self._should_skip_stream_markdown_rerender(full_text)
                and bool(getattr(self.console, "is_terminal", False))
            )
            if should_rerender:
                self._erase_streamed_output_lines(self._count_terminal_lines(full_text))
                self._render_markdown(full_text)
            else:
                self.console.print("")
            self._stream_buffer.clear()
            self._assistant_stream_open = False
            return
        if not clean:
            self._stream_buffer.clear()
            return
        self._open_answer_section()
        self._render_markdown(clean)
        self._stream_buffer.clear()

    def on_progress_update(self, message: str) -> None:
        if self._trace_level == "off":
            return
        self._emit_thinking(message, style="dim")
        if _progress_message_completes_activity(message):
            self._stop_thinking_spinner()
            return
        if not self._assistant_stream_open and not self._tool_start_info:
            self._start_thinking_spinner(label="Thinking...")

    def on_subagent_start(self, event: SubagentStartEvent) -> None:
        if self._trace_level == "off":
            return
        self._emit_subagent_border_top(
            subagent_name=subagent_identity(event.name, event.label),
            subagent_mode=event.mode,
        )

    def on_subagent_end(self, event: SubagentEndEvent) -> None:
        if self._trace_level == "off":
            return
        status_label = "finished" if event.status == "success" else event.status
        display_name = subagent_identity(event.name, event.label)
        if event.error:
            self._emit_subagent_trace(
                subagent_name=display_name,
                message=f"Error: {_truncate_inline(_redact(event.error), max_chars=140)}",
                style=_STYLE_FAILURE,
            )
        status_style = _STYLE_SUCCESS if event.status == "success" else _STYLE_FAILURE
        self._emit_subagent_border_bottom(
            subagent_name=display_name,
            status_label=status_label,
            status_style=status_style,
            steps_completed=event.steps_completed,
            elapsed_ms=event.elapsed_ms,
        )

    def on_tool_start(self, event: ToolStartEvent) -> None:
        self._flush_reasoning_summary()
        self._stop_thinking_spinner()
        if self._turn_started and not self._working_banner_shown:
            elapsed = self._consume_turn_elapsed_label()
            if elapsed is None:
                self.console.print("Working... Press Esc to interrupt.", style=_STYLE_META)
            else:
                self.console.print(
                    Text.assemble(
                        ("Working... Press Esc to interrupt.", _STYLE_META),
                        ("  ", _STYLE_META),
                        (elapsed, _STYLE_META),
                    ),
                    highlight=False,
                )
            self._working_banner_shown = True
        display = _tool_display_name(event.name)
        self._tool_start_info[event.tool_call_id] = (event.step, display)
        if self._trace_level == "off":
            return
        if event.subagent_name:
            self._emit_subagent_trace(
                subagent_name=event.subagent_name,
                message=f"Step {event.step}: {display}",
                style=_STYLE_CHROME,
                nesting_depth=event.nesting_depth,
            )
        else:
            self._emit_thinking(f"Step {event.step}: {display}", style=_STYLE_CHROME)
        if self._trace_level != "full":
            return
        if event.subagent_name:
            self._emit_subagent_trace(
                subagent_name=event.subagent_name,
                message=f"Input: {_tool_input_preview(event.name, event.args)}",
                style=_STYLE_META,
                nesting_depth=event.nesting_depth,
            )
        else:
            self._emit_thinking(
                f"Input: {_tool_input_preview(event.name, event.args)}",
                style=_STYLE_META,
            )

    def on_tool_output(self, event: ToolOutputEvent) -> None:
        summary = _summarize_tool_output(event.name, event.chunk)
        if summary.strip():
            self._tool_output_summary[event.tool_call_id] = summary

    def on_tool_end(self, event: ToolEndEvent) -> None:
        display = _tool_display_name(event.name)
        elapsed = _format_duration_ms(event.elapsed_ms)
        # The human's approval-wait is disclosed, not billed to the tool.
        approval_wait_ms = int(getattr(event, "approval_wait_ms", 0) or 0)
        if approval_wait_ms > 0:
            elapsed = f"{elapsed} · approval {_format_duration_ms(approval_wait_ms)}"
        meta = event.meta if isinstance(event.meta, dict) else {}
        detail = ""
        err = meta.get("error")
        if err:
            err_preview = _truncate_inline(_redact(str(err)), max_chars=140)
            detail = f": {err_preview}"
        self._tool_start_info.pop(event.tool_call_id, None)
        summary = self._tool_output_summary.pop(event.tool_call_id, "").strip()
        # A withdrawn optional tool reports "done" to the model but never ran.
        unavailable = event.status == "done" and bool(meta.get("tool_unavailable"))
        # A page the remote site refused, stalled, or dropped is that source's
        # outcome, not an agent failure: rendered like any other trace line.
        site_reason = remote_site_outcome(meta) if event.status != "done" else ""
        message = ""
        style = _STYLE_CHROME
        if unavailable:
            message = f"{display} unavailable ({elapsed})"
            cause = str(meta.get("unavailable_reason") or "")
            if cause:
                message += f": {_truncate_inline(_redact(cause), max_chars=140)}"
            style = _STYLE_WARNING
        elif event.status == "done":
            if self._trace_level != "off":
                outcome = f"{display}: {summary}" if summary else display
                message = f"{outcome} ({elapsed})"
        elif meta.get("approval_declined"):
            message = f"{display} approval declined ({elapsed}){detail}"
            style = "red"
        elif site_reason:
            if self._trace_level != "off":
                message = f"{display} · {site_reason} ({elapsed})"
        else:
            message = f"{display} failed ({elapsed}){detail}"
            style = "red"
        if message:
            if event.subagent_name:
                self._emit_subagent_trace(
                    subagent_name=event.subagent_name,
                    message=message,
                    style=style,
                    nesting_depth=event.nesting_depth,
                )
            else:
                self._emit_thinking(message, style=style)
        if not self._assistant_stream_open and not self._tool_start_info:
            self._start_thinking_spinner(label="Reasoning...")

    def on_patch_generated(self, event: PatchEvent) -> None:
        self._flush_reasoning_summary()
        self._stop_thinking_spinner()
        self.console.print(
            _prefixed_text(
                prefix=_bar_prefix(self.console),
                prefix_style=_STYLE_CHROME,
                text=", ".join(event.files[:5]) or "Patch Preview",
                text_style=_STYLE_EMPHASIS,
            ),
            highlight=False,
        )
        if event.summary:
            self.console.print(
                _prefixed_text(
                    prefix=_bar_prefix(self.console),
                    prefix_style=_STYLE_CHROME,
                    text=event.summary,
                    text_style=_STYLE_META,
                ),
                highlight=False,
            )
        preview = _redact(event.diff)
        if len(preview) > 1200:
            preview = preview[:1200] + "\n...(truncated)"
        lines = _split_lines(preview or "(empty diff)")
        for line in lines:
            style = _STYLE_META
            if line.startswith(("diff --git", "--- ", "+++ ", "@@")):
                style = _STYLE_EMPHASIS
            elif line.startswith("+"):
                style = _STYLE_EMPHASIS
            elif line.startswith("-"):
                style = _STYLE_CHROME
            self.console.print(
                _prefixed_text(
                    prefix=_bar_prefix(self.console),
                    prefix_style=_STYLE_CHROME,
                    text=line,
                    text_style=style,
                ),
                highlight=False,
            )

    def on_error(self, err: str) -> None:
        self._flush_reasoning_summary()
        self._stop_thinking_spinner()
        if self._assistant_stream_open:
            self.console.print("")
            self._assistant_stream_open = False
        self._turn_started = False
        self._working_banner_shown = False
        self._reset_thinking()
        try:
            self.console.print(_error_renderable(err))
        except Exception as exc:  # noqa: BLE001 - final output fallback must not double-crash.
            safe_plain_error(
                stream=getattr(self.console, "file", None),
                error_type=type(exc).__name__,
                message=err,
            )

    def on_warning(self, warning: str) -> None:
        self._flush_reasoning_summary()
        self._stop_thinking_spinner()
        if self._assistant_stream_open:
            self.console.print("")
            self._assistant_stream_open = False
        try:
            self.console.print(_warning_renderable(warning))
        except Exception:
            safe_plain_error(
                stream=getattr(self.console, "file", None),
                error_type="Warning",
                message=warning,
            )

    def _emit_event_info(self, message: str) -> None:
        if self._trace_level == "off":
            return
        self._emit_thinking(message, style=_STYLE_META)

    def _event_scope_prefix(
        self,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> str:
        parts = [part for part in (worker_id, role) if part]
        if not parts:
            return ""
        return "[" + " · ".join(parts) + "] "

    # RichSurface renders these through legacy on_* methods; emit_* are no-ops here so
    # dual emission from agent_loop does not double-render in the Rich CLI.
    def emit_message_delta(
        self,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (text, worker_id, role)

    def emit_message_end(
        self,
        text: str = "",
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (text, worker_id, role)

    def emit_tool_call_started(
        self,
        call_id: str,
        name: str,
        arguments_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (call_id, name, arguments_preview, worker_id, role)

    def emit_tool_call_progress(
        self,
        call_id: str,
        text: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (call_id, text, worker_id, role)

    def emit_tool_call_completed(
        self,
        call_id: str,
        success: bool,
        result_preview: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        _ = (call_id, success, result_preview, worker_id, role)

    def emit_status_update(
        self,
        *,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        cached_tokens: int | None = None,
        cost_usd: float | None = None,
        mode: str | None = None,
        model: str | None = None,
        step: int | None = None,
        step_budget: int | None = None,
    ) -> None:
        if self._trace_level == "off":
            return
        parts: list[str] = []
        if model:
            parts.append(f"model={model}")
        if mode:
            parts.append(f"mode={mode}")
        if step is not None:
            step_text = f"step={step}"
            if step_budget is not None:
                step_text += f"/{step_budget}"
            parts.append(step_text)
        if tokens_in is not None:
            parts.append(f"in={tokens_in}")
        if tokens_out is not None:
            parts.append(f"out={tokens_out}")
        if cached_tokens is not None:
            parts.append(f"cached={cached_tokens}")
        if cost_usd is not None:
            parts.append(f"cost=${cost_usd:.6f}")
        self._emit_trace_line(
            " · ".join(parts) if parts else "updated",
            style=_STYLE_META,
            prefix="Status: ",
            prefix_style=_STYLE_META,
        )

    def emit_mode_changed(self, mode: str) -> None:
        if self._trace_level == "off":
            return
        self._emit_trace_line(
            mode,
            style=_STYLE_META,
            prefix="Permissions: ",
            prefix_style=_STYLE_META,
        )

    def reset_streamed_assistant(self) -> None:
        # Classic terminal output cannot be unprinted; mark the restart so the
        # restreamed reply that follows is readable as a retry, not an echo.
        self._emit_event_info("Provider retry — restarting the reply…")

    def emit_persona_changed(self, persona: str, effective_mode: str, source: str = "user") -> None:
        self._emit_event_info(f"Persona: {persona} ({effective_mode}, {source})")

    def emit_plan_node_updated(
        self,
        node_id: str,
        state: str,
        summary: str | None = None,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        # TODO: dedicated rendering for plan_node_updated.
        prefix = self._event_scope_prefix(worker_id=worker_id, role=role)
        detail = f" · {summary}" if summary else ""
        self._emit_event_info(f"{prefix}Plan {node_id}: {state}{detail}")

    def emit_swarm_worker_state_changed(
        self,
        worker_id: str,
        state: str,
        *,
        role: str | None = None,
    ) -> None:
        # TODO: dedicated rendering for swarm_worker_state_changed.
        suffix = f" · {role}" if role else ""
        self._emit_event_info(f"Worker {worker_id}: {state}{suffix}")

    def emit_verify_gate_result(
        self,
        command: str,
        success: bool,
        summary: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        # TODO: dedicated rendering for verify_gate_result.
        prefix = self._event_scope_prefix(worker_id=worker_id, role=role)
        state = "passed" if success else "failed"
        self._emit_event_info(f"{prefix}Verify {state}: {command} · {summary}")

    def emit_approval_reviewed(
        self,
        outcome: str,
        subject: str,
        reason: str = "",
        *,
        risk: str = "",
        approval_kind: str = "",
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        prefix = self._event_scope_prefix(worker_id=worker_id, role=role)
        self._emit_event_info(f"{prefix}{approval_review_line(outcome, subject, reason)}")

    def emit_review_gate_decision(
        self,
        decision: str,
        summary: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        # TODO: dedicated rendering for review_gate_decision.
        prefix = self._event_scope_prefix(worker_id=worker_id, role=role)
        self._emit_event_info(f"{prefix}Review {decision}: {summary}")

    def emit_error(
        self,
        code: str,
        message: str,
        recoverable: bool,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        prefix = self._event_scope_prefix(worker_id=worker_id, role=role)
        code_text = f"{code}: " if code else ""
        recovery_text = "" if recoverable else " (not recoverable)"
        self.on_error(f"{prefix}{code_text}{message}{recovery_text}")

    def emit_warning(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        prefix = self._event_scope_prefix(worker_id=worker_id, role=role)
        self.on_warning(f"{prefix}{message}")

    def emit_info(
        self,
        message: str,
        *,
        worker_id: str | None = None,
        role: str | None = None,
    ) -> None:
        prefix = self._event_scope_prefix(worker_id=worker_id, role=role)
        self.on_progress_update(f"{prefix}{message}")

    def emit_prompt_for_input(self, prompt_id: str, prompt_text: str, kind: str) -> None:
        # TODO: dedicated rendering for prompt_for_input.
        self._emit_event_info(f"Prompt {prompt_id} ({kind}): {prompt_text}")

    def emit_config_form_request(self, form_id: str, schema: dict[str, Any]) -> None:
        # TODO: dedicated rendering for config_form_request.
        title = str(schema.get("title") or schema.get("name") or "configuration")
        self._emit_event_info(f"Config form {form_id}: {title}")

    def emit(self, event: Event) -> None:
        if event.type == MessageDelta.type:
            self.emit_message_delta(event.text, worker_id=event.worker_id, role=event.role)
        elif event.type == MessageEnd.type:
            self.emit_message_end(event.text, worker_id=event.worker_id, role=event.role)
        elif event.type == ToolCallStarted.type:
            self.emit_tool_call_started(
                event.call_id,
                event.name,
                event.arguments_preview,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ToolCallProgress.type:
            self.emit_tool_call_progress(
                event.call_id,
                event.text,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ToolCallCompleted.type:
            self.emit_tool_call_completed(
                event.call_id,
                event.success,
                event.result_preview,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == StatusUpdate.type:
            self.emit_status_update(
                tokens_in=event.tokens_in,
                tokens_out=event.tokens_out,
                cached_tokens=event.cached_tokens,
                cost_usd=event.cost_usd,
                mode=event.mode,
                model=event.model,
                step=event.step,
                step_budget=event.step_budget,
            )
        elif event.type == ModeChanged.type:
            self.emit_mode_changed(event.mode)
        elif event.type == PersonaChanged.type:
            self.emit_persona_changed(event.persona, event.effective_mode, event.source)
        elif event.type == PlanNodeUpdated.type:
            self.emit_plan_node_updated(
                event.node_id,
                event.state,
                event.summary,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == SwarmWorkerStateChanged.type:
            self.emit_swarm_worker_state_changed(event.worker_id, event.state, role=event.role)
        elif event.type == VerifyGateResult.type:
            self.emit_verify_gate_result(
                event.command,
                event.success,
                event.summary,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ApprovalReviewed.type:
            self.emit_approval_reviewed(
                event.outcome,
                event.subject,
                event.reason,
                risk=event.risk,
                approval_kind=event.approval_kind,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ReviewGateDecision.type:
            self.emit_review_gate_decision(
                event.decision,
                event.summary,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == ErrorRaised.type:
            self.emit_error(
                event.code,
                event.message,
                event.recoverable,
                worker_id=event.worker_id,
                role=event.role,
            )
        elif event.type == WarningEmitted.type:
            self.emit_warning(event.message, worker_id=event.worker_id, role=event.role)
        elif event.type == InfoEmitted.type:
            self.emit_info(event.message, worker_id=event.worker_id, role=event.role)
        elif event.type == PromptForInput.type:
            self.emit_prompt_for_input(event.prompt_id, event.prompt_text, event.kind)
        elif event.type == ConfigFormRequest.type:
            self.emit_config_form_request(event.form_id, event.schema)

    def _prompt_approval_choice_fallback(self) -> str:
        try:
            return (
                Prompt.ask(
                    "Select option",
                    console=self.console,
                    default="n",
                    show_default=False,
                    show_choices=False,
                )
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            return "n"

    def _uses_inline_approval_selector(self) -> bool:
        if not getattr(self.console, "is_terminal", False):
            return False
        if os.name != "posix":
            return False
        try:
            fd = os.open("/dev/tty", os.O_RDWR)
        except Exception:
            return False
        try:
            return True
        finally:
            os.close(fd)

    def _approval_selector_options(self) -> list[tuple[str, str]]:
        options = [
            ("y", "Allow"),
            ("a", "Always allow for session"),
        ]
        if self._approval_dir_candidate is not None:
            options.append(("d", f"Always for this folder ({self._approval_dir_candidate})"))
        options.extend(
            [
                ("n", "Deny"),
                ("v", "View context"),
            ]
        )
        return options

    def _approval_confirmation(self, choice: str) -> tuple[str, str] | None:
        mapping = {
            "y": (" Allowed", _STYLE_SUCCESS),
            "a": (" Always allowed", _STYLE_SUCCESS),
            "d": (" Always allowed for this folder", _STYLE_SUCCESS),
            "n": (" Denied", _STYLE_WARNING),
        }
        return mapping.get(choice)

    def _render_approval_selector_menu(self, *, selected_index: int) -> None:
        options = self._approval_selector_options()
        for index, (_key, label) in enumerate(options):
            selected = index == selected_index
            prefix = "  > " if selected else "    "
            prefix_style = "bold green" if selected else _STYLE_META
            text_style = _STYLE_EMPHASIS if selected else _STYLE_META
            self.console.print(
                Text.assemble((prefix, prefix_style), (label, text_style)),
                highlight=False,
            )

    def _erase_approval_selector_menu(self) -> None:
        stream = self.console.file if self.console.file is not None else sys.stdout
        for _ in range(len(self._approval_selector_options())):
            safe_write(stream, "\033[A\033[2K")
        safe_write(stream, "\r")

    def _read_approval_selector_keypress(self) -> str:
        import select
        import termios
        import tty

        fd = os.open("/dev/tty", os.O_RDWR)
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setraw(fd)
            first = os.read(fd, 1)
            if not first:
                raise EOFError
            ch = first.decode("utf-8", errors="ignore")
            if ch == "\x1b":
                if select.select([fd], [], [], 0.05)[0]:
                    second = os.read(fd, 1)
                    if not second:
                        return "\x1b"
                    ch2 = second.decode("utf-8", errors="ignore")
                    if ch2 == "[":
                        if not select.select([fd], [], [], 0.05)[0]:
                            return "\x1b"
                        third = os.read(fd, 1)
                        if not third:
                            return "\x1b"
                        ch3 = third.decode("utf-8", errors="ignore")
                        return f"\x1b[{ch3}"
                    if ch2 == "O":
                        if not select.select([fd], [], [], 0.05)[0]:
                            return "\x1b"
                        third = os.read(fd, 1)
                        if not third:
                            return "\x1b"
                        ch3 = third.decode("utf-8", errors="ignore")
                        return f"\x1bO{ch3}"
                return "\x1b"
            return ch
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            finally:
                os.close(fd)

    def _prompt_approval_selector(self) -> str:
        options = self._approval_selector_options()
        selected_index = 0
        self._render_approval_selector_menu(selected_index=selected_index)
        menu_visible = True
        try:
            while True:
                try:
                    key = self._read_approval_selector_keypress()
                except (EOFError, KeyboardInterrupt):
                    key = "n"
                except Exception:
                    if menu_visible:
                        self._erase_approval_selector_menu()
                        menu_visible = False
                    return self._prompt_approval_choice_fallback()

                lowered = key.lower() if key else ""
                if key in {"\r", "\n"}:
                    choice = options[selected_index][0]
                    break
                if lowered in {option_key for option_key, _label in options}:
                    choice = lowered
                    break
                if lowered == "k" or key == "\x1b[A":
                    selected_index = (selected_index - 1) % len(options)
                elif lowered == "j" or key == "\x1b[B":
                    selected_index = (selected_index + 1) % len(options)
                elif key in {"\x1b[H", "\x1bOH"}:
                    selected_index = 0
                elif key in {"\x1b[F", "\x1bOF"}:
                    selected_index = len(options) - 1
                elif key in {"\x03", "\x04", "\x1b"}:
                    choice = "n"
                    break
                else:
                    continue
                self._erase_approval_selector_menu()
                menu_visible = False
                self._render_approval_selector_menu(selected_index=selected_index)
                menu_visible = True
        finally:
            if menu_visible:
                self._erase_approval_selector_menu()

        confirmation = self._approval_confirmation(choice)
        if confirmation is not None:
            message, style = confirmation
            self.console.print(message, style=style, highlight=False)
        return choice

    def _prompt_approval_choice(self) -> str:
        # The human's decision time must not be billed to the tool.
        wait_started = time.perf_counter()
        try:
            if self._uses_inline_approval_selector():
                with interactive_prompt_guard(owns_terminal=True):
                    return self._prompt_approval_selector()
            with interactive_prompt_guard():
                return self._prompt_approval_choice_fallback()
        finally:
            self.approval_wait_ms_total += int((time.perf_counter() - wait_started) * 1000)

    def standing_approval(self, request: ApprovalRequest) -> StandingApproval | None:
        """An earlier "always" answer already covers ``request``."""

        scope_info = approval_session_scope_for_request(request)
        if scope_info.key is not None and scope_info.key in self._allow_for_session:
            return StandingApproval("allow", "you allowed this earlier in this session")
        if request_matches_dir_grants(request, self._session_dir_grants):
            return StandingApproval("allow", "you allowed this folder earlier in this session")
        return None

    def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self._stop_thinking_spinner()
        scope_info = approval_session_scope_for_request(request)
        if scope_info.key is not None and scope_info.key in self._allow_for_session:
            return ApprovalDecision(allow=True, allow_for_session=True)
        if request_matches_dir_grants(request, self._session_dir_grants):
            return ApprovalDecision(allow=True, allow_for_session=True)
        self._approval_dir_candidate = approval_dir_grant_candidate(request)

        files = ", ".join(request.files[:8]) if request.files else "-"
        command = request.command or "-"
        preview = _redact(request.preview or "")
        preview_available = bool(preview.strip())
        lowered_command = command.lower()
        destructive = request.kind == "fs_delete" or any(
            token in lowered_command for token in (" rm ", "rm -", "force", "delete")
        )
        bar_style = "red" if destructive else _STYLE_EMPHASIS
        title_style = "red" if destructive else _STYLE_EMPHASIS
        target = command if request.command else files
        if request.kind.startswith("custom_tool_run:"):
            action_label = "Run custom tool"
        else:
            action_label = {
                "shell_run": "Run command",
                "fs_write": "Write files",
                "fs_delete": "Delete files",
            }.get(request.kind, f"Approve {request.kind}")
        headline = action_label
        if request.reason:
            headline += f"{_separator(self.console)}{request.reason}"

        self.console.print(
            _prefixed_text(
                prefix=_bar_prefix(self.console),
                prefix_style=bar_style,
                text=headline,
                text_style=title_style,
            ),
            highlight=False,
        )
        self.console.print(
            _prefixed_text(
                prefix=_bar_prefix(self.console),
                prefix_style=bar_style,
                text=target,
                text_style=_STYLE_CONTENT,
            ),
            highlight=False,
        )
        if request.command and request.files:
            self.console.print(
                _prefixed_text(
                    prefix="  ",
                    prefix_style=_STYLE_CONTENT,
                    text=f"Files: {files}",
                    text_style=_STYLE_META,
                ),
                highlight=False,
            )
        context_line = (
            f"Context available ({len(preview)} chars) · [v] view"
            if preview_available
            else "Context none"
        )
        if preview_available:
            self.console.print(
                Text.assemble(
                    ("  ", _STYLE_CONTENT),
                    ("Context available ", _STYLE_META),
                    ("[v]", _STYLE_EMPHASIS),
                    (" to view", _STYLE_META),
                ),
                highlight=False,
            )
        else:
            self.console.print(
                _prefixed_text(
                    prefix="  ",
                    prefix_style=_STYLE_CONTENT,
                    text=context_line,
                    text_style=_STYLE_META,
                ),
                highlight=False,
            )
        if not self._uses_inline_approval_selector():
            options = [
                ("  ", _STYLE_CONTENT),
                ("[y]", _STYLE_EMPHASIS),
                (" allow  ", _STYLE_META),
            ]
            if scope_info.supported:
                options.extend(
                    [
                        ("[a]", _STYLE_EMPHASIS),
                        (" always  ", _STYLE_META),
                    ]
                )
            if self._approval_dir_candidate is not None:
                options.extend(
                    [
                        ("[d]", _STYLE_EMPHASIS),
                        (f" folder ({self._approval_dir_candidate})  ", _STYLE_META),
                    ]
                )
            options.extend(
                [
                    ("[n]", _STYLE_EMPHASIS),
                    (" deny  ", _STYLE_META),
                    ("[v]", _STYLE_EMPHASIS),
                    (" view", _STYLE_META),
                ]
            )
            self.console.print(Text.assemble(*options), highlight=False)
        elif scope_info.warning:
            self.console.print(
                _prefixed_text(
                    prefix="  ",
                    prefix_style=_STYLE_CONTENT,
                    text=scope_info.warning,
                    text_style=_STYLE_META,
                ),
                highlight=False,
            )
        while True:
            choice = self._prompt_approval_choice()
            if choice in {"1", "y"}:
                return ApprovalDecision(allow=True)
            if choice == "d":
                directory = self._approval_dir_candidate
                if directory is not None:
                    self._session_dir_grants.add((str(request.kind), directory))
                    return ApprovalDecision(allow=True, allow_for_session=True)
                return ApprovalDecision(allow=True)
            if choice in {"2", "a"}:
                if scope_info.supported and scope_info.key is not None:
                    self._allow_for_session.add(scope_info.key)
                    return ApprovalDecision(allow=True, allow_for_session=True)
                self.console.print(
                    _prefixed_text(
                        prefix="  ",
                        prefix_style=_STYLE_CONTENT,
                        text=scope_info.warning
                        or "Allow for session is unavailable for this approval.",
                        text_style=_STYLE_META,
                    ),
                    highlight=False,
                )
                return ApprovalDecision(allow=True)
            if choice in {"3", "n"}:
                return ApprovalDecision(allow=False)
            if choice in {"4", "v"}:
                self.console.print(
                    _prefixed_text(
                        prefix=_bar_prefix(self.console),
                        prefix_style=_STYLE_CHROME,
                        text="Context",
                        text_style=_STYLE_EMPHASIS,
                    ),
                    highlight=False,
                )
                self._print_left_bar_block(
                    _split_lines(_redact(request.preview or "") or "(no preview)"),
                    bar_style=_STYLE_CHROME,
                    text_style=_STYLE_CONTENT,
                )
                continue
            self.console.print("Enter y, a, n, or v.", style="red", highlight=False)
