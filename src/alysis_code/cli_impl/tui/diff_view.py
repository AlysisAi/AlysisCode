"""Read-only change summary and expandable, code-first diff overlay."""

from __future__ import annotations

import asyncio
import re
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.layout.screen import Char
from prompt_toolkit.lexers import Lexer
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import TextArea

from ...diff_view import ChangedFile, ChangeList, DiffReader, FilePatch


@dataclass(frozen=True)
class DiffLine:
    text: str
    number: int | None = None
    sign: str = " "


@dataclass(frozen=True)
class DisplayPatch:
    lines: tuple[DiffLine, ...] = ()
    hunks: tuple[int, ...] = ()
    added: int = 0
    removed: int = 0

    @property
    def gutter(self) -> int:
        return max(
            (len(str(line.number)) for line in self.lines if line.number is not None), default=1
        )

    @property
    def text(self) -> str:
        width = self.gutter
        return "\n".join(
            f"{line.number:>{width}} {line.sign}{line.text}"
            if line.number is not None
            else line.text
            for line in self.lines
        )


_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def display_patch(patch: FilePatch) -> DisplayPatch:
    """Hide transport headers, preserving code, file metadata and omissions.

    Parse hunk lengths before recognizing headers: code beginning with --- or
    +++ is still a deletion/addition, never a file header to discard.
    """
    rows: list[DiffLine] = []
    hunks: list[int] = []
    old = new = old_left = new_left = added = removed = 0
    for line in patch.text.split("\n"):
        match = _HUNK.match(line)
        if match:
            old, old_left, new, new_left = (
                int(match[1]),
                int(match[2] or 1),
                int(match[3]),
                int(match[4] or 1),
            )
            if hunks:
                rows.append(DiffLine("⋯"))
            hunks.append(len(rows))
        elif old_left and line.startswith("-"):
            rows.append(DiffLine(line[1:], old, "-"))
            old += 1
            old_left -= 1
            removed += 1
        elif new_left and line.startswith("+"):
            rows.append(DiffLine(line[1:], new, "+"))
            new += 1
            new_left -= 1
            added += 1
        elif old_left and new_left and line.startswith(" "):
            rows.append(DiffLine(line[1:], new))
            old += 1
            new += 1
            old_left -= 1
            new_left -= 1
        elif line.startswith(
            (
                "diff --git ",
                "index ",
                "--- ",
                "+++ ",
                "similarity index ",
                "rename from ",
                "rename to ",
            )
        ):
            continue
        elif line.startswith("\\ No newline at end of file"):
            rows.append(DiffLine("No newline at end of file"))
        elif line.startswith("Binary files "):
            # DiffReader supplies the concise binary-content explanation too.
            continue
        elif line:
            rows.append(DiffLine(line))
    if not rows:
        rows.append(DiffLine("No text changes."))
    if patch.truncated and not any("truncated" in row.text.lower() for row in rows):
        rows.append(DiffLine("Diff truncated; remaining content omitted."))
    return DisplayPatch(tuple(rows), tuple(hunks), added, removed)


class _DiffLexer(Lexer):
    def __init__(self, current: Callable[[], DisplayPatch]):
        self.current = current

    def lex_document(self, document):
        patch = self.current()
        width = patch.gutter

        def line_tokens(index):
            if index >= len(patch.lines):
                return [("", document.lines[index])]
            row = patch.lines[index]
            if row.number is None:
                return [("class:tui.diff.muted", row.text)]
            style = {"+": "class:tui.diff.add", "-": "class:tui.diff.remove"}.get(row.sign, "")
            return [
                ("class:tui.diff.muted", f"{row.number:>{width}} "),
                (style, row.sign + row.text),
            ]

        return line_tokens


def _file_state(entry: ChangedFile) -> str:
    status = {
        "M": "Modified",
        "A": "Added",
        "D": "Deleted",
        "R": "Renamed",
        "C": "Copied",
        "T": "Type changed",
        "U": "Unmerged",
        "?": "New file",
    }
    return status.get(entry.status[:1], entry.status)


