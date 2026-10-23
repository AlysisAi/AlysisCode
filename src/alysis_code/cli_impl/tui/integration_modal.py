"""Keyboard inspector component; the chat owner supplies scheduling and idle ownership.

``dispatcher.submit`` must atomically queue work behind running turns and retain
its reservation until work returns. ``cancel_pending`` must return False once
started. Callbacks can originate on worker threads; ``schedule_ui`` marshals them.
The caller owns the executor/dispatcher lifetime, including work after close.
``context_identity`` must return a thread-safe immutable session/workspace token;
it must change for session/account/workspace replacement, including managerless
sessions. ``on_outcome`` belongs to that captured owner, not whichever session is
currently focused. ``on_close`` restores the caller-captured composer focus.
"""

from __future__ import annotations

from collections.abc import Callable
from concurrent.futures import Executor, Future
from typing import Protocol

from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension

from .integration_inspector import (
    ActionOutcome,
    IntegrationAction,
    IntegrationController,
    IntegrationSnapshot,
    Kind,
    snapshot_lines,
)
from .modal_focus import focus_modal
from .panel_text import PanelText

_STATE_LABELS = {
    "effective enabled (configuration)": "enabled (config)",
    "effective disabled (configuration)": "disabled (config)",
    "blocked by workspace trust": "trust required",
}


class PendingIntegrationWork(Protocol):
    def cancel_pending(self) -> bool:
        """Atomically remove queued work; False means started or already completed."""
        ...


class IntegrationDispatcher(Protocol):
    def submit(
        self,
        work: Callable[[], ActionOutcome],
        *,
        started: Callable[[], None],
        completed: Callable[[ActionOutcome], None],
    ) -> PendingIntegrationWork:
        """Reserve the serialized idle boundary through work completion.

        Call completed exactly once for started work, including failures. A
        successful pending cancellation must never execute work. Submission
        failure must raise before accepting work. Never call work on the UI thread.
        """
        ...


