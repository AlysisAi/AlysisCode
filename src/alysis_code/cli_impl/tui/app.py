"""The full-screen TUI shell + agent host.

A prompt_toolkit ``Application`` (alt-screen) that reproduces the launch
screenshot: a centered white owl animation, the "What can I do for you?"
heading, a dim hint line, a larger bordered multi-line input box, and a pinned
2-line footer (brand · model · context/tokens/cost; persona · execution mode ·
user · workspace · branch). Enter submits, Ctrl+J inserts a newline,
Tab on an empty input cycles the persona, and Shift+Tab selects the execution
mode (read → safe → fast → full) for the next message via the
``mode_cycle`` callback. The active mode remains the sole approval authority
until that next-message boundary.

Phase 2 wires the agent: when a ``session_builder`` is supplied, the welcome body
swaps for a scrollable transcript and each submission runs ``session.run_turn``
on a worker thread. A :class:`TuiSurface` streams the agent's tokens, tool trace
and errors into the transcript; Ctrl+C interrupts the running turn (and exits
when idle). Without a ``session_builder`` the shell keeps the Phase 1 stub reply.
"""

from __future__ import annotations

import asyncio
import os
import re
import textwrap
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.cursor_shapes import CursorShape
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.formatted_text import (
    FormattedText,
    StyleAndTextTuples,
    fragment_list_to_text,
    split_lines,
    to_formatted_text,
)
from prompt_toolkit.input import create_input
from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import Layout, WindowAlign
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    ScrollOffsets,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.processors import Processor, Transformation
from prompt_toolkit.layout.utils import explode_text_fragments
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.styles import Style, merge_styles
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Frame, TextArea

from ...agent.steering import (
    MAX_PENDING_STEER_MESSAGES,
    ResolvedOperation,
    ops_inbox_for,
    steer_inbox_for,
)
from ...branding import env_get
from ...cancellation import InteractiveCancellationToken
from ...clipboard import ClipboardError, copy_text_to_clipboard, paste_clipboard_image
from ...host_browser import open_url
from ...llm.types import LLMError
from ...llm_error_display import friendly_llm_error_message, is_network_or_model_error
from ...review_service import ReviewController
from ...subagent_labels import subagent_identity
from ...surface.styles import TerminalTheme
from ...surface.theme import detect_terminal_theme
from ...surface.types import ApprovalDecision, SubagentStartEvent
from ..chat.commands import _record_chat_local_command
from ..chat.forge_visibility import command_visible
from ..chat.mid_turn_policy import (
    EXIT_WORDS,
    MidTurnAction,
    block_message,
    classify_mid_turn,
    defer_message,
    deferred_display_label,
    is_command,
)
from . import content as _content
from .actions import MODEL_DESCRIPTION
from .attachments import (
    ATTACHMENT_TOKEN_RE,
    Attachment,
    AttachmentError,
    AttachmentRegistry,
    detect_dropped_paths,
    trailing_drop_run,
)
from .attachments import token_after as _attachment_token_after
from .attachments import token_at as _attachment_token_at
from .attachments import token_before as _attachment_token_before
from .composer import COMPOSER_PROMPT, ComposerWrapProcessor, vertical_move
from .diff_view import DiffViewer
from .footer import footer_fragments
from .forge_status import (
    forge_status_bucket,
    forge_status_counts,
    forge_status_glyph,
)
from .integration_modal import IntegrationModal
from .links import (
    browser_url,
    link_at,
    link_fragments,
    link_segments,
    pad_after_trailing_link,
)
from .local_action_lifecycle import TuiLocalActionLifecycle, TurnOutcome
from .local_mode_host import ReservedIntegrationDispatcher, integration_controller
from .markdown import render_markdown_rows
from .navigation_ui import SessionSearchModal, TranscriptSearchModal
from .owl import load_owl_animation
from .plan_meta import (
    PLAN_META_ROW_ROLES,
    plan_meta_rows_with_kinds,
    strip_plan_meta_copy_chrome,
)
from .plan_meta import plan_meta_rows as render_plan_meta_rows
from .planning_chooser import PlanningController
from .prompt_history import PromptEntry, PromptHistory
from .review import ReviewModal
from .review_client import dispose_review_engine
from .selection_bindings import selection_editing_bindings
from .state import TuiState
from .subagent_panel import (
    append_bounded_entries,
    quiet_time_label,
    subagent_panel_rows,
)
from .surface import TuiSurface, set_active_cancellation
from .tips import working_tip
from .transcript import TuiTranscript

_EXIT_WORDS = EXIT_WORDS
_SPINNER_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_MAX_PENDING_COMMANDS = MAX_PENDING_STEER_MESSAGES
_INLINE_PASTE_MAX_LINES = 8
_INLINE_PASTE_MAX_CHARS = 800
_PASTE_TOKEN_RE = re.compile(r"\[pasted #(?P<id>[1-9]\d*) \+(?P<lines>\d+) lines\]")
_PENDING_STEER_HEADER = (
    "message to be submitted after the next tool call - ctrl+c interrupts and sends it immediately"
)
_PENDING_STEERS_HEADER = "messages to be submitted after the next tool call - ctrl+c interrupts and sends them immediately"
_PENDING_QUEUED_HEADER = "queued follow-up inputs"
_PENDING_EDIT_HINT = "shift + ← edit last queued message"


def _record_tui_local_interaction(*, session: Any, action: str) -> None:
    """Persist a value-free marker for an accepted TUI keyboard shortcut."""

    store = getattr(session, "store", None)
    append = getattr(store, "append", None)
    if callable(append):
        append("chat_local_interaction", {"action": action})


