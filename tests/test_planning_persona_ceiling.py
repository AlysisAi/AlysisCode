import pytest
from test_persona_modes import _FakeChatSession


@pytest.fixture
def loop_mod(monkeypatch):
    from alysis_code.cli_impl.chat import loop

    def apply_mode(*, session, next_mode, persist_default_mode):
        assert persist_default_mode is False
        session.mode = next_mode

    monkeypatch.setattr(loop, "_apply_chat_effective_mode", apply_mode)
    return loop


def test_planning_ceiling_keeps_ask_readonly_and_returns_to_ask(loop_mod):
    session = _FakeChatSession(mode="auto")
    loop_mod._apply_chat_persona(session=session, persona="ask")
    assert session.mode == "readonly"
    assert session.persona_restore_mode == "auto"
    messages = session.messages
    loop_mod._apply_chat_persona(
        session=session, persona="architect", permission_ceiling=session.mode
    )
    assert session.mode == "readonly"
    assert session.persona_restore_mode == "auto"
    assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
    loop_mod._apply_chat_persona(session=session, persona="ask", permission_ceiling="readonly")
    assert session.persona == "ask"
    assert session.mode == "readonly"
    assert session.persona_restore_mode == "auto"
    assert session.messages is messages


def test_explicit_persona_without_ceiling_retains_existing_restore_semantics(loop_mod):
    session = _FakeChatSession(mode="auto")
    loop_mod._apply_chat_persona(session=session, persona="ask")
    loop_mod._apply_chat_persona(session=session, persona="architect")
    assert session.mode == "review"
    assert session.persona_restore_mode == "auto"


@pytest.mark.parametrize("invalid", ["", "unknown", "AUTO"])
def test_invalid_ceiling_rejected_before_state_changes(loop_mod, invalid):
    session = _FakeChatSession(mode="readonly")
    with pytest.raises(ValueError):
        loop_mod._apply_chat_persona(
            session=session, persona="architect", permission_ceiling=invalid
        )
    assert session.mode == "readonly"
    assert session.persona == "code"
    assert session.store.events == []