class IntegrationModal:
    def __init__(
        self,
        *,
        kind: Kind,
        controller: IntegrationController,
        dispatcher: IntegrationDispatcher,
        executor: Executor,
        schedule_ui: Callable[[Callable[[], None]], None],
        context_identity: Callable[[], object],
        on_close: Callable[[], None],
        on_outcome: Callable[[ActionOutcome], None],
        invalidate: Callable[[], None],
    ) -> None:
        self.kind = kind
        self.controller = controller
        self.dispatcher = dispatcher
        self.executor = executor
        self.schedule_ui = schedule_ui
        self.context_identity = context_identity
        self.owner = context_identity()
        self.on_close = on_close
        self.on_outcome = on_outcome
        self.invalidate = invalidate
        self.is_open = True
        self.snapshot = IntegrationSnapshot(kind)
        self.selected = 0
        self.status = "Loading integration state…"
        self._generation = 0
        self._operation = 0
        self._busy = False
        self._handle: PendingIntegrationWork | None = None
        self._refresh_future: Future | None = None
        self.list_control = FormattedTextControl(
            self._list_text,
            focusable=True,
            get_cursor_position=lambda: Point(x=0, y=self.selected),
        )
        self.details_open = False
        self.help_open = False
        self.details = PanelText(height=Dimension(min=3))
        bindings = KeyBindings()

        @bindings.add("escape")
        def close(event):
            self.close()

        @bindings.add("tab")
        def next_focus(event):
            focus_modal(event.app.layout, self.container)

        @bindings.add("s-tab")
        def previous_focus(event):
            focus_modal(event.app.layout, self.container, -1)

        @bindings.add("up", filter=has_focus(self.list_control))
        def previous_row(event):
            self.select(-1)

        @bindings.add("down", filter=has_focus(self.list_control))
        def next_row(event):
            self.select(1)

        @bindings.add("enter", filter=has_focus(self.list_control))
        def show_details(event):
            self.details_open = True
            self._show_details()
            event.app.layout.focus(self.details)

        @bindings.add("r")
        def refresh(event):
            self.refresh()

        for key, action in (("e", "enable"), ("d", "disable"), ("x", "restart")):

            def invoke(event, action=action):
                self.request_action(action)

            bindings.add(key)(invoke)

        @bindings.add("i")
        def details(event):
            self.details_open = not self.details_open
            self._show_details()

        @bindings.add("f1")
        @bindings.add("?")
        def help_(event):
            self.help_open = not self.help_open
            self.invalidate()

        self.container = HSplit(
            [
                Window(
                    self.list_control,
                    height=Dimension(min=1, max=4),
                    dont_extend_height=True,
                ),
                Window(height=Dimension(min=0, preferred=1, max=1)),
                self.details,
                Window(
                    FormattedTextControl(lambda: [("", self.status)]),
                    height=Dimension(min=1, max=3),
                    wrap_lines=True,
                    dont_extend_height=True,
                ),
                ConditionalContainer(
                    Window(
                        FormattedTextControl(
                            "↑/↓ select or scroll · Tab focus · Enter details\n"
                            "PgUp/PgDn/Home/End scroll · R refresh · Esc close"
                        ),
                        height=3,
                        wrap_lines=True,
                    ),
                    filter=Condition(lambda: self.help_open),
                ),
                Window(
                    FormattedTextControl(self._help_text),
                    height=Dimension(min=1, max=3),
                    wrap_lines=True,
                    dont_extend_height=True,
                ),
            ],
            key_bindings=bindings,
            modal=True,
        )
        self.focus_target = self.list_control
        self.refresh()

    def _current(self) -> bool:
        return self.is_open and self.context_identity() == self.owner

    def _list_text(self):
        if not self.snapshot.rows:
            return [("", "No integrations available.")]
        return [
            (
                "class:selected" if i == self.selected else "",
                f"{'>' if i == self.selected else ' '} {row.label} [{row.scope}] — "
                f"{_STATE_LABELS.get(row.state, row.state)}"
                + ("\n" if i + 1 < len(self.snapshot.rows) else ""),
            )
            for i, row in enumerate(self.snapshot.rows)
        ]

    def _help_text(self):
        actions = self.snapshot.rows[self.selected].actions if self.snapshot.rows else ()
        labels = {"enable": "E Enable", "disable": "D Disable", "restart": "X Restart"}
        action_text = " · ".join(labels[action] for action in actions if action in labels)
        return [
            (
                "",
                ("I less" if self.details_open else "I details")
                + " · F1 help · Esc close"
                + ("\n" + action_text if action_text else ""),
            )
        ]

    def _show_details(self):
        rows = (self.snapshot.rows[self.selected],) if self.snapshot.rows else ()
        self.details.text = "\n".join(
            snapshot_lines(IntegrationSnapshot(self.kind, rows, self.snapshot.notices))
        )
        if not self.details_open:
            lines = list(self.snapshot.notices)
            for row in rows:
                lines += [
                    f"{row.configuration_label}: {'enabled' if row.configured_enabled else 'disabled'}"
                    + (f" · {len(row.tools)} tools" if self.kind == "mcp" else ""),
                ]
                if row.error:
                    lines.append("Error: " + row.error)
                # These include actionable lifecycle/authentication notices, not raw config.
                lines.extend(row.details if row.summary is None else row.summary)
            self.details.text = "\n".join(lines) or "No integrations available."
        self.invalidate()

    def select(self, offset: int) -> None:
        if self.snapshot.rows:
            self.selected = max(0, min(len(self.snapshot.rows) - 1, self.selected + offset))
            self._show_details()

    def refresh(self, *, preserve_status: bool = False) -> None:
        if not self._current():
            return
        self._generation += 1
        generation = self._generation
        previous = (
            (self.snapshot.rows[self.selected].key, self.snapshot.rows[self.selected].scope)
            if self.snapshot.rows
            else None
        )
        if self._refresh_future:
            self._refresh_future.cancel()
        if not self._busy and not preserve_status:
            self.status = "Refreshing integration state…"
        self.invalidate()
        try:
            future = self.executor.submit(self.controller.refresh, self.kind)
        except Exception:
            self.status = "Refresh unavailable. Reopen the inspector to retry."
            self.invalidate()
            return
        self._refresh_future = future

        def done(future):
            if future.cancelled():
                return
            try:
                snapshot = future.result()
            except Exception:
                snapshot = IntegrationSnapshot(
                    self.kind, notices=("Refresh failed. Check configuration and retry.",)
                )

            def publish():
                if not self._current() or generation != self._generation:
                    return
                self.snapshot = snapshot
                self.selected = next(
                    (i for i, row in enumerate(snapshot.rows) if (row.key, row.scope) == previous),
                    0,
                )
                if not self._busy and self.status.startswith(("Refreshing", "Loading")):
                    self.status = "Current state loaded."
                self._show_details()

            self.schedule_ui(publish)

        future.add_done_callback(done)

    def request_action(self, action: str) -> None:
        if not self._current() or self._busy or not self.snapshot.rows:
            return
        row = self.snapshot.rows[self.selected]
        if action not in row.actions:
            self.status = (
                "Action unavailable. See external configuration/authentication instructions."
            )
            self.invalidate()
            return
        request = IntegrationAction(self.kind, row.key, action, row.scope)
        owner = self.owner
        try:
            manager = self.controller.manager_identity()
        except Exception:
            self.status = "Backend unavailable. Refresh or restart the session."
            self.invalidate()
            return
        self._operation += 1
        operation = self._operation
        self._busy = True
        self.status = "Queued — waiting for the current turn to finish."
        self.invalidate()

        def work():
            # The caller owns the idle reservation here, including revalidation.
            if not self.is_open or self.context_identity() != owner:
                return ActionOutcome(
                    "cancelled", "Inspector or session changed before the action started."
                )
            try:
                if self.controller.manager_identity() is not manager:
                    return ActionOutcome(
                        "cancelled", "Session manager changed before the action started."
                    )
            except Exception:
                return ActionOutcome(
                    "unavailable", "Live backend unavailable. Restart the session."
                )
            try:
                result = self.controller.request(request)
                if result.status != "deferred":
                    return result
                return self.controller.apply_pending_at_idle(turn_running=False)
            except Exception:
                return ActionOutcome(
                    "failed",
                    "Integration change failed. Refresh and check configuration externally.",
                )

        def started():
            def publish():
                if self._current() and self._busy and operation == self._operation:
                    self.status = "Applying — closing will not cancel an action already started."
                    self.invalidate()

            self.schedule_ui(publish)

        def completed(outcome):
            def publish():
                # Always deliver the actual outcome, even after closing or switching sessions.
                self.on_outcome(outcome)
                if operation != self._operation:
                    return
                self._busy = False
                self._handle = None
                if self._current():
                    self.status = (
                        f"{outcome.status.replace('_', ' ').capitalize()}: {outcome.message}"
                    )
                    self.refresh(preserve_status=True)
                    self.invalidate()

            self.schedule_ui(publish)

        try:
            handle = self.dispatcher.submit(work, started=started, completed=completed)
            if self._busy:
                self._handle = handle
        except Exception:
            self._busy = False
            self.status = "Failed to queue action. No change was submitted; retry when idle."
            self.invalidate()

    def close(self) -> None:
        if not self.is_open:
            return
        self.is_open = False
        self._generation += 1
        if self._refresh_future:
            self._refresh_future.cancel()
        if self._handle is not None and self._handle.cancel_pending():
            self._busy = False
            self._handle = None
            self.on_outcome(ActionOutcome("cancelled", "Queued integration change cancelled."))
        # Caller restores its captured composer focus/draft; this component never edits either.
        self.on_close()
