from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ...config import AppConfig
from ...forge import RunPaths
from ...forge_planner_engine import ForgePlannerState


@dataclass
class _ForgePlannerSessionState(ForgePlannerState):
    """Legacy CLI spelling for the UI-neutral planner state."""

    # Consecutive clarification-only planner turns for the current goal. Past the
    # policy cap the planner is made to draft a plan instead of asking again.


@dataclass(frozen=True)
class _ForgeEnterCommand:
    entry_mode: str
    initial_goal: str = ""
    usage_error: str | None = None
    # Explicit plan-assistant choice carried by the ``/forge --planner`` /
    # ``/forge --no-planner`` forms (emitted by the TUI intro popup's native
    # "Use the planner?" prompt, and usable directly). ``None`` means "no
    # explicit choice" — the caller falls back to its own default.
    planner_assistant_default: bool | None = None


@dataclass(frozen=True)
class _ForgeChatEntrySelection:
    paths: RunPaths
    plan: dict[str, Any]
    entry_kind: str


@dataclass
class _ForgeChatState:
    ui_mode: str = "chat"
    paths: RunPaths | None = None
    plan: dict[str, Any] | None = None
    assistant_enabled: bool = False
    entry_request_mode: str = "plain"
    entry_goal: str = ""
    planner_session: _ForgePlannerSessionState = field(default_factory=_ForgePlannerSessionState)
    # Swarm knobs chosen in the TUI launch gate (``/execute plan``), read by the
    # ``/execute`` command handler when present. Empty dict → the handler's own
    # defaults apply (so the classic, non-TUI path is unchanged). Recognised keys:
    # ``parallel`` (int), ``scope_mode`` ("strict"|"warn"), ``verify_mode``
    # ("warn"|"strict"|"off"), ``review`` (bool).
    swarm_knobs: dict[str, Any] = field(default_factory=dict)
    # True once the current ``/execute plan`` invocation actually reached
    # ``run_swarm`` (reset at the top of each invocation). Lets the TUI worker
    # distinguish "the handler rejected the execute before anything ran" from
    # "a swarm ran" when finalizing the live forge view.
    swarm_run_attempted: bool = False

    @property
    def planner_transcript(self) -> list[dict[str, str]]:
        return self.planner_session.transcript

    @planner_transcript.setter
    def planner_transcript(self, value: list[dict[str, str]]) -> None:
        self.planner_session.transcript = list(value)

    @property
    def planner_cfg(self) -> AppConfig | None:
        return self.planner_session.cfg

    @planner_cfg.setter
    def planner_cfg(self, value: AppConfig | None) -> None:
        self.planner_session.cfg = value

    @property
    def workspace_context(self) -> dict[str, Any] | None:
        return self.planner_session.workspace_context

    @workspace_context.setter
    def workspace_context(self, value: dict[str, Any] | None) -> None:
        self.planner_session.workspace_context = value

    @property
    def planner_awaiting_clarification(self) -> bool:
        return bool(self.planner_session.awaiting_clarification)

    @planner_awaiting_clarification.setter
    def planner_awaiting_clarification(self, value: bool) -> None:
        self.planner_session.awaiting_clarification = bool(value)

    @property
    def planner_pending_questions(self) -> list[str]:
        return self.planner_session.pending_questions

    @planner_pending_questions.setter
    def planner_pending_questions(self, value: list[str]) -> None:
        self.planner_session.pending_questions = list(value)


@dataclass(frozen=True)
class _ChatExecutionRequest:
    instruction: str
    routing_mode_override: str | None = None
    ephemeral_system_messages: tuple[str, ...] = ()
    ephemeral_user_messages: tuple[str, ...] = ()
    # One-turn execution-mode override (``/ask``): the chat loop applies
    # ``mode_override`` before the turn and restores ``restore_mode_after``
    # afterwards, exception-safe.
    mode_override: str | None = None
    restore_mode_after: str | None = None
    chat_only: bool = False


__all__ = [name for name in globals() if not name.startswith("__") or name == "__version__"]
