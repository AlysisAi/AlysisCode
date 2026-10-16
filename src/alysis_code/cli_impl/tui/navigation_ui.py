"""Local navigation dialogs. File searches run in one bounded work queue."""

from __future__ import annotations

import asyncio
from concurrent.futures import Future

from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.selection import SelectionState
from prompt_toolkit.widgets import TextArea

from .panel_text import PanelText
from .transcript_navigation import TranscriptNavigator


class SessionSearchModal:
    def __init__(self, provider, *, executor, on_select, on_close, invalidate, eligible):
        self.provider, self.executor = provider, executor
        self.on_select, self.on_close = on_select, on_close
        self.invalidate, self.eligible = invalidate, eligible
        self.is_open = True
        self.rows = ()
        self.index = 0
        self.status = "Searching…"
        self.renaming = False
        self.details_open = False
        self.details = PanelText()
        self._generation = 0
        self._task = None
        self.query = TextArea(height=1, multiline=False, prompt="Search: ")
        self.focus_target = self.query
        self.query.buffer.on_text_changed += lambda _: self.search()
        bindings = KeyBindings()
        choosing = Condition(lambda: not self.details_open)

        @bindings.add("escape", eager=True)
        def cancel(event):
            self.close()

        @bindings.add("up", filter=choosing)
        @bindings.add("down", filter=choosing)
        def move(event):
            self.index = min(
                max(0, self.index + (-1 if event.key_sequence[0].key == "up" else 1)),
                max(0, len(self.rows) - 1),
            )
            self.invalidate()

        @bindings.add("f2", filter=choosing)
        def rename(event):
            if self.rows and not self.renaming:
                self.renaming = True
                self.rename_id = self.rows[self.index].session_id
                self.query.text = self.rows[self.index].title
                self.query.buffer.cursor_position = len(self.query.text)
                self.status = "Rename selected conversation; Enter saves, Escape closes."

        @bindings.add("enter", eager=True, filter=choosing)
        def accept(event):
            if self._task is None or self._task.done():
                self._task = asyncio.create_task(self._accept())

        @bindings.add("f1")
        def details(event):
            self.details_open = not self.details_open
            row = self.rows[self.index] if self.rows else None
            self.details.text = (
                f"{row.title}\n\n{row.snippet}\n\nConversation: {row.session_id}\n\n" if row else ""
            ) + "Up/Down chooses a conversation. Enter resumes it. F2 renames it. Type to search."
            event.app.layout.focus(self.details if self.details_open else self.query)

        self.list_window = Window(
            FormattedTextControl(self._rows, get_cursor_position=lambda: Point(x=0, y=self.index)),
            height=D(min=2, preferred=10),
        )
        self.container = HSplit(
            [
                ConditionalContainer(
                    HSplit(
                        [
                            self.query,
                            Window(
                                FormattedTextControl(lambda: self.status),
                                height=D(min=1, max=3),
                                wrap_lines=True,
                            ),
                            self.list_window,
                            Window(
                                FormattedTextControl(
                                    lambda: self.rows[self.index].snippet if self.rows else ""
                                ),
                                height=D(min=1, max=3),
                                wrap_lines=True,
                            ),
                        ]
                    ),
                    filter=choosing,
                ),
                ConditionalContainer(self.details, filter=Condition(lambda: self.details_open)),
                Window(
                    FormattedTextControl(
                        lambda: (
                            "↑↓ scroll · F1 back · Esc close"
                            if self.details_open
                            else "Enter resume · F2 rename · F1 info · Esc close"
                        )
                    ),
                    height=1,
                ),
            ],
            key_bindings=bindings,
            modal=True,
        )

    def _rows(self):
        result = []
        for i, row in enumerate(self.rows):
            result.append(
                (
                    "reverse" if i == self.index else "",
                    f"{'>' if i == self.index else ' '} {row.title}\n",
                )
            )
        return result

    def search(self):
        if not self.is_open or self.renaming:
            return
        self._generation += 1
        # Debounce in one coroutine. Do not enqueue a future for each keystroke;
        # an in-flight search finishes before it picks up the newest query.
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._search())

    async def _search(self):
        while self.is_open:
            generation, query = self._generation, self.query.text
            await asyncio.sleep(0.15)
            if generation != self._generation:
                continue
            self.status = "Searching…"
            self.invalidate()
            try:
                result = await asyncio.get_running_loop().run_in_executor(
                    self.executor, self.provider.search, query
                )
            except Exception:
                if self.is_open and generation == self._generation:
                    self.rows = ()
                    self.status = "Session search unavailable. Check session storage and retry."
                break
            if not self.is_open:
                return
            if generation != self._generation:
                continue
            self.rows, self.index = result.rows, 0
            self.status = (
                f"{len(self.rows)} conversations" if self.rows else "No matching conversations"
            ) + (
                " · Search limited to recent sessions; refine your query."
                if result.truncated
                else ""
            )
            break
        self.invalidate()

    async def _accept(self):
        if not self.eligible():
            self.status = "Wait for active and queued work to finish before resuming."
            self.invalidate()
            return
        loop = asyncio.get_running_loop()
        if self.renaming:
            try:
                await loop.run_in_executor(
                    self.executor, self.provider.rename, self.rename_id, self.query.text
                )
            except Exception:
                self.status = "Rename failed. The conversation is unchanged."
                self.invalidate()
                return
            self.renaming = False
            self.query.text = ""
            self._generation += 1
            await self._search()
            return
        if not self.rows:
            return
        selected = self.rows[self.index].session_id
        self.status = "Checking conversation…"
        self.invalidate()
        try:
            await loop.run_in_executor(self.executor, self.provider.resolve, selected)
            if not self.is_open:
                return
            if not self.eligible():
                raise RuntimeError("Busy")
            success = self.on_select(selected)
            if isinstance(success, Future):
                success = await asyncio.wrap_future(success)
            if success:
                self.close()
            else:
                self.status = "Resume failed. Your current conversation and draft are retained."
        except Exception:
            self.status = "Conversation unavailable or resume failed. Current draft retained."
        self.invalidate()

    def close(self):
        if self.is_open:
            self.is_open = False
            self._generation += 1
            if self._task is not None:
                self._task.cancel()
            self.on_close()


