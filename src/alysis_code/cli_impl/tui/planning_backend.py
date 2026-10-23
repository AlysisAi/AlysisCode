"""Existing chat planning backends; callers must reserve the session while invoking."""

from io import StringIO

from ...personas import persona_modes_enabled
from ...surface import ApprovalRequest
from ...surface.console import make_console
from .planning_chooser import PlanningState


class ChatPlanningBackend:
    def __init__(self, session_provider, forge_state, tui_state, *, apply_persona, enter_forge):
        self.session_provider = session_provider
        self.forge, self.tui = forge_state, tui_state
        self._apply_persona, self._enter_forge = apply_persona, enter_forge

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
            ui_mode=self.forge.ui_mode,
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

    def forge_allowed(self):
        session = self.session_provider()
        if session is None or session.mode not in {"review", "auto", "fullaccess"}:
            return False
        # Forge's existing entry helper performs multiple direct artifact writes.
        # It cannot enforce arbitrary agent path scopes; fail closed, never widen.
        if (
            getattr(session, "allow_write_globs", None) is not None
            or getattr(session, "persona_allow_write_globs", None) is not None
            or bool(getattr(session, "deny_write_prefixes", None))
        ):
            return False
        surface = getattr(session, "surface", None)
        if surface is None:
            return False
        decision = surface.request_approval(
            ApprovalRequest(
                kind="write",
                reason="Enter Forge and create or refresh its planning artifacts?",
                preview="Forge writes plan/workspace metadata and its current-run pointer. This does not start a model turn or execute the plan.",
                files=[str(session.root)],
                metadata={"allow_for_session_disabled": True},
            )
        )
        return bool(decision.allow)

    def enter_forge(self):
        session = self.session_provider()
        entered = self._enter_forge(
            root=session.root,
            console=make_console(file=StringIO()),
            forge_state=self.forge,
            model=getattr(session, "model", None),
            mode=session.mode,
            planner_assistant_default=False,
        )
        self._sync()
        return entered

    def leave_forge(self):
        # /back's state transition without its optional disk transcript note: a
        # later readonly restriction must still permit leaving with plan intact.
        self.forge.ui_mode = "chat"
        self._sync()

    def _sync(self):
        self.tui.forge_mode = self.forge.ui_mode == "forge"
        self.tui.forge_run_id = (
            str(getattr(getattr(self.forge, "paths", None), "run_id", "") or "")
            if self.tui.forge_mode
            else ""
        )