def _normalize_paste_text(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _paste_line_count(text: str) -> int:
    return max(1, len(text.splitlines()))


def _paste_token(paste_id: int, payload: str) -> str:
    return f"[pasted #{paste_id} +{_paste_line_count(payload)} lines]"


def _paste_token_at_cursor(text: str, cursor_position: int) -> re.Match[str] | None:
    for match in _PASTE_TOKEN_RE.finditer(text):
        if match.start() <= cursor_position <= match.end():
            return match
    return None


def _paste_token_before_cursor(text: str, cursor_position: int) -> re.Match[str] | None:
    for match in _PASTE_TOKEN_RE.finditer(text):
        if match.end() == cursor_position:
            return match
    return None


def _paste_token_after_cursor(text: str, cursor_position: int) -> re.Match[str] | None:
    for match in _PASTE_TOKEN_RE.finditer(text):
        if match.start() == cursor_position:
            return match
    return None


class _PasteRegistry:
    def __init__(self) -> None:
        self._payloads: dict[int, str] = {}
        self._next_id = 1

    def add(self, payload: str) -> int:
        paste_id = self._next_id
        self._next_id += 1
        self._payloads[paste_id] = payload
        return paste_id

    def get(self, paste_id: int) -> str | None:
        return self._payloads.get(paste_id)

    def update(self, paste_id: int, payload: str) -> bool:
        if paste_id not in self._payloads:
            return False
        self._payloads[paste_id] = payload
        return True

    def snapshot(self) -> dict[int, str]:
        return dict(self._payloads)

    def restore(self, payloads: dict[int, str]) -> None:
        self._payloads.update(payloads)
        self._next_id = max(self._next_id, max(payloads, default=0) + 1)

    def live_matches(self, text: str) -> list[re.Match[str]]:
        return [
            match
            for match in _PASTE_TOKEN_RE.finditer(text)
            if int(match.group("id")) in self._payloads
        ]

    def retain_tokens(self, text: str) -> None:
        live_ids = {int(match.group("id")) for match in _PASTE_TOKEN_RE.finditer(text)}
        for paste_id in tuple(self._payloads):
            if paste_id not in live_ids:
                self._payloads.pop(paste_id, None)

    def expand(self, text: str) -> str:
        self.retain_tokens(text)

        def replace(match: re.Match[str]) -> str:
            return self._payloads.get(int(match.group("id")), match.group(0))

        return _PASTE_TOKEN_RE.sub(replace, text)


class ConfigReloadOutcome(Enum):
    """Result of applying a saved configuration to the live TUI session."""

    APPLIED = "applied"
    FAILED = "failed"
    RESTART = "restart"


class _DeferredOperationKind(Enum):
    COMMAND = "command"
    CONFIG_RELOAD = "config_reload"


@dataclass(frozen=True)
class _DeferredOperation:
    kind: _DeferredOperationKind
    text: str = ""


@dataclass(frozen=True)
class _PendingMessage:
    text: str
    display_text: str
    # A steer restored after an LLM rollback may already be visible in the
    # transcript.  When it is rescued as the next queued turn, keep that
    # original echo instead of rendering the same user message a second time.
    echoed: bool = False
    # Chips that were in the composer when this message was queued. They ride
    # with it when it is drained into its own turn, and are re-adopted by the
    # registry when the message is recalled back into the input box.
    attachments: tuple[Attachment, ...] = ()
    pastes: tuple[tuple[int, str], ...] = ()


class _DispatchOutcome(Enum):
    CONTINUE = "continue"
    EXIT = "exit"


def _pending_command_count(pending: list[_DeferredOperation]) -> int:
    return sum(operation.kind is _DeferredOperationKind.COMMAND for operation in pending)


def _stage_pending_command(pending: list[_DeferredOperation], text: str) -> bool:
    """Append a command without losing chronology or independent mutations."""
    raw = str(text or "")
    stripped = raw.strip()
    if not stripped:
        return False
    if _pending_command_count(pending) >= _MAX_PENDING_COMMANDS:
        return False
    pending.append(_DeferredOperation(_DeferredOperationKind.COMMAND, raw))
    return True


def _stage_pending_config_reload(pending: list[_DeferredOperation]) -> None:
    """Stage a reload at its chronological position, coalescing adjacent saves."""
    if pending and pending[-1].kind is _DeferredOperationKind.CONFIG_RELOAD:
        return
    pending.append(_DeferredOperation(_DeferredOperationKind.CONFIG_RELOAD))


def _model_access_setup_hint(subscription_provider_id: str | None) -> str:
    if subscription_provider_id:
        return "Set up model access: /login to choose a connection · /config for an API key"
    return "Set up model access in /config"


class _SizedRowsControl(UIControl):
    """Non-focusable rows built for the exact size they are drawn at.

    ``render(width, height)`` returns the rows; ``height`` is ``None`` while the
    layout is only measuring. A FormattedTextControl builds its text once per
    render pass, before it knows its height, so content too tall for a small
    terminal could only be clipped. This lets the welcome landing leave the owl
    out instead.
    """

    def __init__(self, render: Callable[[int, int | None], list[StyleAndTextTuples]]) -> None:
        self._render = render

    def preferred_height(
        self,
        width: int,
        max_available_height: int,
        wrap_lines: bool,
        get_line_prefix: Any,
    ) -> int | None:
        return len(self._render(width, None))

    def create_content(self, width: int, height: int) -> UIContent:
        rows = self._render(width, height)
        return UIContent(get_line=lambda i: rows[i], line_count=len(rows), show_cursor=False)


# Single accent colour for the input frame, the "> " prompt, the user band and
# the assistant marker — a green shade. ``_BAND_BG`` is the subtle full-width
# highlight behind the user's own message.
_ACCENT = "#3fb950"
_BAND_BG = "#21262d"
# Marker drawn in front of every assistant reply so it reads as the agent's.
_ASSIST_MARK = "✦"
# The model's "thinking" (reasoning) aside, tucked just under the question:
# a chevron header (``▾`` open / ``▸`` collapsed) above a dim left rail ``│``.
_THINK_OPEN = "▾"
_THINK_CLOSED = "▸"
_THINK_RAIL = "│"

# Explicit transcript copies are semantic, not a dump of rendered terminal
# cells. Provider reasoning summaries and agent/tool chrome stay visible in the
# pane but never enter the clipboard; unknown future roles fail closed until
# they are intentionally classified here.
_COPYABLE_TRANSCRIPT_ROLES = frozenset(
    {"user", "assistant", "error", "warn", "info", "system", "spacer", "planmeta", "planmetacont"}
)

_COMPLETION_MENU_BOTTOM = 8
_COMPLETION_MENU_MAX_HEIGHT = 8
_COMPLETION_MENU_MAX_WIDTH = 84
_SUBAGENT_PANEL_HEIGHT = 10
_SUBAGENT_PANEL_CONTENT_HEIGHT = _SUBAGENT_PANEL_HEIGHT - 2

_STYLE = Style.from_dict(
    {
        "tui.heading": "bold",
        "tui.credit": "#7a7a7a",
        "tui.hint": "italic #8a8a8a",
        "tui.placeholder": "#6c6c6c",
        "tui.input": "",
        "tui.prompt": f"bold {_ACCENT}",
        # Attachment chips ("[Image #1]") in the composer: the same soft band the
        # selected picker row uses, so a dropped file reads as one object instead
        # of a wall of path text. Green stays the only accent.
        "tui.input.attachment": f"bg:{_BAND_BG}",
        # Accent-coloured border highlights the input box.
        "frame.border": _ACCENT,
        "tui.footer.mark": "#56b6c2",
        "tui.footer.brand": "bold",
        "tui.footer.model": "#d0d0d0",
        "tui.footer.value": "#8a8a8a",
        "tui.footer.dim": "#6c6c6c",
        "tui.footer.user": "#8a8a8a",
        "tui.footer.workspace": "#8a8a8a",
        "tui.footer.branch": "#56b6c2",
        "tui.footer.context": "#8a8a8a",
        # Forge-session badge: a distinct bold violet chip so the planning mode is
        # unmistakable (green stays the chat accent, cyan the brand/branch).
        "tui.footer.forge": "bold #bc8cff",
        # Active-subagent badge base style; the footer overlays each agent's own
        # accent (see tui.subagent_identity), so blue is only the fallback
        # (distinct from green chat accent, violet forge, and cyan brand).
        "tui.footer.subagent": "bold #58a6ff",
        # User's own message: a full-width highlighted band (the "> " in accent).
        "tui.transcript.userband": f"bold bg:{_BAND_BG}",
        "tui.transcript.userprompt": f"bold {_ACCENT} bg:{_BAND_BG}",
        # Assistant reply: plain text behind an accent marker.
        "tui.transcript.assistantmark": f"bold {_ACCENT}",
        "tui.transcript.assistant": "",
        # Model reasoning ("thinking"): muted + italic so it reads as background
        # process, not the answer.
        "tui.transcript.reasoning": "italic #8b949e",
        "tui.transcript.reasoningmark": "#6e7681",
        "tui.transcript.system": "#8a8a8a",
        "tui.transcript.trace": "#6c6c6c",
        "tui.transcript.error": "#e06c75",
        "tui.transcript.warn": "#d19a66",
        "tui.transcript.selection": "reverse",
        "tui.transcript.link": "underline #58a6ff",
        # Subagent identity line ("↪ <name> · <mode>") — the shared subagent
        # blue (per-agent accents live in the footer badge).
        "tui.transcript.subagent": "bold #58a6ff",
        "tui.status": "#8a8a8a",
        "tui.diff.muted": "#8a8a8a",
        "tui.diff.add": "#3fb950",
        "tui.diff.remove": "#e06c75",
        # Trace colors describe outcomes, with text/glyphs retained in monochrome.
        "tui.trace.active": "bold #58a6ff",
        "tui.trace.success": "bold #3fb950",
        "tui.trace.warning": "bold #d19a66",
        "tui.trace.command": "#56b6c2",
        "tui.trace.agent": "#bc8cff",
        "tui.trace.detail": "#c9d1d9",
        "tui.tip.link": f"underline {_ACCENT}",
        # Working line turns amber once a turn has run long (>=30s).
        "tui.status.warn": "#d29922",
        # Full-screen /config surface. Keep this semantic so terminal-theme
        # translation can pair its background with the matching foregrounds.
        "tui.config": "bg:#0d1117",
        # Question cards: a quiet border, distinct heading, and one focused row.
        "tui.question": "#c9d1d9 bg:#0d1117",
        "tui.question frame.border": "#30363d bg:#0d1117",
        "tui.question.meta": f"bold {_ACCENT} bg:#0d1117",
        "tui.question.heading": "bold #e6edf3 bg:#0d1117",
        "tui.question.muted": "#8b949e bg:#0d1117",
        "tui.question.option": "#c9d1d9 bg:#0d1117",
        "tui.question.active": f"bold {_ACCENT} bg:{_BAND_BG}",
        "tui.question.input": "#e6edf3 bg:#161b22",
        "tui.question.field frame.border": "#30363d bg:#0d1117",
        "tui.question.error": "#d19a66 bg:#0d1117",
        # Right-hand scrollbar. The margin paints spaces, so the colour must be a
        # BACKGROUND (a foreground does nothing on a blank cell, leaving the ugly
        # default light-grey bar). Faint near-black track + accent-green thumb.
        "scrollbar.background": "bg:#1a1a1a",
        "scrollbar.button": f"bg:{_ACCENT}",
        "scrollbar.arrow": f"{_ACCENT} bg:#1a1a1a",
        "scrollbar.start": "nounderline",
        "scrollbar.end": "nounderline",
        # Centered /help popup: an opaque dark panel (so the transcript behind it
        # does not bleed through), green commands on the left, dim descriptions on
        # the right, amber section headers, a dim footer hint.
        "tui.help": "bg:#0d1117",
        "tui.help.cmd": f"bold {_ACCENT} bg:#0d1117",
        "tui.help.desc": "#c9d1d9 bg:#0d1117",
        # Section headers: bold + bright neutral (no colour clash with the green
        # commands) so the green stays the only accent.
        "tui.help.section": "bold #e6edf3 bg:#0d1117",
        "tui.help.hint": "italic #6e7681 bg:#0d1117",
        "tui.help.frame": "bg:#0d1117",
        "tui.help.frame frame.border": _ACCENT,
        # Key/value panels (e.g. /status) reuse the same opaque /help chrome. Keys
        # read as dim labels; values are toned by health: accent green = on/healthy,
        # plain = neutral/degraded, amber = caution, red = error. Green stays the
        # single accent — degraded states are plain, never a second accent.
        "tui.help.key": "#8a8a8a bg:#0d1117",
        "tui.help.accent": f"bold {_ACCENT} bg:#0d1117",
        "tui.help.warn": "#d19a66 bg:#0d1117",
        "tui.help.err": "#e06c75 bg:#0d1117",
        # Slash-command dropdown (shown while typing "/…"): same dark panel as the
        # popups; the highlighted row uses the band bg + accent green so it reads
        # like the panels. Green stays the only accent.
        "completion-menu": "bg:#0d1117",
        "completion-menu.completion": "bg:#0d1117 #c9d1d9",
        "completion-menu.completion.current": f"bg:{_BAND_BG} bold {_ACCENT}",
        "completion-menu.meta.completion": "bg:#0d1117 #6e7681",
        "completion-menu.meta.completion.current": f"bg:{_BAND_BG} #c9d1d9",
        # Selectable picker (e.g. /permissions): same dark panel; the focused row gets the
        # band bg + accent caret/label so it stands out. Green is the only accent.
        "tui.picker": "bg:#0d1117",
        "tui.picker.num": "#8a8a8a bg:#0d1117",
        "tui.picker.label": "#c9d1d9 bg:#0d1117",
        "tui.picker.desc": "#8a8a8a bg:#0d1117",
        "tui.picker.warndesc": "#d29922 bg:#0d1117",
        "tui.picker.header": "bold #6e7681 bg:#0d1117",
        "tui.picker.tag": f"{_ACCENT} bg:#0d1117",
        "tui.picker.hint": "italic #6e7681 bg:#0d1117",
        "tui.picker.sel": f"bg:{_BAND_BG}",
        "tui.picker.selcaret": f"bold {_ACCENT} bg:{_BAND_BG}",
        "tui.picker.selnum": f"bold {_ACCENT} bg:{_BAND_BG}",
        "tui.picker.sellabel": f"bold {_ACCENT} bg:{_BAND_BG}",
        "tui.picker.seldesc": f"#c9d1d9 bg:{_BAND_BG}",
        "tui.picker.seltag": f"{_ACCENT} bg:{_BAND_BG}",
        # Execution-mode badge in the footer: accent green normally, amber when in
        # the unguarded fullaccess mode so the danger state is glanceable.
        "tui.footer.mode": f"bold {_ACCENT}",
        "tui.footer.mode.warn": "bold #d19a66",
        # Live Forge execution view (the task table + phase line shown while
        # /execute plan runs). Violet ties it to the FORGE badge; done=green,
        # failed=red so task outcomes are glanceable.
        "tui.forge.head": "bold #bc8cff",
        "tui.forge.rule": "#6e7681",
        "tui.forge.done": f"bold {_ACCENT}",
        "tui.forge.fail": "bold #e06c75",
        "tui.forge.run": "bold #bc8cff",
        "tui.forge.idle": "#6c6c6c",
        "tui.forge.id": "#8a8a8a",
        "tui.forge.title": "#c9d1d9",
        # Live header progress gauge: X/Y count (violet, ties to the header), a
        # violet "done" bar segment, a red "failed" segment, and a dim empty
        # track — so run progress is glanceable without reading every row.
        "tui.forge.count": "bold #bc8cff",
        "tui.forge.meter": "#bc8cff",
        "tui.forge.meterfail": "#e06c75",
        "tui.forge.meterempty": "#3a3a3a",
        # Per-task elapsed timer on a running row (dim so it never competes with
        # the title).
        "tui.forge.timer": "#6e7681",
        # Forge planner plan-update aside ("▸ plan updated · N notes"): violet
        # chevrons/glyphs tie it to the Forge identity, touched-task titles read
        # bright like the forge table, planner warnings get an amber ⚠, and the
        # rest stays dim so the aside never competes with the reply above it.
        "tui.planmeta.mark": "#bc8cff",
        "tui.planmeta.head": "#8b949e",
        "tui.planmeta.dim": "#6c6c6c",
        "tui.planmeta.subject": "#c9d1d9",
        "tui.planmeta.tag": "italic #8a8a8a",
        "tui.planmeta.warn": "#d19a66",
        "tui.planmeta.hint": "italic #6e7681",
        # Live subagent tail: blue identity chrome with quiet body text. The
        # frame sits in the root HSplit, so it takes space from the transcript.
        "tui.subpanel": "",
        "tui.subpanel frame.border": "#58a6ff",
        "tui.subpanel.head": "bold #58a6ff",
        "tui.subpanel.state": "bold #c9d1d9",
        "tui.subpanel.dim": "#8b949e",
        "tui.subpanel.tool": "#d19a66",
        "tui.subpanel.result": "#8b949e",
        "tui.subpanel.assistant": "#c9d1d9",
        "tui.subpanel.user": "#8b949e",
        "tui.subpanel.hint": "italic #6e7681",
        # Pending user messages sit in a quiet full-width band directly above
        # the input. The darker surface separates queued work from transcript
        # prose; the blue heading and dim hint establish hierarchy without
        # making the panel look like an error or approval prompt.
        "tui.pending-messages": "bg:#161b22",
        "tui.pending-messages.head": "bold #79c0ff bg:#161b22",
        "tui.pending-messages.row": "#e6edf3 bg:#161b22",
        "tui.pending-messages.hint": "italic #8b949e bg:#161b22",
        # Violet frame border for Forge popups (/show, /plan, the launch gate),
        # applied by wrapping the shared help frame in a container that adds the
        # ``tui.forgeframe`` ancestor class. MUST be declared AFTER
        # ``tui.help.frame frame.border`` above: both selectors have equal
        # specificity, so the later declaration wins the tie and repaints the
        # border violet only while a Forge panel is open.
        "tui.forgeframe frame.border": "#bc8cff",
        # In-TUI editor float (e.g. /plan edit): dark panel + a status bar that
        # turns red when a save is rejected (invalid JSON / shape).
        "tui.editor": "bg:#0d1117 #c9d1d9",
        "tui.editor.statusbar": "bg:#161b22",
        "tui.editor.hint": "italic #6e7681 bg:#161b22",
        "tui.editor.err": "bold #e06c75 bg:#161b22",
        # Approval modal: an amber-bordered popup (red-bordered when destructive) so
        # the question grabs attention; colour-coded keys (y green / a cyan / n red).
        "tui.approve": "bg:#0d1117",
        "tui.approve.head": "bold #d19a66 bg:#0d1117",
        "tui.approve.head.danger": "bold #e06c75 bg:#0d1117",
        "tui.approve.target": "bold #e6edf3 bg:#0d1117",
        "tui.approve.reason": "italic #8a8a8a bg:#0d1117",
        "tui.approve.optlabel": "#c9d1d9 bg:#0d1117",
        "tui.approve.key.yes": f"bold {_ACCENT} bg:#0d1117",
        "tui.approve.key.always": "bold #56b6c2 bg:#0d1117",
        "tui.approve.key.no": "bold #e06c75 bg:#0d1117",
        "tui.approve.frame": "bg:#0d1117",
        "tui.approve.frame frame.border": "#d19a66",
        # Transcript style while a small modal is up - muted gray on the
        # base background, so context stays readable behind the modal instead
        # of vanishing under the old space-painting scrim.
        "tui.dimbehind": "fg:#57606a bg:#0d1117 nobold noitalic nounderline",
    }
)

_DARK_STYLE_RULES: dict[str, str] = dict(_STYLE.style_rules)

_LIGHT_COLOR_TOKENS = {
    "#3fb950": "#187a3d",
    "#21262d": "#eef3ee",
    "#7a7a7a": "#57606a",
    "#8a8a8a": "#57606a",
    "#6c6c6c": "#6e7781",
    "#56b6c2": "#0969da",
    "#d0d0d0": "#24292f",
    "#bc8cff": "#8250df",
    "#e3b341": "#9a6700",
    "#58a6ff": "#0969da",
    "#8b949e": "#57606a",
    "#6e7681": "#57606a",
    "#e06c75": "#cf222e",
    "#d19a66": "#9a6700",
    "#d29922": "#9a6700",
    "#1a1a1a": "#d0d7de",
    "#0d1117": "#f6f8fa",
    "#c9d1d9": "#24292f",
    "#e6edf3": "#1f2328",
    "#3a3a3a": "#d8dee4",
    "#161b22": "#eaeef2",
}

_NEUTRAL_COLOR_TOKENS = {
    "#3fb950": "ansigreen",
    "#21262d": "",
    "#7a7a7a": "",
    "#8a8a8a": "",
    "#6c6c6c": "",
    "#56b6c2": "ansicyan",
    "#d0d0d0": "",
    "#bc8cff": "ansimagenta",
    "#e3b341": "ansiyellow",
    "#58a6ff": "ansiblue",
    "#8b949e": "",
    "#6e7681": "",
    "#e06c75": "ansired",
    "#d19a66": "ansiyellow",
    "#d29922": "ansiyellow",
    "#1a1a1a": "",
    "#0d1117": "",
    "#c9d1d9": "",
    "#e6edf3": "",
    "#3a3a3a": "",
    "#161b22": "",
}


def _theme_style_value(value: str, theme: TerminalTheme) -> str:
    if theme == "dark":
        return value
    replacements = _LIGHT_COLOR_TOKENS if theme == "light" else _NEUTRAL_COLOR_TOKENS
    translated: list[str] = []
    for token in value.split():
        if token.startswith("bg:"):
            replacement = replacements.get(token[3:], token[3:])
            if replacement:
                translated.append(f"bg:{replacement}")
            continue
        replacement = replacements.get(token, token)
        if replacement:
            translated.append(replacement)
    return " ".join(translated)


def _build_tui_style(theme: TerminalTheme) -> Style:
    """Build chat chrome from semantic terminal-theme colours.

    Unknown terminals use the neutral palette: semantic ANSI accents remain,
    while surface backgrounds and assumed foreground grays are inherited from
    the terminal. This avoids guessing either a dark or a light canvas.
    """

    rules = {
        selector: _theme_style_value(value, theme) for selector, value in _DARK_STYLE_RULES.items()
    }
    if theme == "light":
        rules["tui.input"] = "#1f2328"
        rules["tui.transcript.userband"] = "bold #24292f bg:#eef3ee"
        rules["tui.trace.command"] = "#0b7285"
    elif theme == "dark":
        rules["tui.input"] = "#e6edf3"
    return Style.from_dict(rules)


# Stub reply used only when no agent session is wired (Phase 1 / tests).
_PREVIEW_REPLY = "TUI preview - no agent session attached."


def _user_band_rows(text: str, width: int) -> list[list[tuple[str, str]]]:
    """Render the user's own message as a full-width highlighted band (one blank
    band row above and below the text) so their question stands out from the
    agent's reply. Wrap-aware; every row is padded to ``width``.

    Returns a list of rows, each a list of ``(style, text)`` fragments.
    """
    width = max(8, int(width))
    inner = max(1, width - 2)  # room for the "› " / "  " prefix
    wrapped: list[tuple[str, list[tuple[str, str | None]]]] = []
    for line in text.split("\n") or [""]:
        chunks = (textwrap.wrap(line, inner) or [""]) if line else [""]
        wrapped.extend(zip(chunks, link_segments(line, chunks), strict=True))
    if not wrapped:
        wrapped = [("", [("", None)])]
    band = "class:tui.transcript.userband"
    prompt = "class:tui.transcript.userprompt"
    blank_row: list[tuple[str, str]] = [(band, " " * width)]
    rows: list[list[tuple[str, str]]] = [blank_row]
    for index, (line, segments) in enumerate(wrapped):
        prefix = "› " if index == 0 else "  "
        text_width = width - len(prefix)
        if any(target for _text, target in segments):
            padding = [(band, " " * (text_width - len(line)))] if len(line) < text_width else []
            rows.append([(prompt, prefix), *link_fragments(band, segments), *padding])
        else:
            rows.append([(prompt, prefix), (band, line.ljust(text_width))])
    rows.append([(band, " " * width)])
    return rows


# Rows scrolled per mouse-wheel notch. Three matches the familiar default used
# by editors such as Vim, while an environment override keeps high-resolution
# trackpads and coarse physical wheels tunable without a persistent HUD.
_SCROLL_SPEED_ENV = "ALYSIS_SCROLL_SPEED"
_DEFAULT_WHEEL_STEP_ROWS = 3
_MIN_WHEEL_STEP_ROWS = 1
_MAX_WHEEL_STEP_ROWS = 20
_COPY_NOTICE_SECONDS = 1.5
# A link notice names a URL, or a failure the user has to act on, so it stays up
# longer than the copy acknowledgement.
_LINK_NOTICE_SECONDS = 3.0
# A double click is two clicks: the second must not open the page again.
_LINK_REOPEN_GUARD_SECONDS = 1.0
_DRAG_SCROLL_INTERVAL_SECONDS = 0.06


def _resolve_wheel_step_rows(raw_value: str | None = None) -> int:
    raw = env_get(_SCROLL_SPEED_ENV, "") if raw_value is None else raw_value
    try:
        parsed = int(str(raw or "").strip())
    except (TypeError, ValueError):
        return _DEFAULT_WHEEL_STEP_ROWS
    return max(_MIN_WHEEL_STEP_ROWS, min(parsed, _MAX_WHEEL_STEP_ROWS))


def _resolve_tui_input(explicit_input: Any | None) -> tuple[Any, Any | None]:
    """Prefer the controlling terminal when stdin is redirected through a pipe."""
    if explicit_input is not None:
        return explicit_input, None
    created_input = create_input(always_prefer_tty=True)
    return created_input, created_input


class _ScrollableControl(FormattedTextControl):
    """FormattedTextControl that routes mouse-wheel events to ``on_scroll`` so the
    wheel drives our follow/scroll state. Returning ``None`` marks the event
    handled, stopping the Window's own ``vertical_scroll`` (which the cursor-pin
    would otherwise override); other events fall through unchanged.
    """

    def __init__(
        self,
        *args: Any,
        on_scroll: Callable[[int], None],
        on_mouse_event: Callable[[Any], Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._on_scroll = on_scroll
        self._on_mouse_event = on_mouse_event

    def mouse_handler(self, mouse_event: Any) -> Any:
        event_type = mouse_event.event_type
        if event_type == MouseEventType.SCROLL_UP:
            self._on_scroll(-1)
            return None
        if event_type == MouseEventType.SCROLL_DOWN:
            self._on_scroll(1)
            return None
        if self._on_mouse_event is not None:
            return self._on_mouse_event(mouse_event)
        return NotImplemented


class _MouseCaptureWindow(Window):
    """Transparent overlay window that receives raw screen mouse coordinates.

    ``Window`` normally translates screen coordinates into content coordinates
    before calling its control. Its fallback handler still receives the original
    event, which lets a full-screen transparent float retain a drag after the
    pointer leaves the control where that drag began.
    """

    def __init__(self, on_mouse_event: Callable[[MouseEvent], Any]) -> None:
        super().__init__()
        self._on_mouse_event = on_mouse_event

    def _mouse_handler(self, mouse_event: MouseEvent) -> Any:
        return self._on_mouse_event(mouse_event)


def _drag_scroll_direction(*, screen_y: int, window_top: int, window_height: int) -> int:
    """Return -1/0/+1 when a drag is at the top/middle/bottom viewport edge."""
    if window_height <= 0:
        return 0
    if screen_y <= window_top:
        return -1
    if screen_y >= window_top + window_height - 1:
        return 1
    return 0


def _project_mouse_event_to_window(
    mouse_event: MouseEvent,
    window: Window,
) -> tuple[MouseEvent, int] | None:
    """Project a screen event onto the nearest visible cell of ``window``.

    The capture overlay receives absolute layout coordinates. ``WindowRenderInfo``
    keeps the exact origin used by prompt_toolkit's own mouse mapper, while its
    public visible-line map converts the clamped screen row back to content space.
    """
    info = window.render_info
    if info is None or info.window_height <= 0 or info.window_width <= 0:
        return None

    # prompt_toolkit does not expose the rendered content origin publicly; these
    # two coordinates are nevertheless the source used by its own mouse wrapper.
    window_top = int(info._y_offset)
    window_left = int(info._x_offset)
    relative_y = max(0, min(mouse_event.position.y - window_top, info.window_height - 1))
    relative_x = max(0, min(mouse_event.position.x - window_left, info.window_width - 1))

    row_col = info.visible_line_to_row_col.get(relative_y)
    if row_col is None:
        visible_rows = sorted(info.visible_line_to_row_col)
        if not visible_rows:
            return None
        nearest = min(visible_rows, key=lambda row: abs(row - relative_y))
        row_col = info.visible_line_to_row_col[nearest]
    row, column = row_col
    content_x = column + relative_x
    # Screen columns and content character indices differ for wide/combining
    # characters. Use the same renderer mapping as Window's mouse handler.
    screen_y = window_top + relative_y
    screen_x = window_left + relative_x
    candidates = [
        (x, content_row, content_column)
        for (content_row, content_column), (y, x) in getattr(info, "_rowcol_to_yx", {}).items()
        if y == screen_y and x <= screen_x
    ]
    if candidates:
        _screen_x, row, content_x = max(candidates)
    projected = MouseEvent(
        position=Point(x=max(0, content_x), y=max(0, row)),
        event_type=mouse_event.event_type,
        button=mouse_event.button,
        modifiers=mouse_event.modifiers,
    )
    direction = _drag_scroll_direction(
        screen_y=mouse_event.position.y,
        window_top=window_top,
        window_height=info.window_height,
    )
    return projected, direction


def _ordered_selection(anchor: Point, active: Point) -> tuple[Point, Point]:
    if (anchor.y, anchor.x) <= (active.y, active.x):
        return anchor, active
    return active, anchor


def _selection_span_for_row(
    *,
    row_index: int,
    row_length: int,
    anchor: Point,
    active: Point,
) -> tuple[int, int] | None:
    if anchor == active:
        return None
    start, end = _ordered_selection(anchor, active)
    if row_index < start.y or row_index > end.y:
        return None
    start_col = start.x if row_index == start.y else 0
    end_col = end.x + 1 if row_index == end.y else row_length
    start_col = max(0, min(start_col, row_length))
    end_col = max(start_col, min(end_col, row_length))
    return (start_col, end_col) if end_col > start_col else None


def _selected_text(
    rows: list[str],
    anchor: Point,
    active: Point,
    *,
    row_roles: list[str] | None = None,
) -> str:
    """Extract the selected transcript text.

    ``row_roles`` is supplied by the live TUI and makes clipboard copies
    semantic: only conversation/diagnostic rows are eligible, while provider
    reasoning summaries, tool traces, Forge activity, and other UI chrome are
    omitted. The optional default preserves the helper's plain-row behaviour for
    callers that do not have transcript role metadata.
    """
    if not rows or anchor == active:
        return ""
    start, end = _ordered_selection(anchor, active)
    first_row = max(0, min(start.y, len(rows) - 1))
    last_row = max(first_row, min(end.y, len(rows) - 1))
    pieces: list[str] = []
    for row_index in range(first_row, last_row + 1):
        row = rows[row_index]
        role = None
        if row_roles is not None:
            role = row_roles[row_index] if row_index < len(row_roles) else ""
            if role not in _COPYABLE_TRANSCRIPT_ROLES:
                continue
        span = _selection_span_for_row(
            row_index=row_index,
            row_length=len(row),
            anchor=start,
            active=end,
        )
        if span is None:
            pieces.append("")
            continue
        piece = row[span[0] : span[1]].rstrip()
        pieces.append(_strip_transcript_copy_chrome(piece, role=role, starts_row=span[0] == 0))
    return "\n".join(pieces).strip("\n")


def _strip_transcript_copy_chrome(
    text: str, *, role: str | None = None, starts_row: bool = True
) -> str:
    """Remove Alysis Code-only prefixes from copied transcript rows."""
    if role == "planmeta":
        # Plan-aside lead rows: drop the chevron/bullet/⚠ glyph and the expand
        # hint, keep the indentation (it carries the note hierarchy). A sweep
        # that starts mid-row begins with note content, so only the hint goes.
        return strip_plan_meta_copy_chrome(text, starts_row=starts_row)
    if role == "planmetacont":
        # A wrap continuation of a plan-aside note: its first character is
        # note CONTENT (which may legitimately be '+', '~', or '·') — never
        # strip anything from it.
        return text
    prefixes = (
        "› ",
        f"{_ASSIST_MARK} ",
        f"{_THINK_OPEN} ",
        f"{_THINK_CLOSED} ",
        f"{_THINK_RAIL} ",
    )
    for prefix in prefixes:
        if text.startswith(prefix):
            return text[len(prefix) :]
    # Failed tool outcomes remain useful diagnostics even though successful tool
    # trace rows are excluded. Remove their status decoration without treating a
    # literal glyph in user/assistant content as chrome.
    if role == "error" and text.startswith("✗ "):
        return text[2:]
    if role == "warn" and text.startswith("⚠ "):
        return text[2:]
    return text


def _plan_meta_press_hit(row_roles: list[str], y: int) -> bool:
    """True when a mouse press at row ``y`` lands on the plan-update aside
    (any of its rows, including wrap continuations)."""
    return 0 <= y < len(row_roles) and row_roles[y] in PLAN_META_ROW_ROLES


def _plan_meta_click_toggles(
    *, pressed_planmeta: bool, anchor: Point | None, release: Point, selected: str
) -> bool:
    """The press+release was a plain CLICK on the aside — the pointer never
    moved and nothing was swept — so the release toggles it. Any drag, even
    one whose sweep strips to empty text (e.g. out over the blank margin or
    chrome rows), is a selection gesture and must never toggle."""
    return bool(pressed_planmeta) and not selected and anchor == release


def _highlight_selection_in_row(
    row: list[tuple[str, str]],
    *,
    row_index: int,
    anchor: Point | None,
    active: Point | None,
) -> list[tuple[str, str]]:
    if anchor is None or active is None:
        return row
    row_length = sum(len(text) for _style, text in row)
    span = _selection_span_for_row(
        row_index=row_index,
        row_length=row_length,
        anchor=anchor,
        active=active,
    )
    if span is None:
        return row
    selection_start, selection_end = span
    highlighted: list[tuple[str, str]] = []
    cursor = 0
    for style, text in row:
        fragment_start = cursor
        fragment_end = cursor + len(text)
        overlap_start = max(fragment_start, selection_start)
        overlap_end = min(fragment_end, selection_end)
        if overlap_start >= overlap_end:
            highlighted.append((style, text))
        else:
            before = text[: overlap_start - fragment_start]
            selected = text[overlap_start - fragment_start : overlap_end - fragment_start]
            after = text[overlap_end - fragment_start :]
            if before:
                highlighted.append((style, before))
            highlighted.append((f"{style} class:tui.transcript.selection".strip(), selected))
            if after:
                highlighted.append((style, after))
        cursor = fragment_end
    return highlighted


def _copy_selection_notice(selected: str) -> str:
    """Copy a completed selection and return the transient status message."""
    try:
        copy_text_to_clipboard(selected)
    except ClipboardError:
        return "Selected text · clipboard unavailable"
    unit = "character" if len(selected) == 1 else "characters"
    return f"Copied {len(selected):,} {unit}"


def _link_fallback_notice(url: str) -> str:
    """No browser could be launched (SSH, headless box): hand the link over instead."""
    try:
        copy_text_to_clipboard(url)
    except Exception:  # noqa: BLE001 - runs on a helper thread; must not raise
        return f"Couldn't open a browser for {_clip_cell(url, 60)}"
    return "Couldn't open a browser · link copied to the clipboard"


def _scroll_target(current_row: int, last_row: int, delta: int) -> tuple[int, bool]:
    """Clamp a scroll move and report whether we landed at the live tail.

    Returns ``(new_row, follow)`` — ``follow`` is True when ``new_row`` reaches the
    last row, so new content keeps auto-scrolling.
    """
    last = max(0, last_row)
    target = max(0, min(current_row + delta, last))
    return target, target >= last


def _wrap_line(line: str, width: int) -> list[str]:
    """Wrap one logical line to ``<= width`` columns, preserving blank lines.

    The transcript window renders with ``wrap_lines=True``, so any emitted row
    WIDER than the content width is silently re-wrapped into extra *screen* rows
    that the follow/scroll math (which counts logical rows) never accounts for.
    That undercount makes auto-follow undershoot, pushing the live "thinking"
    line below the fold so it reads as hidden behind the footer. Pre-wrapping
    every emitted row keeps logical rows == screen rows so follow lands on the
    true last line. ``break_long_words`` guarantees even a single long token
    (e.g. a URL) is hard-broken to ``<= width``.
    """
    width = max(1, int(width))
    if not line:
        return [""]
    return textwrap.wrap(line, width, break_long_words=True, break_on_hyphens=False) or [""]


def _plain_role_rows(style: str, text: str, width: int) -> list[list[tuple[str, str]]]:
    """Render a non-streamed line (trace/error/warn/system/info) wrapped to width.

    URLs become clickable link fragments (see ``links.py``).
    """
    rows: list[list[tuple[str, str]]] = []
    for sub in text.split("\n") or [""]:
        chunks = _wrap_line(sub, width)
        for segments in link_segments(sub, chunks):
            rows.append(link_fragments(style, segments))
    return rows


def _completion_menu_height(
    rows: int,
    *,
    bottom_margin: int = _COMPLETION_MENU_BOTTOM,
    max_height: int = _COMPLETION_MENU_MAX_HEIGHT,
) -> int:
    """Cap the slash menu so it stays above the input/footer chrome."""
    available = max(1, int(rows) - int(bottom_margin) - 2)
    return max(1, min(int(max_height), available))


def _completion_menu_width(
    cols: int,
    *,
    max_width: int = _COMPLETION_MENU_MAX_WIDTH,
) -> int:
    """Bound completion rows so long command help cannot overflow right."""
    return max(20, min(int(max_width), max(20, int(cols) - 8)))


def _assistant_rows(
    text: str,
    width: int = 80,
    *,
    markdown: bool = True,
    theme: TerminalTheme = "neutral",
) -> list[list[tuple[str, str]]]:
    """Render an assistant reply behind the accent marker, with continuation
    lines indented to align under the first line's text.

    When ``markdown`` is on (the reply is complete) and the text carries
    block-level markdown, it is rendered through Rich into styled rows — headings,
    lists, tables and syntax-highlighted code blocks. The marker lands on the
    first non-blank row; everything else is indented two columns to match. While
    a reply is still streaming the caller passes ``markdown=False`` so partial
    output (a half-open code fence) renders as plain text and never flickers.
    """
    mark = "class:tui.transcript.assistantmark"
    body = "class:tui.transcript.assistant"
    if markdown:
        md_rows = render_markdown_rows(text, max(1, int(width) - 2), theme)
        if md_rows is not None:
            marker_at = next(
                (i for i, row in enumerate(md_rows) if "".join(t for _s, t in row).strip()),
                0,
            )
            rows: list[list[tuple[str, str]]] = []
            for index, row in enumerate(md_rows):
                prefix = (mark, f"{_ASSIST_MARK} ") if index == marker_at else (body, "  ")
                rows.append([prefix, *row])
            return rows
    # Plain fallback (streaming or non-markdown). Wrap to the content width minus
    # the 2-col marker/indent so logical rows match screen rows (see _wrap_line).
    inner = max(1, int(width) - 2)
    rows = []
    first = True
    for line in text.split("\n") or [""]:
        chunks = _wrap_line(line, inner)
        for segments in link_segments(line, chunks):
            if first:
                rows.append([(mark, f"{_ASSIST_MARK} "), *link_fragments(body, segments)])
                first = False
            else:
                rows.append(link_fragments(body, segments, prefix="  "))
    return rows


def _reasoning_rows(
    text: str,
    width: int,
    *,
    live: bool,
    secs: int,
    expanded: bool,
    spinner: str = "",
    elapsed: int = 0,
) -> list[list[tuple[str, str]]]:
    """Render provider-returned reasoning text or a summary as a dim aside.

    A header sits flush left, directly beneath the user's message: while ``live``
    it animates as ``⠋ thinking… Ns`` (the ``spinner`` char + ``elapsed`` seconds);
    once closed it collapses to a short header (expanded automatically for
    ``/trace full``). Provider adapters keep encrypted state and signatures
    separate from the text passed to this renderer.
    """
    rail = "class:tui.transcript.reasoningmark"
    body = "class:tui.transcript.reasoning"
    show_body = live or expanded
    if live:
        lead = spinner or _THINK_OPEN
        header = f"thinking… {elapsed}s" if elapsed else "thinking…"
    elif expanded:
        lead = _THINK_OPEN
        header = "thinking"
    else:
        lead = _THINK_CLOSED
        preview = " ".join(str(text or "").split()) or "thinking"
        preview_limit = max(12, int(width) - 2)
        header = (
            preview
            if len(preview) <= preview_limit
            else preview[: max(1, preview_limit - 1)].rstrip() + "…"
        )
    rows: list[list[tuple[str, str]]] = [[(rail, f"{lead} "), (body, header)]]
    if not show_body:
        return rows
    inner = max(1, int(width) - 2)  # room for the "│ " rail
    for line in text.split("\n"):
        chunks = (textwrap.wrap(line, inner) if line else [""]) or [""]
        for segments in link_segments(line, chunks):
            rows.append([(rail, f"{_THINK_RAIL} "), *link_fragments(body, segments)])
    return rows


def _activity_rows(
    spinner: str,
    label: str,
    elapsed: int,
    *,
    elapsed_is_run_time: bool = False,
) -> list[list[tuple[str, str]]]:
    """The single live activity line shown under the content while a turn runs —
    ``⠋ thinking… Ns`` or ``⠋ Search Web… Ns``. Matches the live reasoning header
    so the indicator and a streaming thought flow into each other seamlessly."""
    rail = "class:tui.trace.active"
    body = "class:tui.trace.detail"
    clock = (
        f" \u00b7 run time {elapsed}s"
        if elapsed and elapsed_is_run_time
        else f" {elapsed}s"
        if elapsed
        else ""
    )
    return [[(rail, f"{spinner} "), (body, label), ("class:tui.diff.muted", clock)]]


def _sync_subagent_started_at(
    started_at: dict[str, float],
    names: tuple[str, ...],
    *,
    now: float,
) -> None:
    active = {str(name) for name in names if str(name)}
    for stale_name in set(started_at) - active:
        started_at.pop(stale_name, None)
    for name in active:
        started_at.setdefault(name, float(now))


def _activity_elapsed_seconds(
    *,
    turn_started: float,
    cancellation_started: float = 0.0,
    active_subagent: str,
    subagent_started_at: dict[str, float],
    now: float,
) -> int:
    # Cancellation is its own lifecycle phase.  Reusing the turn/subagent
    # clock makes ``Cancelling current turn...`` appear to have been running
    # for the entire turn, which is both alarming and useless for diagnosing
    # provider/worker unwind latency.
    started = cancellation_started or None
    if started is None:
        started = subagent_started_at.get(active_subagent) if active_subagent else None
    if started is None:
        started = turn_started
    return max(0, int(float(now) - float(started))) if started else 0


def _plan_meta_rows(text: str, width: int, *, expanded: bool) -> list[list[tuple[str, str]]]:
    """Render captured Forge planner meta / plan-reconciliation notes as a
    collapsible aside beneath the reply.

    Collapsed (default): one ``▸`` summary line (an honest headline plus note /
    task counts) so the assistant answer stays uncluttered. Expanded (Ctrl+O or
    click): a ``▾`` header and the notes grouped per touched task — parsing,
    grouping, and styling live in ``plan_meta`` (see that module's docstring).
    Every row is ``<= width`` (the cursor-pin scroll math depends on it)."""
    return render_plan_meta_rows(text, width, expanded=expanded)


def _subagent_panel_container(
    panel_state: dict[str, Any], *, hint_provider: Callable[[], str] | None = None
) -> ConditionalContainer:
    """Build the in-flow live-child pane around mutable ``panel_state``."""

    def _fragments() -> FormattedText:
        run_id = str(panel_state.get("selected_run_id") or "")
        if not run_id:
            return FormattedText([])
        try:
            cols = get_app().output.get_size().columns
        except Exception:
            cols = 80
        width = max(12, int(cols) - 2)
        position, total = _subagent_panel_view_position(panel_state, run_id)
        statuses = panel_state.get("statuses", {})
        entries = panel_state.get("entries", {})
        status = statuses.get(run_id, {}) if isinstance(statuses, dict) else {}
        buffered = entries.get(run_id, []) if isinstance(entries, dict) else []
        rows = subagent_panel_rows(
            status if isinstance(status, dict) else {},
            buffered if isinstance(buffered, list) else [],
            width=width,
            height=_SUBAGENT_PANEL_CONTENT_HEIGHT,
            position=position,
            total=total,
            key_hint=hint_provider() if hint_provider is not None else None,
        )
        fragments: list[tuple[str, str]] = []
        for index, row in enumerate(rows):
            fragments.extend(row)
            if index < len(rows) - 1:
                fragments.append(("", "\n"))
        return FormattedText(fragments)

    body = Window(
        FormattedTextControl(_fragments, focusable=False),
        wrap_lines=False,
        style="class:tui.subpanel",
    )
    frame = Frame(
        body,
        title="Subagent live",
        style="class:tui.subpanel",
        height=D(min=3, preferred=_SUBAGENT_PANEL_HEIGHT, max=_SUBAGENT_PANEL_HEIGHT),
    )
    return ConditionalContainer(
        frame,
        filter=Condition(lambda: bool(panel_state.get("selected_run_id"))),
    )


def _subagent_panel_view_position(
    panel_state: dict[str, Any],
    run_id: str,
) -> tuple[int, int]:
    """Return the selected child's current children-only view number."""
    order = list(dict.fromkeys(str(value) for value in panel_state.get("run_order", []) if value))
    try:
        position = order.index(str(run_id)) + 1
    except ValueError:
        position = 1
    return position, max(1, len(order))


def _cycle_subagent_panel(panel_state: dict[str, Any], delta: int) -> str:
    """Move through child runs in spawn order, wrapping within children."""
    order = list(dict.fromkeys(str(value) for value in panel_state.get("run_order", []) if value))
    panel_state["run_order"] = order
    if not order:
        panel_state["selected_run_id"] = ""
        return ""
    selected = str(panel_state.get("selected_run_id") or "")
    try:
        current = order.index(selected)
    except ValueError:
        current = -1 if int(delta) >= 0 else 0
    target = (current + int(delta)) % len(order)
    selected = order[target]
    panel_state["selected_run_id"] = selected
    panel_state["last_poll"] = None
    return selected


_ACTIVE_SUBAGENT_STATES = frozenset({"spawned", "queued", "waiting", "running"})


def _subagents_picker_spec(
    scheduler: Any,
    panel_state: dict[str, Any],
) -> dict[str, Any] | None:
    """Build the active-child picker and wire selection to the live panel."""
    status = scheduler.status()
    raw_children = status.get("children") if isinstance(status, dict) else None
    active_children = [
        dict(child)
        for child in (raw_children if isinstance(raw_children, list) else [])
        if isinstance(child, dict)
        and str(child.get("state") or "").strip().lower() in _ACTIVE_SUBAGENT_STATES
        and str(child.get("run_id") or "").strip()
    ]
    if not active_children:
        return None

    children_by_run_id = {str(child["run_id"]): child for child in active_children}
    selected_run_id = str(panel_state.get("selected_run_id") or "")
    rows: list[dict[str, Any]] = []
    for child in active_children:
        run_id = str(child["run_id"])
        state = str(child.get("state") or "unknown")
        activity = " ".join(str(child.get("activity") or "working").split())
        workspace = str(child.get("workspace_view") or "shared")
        steps = max(0, int(child.get("steps_completed") or 0))
        elapsed_ms = max(0, int(child.get("elapsed_ms") or 0))
        elapsed = f"{elapsed_ms // 1000}s"
        quiet = quiet_time_label(child)
        quiet_suffix = f" \u00b7 {quiet}" if quiet else ""
        rows.append(
            {
                "label": subagent_identity(
                    child.get("subagent") or "subagent",
                    child.get("label"),
                ),
                "description": (
                    f"{state} \u00b7 {activity} \u00b7 {workspace} \u00b7 "
                    f"{steps} steps \u00b7 {elapsed}{quiet_suffix}"
                ),
                "value": run_id,
                "current": run_id == selected_run_id,
            }
        )

    def _select(run_id: str) -> list[tuple[str, str]]:
        selected = children_by_run_id.get(str(run_id))
        if selected is None:
            return []
        selected_id = str(selected["run_id"])
        run_order = panel_state.setdefault("run_order", [])
        if selected_id not in run_order:
            run_order.append(selected_id)
        panel_state.setdefault("cursors", {}).setdefault(selected_id, 0)
        panel_state.setdefault("entries", {}).setdefault(selected_id, [])
        panel_state.setdefault("statuses", {})[selected_id] = dict(selected)
        panel_state.setdefault("lifecycles", {}).setdefault(
            selected_id,
            {
                "run_id": selected_id,
                "subagent": str(selected.get("subagent") or "subagent"),
                "state": str(selected.get("state") or ""),
                "collected": False,
            },
        )
        panel_state["selected_run_id"] = selected_id
        panel_state["last_poll"] = None
        return []

    return {
        "title": "Active Subagents",
        "hint": "Enter open \u00b7 Esc close",
        "rows": rows,
        "on_select": _select,
    }


def _evict_subagent_panel_runs(
    panel_state: dict[str, Any],
    *,
    departed_run_id: str,
) -> list[dict[str, str]]:
    """Drop only the terminal pane the operator has just navigated away from."""
    selected = str(panel_state.get("selected_run_id") or "")
    departed = str(departed_run_id or "").strip()
    if not departed or departed == selected:
        return []
    lifecycles = panel_state.get("lifecycles", {})
    statuses = panel_state.get("statuses", {})
    if departed not in panel_state.get("run_order", []):
        return []
    lifecycle = lifecycles.get(departed, {}) if isinstance(lifecycles, dict) else {}
    if str(lifecycle.get("state") or "") not in {"joined", "cancelled"}:
        return []
    status = statuses.get(departed, {}) if isinstance(statuses, dict) else {}
    name = str(status.get("subagent") or lifecycle.get("subagent") or "subagent")
    outcome = str(lifecycle.get("outcome") or "").strip().lower()
    panel_state["run_order"].remove(departed)
    for key in ("cursors", "entries", "statuses", "lifecycles", "poll_failures"):
        mapping = panel_state.get(key)
        if isinstance(mapping, dict):
            mapping.pop(departed, None)
    return [{"run_id": departed, "subagent": name, "outcome": outcome}]


def _poll_selected_subagent(
    panel_state: dict[str, Any],
    scheduler: Any,
    *,
    now: float | None = None,
    on_failure: Callable[[str, str], None] | None = None,
) -> bool:
    """Incrementally refresh only the selected child, at most every 0.4s."""
    run_id = str(panel_state.get("selected_run_id") or "")
    if not run_id:
        return False

    def _record_failure(condition: str) -> bool:
        failures = panel_state.setdefault("poll_failures", {})
        if not isinstance(failures, dict) or run_id in failures:
            return False
        failures[run_id] = condition
        if on_failure is not None:
            try:
                on_failure(run_id, condition)
            except Exception:
                pass
        return True

    view_since = getattr(scheduler, "view_since", None)
    if scheduler is None or not callable(view_since):
        return _record_failure("view_since_unavailable")
    stamp = time.monotonic() if now is None else float(now)
    last_poll = panel_state.get("last_poll")
    if isinstance(last_poll, (int, float)) and stamp - float(last_poll) < 0.4:
        return False
    panel_state["last_poll"] = stamp
    cursors = panel_state.setdefault("cursors", {})
    cursor = max(0, int(cursors.get(run_id, 0)))
    try:
        payload = view_since(run_id=run_id, cursor=cursor)
    except Exception:
        return _record_failure("view_since_exception")
    if not isinstance(payload, dict) or payload.get("error"):
        return _record_failure("view_since_error_payload")
    try:
        cursors[run_id] = max(cursor, int(payload.get("next_cursor", cursor)))
    except (TypeError, ValueError):
        cursors[run_id] = cursor
    raw_entries = payload.get("transcript_tail")
    incoming = [
        {
            "kind": str(entry.get("kind") or ""),
            "summary": str(entry.get("summary") or ""),
        }
        for entry in (raw_entries if isinstance(raw_entries, list) else [])
        if isinstance(entry, dict)
    ]
    entries = panel_state.setdefault("entries", {})
    entries[run_id] = append_bounded_entries(entries.get(run_id, []), incoming)
    panel_state.setdefault("statuses", {})[run_id] = {
        key: value
        for key, value in payload.items()
        if key not in {"transcript_tail", "next_cursor"}
    }
    return True


# --------------------------------------------------------- live Forge view
# The status→bucket/glyph authority lives in ``forge_status`` (imported above), so
# a row's glyph can never contradict the header's "N done · N failed" count.


def _forge_task_visual(status: str, active: bool, spinner: str) -> tuple[str, str]:
    """Return ``(glyph, style_class)`` for one forge task row given its status and
    whether the swarm is currently working it. Done=✓ green, failed=✗ red, the
    in-flight task spins (violet), everything else is a dim ``○``."""
    glyph = forge_status_glyph(status, active=active, spinner=spinner)
    bucket = forge_status_bucket(status)
    if bucket == "done":
        return glyph, "class:tui.forge.done"
    if bucket == "failed":
        return glyph, "class:tui.forge.fail"
    if bucket == "running" or active:
        # A running task — or one the swarm just picked up (event arrived before
        # its on-disk status flipped) — spins in violet.
        return glyph, "class:tui.forge.run"
    return glyph, "class:tui.forge.idle"


def _forge_fmt_elapsed(seconds: int) -> str:
    """Compact elapsed like ``12s`` / ``1m04s`` — bounded to AT MOST 6 chars so it
    always fits the reserved timer column (the row width invariant depends on it)."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    minutes, secs = divmod(seconds, 60)
    if minutes < 100:
        return f"{minutes}m{secs:02d}s"
    if minutes < 100000:
        return f"{minutes}m"  # "100m".."99999m" — still <= 6 chars
    return "99999m"  # clamp an absurd/stuck elapsed so the 6-char contract holds


def _forge_header_fragments(
    width: int, run_id: str, done_n: int, failed_n: int, total: int
) -> list[tuple[str, str]]:
    """Build the live header — ``FORGE · run-xxx  ███░░  5/9`` — as styled
    fragments that sum to EXACTLY ``width`` columns (falling back to a dim rule
    when the panel is too narrow for a bar). Only narrow, single-column glyphs
    are used (block/▓/░/─) so the char count matches the display width and the
    cursor-pin scroll math stays exact."""
    width = max(1, int(width))
    run_label = f"FORGE · {run_id}" if run_id else "FORGE"
    count = f"{done_n}/{total}" if total else ""
    # Right block is " <count>" (a leading space), clipped so it can never exceed
    # the row on an ultra-narrow panel.
    right = (" " + count) if count else ""
    right = right[:width]
    left_avail = width - len(right)
    label = run_label[: max(0, left_avail)]
    mid_w = left_avail - len(label)  # columns between the label and the count

    frags: list[tuple[str, str]] = [("class:tui.forge.head", label)]
    if total > 0 and mid_w >= 6:
        # " " + bar(mid_w-2) + " "
        bar_w = mid_w - 2
        done_seg = min(bar_w, round(bar_w * done_n / total))
        fail_seg = min(bar_w - done_seg, round(bar_w * failed_n / total))
        empty_seg = bar_w - done_seg - fail_seg
        frags.append(("class:tui.forge.rule", " "))
        if done_seg:
            frags.append(("class:tui.forge.meter", "█" * done_seg))
        if fail_seg:
            frags.append(("class:tui.forge.meterfail", "█" * fail_seg))
        if empty_seg:
            frags.append(("class:tui.forge.meterempty", "░" * empty_seg))
        frags.append(("class:tui.forge.rule", " "))
    elif mid_w >= 2:
        frags.append(("class:tui.forge.rule", " " + "─" * (mid_w - 1)))
    elif mid_w == 1:
        frags.append(("class:tui.forge.rule", " "))
    if right:
        frags.append(("class:tui.forge.count", right))
    return frags


def _forge_view_rows(
    view: dict[str, Any],
    width: int,
    spinner: str,
    elapsed: int,
    task_elapsed: dict[str, int] | None = None,
) -> list[list[tuple[str, str]]]:
    """Render the live Forge execution block: a violet header with a progress
    gauge, a per-task status table (glyph · id · status · title · timer) and a
    phase/spinner line. ``task_elapsed`` maps a running task id to its elapsed
    seconds (from the transcript's per-task ``active_map``); when omitted, no
    per-row timers are drawn. Every row is ``<= width`` so the transcript's
    cursor-pin scroll math stays exact."""
    width = max(20, int(width))
    rows: list[list[tuple[str, str]]] = []
    run_id = str(view.get("run_id") or "")
    done = bool(view.get("done"))
    tasks = list(view.get("tasks") or [])
    active = view.get("active")
    active_map = view.get("active_map") or {}
    timers = dict(task_elapsed or {})

    # Header: "FORGE · run-xxx  ███░░  5/9" — a live progress gauge (done in
    # violet, failed in red, the rest a dim track) so run progress is glanceable.
    done_n, failed_n, _remaining_n = forge_status_counts(
        str(t.get("status", "") or "planned") for t in tasks
    )
    rows.append(_forge_header_fragments(width, run_id, done_n, failed_n, len(tasks)))

    # Task table — aligned id + status columns, title fills the rest, and a
    # right-aligned elapsed timer on each running row. Column widths are capped
    # RELATIVE to the width so the fixed prefix can never exceed the panel (which
    # would silently wrap the row and break the scroll math).
    if tasks:
        id_w = min(
            max((len(str(t.get("id", ""))) for t in tasks), default=2), 8, max(2, width // 6)
        )
        status_w = min(
            max((len(str(t.get("status", ""))) for t in tasks), default=7), 16, max(4, width // 4)
        )
        prefix = 2 + 2 + id_w + 2 + status_w + 2  # indent+glyph+id+gap+status+gap
        # Reserve a right-hand timer column only when a running task actually has
        # a timer AND the panel is wide enough to spare it.
        timer_strs = {tid: _forge_fmt_elapsed(sec) for tid, sec in timers.items()}
        timer_w = min(6, max((len(s) for s in timer_strs.values()), default=0))
        reserve = (timer_w + 1) if (timer_w and width - prefix > timer_w + 4) else 0
        title_w = max(0, width - prefix - reserve)
        for task in tasks:
            tid = str(task.get("id", ""))
            status = str(task.get("status", "") or "planned")
            title = str(task.get("title", "") or "")
            is_active = tid in active_map or (active is not None and tid == active)
            glyph, gstyle = _forge_task_visual(status, is_active, spinner)
            id_cell = tid[:id_w].ljust(id_w)
            status_cell = status[:status_w].ljust(status_w)
            row = [
                ("class:tui.forge.idle", "  "),
                (gstyle, f"{glyph} "),
                ("class:tui.forge.id", f"{id_cell}  "),
                (gstyle, f"{status_cell}  "),
                (
                    "class:tui.forge.title",
                    title[:title_w].ljust(title_w) if reserve else title[:title_w],
                ),
            ]
            if reserve:
                ts = timer_strs.get(tid, "")
                # Blank (spaces) for a non-running row keeps the row within width
                # without misaligning the running rows' timers. The timer is clipped
                # to the reserved column so an over-long value can never overflow.
                row.append(
                    (
                        "class:tui.forge.timer",
                        " " + ts[:timer_w].rjust(timer_w) if ts else " " * (timer_w + 1),
                    )
                )
            rows.append(row)
    else:
        rows.append([("class:tui.forge.idle", "  (no execution-ready tasks)"[: max(0, width)])])

    # Phase / spinner status line.
    message = str(view.get("message") or "")
    if done:
        # Colour the summary by the run outcome so a green ✓ never sits next to a
        # failed task: green ✓ on a clean run, red ✗ when anything failed/remains.
        ok = bool(view.get("ok", True))
        glyph = "✓" if ok else "✗"
        gstyle = "class:tui.forge.done" if ok else "class:tui.forge.fail"
        line = (message or ("Done." if ok else "Finished with issues."))[: max(0, width - 2)]
        rows.append([(gstyle, f"{glyph} "), ("class:tui.transcript.system", line)])
    else:
        phase = str(view.get("phase") or "execute")
        running = len(active_map)
        lead = f"{running} running · " if running > 1 else ""
        text = f"{lead}{phase}" if not message else f"{lead}{phase} · {message}"
        if elapsed:
            text = f"{text} · {elapsed}s"
        text = text[: max(0, width - 2)]
        rows.append(
            [("class:tui.forge.run", f"{spinner} "), ("class:tui.transcript.reasoning", text)]
        )
    return rows


# ------------------------------------------------------------- float geometry
# Every popup renders pre-wrapped, pre-padded rows into a Window(wrap_lines=True).
# That only holds together while the width a row builder used equals the width
# prompt_toolkit actually paints into: a row even ONE column too wide is
# hard-character-wrapped to column 0 — mid-word, straight through the aligned
# label/description columns — and the doubled screen rows overflow the float's
# height, clipping the bottom (hint lines, key legends) off the box.
#
# So each float's content width is derived HERE from that float's OWN outer width
# minus its OWN chrome. Never borrow a sibling's formula: the popups are
# deliberately different widths, and a borrowed one is wrong at every size.
_FRAME_COLS = 2  # Frame draws one border column on each side
_SCROLLBAR_COLS = 1  # ScrollbarMargin takes one column away from the content


def _picker_width_for(cols: int) -> int:
    """Outer width of the picker float on a ``cols``-wide terminal."""
    return max(40, min(int(cols) - 8, 76))


def _picker_content_width_for(cols: int) -> int:
    """Width the picker's rows must render to (framed; no margins)."""
    return max(20, _picker_width_for(cols) - _FRAME_COLS)


def _help_panel_width_for(cols: int) -> int:
    """Outer width of the help/panel float.

    Wide side margins so the panel clearly floats over the app rather than
    filling the screen, and a cap so it is never absurdly wide.
    """
    return max(38, min(int(cols) - 8, 86))


def _help_content_width_for(cols: int) -> int:
    """Width the help/panel rows must render to (framed AND scrollable)."""
    return max(20, _help_panel_width_for(cols) - _FRAME_COLS - _SCROLLBAR_COLS)


def _approval_width_for(cols: int) -> int:
    """Outer width of the approval modal."""
    return max(40, min(int(cols) - 8, 72))


def _approval_content_width_for(cols: int) -> int:
    """Width the approval modal's rows must render to (framed; no margins)."""
    return max(20, _approval_width_for(cols) - _FRAME_COLS)


def _transcript_content_width_for(cols: int) -> int:
    """Width the transcript's rows must render to.

    The transcript is the full-width pane (no frame) but carries a scrollbar
    margin, so it is one column narrower than the terminal.
    """
    return max(1, int(cols) - _SCROLLBAR_COLS)


# ----------------------------------------------------------------- /help popup
HelpSection = tuple[str, list[tuple[str, str]]]

_HELP_HINT = "↑↓ / wheel scroll · End bottom · Esc close"


def _clip_cell(text: str, limit: int) -> str:
    """Hard-clip ``text`` to ``limit`` columns with an ellipsis.

    Guarantees the layout contract every panel builder shares: an emitted row
    must be exactly the panel width (the cursor-pin scroll math relies on it, and
    an over-wide row re-wraps to column 0), so a label longer than its column is
    clipped rather than allowed to push the row out.
    """
    limit = max(0, limit)
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return text[: limit - 1] + "…"


def _fit_label_column(label_w: int, width: int, reserved: int) -> int:
    """Shrink a left label column so the row still fits ``width``.

    Flooring the *right* column instead (``max(10, width - label_w - gap)``) is
    the trap: on a narrow panel it silently emits rows wider than the window,
    which then hard-wrap. The label column is the one that must give way — it is
    clipped, whereas the right column wraps.

    Capped at half the remaining room so one long command/key can never squeeze
    the descriptions down to a few columns and shred them one letter per line.
    At normal widths the label cap (30 / 22) binds first, so this changes nothing.
    """
    room = max(0, int(width) - int(reserved))
    return max(0, min(int(label_w), room // 2))


def _fallback_help_sections() -> list[HelpSection]:
    """Static command list used if the canonical grouped help can't be imported."""
    try:
        from ..chat_slash_completer import get_chat_specs

        return [("Commands", [(spec.usage, spec.description) for spec in get_chat_specs()])]
    except Exception:
        return [
            ("Commands", [("/help", "Show available commands"), ("/exit", "Exit chat")]),
        ]


def _help_rows_for_sections(sections: list[HelpSection], width: int) -> list[list[tuple[str, str]]]:
    """Render help ``sections`` into padded fragment rows for the popup.

    Each command-row is ``<green command>  <dim description>`` with the commands
    left-aligned in a shared column so they line up and read easily; long
    descriptions wrap with a hanging indent under the description column. Every
    row is padded to ``width`` so the panel background fills as a solid block.
    """
    width = max(20, int(width))
    pad = "class:tui.help"
    cmd_style = "class:tui.help.cmd"
    desc_style = "class:tui.help.desc"
    sec_style = "class:tui.help.section"
    hint_style = "class:tui.help.hint"

    all_cmds = [cmd for _name, rows in sections for cmd, _desc in rows]
    gap = 2
    cmd_w = _fit_label_column(min(max((len(c) for c in all_cmds), default=0), 30), width, gap)
    desc_w = max(1, width - cmd_w - gap)

    def _pad(fragments: list[tuple[str, str]], used: int) -> list[tuple[str, str]]:
        return [*fragments, (pad, " " * max(0, width - used))]

    out: list[list[tuple[str, str]]] = []
    for index, (name, rows) in enumerate(sections):
        if index > 0:
            out.append([(pad, " " * width)])  # blank row between sections
        header = _clip_cell(name, width)
        out.append(_pad([(sec_style, header)], len(header)))
        for cmd, desc in rows:
            chunks = _wrap_line(desc, desc_w)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    out.append(
                        _pad(
                            [
                                (cmd_style, _clip_cell(cmd, cmd_w).ljust(cmd_w)),
                                (pad, " " * gap),
                                (desc_style, chunk),
                            ],
                            cmd_w + gap + len(chunk),
                        )
                    )
                else:
                    out.append(
                        _pad(
                            [(pad, " " * (cmd_w + gap)), (desc_style, chunk)],
                            cmd_w + gap + len(chunk),
                        )
                    )
    out.append([(pad, " " * width)])
    # Wrap (never emit raw): the hint is 42 columns and would overflow — and thus
    # re-wrap to column 0 — on any narrower panel. Both sibling builders wrap theirs.
    for segment in str(_HELP_HINT).split("\n"):
        for hline in _wrap_line(segment, width):
            out.append(_pad([(hint_style, hline)], len(hline)))
    return out


# A key/value panel section: a header plus ``(key, value, tone)`` rows. ``tone``
# selects the value colour: "accent" (on/healthy, green), "plain" (neutral),
# "warn" (amber), "err" (red), "dim" (muted). Anything else falls back to plain.
PanelSection = tuple[str, list[tuple[str, str, str]]]

_PANEL_TONE_STYLE = {
    "accent": "class:tui.help.accent",
    "plain": "class:tui.help.desc",
    "warn": "class:tui.help.warn",
    "err": "class:tui.help.err",
    "dim": "class:tui.help.key",
}


def _render_kv_panel_rows(
    sections: list[PanelSection], width: int, hint: str = _HELP_HINT
) -> list[list[tuple[str, str]]]:
    """Render key/value ``sections`` into padded fragment rows for a popup panel.

    Each row is ``<dim key>  <toned value>`` with the keys left-aligned in a shared
    column so they line up; long values wrap with a hanging indent under the value
    column. Section headers use the same bright style as /help, a blank row sits
    between sections, and a dim hint closes the panel. Every row is padded to
    ``width`` so the panel background fills as a solid block — exactly like
    :func:`_help_rows_for_sections`, so both feed the one reusable panel overlay.
    """
    width = max(20, int(width))
    pad = "class:tui.help"
    key_style = "class:tui.help.key"
    sec_style = "class:tui.help.section"
    hint_style = "class:tui.help.hint"
    indent = 2

    all_keys = [key for _name, rows in sections for key, _val, _tone in rows]
    gap = 2
    key_w = _fit_label_column(
        min(max((len(key) for key in all_keys), default=0), 22), width, indent + gap
    )
    val_w = max(1, width - indent - key_w - gap)

    def _pad(fragments: list[tuple[str, str]], used: int) -> list[tuple[str, str]]:
        return [*fragments, (pad, " " * max(0, width - used))]

    _clip = _clip_cell
    header_w = max(0, width - indent)
    out: list[list[tuple[str, str]]] = []
    for index, (name, rows) in enumerate(sections):
        if index > 0:
            out.append([(pad, " " * width)])  # blank row between sections
        header = _clip(name, header_w)
        out.append(_pad([(pad, " " * indent), (sec_style, header)], indent + len(header)))
        for key, value, tone in rows:
            val_style = _PANEL_TONE_STYLE.get(tone, "class:tui.help.desc")
            key_cell = _clip(key, key_w).ljust(key_w)
            chunks = _wrap_line(str(value), val_w)
            for i, chunk in enumerate(chunks):
                if i == 0:
                    out.append(
                        _pad(
                            [
                                (pad, " " * indent),
                                (key_style, key_cell),
                                (pad, " " * gap),
                                (val_style, chunk),
                            ],
                            indent + key_w + gap + len(chunk),
                        )
                    )
                else:
                    out.append(
                        _pad(
                            [(pad, " " * (indent + key_w + gap)), (val_style, chunk)],
                            indent + key_w + gap + len(chunk),
                        )
                    )
    out.append([(pad, " " * width)])
    if hint:
        # Honour explicit line breaks, then wrap (never clip) so a long hint is
        # never cut off at the right edge.
        for segment in str(hint).split("\n"):
            for hline in _wrap_line(segment, width):
                out.append(_pad([(hint_style, hline)], len(hline)))
    return out


def _render_doc_panel_rows(
    text: str,
    width: int,
    hint: str = _HELP_HINT,
    *,
    theme: TerminalTheme = "neutral",
) -> list[list[tuple[str, str]]]:
    """Render freeform document text into rows for the centered popup panel.

    Markdown text (PLAN.md, a README preview, …) is rendered through the same Rich
    pipeline as assistant replies — headings, lists, tables, syntax-highlighted
    code — so ``/plan markdown`` reads like a real document instead of a pager
    dump. Non-markdown text falls back to plain wrapped lines. Every row is
    ``<= width`` so the cursor-pin scroll math (which counts logical rows) stays
    exact; the popup window's dark background fills the rest of each row.
    """
    width = max(20, int(width))
    pad = "class:tui.help"
    body_style = "class:tui.help.desc"
    hint_style = "class:tui.help.hint"
    out: list[list[tuple[str, str]]] = []
    md_rows = render_markdown_rows(text, width, theme)
    if md_rows is not None:
        # render_markdown_rows is memoized and returns cache-owned lists; copy each
        # inner row so this panel can never mutate a shared cache entry in place.
        out.extend([list(row) for row in md_rows])
    else:
        for line in text.split("\n") or [""]:
            chunks = _wrap_line(line, width)
            for segments in link_segments(line, chunks):
                out.append(link_fragments(body_style, segments))
    if not out:
        out.append([(body_style, "(empty)")])
    out.append([(pad, " " * width)])
    if hint:
        for segment in str(hint).split("\n"):
            for hline in _wrap_line(segment, width):
                out.append([(hint_style, hline)])
    return out


# A picker row: a selectable option with a label, a one-line description, the
# value passed to the select callback, whether it is the current setting, and an
# optional "tag" override (e.g. "(default)"; empty string suppresses the tag).
PickerRow = dict[str, Any]

_PICKER_HINT = "↑↓ move · 1-9 pick · Enter select · Esc cancel"


# Most lines a single picker option's description may wrap to before it is capped
# with an ellipsis (so a long blurb takes a few lines, never ten).
_PICKER_MAX_DESC_LINES = 3


def _render_picker_rows(
    rows: list[PickerRow], index: int, width: int, hint: str = _PICKER_HINT, numbered: bool = True
) -> list[list[tuple[str, str]]]:
    """Render a numbered, selectable option list for the picker popup.

    Each option is ``› N. label   description`` with the descriptions aligned in a
    shared column; a description that does not fit on one line **wraps** onto
    continuation lines (indented under the column), capped at
    :data:`_PICKER_MAX_DESC_LINES` so it never grows unbounded. The focused row
    (``index``) is painted with the band background and an accent caret/label
    across all its lines, and the active setting is tagged "(current)" — unless
    the row carries a ``tag`` key, which overrides the tag text (an empty string
    suppresses it entirely). Every row is padded to ``width`` so the highlight
    band stays aligned.

    Rows may carry a ``kind``: "header" paints a dim upper-case section caption
    and "spacer" a blank band line — both flush-left, unnumbered, unselectable.
    With ``numbered=False`` the ``N.`` prefixes are dropped (grouped menus
    navigate by arrows, not digits). A row ``tone`` of "warn" tints the
    description so problem summaries stand out without stealing the selection
    band.
    """
    width = max(20, int(width))
    gap = 2

    def _kind(r: PickerRow) -> str:
        return str(r.get("kind", "item") or "item")

    items = [r for r in rows if _kind(r) == "item"]
    num_w = max((len(f"{i + 1}. ") for i in range(len(items))), default=3) if numbered else 0
    label_w = max((len(str(r.get("label", ""))) for r in items), default=0)
    # Aligned description column = caret + number + widest label + gap, capped so a
    # very long label / narrow panel still leaves room for the description.
    desc_col = min(2 + num_w + label_w + gap, max(12, width - 12))
    desc_w = max(6, width - desc_col)
    label_room = max(1, desc_col - 2 - num_w - gap)
    if not any(
        row.get("description") or (row.get("tag") if "tag" in row else row.get("current"))
        for row in items
    ):
        # A menu without descriptions needs no reserved description column.
        # Let action labels use the available width, especially on small TTYs.
        desc_col = width
        label_room = max(1, width - 2 - num_w)

    out: list[list[tuple[str, str]]] = []
    item_no = 0
    for i, row in enumerate(rows):
        kind = _kind(row)
        if kind == "spacer":
            out.append([("class:tui.picker", " " * width)])
            continue
        if kind == "header":
            text = str(row.get("label", "")).upper()
            if len(text) > width:
                text = text[: max(1, width - 1)] + "…"
            out.append(
                [
                    ("class:tui.picker.header", text),
                    ("class:tui.picker", " " * max(0, width - len(text))),
                ]
            )
            continue
        item_no += 1
        sel = i == index
        base = "class:tui.picker.sel" if sel else "class:tui.picker"
        caret_style = "class:tui.picker.selcaret" if sel else "class:tui.picker.num"
        num_style = "class:tui.picker.selnum" if sel else "class:tui.picker.num"
        label_style = "class:tui.picker.sellabel" if sel else "class:tui.picker.label"
        if sel:
            desc_style = "class:tui.picker.seldesc"
        elif str(row.get("tone", "") or "") == "warn":
            desc_style = "class:tui.picker.warndesc"
        else:
            desc_style = "class:tui.picker.desc"
        tag_style = "class:tui.picker.seltag" if sel else "class:tui.picker.tag"

        num = f"{item_no}. " if numbered else ""
        label = str(row.get("label", ""))
        if len(label) > label_room:  # only trips on a very narrow panel
            label = label[: max(1, label_room - 1)] + "…"
        desc = str(row.get("description", "") or "")
        if "tag" in row:
            tag = str(row.get("tag") or "")
        else:
            tag = "(current)" if row.get("current") else ""

        desc_lines = _wrap_line(desc, desc_w) if desc else [""]
        if len(desc_lines) > _PICKER_MAX_DESC_LINES:
            desc_lines = desc_lines[:_PICKER_MAX_DESC_LINES]
            last = desc_lines[-1][: max(1, desc_w - 1)].rstrip()
            desc_lines[-1] = last + "…"
        # The tag rides the last description line when it fits; otherwise the
        # description's last word is pulled down with it so the tag reads as a
        # natural wrap — never a lone, misaligned "(current)"/"(default)" line.
        tag_line = -1
        if tag:
            last = desc_lines[-1]
            if not last or len(last) + 1 + len(tag) <= desc_w:
                tag_line = len(desc_lines) - 1
            else:
                cut = last.rfind(" ")
                if 0 < cut and (len(last) - cut - 1) + 1 + len(tag) <= desc_w:
                    desc_lines[-1] = last[:cut]
                    desc_lines.append(last[cut + 1 :])
                else:
                    desc_lines.append("")
                tag_line = len(desc_lines) - 1

        for li, dline in enumerate(desc_lines):
            frags: list[tuple[str, str]] = []
            if li == 0:
                pad_label = " " * max(0, desc_col - (2 + len(num) + len(label)))
                frags.append((caret_style, "› " if sel else "  "))
                frags.append((num_style, num))
                frags.append((label_style, label))
                frags.append((base, pad_label))
            else:
                frags.append((base, " " * desc_col))
            used = desc_col
            if dline:
                frags.append((desc_style, dline))
                used += len(dline)
            if li == tag_line and tag:
                tag_frag = f" {tag}" if dline else tag
                frags.append((tag_style, tag_frag))
                used += len(tag_frag)
            if used < width:
                frags.append((base, " " * (width - used)))
            out.append(frags)

    out.append([("class:tui.picker", " " * width)])
    if hint:
        # Honour explicit line breaks, then wrap (never clip) so a long hint — e.g.
        # the "auto-delegate off" note — is never cut off at the right edge.
        for segment in str(hint).split("\n"):
            for hline in _wrap_line(segment, width):
                out.append(
                    [
                        ("class:tui.picker.hint", hline),
                        ("class:tui.picker", " " * max(0, width - len(hline))),
                    ]
                )
    return out


# ------------------------------------------------------------- approval modal
_APPROVE_ACTION_LABELS = {
    "shell_run": "Run command",
    "fs_write": "Write files",
    "fs_delete": "Delete files",
}


# Rows the approval modal always spends on chrome: a leading blank, the headline,
# a blank, [body], a blank, the key legend, a trailing blank.
_APPROVAL_FIXED_ROWS = 6


def _approval_body_budget(screen_rows: int) -> int:
    """Body lines that still leave the ``[y]/[a]/[n]`` legend on screen.

    The modal deliberately does not scroll, so any row past the window height is
    simply unreachable — and the legend sits at the BOTTOM. Without a budget a
    long command silently pushes it out and the user is asked to consent to
    something with no visible way to answer.
    """
    return max(2, int(screen_rows) - 4 - _FRAME_COLS - _APPROVAL_FIXED_ROWS)


def _elide_middle(lines: list[str], budget: int) -> list[str]:
    """Trim ``lines`` to ``budget`` rows, dropping the MIDDLE and saying so.

    Both ends are kept on purpose: this renders shell commands for a consent
    prompt, where the part that matters is as likely to be at the end
    (``… && rm -rf /``) as at the start, so truncating the tail could hide
    exactly what the user needs to see before approving.
    """
    if budget <= 0:
        return []
    if len(lines) <= budget:
        return lines
    hidden = len(lines) - budget + 1
    head = budget // 2
    tail = budget - 1 - head
    kept_tail = lines[len(lines) - tail :] if tail > 0 else []
    return [*lines[:head], f"… {hidden} more lines hidden …", *kept_tail]


def _approval_action_label(kind: str) -> str:
    if kind.startswith("custom_tool_run:"):
        return "Run custom tool"
    return _APPROVE_ACTION_LABELS.get(kind, f"Approve {kind}")


def _approval_is_destructive(kind: str, command: str) -> bool:
    if kind == "fs_delete":
        return True
    lowered = f" {command.lower()} "
    return any(token in lowered for token in (" rm ", "rm -", "force", "delete"))


def _render_approval_rows(
    request: Any, width: int, *, max_body_lines: int | None = None
) -> list[list[tuple[str, str]]]:
    """Render the approval modal body: a headline (amber, red if destructive), the
    target command/files (bright), the reason (dim), and the colour-coded keys.

    ``max_body_lines`` bounds the target/reason block so the key legend below it
    stays inside the (unscrollable) modal; see :func:`_approval_body_budget`.
    ``None`` leaves the body unbounded.
    """
    width = max(20, int(width))
    kind = str(getattr(request, "kind", "") or "")
    command = str(getattr(request, "command", "") or "")
    files = list(getattr(request, "files", []) or [])
    reason = str(getattr(request, "reason", "") or "")
    target = command or (", ".join(files) if files else kind)
    danger = _approval_is_destructive(kind, command)

    base = "class:tui.approve"
    head_style = "class:tui.approve.head.danger" if danger else "class:tui.approve.head"
    indent = 2
    inner = max(8, width - indent * 2)

    def _pad(fragments: list[tuple[str, str]], used: int) -> list[tuple[str, str]]:
        return [*fragments, (base, " " * max(0, width - used))]

    out: list[list[tuple[str, str]]] = [[(base, " " * width)]]
    head = f"⚠ {_approval_action_label(kind)}"
    out.append(_pad([(base, " " * indent), (head_style, head)], indent + len(head)))
    out.append([(base, " " * width)])
    target_lines = _wrap_line(target, inner)
    reason_lines = _wrap_line(reason, inner) if reason else []
    if max_body_lines is not None:
        # The target is what is being consented to, so it keeps the lion's share;
        # the reason is capped at a third and elided first.
        if reason_lines:
            reason_lines = _elide_middle(reason_lines, max(1, max_body_lines // 3))
        target_lines = _elide_middle(target_lines, max(1, max_body_lines - len(reason_lines)))
    for chunk in target_lines:
        out.append(
            _pad([(base, " " * indent), ("class:tui.approve.target", chunk)], indent + len(chunk))
        )
    for chunk in reason_lines:
        out.append(
            _pad([(base, " " * indent), ("class:tui.approve.reason", chunk)], indent + len(chunk))
        )
    out.append([(base, " " * width)])
    # Folder-wide grant: offered only when the request's files share one
    # non-root directory and the approval may widen (never sensitive prompts).
    from ...approval_scope import approval_dir_grant_candidate

    dir_candidate = approval_dir_grant_candidate(request)
    opts: list[tuple[str, str]] = [
        (base, " " * indent),
        ("class:tui.approve.key.yes", "[y]"),
        ("class:tui.approve.optlabel", " yes   "),
        ("class:tui.approve.key.always", "[a]"),
        ("class:tui.approve.optlabel", " always   "),
    ]
    used_text = "[y] yes   [a] always   "
    if dir_candidate is not None:
        opts.extend(
            [
                ("class:tui.approve.key.always", "[d]"),
                ("class:tui.approve.optlabel", " folder   "),
            ]
        )
        used_text += "[d] folder   "
    opts.extend(
        [
            ("class:tui.approve.key.no", "[n]"),
            ("class:tui.approve.optlabel", " no"),
        ]
    )
    used_text += "[n] no"
    out.append(_pad(opts, indent + len(used_text)))
    if dir_candidate is not None:
        hint = f"d = always allow {kind} under {dir_candidate}"[: max(10, width - indent)]
        out.append(
            _pad(
                [(base, " " * indent), ("class:tui.approve.reason", hint)],
                indent + len(hint),
            )
        )
    out.append([(base, " " * width)])
    return out


# Compatibility alias for tests and extensions that imported the original TUI
# private name before the token became shared with classic interactive chat.
_Cancellation = InteractiveCancellationToken


class _PlaceholderProcessor(Processor):
    """Show dim placeholder text while the input buffer is empty.

    ``text`` may be a string or a zero-arg callable resolved at render time (so
    the placeholder can change once a conversation is underway). The text carries
    a leading space so the cursor rests on a blank cell in front of it.
    """

    def __init__(
        self,
        text: str | Callable[[], str],
        style: str = "class:tui.placeholder",
    ) -> None:
        self._text = text
        self._style = style

    def _resolve(self) -> str:
        if callable(self._text):
            try:
                return self._text()
            except Exception:
                return ""
        return self._text

    def apply_transformation(self, ti: Any) -> Transformation:
        if ti.lineno == 0 and not ti.document.text:
            return Transformation([(self._style, self._resolve())])
        return Transformation(ti.fragments)


class _AttachmentChipProcessor(Processor):
    """Tint live attachment chips so a dropped file reads as one object.

    Restyling only — the buffer text is untouched, so cursor motion, selection
    and every offset the other processors compute stay exactly as they were.
    ``is_live`` keeps an unregistered look-alike (someone typing ``[Image #9]``
    by hand) rendering as the plain text it is.
    """

    def __init__(
        self,
        is_live: Callable[[str], bool],
        style: str = "class:tui.input.attachment",
    ) -> None:
        self._is_live = is_live
        self._style = style

    def apply_transformation(self, ti: Any) -> Transformation:
        line = fragment_list_to_text(ti.fragments)
        if "[" not in line:
            return Transformation(ti.fragments)
        spans = [
            match.span()
            for match in ATTACHMENT_TOKEN_RE.finditer(line)
            if self._is_live(match.group(0))
        ]
        if not spans:
            return Transformation(ti.fragments)
        fragments = explode_text_fragments(ti.fragments)
        for start, end in spans:
            for index in range(start, min(end, len(fragments))):
                fragment = fragments[index]
                fragments[index] = (f"{fragment[0]} {self._style}", *fragment[1:])
        return Transformation(fragments)


def _has_conversation(entries: list[tuple[str, str]]) -> bool:
    """True once a real turn exists in the transcript.

    Drives the welcome-landing ↔ chat split: the owl/wordmark landing stays up
    until the first *user/assistant* turn. Startup notices (the streaming-disabled
    warning, ``system``/``trace`` lines) are appended to the transcript at launch
    but must not count as a conversation, otherwise they would instantly dismiss
    the landing the moment the app opens.
    """
    return any(role in ("user", "assistant") for role, _ in entries)


def _duplicate_assistant_indices(
    entries: list[tuple[str, str]], streaming_index: int | None
) -> set[int]:
    """Redundant assistant blocks within a user turn. Hide a live block while
    it repeats the previous answer's prefix; if it diverges, show it in full.
    When the new answer includes all of the previous text, show the fuller
    answer once. Comparisons collapse whitespace and never cross user turns.
    """
    skip: set[int] = set()
    previous: str | None = None
    previous_index: int | None = None
    for index, (role, text) in enumerate(entries):
        if role == "user":
            previous = None
            previous_index = None
            continue
        if role != "assistant":
            continue
        normalized = " ".join(str(text or "").split())
        if not normalized:
            continue
        if previous == normalized or (
            streaming_index == index and previous is not None and previous.startswith(normalized)
        ):
            skip.add(index)
            continue
        if previous is not None and previous_index is not None and normalized.startswith(previous):
            # A fuller answer includes the previous candidate/partial answer.
            # Keep the complete text once, including any host-added notices.
            skip.add(previous_index)
        previous = normalized
        previous_index = index
    return skip


def _status_line_fragments(
    *,
    running: bool,
    notice: str = "",
    paste_hint: str = "",
    attachment_hint: str = "",
    selection_available: bool = False,
    input_pending: bool = False,
    queued_count: int = 0,
    step_staged_count: int = 0,
    turn_end_staged_count: int = 0,
    vim: bool = False,
) -> FormattedText:
    composer_hints = [hint.strip() for hint in (attachment_hint, paste_hint) if hint.strip()]
    if composer_hints:
        return FormattedText([("class:tui.status", "  " + "  ·  ".join(composer_hints))])
    # A notice answers something the user just did (copied text, clicked a
    # link), so it briefly outranks the standing mid-turn reminders.
    if notice.strip():
        return FormattedText([("class:tui.status", f"  {notice.strip()}")])
    if running:
        if input_pending:
            return FormattedText(
                [
                    (
                        "class:tui.status",
                        "  Enter to steer - Ctrl+Q to queue - "
                        + ("Ctrl+C" if vim else "Esc")
                        + " to interrupt",
                    )
                ]
            )
        if queued_count > 0 or step_staged_count > 0 or turn_end_staged_count > 0:
            label = "message" if queued_count == 1 else "messages"
            parts = []
            if step_staged_count:
                parts.append(f"{step_staged_count} applying next step")
            if turn_end_staged_count:
                parts.append(f"{turn_end_staged_count} at next message")
            if queued_count:
                parts.append(f"{queued_count} queued {label}")
            return FormattedText(
                [
                    (
                        "class:tui.status",
                        f"  {' - '.join(parts)} - "
                        + ("Ctrl+C" if vim else "Esc or Ctrl+C")
                        + " to interrupt",
                    )
                ]
            )
        return FormattedText(
            [("class:tui.status", "  " + ("Ctrl+C" if vim else "Esc or Ctrl+C") + " to interrupt")]
        )
    if selection_available:
        return FormattedText([("class:tui.status", "  ctrl+c to copy")])
    return FormattedText([])


def _pending_message_fragments(
    steer_messages: list[_PendingMessage],
    queued_messages: list[_PendingMessage],
    *,
    width: int,
) -> FormattedText:
    """Render pending messages as single clipped rows without changing payloads."""
    width = max(1, int(width))
    lines: list[tuple[str, str]] = []

    def clip_message(text: str) -> str:
        if len(text) <= width:
            return text
        if width <= 3:
            return "." * width
        return text[: width - 3] + "..."

    def append_line(style: str, text: str, *, clip: bool = True) -> None:
        if lines:
            lines.append(("", "\n"))
        lines.append((style, clip_message(text) if clip else text))

    if steer_messages:
        header = _PENDING_STEER_HEADER if len(steer_messages) == 1 else _PENDING_STEERS_HEADER
        append_line("class:tui.pending-messages.head", header, clip=False)
        for message in steer_messages:
            preview = " ".join(message.display_text.split())
            append_line("class:tui.pending-messages.row", f"  > {preview}")

    if queued_messages:
        append_line(
            "class:tui.pending-messages.head",
            _PENDING_QUEUED_HEADER,
            clip=False,
        )
        for message in queued_messages:
            preview = " ".join(message.display_text.split())
            append_line("class:tui.pending-messages.row", f"  > {preview}")

    if queued_messages:
        append_line("class:tui.pending-messages.hint", _PENDING_EDIT_HINT)

    return FormattedText(lines)


def run_tui(
    state: TuiState,
    *,
    owl_color: bool = True,
    input: Any | None = None,
    output: Any | None = None,
    session_builder: Callable[[TuiSurface], Any] | None = None,
    on_turn_complete: Callable[[], None] | None = None,
    on_hud_refresh: Callable[[], None] | None = None,
    command_runner: Callable[[Any, str, int], tuple[str, str, str | None, dict[str, Any] | None]]
    | None = None,
    background_turns: bool = True,
    help_sections: list[HelpSection] | None = None,
    panel_providers: dict[str, Callable[[], dict[str, Any] | None]] | None = None,
    picker_providers: dict[str, Callable[[], dict[str, Any] | None]] | None = None,
    persona_cycle: Callable[[], list[tuple[str, str]] | None] | None = None,
    persona_stage_target: Callable[[str | None], str | None] | None = None,
    mode_cycle: Callable[[], list[tuple[str, str]] | None] | None = None,
    before_turn: Callable[[Any, dict[str, Any]], Callable[[], None] | None] | None = None,
    step_operation_resolver: Callable[[Any, str], ResolvedOperation | None] | None = None,
    step_operation_apply: Callable[[Any, ResolvedOperation], None] | None = None,
    completer: Any | None = None,
    config_flow_factory: Callable[[], Any] | None = None,
    report_flow_factory: Callable[[Any, str], Any] | None = None,
    on_config_saved: Callable[[], ConfigReloadOutcome | bool | None] | None = None,
    unavailable_message: str | None = None,
    subscription_provider_id: str | None = None,
    open_config_on_start: bool = False,
    theme: TerminalTheme | None = None,
    initial_input_text: str = "",
    workspace_root: Path | None = None,
    workdir_provider: Callable[[], Path] | None = None,
    diff_viewer_factory: Callable[..., DiffViewer] | None = DiffViewer,
    review_engine_factory: Callable[[Path], Callable[..., Any]] | None = None,
    session_search_provider: Callable[[], Any] | None = None,
    resume_session: Callable[[str], bool] | None = None,
    local_lifecycle: TuiLocalActionLifecycle | None = None,
    history_persistence: bool = True,
    planning_backend: Any | None = None,
) -> tuple[Any, list[tuple[str, str]]]:
    """Run the full-screen TUI shell, optionally hosting a real agent session.

    ``initial_input_text`` prefills the chat input on startup — the chat loop
    passes text the user typed at the unpainted workspace-guard steps
    so it lands in the input box instead of being lost.

    ``workspace_root`` is the session's active workdir. It anchors composer
    attachments: a relative path given to ``/image`` resolves against it, and a
    clipboard image is written beneath it. It defaults to the process CWD.
    ``workdir_provider`` resolves the current directory for text-only @ references;
    the chat loop supplies the live session directory, including navigation changes.

    ``review_engine_factory(workspace)`` snapshots review configuration and returns
    a callable accepting a cancellation token. Only explicit Start invokes that
    callable, in a serialized worker; its dedicated engine is disposed after the
    provider call ends. Omit it to keep review unavailable on this surface.

    Returns ``(result, transcript)`` where ``result`` is the value passed to
    ``app.exit`` (an exit word, or ``None`` for Ctrl-C/Ctrl-D) and ``transcript``
    is the list of ``(role, text)`` entries shown in the pane.

    ``session_builder`` receives the :class:`TuiSurface` and returns an object
    exposing ``run_turn(text, *, cancellation_token=...)`` (an ``AgentSession``).
    When omitted, submissions show a static stub reply (Phase 1 behaviour).

    ``on_turn_complete`` is invoked (worker thread) once after each turn so the
    caller can refresh live HUD fields (tokens/cost/context) on ``state``.
    ``on_hud_refresh`` (optional) is the same idea but called *during* a turn at
    safe points (tool-end / message-done, throttled) so a long multi-step turn's
    footer numbers advance instead of staying frozen until the turn finishes; pass
    the same refresher used for ``on_turn_complete``.

    ``command_runner(session, text, width)`` routes a submission through the chat
    command handler and returns ``(action, output, instruction, run_kwargs)``:
    ``action`` is ``"exit"`` | ``"handled"`` | ``"run"``; ``output`` is captured
    text to show in the transcript; for ``"run"`` the turn uses ``instruction``
    (defaulting to ``text``) and ``run_kwargs``. When omitted, slash commands are
    sent to the agent as plain messages.

    ``before_turn(session, run_kwargs)`` runs synchronously after a submission has
    resolved to a real turn but before ``session.run_turn`` starts. Raising rejects
    that turn. It may return a cleanup callback, which runs exactly once after the
    turn or before an interrupted turn releases the UI. This is the safe boundary
    used to activate pending Permissions and enforce temporary turn scopes without
    mutating a turn that is already in flight.

    ``input``/``output`` are injectable so tests can drive the application with a
    pipe input and a dummy output. ``background_turns=False`` runs turns inline
    (used by tests for deterministic ordering).

    ``unavailable_message`` keeps the shell usable without constructing a model
    session: plain prompts show that blocker, while native commands such as
    ``/help`` and ``/config`` continue to work. The native ``/login`` picker returns
    a sentinel result so the caller can perform browser login outside the terminal's
    alternate screen. ``open_config_on_start`` opens the configuration overlay on
    first paint (used after a successful login when model/reasoning selection is
    still required).

    ``panel_providers`` maps a command name (e.g. ``"/status"``) to a callable
    ``provider(arg)`` returning a panel spec ``{"title", "hint"?, "sections"}``
    where ``sections`` is a list of :data:`PanelSection`, or ``None`` to decline.
    ``arg`` is the text after the command word. When a spec is returned the command
    is TUI-native (like ``/help``): it opens the centered popup over the
    FloatContainer instead of routing through ``command_runner`` and never echoes
    into the transcript. When the provider returns ``None`` (e.g. ``/usage hud on``,
    ``/config set …``) the submission falls through to ``picker_providers`` / the
    command runner so the typed form still applies.
    """
    terminal_theme = theme or detect_terminal_theme()
    owl = load_owl_animation(color_enabled=owl_color, theme=terminal_theme)
    transcript = TuiTranscript()
    paste_registry = _PasteRegistry()
    attachment_registry = AttachmentRegistry()
    attachment_root = Path(workspace_root) if workspace_root is not None else Path.cwd()
    prompt_history = PromptHistory(attachment_root, persistence=history_persistence)
    history_warning = {"shown": False}
    wheel_step_rows = _resolve_wheel_step_rows()

    # ---- turn/run state (mutated across the UI and one worker thread) ----
    running: dict[str, bool] = {"on": False}
    retiring: dict[str, bool] = {"on": False}
    turn_state_lock = threading.RLock()
    pending_turns: list[_PendingMessage] = []
    pending_steers: list[_PendingMessage] = []
    delivered_steers: list[_PendingMessage] = []
    pending_messages_lock = threading.RLock()
    recalled_pending: dict[str, bool | None] = {"queue": None}
    pending_operations: list[_DeferredOperation] = []
    draining: dict[str, bool] = {"on": False}
    spinner: dict[str, int] = {"i": 0}
    tip_state: dict[str, int] = {"turn_index": -1}
    tip_link_press: tuple[str, Point, Point] | None = None
    # A left press that landed on a transcript/popup link, as ``(url, content
    # point)``; the matching release opens it, any drag off it cancels.
    link_press: dict[str, tuple[str, Point] | None] = {"transcript": None, "panel": None}
    link_opened: dict[str, Any] = {"url": "", "at": 0.0}
    cancel_box: dict[str, _Cancellation | None] = {"token": None}
    turn_cleanup_box: dict[str, Callable[[], None] | None] = {"callback": None}
    approval_box: dict[str, Any] = {"event": None, "decision": None, "request": None}
    worker_box: dict[str, threading.Thread | None] = {"thread": None}
    # Scroll state: ``follow`` pins to the latest line; once the user scrolls up
    # it sticks at ``offset`` (top visible row) until they return to the bottom
    # (or send a message).
    scroll: dict[str, Any] = {"follow": True, "offset": 0}
    # ``started`` stamps the running turn so the status line can show elapsed
    # time; ``reasoning_expanded`` is a local expansion override (Alt+R).
    run_box: dict[str, float] = {"started": 0.0, "cancellation_started": 0.0}
    subagent_started_at: dict[str, float] = {}
    view: dict[str, bool] = {"reasoning_expanded": False, "planmeta_expanded": False}
    subagent_panel: dict[str, Any] = {
        "selected_run_id": "",
        "run_order": [],
        "cursors": {},
        "entries": {},
        "statuses": {},
        "lifecycles": {},
        "poll_failures": {},
        "last_poll": None,
        "tip_shown": False,
    }

    # Centered popup panel (the reusable /help recipe): ``on`` toggles the Float;
    # ``offset`` is the top visible row (driven through the same cursor-pin trick as
    # the transcript so a set scroll position sticks); ``title`` retitles the Frame;
    # ``builder`` is the active row-builder (``width -> rows``) so /help, /status and
    # every future panel share one overlay. ``n`` caches the rendered row count.
    # ``confirm`` (when set) is a command string a panel runs on Enter — the popup
    # then acts as a confirm dialog (e.g. the /forge intro: Enter enters Forge, Esc
    # cancels). It is None for ordinary read-only panels, where Enter just closes.
    help_box: dict[str, Any] = {
        "on": False,
        "offset": 0,
        "title": "Commands",
        "builder": None,
        "confirm": None,
        # Optional accent for the panel frame border: "forge" repaints it violet
        # (ties /show, /plan, the launch gate to the FORGE identity); None/"" is
        # the default green chrome shared by /help, /status, …
        "accent": None,
    }
    # ``rows`` keeps the last rendered popup rows so a click can find its link.
    help_rows: dict[str, Any] = {"n": 0, "rows": []}
    # Selectable picker popup (e.g. /permissions): ``on`` toggles the Float, ``index`` is
    # the focused row, ``rows`` the option list, ``on_select`` the apply callback
    # (value -> list[(role, text)] messages to echo). Shares the dark popup chrome.
    picker_box: dict[str, Any] = {
        "on": False,
        "index": 0,
        "title": "Select",
        "hint": _PICKER_HINT,
        "rows": [],
        "on_select": None,
    }
    # In-TUI editor float (e.g. /plan edit on plan.json): ``on`` toggles the Float,
    # ``on_save`` validates+persists the buffer (returns ``(ok, message)``), and
    # ``status`` shows the last save error inline so the user can fix and re-save.
    editor_box: dict[str, Any] = {"on": False, "title": "Edit", "on_save": None, "status": ""}
    drag_capture: dict[str, Any] = {
        "target": None,
        "direction": 0,
        "step": None,
        "generation": 0,
        "worker_generation": None,
        "early_mouse_handlers": None,
    }

    def _safe_invalidate() -> None:
        try:
            get_app().invalidate()
        except Exception:
            pass

    def _begin_drag_capture(target: str, step: Callable[[int], None] | None = None) -> None:
        nonlocal tip_link_press
        if target != "tip_link":
            tip_link_press = None
        drag_capture["generation"] += 1
        drag_capture.update({"target": target, "direction": 0, "step": step})
        if target == "tip_link":
            # A whole press/drag/release can arrive in one input batch, before
            # the capture float repaints. Capture those events immediately too.
            application = get_app()
            renderer = application.renderer
            previous = renderer.mouse_handlers
            captured = MouseHandlers()
            size = application.output.get_size()
            captured.set_mouse_handler_for_range(
                0, size.columns, 0, size.rows, _tip_link_mouse_event
            )
            renderer.mouse_handlers = captured
            drag_capture["early_mouse_handlers"] = (renderer, previous, captured)
        _safe_invalidate()

    def _stop_drag_capture() -> None:
        nonlocal tip_link_press
        tip_link_press = None
        early_handlers = drag_capture.pop("early_mouse_handlers", None)
        if early_handlers is not None:
            renderer, previous, captured = early_handlers
            # A repaint installs its own capture map; never restore over it.
            if renderer.mouse_handlers is captured:
                renderer.mouse_handlers = previous
        if drag_capture["target"] is None:
            return
        drag_capture["generation"] += 1
        drag_capture.update({"target": None, "direction": 0, "step": None})
        _safe_invalidate()

    def _set_drag_scroll_direction(direction: int) -> None:
        drag_capture["direction"] = max(-1, min(int(direction), 1))
        if not drag_capture["direction"] or drag_capture["target"] is None:
            return

        generation = int(drag_capture["generation"])
        if drag_capture["worker_generation"] == generation:
            return
        drag_capture["worker_generation"] = generation

        def _auto_scroll() -> None:
            while drag_capture["generation"] == generation:
                time.sleep(_DRAG_SCROLL_INTERVAL_SECONDS)
                if drag_capture["generation"] != generation:
                    break
                direction_now = int(drag_capture["direction"])
                step = drag_capture.get("step")
                if direction_now and callable(step):
                    step(direction_now)
                    _safe_invalidate()

        threading.Thread(target=_auto_scroll, daemon=True).start()

    def _reload_saved_config() -> ConfigReloadOutcome:
        outcome = ConfigReloadOutcome.APPLIED
        if on_config_saved is not None:
            try:
                callback_result = on_config_saved()
            except Exception:  # noqa: BLE001 - treat any reload error as a failure
                outcome = ConfigReloadOutcome.FAILED
            else:
                if isinstance(callback_result, ConfigReloadOutcome):
                    outcome = callback_result
                elif callback_result is False:
                    outcome = ConfigReloadOutcome.FAILED
        if outcome is ConfigReloadOutcome.FAILED:
            transcript.append(
                "error",
                "Configuration saved to disk, but the running session could not be "
                "reloaded - restart Alysis Code for the new settings to take effect.",
            )
        return outcome

    transcript.set_invalidate(_safe_invalidate)

    def _app_running() -> bool:
        try:
            return bool(getattr(get_app(), "is_running", False))
        except Exception:
            return False

    # ---- approval (centered modal popup; only reached when approvals are set to ask) ----
    def _approval_ui(request: Any) -> Any:
        if not _app_running():
            return ApprovalDecision(allow=False)
        event = threading.Event()
        approval_box["event"] = event
        approval_box["decision"] = None
        approval_box["request"] = request  # drives the approval Float
        _safe_invalidate()
        # Bounded wait so the worker leaves event.wait() if the app is torn down
        # (EOF/exception) before the user (or the exit hook) resolves it.
        while not event.wait(timeout=0.2):
            if not _app_running():
                approval_box["event"] = None
                approval_box["decision"] = None
                approval_box["request"] = None
                return ApprovalDecision(allow=False)
        decision = approval_box.get("decision") or ApprovalDecision(allow=False)
        approval_box["event"] = None
        approval_box["decision"] = None
        approval_box["request"] = None
        _safe_invalidate()
        return decision

    def _resolve_approval(*, allow: bool, always: bool = False, always_dir: bool = False) -> None:
        event = approval_box.get("event")
        if event is None:
            return
        approval_box["decision"] = ApprovalDecision(
            allow=allow, allow_for_session=always, allow_for_session_dir=always_dir
        )
        verdict = "allowed" if allow else "denied"
        transcript.append("trace", f"· approval {verdict}")
        event.set()
        _safe_invalidate()

    _approval_pending = Condition(lambda: approval_box.get("event") is not None)

    # ---- build the agent surface + session (eager; failure → caller falls back) ----
    surface: TuiSurface | None = None
    session: Any | None = None

    def _sync_delivered_steers() -> int:
        """Move FIFO messages whose inbox slots were drained into the transcript."""
        with pending_messages_lock:
            if session is None or not pending_steers:
                return 0
            inbox = steer_inbox_for(session)
            pending_count = inbox.pending_count() if inbox is not None else 0
            delivered_count = max(0, len(pending_steers) - pending_count)
            if not delivered_count:
                return 0
            delivered = pending_steers[:delivered_count]
            del pending_steers[:delivered_count]
            # Keep the display values until the turn settles.  The core can
            # restore drained steers after a failed model request, and the raw
            # inbox text alone is not enough to reconstruct paste-token labels.
            delivered_steers.extend(delivered)
        for message in delivered:
            transcript.append_user(message.display_text)
        return delivered_count

    def _restore_rolled_back_steers(messages: list[str]) -> None:
        """Re-track delivered steers that the core returned to its inbox."""

        restored: list[_PendingMessage] = []
        with pending_messages_lock:
            for text in messages:
                match_index = next(
                    (
                        index
                        for index, message in enumerate(delivered_steers)
                        if message.text == text
                    ),
                    None,
                )
                if match_index is None:
                    # No visible-output callback ran before rollback, so this
                    # message is still represented in ``pending_steers`` and
                    # has not been echoed yet.
                    continue
                message = delivered_steers.pop(match_index)
                restored.append(
                    _PendingMessage(
                        text=message.text,
                        display_text=message.display_text,
                        echoed=True,
                        pastes=message.pastes,
                    )
                )
            # SteerInbox.restore_front() places rolled-back messages ahead of
            # newer arrivals; mirror that chronology in the display metadata.
            pending_steers[:0] = restored
        _safe_invalidate()

    def _drain_undelivered_steers() -> list[_PendingMessage]:
        """Drain the steer inbox while preserving each TUI display value."""
        if session is None:
            return []
        inbox = steer_inbox_for(session)
        if inbox is None:
            return []
        with pending_messages_lock:
            undelivered = inbox.drain()
            tracked = pending_steers[: len(undelivered)]
            del pending_steers[: len(undelivered)]
        return [
            tracked[index]
            if index < len(tracked) and tracked[index].text == text
            else _PendingMessage(text=text, display_text=text)
            for index, text in enumerate(undelivered)
        ]

    def _set_active_subagent(name: str | None) -> None:
        # Called from worker/subagent threads on subagent start/end: pin (or
        # clear) the footer's "↪ <name>" badge so the user always knows a nested
        # agent is doing the work right now. Pinning a NAME requires a live turn
        # — an abandoned turn's parallel subagent threads outlive a soft
        # interrupt (their cancellation is thread-local to the turn worker) and
        # must not re-light the badge of an idle session. Clearing always wins.
        if name and (not running["on"] or retiring["on"]):
            return
        if name:
            subagent_started_at.setdefault(str(name), time.monotonic())
        else:
            subagent_started_at.clear()
        state.active_subagent = str(name or "")
        _safe_invalidate()

    def _set_active_subagents(names: tuple[str, ...]) -> None:
        if names and (not running["on"] or retiring["on"]):
            return
        _sync_subagent_started_at(subagent_started_at, names, now=time.monotonic())
        state.active_subagents = tuple(str(name) for name in names if str(name))
        state.active_subagent = state.active_subagents[-1] if state.active_subagents else ""
        _safe_invalidate()

    def _action_key_hint(action_id: str, *, paste: bool = False) -> str:
        return {
            "history": "ctrl+r",
            "reasoning": "alt+r",
            "diff": "ctrl+p",
            "paste.edit": "alt+p",
            "subagent.previous": "alt+left",
            "subagent.next": "alt+right",
            "subagent.close": "esc",
            "planmeta.toggle": "ctrl+o",
        }.get(action_id, "")

    def _register_subagent_run(event: SubagentStartEvent) -> None:
        run_id = str(event.subagent_run_id or "").strip()
        if not run_id:
            return
        order = subagent_panel["run_order"]
        if run_id not in order:
            order.append(run_id)
        subagent_panel["cursors"].setdefault(run_id, 0)
        subagent_panel["entries"].setdefault(run_id, [])
        subagent_panel["lifecycles"][run_id] = {
            "run_id": run_id,
            "subagent": str(event.name),
            "label": str(event.label or ""),
            "state": "running",
            "collected": False,
        }
        subagent_panel["statuses"][run_id] = {
            "run_id": run_id,
            "subagent": str(event.name),
            "label": str(event.label or ""),
            "state": "running",
            "elapsed_ms": 0,
            "steps_completed": 0,
            "workspace_view": str(event.workspace_view or "shared"),
        }
        if not subagent_panel["tip_shown"] and not subagent_panel["selected_run_id"]:
            transcript.append(
                "info", f"tip: {_action_key_hint('subagent.next')} to follow subagent work"
            )
            subagent_panel["tip_shown"] = True
        _safe_invalidate()

    def _subagent_lifecycle_changed(payload: dict[str, Any]) -> None:
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            return
        subagent_panel["lifecycles"][run_id] = dict(payload)
        status = subagent_panel["statuses"].get(run_id)
        if isinstance(status, dict):
            status["state"] = str(payload.get("state") or status.get("state") or "")
        _safe_invalidate()

    def _report_subagent_poll_failure(run_id: str, condition: str) -> None:
        transcript.append(
            "warn",
            f"Subagent panel refresh failed for run {run_id}: {condition}.",
        )
        _safe_invalidate()

    def _child_scheduler_replaced(reason: str) -> None:
        had_history = bool(
            subagent_panel["selected_run_id"]
            or subagent_panel["run_order"]
            or any(
                subagent_panel[key]
                for key in (
                    "cursors",
                    "entries",
                    "statuses",
                    "lifecycles",
                    "poll_failures",
                )
            )
        )
        subagent_panel["selected_run_id"] = ""
        subagent_panel["run_order"].clear()
        for key in (
            "cursors",
            "entries",
            "statuses",
            "lifecycles",
            "poll_failures",
        ):
            subagent_panel[key].clear()
        subagent_panel["last_poll"] = None
        if had_history:
            detail = " ".join(str(reason or "settings applied").split())
            transcript.append("info", f"subagent history cleared: {detail}")
        _safe_invalidate()

    if session_builder is not None:
        surface = TuiSurface(
            transcript,
            request_approval_ui=_approval_ui,
            # Mid-turn HUD refresher (throttled, worker-thread) so the footer's
            # context/tokens/cost advance while a long multi-step turn runs, not just
            # once it ends. Distinct from on_turn_complete so the end-of-turn hook
            # keeps its fire-once-per-turn contract.
            on_hud_refresh=on_hud_refresh,
            on_subagent_activity=_set_active_subagent,
            on_subagent_activities=_set_active_subagents,
            on_subagent_run_started=_register_subagent_run,
            before_visible_output=_sync_delivered_steers,
            on_steer_messages_restored=_restore_rolled_back_steers,
        )
        session = session_builder(surface)

        def _pending_operation_labels() -> list[str]:
            labels: list[str] = []
            pending_permissions = str(
                getattr(session, "pending_permissions_mode", "") or ""
            ).strip()
            if pending_permissions:
                labels.append(f"permissions: {pending_permissions}")
            operation_inbox = ops_inbox_for(session)
            if operation_inbox is not None:
                labels.extend(
                    str(operation.display_label or operation.kind).strip()
                    for operation in operation_inbox.snapshot()
                )
            for pending_operation in pending_operations:
                if pending_operation.kind is _DeferredOperationKind.COMMAND:
                    labels.append(deferred_display_label(pending_operation.text))
                else:
                    labels.append("config reload")
            return [label for label in labels if label]

        try:
            session.pending_operation_labels = _pending_operation_labels
        except Exception:  # noqa: BLE001 - slotted test sessions can opt out
            pass
        if step_operation_apply is not None:
            session.apply_staged_operation = lambda operation: step_operation_apply(
                session, operation
            )
        session.on_child_scheduler_replaced = _child_scheduler_replaced
        scheduler = getattr(session, "child_scheduler", None)
        set_lifecycle_listener = getattr(scheduler, "set_lifecycle_listener", None)
        if callable(set_lifecycle_listener):
            set_lifecycle_listener(_subagent_lifecycle_changed)
        # Seed the footer HUD from the freshly built session so the bottom-right
        # context/tokens/cost read accurately on the first paint, instead of a flat
        # "context 100%" until the first turn completes.
        if on_hud_refresh is not None:
            try:
                on_hud_refresh()
            except Exception:
                pass

    # ---- welcome body (owl + wordmark + hint), shown until first message ----
    # Laid out for the size it is drawn at, so a window smaller than full
    # screen reflows the text instead of clipping it.
    def _welcome_rows(width: int, height: int | None) -> list[StyleAndTextTuples]:
        setup_hint = (
            _model_access_setup_hint(subscription_provider_id) if state.connection_status else None
        )
        text_rows = _content.welcome_text_rows(width, setup_hint=setup_hint)
        rows: list[StyleAndTextTuples] = [[]]
        owl_ansi = owl.current_ansi()
        if owl_ansi is not None:
            owl_rows = list(split_lines(to_formatted_text(owl_ansi)))
            # Leave the owl out rather than clip it: in a pane narrower than
            # the owl, or too short to hold it above the wordmark and hint.
            needed = len(rows) + len(owl_rows) + 1 + len(text_rows)
            if owl.width <= width and (height is None or needed <= height):
                rows.extend(owl_rows)
                rows.append([])
        rows.extend(text_rows)
        return rows

    welcome_window = Window(_SizedRowsControl(_welcome_rows), align=WindowAlign.CENTER)

    # ---- transcript pane (pull-based; the worker only mutates the model) ----
    def _run_elapsed() -> int:
        return _activity_elapsed_seconds(
            turn_started=run_box["started"],
            cancellation_started=run_box["cancellation_started"],
            active_subagent=state.active_subagent,
            subagent_started_at=subagent_started_at,
            now=time.monotonic(),
        )

    _row_count = {"n": 0}
    selection: dict[str, Any] = {
        "anchor": None,
        "active": None,
        "dragging": False,
        "rows": [],
        # The same rows as fragments, which is where a clicked link's URL lives.
        "fragment_rows": [],
        "row_roles": [],
        "width": None,
        # True while a press that began on the plan-meta aside is in flight
        # (click on release = toggle; drag = select its text).
        "planmeta_press": False,
    }
    selection_notice: dict[str, Any] = {"text": "", "generation": 0}
    _role_styles = {
        "system": "class:tui.transcript.system",
        "trace": "class:tui.transcript.trace",
        "error": "class:tui.transcript.error",
        "warn": "class:tui.transcript.warn",
        "info": "class:tui.transcript.system",
        "subagent": "class:tui.transcript.subagent",
    }

    def _transcript_fragments() -> FormattedText:
        # The transcript renders before the pending area. Detect a worker-side
        # full-FIFO drain here so delivered user bands precede any model output
        # painted in the same frame.
        _sync_delivered_steers()
        entries, status, streaming_index = transcript.snapshot()
        reasoning_index, reasoning_secs = transcript.reasoning_snapshot()
        # Use the transcript's content width — the terminal less its scrollbar
        # margin — so full-width user bands never overflow into a wrapped extra
        # row. Derived, not read back from render_info, which lags a frame behind
        # a resize and is absent entirely on the first one.
        width = _transcript_content_width_for(_current_width())
        if selection["width"] not in {None, width}:
            link_press["transcript"] = None
            selection.update({"anchor": None, "active": None, "dragging": False})
        selection["width"] = width
        rendered_rows: list[list[tuple[str, str]]] = []
        rendered_row_roles: list[str] = []
        duplicate_assistant = _duplicate_assistant_indices(entries, streaming_index)
        future_copyable_entries = [False] * len(entries)
        seen_copyable_entry = False
        for entry_index in range(len(entries) - 1, -1, -1):
            future_copyable_entries[entry_index] = seen_copyable_entry
            entry_role = entries[entry_index][0]
            if entry_index not in duplicate_assistant and entry_role in _COPYABLE_TRANSCRIPT_ROLES:
                seen_copyable_entry = True
        for index, (role, text) in enumerate(entries):
            if index in duplicate_assistant:
                # A re-emitted verbatim copy of the same answer (multi-step turn):
                # render it once, drop the repeats.
                continue
            # A block builder may stamp per-row roles (planmeta lead vs wrap
            # continuation); everything else uses the entry role for all rows.
            block_roles: list[str] | None = None
            if role == "user":
                block = _user_band_rows(text, width)  # full-width highlighted band
            elif role == "assistant":
                # Markdown-render once the block is complete; keep the still-
                # streaming block plain so a half-open code fence never flickers.
                done = streaming_index != index
                block = _assistant_rows(text, width, markdown=done, theme=terminal_theme)
            elif role == "patch":
                from .tool_trace import patch_rows

                if transcript.trace_level == "off":
                    continue
                block = patch_rows(text, width, expanded=transcript.trace_level == "full")
            elif role == "tool_output":
                from .tool_trace import output_rows

                if transcript.trace_level != "full":
                    continue
                block = output_rows(text, width)
            elif role == "trace":
                from .tool_trace import trace_rows

                block = trace_rows(text, width)
            elif role == "planmeta":
                # Collapsible Forge planner meta / plan-reconciliation notes.
                # Rows carry per-row kinds: lead rows ("planmeta") own a
                # strippable glyph; wrap continuations ("planmetacont") start
                # with note content the copy path must keep verbatim.
                pairs = plan_meta_rows_with_kinds(text, width, expanded=view["planmeta_expanded"])
                block = [row for row, _kind in pairs]
                block_roles = [kind for _row, kind in pairs]
            elif role == "reasoning":
                is_live = reasoning_index == index
                expanded = view["reasoning_expanded"] or transcript.trace_level == "full"
                block = _reasoning_rows(
                    text,
                    width,
                    live=is_live,
                    secs=reasoning_secs.get(index, 0),
                    expanded=expanded,
                    spinner=_SPINNER_FRAMES[spinner["i"] % len(_SPINNER_FRAMES)] if is_live else "",
                    elapsed=_run_elapsed() if is_live else 0,
                )
            else:
                style = _role_styles.get(role, "")
                block = _plain_role_rows(style, text, width)
            rendered_rows.extend(block)
            rendered_row_roles.extend(
                block_roles if block_roles is not None else [role] * len(block)
            )
            # Blank spacer between blocks for readability (so the thinking aside
            # is not glued to the highlighted question box above it).
            if index != len(entries) - 1:
                rendered_rows.append([])
                # Preserve a single semantic paragraph break between copyable
                # entries even when one or more non-copyable trace/reasoning
                # blocks are displayed between them.
                rendered_row_roles.append(
                    "spacer"
                    if role in _COPYABLE_TRANSCRIPT_ROLES and future_copyable_entries[index]
                    else "chrome"
                )
        # Live Forge execution view (the task table + phase/spinner line) while
        # /execute plan runs the swarm — and it stays frozen as the final table once
        # the run completes (until the next submission clears it). It replaces the
        # generic activity indicator for the duration of a forge run.
        forge_view = transcript.forge_snapshot()
        if forge_view is not None:
            frame = _SPINNER_FRAMES[spinner["i"] % len(_SPINNER_FRAMES)]
            now_mono = time.monotonic()
            try:
                forge_elapsed = int(max(0, now_mono - float(forge_view["started"])))
            except Exception:
                forge_elapsed = 0
            # Per-task elapsed for the running rows (from the transcript's
            # active_map, stamped on the same monotonic clock the UI reads here).
            forge_task_elapsed: dict[str, int] = {}
            for _tid, _info in (forge_view.get("active_map") or {}).items():
                _started = _info.get("started") if isinstance(_info, dict) else None
                if _started is not None:
                    try:
                        forge_task_elapsed[str(_tid)] = int(max(0, now_mono - float(_started)))
                    except (TypeError, ValueError):
                        continue
            if entries:
                rendered_rows.append([])
                rendered_row_roles.append("chrome")
            forge_rows = _forge_view_rows(
                forge_view, width, frame, forge_elapsed, forge_task_elapsed
            )
            rendered_rows.extend(forge_rows)
            rendered_row_roles.extend(["forge"] * len(forge_rows))
        # Single live activity indicator under the content while the turn runs:
        # the model's current step — "thinking…" or the running tool name — with a
        # spinner + elapsed. Hidden while a reasoning block is itself live (it
        # carries its own header), while the answer streams, or while the forge view
        # is showing (it carries its own spinner). A blank line above gives it room.
        elif running["on"] and reasoning_index is None and streaming_index is None:
            frame = _SPINNER_FRAMES[spinner["i"] % len(_SPINNER_FRAMES)]
            label = status or "thinking…"
            if entries:
                rendered_rows.append([])
                rendered_row_roles.append("chrome")
            activity_rows = _activity_rows(
                frame,
                label,
                _run_elapsed(),
                elapsed_is_run_time=bool(state.active_subagent) and not retiring["on"],
            )
            rendered_rows.extend(activity_rows)
            rendered_row_roles.extend(["activity"] * len(activity_rows))
        rendered_rows = [pad_after_trailing_link(row, width) for row in rendered_rows]
        plain_rows = [fragment_list_to_text(row) for row in rendered_rows]
        selection["rows"] = plain_rows
        selection["fragment_rows"] = rendered_rows
        selection["row_roles"] = rendered_row_roles
        anchor = selection["anchor"]
        active = selection["active"]
        fragments: list[tuple[str, str]] = []
        for row_index, row in enumerate(rendered_rows):
            fragments.extend(
                _highlight_selection_in_row(
                    row,
                    row_index=row_index,
                    anchor=anchor,
                    active=active,
                )
            )
            fragments.append(("", "\n"))
        _row_count["n"] = len(rendered_rows)
        # While a small modal (approval / picker / help / editor) is up,
        # dim the transcript instead of blanking it — the appended class wins
        # the style merge, muting color while the text stays readable.
        try:
            modal_up = _small_modal_open()
        except Exception:
            modal_up = False
        if modal_up:
            fragments = [
                (f"{style} class:tui.dimbehind" if style else "class:tui.dimbehind", text)
                for style, text in fragments
            ]
        return FormattedText(fragments)

    def _last_row() -> int:
        return max(0, _row_count["n"] - 1)

    def _win_height() -> int:
        info = transcript_window.render_info
        return info.window_height if info is not None else 0

    def _follow_top() -> int:
        # Top visible row when pinned to the bottom (so the last screen shows).
        return max(0, _last_row() - max(0, _win_height() - 1))

    def _cursor_row() -> int:
        # We pin the Window's "cursor" to the TOP of the viewport (via the huge
        # bottom scroll-offset below), so vertical_scroll == this row. Following →
        # the bottom screen; otherwise the user's scroll position.
        if scroll["follow"]:
            return _follow_top()
        return max(0, min(int(scroll["offset"]), _last_row()))

    def _scroll_move(delta: int) -> None:
        current = _follow_top() if scroll["follow"] else int(scroll["offset"])
        scroll["offset"], scroll["follow"] = _scroll_target(current, _follow_top(), delta)

    def _transcript_drag_scroll(direction: int) -> None:
        link_press["transcript"] = None
        if drag_capture["target"] != "transcript":
            return
        before = _cursor_row()
        _scroll_move(direction)
        after = _cursor_row()
        active = selection.get("active")
        if after == before or active is None:
            return
        edge_row = after if direction < 0 else min(_last_row(), after + max(0, _win_height() - 1))
        selection["active"] = Point(x=active.x, y=edge_row)

    def _wheel_scroll(direction: int) -> None:
        link_press["transcript"] = None
        _scroll_move(direction * wheel_step_rows)
        _safe_invalidate()

    def _show_selection_notice(message: str, seconds: float = _COPY_NOTICE_SECONDS) -> None:
        selection_notice["generation"] += 1
        generation = selection_notice["generation"]
        selection_notice["text"] = message
        _safe_invalidate()

        def _clear_notice() -> None:
            time.sleep(seconds)
            if selection_notice["generation"] == generation:
                selection_notice["text"] = ""
                _safe_invalidate()

        threading.Thread(target=_clear_notice, daemon=True).start()

    def _copy_transcript_selection(selected: str) -> None:
        _show_selection_notice(_copy_selection_notice(selected))

    def _open_link(url: str) -> None:
        """Open a clicked link in the user's browser without blocking the UI."""
        now = time.monotonic()
        if url == link_opened["url"] and now - link_opened["at"] < _LINK_REOPEN_GUARD_SECONDS:
            return
        link_opened.update({"url": url, "at": now})
        # Name the address the browser actually gets (0.0.0.0 opens as localhost).
        target = browser_url(url)
        _show_selection_notice(f"Opening {_clip_cell(target, 72)}", _LINK_NOTICE_SECONDS)

        def _launch() -> None:
            try:
                # quiet: a browser helper must never write over the alt-screen.
                opened = open_url(target, quiet=True)
            except Exception:  # noqa: BLE001 - a failed launch must not kill the TUI
                opened = False
            if not opened:
                _show_selection_notice(_link_fallback_notice(target), _LINK_NOTICE_SECONDS)

        # Starting a browser can take seconds (powershell.exe from WSL).
        threading.Thread(target=_launch, daemon=True).start()

    def _transcript_link_at(point: Point) -> str | None:
        rows = selection.get("fragment_rows") or []
        if not 0 <= point.y < len(rows):
            return None
        try:
            # Behind a modal the transcript is dimmed and its links lose their
            # underline; they stop acting as links too.
            if _small_modal_open() or _config_open():
                return None
        except Exception:
            pass
        if point == Point(x=0, y=0) and _cursor_row() > 0:
            # prompt_toolkit reports a click on a blank row as (0, 0). With the
            # first row scrolled out of view that is the only way to get it.
            return None
        return link_at(rows[point.y], point.x)

    def _transcript_link_cell(point: Point) -> tuple[int, int] | None:
        """Screen cell showing transcript character ``point``, if visible."""
        info = transcript_window.render_info
        if info is None:
            return None
        return info._rowcol_to_yx.get((point.y, point.x))

    def _current_transcript_selection() -> str:
        anchor = selection["anchor"]
        active = selection["active"]
        if anchor is None or active is None:
            return ""
        return _selected_text(
            selection["rows"],
            anchor,
            active,
            row_roles=selection["row_roles"],
        )

    def _transcript_mouse_event(mouse_event: Any, *, captured: bool = False) -> Any:
        # ``captured``: projected from the drag-capture overlay's screen event
        # rather than mapped by the transcript window itself.
        event_type = mouse_event.event_type
        point = Point(x=max(0, mouse_event.position.x), y=max(0, mouse_event.position.y))
        if event_type == MouseEventType.MOUSE_DOWN and mouse_event.button == MouseButton.LEFT:
            # Remember whether the press began on the collapsible plan-meta
            # aside: a plain click (press+release without moving) toggles it
            # on release, same as Ctrl+O, while a DRAG selects its text like
            # any other transcript block. The row_roles list is indexed by the
            # same coordinate the selection code uses, so this is
            # click-accurate wherever the block is scrolled to.
            selection["planmeta_press"] = _plan_meta_press_hit(
                selection.get("row_roles") or [], point.y
            )
            # A press on a link opens it on release — unless it turns into a
            # drag, which selects the text like anywhere else.
            url = _transcript_link_at(point)
            link_press["transcript"] = (url, point) if url else None
            selection.update({"anchor": point, "active": point, "dragging": True})
            _begin_drag_capture("transcript", _transcript_drag_scroll)
            _safe_invalidate()
            return None
        if event_type == MouseEventType.MOUSE_MOVE and selection["dragging"]:
            press = link_press["transcript"]
            if not captured and press is not None and point != press[1]:
                # A move that beat the overlay's first paint: it is in the
                # press's own coordinates, and leaving the link cancels it.
                # Captured moves were settled on screen cells already.
                link_press["transcript"] = None
            selection["active"] = point
            _safe_invalidate()
            return None
        if event_type == MouseEventType.MOUSE_UP and selection["dragging"]:
            # Still pending only when the release beat the capture overlay's
            # first paint (see _captured_drag_mouse_event), so both events came
            # through this window and their content points compare directly.
            press = link_press["transcript"]
            link_press["transcript"] = None
            selection["active"] = point
            selection["dragging"] = False
            _stop_drag_capture()
            selected = _current_transcript_selection()
            if _plan_meta_click_toggles(
                pressed_planmeta=bool(selection.get("planmeta_press")),
                anchor=selection["anchor"],
                release=point,
                selected=selected,
            ):
                view["planmeta_expanded"] = not view["planmeta_expanded"]
            if not selected:
                selection.update({"anchor": None, "active": None})
                if press is not None and press[1] == point:
                    _open_link(press[0])
            selection["planmeta_press"] = False
            _safe_invalidate()
            return None
        return NotImplemented

    transcript_window = Window(
        _ScrollableControl(
            _transcript_fragments,
            focusable=False,
            show_cursor=False,
            get_cursor_position=lambda: Point(x=0, y=_cursor_row()),
            on_scroll=_wheel_scroll,
            on_mouse_event=_transcript_mouse_event,
        ),
        wrap_lines=True,
        # Huge bottom scroll-offset pins our "cursor" (the top visible row) to the
        # top of the viewport, so vertical_scroll tracks it 1:1 → exact, smooth
        # scrolling without cursor-visibility snap.
        scroll_offsets=ScrollOffsets(bottom=10**6),
        right_margins=[ScrollbarMargin(display_arrows=True)],
    )

    def _scroll_page_rows() -> int:
        height = _win_height()
        return max(1, height - 1) if height > 0 else 10

    # The welcome landing (owl + wordmark) stays up until the *conversation*
    # actually starts — the first user/assistant turn. Startup notices (the
    # streaming-disabled warning, system/trace lines) get appended to the
    # transcript too, but they must NOT dismiss the landing, so the welcome/chat
    # split keys on real turns rather than "any transcript entry". The notices
    # are still there and surface as soon as the first message is sent.
    def _conversation_started() -> bool:
        return _has_conversation(transcript.entries)

    has_messages = Condition(_conversation_started)

    def _live_paste_matches() -> list[re.Match[str]]:
        return paste_registry.live_matches(input_area.buffer.text)

    def _paste_hint_text() -> str:
        matches = _live_paste_matches()
        shortcut = _action_key_hint("paste.edit", paste=True) if matches else ""
        if len(matches) == 1:
            match = matches[0]
            return f"pasted #{match.group('id')} +{match.group('lines')} lines - {shortcut} to view"
        if len(matches) > 1:
            return f"{len(matches)} pasted blocks - cursor on one, {shortcut} to view"
        return ""

    has_live_paste_tokens = Condition(lambda: bool(_live_paste_matches()))

    def _live_attachment_matches() -> list[re.Match[str]]:
        return attachment_registry.live_matches(input_area.buffer.text)

    def _attachment_hint_text() -> str:
        return attachment_registry.hint(input_area.buffer.text)

    has_live_attachments = Condition(lambda: bool(_live_attachment_matches()))

    # Re-entrancy guard: inserting a chip fires on_text_changed, which must not
    # run the drop scan against the text the scan itself just wrote.
    attaching = {"on": False}

    def _attach_paths(paths: Sequence[Path | str], *, announce_errors: bool = True) -> int:
        """Register ``paths`` as chips in the composer; return how many landed.

        Whatever cannot be attached (a missing file, an oversized image) is
        reported inline and skipped, so one bad path in a multi-file drop never
        loses the good ones.
        """

        attached = 0
        attaching["on"] = True
        try:
            attached = _attach_paths_locked(paths, announce_errors=announce_errors)
        finally:
            attaching["on"] = False
        if attached:
            input_area.buffer.cancel_completion()
        _safe_invalidate()
        return attached

    def _attach_paths_locked(paths: Sequence[Path | str], *, announce_errors: bool) -> int:
        attached = 0
        for candidate in paths:
            try:
                attachment = attachment_registry.add_path(
                    candidate,
                    root=Path(workdir_provider()) if workdir_provider else attachment_root,
                )
            except AttachmentError as exc:
                if announce_errors:
                    transcript.append("warn", f"Could not attach: {exc}")
                continue
            buff = input_area.buffer
            before = buff.text[: buff.cursor_position]
            after = buff.text[buff.cursor_position :]
            # A chip is one word: never glue it onto what the user was typing,
            # and leave the cursor a space past it so the next keystroke (or the
            # next chip in a multi-file drop) starts cleanly.
            prefix = "" if not before or before[-1].isspace() else " "
            suffix = "" if after[:1].isspace() else " "
            buff.insert_text(f"{prefix}{attachment.token}{suffix}")
            attached += 1
        return attached

    def _chip_typed_drop() -> bool:
        """Chip a path a terminal typed in rather than sending as a paste.

        Not every terminal wraps a drag/drop in bracketed-paste markers, so the
        path can arrive one character at a time and never reach the paste
        handler. Only the shapes a terminal produces and a person writing a
        message does not — a fully quoted run, a ``file://`` URI — are taken.
        """

        if attaching["on"]:
            return False
        buff = input_area.buffer
        cursor = buff.cursor_position
        run = trailing_drop_run(buff.text[:cursor])
        if run is None:
            return False
        paths = detect_dropped_paths(run)
        if not paths:
            return False
        attaching["on"] = True
        try:
            buff.delete_before_cursor(count=len(run))
            attached = _attach_paths_locked(paths, announce_errors=False)
        finally:
            attaching["on"] = False
        if not attached:
            # Nothing landed (the file vanished between the scan and the add):
            # put the text back so the user still sees what they dropped.
            buff.insert_text(run)
            return False
        buff.cancel_completion()
        _safe_invalidate()
        return True

    def _attach_clipboard_image() -> None:
        """Ctrl+V / bare ``/image``: save the clipboard picture and chip it."""

        try:
            saved = paste_clipboard_image(
                root=Path(workdir_provider()) if workdir_provider else attachment_root,
                output_path=None,
            )
        except ClipboardError as exc:
            transcript.append("warn", f"Clipboard: {exc}")
            _safe_invalidate()
            return
        except Exception as exc:  # noqa: BLE001 - never crash the UI on a clipboard read
            transcript.append("error", f"Clipboard image failed: {exc}")
            _safe_invalidate()
            return
        _attach_paths([saved])

    # ---- status / working line (between transcript and input) ----
    def _status_text() -> FormattedText:
        # All "agent is working" feedback (thinking + running tool, with the one
        # timer) now lives in the transcript under the question via the live
        # activity indicator. This line only carries the interrupt reminder while
        # busy, so there is never a second timer here.
        operation_inbox = ops_inbox_for(session) if session is not None else None
        fragments = _status_line_fragments(
            running=bool(running["on"] and not retiring["on"]),
            notice=str(selection_notice["text"] or ""),
            paste_hint=_paste_hint_text(),
            attachment_hint=_attachment_hint_text(),
            selection_available=bool(_current_transcript_selection()),
            input_pending=bool(input_area.buffer.text.strip()),
            queued_count=len(pending_turns),
            step_staged_count=(
                operation_inbox.pending_count() if operation_inbox is not None else 0
            ),
            turn_end_staged_count=_pending_command_count(pending_operations),
        )
        return fragments

    status_window = Window(FormattedTextControl(_status_text, focusable=False), height=1)

    def _working_tip_visible() -> bool:
        return (
            running["on"] and not retiring["on"] and not _small_modal_open() and not _config_open()
        )

    def _tip_link_mouse_event(event: MouseEvent) -> None:
        """Complete a link click using screen coordinates, including captured drags."""
        press = tip_link_press
        if press is None:
            return
        url, content_position, screen_position = press
        if event.event_type == MouseEventType.MOUSE_MOVE and event.position == screen_position:
            return
        _stop_drag_capture()
        info = working_tip_window.render_info
        current_tip = working_tip(
            time.monotonic() - run_box["started"], turn_index=tip_state["turn_index"]
        )
        if (
            event.event_type == MouseEventType.MOUSE_UP
            and event.button == MouseButton.LEFT
            and event.position == screen_position
            and _working_tip_visible()
            and current_tip.url == url
            and info is not None
            and info._rowcol_to_yx.get((content_position.y, content_position.x))
            == (screen_position.y, screen_position.x)
        ):
            # Browser startup can block on some hosts; keep the live TUI responsive.
            threading.Thread(target=open_url, args=(url,), daemon=True).start()

    def _working_tip_text() -> FormattedText:
        tip = working_tip(time.monotonic() - run_box["started"], turn_index=tip_state["turn_index"])
        if tip_link_press is not None and tip.url != tip_link_press[0]:
            _stop_drag_capture()
        if tip.url is None:
            return FormattedText([("class:tui.status", f"  Tip: {tip.text}")])

        def _open_tip_link(event: MouseEvent) -> None:
            nonlocal tip_link_press
            info = working_tip_window.render_info
            # Use the renderer's mapping so wide characters and clipping have
            # the same hit positions as prompt_toolkit's own mouse handling.
            position = (
                info._rowcol_to_yx.get((event.position.y, event.position.x))
                if info is not None
                else None
            )
            if position is None:
                _stop_drag_capture()
                return
            screen_position = Point(x=position[1], y=position[0])
            if event.event_type == MouseEventType.MOUSE_DOWN and event.button == MouseButton.LEFT:
                tip_link_press = (tip.url, event.position, screen_position)
                _begin_drag_capture("tip_link")
            else:
                # A press and release can arrive before the capture overlay has
                # repainted. Route those events through the same click handler.
                _tip_link_mouse_event(
                    MouseEvent(screen_position, event.event_type, event.button, event.modifiers)
                )

        return FormattedText(
            [
                ("class:tui.status", "  Tip: "),
                ("class:tui.tip.link", tip.text, _open_tip_link),
                ("class:tui.status underline", f" {tip.url}", _open_tip_link),
            ]
        )

    working_tip_window = Window(
        FormattedTextControl(_working_tip_text, focusable=False),
        height=1,
        wrap_lines=False,
    )

    # ---- input box (multiline; Enter submits, Ctrl+J adds a line) ----
    # Grows from one row up to a few as the user adds lines (so a pasted/multi-line
    # prompt stays visible); empty it is one row, keeping the welcome centering.
    # Long lines word-wrap under a hanging indent (see composer.py).
    def _composer_width() -> int:
        # The input frame spans the terminal (its side spacers are 0 wide), so
        # each row holds the columns between the frame's two borders. Read from
        # the output size rather than render_info so a resize applies at once.
        return max(1, _current_width() - 2)

    def _placeholder_text() -> str:
        # Inside a Forge session the input is a plan editor — nudge the verbs.
        if getattr(state, "forge_mode", False):
            return " " + _content.INPUT_PLACEHOLDER_FORGE
        # Long greeting on the welcome screen; short follow-up once chatting.
        # (The Tab persona shortcut is advertised in the welcome HINT_TEXT,
        # outside the input box — a placeholder suffix wraps on narrow panes.)
        if _conversation_started():
            return " " + _content.INPUT_PLACEHOLDER_FOLLOWUP
        # The greeting only while it fits on one row (with the cursor's column
        # to spare). A narrower pane gets the short prompt instead of a
        # greeting folded over two rows that collapses on the first keystroke.
        greeting = " " + _content.INPUT_PLACEHOLDER
        if get_cwidth(COMPOSER_PROMPT + greeting) < _composer_width():
            return greeting
        return " " + _content.INPUT_PLACEHOLDER_FOLLOWUP

    def _complete_while_typing() -> bool:
        buffer = input_area.buffer
        if _paste_token_at_cursor(buffer.text, buffer.cursor_position) is not None:
            return False
        # A cursor resting inside "[Image #1]" is editing a chip, not typing a
        # command, so the slash dropdown stays shut.
        return _attachment_token_at(buffer.text, buffer.cursor_position) is None

    from prompt_toolkit.completion import DummyCompleter

    from ..file_mention_completer import ChatComposerCompleter, mention_at_cursor

    composer_completer = ChatComposerCompleter(
        completer if completer is not None else DummyCompleter(),
        workdir_provider=workdir_provider or (lambda: attachment_root),
    )
    input_area = TextArea(
        height=D(min=1, max=8),
        multiline=True,
        wrap_lines=True,
        style="class:tui.input",
        completer=composer_completer,
        complete_while_typing=Condition(_complete_while_typing),
        input_processors=[
            _PlaceholderProcessor(_placeholder_text),
            _AttachmentChipProcessor(attachment_registry.is_live_token),
            # Last: it pads rows to the rendered width (and draws the prompt).
            ComposerWrapProcessor(COMPOSER_PROMPT, prompt_style="class:tui.prompt"),
        ],
    )

    # PTK drops concurrent async-completer calls. Track the actual jobs so an
    # explicit Tab can retire an obsolete cursor request before starting anew.
    completion_jobs: set[asyncio.Task] = set()
    completion_request = {"generation": 0, "pending": False, "task": None}
    original_async_completer = input_area.buffer._async_completer

    async def tracked_async_completer(*args: Any, **kwargs: Any) -> None:
        task = asyncio.current_task()
        completion_jobs.add(task)
        try:
            await original_async_completer(*args, **kwargs)
        finally:
            completion_jobs.discard(task)

    input_area.buffer._async_completer = tracked_async_completer

    def invalidate_completion_request(_buffer: Any = None) -> None:
        completion_request["generation"] += 1
        completion_request["pending"] = False
        task = completion_request["task"]
        if task is not None and not task.done():
            task.cancel()
        completion_request["task"] = None

    input_area.buffer.on_text_changed += invalidate_completion_request
    input_area.buffer.on_cursor_position_changed += invalidate_completion_request
    original_cancel_completion = input_area.buffer.cancel_completion

    def cancel_composer_completion() -> None:
        invalidate_completion_request()
        original_cancel_completion()

    input_area.buffer.cancel_completion = cancel_composer_completion

    def request_explicit_completion() -> None:
        buff = input_area.buffer
        invalidate_completion_request()
        generation = completion_request["generation"]
        document = buff.document
        completion_request["pending"] = True
        owner = get_app()

        async def restart() -> None:
            try:
                # Let already-scheduled automatic jobs register before taking
                # ownership. Document/cursor events invalidate this waiter.
                await asyncio.sleep(0)
                jobs = tuple(completion_jobs)
                for job in jobs:
                    if not job.cancelling():
                        job.cancel()
                if jobs:
                    # One pending request waits asynchronously for real unwind;
                    # cancellation of this waiter must not interrupt old cleanup.
                    await asyncio.wait(jobs)
                if (
                    generation != completion_request["generation"]
                    or buff.document != document
                    or owner.is_done
                    or owner.current_buffer is not buff
                    or _small_modal_open()
                    or _config_open()
                    or retiring["on"]
                    or not _complete_while_typing()
                ):
                    return
                buff.complete_state = None
                buff.start_completion(select_first=True)
            finally:
                if generation == completion_request["generation"]:
                    completion_request["pending"] = False

        completion_request["task"] = owner.create_background_task(restart())

    action_dispatching = {"on": False}

    def _drop_orphaned_paste_entries(_buffer: Any) -> None:
        if action_dispatching["on"]:
            return
        # Tokens are deliberately plain text. If an internal edit mangles their
        # syntax, the token stops matching, its payload is dropped, and the
        # remaining text is submitted literally instead of being guessed back.
        # Attachment chips follow the same rule: a half-deleted "[Image #1]"
        # detaches the file rather than sending a file the user cannot see.
        paste_registry.retain_tokens(input_area.buffer.text)
        attachment_registry.retain_tokens(input_area.buffer.text)
        _chip_typed_drop()

    input_area.buffer.on_text_changed += _drop_orphaned_paste_entries

    if initial_input_text:
        # Startup-picker stash: text the user typed before the
        # workspace-guard steps painted arrives here, so their words start the
        # session in the input box instead of vanishing.
        try:
            input_area.text = str(initial_input_text)
            input_area.buffer.cursor_position = len(input_area.text)
        except Exception:  # noqa: BLE001 - prefill is best-effort
            pass
    welcome_visible = Condition(
        lambda: not _conversation_started() and input_area.buffer.complete_state is None
    )
    welcome_completion_blank = Condition(
        lambda: not _conversation_started() and input_area.buffer.complete_state is not None
    )

    # ---- turn execution ----
    def _current_width() -> int:
        try:
            return get_app().output.get_size().columns
        except Exception:
            return 80

    def _current_rows() -> int:
        try:
            return get_app().output.get_size().rows
        except Exception:
            return 24

    def _capture_ui_dispatch() -> Callable[[Callable[[], None]], None]:
        """Capture the owner before starting a bare background thread."""
        owner = get_app()
        loop = getattr(owner, "loop", None)
        owner_context = copy_context()

        def dispatch(callback: Callable[[], None]) -> None:
            def publish() -> None:
                if owner.is_running and not owner.is_done and not loop.is_closed():
                    callback()

            if loop is None or loop.is_closed() or not owner.is_running or owner.is_done:
                return
            try:
                loop.call_soon_threadsafe(lambda: owner_context.run(publish))
            except RuntimeError:
                # Teardown can close the loop after the checks above. Never
                # drain the UI-owned queue inline on a background worker.
                return

        return dispatch

    def _run_turn_blocking(
        instruction: str,
        run_kwargs: dict[str, Any],
        ui_dispatch: Callable[[Callable[[], None]], None] | None = None,
    ) -> None:
        my_token = cancel_box["token"]
        turn_cleanup = run_kwargs.pop("_alysis_turn_cleanup", None)
        # Tag this worker thread so the surface can drop its output if it gets
        # soft-interrupted (and keeps blocking on a slow model in the background).
        set_active_cancellation(my_token)
        failed = False
        worker_result = None
        worker_exception = None
        cancelled = my_token is not None and getattr(my_token, "is_cancelled", False)
        # Commands that perform blocking work can provide a deferred callable.
        # Pop it so private TUI orchestration never leaks into
        # session.run_turn(**run_kwargs). The callable shares the normal turn's
        # worker, cancellation token, HUD refresh, and teardown lifecycle.
        deferred_execute = run_kwargs.pop("_deferred_execute", None) if run_kwargs else None
        try:
            if callable(deferred_execute):
                worker_result = deferred_execute(my_token)
            else:
                from ...personas import (
                    persona_overlay_messages,
                    persona_overlay_user_messages,
                )

                effective_kwargs = dict(run_kwargs or {})
                persona_overlays = persona_overlay_messages(
                    cfg=getattr(session, "cfg", None),
                    persona=getattr(session, "persona", "code"),
                    registry=getattr(session, "persona_registry", None),
                )
                if persona_overlays:
                    effective_kwargs["ephemeral_system_messages"] = (
                        list(effective_kwargs.get("ephemeral_system_messages") or [])
                        + persona_overlays
                    )
                persona_user_overlays = persona_overlay_user_messages(
                    cfg=getattr(session, "cfg", None),
                    persona=getattr(session, "persona", "code"),
                    registry=getattr(session, "persona_registry", None),
                )
                if persona_user_overlays:
                    effective_kwargs["ephemeral_user_messages"] = persona_user_overlays + list(
                        effective_kwargs.get("ephemeral_user_messages") or []
                    )
                worker_result = session.run_turn(
                    instruction, cancellation_token=my_token, **effective_kwargs
                )
            if type(worker_result) is int:
                failed = worker_result != 0
                cancelled = cancelled or worker_result in {130, 143}
        except KeyboardInterrupt as exc:
            worker_exception = exc
            failed = True
            cancelled = True
            # A soft-interrupt already printed "Interrupted." from the key handler.
            if not (my_token is not None and my_token.is_cancelled):
                transcript.append("warn", "Interrupted.")
        except BaseException as exc:  # noqa: BLE001 - surface any failure inline
            worker_exception = exc
            failed = True
            cancelled = my_token is not None and my_token.is_cancelled
            if not cancelled:
                if isinstance(exc, LLMError) or is_network_or_model_error(str(exc)):
                    transcript.append("error", friendly_llm_error_message(exc))
                else:
                    transcript.append("error", f"{type(exc).__name__}: {exc}")
        finally:
            # Tool execution has actually returned. Permission cleanup can also
            # run on interrupt, so it is deliberately not our capture boundary.
            if local_lifecycle is not None:
                try:
                    local_lifecycle.finish_turn(
                        failed=failed or cancelled or bool(my_token and my_token.is_cancelled),
                        outcome=TurnOutcome(
                            worker_result,
                            worker_exception,
                            cancelled or bool(my_token and my_token.is_cancelled),
                        ),
                    )
                except Exception as exc:
                    transcript.append("error", f"Local action cleanup failed: {exc}")
                    worker_exception = worker_exception or exc
            if callable(turn_cleanup):
                turn_cleanup()
                if turn_cleanup_box.get("callback") is turn_cleanup:
                    turn_cleanup_box["callback"] = None
            # Only reset shared run state if we are still the active turn — a
            # soft-interrupt (or a newer turn) may have moved on while we were
            # blocked, and we must not stomp on its state when we finally unwind.
            with turn_state_lock:
                owns_active_turn = cancel_box.get("token") is my_token
            if owns_active_turn:
                if surface is not None:
                    surface.user_questions.reset()
                # Settle any final drain before discarding the per-turn display
                # metadata.  A rollback callback has already moved restored
                # messages back to ``pending_steers`` at this point.
                _sync_delivered_steers()
                with pending_messages_lock:
                    delivered_steers.clear()
                transcript.set_status(None)
                # The turn is over — no subagent can still be active. Idempotent
                # (the end event normally cleared it); catches error unwinds.
                state.active_subagent = ""
                state.active_subagents = ()
                if on_turn_complete is not None:
                    try:
                        on_turn_complete()
                    except Exception:
                        pass

                def finish_on_ui() -> None:
                    # Keep the turn busy until its ordered operations can drain
                    # in this same UI callback. Otherwise input can overtake a
                    # saved-config reload between worker exit and publication.
                    with turn_state_lock:
                        if cancel_box.get("token") is not my_token:
                            return
                        retiring["on"] = False
                        running["on"] = False
                        cancel_box["token"] = None
                    if background_turns:
                        _start_next_pending_turn()

                if background_turns:
                    if ui_dispatch is not None:
                        ui_dispatch(finish_on_ui)
                else:
                    finish_on_ui()
            _safe_invalidate()

    def _apply_idle_step_operations() -> int:
        """Apply resolved operations that reached the idle boundary undrained."""
        if session is None:
            return 0
        inbox = ops_inbox_for(session)
        if inbox is None:
            return 0
        apply_staged_operation = getattr(session, "apply_staged_operation", None)
        applied_count = 0
        for operation in inbox.drain():
            label = str(operation.display_label or operation.kind).strip()
            try:
                if not callable(apply_staged_operation):
                    raise RuntimeError("no staged-operation apply handler is attached")
                apply_staged_operation(operation)
            except Exception as exc:  # noqa: BLE001 - drop this op, keep the UI alive
                transcript.append("error", f"{label} - failed to apply: {exc}")
                continue
            transcript.append("system", f"{label} - applied")
            store = getattr(session, "store", None)
            append = getattr(store, "append", None)
            if callable(append):
                try:
                    append(
                        "staged_operation_applied",
                        {"kind": operation.kind, "step": 0},
                    )
                except Exception:
                    pass
            applied_count += 1
        return applied_count

    def _clear_pending_work(*, preserve_operations: bool = False) -> tuple[int, int, bool]:
        """Discard queued work, optionally preserving both staged-operation tiers."""
        _sync_delivered_steers()
        operation_count = 0 if preserve_operations else len(pending_operations)
        reload_pending = (not preserve_operations) and any(
            operation.kind is _DeferredOperationKind.CONFIG_RELOAD
            for operation in pending_operations
        )
        discarded = len(pending_turns) + operation_count
        if not preserve_operations:
            pending_operations.clear()
        pending_turns.clear()
        if session is not None:
            inbox = steer_inbox_for(session)
            if inbox is not None:
                discarded += len(inbox.drain())
        with pending_messages_lock:
            pending_steers.clear()
            delivered_steers.clear()
        return discarded, operation_count, reload_pending

    def _stop_pending_work_for_terminal_action() -> None:
        discarded, _operation_count, _reload_pending = _clear_pending_work()
        if discarded:
            label = "item" if discarded == 1 else "items"
            transcript.append(
                "warn",
                f"Discarded {discarded} pending {label} because this session is closing.",
            )
        _safe_invalidate()

    def _start_next_pending_turn() -> bool:
        """Apply ordered deferred operations, rescue steering, then start a turn."""
        if running["on"]:
            return False
        if local_lifecycle is not None and local_lifecycle.mutating:
            return False
        _sync_delivered_steers()
        _apply_idle_step_operations()
        while pending_operations and not running["on"]:
            operation = pending_operations.pop(0)
            if operation.kind is _DeferredOperationKind.COMMAND:
                dispatch_outcome = _dispatch_command(
                    operation.text,
                    echo=False,
                    allow_run=False,
                    deferred=True,
                )
                if dispatch_outcome is _DispatchOutcome.EXIT:
                    _stop_pending_work_for_terminal_action()
                    return False
                continue
            reload_outcome = _reload_saved_config()
            if reload_outcome is ConfigReloadOutcome.RESTART:
                _stop_pending_work_for_terminal_action()
                return False
        if session is not None:
            inbox = steer_inbox_for(session)
            if inbox is not None:
                rescued = _drain_undelivered_steers()
                if rescued:
                    # Append late steering after earlier queued work. Keep both
                    # queues bounded by returning overflow to the inbox; each
                    # completed turn frees one slot and retries the handoff.
                    capacity = max(0, MAX_PENDING_STEER_MESSAGES - len(pending_turns))
                    pending_turns.extend(rescued[:capacity])
                    pending_steers.extend(rescued[capacity:])
                    for deferred_message in rescued[capacity:]:
                        inbox.send(deferred_message.text)
        if not pending_turns or running["on"]:
            return False
        # Peek first and consume only after _begin_run accepts it. Popping before
        # the concurrency guard would lose the user's queued message on refusal.
        next_message = pending_turns[0]
        if _begin_run(
            next_message.text,
            {},
            notice="Running queued message.",
            user_display_text=None if next_message.echoed else next_message.display_text,
            image_paths=[
                os.fspath(attachment.path)
                for attachment in next_message.attachments
                if attachment.kind == "image"
            ],
        ):
            pending_turns.pop(0)
            return True
        return False

    def _begin_run(
        instruction: str,
        run_kwargs: dict[str, Any],
        *,
        notice: str = "",
        user_display_text: str | None = None,
        image_paths: Sequence[str] = (),
    ) -> bool:
        if local_lifecycle is not None and local_lifecycle.mutating:
            transcript.append(
                "system", "Local action is still finishing. The turn was not started."
            )
            return False
        if review_box["controller"] is not None and review_box["controller"].running:
            transcript.append(
                "system", "Close or cancel review before starting a conversation turn."
            )
            return False
        with turn_state_lock:
            turn_retiring = retiring["on"]
            turn_busy = running["on"] or turn_retiring
        if turn_busy:
            transcript.append(
                "warn",
                (
                    "A turn is still finishing before another can start."
                    if turn_retiring
                    else "A turn is already running."
                ),
            )
            _safe_invalidate()
            return False
        turn_cleanup: Callable[[], None] | None = None
        if session is not None and before_turn is not None:
            try:
                raw_cleanup = before_turn(session, run_kwargs)
                if raw_cleanup is not None:
                    cleanup_lock = threading.Lock()
                    cleanup_done = False

                    def _cleanup_once() -> None:
                        nonlocal cleanup_done
                        with cleanup_lock:
                            if cleanup_done:
                                return
                            cleanup_done = True
                            try:
                                raw_cleanup()
                            except Exception as exc:  # noqa: BLE001 - restoration is best-effort
                                transcript.append(
                                    "error",
                                    f"Could not restore turn Permissions: {exc}",
                                )

                    turn_cleanup = _cleanup_once
            except Exception as exc:  # noqa: BLE001 - pending state stays retryable
                transcript.append(
                    "error",
                    f"Could not prepare the next message: {exc}. The message was not sent.",
                )
                _safe_invalidate()
                return False
        if local_lifecycle is not None:
            try:
                local_lifecycle.begin_turn(session, check_gate=False)
            except Exception as exc:
                if turn_cleanup is not None:
                    turn_cleanup()
                transcript.append("error", f"Could not reserve conversation turn: {exc}")
                return False
        turn_cleanup_box["callback"] = turn_cleanup
        if turn_cleanup is not None:
            run_kwargs["_alysis_turn_cleanup"] = turn_cleanup
        # Composer attachments are the user's, not a command's: merge them in
        # after the command runner has built its own kwargs, and never clobber
        # images a command already staged.
        if image_paths and not run_kwargs.get("image_paths"):
            run_kwargs["image_paths"] = list(image_paths)
        if notice:
            transcript.append("system", notice)
        if user_display_text is not None:
            transcript.append_user(user_display_text)
        with turn_state_lock:
            running["on"] = True
            retiring["on"] = False
        subagent_panel["tip_shown"] = False
        cancel_box["token"] = _Cancellation()
        run_box["started"] = time.monotonic()
        run_box["cancellation_started"] = 0.0
        tip_state["turn_index"] += 1
        # No "Thinking…" footer status — the transient thinking indicator now
        # renders under the question (see _transcript_fragments).
        transcript.set_status(None)
        _safe_invalidate()
        try:
            if background_turns:
                worker = threading.Thread(
                    target=_run_turn_blocking,
                    args=(instruction, run_kwargs, _capture_ui_dispatch()),
                    daemon=True,
                )
                worker_box["thread"] = worker
                worker.start()
            else:
                _run_turn_blocking(instruction, run_kwargs)
        except BaseException:
            if local_lifecycle is not None:
                local_lifecycle.finish_turn(failed=True)
            if turn_cleanup is not None:
                turn_cleanup()
            turn_cleanup_box["callback"] = None
            with turn_state_lock:
                running["on"] = False
                retiring["on"] = False
                cancel_box["token"] = None
            raise
        if not background_turns:
            # Inline tests run turns synchronously. Drain iteratively behind a
            # re-entrancy guard so N queued messages do not recurse N frame sets.
            if not draining["on"]:
                draining["on"] = True
                try:
                    while (pending_operations or pending_turns) and not running["on"]:
                        if not _start_next_pending_turn():
                            break
                finally:
                    draining["on"] = False
        return True

    def _soft_interrupt() -> None:
        # Respond to Ctrl+C / Esc immediately without allowing two turns to mutate
        # the same AgentSession. The composer stays usable, but submissions queue
        # until this worker's normal finally path retires it and starts the next.
        with turn_state_lock:
            if retiring["on"]:
                return
            token = cancel_box.get("token")
            if token is None and not running["on"]:
                return
            retiring["on"] = True
            run_box["cancellation_started"] = time.monotonic()
        transcript.set_status("Cancelling current turn…")
        if token is not None:
            token.cancel_nonblocking()
        surface = getattr(session, "surface", None)
        questions = getattr(surface, "user_questions", None)
        if questions is not None:
            questions.reset()
        pending = approval_box.get("event")
        if pending is not None:
            approval_box["decision"] = ApprovalDecision(allow=False)
            pending.set()
        surface = getattr(session, "surface", None)
        interrupt_forge = getattr(surface, "interrupt_forge", None)
        if callable(interrupt_forge):
            try:
                interrupt_forge()
            except Exception:
                pass
        rescued = _drain_undelivered_steers()
        pending_turns.extend(rescued)
        with pending_messages_lock:
            delivered_steers.clear()
        if rescued:
            transcript.append(
                "warn",
                f"interrupted - {len(rescued)} pending messages will start the next turn",
            )
        else:
            transcript.append("warn", "Interrupted.")
        # The abandoned worker may never deliver its subagent-end event; drop the
        # badge now so the footer cannot claim a dead subagent is still working.
        # Also forget the surface's live-subagent stack: the abandoned runs' late
        # end events then find no matching entry and are dropped whole, instead
        # of popping (or re-pinning) whatever the NEXT turn is running.
        state.active_subagent = ""
        state.active_subagents = ()
        clear_subagents = getattr(surface, "clear_subagent_activity", None)
        if callable(clear_subagents):
            try:
                clear_subagents()
            except Exception:
                pass
        scroll["follow"] = True
        _safe_invalidate()

    def _dispatch_command(
        text: str,
        *,
        display_text: str | None = None,
        echo: bool = True,
        allow_run: bool = True,
        deferred: bool = False,
        reserved: bool = False,
        clear_transcript_before_output: bool = False,
        image_paths: Sequence[str] = (),
        on_accepted: Callable[[], None] | None = None,
    ) -> _DispatchOutcome:
        """Route ``text`` straight through the chat command runner.

        This is the real path of :func:`_submit` (echo the user line, run the
        command handler, then exit / begin a turn / show captured output). It is
        factored out so a confirm popup can run its command on Enter — bypassing
        the /help, panel and picker interceptions — e.g. the /forge intro popup
        whose Enter enters the Forge session. No-ops without a session + runner.
        """
        if session is None or command_runner is None:
            return _DispatchOutcome.CONTINUE
        if local_lifecycle is not None and local_lifecycle.mutating and not reserved:
            transcript.append(
                "system", "Local action is still finishing. The command was not applied."
            )
            return _DispatchOutcome.CONTINUE
        scroll["follow"] = True
        if echo:
            input_area.buffer.reset()
            transcript.append_user(text if display_text is None else display_text)
        try:
            # The command's captured Rich output is appended to the transcript and
            # re-wrapped at the transcript's content width, so render it at that
            # width too — at the raw terminal width every full-width line is one
            # column too long and splits, tearing panel borders apart.
            if deferred:
                session._alysis_applying_deferred_command = True
            try:
                action, output, instruction, run_kwargs = command_runner(
                    session, text, _transcript_content_width_for(_current_width())
                )
            finally:
                if deferred:
                    try:
                        del session._alysis_applying_deferred_command
                    except AttributeError:
                        pass
        except Exception as exc:  # noqa: BLE001 - never crash the UI on a command
            transcript.append("error", f"Command failed: {exc}")
            _safe_invalidate()
            return _DispatchOutcome.CONTINUE
        if clear_transcript_before_output:
            transcript.clear()
            scroll["offset"] = 0
        if output:
            transcript.append("system", output)
        if action == "exit":
            get_app().exit(result=text.strip())
            return _DispatchOutcome.EXIT
        if action == "run":
            if not allow_run:
                transcript.append(
                    "warn",
                    f"Deferred command was rejected because it tried to start a turn: {text}",
                )
                _safe_invalidate()
                return _DispatchOutcome.CONTINUE
            accepted = _begin_run(
                instruction if instruction is not None else text,
                run_kwargs or {},
                image_paths=image_paths,
            )
            if accepted and on_accepted is not None:
                on_accepted()
            return _DispatchOutcome.CONTINUE
        _safe_invalidate()  # "handled" — output already shown
        return _DispatchOutcome.CONTINUE

    def _defer_mid_turn_command(text: str, *, display_text: str | None = None) -> None:
        if not _stage_pending_command(pending_operations, text):
            transcript.append(
                "warn",
                f"Deferred command queue is full ({_MAX_PENDING_COMMANDS}) - "
                "let the current turn finish before adding more.",
            )
            _safe_invalidate()
            return
        input_area.buffer.reset()
        scroll["follow"] = True
        transcript.append_user(text if display_text is None else display_text)
        transcript.append("system", defer_message(text))
        _safe_invalidate()

    def _stage_mid_turn_step_operation(
        text: str,
        operation: ResolvedOperation,
        *,
        display_text: str | None = None,
    ) -> None:
        input_area.buffer.reset()
        scroll["follow"] = True
        transcript.append_user(text if display_text is None else display_text)
        if operation.kind == "mode":
            # Permission selection is safe on the UI thread because it changes
            # only a pending scalar. Never put it in the worker's step inbox:
            # draining that inbox during the active turn would make the tool
            # surface generation-dependent again.
            try:
                if step_operation_apply is None or session is None:
                    raise RuntimeError("no Permissions staging handler is attached")
                step_operation_apply(session, operation)
            except Exception as exc:  # noqa: BLE001 - keep the active turn intact
                transcript.append("error", f"{operation.display_label} - failed to select: {exc}")
                _safe_invalidate()
                return
            transcript.append("system", f"{operation.display_label} - next message")
            _safe_invalidate()
            return
        inbox = ops_inbox_for(session, create=True) if session is not None else None
        if inbox is not None and inbox.send(operation):
            transcript.append("system", defer_message(text, display_label=operation.display_label))
            _safe_invalidate()
            return
        if _stage_pending_command(pending_operations, text):
            transcript.append(
                "system",
                f"{operation.display_label} - next message",
            )
        else:
            transcript.append(
                "warn",
                f"Deferred command queue is full ({_MAX_PENDING_COMMANDS}) - "
                "let the current turn finish before adding more.",
            )
        _safe_invalidate()

    def _latest_staged_persona() -> str | None:
        for operation in reversed(pending_operations):
            if operation.kind is not _DeferredOperationKind.COMMAND:
                continue
            parts = operation.text.strip().split(maxsplit=1)
            if len(parts) == 2 and parts[0].lower() == "/persona":
                return parts[1].strip().lower() or None
        return None

    def _stage_tab_persona() -> None:
        if persona_stage_target is None:
            transcript.append("warn", block_message("/persona"))
            _safe_invalidate()
            return
        try:
            target = persona_stage_target(_latest_staged_persona())
        except Exception as exc:  # noqa: BLE001 - never crash the UI on Tab
            transcript.append("error", f"persona cycle failed: {exc}")
            _safe_invalidate()
            return
        if not target:
            return
        command = f"/persona {target}"
        if (
            pending_operations
            and pending_operations[-1].kind is _DeferredOperationKind.COMMAND
            and pending_operations[-1].text.strip().lower().startswith("/persona ")
        ):
            pending_operations[-1] = _DeferredOperation(
                _DeferredOperationKind.COMMAND,
                command,
            )
        elif not _stage_pending_command(pending_operations, command):
            transcript.append(
                "warn",
                f"Deferred command queue is full ({_MAX_PENDING_COMMANDS}) - "
                "let the current turn finish before adding more.",
            )
            _safe_invalidate()
            return
        _record_tui_local_interaction(session=session, action="persona_cycle")
        transcript.append("system", defer_message(command))
        _safe_invalidate()

    def _resolve_mid_turn_step_operation(text: str) -> ResolvedOperation | None:
        if session is None or step_operation_resolver is None:
            return None
        return step_operation_resolver(session, text)

    def _open_mid_turn_step_picker(command: str) -> bool:
        if command not in {"/permissions", "/stream"} or not picker_providers:
            return False
        provider = picker_providers.get(command)
        if provider is None:
            return False
        spec = provider()
        if not spec or not spec.get("rows"):
            return False
        if command == "/permissions":
            # The provider's callback stages the selection directly. Wrapping it
            # in the step inbox would let the running turn apply it mid-flight.
            input_area.buffer.reset()
            _open_picker(spec)
            return True
        wrapped = dict(spec)

        def _stage_selected(value: Any) -> None:
            explicit = f"{command} {value}"
            try:
                operation = _resolve_mid_turn_step_operation(explicit)
            except Exception as exc:  # noqa: BLE001 - validation errors stay in the UI
                transcript.append("error", str(exc))
                _safe_invalidate()
                return
            if operation is None:
                transcript.append("error", f"{command} selection could not be staged")
                _safe_invalidate()
                return
            _stage_mid_turn_step_operation(explicit, operation)

        wrapped["on_select"] = _stage_selected
        input_area.buffer.reset()
        _open_picker(wrapped)
        return True

    def _deliver_mid_turn_message(
        text: str,
        *,
        display_text: str | None = None,
        queue: bool,
        attachments: Sequence[Attachment] = (),
    ) -> bool:
        buff = input_area.buffer
        stripped = text.strip()
        display_stripped = (text if display_text is None else display_text).strip()
        if not stripped:
            return False
        scroll["follow"] = True
        inbox = steer_inbox_for(session, create=True) if session is not None else None
        if attachments and not queue and inbox is not None:
            # A steer is delivered into the running turn as text; an attached
            # image can only ride a turn of its own. Queue rather than silently
            # dropping what the user attached.
            queue = True
            transcript.append(
                "system",
                "Attachments send with their own turn - queued this message instead of steering.",
            )
        if queue or inbox is None:
            with pending_messages_lock:
                if len(pending_turns) >= MAX_PENDING_STEER_MESSAGES:
                    transcript.append(
                        "warn",
                        f"Queue is full ({MAX_PENDING_STEER_MESSAGES}) - "
                        "let queued work run before adding more.",
                    )
                    _safe_invalidate()
                    return False
                pending_turns.append(
                    _PendingMessage(
                        text=stripped,
                        display_text=display_stripped,
                        attachments=tuple(attachments),
                        pastes=tuple(paste_registry.snapshot().items()),
                    )
                )
            buff.reset()
            _safe_invalidate()
            return True

        with pending_messages_lock:
            _sync_delivered_steers()
            display_value = display_stripped if display_text is not None else stripped
            pending_steers.append(
                _PendingMessage(
                    text=stripped,
                    display_text=display_value,
                    pastes=tuple(paste_registry.snapshot().items()),
                )
            )
            dropped_before = inbox.dropped_count()
            delivered = inbox.send(stripped)
            newly_dropped = inbox.dropped_count() - dropped_before
            if newly_dropped > 0:
                del pending_steers[:newly_dropped]
            if pending_steers:
                pending_steers[-1] = _PendingMessage(
                    text=delivered,
                    display_text=display_stripped if display_text is not None else delivered,
                    pastes=tuple(paste_registry.snapshot().items()),
                )
        buff.reset()
        if newly_dropped > 0:
            label = "message" if newly_dropped == 1 else "messages"
            transcript.append(
                "warn",
                f"Dropped {newly_dropped} older undelivered {label} - "
                "too many arrived before the next step.",
            )
        _safe_invalidate()
        return True

    def _recall_latest_queued_message() -> bool:
        """Move the newest queued follow-up back into the composer."""
        buff = input_area.buffer
        if buff.text:
            return False

        with pending_messages_lock:
            if not pending_turns:
                return False
            message = pending_turns.pop()

        recalled_pending["queue"] = True
        prompt_history.index, prompt_history.draft = None, None
        buff.reset()
        # Re-adopt before the text lands: on_text_changed sweeps chips that the
        # registry does not know, which would silently detach them.
        attachment_registry.restore(message.attachments)
        paste_registry.restore(dict(message.pastes))
        buff.insert_text(message.display_text)
        buff.cancel_completion()
        _safe_invalidate()
        return True

    def _composer_snapshot():
        return (
            input_area.buffer.document,
            attachment_registry.snapshot(),
            paste_registry.snapshot(),
        )

    def _submit(*, queue_instead: bool | None = None) -> None:
        buff = input_area.buffer
        # Trailing composer whitespace (the space a chip leaves behind the
        # cursor, a stray Ctrl+J newline) is chrome, not message. Trimming it
        # here — before expansion — never reaches inside a paste payload.
        display_text = buff.text.rstrip()
        if not display_text.strip():
            return
        # Composer chips resolve before paste tokens: the chips live in the text
        # the user sees, while an expanded paste payload is opaque content that
        # must never be re-scanned for tokens.
        submitted_attachments = tuple(attachment_registry.attachments_in(display_text))
        if any(not a.path.exists() for a in submitted_attachments):
            transcript.append(
                "error",
                "History attachment unavailable. Reattach it or remove its chip before sending.",
            )
            _safe_invalidate()
            return
        history_entry = PromptEntry(
            display_text, submitted_attachments, tuple(paste_registry.snapshot().items())
        )

        def _accept_prompt() -> None:
            if not is_command(history_entry.text.strip()):
                prompt_history.accept(history_entry)
                if prompt_history.persistence_error and not history_warning["shown"]:
                    history_warning["shown"] = True
                    transcript.append(
                        "warn",
                        "Prompt history could not be saved; recall is available only until exit.",
                    )

        submitted_images = [
            os.fspath(attachment.path)
            for attachment in submitted_attachments
            if attachment.kind == "image"
        ]
        text = paste_registry.expand(attachment_registry.expand(display_text))
        stripped = text.strip()
        if approval_box.get("event") is not None:
            return
        command_parts = stripped.split(maxsplit=1)
        local_name = command_parts[0].lower()
        if local_name in {"/mcp", "/extensions"}:
            if len(command_parts) > 1:
                transcript.append(
                    "system",
                    f"Use {local_name} without arguments to inspect integrations. Configuration and authentication instructions are shown there.",
                )
            else:
                _open_integrations(local_name[1:])
            return
        if local_name == "/planning" or (local_name == "/plan" and not state.forge_mode):
            if len(command_parts) > 1:
                transcript.append(
                    "system", "Use /plan without arguments to choose a planning workflow."
                )
            else:
                _open_planning()
            return
        if local_name == "/recover":
            transcript.append("system", "Unknown command: /recover. Try /help.")
            return
        if local_lifecycle is not None and local_lifecycle.mutating:
            transcript.append("system", "Local action is still finishing. Your draft is retained.")
            return
        if local_name == "/resume" and session_search_provider is not None:
            if not _idle_for_navigation():
                transcript.append(
                    "system", "Wait for active and queued work to finish before resuming."
                )
            elif len(command_parts) == 1:
                _open_sessions()
            elif resume_session is not None and session_search_provider() is not None:
                _resume_reserved(command_parts[1].strip(), command_text=text)
            else:
                _dispatch_command(text, echo=False, allow_run=False)
            return
        if local_name in {"/find", "/copy-last"}:
            if local_name == "/find":
                _open_transcript_search(command_parts[1] if len(command_parts) > 1 else "")
            elif len(command_parts) > 1:
                transcript.append("system", "Use /copy-last without arguments.")
            else:
                _copy_last_response()
            return
        if command_parts[0].lower() == "/review":
            if len(command_parts) > 1:
                transcript.append(
                    "system", "Use /review without arguments, then choose a scope and Start."
                )
            else:
                _open_review()
            return
        # Inspection is local and read-only even during an active/retiring turn.
        # Never hand this token (including unsupported arguments) to a model.
        if command_parts[0].lower() == "/diff":
            if len(command_parts) > 1:
                transcript.append(
                    "system", "Use /diff without arguments to browse working-tree changes."
                )
            else:
                _open_diff()
            return
        _record_chat_local_command(
            session=session,
            command_token=command_parts[0],
            has_argument=len(command_parts) == 2 and bool(command_parts[1].strip()),
        )
        # /image is TUI-native: it edits the composer instead of starting a turn,
        # so it is answered here — before every turn-state branch below — and
        # works just as well while a turn is running.
        if command_parts[0].lower() in {"/image", "/paste-image"}:
            argument = command_parts[1].strip() if len(command_parts) == 2 else ""
            buff.reset()
            if argument:
                _attach_paths([argument])
            else:
                _attach_clipboard_image()
            return
        if (
            stripped.lower() == "/model"
            and config_overlay is not None
            and config_flow_factory is not None
            and not retiring["on"]
        ):

            def model_flow() -> Any:
                flow = config_flow_factory()
                flow.open_default_model()
                return flow

            buff.reset()
            config_overlay.open(flow_factory=model_flow)
            return
        if review_box["controller"] is not None and review_box["controller"].running:
            transcript.append(
                "system", "Close or cancel review before starting a conversation turn."
            )
            return False
        with turn_state_lock:
            turn_retiring = retiring["on"]
        if turn_retiring:
            if command_parts[0].lower() in {"/report", "/feedback"}:
                transcript.append(
                    "warn", "Report is available once the interrupted turn has finished stopping."
                )
                _safe_invalidate()
                return
            # The cancelled worker still owns the AgentSession until its finally
            # path returns. Accept user input now, but preserve it strictly as
            # next-turn work so it cannot steer or reconfigure that old worker.
            if is_command(stripped):
                _defer_mid_turn_command(text, display_text=display_text)
            else:
                if _deliver_mid_turn_message(
                    text,
                    display_text=display_text if display_text != text else None,
                    queue=True,
                    attachments=submitted_attachments,
                ):
                    _accept_prompt()
                    recalled_pending["queue"] = None
            return
        if running["on"]:
            command = stripped.split(maxsplit=1)[0].lower()
            ui_mode = "forge" if getattr(state, "forge_mode", False) else "chat"
            if command.startswith("/"):
                recalled_pending["queue"] = None
            if command.startswith("/") and not command_visible(command, ui_mode=ui_mode):
                _dispatch_command(text, display_text=display_text, allow_run=False)
                return
            if config_overlay is not None and stripped.lower() == "/config":
                buff.reset()
                config_overlay.open()
                return
            if stripped.lower() in {"/permissions", "/stream"}:
                try:
                    if _open_mid_turn_step_picker(stripped.lower()):
                        return
                except Exception as exc:  # noqa: BLE001 - never crash the UI on a picker
                    buff.reset()
                    transcript.append("error", f"{stripped.lower()} failed: {exc}")
                    _safe_invalidate()
                    return
            try:
                step_operation = _resolve_mid_turn_step_operation(stripped)
            except Exception as exc:  # noqa: BLE001 - validation errors stay in the UI
                buff.reset()
                transcript.append_user(display_text)
                transcript.append("error", str(exc))
                _safe_invalidate()
                return
            if step_operation is not None:
                _stage_mid_turn_step_operation(
                    text,
                    step_operation,
                    display_text=display_text,
                )
                return
            action = classify_mid_turn(stripped)
            if action is MidTurnAction.BLOCK:
                recalled_pending["queue"] = None
                transcript.append("warn", block_message(stripped))
                _safe_invalidate()
                return
            if action is MidTurnAction.DEFER:
                _defer_mid_turn_command(text, display_text=display_text)
                return
            if action is MidTurnAction.MESSAGE:
                recalled_queue = recalled_pending["queue"]
                effective_queue = (
                    bool(recalled_queue)
                    if queue_instead is None and recalled_queue is not None
                    else bool(queue_instead)
                )
                if _deliver_mid_turn_message(
                    text,
                    display_text=display_text if display_text != text else None,
                    queue=effective_queue,
                    attachments=submitted_attachments,
                ):
                    _accept_prompt()
                    recalled_pending["queue"] = None
                return
        recalled_pending["queue"] = None
        # Returning to the live tail whenever the user sends something.
        scroll["follow"] = True

        if report_overlay is not None and command_parts[0].lower() in {"/report", "/feedback"}:
            feedback = command_parts[1] if len(command_parts) > 1 else ""
            buff.reset()
            report_overlay.open(flow_factory=lambda: report_flow_factory(session, feedback))
            return

        # /help is TUI-native: open the centered command popup instead of routing
        # it anywhere (works with or without a session).
        if stripped.lower() == "/help":
            buff.reset()
            _open_help()
            return

        login_parts = stripped.split(maxsplit=1)
        if len(login_parts) == 2 and login_parts[0].lower() == "/login":
            buff.reset()
            get_app().exit(result=("login_connection", login_parts[1].strip()))
            return

        # Keep the TUI transcript and the model-visible conversation aligned.
        # The command runner owns the real session reset; clear the rendered
        # transcript only after that handler returns, then show its outcome.
        if session is not None and stripped.lower() == "/clear":
            buff.reset()
            _dispatch_command(
                text,
                echo=False,
                allow_run=False,
                clear_transcript_before_output=True,
            )
            return

        # /subagents is a read-only TUI window over the scheduler's live child
        # set. Selection opens the existing live panel; an empty set stays inline.
        if stripped.lower() == "/subagents":
            buff.reset()
            scheduler = getattr(session, "child_scheduler", None)
            if scheduler is None:
                transcript.append("system", "no active subagents")
                _safe_invalidate()
                return
            try:
                spec = _subagents_picker_spec(scheduler, subagent_panel)
            except Exception as exc:  # noqa: BLE001 - inspection must not crash the UI
                transcript.append("error", f"/subagents failed: {exc}")
                _safe_invalidate()
                return
            if spec is None:
                transcript.append("system", "no active subagents")
                _safe_invalidate()
                return
            _open_picker(spec)
            return

        # /config is TUI-native: bare `/config` opens the full configuration menu
        # overlay (the classic interactive menu can't run in the alt-screen). The
        # argument forms (`/config show|list|set|clear|…`) fall through to the panel
        # provider / command runner so the typed forms still apply.
        if config_overlay is not None and stripped.lower() == "/config":
            buff.reset()
            config_overlay.open()
            return

        # Panel commands (e.g. /status, /usage) are TUI-native too: the first token
        # names a provider, called with the remaining argument, that returns a
        # {title, hint?, sections} spec to open the centered popup instead of routing
        # to the command runner (no transcript echo). Returning None means "this
        # invocation isn't a panel" (e.g. "/usage hud on", "/config set …") — we fall
        # through to the picker / command runner so the typed form still applies.
        if panel_providers:
            parts = stripped.split(maxsplit=1)
            name = parts[0].lower()
            ui_mode = "forge" if getattr(state, "forge_mode", False) else "chat"
            provider = panel_providers.get(name) if command_visible(name, ui_mode=ui_mode) else None
            if provider is not None:
                cmd_arg = parts[1].strip() if len(parts) > 1 else ""
                try:
                    spec = provider(cmd_arg)
                except Exception as exc:  # noqa: BLE001 - never crash the UI on a panel
                    buff.reset()
                    transcript.append("error", f"{name} failed: {exc}")
                    _safe_invalidate()
                    return
                if spec is not None:
                    buff.reset()
                    hint = spec.get("hint") or _HELP_HINT
                    title = spec.get("title") or name
                    editor_spec = spec.get("editor")
                    picker_spec = spec.get("picker")
                    body = spec.get("body")
                    # An optional confirm command turns the panel into a confirm
                    # dialog: Enter runs it (e.g. the /forge intro enters Forge),
                    # Esc/q cancels. None for ordinary read-only panels.
                    confirm = spec.get("confirm")
                    on_confirm = spec.get("on_confirm")
                    # Optional violet frame accent for Forge panels (default None).
                    accent = spec.get("accent")
                    if picker_spec is not None:
                        # A command that opens a selectable popup directly (e.g. the
                        # forge /execute launch gate). Distinct from picker_providers
                        # (which only fire for the bare, no-argument form).
                        _open_picker(picker_spec)
                        return
                    if editor_spec is not None:
                        # An editable document (e.g. /plan edit) opens the editor
                        # float instead of a read-only panel.
                        _open_editor(editor_spec)
                        return
                    if body is not None:
                        # A document panel (e.g. /plan markdown → PLAN.md): render the
                        # raw text (markdown-aware) instead of key/value sections.
                        _open_panel(
                            title,
                            lambda width, _b=body, _h=hint: _render_doc_panel_rows(
                                _b,
                                width,
                                _h,
                                theme=terminal_theme,
                            ),
                            confirm=confirm,
                            on_confirm=on_confirm,
                            accent=accent,
                        )
                    else:
                        sections = spec.get("sections") or []
                        _open_panel(
                            title,
                            lambda width, _s=sections, _h=hint: _render_kv_panel_rows(
                                _s, width, _h
                            ),
                            confirm=confirm,
                            on_confirm=on_confirm,
                            accent=accent,
                        )
                    return
                # spec is None → not a panel for this argument; fall through below.

        # Picker commands (e.g. bare /permissions) open the selectable popup. Only the
        # no-arg form opens the picker; "/permissions fast" falls through to the command
        # runner so an explicit choice still applies inline.
        if picker_providers:
            parts = stripped.split()
            name = parts[0].lower()
            ui_mode = "forge" if getattr(state, "forge_mode", False) else "chat"
            if (
                len(parts) == 1
                and name in picker_providers
                and command_visible(name, ui_mode=ui_mode)
            ):
                try:
                    spec = picker_providers[name]()
                except Exception as exc:  # noqa: BLE001 - never crash the UI on a picker
                    buff.reset()
                    transcript.append("error", f"{name} failed: {exc}")
                    _safe_invalidate()
                    return
                if spec and spec.get("rows"):
                    buff.reset()
                    _open_picker(spec)
                    return
                # Nothing to pick (e.g. no subagents registered) → fall through to
                # the command runner so it can explain/guide instead of silently
                # swallowing the command.

        # Real path with slash-command support: route every submission through
        # the chat command handler (it returns "run" for plain messages).
        if session is not None and command_runner is not None:
            _dispatch_command(
                text,
                display_text=display_text,
                image_paths=submitted_images,
                on_accepted=_accept_prompt,
            )
            return

        # No command runner (Phase 2 fake session / tests): exit words + run.
        if stripped.lower() in _EXIT_WORDS:
            get_app().exit(result=stripped)
            return
        if session is not None:
            buff.reset()
            transcript.append_user(display_text)
            if _begin_run(text, {}, image_paths=submitted_images):
                _accept_prompt()
            return

        # Shell-only path: keep configuration/help available while model calls are
        # blocked. Phase-1 tests still get the historical preview reply when no
        # explicit blocker was supplied.
        transcript.append_user(display_text)
        if unavailable_message:
            transcript.append("warn", unavailable_message)
        else:
            transcript.append("system", _PREVIEW_REPLY)
        buff.reset()
        _safe_invalidate()

    # ---- footer ----
    def _footer_text() -> FormattedText:
        try:
            width = get_app().output.get_size().columns
        except Exception:
            width = 80
        return footer_fragments(state, width=width)

    footer_window = Window(FormattedTextControl(_footer_text, focusable=False), height=2)

    body = HSplit(
        [
            ConditionalContainer(welcome_window, filter=welcome_visible),
            ConditionalContainer(Window(), filter=welcome_completion_blank),
            ConditionalContainer(transcript_window, filter=has_messages),
        ]
    )

    input_frame = Frame(
        input_area,
        title="",
    )

    def _side_width() -> Any:
        return D.exact(0)

    def _box_height() -> int:
        return 3

    input_row = VSplit(
        [
            Window(width=_side_width, height=_box_height),
            input_frame,
            Window(width=_side_width, height=_box_height),
        ]
    )
    subagent_panel_container = _subagent_panel_container(
        subagent_panel, hint_provider=lambda: _subagent_key_hint()
    )

    def _pending_messages_text() -> FormattedText:
        _sync_delivered_steers()
        with pending_messages_lock:
            return _pending_message_fragments(
                list(pending_steers),
                list(pending_turns),
                width=_current_width(),
            )

    def _pending_messages_line_count() -> int:
        with pending_messages_lock:
            width = max(1, _current_width())
            count = len(pending_steers) + len(pending_turns)
            if pending_steers:
                header = (
                    _PENDING_STEER_HEADER if len(pending_steers) == 1 else _PENDING_STEERS_HEADER
                )
                count += max(1, (len(header) + width - 1) // width)
            if pending_turns:
                count += max(1, (len(_PENDING_QUEUED_HEADER) + width - 1) // width)
            if pending_turns:
                count += 1
            return count

    pending_messages_window = Window(
        FormattedTextControl(_pending_messages_text, focusable=False),
        height=lambda: D(
            min=0,
            preferred=_pending_messages_line_count(),
            max=_pending_messages_line_count(),
        ),
        wrap_lines=True,
        style="class:tui.pending-messages",
    )
    pending_messages_container = ConditionalContainer(
        pending_messages_window,
        filter=Condition(lambda: bool(pending_steers or pending_turns)),
    )

    root = HSplit(
        [
            body,
            # Working/status line only appears once a conversation is underway, so
            # the welcome screen keeps its Phase 1 spacing exactly.
            ConditionalContainer(
                status_window,
                filter=has_messages | has_live_paste_tokens | has_live_attachments,
            ),
            subagent_panel_container,
            pending_messages_container,
            ConditionalContainer(
                Window(
                    FormattedTextControl(
                        lambda: (
                            f"  Question · {surface.user_questions.remaining_seconds}s · Ctrl+G answer"
                            if surface is not None
                            else ""
                        )
                    ),
                    height=1,
                    style="class:tui.status",
                ),
                filter=Condition(
                    lambda: (
                        surface is not None
                        and surface.user_questions.active is not None
                        and not (question_panel is not None and question_panel.open_condition())
                    )
                ),
            ),
            ConditionalContainer(
                working_tip_window,
                filter=Condition(_working_tip_visible),
            ),
            input_row,
            Window(height=1),
            footer_window,
        ]
    )

    # ---- /help popup (centered Float over the root; opaque command panel) ----
    _help_open = Condition(lambda: help_box["on"])

    def _resolve_help_sections() -> list[HelpSection]:
        if help_sections is not None:
            sections = list(help_sections)
        else:
            try:
                from ..commands.welcome import _chat_command_sections

                ui_mode = "forge" if getattr(state, "forge_mode", False) else "chat"
                sections = list(_chat_command_sections(ui_mode=ui_mode) or [])
            except Exception:
                sections = []
            if not sections:
                sections = _fallback_help_sections()
        discovery = [("Ctrl+R", "Search prompt history"), ("Alt+R", "Expand reasoning")]
        discovery.extend(
            [
                ("/find", "Find transcript text; Enter/F3 next, Ctrl+P previous"),
                ("/copy-last", "Copy the last assistant response as Markdown"),
            ]
        )
        if session_search_provider is not None and resume_session is not None:
            discovery.append(("/resume", "Search saved conversations; F2 renames the selection"))
        if diff_viewer_factory is not None:
            discovery.append(("/diff", "Browse staged, unstaged and untracked changes locally"))
        discovery.extend(
            (
                ("/mcp", "Inspect MCP runtime state, tools and connection controls"),
                ("/extensions", "Inspect installed extensions and restart-required configuration"),
            )
        )
        if planning_controller is not None:
            discovery.append(
                (
                    "/plan · /planning",
                    "Choose Architect or Forge; /planning also returns to the prior mode",
                )
            )
        if review_engine_factory is not None:
            discovery.append(("/review", "Choose scope and Start a structured review while idle"))
        if config_overlay is not None:
            discovery.append(("/model", MODEL_DESCRIPTION))
        return [("Action discovery", discovery), *sections]

    def _help_fragments() -> FormattedText:
        # Derived from the help float's own width, less its frame AND its
        # scrollbar margin — the margin is a content column, so a builder that
        # forgets it renders every row one column too wide (see "float geometry").
        width = _help_content_width_for(_current_width())
        builder = help_box.get("builder")
        if builder is None:
            rows = _help_rows_for_sections(_resolve_help_sections(), max(20, width))
        else:
            rows = builder(max(20, width))
        # Links in a doc panel are clickable too. A blank row gets one cell:
        # prompt_toolkit reports a click on an empty row as (0, 0), which could
        # land on a link in the first row.
        rows = [
            pad_after_trailing_link(row, max(20, width))
            if fragment_list_to_text(row)
            else [("", " ")]
            for row in rows
        ]
        help_rows["n"] = len(rows)
        help_rows["rows"] = rows
        fragments: list[tuple[str, str]] = []
        for index, row in enumerate(rows):
            if index:
                fragments.append(("", "\n"))
            fragments.extend(row)
        return FormattedText(fragments)

    # Scroll math: the cursor is pinned to the TOP of the viewport via the huge
    # bottom scroll-offset, so vertical_scroll tracks the offset 1:1. The max scroll
    # (follow-top) is taken STRAIGHT from render_info — `content_height -
    # window_height` is the exact value the ScrollbarMargin treats as fully
    # scrolled, so the clamp and the scrollbar agree and the thumb reaches the end.
    def _help_win_height() -> int:
        info = help_window.render_info
        return info.window_height if info is not None else 0

    def _help_follow_top() -> int:
        info = help_window.render_info
        if info is None:
            return max(0, help_rows["n"] - 1)
        return max(0, info.content_height - info.window_height)

    def _help_cursor_row() -> int:
        return max(0, min(int(help_box["offset"]), _help_follow_top()))

    def _help_panel_width() -> int:
        return _help_panel_width_for(_current_width())

    def _help_panel_height() -> int:
        try:
            rows = get_app().output.get_size().rows
        except Exception:
            rows = 24
        # Outer frame height; leave a clear top/bottom margin and never overflow
        # the screen, so render_info is exact and scrolling can reach the bottom.
        return max(6, rows - 6)

    def _help_mouse_event(mouse_event: MouseEvent) -> Any:
        """Open a link clicked in the popup (press and release on one cell)."""
        rows = help_rows.get("rows") or []
        point = Point(x=max(0, mouse_event.position.x), y=max(0, mouse_event.position.y))
        url = link_at(rows[point.y], point.x) if point.y < len(rows) else None
        event_type = mouse_event.event_type
        if event_type == MouseEventType.MOUSE_DOWN:
            left = mouse_event.button == MouseButton.LEFT
            link_press["panel"] = (url, point) if url and left else None
        elif event_type == MouseEventType.MOUSE_UP:
            press = link_press["panel"]
            link_press["panel"] = None
            if press is not None and press == (url, point):
                _open_link(press[0])
                return None
        elif event_type == MouseEventType.MOUSE_MOVE:
            press = link_press["panel"]
            if press is not None and press[1] != point:
                link_press["panel"] = None
        return NotImplemented

    help_window = Window(
        _ScrollableControl(
            _help_fragments,
            focusable=True,
            show_cursor=False,
            get_cursor_position=lambda: Point(x=0, y=_help_cursor_row()),
            on_scroll=lambda direction: _help_scroll(direction * wheel_step_rows),
            on_mouse_event=_help_mouse_event,
        ),
        # MUST be True: the cursor-pin scroll trick only works on prompt_toolkit's
        # line-wrapping scroll path (_scroll_when_linewrapping). With wrap_lines
        # False the offset never moved vertical_scroll, so the scrollbar thumb
        # could not reach the bottom. Rows are pre-padded to the content width, so
        # nothing actually wraps.
        wrap_lines=True,
        style="class:tui.help",
        scroll_offsets=ScrollOffsets(bottom=10**6),
        right_margins=[ScrollbarMargin(display_arrows=False)],
    )
    help_frame = Frame(help_window, title="Commands", style="class:tui.help.frame")

    def _panel_frame_accent_style() -> str:
        # A container style callable: while a Forge panel is open it adds the
        # ``tui.forgeframe`` ancestor class, which (declared after the green
        # ``tui.help.frame frame.border`` rule) repaints the frame border violet.
        # Empty string for every other panel keeps the default green chrome.
        return "class:tui.forgeframe" if help_box.get("accent") == "forge" else ""

    # Wrap the shared frame so the accent class can be toggled per open panel
    # without rebuilding the Frame (whose style is baked in at construction).
    help_frame_wrapper = HSplit([help_frame], style=_panel_frame_accent_style)
    help_float = Float(
        content=ConditionalContainer(help_frame_wrapper, filter=_help_open),
        width=_help_panel_width,
        height=_help_panel_height,
    )

    def _open_panel(
        title: str,
        builder: Callable[[int], list[list[tuple[str, str]]]],
        *,
        confirm: str | None = None,
        on_confirm: Callable[[], list[tuple[str, str]] | None] | None = None,
        accent: str | None = None,
    ) -> None:
        # Open the centered popup with an arbitrary title + row-builder. /help and
        # every panel command (/status, …) route through here so there is one
        # overlay, one set of scroll/close keys, one Float. ``confirm`` (when set)
        # makes Enter run that command instead of merely closing (confirm dialog);
        # ``on_confirm`` is the callback form — Enter runs it (and shows any returned
        # transcript messages) instead of dispatching a command. Esc/q always
        # cancel (neither fires).
        help_box["on"] = True
        help_box["offset"] = 0
        help_box["title"] = title
        help_box["builder"] = builder
        help_box["confirm"] = confirm
        help_box["on_confirm"] = on_confirm
        help_box["accent"] = accent
        try:
            help_frame.title = title
        except Exception:
            pass
        try:
            get_app().layout.focus(help_window)
        except Exception:
            pass
        _safe_invalidate()

    def _open_help() -> None:
        _open_panel(
            "Commands",
            lambda width: _help_rows_for_sections(_resolve_help_sections(), width),
        )

    def _close_help() -> None:
        if not help_box["on"]:
            return
        help_box["on"] = False
        help_box["confirm"] = None
        help_box["on_confirm"] = None
        help_box["accent"] = None
        try:
            get_app().layout.focus(input_area)
        except Exception:
            pass
        _safe_invalidate()

    def _help_page_rows() -> int:
        return max(1, _help_win_height() - 1)

    def _help_scroll(delta: int) -> None:
        new_offset, _follow = _scroll_target(int(help_box["offset"]), _help_follow_top(), delta)
        help_box["offset"] = new_offset
        _safe_invalidate()

    diff_box: dict[str, DiffViewer | None] = {"viewer": None}
    _diff_open = Condition(lambda: diff_box["viewer"] is not None and diff_box["viewer"].is_open)

    def _close_diff() -> None:
        # The composer and its registries were never reset or rewritten.
        if app.is_running:
            app.layout.focus(input_area)
        _safe_invalidate()

    def _open_diff() -> None:
        if diff_viewer_factory is None:
            transcript.append("system", "The diff viewer is unavailable in this surface.")
            return
        input_area.buffer.cancel_completion()
        try:
            workspace = Path(workdir_provider()) if workdir_provider else attachment_root
            viewer = diff_viewer_factory(
                workspace, on_close=_close_diff, invalidate=_safe_invalidate
            )
            diff_box["viewer"] = viewer
            get_app().layout.focus(viewer.focus_target)
            get_app().layout.update_parents_relations()
            viewer.start_refresh()
        except Exception as exc:
            if diff_box["viewer"] is not None:
                diff_box["viewer"].close()
            transcript.append("error", f"Diff viewer unavailable: {exc}")
        _safe_invalidate()

    diff_float = Float(
        content=ConditionalContainer(
            Frame(
                DynamicContainer(
                    lambda: diff_box["viewer"].container if diff_box["viewer"] else Window()
                ),
                title="Changes",
            ),
            filter=_diff_open,
        ),
        left=1,
        right=1,
        top=1,
        bottom=1,
    )

    review_box: dict[str, Any] = {"modal": None, "controller": None, "workspace": None}
    _review_open = Condition(
        lambda: review_box["modal"] is not None and review_box["modal"].is_open
    )

    def _idle_for_navigation() -> bool:
        # The worker can be idle briefly before its scheduled queue drain runs.
        # Pending work is part of eligibility, not merely running/retiring flags.
        with turn_state_lock:
            busy = running["on"] or retiring["on"]
        with pending_messages_lock:
            steers = bool(pending_steers or delivered_steers)
        return not (
            busy
            or pending_turns
            or pending_operations
            or steers
            or (local_lifecycle is not None and local_lifecycle.mutating)
        )

    def _open_review() -> None:
        if not _idle_for_navigation():
            transcript.append("system", "Review is available once the active turn has finished.")
            return
        if review_engine_factory is None:
            transcript.append(
                "system",
                "Structured review is unavailable. Configure a provider with /config or /login.",
            )
            return
        input_area.buffer.cancel_completion()
        try:
            workspace = Path(workdir_provider()) if workdir_provider else attachment_root
            # Bind workspace/config now; opening creates no provider client.
            factory = review_engine_factory(workspace)
            controller = review_box["controller"]
            if controller is None:
                controller = ReviewController(
                    factory, on_change=lambda: None, dispose_engine=dispose_review_engine
                )
                review_box["controller"] = controller
            else:
                controller.engine_factory = factory
                if review_box["workspace"] != workspace:
                    controller.result = None
                    controller.status = "Choose a scope, then start review."
            review_box["workspace"] = workspace
            modal = ReviewModal(
                workspace,
                engine_factory=factory,
                controller=controller,
                on_close=_close_diff,
                invalidate=_safe_invalidate,
                eligible=_idle_for_navigation,
            )
            review_box["modal"] = modal
            get_app().layout.focus(modal.focus_target)
            get_app().layout.update_parents_relations()
        except Exception:
            transcript.append(
                "error",
                "Review setup unavailable. Check provider configuration with /config or /login.",
            )
        _safe_invalidate()

    review_float = Float(
        content=ConditionalContainer(
            Frame(
                DynamicContainer(
                    lambda: review_box["modal"].container if review_box["modal"] else Window()
                ),
                title="Structured review",
            ),
            filter=_review_open,
        ),
        left=1,
        right=1,
        top=0,
        bottom=0,
    )

    navigation_box: dict[str, Any] = {"modal": None}
    navigation_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-search")
    _navigation_open = Condition(
        lambda: navigation_box["modal"] is not None and navigation_box["modal"].is_open
    )

    def _show_navigation(modal: Any) -> None:
        input_area.buffer.cancel_completion()
        navigation_box["modal"] = modal
        get_app().layout.focus(modal.focus_target)
        get_app().layout.update_parents_relations()
        _safe_invalidate()

    def _local_ui_scheduler():
        loop = get_app().loop

        def post(callback):
            if loop is not None and loop.is_running() and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(callback)
                except RuntimeError:
                    pass

        return post

    def _open_integrations(kind):
        owner = session
        workspace = Path(getattr(owner, "root", None) or attachment_root).resolve()
        controller = integration_controller(lambda: session, workspace)
        post = _local_ui_scheduler()
        modal = IntegrationModal(
            kind=kind,
            controller=controller,
            dispatcher=ReservedIntegrationDispatcher(local_lifecycle, lambda: session, owner),
            executor=navigation_executor,
            schedule_ui=post,
            context_identity=lambda: (
                id(session),
                str(getattr(getattr(session, "store", None), "session_id", "")),
                str(getattr(session, "root", workspace)),
            ),
            on_close=_close_diff,
            invalidate=_safe_invalidate,
            on_outcome=lambda outcome: transcript.append("system", outcome.message),
        )
        _show_navigation(modal)

    def _dispatch_planning(work):
        if local_lifecycle is None:
            raise RuntimeError("Live session reservation unavailable")
        owner = session
        identity = local_lifecycle.identity_for(owner)

        def reserved():
            if session is not owner:
                raise RuntimeError("Session changed")
            return work()

        owner_context = copy_context()
        future = local_lifecycle.submit_local(identity, lambda: owner_context.run(reserved))

        def finished(done):
            try:
                outcome = done.result()
            except Exception:
                transcript.append(
                    "system",
                    "Planning change refused; wait for active/queued work and wait for local actions.",
                )
            else:
                transcript.append("system", outcome.message)
            _safe_invalidate()

        future.add_done_callback(finished)

    planning_controller = (
        PlanningController(
            read_state=planning_backend.state,
            dispatch=_dispatch_planning,
            apply_persona=planning_backend.apply_persona,
            enter_forge=planning_backend.enter_forge,
            leave_forge=planning_backend.leave_forge,
            forge_allowed=planning_backend.forge_allowed,
        )
        if planning_backend is not None
        else None
    )

    def _open_planning():
        if planning_controller is None or session is None or local_lifecycle is None:
            transcript.append("system", "Planning choices are unavailable in this surface.")
        elif not _idle_for_navigation():
            transcript.append(
                "system",
                "Wait for active, retiring and queued work; wait for local actions before planning.",
            )
        else:
            _open_picker(planning_controller.picker_spec())

    def _open_sessions() -> None:
        if not _idle_for_navigation():
            transcript.append(
                "system", "Wait for active and queued work to finish before resuming."
            )
            return
        if session_search_provider is None or resume_session is None:
            transcript.append("system", "Session search is unavailable in this surface.")
            return
        try:
            provider = session_search_provider()
            if provider is None:
                raise ValueError("No session storage")
            modal = SessionSearchModal(
                provider,
                executor=navigation_executor,
                on_select=_resume_reserved,
                on_close=_close_diff,
                invalidate=_safe_invalidate,
                eligible=_idle_for_navigation,
            )
            _show_navigation(modal)
            modal.search()
        except Exception:
            transcript.append("error", "Session search unavailable. Current conversation retained.")

    def _open_transcript_search(query: str = "") -> None:
        entries, _, _ = transcript.snapshot()
        _show_navigation(
            TranscriptSearchModal(
                entries, on_close=_close_diff, invalidate=_safe_invalidate, query=query
            )
        )

    def _copy_last_response() -> None:
        text = transcript.last_response_text()
        if not text:
            transcript.append("system", "No assistant response to copy.")
            return
        try:
            copy_text_to_clipboard(text)
        except (ClipboardError, OSError):
            transcript.append("system", "Clipboard unavailable; response was not copied.")
        else:
            transcript.append("system", "Last assistant response copied.")

    navigation_float = Float(
        content=ConditionalContainer(
            Frame(
                DynamicContainer(
                    lambda: (
                        navigation_box["modal"].container if navigation_box["modal"] else Window()
                    )
                ),
                title="Conversation navigation",
            ),
            filter=_navigation_open,
        ),
        left=1,
        right=1,
        top=1,
        bottom=1,
    )

    def _resume_reserved(target, *, command_text=None):
        if resume_session is None:
            return False

        def activate():
            provider = session_search_provider() if session_search_provider is not None else None
            if provider is not None:
                provider.resolve(target)
            return bool(resume_session(target))

        if local_lifecycle is None:
            try:
                return activate()
            except Exception:
                transcript.append("error", "Resume failed; the current conversation was retained.")
                return False
        future = local_lifecycle.submit_local(local_lifecycle.identity_for(session), activate)

        def finished(done):
            try:
                done.result()
            except Exception:
                _post_local_ui(
                    lambda: transcript.append(
                        "error", "Resume failed; the current conversation was retained."
                    )
                )
            _safe_invalidate()

        future.add_done_callback(finished)
        return future

    def _post_local_ui(callback):
        # Teardown waits for the reservation after the event loop has stopped.
        # Outcome publication must not wait for that stopped UI thread.
        loop = app.loop
        if app.is_done or loop is None or loop.is_closed():
            callback()
        else:
            try:
                loop.call_soon_threadsafe(callback)
            except RuntimeError:
                callback()

    if local_lifecycle is not None:
        local_lifecycle.session_provider = lambda: session
        local_lifecycle.idle = _idle_for_navigation
    _file_mutation_active = Condition(
        lambda: local_lifecycle is not None and local_lifecycle.mutating
    )

    def _subagent_key_hint() -> str:
        return "alt+left / alt+right to switch · esc to close"

    history_search = {"on": False, "query": ""}

    def _recall_history(index: int) -> None:
        if prompt_history.index is None:
            prompt_history.draft = (
                input_area.buffer.document,
                tuple(attachment_registry.attachments_in(input_area.text)),
                paste_registry.snapshot(),
            )
        entry = prompt_history.entries[index]
        prompt_history.index = index
        action_dispatching["on"] = True
        try:
            attachment_registry.retain_tokens("")
            paste_registry.retain_tokens("")
            paste_registry.restore(dict(entry.pastes))
            attachment_registry.restore(entry.attachments)
            input_area.text = entry.text
            input_area.buffer.cursor_position = len(entry.text)
            input_area.buffer.cancel_completion()
        finally:
            action_dispatching["on"] = False
        if any(not a.path.exists() for a in entry.attachments):
            transcript.append(
                "warn",
                "History attachment unavailable. Reattach it or remove its chip before sending.",
            )
        recalled_pending["queue"] = None
        _safe_invalidate()

    def _history_move(delta: int) -> None:
        index = prompt_history.index
        if delta < 0 and prompt_history.entries:
            _recall_history(max(0, (len(prompt_history.entries) if index is None else index) - 1))
        elif delta > 0 and index is not None:
            if index + 1 < len(prompt_history.entries):
                _recall_history(index + 1)
            else:
                document, attachments, payloads = prompt_history.draft
                action_dispatching["on"] = True
                try:
                    attachment_registry.retain_tokens("")
                    attachment_registry.restore(attachments)
                    paste_registry.restore(payloads)
                    input_area.buffer.document = document
                finally:
                    action_dispatching["on"] = False
                prompt_history.index, prompt_history.draft = None, None

    def _open_history_search() -> None:
        history_search.update(on=True, query="")
        input_area.buffer.cancel_completion()
        _open_picker(
            {
                "title": "Prompt history",
                "on_select": lambda value: _recall_history(int(value)),
            }
        )
        _refresh_history_search()

    def _refresh_history_search() -> None:
        picker_box["rows"] = [
            {
                "value": str(index),
                "label": entry.searchable_text[:240].replace("\n", " ↵ "),
                "description": "Attachment unavailable"
                if any(not a.path.exists() for a in entry.attachments)
                else "Enter recalls into composer",
            }
            for index, entry in prompt_history.search(history_search["query"])
        ]
        picker_box["index"] = 0
        picker_box["hint"] = (
            f"Search: {history_search['query']} · ↑↓ select · Enter recall · Esc keep draft"
        )
        _safe_invalidate()

    # ---- picker popup (centered Float; selectable option list, e.g. /permissions) ----
    _picker_open = Condition(lambda: picker_box["on"])

    def _picker_fragments() -> FormattedText:
        # render_info is only assigned AFTER create_content has run, so the first
        # frame a popup is shown has none and must fall back to arithmetic — from
        # the picker's OWN float, never the help panel's (see "float geometry").
        width = _picker_content_width()
        rows = _render_picker_rows(
            picker_box["rows"], int(picker_box["index"]), max(20, width), picker_box["hint"]
        )
        fragments: list[tuple[str, str]] = []
        for index, row in enumerate(rows):
            if index:
                fragments.append(("", "\n"))
            fragments.extend(row)
        return FormattedText(fragments)

    def _picker_width() -> int:
        return _picker_width_for(_current_width())

    def _picker_content_width() -> int:
        # The single source of truth for every picker row build, so the fragments,
        # the height and the cursor row can never disagree about the width.
        return _picker_content_width_for(_current_width())

    def _picker_height() -> int:
        try:
            rows = get_app().output.get_size().rows
        except Exception:
            rows = 24
        # Descriptions may wrap to a few lines, so size from the ACTUAL rendered
        # line count (independent of which row is selected) at the panel's inner
        # width; plus the 2-row frame border, capped to the screen.
        content = len(
            _render_picker_rows(
                picker_box["rows"],
                int(picker_box["index"]),
                _picker_content_width(),
                picker_box["hint"],
            )
        )
        return max(5, min(content + _FRAME_COLS, rows - 4))

    def _picker_cursor_position() -> Point:
        # Report the cursor at the selected entry's first line so the Window scrolls
        # to keep the highlighted option visible — essential when a long list (e.g.
        # /resume with many sessions) is taller than the popup. The selected entry's
        # first line is the only one carrying the accent caret style, so find it in
        # the freshly rendered rows (at the panel's real content width).
        rows = _render_picker_rows(
            picker_box["rows"],
            int(picker_box["index"]),
            _picker_content_width(),
            picker_box["hint"],
        )
        for line_no, line in enumerate(rows):
            if any(style == "class:tui.picker.selcaret" for style, _text in line):
                return Point(0, line_no)
        return Point(0, 0)

    picker_window = Window(
        FormattedTextControl(
            _picker_fragments,
            focusable=True,
            show_cursor=False,
            get_cursor_position=_picker_cursor_position,
        ),
        wrap_lines=True,
        style="class:tui.picker",
    )
    picker_frame = Frame(picker_window, title="Select", style="class:tui.help.frame")
    picker_float = Float(
        content=ConditionalContainer(picker_frame, filter=_picker_open),
        width=_picker_width,
        height=_picker_height,
    )

    def _close_picker() -> None:
        if not picker_box["on"]:
            return
        picker_box["on"] = False
        history_search["on"] = False
        try:
            get_app().layout.focus(input_area)
        except Exception:
            pass
        _safe_invalidate()

    def _open_picker(spec: dict[str, Any]) -> None:
        rows = list(spec.get("rows") or [])
        picker_box["on"] = True
        picker_box["rows"] = rows
        picker_box["title"] = spec.get("title") or "Select"
        picker_box["hint"] = spec.get("hint") or _PICKER_HINT
        picker_box["on_select"] = spec.get("on_select")
        # Pre-select the row flagged as current (else the first option).
        picker_box["index"] = next((i for i, r in enumerate(rows) if r.get("current")), 0)
        try:
            picker_frame.title = picker_box["title"]
        except Exception:
            pass
        try:
            get_app().layout.focus(picker_window)
        except Exception:
            pass
        _safe_invalidate()

    def _picker_move(delta: int) -> None:
        count = len(picker_box["rows"])
        if count:
            picker_box["index"] = max(0, min(int(picker_box["index"]) + delta, count - 1))
        _safe_invalidate()

    def _picker_choose(idx: int) -> None:
        rows = picker_box["rows"]
        if not rows or idx < 0 or idx >= len(rows):
            return
        value = rows[idx].get("value")
        on_select = picker_box["on_select"]
        _close_picker()
        if on_select is None or value is None:
            return
        try:
            result = on_select(value)
        except Exception as exc:  # noqa: BLE001 - never crash the UI on a selection
            transcript.append("error", f"selection failed: {exc}")
            result = None
        # on_select returns either a list of (role, text) messages to echo, or a
        # dict {"messages"?: [...], "prefill"?: str, "submit"?: str, "exit"?: Any}.
        #   · ``prefill`` drops text into the input box (cursor at end, focused)
        #     so a picked option can be finished by typing.
        #   · ``submit`` runs text straight through the submit pipeline as if typed —
        #     e.g. a forge /plan option immediately opens its panel (no extra Enter).
        messages = result
        prefill: str | None = None
        submit: str | None = None
        exit_result: Any | None = None
        next_picker: dict[str, Any] | None = None
        if isinstance(result, dict):
            messages = result.get("messages")
            prefill = result.get("prefill")
            submit = result.get("submit")
            exit_result = result.get("exit")
            next_picker = result.get("picker")
        for role, text in messages or []:
            transcript.append(role, text)
        if next_picker is not None:
            # Re-open a (freshly rebuilt) picker — e.g. the launch gate cycles a
            # swarm knob and re-presents the menu with the updated value.
            _open_picker(next_picker)
            _safe_invalidate()
            return
        if exit_result is not None:
            get_app().exit(result=exit_result)
            return
        if submit is not None:
            try:
                input_area.text = str(submit)
                input_area.buffer.cursor_position = len(input_area.text)
                get_app().layout.focus(input_area)
                _submit()
            except Exception:  # noqa: BLE001 - submit is best-effort
                pass
            scroll["follow"] = True
            _safe_invalidate()
            return
        if prefill is not None:
            try:
                prefill_text = str(prefill)
                input_area.text = prefill_text
                input_area.buffer.cursor_position = len(prefill_text)
                get_app().layout.focus(input_area)
            except Exception:  # noqa: BLE001 - prefill is best-effort
                pass
        scroll["follow"] = True
        _safe_invalidate()

    # ---- in-TUI editor (centered Float; e.g. /plan edit on plan.json) ----
    _editor_open = Condition(lambda: editor_box["on"])

    editor_area = TextArea(
        multiline=True,
        wrap_lines=False,
        scrollbar=True,
        line_numbers=True,
        focusable=True,
        style="class:tui.editor",
    )
    _native_editor_mouse_handler = editor_area.control.mouse_handler

    def _editor_drag_scroll(direction: int) -> None:
        if not editor_box["on"] or drag_capture["target"] != "editor":
            return
        if direction < 0:
            editor_area.buffer.cursor_up()
        else:
            editor_area.buffer.cursor_down()

    def _editor_mouse_handler(mouse_event: MouseEvent) -> Any:
        result = _native_editor_mouse_handler(mouse_event)
        if mouse_event.event_type == MouseEventType.MOUSE_DOWN:
            if mouse_event.button == MouseButton.LEFT and editor_box["on"]:
                _begin_drag_capture("editor", _editor_drag_scroll)
        elif (
            mouse_event.event_type == MouseEventType.MOUSE_UP and drag_capture["target"] == "editor"
        ):
            _stop_drag_capture()
        return result

    editor_area.control.mouse_handler = _editor_mouse_handler

    def _editor_status_bar() -> FormattedText:
        status = str(editor_box.get("status") or "")
        hint = "Ctrl+S save · Esc cancel"
        if status:
            return FormattedText(
                [("class:tui.editor.err", f" {status}  "), ("class:tui.editor.hint", hint)]
            )
        return FormattedText([("class:tui.editor.hint", f" {hint}")])

    def _editor_width() -> int:
        try:
            cols = get_app().output.get_size().columns
        except Exception:
            cols = 80
        return max(40, min(cols - 6, 100))

    def _editor_height() -> int:
        try:
            rows = get_app().output.get_size().rows
        except Exception:
            rows = 24
        return max(8, rows - 4)

    editor_body = HSplit(
        [
            editor_area,
            Window(
                FormattedTextControl(_editor_status_bar, focusable=False),
                height=1,
                style="class:tui.editor.statusbar",
            ),
        ]
    )
    editor_frame = Frame(editor_body, title="Edit", style="class:tui.help.frame")
    editor_float = Float(
        content=ConditionalContainer(editor_frame, filter=_editor_open),
        width=_editor_width,
        height=_editor_height,
    )

    def _open_editor(spec: dict[str, Any]) -> None:
        _stop_drag_capture()
        editor_box["on"] = True
        editor_box["title"] = spec.get("title") or "Edit"
        editor_box["on_save"] = spec.get("on_save")
        editor_box["status"] = ""
        editor_area.text = str(spec.get("text") or "")
        editor_area.buffer.cursor_position = 0
        try:
            editor_frame.title = editor_box["title"]
        except Exception:
            pass
        try:
            get_app().layout.focus(editor_area)
        except Exception:
            pass
        _safe_invalidate()

    def _open_paste_editor() -> None:
        buff = input_area.buffer
        match = _paste_token_at_cursor(buff.text, buff.cursor_position)
        payload = paste_registry.get(int(match.group("id"))) if match is not None else None
        if payload is None:
            matches = _live_paste_matches()
            if len(matches) != 1:
                return
            match = matches[0]
            payload = paste_registry.get(int(match.group("id")))
        if match is None or payload is None:
            return
        paste_id = int(match.group("id"))

        def _save_paste(edited_text: str) -> tuple[bool, str]:
            edited_text = _normalize_paste_text(edited_text)
            current_match = next(
                (
                    candidate
                    for candidate in _PASTE_TOKEN_RE.finditer(buff.text)
                    if int(candidate.group("id")) == paste_id
                ),
                None,
            )
            if current_match is None or not paste_registry.update(paste_id, edited_text):
                return False, "Paste token is no longer available."

            old_text = buff.text
            old_cursor = buff.cursor_position
            replacement = _paste_token(paste_id, edited_text)
            buff.text = (
                old_text[: current_match.start()] + replacement + old_text[current_match.end() :]
            )
            if old_cursor <= current_match.start():
                buff.cursor_position = old_cursor
            elif old_cursor <= current_match.end():
                buff.cursor_position = current_match.start() + len(replacement)
            else:
                buff.cursor_position = old_cursor + len(replacement) - len(current_match.group(0))
            return True, ""

        _open_editor(
            {
                "title": f"pasted #{paste_id}",
                "text": payload,
                "on_save": _save_paste,
            }
        )

    def _close_editor() -> None:
        if not editor_box["on"]:
            return
        if drag_capture["target"] == "editor":
            _stop_drag_capture()
        editor_box["on"] = False
        editor_box["on_save"] = None
        try:
            get_app().layout.focus(input_area)
        except Exception:
            pass
        _safe_invalidate()

    def _editor_save() -> None:
        on_save = editor_box.get("on_save")
        if not callable(on_save):
            _close_editor()
            return
        try:
            ok, message = on_save(editor_area.text)
        except Exception as exc:  # noqa: BLE001 - never crash the UI on a save
            ok, message = False, f"Save failed: {exc}"
        if ok:
            _close_editor()
            if message:
                transcript.append("system", str(message))
            scroll["follow"] = True
        else:
            # Keep editing so the user can fix the error (shown in the status bar).
            editor_box["status"] = str(message or "Invalid input.")
        _safe_invalidate()

    # ---- approval modal (centered Float; shown while an approval is pending) ----
    def _approval_fragments() -> FormattedText:
        request = approval_box.get("request")
        if request is None:
            return FormattedText([])
        rows = _render_approval_rows(
            request,
            _approval_content_width_for(_current_width()),
            max_body_lines=_approval_body_budget(_current_rows()),
        )
        fragments: list[tuple[str, str]] = []
        for index, row in enumerate(rows):
            if index:
                fragments.append(("", "\n"))
            fragments.extend(row)
        return FormattedText(fragments)

    def _approval_width() -> int:
        return _approval_width_for(_current_width())

    def _approval_height() -> int:
        request = approval_box.get("request")
        if request is None:
            return 6
        rows = _current_rows()
        content = len(
            _render_approval_rows(
                request,
                _approval_content_width_for(_current_width()),
                max_body_lines=_approval_body_budget(rows),
            )
        )
        return max(6, min(content + _FRAME_COLS, rows - 4))

    approval_window = Window(
        FormattedTextControl(_approval_fragments, focusable=False, show_cursor=False),
        wrap_lines=True,
        style="class:tui.approve",
    )
    # Static amber border (Frame.style must be a string); destructive actions are
    # signalled by the red headline + red [n] key inside the body instead.
    approval_frame = Frame(
        approval_window, title="Approval required", style="class:tui.approve.frame"
    )
    approval_float = Float(
        content=ConditionalContainer(approval_frame, filter=_approval_pending),
        width=_approval_width,
        height=_approval_height,
    )

    # ---- /config overlay (full-screen modal driven by ConfigFlow) ----
    # Built only when the host wires a flow factory. It hosts the whole interactive
    # configuration menu inside this same Application (the chat transcript/session
    # survive), so bare `/config` no longer drops to a read-only panel.
    config_overlay = None
    if config_flow_factory is not None:
        from .config_overlay import ConfigOverlay

        def _on_config_overlay_saved(count: int) -> None:
            if running["on"] or retiring["on"] or False:
                _stage_pending_config_reload(pending_operations)
                if count > 0:
                    word = "change" if count == 1 else "changes"
                    transcript.append(
                        "system",
                        f"Configuration saved ({count} {word}). Session reload applies "
                        "at your next message.",
                    )
                else:
                    transcript.append(
                        "system",
                        "Configuration saved (no changes). Session reload applies "
                        "at your next message.",
                    )
                _safe_invalidate()
                return

            reload_outcome = _reload_saved_config()
            if reload_outcome is ConfigReloadOutcome.APPLIED and count > 0:
                word = "change" if count == 1 else "changes"
                transcript.append(
                    "system",
                    f"Configuration saved ({count} {word}). New settings apply on the next turn.",
                )
            elif reload_outcome is ConfigReloadOutcome.APPLIED:
                transcript.append("system", "Configuration saved (no changes).")
            _safe_invalidate()

        def _on_config_switch_workspace(path: str) -> None:
            # Exit the TUI with a sentinel result; the chat loop relaunches a fresh
            # session bound to this folder (the live workspace root can't change in
            # place). The transcript/session of the current project end here.
            try:
                get_app().exit(result=("switch_workspace", str(path)))
            except Exception:  # noqa: BLE001 - never crash on teardown
                pass

        config_overlay = ConfigOverlay(
            flow_factory=config_flow_factory,
            on_saved=_on_config_overlay_saved,
            on_error=lambda msg: (transcript.append("error", msg), _safe_invalidate()),
            on_switch_workspace=_on_config_switch_workspace,
            focus_chat=lambda: get_app().layout.focus(input_area),
        )
    _config_open = (
        config_overlay.open_condition if config_overlay is not None else Condition(lambda: False)
    )
    _config_floats = [config_overlay.float] if config_overlay is not None else []
    report_overlay = None
    if report_flow_factory is not None:
        from .report_overlay import ReportOverlay

        def _on_report_closed(flow: Any) -> None:
            if flow.receipt is not None:
                transcript.append(
                    "system",
                    f"Feedback sent privately to Alysis. Report ID: {flow.receipt.report_id}",
                )
            if flow.result is not None:
                transcript.append(
                    "system",
                    f"Feedback bundle archive: {flow.result.zip_path}\nThe archive was not uploaded.",
                )
            _safe_invalidate()

        report_overlay = ReportOverlay(
            flow_factory=lambda: report_flow_factory(session, ""),
            on_closed=_on_report_closed,
            on_error=lambda msg: (transcript.append("error", msg), _safe_invalidate()),
            focus_chat=lambda: get_app().layout.focus(input_area),
        )
        _config_open = _config_open | report_overlay.open_condition
        _config_floats.append(report_overlay.float)
    _other_modal_open = (
        _help_open
        | _picker_open
        | _editor_open
        | _approval_pending
        | _diff_open
        | _review_open
        | _navigation_open
    )
    from .question_panel import QuestionPanel

    question_panel = (
        QuestionPanel(
            broker=surface.user_questions,
            restore_focus=lambda: get_app().layout.focus(input_area),
            blocked=lambda: bool(_other_modal_open() or _config_open()),
            on_answer=lambda text: transcript.append_user(text),
        )
        if surface is not None
        else None
    )
    _question_open = (
        question_panel.open_condition if question_panel is not None else Condition(lambda: False)
    )
    _small_modal_open = _other_modal_open | _question_open
    _completion_allowed = ~_small_modal_open & ~_config_open

    def _completion_float_width() -> int:
        try:
            cols = get_app().output.get_size().columns
        except Exception:
            cols = 80
        return _completion_menu_width(cols)

    def _completion_float_height() -> int:
        try:
            rows = get_app().output.get_size().rows
        except Exception:
            rows = 24
        return _completion_menu_height(rows)

    completion_float = Float(
        xcursor=True,
        bottom=_COMPLETION_MENU_BOTTOM,
        width=_completion_float_width,
        height=_completion_float_height,
        content=CompletionsMenu(
            max_height=_COMPLETION_MENU_MAX_HEIGHT,
            scroll_offset=1,
            extra_filter=_completion_allowed,
        ),
    )
    # There used to be a full-screen scrim Float here painting spaces over
    # the transcript whenever a small modal was up. In a terminal that is a
    # blackout, not a dim — the transcript (and all context) became unreadable
    # exactly while a modal asked the user to judge it. The
    # transcript now dims its own text instead (see _transcript_fragments), so
    # the modal stays dominant while the content behind it remains legible.

    def _settle_transcript_link_press(mouse_event: MouseEvent) -> bool:
        """Resolve a pending link press from a raw screen event; True once opened.

        Compared as screen cells, as the tip link does: the projection used for
        selection measures columns in cells, so a wide character earlier in the
        row would otherwise make a genuine click look like a drag.
        """
        press = link_press["transcript"]
        if press is None or mouse_event.event_type not in {
            MouseEventType.MOUSE_MOVE,
            MouseEventType.MOUSE_UP,
        }:
            return False
        url, point = press
        cell = _transcript_link_cell(point)
        rows = selection.get("rows") or []
        cell_width = max(1, get_cwidth(rows[point.y][point.x])) if cell is not None else 1
        if cell is None or not (
            mouse_event.position.y == cell[0]
            and cell[1] <= mouse_event.position.x < cell[1] + cell_width
        ):
            # The pointer left the link: from here on this is a selection drag.
            link_press["transcript"] = None
            return False
        if mouse_event.event_type != MouseEventType.MOUSE_UP:
            return False
        link_press["transcript"] = None
        selection.update({"anchor": None, "active": None, "dragging": False})
        selection["planmeta_press"] = False
        _stop_drag_capture()
        _open_link(url)
        return True

    def _captured_drag_mouse_event(mouse_event: MouseEvent) -> Any:
        target = drag_capture.get("target")
        if target == "tip_link":
            return _tip_link_mouse_event(mouse_event)
        if target == "transcript" and mouse_event.event_type in {
            MouseEventType.SCROLL_UP,
            MouseEventType.SCROLL_DOWN,
        }:
            link_press["transcript"] = None
        if target == "transcript" and _settle_transcript_link_press(mouse_event):
            return None
        if target == "transcript":
            target_window = transcript_window
        elif target == "editor":
            target_window = editor_area.window
        else:
            return NotImplemented
        projected = _project_mouse_event_to_window(mouse_event, target_window)
        if projected is None:
            if mouse_event.event_type == MouseEventType.MOUSE_UP:
                _stop_drag_capture()
            return None

        target_event, direction = projected
        if target == "transcript":
            result = _transcript_mouse_event(target_event, captured=True)
        elif target == "editor":
            result = _editor_mouse_handler(target_event)

        if mouse_event.event_type == MouseEventType.MOUSE_MOVE:
            _set_drag_scroll_direction(direction)
        elif mouse_event.event_type == MouseEventType.MOUSE_UP:
            _stop_drag_capture()
        return result

    drag_capture_active = Condition(lambda: drag_capture["target"] is not None)
    drag_capture_float = Float(
        content=ConditionalContainer(
            _MouseCaptureWindow(_captured_drag_mouse_event),
            filter=drag_capture_active,
        ),
        left=0,
        right=0,
        top=0,
        bottom=0,
        z_index=10**6,
        transparent=True,
    )

    root_container = FloatContainer(
        content=root,
        floats=[
            # Slash-command dropdown: horizontally follows the cursor, but is
            # vertically pinned above the input/footer chrome so it never eats the
            # input frame border.
            completion_float,
            # Opaque backing for the smaller centered modals. /config already owns
            # its full-screen opaque float; these panels need the same masking.
            help_float,
            picker_float,
            editor_float,
            diff_float,
            review_float,
            navigation_float,
            *([question_panel.float] if question_panel is not None else []),
            # The /config overlay covers the whole screen while open.
            *_config_floats,
            # Approval modal sits on top — it interrupts a running turn for a y/a/n.
            approval_float,
            # While selecting, retain drag/release events beyond the source window.
            drag_capture_float,
        ],
    )

    # ---- key bindings ----
    kb = KeyBindings()
    _input_focused = has_focus(input_area)
    _input_has_selection = Condition(lambda: input_area.buffer.selection_state is not None)

    def _registered_paste_match_at_cursor() -> re.Match[str] | None:
        buff = input_area.buffer
        match = _paste_token_at_cursor(buff.text, buff.cursor_position)
        if match is None or paste_registry.get(int(match.group("id"))) is None:
            return None
        return match

    def _registered_paste_match_before_cursor() -> re.Match[str] | None:
        buff = input_area.buffer
        match = _paste_token_before_cursor(buff.text, buff.cursor_position)
        if match is None or paste_registry.get(int(match.group("id"))) is None:
            return None
        return match

    def _registered_paste_match_after_cursor() -> re.Match[str] | None:
        buff = input_area.buffer
        match = _paste_token_after_cursor(buff.text, buff.cursor_position)
        if match is None or paste_registry.get(int(match.group("id"))) is None:
            return None
        return match

    def _registered_attachment_match_before_cursor() -> re.Match[str] | None:
        buff = input_area.buffer
        match = _attachment_token_before(buff.text, buff.cursor_position)
        if match is None or not attachment_registry.is_live_token(match.group(0)):
            return None
        return match

    def _registered_attachment_match_after_cursor() -> re.Match[str] | None:
        buff = input_area.buffer
        match = _attachment_token_after(buff.text, buff.cursor_position)
        if match is None or not attachment_registry.is_live_token(match.group(0)):
            return None
        return match

    _input_attachment_token_before_cursor = _input_focused & Condition(
        lambda: _registered_attachment_match_before_cursor() is not None
    )
    _input_attachment_token_after_cursor = _input_focused & Condition(
        lambda: _registered_attachment_match_after_cursor() is not None
    )

    _input_paste_token_at_cursor = _input_focused & Condition(
        lambda: _registered_paste_match_at_cursor() is not None
    )
    _input_single_paste_token = _input_focused & Condition(lambda: len(_live_paste_matches()) == 1)
    _input_paste_editor_available = _input_paste_token_at_cursor | _input_single_paste_token
    _input_paste_token_before_cursor = _input_focused & Condition(
        lambda: _registered_paste_match_before_cursor() is not None
    )
    _input_paste_token_after_cursor = _input_focused & Condition(
        lambda: _registered_paste_match_after_cursor() is not None
    )
    # True while the slash-command dropdown is showing for the input buffer.
    _completing = Condition(
        lambda: input_area.buffer.complete_state is not None or completion_request["pending"]
    )
    _has_children = Condition(lambda: bool(subagent_panel["run_order"]))
    _subagent_panel_open = Condition(lambda: bool(subagent_panel["selected_run_id"]))
    # A turn (or its pending approval) is in flight — Esc / Ctrl+C interrupt it.
    _turn_active = Condition(lambda: running["on"] or approval_box.get("event") is not None)
    _input_empty = Condition(lambda: not input_area.buffer.text)
    _has_queued_messages = Condition(lambda: bool(pending_turns))

    def _navigate_subagent(delta: int) -> None:
        previous = str(subagent_panel["selected_run_id"] or "")
        _cycle_subagent_panel(subagent_panel, delta)
        _evict_subagent_panel_runs(
            subagent_panel,
            departed_run_id=previous,
        )

    def _close_selected_subagent() -> None:
        previous = str(subagent_panel["selected_run_id"] or "")
        subagent_panel["selected_run_id"] = ""
        subagent_panel["last_poll"] = None
        _evict_subagent_panel_runs(
            subagent_panel,
            departed_run_id=previous,
        )

    def _interrupt_or_exit(event: Any, *, copy_selection: bool) -> None:
        if _file_mutation_active():
            transcript.append("system", "Local action is still finishing; waiting for its result.")
            return
        if _navigation_open():
            navigation_box["modal"].close()
            return
        if _review_open():
            review_box["modal"].close()
            return
        if _diff_open():
            diff_box["viewer"].close()
            return
        if report_overlay is not None and report_overlay.is_open():
            report_overlay.request_cancel()
            return
        if config_overlay is not None and config_overlay.is_open():
            config_overlay.request_cancel()
            return
        if help_box["on"]:
            _close_help()
            return
        if picker_box["on"]:
            _close_picker()
            return
        if editor_box["on"]:
            _close_editor()
            return
        if running["on"] or approval_box.get("event") is not None:
            _soft_interrupt()
            return
        if copy_selection:
            selected = _current_transcript_selection()
            if selected:
                selection.update({"anchor": None, "active": None, "dragging": False})
                threading.Thread(
                    target=_copy_transcript_selection,
                    args=(selected,),
                    daemon=True,
                ).start()
                event.app.invalidate()
                return
        event.app.exit(result=None)

    @kb.add("c-c")
    def _copy_interrupt_or_exit(event: Any) -> None:
        _interrupt_or_exit(event, copy_selection=True)

    @kb.add("c-d")
    def _eof_or_exit(event: Any) -> None:
        _interrupt_or_exit(event, copy_selection=False)

    def _previous_subagent(event: Any) -> None:
        _navigate_subagent(-1)
        event.app.invalidate()

    def _next_subagent(event: Any) -> None:
        _navigate_subagent(1)
        event.app.invalidate()

    @kb.add(
        "escape",
        filter=_input_focused
        & _subagent_panel_open
        & ~_small_modal_open
        & ~_completing
        & ~_config_open,
        eager=False,
    )
    def _close_subagent_panel(event: Any) -> None:
        _close_selected_subagent()
        event.app.invalidate()

    # ---- in-TUI editor keys (only while the editor float is open) ----
    @kb.add("c-s", filter=_editor_open)
    def _editor_save_key(event: Any) -> None:
        _editor_save()

    @kb.add("escape", filter=_editor_open)  # non-eager so arrow ESC-sequences parse
    def _editor_cancel_key(event: Any) -> None:
        _close_editor()

    @kb.add(
        "escape",
        filter=_turn_active
        & ~_small_modal_open
        & ~_completing
        & ~_config_open
        & ~_file_mutation_active
        & ~_subagent_panel_open,
        eager=False,
    )
    def _escape_interrupt(event: Any) -> None:
        # Esc interrupts after prefix resolution, allowing Alt+r to work. Ctrl+C
        # is immediate; Vim owns Esc for its editing mode. Gated to a
        # live turn so it doesn't shadow Esc's other roles (cancel completion, close
        # help/picker).
        _soft_interrupt()

    @kb.add(
        Keys.ShiftLeft,
        filter=_input_focused
        & _turn_active
        & _input_empty
        & _has_queued_messages
        & ~_small_modal_open
        & ~_config_open
        & ~_completing,
        eager=True,
    )
    def _edit_latest_queued_message(event: Any) -> None:
        _recall_latest_queued_message()

    @kb.add(
        "enter",
        filter=_input_focused & ~_small_modal_open & ~_config_open,
        eager=True,
    )
    def _submit_key(event: Any) -> None:
        # When the slash dropdown has a highlighted row, Enter accepts it instead
        # of submitting (so you can pick a command, then edit args / submit). With
        # the menu merely open but nothing highlighted, Enter submits as normal.
        buff = input_area.buffer
        state = buff.complete_state
        if state is not None and state.current_completion is not None:
            buff.apply_completion(state.current_completion)
            return
        _submit()

    @kb.add("c-j", filter=_input_focused & ~_small_modal_open & ~_config_open)
    def _newline(event: Any) -> None:
        input_area.buffer.insert_text("\n")

    @kb.add(
        Keys.BracketedPaste,
        filter=_input_focused & ~_small_modal_open & ~_config_open,
        eager=True,
    )
    def _paste_into_input(event: Any) -> None:
        payload = _normalize_paste_text(event.data)
        # A file dropped on the terminal arrives as its path, in a paste. When
        # the payload is nothing but existing paths, it was a drop (or a
        # deliberate path paste): chip it instead of spilling the path into the
        # message. Anything mixed with prose stays an ordinary paste.
        dropped = detect_dropped_paths(payload)
        if dropped and _attach_paths(dropped):
            return
        line_count = _paste_line_count(payload)
        if line_count <= _INLINE_PASTE_MAX_LINES and len(payload) <= _INLINE_PASTE_MAX_CHARS:
            input_area.buffer.insert_text(payload)
            return

        paste_id = paste_registry.add(payload)
        input_area.buffer.insert_text(_paste_token(paste_id, payload))
        input_area.buffer.cancel_completion()

    @kb.add(
        "backspace",
        filter=_input_paste_token_before_cursor & ~_input_has_selection,
        eager=True,
    )
    def _remove_paste_token_before_cursor(event: Any) -> None:
        match = _registered_paste_match_before_cursor()
        if match is not None:
            input_area.buffer.delete_before_cursor(count=len(match.group(0)))

    @kb.add(
        Keys.Delete,
        filter=_input_paste_token_after_cursor & ~_input_has_selection,
        eager=True,
    )
    def _remove_paste_token_after_cursor(event: Any) -> None:
        match = _registered_paste_match_after_cursor()
        if match is not None:
            input_area.buffer.delete(count=len(match.group(0)))

    # A chip is one object to the user, so one Backspace removes the whole
    # "[Image #1]" (and detaches the file) instead of leaving "[Image #".
    @kb.add(
        "backspace",
        filter=_input_attachment_token_before_cursor & ~_input_has_selection,
        eager=True,
    )
    def _remove_attachment_token_before_cursor(event: Any) -> None:
        match = _registered_attachment_match_before_cursor()
        if match is not None:
            input_area.buffer.delete_before_cursor(count=len(match.group(0)))

    @kb.add(
        Keys.Delete,
        filter=_input_attachment_token_after_cursor & ~_input_has_selection,
        eager=True,
    )
    def _remove_attachment_token_after_cursor(event: Any) -> None:
        match = _registered_attachment_match_after_cursor()
        if match is not None:
            input_area.buffer.delete(count=len(match.group(0)))

    @kb.add(
        "c-v",
        filter=_input_focused & ~_small_modal_open & ~_config_open,
        eager=True,
    )
    def _attach_clipboard_image_key(event: Any) -> None:
        _record_tui_local_interaction(session=session, action="attach_clipboard_image")
        _attach_clipboard_image()

    @kb.add(
        "c-q",
        filter=_input_focused & ~_small_modal_open & ~_config_open & ~_file_mutation_active,
        eager=True,
    )
    def _queue_key(event: Any) -> None:
        # Enter and Shift+Enter are byte-identical in ordinary terminals unless
        # an extended keyboard protocol is negotiated. Ctrl+Q is distinct, and
        # prompt_toolkit raw mode disables IXON so it is not swallowed as XOFF.
        _submit(queue_instead=True)

    # ---- command and workspace-reference dropdown ----
    @kb.add(
        "tab",
        filter=_input_focused & ~_help_open & ~_approval_pending & ~_config_open,
        eager=True,
    )
    def _complete_tab(event: Any) -> None:
        buff = input_area.buffer
        if not _complete_while_typing():
            return
        is_mention = mention_at_cursor(buff.text, buff.cursor_position) is not None
        if buff.complete_state is not None:
            if is_mention:
                state = buff.complete_state
                if not state.completions:
                    request_explicit_completion()
                    return
                selected = state.current_completion or state.completions[0]
                buff.apply_completion(selected)
            else:
                # Preserve slash/skill cycling; references accept the selected row.
                buff.complete_next()
        elif is_mention or buff.document.text_before_cursor.lstrip().startswith(("/", "$")):
            request_explicit_completion()
        elif persona_cycle is not None and not buff.document.text.strip() and not False:
            if running["on"]:
                _stage_tab_persona()
                return
            try:
                messages = persona_cycle()
            except Exception as exc:  # noqa: BLE001 - never crash the UI on Tab
                messages = [("error", f"persona cycle failed: {exc}")]
            else:
                _record_tui_local_interaction(session=session, action="persona_cycle")
            for role, text in messages or []:
                transcript.append(role, text)
            _safe_invalidate()

    @kb.add("down", filter=_input_focused & _completing, eager=True)
    def _complete_down(event: Any) -> None:
        input_area.buffer.complete_next()

    @kb.add("up", filter=_input_focused & _completing, eager=True)
    def _complete_up(event: Any) -> None:
        input_area.buffer.complete_previous()

    @kb.add("escape", filter=_input_focused & _completing, eager=True)
    def _complete_cancel(event: Any) -> None:
        input_area.buffer.cancel_completion()

    # Up/Down step through the composer's visual rows, so a long wrapped line is
    # walked like the editor it looks like. From the first/last row they fall
    # through to accepted-prompt history, preserving the unsent draft.
    composer_goal: dict[str, Any] = {"column": None, "cursor": None, "text": None}

    def _composer_vertical(event: Any, delta: int) -> None:
        buff = input_area.buffer
        for step in range(max(1, event.arg)):
            # Keep the column aimed for across consecutive moves, so passing
            # through a short row doesn't pull the cursor to the left for good.
            same_spot = (
                composer_goal["cursor"] == buff.cursor_position
                and composer_goal["text"] == buff.text
            )
            moved = vertical_move(
                buff.text,
                buff.cursor_position,
                _composer_width(),
                delta,
                goal=composer_goal["column"] if same_spot else None,
            )
            if moved is None:
                remaining = max(1, event.arg) - step
                for _ in range(remaining):
                    _history_move(delta)
                return
            buff.cursor_position, composer_goal["column"] = moved
            composer_goal["cursor"], composer_goal["text"] = buff.cursor_position, buff.text

    _composer_keys = _input_focused & ~_completing & ~_small_modal_open & ~_config_open

    @kb.add(
        "up",
        filter=_composer_keys
        & Condition(lambda: not False or app.vi_state.input_mode == InputMode.INSERT),
    )
    def _composer_up(event: Any) -> None:
        _composer_vertical(event, -1)

    @kb.add(
        "down",
        filter=_composer_keys
        & Condition(lambda: not False or app.vi_state.input_mode == InputMode.INSERT),
    )
    def _composer_down(event: Any) -> None:
        _composer_vertical(event, 1)

    # ---- /help popup keys (only while the popup is open) ----
    @kb.add("escape", filter=_help_open)
    def _help_escape(event: Any) -> None:
        _close_help()

    @kb.add("enter", filter=_help_open, eager=True)
    def _help_confirm(event: Any) -> None:
        # Enter closes the panel; for a confirm panel it also acts. The callback
        # callback form takes precedence; otherwise the stored command runs
        # (e.g. the /forge intro).
        # Esc/q cancel. on_confirm returns either a list of (role, text) rows to
        # echo, or a dict {"messages"?: [...], "picker"?: {...}} whose ``picker``
        # chains a follow-up selection popup (e.g. the /forge intro → "Use the
        # planner?" prompt before entering Forge).
        confirm = help_box.get("confirm")
        on_confirm = help_box.get("on_confirm")
        _close_help()
        if on_confirm is not None:
            try:
                result = on_confirm()
            except Exception as exc:  # noqa: BLE001 - never crash the UI on confirm
                transcript.append("error", f"Confirm action failed: {exc}")
                result = None
            messages = result
            picker_spec: dict[str, Any] | None = None
            if isinstance(result, dict):
                messages = result.get("messages")
                picker_spec = result.get("picker")
            for role, text in messages or []:
                transcript.append(role, text)
            if picker_spec:
                _open_picker(picker_spec)
            _safe_invalidate()
            return
        if confirm:
            _dispatch_command(str(confirm))

    @kb.add("q", filter=_help_open, eager=True)
    def _help_dismiss(event: Any) -> None:
        # q is always a plain dismiss — i.e. cancel for a confirm panel.
        _close_help()

    @kb.add("up", filter=_help_open, eager=True)
    @kb.add("k", filter=_help_open, eager=True)
    def _help_scroll_up(event: Any) -> None:
        _help_scroll(-1)

    @kb.add("down", filter=_help_open, eager=True)
    @kb.add("j", filter=_help_open, eager=True)
    def _help_scroll_down(event: Any) -> None:
        _help_scroll(1)

    @kb.add("home", filter=_help_open, eager=True)
    def _help_to_top(event: Any) -> None:
        help_box["offset"] = 0
        _safe_invalidate()

    @kb.add("end", filter=_help_open, eager=True)
    def _help_to_bottom(event: Any) -> None:
        help_box["offset"] = _help_follow_top()
        _safe_invalidate()

    @kb.add("<any>", filter=Condition(lambda: history_search["on"]), eager=True)
    def _history_search_type(event: Any) -> None:
        if event.data and event.data.isprintable():
            history_search["query"] += event.data
            _refresh_history_search()

    @kb.add("backspace", filter=Condition(lambda: history_search["on"]), eager=True)
    def _history_search_backspace(event: Any) -> None:
        history_search["query"] = history_search["query"][:-1]
        _refresh_history_search()

    # ---- picker popup keys (only while a picker is open) ----
    @kb.add("escape", filter=_picker_open, eager=True)
    @kb.add("q", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    def _picker_cancel(event: Any) -> None:
        _close_picker()

    @kb.add("up", filter=_picker_open, eager=True)
    @kb.add("k", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    def _picker_up(event: Any) -> None:
        _picker_move(-1)

    @kb.add("down", filter=_picker_open, eager=True)
    @kb.add("j", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    def _picker_down(event: Any) -> None:
        _picker_move(1)

    @kb.add("enter", filter=_picker_open, eager=True)
    def _picker_enter(event: Any) -> None:
        _picker_choose(int(picker_box["index"]))

    # Number keys pick (and apply) the matching option directly.
    @kb.add("1", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    @kb.add("2", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    @kb.add("3", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    @kb.add("4", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    @kb.add("5", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    @kb.add("6", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    @kb.add("7", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    @kb.add("8", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    @kb.add("9", filter=_picker_open & ~Condition(lambda: history_search["on"]), eager=True)
    def _picker_digit(event: Any) -> None:
        try:
            _picker_choose(int(event.data) - 1)
        except (TypeError, ValueError):
            pass

    @kb.add(
        "s-tab",
        filter=~_config_open & ~_small_modal_open & ~_file_mutation_active,
        eager=True,
    )
    def _cycle_exec_mode(event: Any) -> None:
        # Shift+Tab advances the next-message selection (read → safe → fast →
        # full → read), the same primitive as the /permissions picker. The
        # callback stages the selection and returns rows to echo — including the
        # fullaccess warning, so selecting the unguarded mode is never silent.
        # Without a callback (Phase 1 shell, tests) the key is inert rather than
        # flipping a footer field the runtime would not honour.
        if mode_cycle is None or False:
            return
        try:
            messages = mode_cycle()
        except Exception as exc:  # noqa: BLE001 - never crash the UI on Shift+Tab
            messages = [("error", f"mode cycle failed: {exc}")]
        else:
            _record_tui_local_interaction(session=session, action="permissions_cycle")
        for role, text in messages or []:
            transcript.append(role, text)
        _safe_invalidate()

    @kb.add("y", filter=_approval_pending, eager=True)
    def _approve_yes(event: Any) -> None:
        _resolve_approval(allow=True)

    @kb.add("a", filter=_approval_pending, eager=True)
    def _approve_always(event: Any) -> None:
        _resolve_approval(allow=True, always=True)

    @kb.add("d", filter=_approval_pending, eager=True)
    def _approve_always_dir(event: Any) -> None:
        # Folder-wide session grant: only meaningful when the pending
        # request has a coverable directory; otherwise the key is inert so a
        # stray "d" cannot silently allow anything.
        from ...approval_scope import approval_dir_grant_candidate

        request = approval_box.get("request")
        if request is None or approval_dir_grant_candidate(request) is None:
            return
        _resolve_approval(allow=True, always_dir=True)

    @kb.add("n", filter=_approval_pending, eager=True)
    def _approve_no(event: Any) -> None:
        _resolve_approval(allow=False)

    def _toggle_reasoning(event: Any) -> None:
        # Expand/collapse provider-generated reasoning-summary blocks.
        view["reasoning_expanded"] = not view["reasoning_expanded"]
        event.app.invalidate()

    def _toggle_plan_meta(event: Any) -> None:
        # Expand/collapse Forge planner meta / plan-reconciliation note blocks.
        view["planmeta_expanded"] = not view["planmeta_expanded"]
        event.app.invalidate()

    # ---- scrollback (works while the input keeps focus) ----
    @kb.add("pageup", filter=_help_open, eager=True)
    def _help_pageup(event: Any) -> None:
        _help_scroll(-_help_page_rows())

    @kb.add("pagedown", filter=_help_open, eager=True)
    def _help_pagedown(event: Any) -> None:
        _help_scroll(_help_page_rows())

    @kb.add("pageup", filter=~_small_modal_open & ~_config_open, eager=True)
    def _scroll_up(event: Any) -> None:
        _scroll_move(-_scroll_page_rows())
        event.app.invalidate()

    @kb.add("pagedown", filter=~_small_modal_open & ~_config_open, eager=True)
    def _scroll_down(event: Any) -> None:
        _scroll_move(_scroll_page_rows())
        event.app.invalidate()

    @kb.add("c-home", filter=~_small_modal_open & ~_config_open, eager=True)
    def _scroll_top(event: Any) -> None:
        scroll["follow"] = False
        scroll["offset"] = 0
        event.app.invalidate()

    @kb.add("c-end", filter=~_small_modal_open & ~_config_open, eager=True)
    def _scroll_bottom(event: Any) -> None:
        scroll["follow"] = True
        event.app.invalidate()

    # The /config overlay adds its own key bindings (gated on its open state) and
    # needs the setup wizard's style classes merged in for its panels to render.
    _app_style = _build_tui_style(terminal_theme)
    if config_overlay is not None:
        config_overlay.register(kb)
    if report_overlay is not None:
        report_overlay.register(kb)
    question_bindings = KeyBindings()
    if question_panel is not None:
        question_panel.register(question_bindings)
    if config_overlay is not None or report_overlay is not None:
        from .setup_app import _build_setup_style

        _app_style = merge_styles([_app_style, _build_setup_style(terminal_theme)])

    for keys, handler in [
        ("c-p", lambda event: _open_diff()),
        ("c-r", lambda event: _open_history_search()),
        ("escape r", _toggle_reasoning),
        ("escape p", lambda event: _open_paste_editor()),
        ("escape left", _previous_subagent),
        ("escape right", _next_subagent),
        ("c-o", _toggle_plan_meta),
    ]:
        kb.add(*keys.split(), filter=_input_focused & ~_small_modal_open & ~_config_open)(handler)
    tui_input, owned_tui_input = _resolve_tui_input(input)
    app: Application = Application(
        layout=Layout(root_container, focused_element=input_area),
        key_bindings=merge_key_bindings(
            [
                selection_editing_bindings(),
                kb,
                # Pending-question controls own their documented keys while available.
                question_bindings,
            ]
        ),
        style=_app_style,
        full_screen=True,
        # Mouse reporting supports transcript wheel/drag selection and can be
        # disabled live. Completed selections are copied automatically.
        mouse_support=True,
        cursor=CursorShape.BEAM,
        input=tui_input,
        output=output,
        # Cap repaints at ~30 FPS. A streaming turn invalidates on every token
        # (transcript._touch → invalidate), which otherwise pins the terminal at
        # the back-to-back repaint rate — a flood of near-full-screen diffs that
        # tears and lags on a slower pty (e.g. WSL → Windows Terminal). Coalescing
        # the token flood into a steady cadence keeps streaming smooth. Kept above
        # the 0.1s spinner/clock cadence so "thinking…" still animates. Pairs with
        # the memoized markdown render, which makes each repaint cheap enough for
        # the cap to actually bite (a redraw slower than the interval defeats it).
        min_redraw_interval=0.033,
    )
    # Before each key (not only repaint), keep non-composer editors/navigation
    # in their fixed Emacs mode, including keys sent before a modal's first frame.

    # ---- animation driver: owl while idle, spinner while a turn runs ----
    def _pre_run() -> None:
        if open_config_on_start and config_overlay is not None:
            config_overlay.open()

        def _spin() -> None:
            while getattr(app, "is_running", False):
                time.sleep(0.1)
                if question_panel is not None and app.loop is not None and not app.loop.is_closed():
                    app.loop.call_soon_threadsafe(question_panel.sync)
                animated = False
                reduced_animation = False
                if not reduced_animation and owl.available and not _conversation_started():
                    owl.advance()
                    animated = True
                if not reduced_animation and (running["on"] or transcript.status):
                    spinner["i"] = (spinner["i"] + 1) % len(_SPINNER_FRAMES)
                    animated = True
                scheduler = getattr(session, "child_scheduler", None)
                if _poll_selected_subagent(
                    subagent_panel,
                    scheduler,
                    on_failure=_report_subagent_poll_failure,
                ):
                    animated = True
                if (
                    not reduced_animation
                    and config_overlay is not None
                    and config_overlay.is_busy()
                ):
                    config_overlay.tick_spinner()
                    animated = True
                if (
                    not reduced_animation
                    and report_overlay is not None
                    and report_overlay.is_busy()
                ):
                    report_overlay.tick_spinner()
                    animated = True
                if animated:
                    try:
                        app.invalidate()
                    except Exception:
                        break

        threading.Thread(target=_spin, daemon=True).start()

    try:
        result = app.run(pre_run=_pre_run)
    finally:
        if surface is not None:
            surface.user_questions.reset()
        # Even a failed app run must unwind the actual worker before its caller
        # closes the session. Soft cancellation alone is not a capture boundary.
        token = cancel_box.get("token")
        if token is not None:
            token.cancel_nonblocking()
        pending = approval_box.get("event")
        if pending is not None:
            approval_box["decision"] = ApprovalDecision(allow=False)
            pending.set()
        worker = worker_box.get("thread")
        if worker is not None and worker.is_alive():
            worker.join()
        if _navigation_open():
            navigation_box["modal"].close()
        navigation_executor.shutdown(wait=False, cancel_futures=True)
        if local_lifecycle is not None:
            local_lifecycle.shutdown()
        if _review_open():
            review_box["modal"].close()
        if _diff_open():
            diff_box["viewer"].close()
        composer_completer.close()
        if report_overlay is not None:
            report_overlay.shutdown()
        _stop_drag_capture()
        if owned_tui_input is not None:
            owned_tui_input.close()

    # Unwind any in-flight turn before returning so the caller can close the
    # session safely (no teardown racing a live worker). The worker remains the
    # sole owner of per-turn permission restoration and other session mutation;
    # cancellation must not restore from this UI thread while it is still live.
    token = cancel_box.get("token")
    if token is not None:
        token.cancel_nonblocking()
    pending = approval_box.get("event")
    if pending is not None:
        approval_box["decision"] = ApprovalDecision(allow=False)
        pending.set()
    worker = worker_box.get("thread")
    if worker is not None and worker.is_alive():
        # No elapsed-time guess makes concurrent AgentSession mutation safe. A
        # non-cooperative worker therefore keeps teardown in the retiring state
        # until it releases ownership through its normal ``finally`` path.
        worker.join()
    scheduler = getattr(session, "child_scheduler", None)
    set_lifecycle_listener = getattr(scheduler, "set_lifecycle_listener", None)
    if callable(set_lifecycle_listener):
        set_lifecycle_listener(None)

    return result, transcript.entries


__all__ = ["run_tui"]
