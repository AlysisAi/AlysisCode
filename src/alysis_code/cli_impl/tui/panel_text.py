"""Read-only panel text with keyboard scrolling through wrapped visual rows."""

from __future__ import annotations

from bisect import bisect_right

from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea


class PanelText(TextArea):
    """A text pane whose arrow/page keys also reach long wrapped paragraphs.

    Tab/Enter/Escape and operation shortcuts belong to the owning modal. This
    control only handles scrolling while it has focus; it never changes text.
    """

    def __init__(self, text: str = "", **kwargs):
        super().__init__(text=text, read_only=True, scrollbar=True, wrap_lines=True, **kwargs)
        self._visual_key = None
        self._visual_rows: list[tuple[int, int]] = []
        bindings = KeyBindings()

        @bindings.add("up")
        @bindings.add("down")
        def line(event):
            self.scroll_rows(-1 if event.key_sequence[0].key == "up" else 1)

        @bindings.add("pageup")
        @bindings.add("pagedown")
        def page(event):
            info = self.window.render_info
            count = max(1, info.window_height - 2) if info else 10
            self.scroll_rows(-count if event.key_sequence[0].key == "pageup" else count)

        @bindings.add("home")
        def first(event):
            self.buffer.selection_state = None
            self.buffer.cursor_position = 0

        @bindings.add("end")
        def last(event):
            self.buffer.selection_state = None
            self.buffer.cursor_position = len(self.text)

        self.control.key_bindings = bindings

    def scroll_rows(self, amount: int) -> None:
        info = self.window.render_info
        if not info:
            return
        document = self.buffer.document
        width = max(1, info.window_width)
        key = (document.text, width)
        if key != self._visual_key:
            self._visual_key = key
            self._visual_rows = []
            for row, line in enumerate(document.lines):
                self._visual_rows.append((row, 0))
                cells = 0
                for column, char in enumerate(line + " "):
                    size = get_cwidth(Char.display_mappings.get(char, char))
                    if cells + size > width:
                        self._visual_rows.append((row, column))
                        cells = 0
                    cells += size
        current = (
            bisect_right(
                self._visual_rows, (document.cursor_position_row, document.cursor_position_col)
            )
            - 1
        )
        target = max(0, min(len(self._visual_rows) - 1, current + amount))
        row, _column = self._visual_rows[target]
        following = self._visual_rows[target + 1] if target + 1 < len(self._visual_rows) else None
        column = following[1] - 1 if following and following[0] == row else len(document.lines[row])
        # A match may be highlighted with a selection (for example in /find).
        # Manual reading must not turn that match into an expanding selection.
        self.buffer.selection_state = None
        self.buffer.cursor_position = document.translate_row_col_to_index(row, column)