class TranscriptSearchModal:
    def __init__(self, entries, *, on_close, invalidate, query=""):
        self.entries = entries
        self.on_close, self.invalidate = on_close, invalidate
        self.is_open = True
        self.navigator = TranscriptNavigator()
        self.query = TextArea(text=query, height=2, multiline=True, prompt="Find: ")
        self.details = PanelText()
        self.status = "Type text to find in the transcript. Ctrl+J adds a newline."
        self.focus_target = self.query
        self.query.buffer.on_text_changed += lambda _: self.find()
        bindings = KeyBindings()

        @bindings.add("escape", eager=True)
        def close(event):
            self.close()

        @bindings.add("enter", eager=True)
        @bindings.add("f3")
        def next_match(event):
            self.navigator.next()
            self.show()

        @bindings.add("c-j")
        def newline(event):
            self.query.buffer.insert_text("\n")

        @bindings.add("c-p")
        def previous_match(event):
            self.navigator.previous()
            self.show()

        @bindings.add("tab")
        def focus(event):
            event.app.layout.focus(
                self.query if event.app.layout.has_focus(self.details) else self.details
            )

        self.container = HSplit(
            [
                self.query,
                Window(FormattedTextControl(lambda: self.status), height=2),
                self.details,
                Window(
                    FormattedTextControl("Enter next · Ctrl+P back · Tab text · Esc close"),
                    height=1,
                ),
            ],
            key_bindings=bindings,
            modal=True,
        )
        self.find()

    def find(self):
        self.navigator.find(self.entries, self.query.text)
        self.show()

    def show(self):
        match = self.navigator.current
        if match is None:
            self.status = (
                "No matches" if self.query.text else "Type text to find. Ctrl+J adds a newline."
            )
            self.details.text = ""
        else:
            role, text = self.entries[match.entry_index]
            self.status = f"Match {self.navigator.index + 1}/{len(self.navigator.matches)} · {role}"
            self.details.buffer.set_document(
                Document(
                    text,
                    cursor_position=match.end,
                    selection=SelectionState(original_cursor_position=match.start),
                ),
                bypass_readonly=True,
            )
            self.details.buffer.selection_state = SelectionState(
                original_cursor_position=match.start
            )
        self.invalidate()

    def close(self):
        if self.is_open:
            self.is_open = False
            self.on_close()
