"""Native report overlay using the same controls and theme as configuration."""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Any

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import FormattedText
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.dimension import Dimension

from .config_overlay import ConfigOverlay
from .report_flow import ReportFlow


class ReportOverlay(ConfigOverlay):
    def __init__(self, *, on_closed: Callable[[ReportFlow], None], **kwargs: Any) -> None:
        super().__init__(input_multiline=True, label="report", **kwargs)
        self._on_closed = on_closed
        self._worker: threading.Thread | None = None
        self._preview_scroll = 0
        self._body_window.allow_scroll_beyond_bottom = Condition(lambda: False)
        self._status_window.wrap_lines = Condition(lambda: True)
        self._status_window.height = Dimension(min=1, max=3)

    def _header_fragments(self) -> FormattedText:
        return FormattedText(
            [("class:setup.header", "◇ alysis"), ("class:setup.headerdim", "  ·  report")]
        )

    def _footer_fragments(self) -> FormattedText:
        mode = self._mode()
        stage = self.flow.stage if self.flow else ""
        if mode == "input":
            text = "Enter review · Alt+Enter newline · Esc back"
        elif mode == "busy":
            text = (
                "Esc / Ctrl+C cancel export"
                if self.flow and self.flow._job in {"save", "export_draft"}
                else "Finishing report action…"
            )
        elif stage == "confirm_send":
            text = (
                "Enter send · ↑↓ options · Esc back"
                if self.flow.index == 0
                else "Enter select · ↑↓ options · Esc back"
            )
        elif stage in {"diagnostics", "feedback_details"}:
            text = "Enter back · Esc back · PgUp/PgDn scroll"
        elif stage == "draft":
            text = "Enter open · Esc back · Ctrl+Y copy URL · PgUp/PgDn scroll"
        elif stage == "complete":
            text = "↑↓ options · Enter select · Esc return to chat"
        else:
            text = "↑↓ move · Enter select · Esc back · Ctrl+C close"
        # Keep a usable legend at narrow widths instead of overflowing the row.
        if self._width() < 55:
            text = {
                "input": "Enter review · Esc back",
                "confirm": "Enter back · Esc back · PgDn"
                if stage in {"diagnostics", "feedback_details"}
                else "Enter open · Esc back · ^Y copy",
                "busy": "Esc cancel"
                if self.flow and self.flow._job in {"save", "export_draft"}
                else "Working…",
            }.get(mode, "↑↓ move · Enter select · Esc back")
            if stage == "confirm_send":
                text = (
                    "Enter send · ↑↓ options · Esc back"
                    if self.flow.index == 0
                    else "Enter select · ↑↓ options · Esc back"
                )
            elif stage == "complete":
                text = "↑↓ options · Enter select · Esc close"
        return FormattedText([("class:setup.footer.dim", text)])

    def _body_fragments(self) -> FormattedText:
        fragments = super()._body_fragments()
        if self._mode() == "confirm":
            count = sum(text.count("\n") for _style, text in fragments)
            self._preview_scroll = min(self._preview_scroll, max(0, count - 1))
            self._body_cursor["row"] = self._preview_scroll
        return fragments

    def request_cancel(self) -> None:
        if self.flow is not None:
            self.flow.request_cancel()
            self._pump()

    def _close(self, *, saved: bool) -> None:
        flow = self.flow
        super()._close(saved=False)
        if flow is not None:
            self._on_closed(flow)

    def _pump(self) -> None:
        flow = self.flow
        if flow is None:
            return
        if flow.stage == "done":
            self._close(saved=flow.saved)
            return
        mode, key = flow.current_mode(), flow.field_key()
        if key != self._last_field["key"]:
            self._input.text = flow.screen().input_default if mode == "input" else ""
            self._input.buffer.cursor_position = len(self._input.text)
            self._last_field["key"] = key
            self._preview_scroll = 0
            self._body_window.reset()
            self._status_window.reset()
        self._reconcile_focus()
        if mode == "busy" and not self._busy["running"]:
            self._busy["running"] = True
            app = get_app()

            def run() -> None:
                try:
                    flow.perform_busy()
                except BaseException as exc:
                    flow.set_busy_failure(f"{type(exc).__name__}: {exc}")
                # Capture the real application on the UI thread; get_app() on
                # a new thread can otherwise return a DummyApplication.
                loop = app.loop
                if loop is not None and not loop.is_closed():
                    loop.call_soon_threadsafe(finish)

            def finish() -> None:
                if self.flow is flow:
                    flow.apply_busy_outcome()
                    self._busy["running"] = False
                    self._pump()

            self._worker = threading.Thread(target=run, name="alysis-report-export", daemon=True)
            self._worker.start()
        self._invalidate()

    def shutdown(self) -> None:
        """Release the session only after its export has finished or cancelled."""
        if self.flow is not None:
            self.flow.request_cancel()
        if self._worker is not None and self._worker.is_alive():
            self._worker.join()

    def register(self, kb: KeyBindings) -> None:
        super().register(kb)
        is_input = Condition(lambda: self.is_open() and self._mode() == "input")
        is_preview = Condition(lambda: self.is_open() and self._mode() == "confirm")
        is_busy = Condition(lambda: self.is_open() and self._mode() == "busy")

        @kb.add("escape", "enter", filter=is_input, eager=True)
        @kb.add("c-j", filter=is_input, eager=True)
        def newline(event: Any) -> None:
            self._input.buffer.insert_text("\n")

        @kb.add("escape", filter=is_busy, eager=True)
        def cancel(event: Any) -> None:
            self.request_cancel()

        @kb.add("c-y", filter=is_preview, eager=True)
        def copy_url(event: Any) -> None:
            self.flow.choose("copy_url")
            self._pump()

        @kb.add("pagedown", filter=is_preview, eager=True)
        @kb.add("down", filter=is_preview, eager=True)
        def scroll_down(event: Any) -> None:
            self._preview_scroll += 1 if event.data == "\x1b[B" else 8
            self._invalidate()

        @kb.add("pageup", filter=is_preview, eager=True)
        @kb.add("up", filter=is_preview, eager=True)
        def scroll_up(event: Any) -> None:
            self._preview_scroll = max(
                0, self._preview_scroll - (1 if event.data == "\x1b[A" else 8)
            )
            self._invalidate()
