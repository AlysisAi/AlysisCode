import io
from types import SimpleNamespace

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console
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
    forge = SimpleNamespace(ui_mode="chat", plan={"goal": "existing"})
    queued, calls = [], []

    def read():
        return PlanningState(
            owner[0],
            session.persona,
            session.mode,
            forge.ui_mode,
            persona_write_globs=(
                tuple(session.persona_allow_write_globs)
                if session.persona_allow_write_globs is not None
                else None
            ),
        )

    def enter():
        calls.append("forge")
        forge.ui_mode = "forge"
        return True

    def leave():
        calls.append("back")
        forge.ui_mode = "chat"

    controller = PlanningController(
        read_state=read,
        dispatch=queued.append,
        apply_persona=lambda **kwargs: loop._apply_chat_persona(session=session, **kwargs),
        enter_forge=enter,
        leave_forge=leave,
    )
    return controller, session, forge, queued, calls, owner, loop


@pytest.mark.parametrize("mode", ["readonly", "review", "auto", "fullaccess"])
def test_architect_and_explicit_return_reuse_real_persona_helper(setup, mode):
    c, session, forge, queued, calls, _, _ = setup
    session.mode = mode
    plan = forge.plan
    messages = session.messages
    c.choose("architect")
    assert session.persona == "debug"
    assert len(queued) == 1
    assert queued.pop()().status == "applied"
    assert session.persona == "architect"
    assert session.mode == ("readonly" if mode == "readonly" else "review")
    assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
    assert "Return to debug" == c.picker_spec()["rows"][2]["label"]
    c.choose("return")
    assert queued.pop()().status == "applied"
    assert session.persona == "debug"
    assert session.mode == mode
    assert forge.plan is plan
    assert session.messages is messages
    assert any(m.get("content") == "Existing plan: keep this." for m in messages)
    assert not calls
    assert c.return_state is None


def test_ask_readonly_saved_auto_return_is_ask_not_code(setup):
    c, session, _, queued, _, _, loop = setup
    loop._apply_chat_persona(session=session, persona="ask")
    assert session.mode == "readonly"
    assert session.persona_restore_mode == "auto"
    c.choose("architect")
    queued.pop()()
    assert session.mode == "readonly"
    assert c.picker_spec()["rows"][2]["label"] == "Return to ask"
    c.choose("return")
    queued.pop()()
    assert session.persona == "ask"
    assert session.mode == "readonly"


def test_later_readonly_choice_is_not_undone_on_return(setup):
    c, session, _, queued, _, _, loop = setup
    c.choose("architect")
    queued.pop()()
    loop._clear_persona_restore(session)
    session.mode = "readonly"
    c.choose("return")
    queued.pop()()
    assert session.persona == "debug"
    assert session.mode == "readonly"


def test_forge_is_fail_closed_without_write_authorization(setup):
    c, session, forge, queued, calls, _, _ = setup
    c.choose("forge")
    assert queued.pop()().status == "unavailable"
    assert calls == []
    assert forge.ui_mode == "chat"
    session.mode = "readonly"
    c.forge_allowed = lambda: pytest.fail("readonly must not reach artifact approval")
    result = c.choose("forge")
    assert "readonly" in result["messages"][0][1]
    assert queued == []


def test_forge_back_uses_real_backend_and_retains_actual_plan(tmp_path, setup):
    from alysis_code import cli
    from alysis_code.cli_impl import chat
    from alysis_code.cli_impl.chat.state import _ForgeChatState

    c, session, _, queued, _, owner, _ = setup
    root = tmp_path / "repo"
    root.mkdir()
    forge = _ForgeChatState()
    console = Console(file=io.StringIO(), force_terminal=False)
    c.read_state = lambda: PlanningState(owner[0], session.persona, session.mode, forge.ui_mode)
    c.forge_allowed = lambda: True  # Explicit fake authorization; no user policy changed.
    c.enter_forge = lambda: cli._enter_forge_mode(
        root=root,
        console=console,
        forge_state=forge,
        mode=session.mode,
        planner_assistant_default=False,
    )
    c.leave_forge = lambda: chat._handle_forge_chat_command_impl(
        cli, input_text="/back", forge_state=forge, session=session, console=console
    )
    c.choose("forge")
    assert queued.pop()().status == "applied"
    assert forge.ui_mode == "forge"
    plan, paths = forge.plan, forge.paths
    assert plan is not None and paths is not None
    data = paths.plan_json_path.read_bytes()
    c.choose("return")
    assert queued.pop()().status == "applied"
    assert forge.ui_mode == "chat"
    assert forge.plan is plan and forge.paths is paths
    assert paths.plan_json_path.read_bytes() == data
    assert session.mode == "auto"


@pytest.mark.parametrize("change", ["session", "mode", "persona"])
def test_stale_selection_and_duplicate_queue_do_not_mutate(setup, change):
    c, session, _, queued, calls, owner, _ = setup
    c.choose("architect")
    c.choose("forge")
    assert len(queued) == 1
    if change == "session":
        owner[0] = "new"
    elif change == "mode":
        session.mode = "readonly"
    else:
        session.persona = "ask"
    assert queued.pop()().status == "cancelled"
    assert not calls
    assert c.return_state is None


def test_custom_write_scope_is_not_silently_replaced(setup):
    c, session, _, queued, _, _, _ = setup
    session.persona_allow_write_globs = ["notes/*.txt"]
    result = c.choose("architect")
    assert "custom write scope" in result["messages"][0][1].lower()
    assert not queued


def test_picker_cancel_and_selection_return_composer_focus(setup):
    c, session, _, queued, _, _, _ = setup

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
    c, _, _, queued, _, _, _ = setup

    def fail(**kwargs):
        raise RuntimeError("token=secret")

    c.apply_persona = fail
    c.choose("architect")
    result = queued.pop()()
    assert result.status == "failed"
    assert "token=secret" not in result.message
    assert c.return_state is None


def test_backend_true_without_transition_is_not_reported_as_active(setup):
    c, _, _, queued, _, _, _ = setup
    c.forge_allowed = lambda: True
    c.enter_forge = lambda: True
    c.choose("forge")
    assert queued.pop()().status == "failed"
    assert c.return_state is None


def test_failed_forge_back_keeps_return_choice(setup):
    c, _, _, queued, _, _, _ = setup
    c.forge_allowed = lambda: True
    c.choose("forge")
    queued.pop()()
    c.leave_forge = lambda: None
    c.choose("return")
    assert queued.pop()().status == "failed"
    assert c.return_state is not None
