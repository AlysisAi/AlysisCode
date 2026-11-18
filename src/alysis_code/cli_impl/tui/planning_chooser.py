"""Planning chooser data and serialized transitions over existing chat backends.

Use picker_spec() with the TUI's existing generic picker; selection never emits
submit/prefill, edits a draft, creates a turn, or owns plan contents. Keep one
controller per session. /plan and /planning show this picker.

The injected dispatcher must reserve the chat's serialized idle boundary until
work returns, then publish the returned outcome. A busy flag alone is unsafe.
apply_persona must call _apply_chat_persona(permission_ceiling=...) at that boundary.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from threading import RLock
from typing import Literal

from ...personas import clamp_persona_exec_mode

Choice = Literal["architect", "return"]


@dataclass(frozen=True)
class PlanningState:
    owner: object = field(repr=False)
    persona: str = "code"
    mode: str = "review"
    personas_enabled: bool = True
    # Explicit custom persona scopes (including an empty deny-all scope) cannot safely be widened by replacing them
    # with Architect's Markdown scope. User allow_write_globs stay backend-owned.
    persona_write_globs: tuple[str, ...] | None = None


@dataclass(frozen=True)
class PlanningOutcome:
    status: str
    message: str


class PlanningController:
    def __init__(
        self,
        *,
        read_state: Callable[[], PlanningState],
        dispatch: Callable[[Callable[[], PlanningOutcome]], None],
        apply_persona: Callable[..., None],
    ) -> None:
        self.read_state, self.dispatch = read_state, dispatch
        self.apply_persona = apply_persona
        self._lock = RLock()
        self._prior: PlanningState | None = None
        self._applied: PlanningState | None = None
        self._pending = False

    @property
    def return_state(self) -> PlanningState | None:
        with self._lock:
            prior = self._prior
        return prior if prior is not None and prior.owner == self.read_state().owner else None

    def _unavailable(self, choice: Choice, state: PlanningState) -> str:
        if state.mode not in {"readonly", "review", "auto", "fullaccess"}:
            return "Current permissions are unknown; inspect Permissions before changing planning mode."
        if choice == "architect":
            if not state.personas_enabled:
                return "Persona modes are disabled."
            if state.persona == "architect":
                return "Already planning in this chat."
            if state.persona_write_globs is not None:
                return "Custom write scope active. Change it with /persona."
        elif choice == "return":
            prior = self.return_state
            if prior is None:
                return "No previous planning mode to return to."
            with self._lock:
                applied = self._applied
            if (
                state.persona_write_globs is not None
                and applied is not None
                and (
                    state.persona != applied.persona
                    or state.persona_write_globs != applied.persona_write_globs
                )
            ):
                return "A newer persona write scope is active. Return cannot replace it; review an explicit persona selection instead."
            if not state.personas_enabled and prior.persona != state.persona:
                return "Persona modes are disabled; cannot restore the previous persona."
        else:
            return "Unknown planning choice."
        return ""

    def picker_spec(self) -> dict:
        state = self.read_state()
        prior = self.return_state
        rows = [
            {
                "label": "Architect",
                "value": "architect",
                "description": "Plan in chat. Markdown edits only; permissions apply.",
                "current": state.persona == "architect",
            },
            {
                "label": f"Return to {prior.persona}" if prior else "Return",
                "value": "return",
                "description": "Keep saved plans and stricter permissions.",
                "current": False,
            },
        ]
        for row in rows:
            reason = self._unavailable(row["value"], state)
            if reason:
                row["description"] = reason
        return {
            "title": "Planning",
            "rows": rows,
            "on_select": self.choose,
            "hint": "↑↓ choose · Enter select · Esc close",
        }

    def choose(self, choice: Choice) -> dict:
        state = self.read_state()
        reason = self._unavailable(choice, state)
        if reason:
            return {"messages": [("system", reason)]}
        with self._lock:
            if self._pending:
                return {"messages": [("system", "A planning change is already queued.")]}
            self._pending = True

        def work():
            try:
                if self.read_state() != state:
                    return PlanningOutcome(
                        "cancelled", "Session or planning state changed; select again."
                    )
                reason = self._unavailable(choice, state)
                if reason:
                    return PlanningOutcome("unavailable", reason)
                prior = self.return_state
                if choice == "architect":
                    self.apply_persona(persona="architect", permission_ceiling=state.mode)
                else:
                    assert prior is not None
                    if state.persona != prior.persona or state.mode != prior.mode:
                        self.apply_persona(persona=prior.persona, permission_ceiling=prior.mode)
                    now = self.read_state()
                    if (
                        now.persona != prior.persona
                        or clamp_persona_exec_mode(now.mode, prior.mode) != now.mode
                    ):
                        return PlanningOutcome(
                            "failed",
                            "Return was not confirmed by the backend. Refresh current state; the saved return choice is retained.",
                        )
                    with self._lock:
                        self._prior = None
                        self._applied = None
                    return PlanningOutcome(
                        "applied",
                        f"Returned to {now.persona} ({now.mode}). Existing plan content is retained.",
                    )
                now = self.read_state()
                if now.persona != "architect":
                    return PlanningOutcome(
                        "failed",
                        "Planning mode was not confirmed by the backend. Refresh current state before retrying.",
                    )
                with self._lock:
                    if prior is None:
                        self._prior = state
                    self._applied = now
                # The real helper implements the ceiling; this check catches a
                # broken injected adapter without claiming it changed nothing.
                if clamp_persona_exec_mode(now.mode, state.mode) != now.mode:
                    return PlanningOutcome(
                        "failed",
                        "Backend exceeded the planning permission ceiling. Stop and inspect Permissions before continuing.",
                    )
                return PlanningOutcome(
                    "applied",
                    f"Planning active: {now.persona} ({now.mode}). Your plan stays in this session. Select Return explicitly to leave planning.",
                )
            except Exception:
                return PlanningOutcome(
                    "failed",
                    "Planning transition failed. Refresh current state before retrying; no new turn was requested.",
                )
            finally:
                with self._lock:
                    self._pending = False

        try:
            self.dispatch(work)
        except Exception:
            with self._lock:
                self._pending = False
            return {"messages": [("system", "Could not queue planning change; retry when idle.")]}
        return {
            "messages": [
                (
                    "system",
                    "Planning change queued for the next idle boundary; your draft is unchanged.",
                )
            ]
        }
