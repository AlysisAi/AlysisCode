"""Standalone review scope selector and read-only snapshot inspection modal."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from prompt_toolkit.application.current import get_app
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import D
from prompt_toolkit.widgets import Button, RadioList, TextArea

from ...code_review import CodeReviewEngine
from ...review_service import (
    ReviewCancellationToken,
    ReviewController,
    inspect_finding,
    make_request,
)
from .panel_text import PanelText


class ReviewModal:
    """Expose container/focus_target; owner retains controller across reopen.

    engine_factory(token) creates a dedicated client in the worker thread.
    dispose_engine(engine), if supplied, runs only after that worker finishes,
    including after cancellation. Never dispose the client from close().
    """

    def __init__(
        self,
        workspace: Path,
        *,
        engine_factory: Callable[[ReviewCancellationToken], CodeReviewEngine],
        on_close: Callable[[], None],
        invalidate: Callable[[], None],
        controller: ReviewController | None = None,
        dispose_engine: Callable[[CodeReviewEngine], None] | None = None,
        eligible: Callable[[], bool] = lambda: True,
    ):
        self.eligible = eligible
        self.workspace = workspace
        self.on_close = on_close
        self.invalidate = invalidate
        self.is_open = True
        self.setup = True
        self.show_help = False
        self.detail_mode = "summary"
        self.index = 0
        self._shown_result = None
        self.controller = controller or ReviewController(
            engine_factory, on_change=self._changed, dispose_engine=dispose_engine
        )
        self.controller.on_change = self._changed
        self.scope = RadioList(
            [
                ("working_tree", "Working tree (staged, unstaged, untracked)"),
                ("branch", "Branch (merge base to head)"),
                ("commit", "Commit (first parent to commit)"),
                ("range", "Range (base to head)"),
            ]
        )
        self.base = TextArea(text="", height=1, multiline=False, prompt="Base: ")
        self.head = TextArea(text="HEAD", height=1, multiline=False, prompt="Head: ")
        self.revision = TextArea(text="HEAD", height=1, multiline=False, prompt="Commit: ")
        self.details = PanelText(height=D(min=3))
        self.findings = FormattedTextControl(self._finding_text, focusable=True)
        self.focus_target = self.scope
        self.start_button = Button("Start review", handler=self.start, width=16)
        self.cancel_button = Button("Cancel review", handler=self.controller.cancel, width=16)
        bindings = KeyBindings()

        @bindings.add("escape", eager=True)
        def close(event):
            self.close()

        @bindings.add("tab")
        def next_field(event):
            event.app.layout.focus_next()

        @bindings.add("s-tab")
        def previous_field(event):
            event.app.layout.focus_previous()

        @bindings.add("up", filter=Condition(lambda: get_app().layout.has_focus(self.findings)))
        @bindings.add("down", filter=Condition(lambda: get_app().layout.has_focus(self.findings)))
        def select(event):
            self.select(-1 if event.key_sequence[0].key == "up" else 1)

        @bindings.add("enter", filter=Condition(lambda: get_app().layout.has_focus(self.findings)))
        def inspect(event):
            self.detail_mode = "evidence"
            self.inspect_selected()
            event.app.layout.focus(self.details)

        not_typing = Condition(
            lambda: (
                not any(
                    get_app().layout.has_focus(field)
                    for field in (self.base, self.head, self.revision)
                )
            )
        )
        has_result = Condition(lambda: not self.setup and self.controller.result is not None)

        @bindings.add("f1")
        @bindings.add("?", filter=not_typing)
        def help_toggle(event):
            self.show_help = not self.show_help

        @bindings.add("c", filter=not_typing & has_result)
        def coverage(event):
            self.detail_mode = "coverage"
            self.inspect_selected()
            event.app.layout.focus(self.details)

        @bindings.add("b", filter=not_typing & has_result)
        def back(event):
            self.detail_mode = "summary"
            self.inspect_selected()
            event.app.layout.focus(self.findings)

        @bindings.add("n", filter=not_typing & has_result)
        def new_review(event):
            self.setup = True
            event.app.layout.focus(self.scope)

        self.container = HSplit(
            [
                Window(
                    FormattedTextControl(
                        lambda: (
                            "Review / "
                            + ("choose scope" if self.setup else self.detail_mode)
                            + " / read only"
                        )
                    ),
                    height=1,
                ),
                ConditionalContainer(
                    HSplit(
                        [
                            self.scope,
                            ConditionalContainer(
                                self.base,
                                filter=Condition(
                                    lambda: self.scope.current_value in {"branch", "range"}
                                ),
                            ),
                            ConditionalContainer(
                                self.head,
                                filter=Condition(
                                    lambda: self.scope.current_value in {"branch", "range"}
                                ),
                            ),
                            ConditionalContainer(
                                self.revision,
                                filter=Condition(lambda: self.scope.current_value == "commit"),
                            ),
                            self.start_button,
                        ]
                    ),
                    filter=Condition(lambda: self.setup and not self.controller.running),
                ),
                ConditionalContainer(
                    self.cancel_button, filter=Condition(lambda: self.controller.running)
                ),
                ConditionalContainer(
                    Window(
                        FormattedTextControl(lambda: self.controller.status),
                        height=D(min=1, max=3),
                        wrap_lines=True,
                    ),
                    filter=Condition(lambda: self.setup or self.controller.running),
                ),
                ConditionalContainer(
                    HSplit(
                        [
                            Window(
                                FormattedTextControl(self._coverage_status),
                                height=D(min=1, max=3),
                                wrap_lines=True,
                            ),
                            Window(self.findings, height=1),
                            self.details,
                        ]
                    ),
                    filter=Condition(lambda: not self.setup),
                ),
                ConditionalContainer(
                    Window(
                        FormattedTextControl(
                            "Tab focus / Space scope / Enter start/evidence\n"
                            "Up/Down findings / C coverage / B back / N new\n"
                            "Text: arrows, PgUp/PgDn, Home/End scroll\n"
                            "Esc closes and cancels a running review."
                        ),
                        height=4,
                        wrap_lines=True,
                    ),
                    filter=Condition(lambda: self.show_help),
                ),
                Window(
                    FormattedTextControl(
                        lambda: (
                            "Tab move • Enter start • F1 help • Esc close"
                            if self.setup
                            else "Enter evidence • C coverage • F1 help • Esc close"
                        )
                    ),
                    height=1,
                ),
            ],
            width=D(weight=1),
            key_bindings=bindings,
            modal=True,
        )
        if self.controller.result is not None:
            self._changed()

    def request(self):
        return make_request(
            self.scope.current_value,
            base=self.base.text,
            head=self.head.text,
            revision=self.revision.text,
        )

    def _scope_text(self):
        mode = self.scope.current_value
        if mode == "working_tree":
            return "Selected: all working-tree changes. Revision fields ignored."
        if mode == "commit":
            return f"Selected: commit {self.revision.text} (first-parent comparison). Base/head ignored."
        return f"Selected: {mode} {self.base.text or '(base required)'} to {self.head.text}" + (
            " via merge base" if mode == "branch" else ""
        )

    def start(self):
        if not self.eligible():
            self.controller.status = (
                "Wait for active and queued work to finish before starting review."
            )
            self.invalidate()
            return
        if self.controller.running:
            return
        try:
            request = self.request()
        except (ValueError, RuntimeError) as exc:
            self.controller.status = f"Invalid scope: {exc}"
            self.invalidate()
            return
        self.index = 0
        self._shown_result = None
        self.details.text = ""
        self.controller.start(request)
        self._focus(self.cancel_button)

    def _changed(self):
        if not self.is_open:
            return
        result = self.controller.result
        if result is not None and result is not self._shown_result:
            self._shown_result = result
            self.setup = False
            self.detail_mode = "summary"
            self.index = 0
            self.inspect_selected()
            self.focus_target = self.findings
            self._focus(self.findings)
        if self.setup and not self.controller.running:
            self._focus(self.scope)
        self.invalidate()

    @staticmethod
    def _focus(target):
        app = get_app()
        try:
            app.layout.focus(target)
        except ValueError:
            pass  # Construction or a retained result before attachment.

    def _finding_text(self):
        result = self.controller.result
        if result is None:
            return [("", "Findings appear here after review.")]
        if not result.findings:
            return [("", "No findings returned. C: coverage details")]
        finding = result.findings[self.index]
        return [
            (
                "",
                f"Finding {self.index + 1}/{len(result.findings)} · {finding.severity} · Up/Down select",
            )
        ]

    def _coverage_status(self):
        result = self.controller.result
        if result is None:
            return ""
        summary = result.summary
        incomplete = (
            summary.omitted_file_count
            or summary.truncated
            or summary.reviewed_file_count < summary.changed_file_count
            or result.diff.omitted_files
            or result.diff.truncated
        )
        warning_count = len(summary.warnings)
        return (
            f"{'INCOMPLETE · ' if incomplete else ''}"
            f"reviewed {summary.reviewed_file_count}/{summary.changed_file_count} files"
            + (f" · {warning_count} warning(s)" if warning_count else "")
            + (" · C: coverage details" if incomplete or warning_count else "")
        )

    def select(self, offset: int):
        result = self.controller.result
        if result is not None and result.findings:
            self.index = min(max(0, self.index + offset), len(result.findings) - 1)
            self.detail_mode = "summary"
            self.inspect_selected()
            self.invalidate()

    def inspect_selected(self):
        result = self.controller.result
        if result is None:
            return
        summary = result.summary
        coverage = f"Reviewed scope: {result.scope.value} {result.diff.metadata} / Verdict: {summary.verdict} / reviewed {summary.reviewed_file_count}/{summary.changed_file_count} files / omitted {summary.omitted_file_count} / truncated: {summary.truncated}"
        warnings = "\n".join(
            (
                *summary.warnings,
                *(f"Omitted {item.path}: {item.reason}" for item in result.diff.omitted_files),
            )
        )
        if self.detail_mode == "coverage":
            self.details.text = (
                f"{coverage}\n{warnings}\n{summary.overview}\n\n"
                "Repository changes only; no authorship attribution."
            )
            return
        if not result.findings:
            self.details.text = (
                "No findings returned.\n\n"
                + summary.overview
                + "\n\nThis is not a guarantee that the changes are defect-free."
            )
            return
        finding = result.findings[self.index]
        header = (
            f"{finding.title}\n"
            f"{finding.path}:{finding.line_start or '?'}-{finding.line_end or '?'} / {finding.severity}\n\n"
            f"{finding.explanation}"
        )
        if self.detail_mode == "summary":
            self.details.text = header
            return
        location = inspect_finding(result, finding)
        self.details.text = (
            header + f"\n\nEvidence: {finding.evidence}\n"
            f"Suggested fix (not applied): {finding.suggested_fix}\n"
            f"Confidence: {finding.confidence}\n{location.notice}\n\n" + location.text
        )

    def close(self):
        if self.is_open:
            self.is_open = False
            self.controller.cancel(notify=False)
            self.controller.on_change = lambda: None
            self.on_close()
            self.invalidate()
