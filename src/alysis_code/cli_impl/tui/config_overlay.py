"""In-app overlay that hosts the TUI configuration menu (:class:`ConfigFlow`).

Unlike the setup wizard (:mod:`cli_impl.tui.setup_app`), which is a standalone
:class:`~prompt_toolkit.application.Application` run before chat, ``/config`` must
open *while the chat application is already running* — the transcript, session, and
scroll position all need to survive. So this is not a second ``Application`` but a
full-screen :class:`~prompt_toolkit.layout.containers.Float` added to the chat app's
``FloatContainer``, plus key bindings registered on the chat app's
``KeyBindings``. It reuses the chat/setup render grammar (the picker renderer, the
``setup.*`` style classes, the cursor-pin scrollable body) so it looks identical to
the rest of the TUI.

The host (:mod:`cli_impl.tui.app`) wires it up:

* appends :attr:`ConfigOverlay.float` to the ``FloatContainer`` floats,
* calls :meth:`ConfigOverlay.register` to add the overlay's key bindings,
* gates its own global bindings with ``& ~overlay.open_condition`` so they don't
  fire underneath the overlay,
* routes bare ``/config`` to :meth:`ConfigOverlay.open` and Ctrl+C to
  :meth:`ConfigOverlay.request_cancel` while open.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    Float,
    HSplit,
    ScrollOffsets,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.processors import (
    BeforeInput,
    ConditionalProcessor,
    PasswordProcessor,
)
from prompt_toolkit.widgets import Frame, TextArea

from .app import (
    _SPINNER_FRAMES,
    _render_picker_rows,
    _transcript_content_width_for,
    _wrap_line,
)
from .setup_app import _TONE_STYLE, _row_to_dict

# Opaque theme-aware background so the full-screen float fully covers the chat
# without pairing a forced dark canvas with light-theme foreground colours.
_BG = "class:tui.config"


class ConfigOverlay:
    """A full-screen ``/config`` overlay driven by a :class:`ConfigFlow`."""

    def __init__(
        self,
        *,
        flow_factory: Callable[[], Any],
        on_saved: Callable[[int], None] | None = None,
        on_error: Callable[[str], None] | None = None,
        on_switch_workspace: Callable[[str], None] | None = None,
        focus_chat: Callable[[], None] | None = None,
        input_multiline: bool = False,
        label: str = "configuration",
    ) -> None:
        self._flow_factory = flow_factory
        self._label = label
        self._on_saved = on_saved
        self._on_error = on_error
        self._on_switch = on_switch_workspace
        self._focus_chat = focus_chat
        self.flow: Any | None = None
        self._open = {"on": False}
        self._busy = {"running": False}
        self._spinner = {"i": 0}
        self._body_cursor = {"row": 0}
        self._last_field: dict[str, Any] = {"key": None}

        self.open_condition = Condition(lambda: self._open["on"])

        # ----------------------------------------------------------- windows
        self._body_window = Window(
            FormattedTextControl(
                self._body_fragments,
                focusable=True,
                get_cursor_position=lambda: Point(0, self._body_cursor["row"]),
            ),
            wrap_lines=True,
            scroll_offsets=ScrollOffsets(top=1, bottom=1),
            right_margins=[ScrollbarMargin(display_arrows=False)],
            style=_BG,
        )
        self._status_window = Window(
            FormattedTextControl(self._status_fragments, focusable=False),
            height=1,
            style=_BG,
        )
        # Mask the input whenever the active input screen asks for it (its
        # ``input_password`` flag) — generic across flows, so both ConfigFlow's
        # api_key stage and PowerConfigFlow's paste-key stage hide the secret.
        _is_password = Condition(
            lambda: (
                self._open["on"]
                and self.flow is not None
                and self.flow.current_mode() == "input"
                and bool(getattr(self.flow.screen(), "input_password", False))
            )
        )
        self._input = TextArea(
            height=Dimension(min=1, preferred=4, max=5) if input_multiline else 1,
            multiline=input_multiline,
            wrap_lines=input_multiline,
            style="class:tui.input",
            input_processors=[
                ConditionalProcessor(PasswordProcessor(), filter=_is_password),
                BeforeInput("> ", style="class:tui.prompt"),
            ],
        )
        self._input_frame = Frame(self._input)

        _is_input = Condition(lambda: self._open["on"] and self._mode() == "input")

        body = HSplit(
            [
                Window(
                    FormattedTextControl(self._header_fragments, focusable=False),
                    height=1,
                    style=_BG,
                ),
                Window(height=1, style=_BG),
                self._body_window,
                self._status_window,
                ConditionalContainer(self._input_frame, filter=_is_input),
                Window(height=1, style=_BG),
                Window(
                    FormattedTextControl(self._footer_fragments, focusable=False),
                    height=1,
                    style=_BG,
                ),
            ],
            style=_BG,
        )
        self.float = Float(
            content=ConditionalContainer(body, filter=self.open_condition),
            left=0,
            right=0,
            top=0,
            bottom=0,
        )

    # ---------------------------------------------------------------- helpers

    def is_open(self) -> bool:
        return self._open["on"]

    def is_busy(self) -> bool:
        return self._open["on"] and self.flow is not None and self.flow.current_mode() == "busy"

    def tick_spinner(self) -> None:
        """Advance the saving spinner one frame (driven by the app's repaint loop)."""
        self._spinner["i"] = (self._spinner["i"] + 1) % len(_SPINNER_FRAMES)

    def _mode(self) -> str:
        return self.flow.current_mode() if self.flow is not None else "message"

    def _invalidate(self) -> None:
        try:
            get_app().invalidate()
        except Exception:
            pass

    def _width(self) -> int:
        # Derived from the terminal, not read back from render_info: the overlay
        # body is a full-screen float (no frame) carrying one scrollbar margin, so
        # it is exactly one column narrower than the screen. render_info is a frame
        # BEHIND — after a resize it still reports the old, wider size, and the
        # rows built from it overflow and hard-wrap into the new, narrower window.
        try:
            cols = get_app().output.get_size().columns
        except Exception:
            return 80
        return max(20, _transcript_content_width_for(cols))

    # ------------------------------------------------------------- rendering

    def _header_fragments(self) -> FormattedText:
        return FormattedText(
            [("class:setup.header", "◇ alysis"), ("class:setup.headerdim", "  ·  config")]
        )

    def _body_fragments(self) -> FormattedText:
        flow = self.flow
        if flow is None:
            return FormattedText([("", "")])
        scr = flow.screen()
        width = self._width()
        rows: list[list[tuple[str, str]]] = []
        if scr.title:
            for line in _wrap_line(scr.title, width):
                rows.append([("class:setup.title", line)])
        if scr.subtitle:
            for paragraph in scr.subtitle.split("\n"):
                for line in _wrap_line(paragraph, width):
                    rows.append([("class:setup.subtitle", line)])
        rows.append([("", "")])
        header_lines = len(rows)
        sel_line = header_lines

        if scr.mode == "list" and scr.rows:
            picker = _render_picker_rows(
                [_row_to_dict(r) for r in scr.rows],
                scr.index,
                width,
                hint=scr.hint,
                numbered=getattr(scr, "numbered", True),
            )
            located = next(
                (i for i, prow in enumerate(picker) if any("selcaret" in s for s, _ in prow)),
                0,
            )
            sel_line = header_lines + located
            rows.extend(picker)
        elif scr.mode == "confirm":
            for text, tone in scr.lines:
                for line in _wrap_line(text, width):
                    rows.append([(_TONE_STYLE.get(tone, "class:setup.text"), line)])
            rows.append([("", "")])
            if scr.hint:
                rows.append([("class:setup.dim", scr.hint)])
        elif scr.mode == "input":
            if scr.input_label:
                for line in _wrap_line(scr.input_label + ":", width):
                    rows.append([("class:setup.text", line)])
            if scr.input_default and not scr.input_password:
                for line in _wrap_line(f"current: {scr.input_default}", width):
                    rows.append([("class:setup.dim", line)])
            # No in-body hint: the footer is the single key legend and shows
            # this screen's action verb (see _footer_fragments).
        elif scr.mode == "busy":
            frame = _SPINNER_FRAMES[self._spinner["i"] % len(_SPINNER_FRAMES)]
            rows.append(
                [
                    ("class:setup.spinner", f"{frame} "),
                    ("class:setup.text", scr.busy_label or "Working…"),
                ]
            )

        self._body_cursor["row"] = sel_line
        out: list[tuple[str, str]] = []
        for row in rows:
            out.extend(row)
            out.append(("", "\n"))
        return FormattedText(out)

    def _status_fragments(self) -> FormattedText:
        flow = self.flow
        if flow is None:
            return FormattedText([("", "")])
        scr = flow.screen()
        if scr.mode == "busy" or not scr.status:
            return FormattedText([("", "")])
        return FormattedText([(_TONE_STYLE.get(scr.status_tone, "class:setup.dim"), scr.status)])

    def _footer_fragments(self) -> FormattedText:
        flow = self.flow
        try:
            width = get_app().output.get_size().columns
        except Exception:
            width = 80
        crumb = flow._breadcrumb() if flow is not None else "configuration"
        left = [
            ("class:setup.footer.brand", "◇ alysis"),
            ("class:setup.footer.dim", f"  ·  {crumb}"),
        ]
        is_menu = flow is not None and getattr(flow, "stage", "") == "menu"
        mode = self._mode()
        if mode == "list":
            if is_menu:
                right_text = "↑↓ move · Enter open · s save · Esc close"
            else:
                # Advertise digit shortcuts only on numbered lists; the footer is
                # the single key legend now that in-body nav hints are gone.
                numbered = True
                if flow is not None:
                    try:
                        numbered = bool(getattr(flow.screen(), "numbered", True))
                    except Exception:
                        numbered = True
                right_text = "↑↓ move · " + ("1-9 pick · " if numbered else "")
                right_text += "Enter select · "
                if self._save_available():
                    right_text += "s save · "
                right_text += "Esc back"
        elif mode == "input":
            # Honour the screen's own action verb ("Enter save", "Enter add",
            # …) so the legend never contradicts what Enter actually does.
            screen_hint = ""
            if flow is not None:
                try:
                    screen_hint = str(getattr(flow.screen(), "hint", "") or "")
                except Exception:
                    screen_hint = ""
            right_text = (
                f"{screen_hint} · Ctrl+C cancel"
                if screen_hint
                else "Enter next · Esc back · Ctrl+C cancel"
            )
        elif mode == "confirm":
            right_text = "Y / N · Esc back"
        elif mode == "busy":
            right_text = "Working…"
        else:
            right_text = "Ctrl+C cancel"
        right = [("class:setup.footer.dim", right_text)]
        left_len = sum(len(t) for _s, t in left)
        right_len = sum(len(t) for _s, t in right)
        gap = max(1, width - left_len - right_len)
        return FormattedText([*left, ("", " " * gap), *right])

    # ------------------------------------------------------------- lifecycle

    def open(self, flow_factory: Callable[[], Any] | None = None) -> None:
        if self._open["on"]:
            return
        try:
            self.flow = (flow_factory or self._flow_factory)()
        except Exception as exc:  # noqa: BLE001 - surface, don't crash the chat UI
            if self._on_error is not None:
                try:
                    self._on_error(f"{self._label} could not open: {exc}")
                except Exception:
                    pass
            self.flow = None
            return
        self._open["on"] = True
        self._busy["running"] = False
        self._spinner["i"] = 0
        self._last_field["key"] = None
        self._pump()

    def request_cancel(self) -> None:
        if not self._open["on"] or self.flow is None:
            return
        if self.flow.current_mode() == "busy":
            return
        self.flow.request_cancel()
        self._pump()

    def _close(self, *, saved: bool) -> None:
        on_saved = self._on_saved
        on_switch = self._on_switch
        flow = self.flow
        count = int(getattr(flow, "changes_count", 0) or 0) if flow is not None else 0
        switch_target = getattr(flow, "switch_workspace", None) if flow is not None else None
        self._open["on"] = False
        self.flow = None
        self._busy["running"] = False
        self._last_field["key"] = None
        try:
            self._input.text = ""
        except Exception:
            pass
        self._restore_focus()
        self._invalidate()
        # "Switch now" takes priority: hand the chosen folder to the host to relaunch
        # the chat there (no "saved N changes" note — we're leaving this session).
        if switch_target and on_switch is not None:
            try:
                on_switch(str(switch_target))
            except Exception:
                pass
            return
        if saved and on_saved is not None:
            try:
                on_saved(count)
            except Exception:
                pass

    # -------------------------------------------------------------- focus

    def _reconcile_focus(self) -> None:
        if not self._open["on"]:
            return
        mode = self._mode()
        target = self._input.window if mode == "input" else self._body_window
        try:
            app = get_app()
            if app.layout.current_window is not target:
                app.layout.focus(target)
        except Exception:
            pass

    def _restore_focus(self) -> None:
        if self._focus_chat is not None:
            try:
                self._focus_chat()
            except Exception:
                pass

    # --------------------------------------------------------------- pump

    def _pump(self) -> None:
        flow = self.flow
        if flow is None:
            return
        if flow.stage == "done":
            self._close(saved=bool(flow.saved))
            return
        mode = flow.current_mode()
        key = flow.field_key()
        if key != self._last_field["key"]:
            # Seed the input box on field change (UI thread only — the save worker
            # never lands on an input stage, so this is race-free).
            if mode == "input":
                scr = flow.screen()
                self._input.text = "" if scr.input_password else (scr.input_default or "")
                self._input.buffer.cursor_position = len(self._input.text)
            else:
                self._input.text = ""
            self._last_field["key"] = key
        self._reconcile_focus()
        if mode == "busy" and not self._busy["running"]:
            self._busy["running"] = True
            threading.Thread(target=self._busy_worker, daemon=True).start()
        self._invalidate()

    def _busy_worker(self) -> None:
        # Run ONLY the blocking save I/O here; it records its outcome on the flow but
        # writes no renderer-visible stage/status. The stage transition + close/reload
        # are then applied on the UI thread (``_finalize_save``), so the render
        # callbacks never read flow state written from this worker.
        flow = self.flow
        if flow is not None:
            try:
                perform_busy = getattr(flow, "perform_busy", None)
                if callable(perform_busy):
                    perform_busy()
                else:
                    flow.perform_save()
            except BaseException as exc:  # noqa: BLE001 - record, never hang the UI
                set_busy_failure = getattr(flow, "set_busy_failure", None)
                if callable(set_busy_failure):
                    set_busy_failure(f"{type(exc).__name__}: {exc}")
                else:
                    flow.set_save_failure(f"{type(exc).__name__}: {exc}")
        self._invalidate()
        try:
            loop = getattr(get_app(), "loop", None)
            if loop is not None:
                loop.call_soon_threadsafe(self._finalize_save)
            else:
                self._finalize_save()
        except Exception:
            self._finalize_save()

    def _finalize_save(self) -> None:
        flow = self.flow
        if flow is not None:
            apply_busy_outcome = getattr(flow, "apply_busy_outcome", None)
            if callable(apply_busy_outcome):
                apply_busy_outcome()
            else:
                flow.apply_save_outcome()
        self._busy["running"] = False
        self._pump()

    # ----------------------------------------------------------- key handling

    def _save_available(self) -> bool:
        # The same shortcut activates the visible save action in either flow.
        # Input fields, confirmations, and busy stages must keep their own keys.
        return bool(
            self._open["on"]
            and self.flow is not None
            and self._mode() == "list"
            and any(row.value == "__save__" for row in self.flow.screen().rows)
        )

    def _on_enter(self) -> None:
        flow = self.flow
        if flow is None:
            return
        mode = flow.current_mode()
        if mode == "input":
            flow.submit_input(self._input.text)
        elif mode == "list":
            flow.choose_current()
        elif mode == "confirm":
            flow.confirm(flow.screen().confirm_default)
        self._pump()

    def register(self, kb: KeyBindings) -> None:
        is_open = self.open_condition
        is_list = Condition(lambda: self._open["on"] and self._mode() == "list")
        is_confirm = Condition(lambda: self._open["on"] and self._mode() == "confirm")
        is_busy = Condition(lambda: self._open["on"] and self._mode() == "busy")
        can_save = Condition(self._save_available)

        @kb.add("enter", filter=is_open & ~is_busy, eager=True)
        def _enter(event: Any) -> None:
            self._on_enter()

        @kb.add("escape", filter=is_open & ~is_busy)
        def _escape(event: Any) -> None:
            if self.flow is not None:
                self.flow.back()
            self._pump()

        @kb.add("up", filter=is_list, eager=True)
        @kb.add("k", filter=is_list, eager=True)
        @kb.add("K", filter=is_list, eager=True)
        def _up(event: Any) -> None:
            if self.flow is not None:
                self.flow.move(-1)
            self._invalidate()

        @kb.add("down", filter=is_list, eager=True)
        @kb.add("j", filter=is_list, eager=True)
        @kb.add("J", filter=is_list, eager=True)
        def _down(event: Any) -> None:
            if self.flow is not None:
                self.flow.move(1)
            self._invalidate()

        for _digit in "123456789":

            @kb.add(_digit, filter=is_list, eager=True)
            def _pick(event: Any) -> None:
                if self.flow is None:
                    return
                try:
                    self.flow.choose_index(int(event.data) - 1)
                except (TypeError, ValueError):
                    return
                self._pump()

        @kb.add("q", filter=(is_list | is_confirm), eager=True)
        @kb.add("Q", filter=(is_list | is_confirm), eager=True)
        def _q(event: Any) -> None:
            if self.flow is not None:
                self.flow.back()
            self._pump()

        @kb.add("s", filter=can_save, eager=True)
        @kb.add("S", filter=can_save, eager=True)
        def _save(event: Any) -> None:
            if self.flow is not None:
                self.flow.choose("__save__")
            self._pump()

        @kb.add("y", filter=is_confirm, eager=True)
        @kb.add("Y", filter=is_confirm, eager=True)
        def _yes(event: Any) -> None:
            if self.flow is not None:
                self.flow.confirm(True)
            self._pump()

        @kb.add("n", filter=is_confirm, eager=True)
        @kb.add("N", filter=is_confirm, eager=True)
        def _no(event: Any) -> None:
            if self.flow is not None:
                self.flow.confirm(False)
            self._pump()


__all__ = ["ConfigOverlay"]
