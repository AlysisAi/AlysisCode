import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_persona_modes import _FakeChatSession

from alysis_code.cli_impl.tui.app import run_tui
from alysis_code.cli_impl.tui.planning_chooser import PlanningController, PlanningState
from alysis_code.cli_impl.tui.state import TuiState


@pytest.fixture
def setup(monkeypatch):
    from alysis_code.cli_impl.chat import loop

    def apply_mode(*, session, next_mode, persist_default_mode):
        assert not persist_default_mode
        session.mode = next_mode

    monkeypatch.setattr(loop, "_apply_chat_effective_mode", apply_mode)
    session = _FakeChatSession(mode="auto", persona="debug")
    session.messages.append({"role": "assistant", "content": "Existing plan: keep this."})
    owner = ["session"]
    queued = []

    def read():
        return PlanningState(
            owner[0],
            session.persona,
            session.mode,
            persona_write_globs=(
                tuple(session.persona_allow_write_globs)
                if session.persona_allow_write_globs is not None
                else None
            ),
        )

    controller = PlanningController(
        read_state=read,
        dispatch=queued.append,
        apply_persona=lambda **kwargs: loop._apply_chat_persona(session=session, **kwargs),
    )
    return controller, session, queued, owner, loop


@pytest.mark.parametrize("mode", ["readonly", "review", "auto", "fullaccess"])
def test_architect_and_explicit_return_reuse_real_persona_helper(setup, mode):
    c, session, queued, _, _ = setup
    session.mode = mode
    messages = session.messages
    c.choose("architect")
    assert session.persona == "debug"
    assert len(queued) == 1
    assert queued.pop()().status == "applied"
    assert session.persona == "architect"
    assert session.mode == ("readonly" if mode == "readonly" else "review")
    assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
    assert "Return to debug" == c.picker_spec()["rows"][1]["label"]
    c.choose("return")
    assert queued.pop()().status == "applied"
    assert session.persona == "debug"
    assert session.mode == mode
    assert session.messages is messages
    assert any(m.get("content") == "Existing plan: keep this." for m in messages)
    assert c.return_state is None


def test_ask_readonly_saved_auto_return_is_ask_not_code(setup):
    c, session, queued, _, loop = setup
    loop._apply_chat_persona(session=session, persona="ask")
    assert session.mode == "readonly"
    assert session.persona_restore_mode == "auto"
    c.choose("architect")
    queued.pop()()
    assert session.mode == "readonly"
    assert c.picker_spec()["rows"][1]["label"] == "Return to ask"
    c.choose("return")
    queued.pop()()
    assert session.persona == "ask"
    assert session.mode == "readonly"


def test_later_readonly_choice_is_not_undone_on_return(setup):
    c, session, queued, _, loop = setup
    c.choose("architect")
    queued.pop()()
    loop._clear_persona_restore(session)
    session.mode = "readonly"
    c.choose("return")
    queued.pop()()
    assert session.persona == "debug"
    assert session.mode == "readonly"


@pytest.mark.parametrize("change", ["session", "mode", "persona"])
def test_stale_selection_and_duplicate_queue_do_not_mutate(setup, change):
    c, session, queued, owner, _ = setup
    c.choose("architect")
    duplicate = c.choose("architect")
    assert "already queued" in duplicate["messages"][0][1]
    assert len(queued) == 1
    if change == "session":
        owner[0] = "new"
    elif change == "mode":
        session.mode = "readonly"
    else:
        session.persona = "ask"
    assert queued.pop()().status == "cancelled"
    assert session.persona != "architect"
    assert c.return_state is None


def test_custom_write_scope_is_not_silently_replaced(setup):
    c, session, queued, _, _ = setup
    session.persona_allow_write_globs = ["notes/*.txt"]
    result = c.choose("architect")
    assert "custom write scope" in result["messages"][0][1].lower()
    assert not queued


def test_picker_cancel_and_selection_return_composer_focus(setup):
    c, session, queued, _, _ = setup

    # /persona is only a test host for the generic picker; normal-chat /plan
    # visibility and its actual action registration remain central integration.
    def run(keys):
        with create_pipe_input() as pipe:
            pipe.send_text(keys)
            return run_tui(
                TuiState(model_name="test"),
                input=pipe,
                output=DummyOutput(),
                owl_color=False,
                background_turns=False,
                picker_providers={"/persona": c.picker_spec},
            )

    _, transcript = run("/persona\r\x1bdraftX\r/exit\r")
    assert ("user", "draftX") in transcript
    assert not queued
    assert session.persona == "debug"
    _, transcript = run("/persona\r\rdraftX\r/exit\r")
    assert ("user", "draftX") in transcript
    assert len(queued) == 1
    assert session.persona == "debug"
    assert queued.pop()().status == "applied"


def test_failed_backend_outcome_does_not_claim_success(setup):
    c, _, queued, _, _ = setup

    def fail(**kwargs):
        raise RuntimeError("token=secret")

    c.apply_persona = fail
    c.choose("architect")
    result = queued.pop()()
    assert result.status == "failed"
    assert "token=secret" not in result.message
    assert c.return_state is None


def test_backend_without_transition_is_not_reported_as_active(setup):
    c, session, queued, _, _ = setup
    c.apply_persona = lambda **kwargs: None  # the backend accepts but changes nothing
    c.choose("architect")
    assert queued.pop()().status == "failed"
    assert session.persona == "debug"
    assert c.return_state is None


def test_failed_return_keeps_return_choice(setup):
    c, session, queued, _, _ = setup
    c.choose("architect")
    assert queued.pop()().status == "applied"
    c.apply_persona = lambda **kwargs: None
    c.choose("return")
    assert queued.pop()().status == "failed"
    assert session.persona == "architect"
    assert c.return_state is not None