@dataclass(frozen=True)
class FileSummary:
    added: int = 0
    removed: int = 0
    partial: bool = False
    note: str = ""

    @classmethod
    def from_patch(cls, patch: FilePatch, display: DisplayPatch) -> FileSummary:
        if not display.hunks:
            if any(
                line.startswith(
                    (
                        "Binary files ",
                        "GIT binary patch",
                        "Binary content is not displayed.",
                        "Content unavailable: binary file",
                    )
                )
                for line in patch.text.split("\n")
            ):
                return cls(note="binary")
            if patch.text.startswith(("Content unavailable:", "No textual diff is available")):
                return cls(note="unavailable")
        return cls(display.added, display.removed, patch.truncated)


def _short_path(path: str, width: int) -> str:
    """Keep counts visible even for long paths and wide Unicode filenames."""
    if get_cwidth(path) <= width:
        return path
    tail = []
    cells = 1
    for char in reversed(path):
        cells += get_cwidth(char)
        if cells > width:
            break
        tail.append(char)
    return "…" + "".join(reversed(tail))


class DiffViewer:
    def __init__(
        self,
        workspace: Path,
        *,
        on_close: Callable[[], None],
        invalidate: Callable[[], None],
        reader: DiffReader | None = None,
        collapsed: bool = True,
    ):
        self.reader = reader or DiffReader(workspace)
        self.workspace = Path(workspace)
        self.on_close = on_close
        self.invalidate = invalidate
        self.is_open = True
        self.changes = ChangeList()
        self.index = 0
        self.message = "Loading working-tree changes…"
        self.files_open = False
        self.collapsed = collapsed
        self.help_open = False
        self._loading = True
        self._patch = DisplayPatch()
        self._truncated = False
        self._generation = 0
        self._refresh_generation = 0
        self._discovering = False
        self._summaries: dict[ChangedFile, FileSummary] = {}
        self._summary_task: asyncio.Task | None = None
        self._loaded_entry: ChangedFile | None = None
        self._visual_key = None
        self._visual_rows: list[tuple[int, int]] = []
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self.diff = TextArea(
            text="",
            read_only=True,
            scrollbar=True,
            wrap_lines=True,
            lexer=_DiffLexer(lambda: self._patch),
        )
        # A wrapped code line continues beyond the number/sign gutter.
        self.diff.window.get_line_prefix = lambda line, wrap: (
            [("", " " * (self._patch.gutter + 2))] if wrap else []
        )
        self.list_control = FormattedTextControl(self._list_text, focusable=True)
        self.summary_control = FormattedTextControl(
            self._summary_text,
            focusable=True,
            get_cursor_position=lambda: Point(x=0, y=self.index - self._summary_range()[0]),
        )
        self.summary_window = Window(self.summary_control, wrap_lines=False)
        self.focus_target = self.summary_control if self.collapsed else self.diff
        bindings = KeyBindings()

        @bindings.add("escape", eager=True)
        @bindings.add("q")
        def close(event):
            self.close()

        @bindings.add("r")
        def refresh(event):
            self.start_refresh()

        @bindings.add("tab")
        @bindings.add("s-tab")
        @bindings.add("f")
        def files(event):
            if self.collapsed:
                return
            self.files_open = not self.files_open
            event.app.layout.focus(self.list_control if self.files_open else self.diff)
            self.invalidate()

        @bindings.add("c")
        def collapse(event):
            self.set_collapsed(not self.collapsed)
            event.app.layout.focus(self.summary_control if self.collapsed else self.diff)

        @bindings.add("?")
        def help_(event):
            self.help_open = not self.help_open
            self.invalidate()

        @bindings.add("up")
        @bindings.add("down")
        def move(event):
            amount = -1 if event.key_sequence[0].key == "up" else 1
            if event.app.layout.has_focus(self.diff):
                self.scroll_rows(amount)
            else:
                self.select(amount)

        @bindings.add("left")
        @bindings.add("right")
        def file_step(event):
            self.select(-1 if event.key_sequence[0].key == "left" else 1)

        @bindings.add("enter")
        def enter(event):
            self.set_collapsed(False)
            self.files_open = False
            event.app.layout.focus(self.diff)
            self.invalidate()

        @bindings.add("pagedown")
        @bindings.add("pageup")
        def page(event):
            if self.collapsed:
                self.select(
                    self._summary_height() * (1 if event.key_sequence[0].key == "pagedown" else -1)
                )
                return
            info = self.diff.window.render_info
            amount = max(1, info.window_height - 2) if info else 15
            if event.key_sequence[0].key == "pagedown":
                self.scroll_rows(amount)
            else:
                self.scroll_rows(-amount)

        @bindings.add("n")
        @bindings.add("p")
        def hunk(event):
            if not self.collapsed:
                self.scroll_hunk(-1 if event.key_sequence[0].key == "p" else 1)

        self.container = HSplit(
            [
                Window(FormattedTextControl(self._title_text), height=1, wrap_lines=False),
                Window(FormattedTextControl(self._subtitle_text), height=1, wrap_lines=False),
                ConditionalContainer(
                    Window(
                        self.list_control,
                        height=lambda: max(1, min(6, len(self.changes.files))),
                        wrap_lines=False,
                    ),
                    filter=Condition(lambda: self.files_open and not self.collapsed),
                ),
                ConditionalContainer(
                    Window(
                        FormattedTextControl(self._notice_text),
                        height=1,
                        wrap_lines=False,
                        style="class:tui.diff.muted",
                    ),
                    filter=Condition(lambda: bool(self._notice_text())),
                ),
                Window(height=1),
                ConditionalContainer(self.summary_window, filter=Condition(lambda: self.collapsed)),
                ConditionalContainer(self.diff, filter=Condition(lambda: not self.collapsed)),
                ConditionalContainer(
                    Window(
                        FormattedTextControl(self._help_text),
                        height=D(min=3),
                        wrap_lines=True,
                        style="class:tui.diff.muted",
                    ),
                    filter=Condition(lambda: self.help_open),
                ),
                Window(height=1),
                Window(
                    FormattedTextControl(self._footer_text),
                    height=1,
                    wrap_lines=False,
                    style="class:tui.diff.muted",
                ),
            ],
            key_bindings=bindings,
            modal=True,
        )

    def _title_text(self):
        if not self.changes.files:
            return [("bold", "Working-tree changes")]
        if self.collapsed:
            count = len(self.changes.files)
            # The same path can have separate staged and unstaged entries.
            return [("bold", f"{count} file change{'s' if count != 1 else ''}")]
        entry = self.changes.files[self.index]
        result = [("bold", " → ".join(entry.paths))]
        if not self._loading and self._patch.hunks:
            result += [
                ("", "  "),
                ("class:tui.diff.add", f"+{self._patch.added}"),
                ("", " "),
                ("class:tui.diff.remove", f"-{self._patch.removed}"),
            ]
            if self._truncated:
                result.append(("class:tui.diff.muted", " (partial)"))
        return result

    def _subtitle_text(self):
        if not self.changes.files:
            return ""
        if self.collapsed:
            return [
                (
                    "class:tui.diff.muted",
                    f"Collapsed view · {self.index + 1}/{len(self.changes.files)}",
                )
            ]
        entry = self.changes.files[self.index]
        return [
            (
                "class:tui.diff.muted",
                f"{entry.section.capitalize()} · {_file_state(entry)}"
                f" · {self.index + 1}/{len(self.changes.files)}",
            )
        ]

    def _notice_text(self) -> str:
        if self._loading:
            return "Loading…"
        if self.changes.notices:
            return "Some changes are unavailable or omitted. Press ? for details."
        return "" if self.changes.files else self.message

    def _help_text(self) -> str:
        root = getattr(self.reader, "workspace", self.workspace)
        return (
            "Enter expand · c collapse/expand · Tab / f file picker when expanded\n"
            "↑↓ select/scroll · ←/→ previous/next file\n"
            "PgUp/PgDn scroll · n/p next/previous change · r refresh · ? hide help\n"
            f"Repository: {root}\n"
            "Staged, unstaged and untracked changes across this repository."
            " Counts marked partial cover only the loaded preview."
            + ("\n" + "\n".join(self.changes.notices) if self.changes.notices else "")
        )

    def _footer_text(self) -> str:
        info = (self.summary_window if self.collapsed else self.diff.window).render_info
        if self.collapsed:
            return (
                "Enter expand · ? help · Esc close"
                if info and info.window_width < 60
                else "↑↓ select · Enter expand · ? help · Esc close"
            )
        if info and info.window_width < 43:
            return "c collapse · ? help · Esc close"
        if info and info.window_width < 60:
            return (
                "↑↓ select · Enter open · ? help · Esc close"
                if self.files_open
                else "c collapse · f files · ? help · Esc close"
            )
        if self.files_open:
            return "↑↓ select · Enter open · f hide files · ? help · Esc close"
        return "↑↓ scroll · ←→ file · c collapse · f files · ? help · Esc close"

    def _summary_height(self) -> int:
        info = self.summary_window.render_info
        return max(1, info.window_height if info else 12)

    def _summary_range(self) -> tuple[int, int]:
        height = self._summary_height()
        start = max(0, min(self.index - height // 2, len(self.changes.files) - height))
        return start, start + height

    def _summary_text(self):
        # Resizing can reveal uncached rows without a selection change. Schedule
        # their bounded reads; no Git or filesystem I/O runs in this callback.
        self._ensure_summaries()
        start, end = self._summary_range()
        info = self.summary_window.render_info
        width = info.window_width if info else 76
        result = []
        for idx, entry in enumerate(self.changes.files[start:end], start):
            summary = self._summaries.get(entry)
            if summary is None:
                stats = [("class:tui.diff.muted", "…")]
            elif summary.note:
                stats = [("class:tui.diff.muted", summary.note)]
            else:
                stats = [
                    ("class:tui.diff.add", f"+{summary.added}"),
                    ("", " "),
                    ("class:tui.diff.remove", f"-{summary.removed}"),
                ]
                if summary.partial:
                    stats.append(("class:tui.diff.muted", " partial"))
            suffix = f"  {entry.section}"
            stats_width = sum(get_cwidth(text) for _, text in stats)
            path_width = max(4, width - stats_width - get_cwidth(suffix) - 6)
            path = _short_path(" → ".join(entry.paths), path_width)
            result += [
                (
                    "bold" if idx == self.index else "",
                    ("› ▸ " if idx == self.index else "  ▸ ") + path,
                ),
                ("", " " * (path_width - get_cwidth(path) + 2)),
                *stats,
                ("class:tui.diff.muted", suffix + "\n"),
            ]
        return result

    def _ensure_summaries(self) -> None:
        if not self.is_open or not self.collapsed or self._discovering:
            return
        start, end = self._summary_range()
        if self._summary_task or not any(
            entry not in self._summaries for entry in self.changes.files[start:end]
        ):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        task = self._spawn(self._load_summaries(self._refresh_generation))
        self._summary_task = task

        def done(completed):
            if self._summary_task is completed:
                self._summary_task = None
                self._ensure_summaries()

        task.add_done_callback(done)

    async def _load_summaries(self, refresh_generation: int) -> None:
        while self.is_open and self.collapsed and refresh_generation == self._refresh_generation:
            start, end = self._summary_range()
            entry = next(
                (entry for entry in self.changes.files[start:end] if entry not in self._summaries),
                None,
            )
            if entry is None or self._discovering:
                return
            try:
                async with self._lock:
                    if refresh_generation != self._refresh_generation or not self.is_open:
                        return
                    patch = await asyncio.to_thread(self.reader.read_patch, entry)
                summary = FileSummary.from_patch(patch, display_patch(patch))
            except Exception:
                summary = FileSummary(note="unavailable")
            if not self.is_open or refresh_generation != self._refresh_generation:
                return
            self._summaries[entry] = summary
            self.invalidate()

    def set_collapsed(self, collapsed: bool) -> None:
        self.collapsed = collapsed
        self.files_open = False
        if collapsed:
            self._generation += 1
            self._loading = self._discovering
            self._ensure_summaries()
        elif self.changes.files and self._loaded_entry != self.changes.files[self.index]:
            self.select(0)
        self.invalidate()

    def _list_text(self):
        if not self.changes.files:
            return [("class:tui.diff.muted", "No changed files.")]
        start = max(0, min(self.index - 2, len(self.changes.files) - 6))
        result = []
        for idx, item in enumerate(self.changes.files[start : start + 6], start):
            selected = idx == self.index
            result += [
                (
                    "reverse" if selected else "",
                    ("› " if selected else "  ") + " → ".join(item.paths),
                ),
                ("reverse" if selected else "class:tui.diff.muted", f"  {item.section}\n"),
            ]
        return result

    def _spawn(self, awaitable) -> asyncio.Task:
        task = asyncio.create_task(awaitable)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def start_refresh(self) -> asyncio.Task:
        return self._spawn(self.refresh())

    def _clear_patch(self) -> None:
        self._patch = DisplayPatch()
        self._truncated = False
        self._loaded_entry = None
        self.diff.text = ""

    async def refresh(self) -> None:
        self._generation += 1
        self._refresh_generation += 1
        self._discovering = True
        refresh_generation = self._refresh_generation
        self.message = "Loading working-tree changes…"
        self._loading = True
        self.invalidate()
        try:
            async with self._lock:
                changes = await asyncio.to_thread(self.reader.list_changes)
            if not self.is_open or refresh_generation != self._refresh_generation:
                return
            # Navigation during discovery must not discard the new file list.
            old = self.changes.files[self.index] if self.changes.files else None
            self.changes = changes
            self._summaries.clear()
            self._discovering = False
            self.index = changes.files.index(old) if old in changes.files else 0
            self.message = " · ".join(changes.notices) or (
                "No working-tree changes." if not changes.files else ""
            )
            self._clear_patch()
            self._generation += 1
            if self.collapsed:
                self._loading = False
                self._ensure_summaries()
            else:
                await self._load(self._generation)
        except Exception as exc:
            if self.is_open and refresh_generation == self._refresh_generation:
                self._generation += 1
                self.changes = ChangeList()
                self._summaries.clear()
                self._discovering = False
                self._clear_patch()
                self._loading = False
                self.message = str(exc)
        self.invalidate()

    def select(self, offset: int) -> asyncio.Task | None:
        if not self.is_open or not self.changes.files:
            return None
        self.index = max(0, min(len(self.changes.files) - 1, self.index + offset))
        self._generation += 1
        self._loading = True
        self._clear_patch()
        self.invalidate()
        if self.collapsed:
            self._loading = self._discovering
            self._ensure_summaries()
            return None
        return self._spawn(self._load(self._generation))

    async def _load(self, generation: int) -> None:
        if not self.changes.files:
            self._clear_patch()
            self._loading = False
            return
        entry = self.changes.files[self.index]
        try:
            async with self._lock:
                if not self.is_open or generation != self._generation:
                    return
                patch = await asyncio.to_thread(self.reader.read_patch, entry)
        except Exception as exc:
            patch = FilePatch(f"Content unavailable: {exc}")
        if self.is_open and generation == self._generation:
            self._patch = display_patch(patch)
            self._summaries[entry] = FileSummary.from_patch(patch, self._patch)
            self._loaded_entry = entry
            self._truncated = patch.truncated
            self.diff.text = self._patch.text
            self._loading = False
            self.invalidate()

    def scroll_rows(self, amount: int) -> None:
        """Move through visual rows, including a single line taller than the pane."""
        info = self.diff.window.render_info
        if not info:
            return
        document = self.diff.buffer.document
        width = max(1, info.window_width)
        key = (document.text, width, self._patch.gutter)
        if key != self._visual_key:
            self._visual_key = key
            self._visual_rows = []
            continuation = min(width - 1, self._patch.gutter + 2)
            for row, line in enumerate(document.lines):
                self._visual_rows.append((row, 0))
                cells = 0
                # BufferControl appends a space so an end-of-line cursor fits.
                for col, char in enumerate(line + " "):
                    size = get_cwidth(Char.display_mappings.get(char, char))
                    if cells + size > width:
                        self._visual_rows.append((row, col))
                        cells = continuation
                    cells += size
        current = (
            bisect_right(
                self._visual_rows, (document.cursor_position_row, document.cursor_position_col)
            )
            - 1
        )
        target = max(0, min(len(self._visual_rows) - 1, current + amount))
        row, col = self._visual_rows[target]
        # Keep the complete target row in view. At a wrap boundary PTK measures
        # only the text before the cursor, which can leave that row off-screen.
        following = self._visual_rows[target + 1] if target + 1 < len(self._visual_rows) else None
        col = following[1] - 1 if following and following[0] == row else len(document.lines[row])
        self.diff.buffer.cursor_position = document.translate_row_col_to_index(row, col)
        self.invalidate()

    def scroll_hunk(self, direction: int) -> None:
        current = self.diff.buffer.document.cursor_position_row
        candidates = [row for row in self._patch.hunks if (row - current) * direction > 0]
        if candidates:
            row = candidates[0] if direction > 0 else candidates[-1]
            self.diff.buffer.cursor_position = self.diff.buffer.document.translate_row_col_to_index(
                row, 0
            )
        self.invalidate()

    def close(self) -> None:
        if not self.is_open:
            return
        self.is_open = False
        self._generation += 1
        self._refresh_generation += 1
        for task in tuple(self._tasks):
            task.cancel()
        self.on_close()
        self.invalidate()
