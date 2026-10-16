"""Planning-only transitions preserve newer independent write restrictions."""

from types import SimpleNamespace

import pytest
from test_persona_modes import _FakeChatSession, _interactive_session

from alysis_code.cli_impl.chat import loop
from alysis_code.cli_impl.tui.planning_backend import ChatPlanningBackend
from alysis_code.cli_impl.tui.planning_chooser import PlanningController
from alysis_code.cli_impl.tui.state import TuiState


def backend_for(session):
    return ChatPlanningBackend(
        lambda: session,
        SimpleNamespace(ui_mode="chat"),
        TuiState(),
        apply_persona=loop._apply_chat_persona,
        enter_forge=lambda **_: False,
    )


def controller_for(session):
    backend = backend_for(session)
    outcomes = []
    controller = PlanningController(
        read_state=backend.state,
        dispatch=lambda fn: outcomes.append(fn()),
        apply_persona=backend.apply_persona,
        enter_forge=backend.enter_forge,
        leave_forge=backend.leave_forge,
    )
    return controller, outcomes


@pytest.mark.parametrize("scope", [[], ["private/**"]])
def test_architect_refuses_existing_persona_scope_without_widening(scope):
    session = _FakeChatSession(mode="review")
    session.persona_allow_write_globs = scope.copy()
    controller, outcomes = controller_for(session)
    assert backend_for(session).state().persona_write_globs == tuple(scope)
    response = controller.choose("architect")
    assert "scope" in response["messages"][0][1]
    assert outcomes == []
    assert session.persona == "code"
    assert session.persona_allow_write_globs == scope


@pytest.mark.parametrize("scope", [[], ["private/**"]])
def test_return_refuses_newer_persona_scope_before_changing_mode(monkeypatch, scope):
    monkeypatch.setattr(
        loop,
        "_apply_chat_effective_mode",
        lambda session, next_mode, **_: setattr(session, "mode", next_mode),
    )
    session = _FakeChatSession(mode="auto")
    controller, outcomes = controller_for(session)
    controller.choose("architect")
    assert outcomes[-1].status == "applied"
    session.persona_allow_write_globs = scope.copy()
    response = controller.choose("return")
    assert "scope" in response["messages"][0][1]
    assert len(outcomes) == 1
    assert session.persona == "architect" and session.mode == "review"
    assert session.persona_allow_write_globs == scope
    assert controller.return_state is not None


@pytest.mark.parametrize("scope", [[], ["allowed/**"]])
@pytest.mark.parametrize("prior_mode", ["auto", "fullaccess"])
def test_real_tools_enforce_newer_user_scope_after_planning_return(tmp_path, prior_mode, scope):
    from alysis_code import cli
    from alysis_code.agent.errors import AgentRuntimeError
    from alysis_code.cli_impl import chat
    from alysis_code.surface import ApprovalDecision

    chat._sync_cli_globals(cli)
    session = _interactive_session(tmp_path, mode=prior_mode)
    try:
        session.surface.request_approval = lambda _: ApprovalDecision(allow=True)
        loop._apply_chat_persona(
            session=session, persona="architect", permission_ceiling=prior_mode
        )
        assert session.persona_restore_write_globs is None
        session.allow_write_globs = scope.copy()  # A newer owner restriction.
        loop._apply_chat_persona(session=session, persona="code", permission_ceiling=prior_mode)
        assert session.allow_write_globs == scope
        with pytest.raises(AgentRuntimeError, match="outside allowed scope"):
            session.tools["fs_write"].run({"path": "outside.md", "content": "must be denied"})
        assert not (tmp_path / "outside.md").exists()
        if scope:
            (tmp_path / "allowed").mkdir()
            session.tools["fs_write"].run({"path": "allowed/plan.md", "content": "allowed"})
            assert (tmp_path / "allowed/plan.md").read_text() == "allowed"
    finally:
        session.close()


def test_planning_entry_keeps_new_user_scope_while_persona_restore_is_active(monkeypatch):
    monkeypatch.setattr(
        loop,
        "_apply_chat_effective_mode",
        lambda session, next_mode, **_: setattr(session, "mode", next_mode),
    )
    session = _FakeChatSession(mode="auto")
    loop._apply_chat_persona(session=session, persona="ask")
    session.allow_write_globs = []
    loop._apply_chat_persona(session=session, persona="architect", permission_ceiling="readonly")
    assert session.allow_write_globs == []
    assert session.persona_restore_write_globs == []
    assert session.mode == "readonly"


def test_explicit_persona_without_planning_ceiling_keeps_legacy_scope_restore(monkeypatch):
    monkeypatch.setattr(
        loop,
        "_apply_chat_effective_mode",
        lambda session, next_mode, **_: setattr(session, "mode", next_mode),
    )
    session = _FakeChatSession(mode="auto")
    loop._apply_chat_persona(session=session, persona="ask")
    session.allow_write_globs = []
    loop._apply_chat_persona(session=session, persona="code")
    assert session.allow_write_globs is None
    assert session.mode == "auto"
