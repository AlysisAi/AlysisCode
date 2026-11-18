"""Existing chat planning backends; callers must reserve the session while invoking."""

from ...personas import persona_modes_enabled
from .planning_chooser import PlanningState


class ChatPlanningBackend:
    def __init__(self, session_provider, tui_state, *, apply_persona):
        self.session_provider = session_provider
        self.tui = tui_state
        self._apply_persona = apply_persona

    def state(self):
        session = self.session_provider()
        return PlanningState(
            owner=(
                id(session),
                str(getattr(getattr(session, "store", None), "session_id", "")),
                str(getattr(session, "root", "")),
            ),
            persona=getattr(session, "persona", "code"),
            mode=getattr(session, "mode", "readonly"),
            personas_enabled=persona_modes_enabled(getattr(session, "cfg", None)),
            persona_write_globs=(
                tuple(session.persona_allow_write_globs)
                if getattr(session, "persona_allow_write_globs", None) is not None
                else None
            ),
        )

    def apply_persona(self, *, persona, permission_ceiling):
        session = self.session_provider()
        self._apply_persona(
            session=session,
            persona=persona,
            source="planning",
            permission_ceiling=permission_ceiling,
        )
        self.tui.persona, self.tui.exec_mode = session.persona, session.mode
