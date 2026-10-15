"""A compact question card with contextual controls and an explicit text editor."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Float, HSplit, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import Box, Frame, TextArea

from ...user_questions import MAX_ANSWER_CHARS, QuestionRequest, UserQuestionBroker
from .composer import wrap_cells


class QuestionPanel:
    def __init__(
        self,
        *,
        broker: UserQuestionBroker,
        restore_focus: Callable[[], None],
        blocked: Callable[[], bool],
        on_answer: Callable[[str], None],
    ) -> None:
        self.broker = broker
        self.restore_focus = restore_focus
        self.blocked = blocked
        self.on_answer = on_answer
        self.request: QuestionRequest | None = None
        self.visible = False
        self.index = self.cursor = self.question_scroll = 0
        self.answers: list[dict[str, Any]] = []
        self.error = ""
        self.editing = False
        self.help_visible = False
        self.open_condition = Condition(lambda: self.visible and not self.blocked())
        self.input = TextArea(
            multiline=True,
            height=Dimension(min=1, preferred=3, max=4),
            wrap_lines=True,
            prompt="› ",
            style="class:tui.question.input",
        )
        self.input.buffer.on_text_changed += self._on_editor_activity
        self.input.buffer.on_cursor_position_changed += self._on_editor_activity
        self.heading = FormattedTextControl(
            self._heading, get_cursor_position=self._heading_cursor, show_cursor=False
        )
        self.choices = FormattedTextControl(
            self._choices,
            focusable=True,
            show_cursor=False,
            get_cursor_position=self._choice_cursor,
        )
        self.frame = Frame(
            Box(
                HSplit(
                    [
                        Window(FormattedTextControl(self._progress), height=1),
                        self._gap(),
                        Window(
                            self.heading,
                            wrap_lines=True,
                            height=Dimension(min=1, max=4),
                            dont_extend_height=True,
                            style="class:tui.question.heading",
                        ),
                        self._gap(),
                        ConditionalContainer(
                            Window(
                                self.choices,
                                wrap_lines=True,
                                height=Dimension(min=2, max=12),
                                dont_extend_height=True,
                            ),
                            filter=Condition(lambda: not self.editing),
                        ),
                        ConditionalContainer(
                            Frame(self.input, style="class:tui.question.field"),
                            filter=Condition(lambda: self.editing),
                        ),
                        ConditionalContainer(
                            Window(
                                FormattedTextControl(lambda: self.error),
                                wrap_lines=True,
                                height=Dimension(min=1, max=2),
                                style="class:tui.question.error",
                            ),
                            filter=Condition(lambda: bool(self.error)),
                        ),
                        self._gap(),
                        ConditionalContainer(
                            Window(
                                FormattedTextControl(self._help),
                                wrap_lines=True,
                                height=Dimension(min=1, max=5),
                                dont_extend_height=True,
                                style="class:tui.question.muted",
                            ),
                            filter=Condition(lambda: self.help_visible),
                        ),
                        Window(
                            FormattedTextControl(self._footer),
                            wrap_lines=True,
                            height=Dimension(min=1, max=3),
                            dont_extend_height=True,
                            style="class:tui.question.muted",
                        ),
                    ]
                ),
                padding=0,
                padding_left=2,
                padding_right=2,
                padding_top=lambda: 0 if self._compact() else 1,
                padding_bottom=lambda: 0 if self._compact() else 1,
            ),
            style="class:tui.question",
        )
        self.float = Float(
            content=ConditionalContainer(self.frame, filter=self.open_condition),
            width=self._width,
        )

    @staticmethod
    def _compact() -> bool:
        return get_app().output.get_size().rows < 20

    @staticmethod
    def _width() -> int:
        return max(16, min(76, get_app().output.get_size().columns - 4))

    def _gap(self):
        return ConditionalContainer(Window(height=1), filter=Condition(lambda: not self._compact()))

    def _question(self):
        return self.request.questions[self.index] if self.request else None

    def _progress(self):
        question = self._question()
        if question is None:
            return []
        kind = {"single": "Choose one", "multiple": "Choose any", "text": "Your answer"}[
            question.kind
        ]
        width = self._width() - 6
        remaining = self.broker.remaining_seconds
        if width >= 60:
            progress = f"QUESTION {self.index + 1} OF {len(self.answers)}"
            detail = f"  ·  {kind}"
            timer = f"Continues in {remaining}s"
        else:
            progress = f"{self.index + 1}/{len(self.answers)}"
            detail = f" · {kind}"
            timer = f"{remaining}s left"
        spacing = " " * max(1, width - get_cwidth(progress + detail + timer))
        return [
            ("class:tui.question.meta", progress),
            ("class:tui.question.muted", detail + spacing + timer),
        ]

    def _heading_cursor(self):
        self.question_scroll = min(
            self.question_scroll, max(0, len(self._heading().splitlines()) - 1)
        )
        return Point(0, self.question_scroll)

    def _heading(self):
        question = self._question()
        if question is None:
            return ""
        width = max(8, self._width() - 6)
        lines = []
        for paragraph in question.question.splitlines():
            if not paragraph:
                lines.append("")
                continue
            rows = wrap_cells(
                [get_cwidth(char) for char in paragraph],
                [char.isspace() for char in paragraph],
                [False] * len(paragraph),
                first_cap=width,
                rest_cap=width,
            )
            lines.extend(paragraph[row.start : row.end].rstrip() for row in rows)
        return "\n".join(lines)

    def _choice_cursor(self):
        question = self._question()
        return Point(0, self.cursor + int(bool(question and self.cursor == len(question.options))))

    def _choices(self):
        question = self._question()
        if question is None:
            return []
        selected = self.answers[self.index]["selected"]
        rows = []
        labels = [*question.options, "Write your own answer…"]
        for index, label in enumerate(labels):
            if index == len(question.options):
                rows.append(("", "\n"))
            current = index == self.cursor
            style = "class:tui.question.active" if current else "class:tui.question.option"
            marker = "›" if current else " "
            if question.kind == "multiple" and index < len(question.options):
                number = "[x]" if label in selected else "[ ]"
            else:
                number = f"{index + 1}."
            text = f"{marker} {number} {' '.join(label.split())}"
            text += " " * max(0, self._width() - 6 - get_cwidth(text))
            rows.append((style, text))
            if index < len(labels) - 1:
                rows.append(("", "\n"))
        return rows

    def _footer(self):
        question = self._question()
        if question is None:
            return ""
        final = self.index == len(self.answers) - 1
        if self.editing:
            action = "Submit" if final else "Next"
            keys = [f"Enter {action}", "Esc Back" if question.options else "Esc Later", "F1 Help"]
        elif self.cursor == len(question.options):
            keys = ["↑↓ Move", "Enter Write", "Esc Later", "F1 Help"]
        elif question.kind == "multiple":
            keys = [
                "Space Toggle",
                f"Enter {'Submit' if final else 'Next'}",
                "Esc Later",
                "F1 Help",
            ]
        else:
            keys = ["↑↓ Move", f"Enter {'Submit' if final else 'Choose'}", "Esc Later", "F1 Help"]
        if self.index:
            keys.insert(-1, "← Back" if not self.editing else "Ctrl+B Back")
        width = self._width() - 6
        lines, line = [], ""
        for key in keys:
            candidate = f"{line}   {key}" if line else key
            if get_cwidth(candidate) > width and line:
                lines.append(line)
                line = key
            else:
                line = candidate
        lines.append(line)
        if len(self._heading().splitlines()) > 4:
            lines.append("PgUp/PgDn scroll question")
        return "\n".join(lines)

    def _help(self):
        return (
            "Tab  Switch to text/choices\nCtrl+B  Previous question · Ctrl+J  New line\n"
            "Ctrl+X  Dismiss questions · Ctrl+G  Reopen later\n"
            f"After {self.broker.timeout_seconds:g}s idle, unanswered questions are skipped."
        )

    def _activity(self) -> bool:
        if self.request is not None and self.broker.touch(self.request.id):
            return True
        self.sync()
        return False

    def _on_editor_activity(self, buffer) -> None:
        if self.visible and self.editing and get_app().layout.has_focus(self.input):
            self._activity()

    def sync(self, *, reopen: bool = False) -> None:
        active = self.broker.active
        if active is None:
            if self.visible:
                self.hide()
            self.request = None
            return
        if self.blocked():
            return
        if self.request is None or self.request.id != active.id:
            self.request = active
            self.index = 0
            self.answers = [{"id": q.id, "selected": [], "text": ""} for q in active.questions]
            self._load_question()
            self.help_visible = False
            reopen = True
        if reopen:
            self.visible = True
            self._focus()
        get_app().invalidate()

    def _load_question(self) -> None:
        question = self._question()
        answer = self.answers[self.index]
        self.question_scroll = self.cursor = 0
        self.error = ""
        self.input.text = answer["text"]
        self.editing = question.kind == "text" or bool(answer["text"] and not answer["selected"])
        if answer["selected"]:
            self.cursor = question.options.index(answer["selected"][0])

    def _focus(self) -> None:
        get_app().layout.focus(self.input if self.editing else self.choices)

    def _edit(self) -> None:
        self.editing = True
        self.error = ""
        self._focus()

    def _show_choices(self) -> None:
        self.answers[self.index]["text"] = self.input.text
        self.editing = False
        self.error = ""
        self._focus()

    def hide(self) -> None:
        self.visible = False
        if get_app().layout.has_focus(self.input) or get_app().layout.has_focus(self.choices):
            self.restore_focus()
        get_app().invalidate()

    def toggle(self) -> None:
        question = self._question()
        if question is None:
            return
        if self.cursor == len(question.options):
            self._edit()
            return
        if question.kind != "multiple":
            return
        selected = self.answers[self.index]["selected"]
        label = question.options[self.cursor]
        if label in selected:
            selected.remove(label)
        else:
            selected.append(label)
        self.error = ""

    def advance(self) -> None:
        if self.request is None:
            return
        question = self._question()
        answer = self.answers[self.index]
        if not self.editing and self.cursor == len(question.options):
            self._edit()
            return
        if self.editing:
            if not self.input.text.strip():
                self.error = "Write an answer to continue."
                return
            if len(self.input.text) > MAX_ANSWER_CHARS:
                self.error = f"Keep the answer within {MAX_ANSWER_CHARS} characters."
                return
            answer["text"] = self.input.text
            if question.kind == "single":
                answer["selected"] = []
        elif question.kind == "single":
            # Enter is the explicit choice; merely highlighting never submits.
            answer["selected"] = [question.options[self.cursor]]
            answer["text"] = ""
        elif not answer["selected"] and not answer["text"].strip():
            self.error = "Use Space to select at least one option."
            return
        if self.index < len(self.answers) - 1:
            try:
                accepted = self.broker.confirm(self.request.id, answer)
            except ValueError as exc:
                self.error = str(exc)
                return
            if not accepted:
                self.sync()
                return
            self.index += 1
            self._load_question()
            self._focus()
            return
        try:
            accepted = self.broker.submit(self.request.id, self.answers)
        except ValueError as exc:
            self.error = str(exc)
            return
        if accepted:
            lines = []
            for q, response in zip(self.request.questions, self.answers, strict=True):
                value = "; ".join(
                    response["selected"]
                    + ([response["text"].strip()] if response["text"].strip() else [])
                )
                lines.append(f"{q.question}\n{value}")
            self.on_answer("\n\n".join(lines))
        self.hide()

    def register(self, bindings: KeyBindings) -> None:
        active = self.open_condition
        choice_focus = active & has_focus(self.choices)

        def interacting(*keys, **kwargs):
            def decorate(handler):
                @bindings.add(*keys, **kwargs)
                def dispatch(event):
                    if self._activity():
                        handler(event)

                return dispatch

            return decorate

        @bindings.add(
            "c-g",
            filter=Condition(lambda: self.broker.active is not None) & ~Condition(self.blocked),
            eager=True,
        )
        def reopen(event):
            self.sync(reopen=True)
            self._activity()

        @interacting("escape", filter=active, eager=True)
        def hide(event):
            if self.help_visible:
                self.help_visible = False
            elif self.editing and self._question().options:
                self._show_choices()
            else:
                self.hide()

        @interacting("f1", filter=active, eager=True)
        def help_keys(event):
            self.help_visible = not self.help_visible

        @interacting("c-x", filter=active, eager=True)
        def dismiss(event):
            if self.request and self.broker.dismiss(self.request.id):
                self.on_answer("Questions dismissed without an answer.")
            self.hide()

        @interacting("up", filter=choice_focus, eager=True)
        def up(event):
            self.cursor = max(0, self.cursor - 1)
            self.error = ""

        @interacting("down", filter=choice_focus, eager=True)
        def down(event):
            self.cursor = min(len(self._question().options), self.cursor + 1)
            self.error = ""

        @interacting("pageup", filter=active, eager=True)
        def question_up(event):
            self.question_scroll = max(0, self.question_scroll - 4)

        @interacting("pagedown", filter=active, eager=True)
        def question_down(event):
            self.question_scroll = min(
                len(self._heading().splitlines()) - 1, self.question_scroll + 4
            )

        @interacting(" ", filter=choice_focus, eager=True)
        def select(event):
            self.toggle()

        @interacting("tab", filter=active, eager=True)
        @interacting("s-tab", filter=active, eager=True)
        def focus(event):
            if self.editing and self._question().options:
                self._show_choices()
            else:
                self._edit()

        @interacting("enter", filter=active, eager=True)
        def submit(event):
            self.advance()

        @interacting("left", filter=choice_focus, eager=True)
        @interacting("c-b", filter=active, eager=True)
        def previous(event):
            if self.index:
                if self.editing:
                    self.answers[self.index]["text"] = self.input.text
                self.index -= 1
                self._load_question()
                self._focus()

        @interacting("c-j", filter=active & has_focus(self.input), eager=True)
        def newline(event):
            self.input.buffer.insert_text("\n")
