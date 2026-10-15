"""Persona modes: registry, config keys, env-context line, surface event.

Personas are conventions layered on the execution-mode gate (see
``docs/personas.md``). This suite pins the persona configuration surface: the
registry vocabulary and defaults, strict config-time validation vs lenient
runtime normalization, the kill-switch pair, the ``persona_models.<persona>``
dotted keys, the environment-context ``persona:`` line (absent for the no-op
Code persona), and the ``PersonaChanged`` surface event dispatch.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from alysis_code.config import AppConfig, ConfigError, set_config_value
from alysis_code.personas import (
    BUILTIN_PERSONAS,
    DEFAULT_PERSONA,
    PERSONA_NAMES,
    clamp_persona_exec_mode,
    get_persona,
    is_persona_name,
    normalize_persona,
    persona_modes_enabled,
    resolve_persona_exec_mode,
    resolve_persona_model_role,
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_builtin_persona_vocabulary() -> None:
    assert PERSONA_NAMES == ("code", "architect", "ask", "debug")
    assert DEFAULT_PERSONA == "code"


def test_code_is_the_no_op_persona() -> None:
    code = BUILTIN_PERSONAS["code"]
    assert code.default_exec_mode == ""
    assert code.model_role == "coding"
    assert code.overlay_prompt == ""


def test_builtin_persona_defaults() -> None:
    architect = BUILTIN_PERSONAS["architect"]
    assert architect.default_exec_mode == "review"
    assert architect.model_role == "planner"
    assert architect.allow_write_globs == ("*.md", "**/*.md")
    assert BUILTIN_PERSONAS["ask"].default_exec_mode == "readonly"
    assert BUILTIN_PERSONAS["ask"].model_role == "comprehension"
    assert BUILTIN_PERSONAS["debug"].default_exec_mode == ""
    assert BUILTIN_PERSONAS["debug"].model_role == "coding"
    # Only architect scopes writes; everyone else has no scope of their own.
    assert BUILTIN_PERSONAS["code"].allow_write_globs == ()
    assert BUILTIN_PERSONAS["ask"].allow_write_globs == ()
    assert BUILTIN_PERSONAS["debug"].allow_write_globs == ()


def test_personas_never_default_to_an_unguarded_mode() -> None:
    # The clamp rule lowers, never raises — but defaults still must never name
    # auto or fullaccess: architect needs review so its markdown write scope
    # always binds (fullaccess would bypass allow_write_globs entirely).
    for definition in BUILTIN_PERSONAS.values():
        assert definition.default_exec_mode in {"", "readonly", "review"}


def test_normalize_persona_is_lenient_and_fails_closed_to_code() -> None:
    assert normalize_persona("Architect ") == "architect"
    assert normalize_persona("ASK") == "ask"
    assert normalize_persona("bogus") == "code"
    assert normalize_persona(None) == "code"
    assert normalize_persona("") == "code"


def test_is_persona_name() -> None:
    assert is_persona_name("debug")
    assert is_persona_name(" Code ")
    assert not is_persona_name("orchestrator")
    assert not is_persona_name(None)


def test_get_persona_falls_back_to_code() -> None:
    assert get_persona("unknown").name == "code"
    assert get_persona("architect").name == "architect"


# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------


def test_persona_modes_enabled_defaults_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    assert persona_modes_enabled(AppConfig()) is True
    assert persona_modes_enabled(None) is True


def test_persona_modes_enabled_config_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    cfg = AppConfig()
    cfg.persona_modes_enabled = False
    assert persona_modes_enabled(cfg) is False


@pytest.mark.parametrize(
    "env_value,expected", [("off", False), ("0", False), ("on", True), ("1", True)]
)
def test_persona_modes_env_wins_over_config(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: bool
) -> None:
    cfg = AppConfig()
    cfg.persona_modes_enabled = not expected
    monkeypatch.setenv("ALYSIS_PERSONA_MODES", env_value)
    assert persona_modes_enabled(cfg) is expected


# ---------------------------------------------------------------------------
# Config keys
# ---------------------------------------------------------------------------


def test_set_default_persona_validation() -> None:
    cfg = AppConfig()
    assert cfg.default_persona == "code"
    for name in ("code", "architect", "ask", "debug"):
        cfg = set_config_value(cfg, "default_persona", name)
        assert cfg.default_persona == name
    cfg = set_config_value(cfg, "default_persona", " Ask ")
    assert cfg.default_persona == "ask"
    with pytest.raises(ConfigError):
        set_config_value(cfg, "default_persona", "orchestrator")


def test_set_persona_modes_enabled_validation() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "persona_modes_enabled", "false")
    assert cfg.persona_modes_enabled is False
    cfg = set_config_value(cfg, "persona_modes_enabled", "on")
    assert cfg.persona_modes_enabled is True
    with pytest.raises(ConfigError):
        set_config_value(cfg, "persona_modes_enabled", "maybe")


def test_persona_models_dotted_key_round_trip() -> None:
    cfg = AppConfig()
    cfg = set_config_value(cfg, "persona_models.architect", "planner")
    cfg = set_config_value(cfg, "persona_models.debug", "review")
    assert cfg.extra_fields["persona_models"] == {"architect": "planner", "debug": "review"}
    # Empty value removes the entry; removing the last entry drops the map.
    cfg = set_config_value(cfg, "persona_models.debug", "")
    assert cfg.extra_fields["persona_models"] == {"architect": "planner"}
    cfg = set_config_value(cfg, "persona_models.architect", "")
    assert "persona_models" not in cfg.extra_fields


def test_persona_models_rejects_unknown_persona_and_role() -> None:
    cfg = AppConfig()
    with pytest.raises(ConfigError):
        set_config_value(cfg, "persona_models.orchestrator", "planner")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "persona_models.architect", "wizard")
    with pytest.raises(ConfigError):
        set_config_value(cfg, "persona_models.", "planner")


def test_unknown_key_error_mentions_persona_models_namespace() -> None:
    cfg = AppConfig()
    with pytest.raises(ConfigError) as excinfo:
        set_config_value(cfg, "persona_mode", "code")
    assert "persona_models.<persona>" in str(excinfo.value)


def test_resolve_persona_model_role_precedence() -> None:
    cfg = AppConfig()
    assert resolve_persona_model_role(cfg, "architect") == "planner"
    cfg = set_config_value(cfg, "persona_models.architect", "review")
    assert resolve_persona_model_role(cfg, "architect") == "review"
    # Unknown personas normalize to Code before lookup.
    assert resolve_persona_model_role(cfg, "bogus") == "coding"
    assert resolve_persona_model_role(None, "ask") == "comprehension"


# ---------------------------------------------------------------------------
# Environment-context line
# ---------------------------------------------------------------------------


def _env_context(**overrides: object) -> str:
    from alysis_code.agent.prompt_context import _environment_context_message

    kwargs: dict[str, object] = dict(
        mode="review",
        yes=False,
        non_interactive=False,
        deny_write_prefixes=[],
        allow_write_globs=None,
        verification_enabled=False,
        recommended_verification_commands=None,
        authoritative_verification_commands=None,
        verification_selection_source=None,
        verification_selection_reason=None,
        verification_contract_type=None,
        verification_authoritative=False,
        one_shot_execution=False,
    )
    kwargs.update(overrides)
    return _environment_context_message(**kwargs)  # type: ignore[arg-type]


def test_environment_context_omits_persona_line_for_code_and_empty() -> None:
    assert "persona:" not in _env_context()
    assert "persona:" not in _env_context(persona="code")


def test_environment_context_includes_non_default_persona_after_mode() -> None:
    content = _env_context(persona="architect")
    lines = content.splitlines()
    assert "persona: architect" in lines
    assert lines.index("persona: architect") == lines.index("mode: review") + 1


def test_environment_context_keeps_user_and_persona_scopes_separate() -> None:
    content = _env_context(
        allow_write_globs=["src/**"],
        persona_allow_write_globs=["**/*.md"],
    )

    assert 'allow_write_globs: ["src/**"]' in content
    assert 'persona_allow_write_globs: ["**/*.md"]' in content


def test_refresh_updates_persona_line_from_session_state() -> None:
    from alysis_code.agent.prompt_context import (
        refresh_session_environment_context_message,
    )

    class _FakeSession:
        mode = "review"
        persona = "ask"
        yes = False
        non_interactive = False
        one_shot_execution = False
        verification_enabled = False
        messages = [
            {
                "role": "user",
                "content": "<environment_context>\nmode: review\n</environment_context>\n",
            },
        ]

    session = _FakeSession()
    assert refresh_session_environment_context_message(session) is True
    refreshed = session.messages[0]["content"]
    assert "persona: ask" in refreshed
    session.persona = "code"
    assert refresh_session_environment_context_message(session) is True
    assert "persona:" not in session.messages[0]["content"]


# ---------------------------------------------------------------------------
# Session default and surface event
# ---------------------------------------------------------------------------


def test_agent_session_persona_field_defaults_to_code() -> None:
    from alysis_code.agent.session import AgentSession

    fields = {f.name: f for f in dataclasses.fields(AgentSession)}
    assert fields["persona"].default == "code"


def test_persona_changed_event_shape_and_registry() -> None:
    from alysis_code.surface.events import EVENT_REGISTRY, PersonaChanged

    event = PersonaChanged(persona="ask", effective_mode="readonly", source="model")
    assert event.type == "persona_changed"
    payload = event.to_dict()
    assert payload["persona"] == "ask"
    assert payload["effective_mode"] == "readonly"
    assert payload["source"] == "model"
    assert EVENT_REGISTRY["persona_changed"] is PersonaChanged


def test_persona_changed_dispatches_through_surface_emit() -> None:
    from alysis_code.surface.base import Surface
    from alysis_code.surface.events import PersonaChanged

    recorded: list[tuple[str, str, str]] = []

    class _Recorder(Surface):
        def emit_persona_changed(
            self, persona: str, effective_mode: str, source: str = "user"
        ) -> None:
            recorded.append((persona, effective_mode, source))

    surface = _Recorder()
    surface.emit(PersonaChanged(persona="ask", effective_mode="readonly", source="model"))
    assert recorded == [("ask", "readonly", "model")]


# ---------------------------------------------------------------------------
# Clamp rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "persona_default,base,expected",
    [
        ("", "review", "review"),
        ("", "readonly", "readonly"),
        ("", "fullaccess", "fullaccess"),
        ("readonly", "review", "readonly"),
        ("readonly", "auto", "readonly"),
        ("readonly", "readonly", "readonly"),
        ("fullaccess", "review", "review"),
        ("auto", "review", "review"),
        ("bogus", "auto", "auto"),
        ("readonly", "bogus", "readonly"),
        ("", "", "review"),
    ],
)
def test_clamp_persona_exec_mode(persona_default: str, base: str, expected: str) -> None:
    assert clamp_persona_exec_mode(persona_default, base) == expected


def test_scoped_persona_cannot_leave_scope_unenforced_in_fullaccess() -> None:
    from alysis_code.personas import PersonaDefinition

    definition = PersonaDefinition(
        name="docs-writer",
        description="Documentation writer",
        default_exec_mode="",
        model_role="coding",
        allow_write_globs=("docs/**",),
    )

    assert resolve_persona_exec_mode(definition, "fullaccess") == "review"
    assert resolve_persona_exec_mode(definition, "auto") == "auto"


# ---------------------------------------------------------------------------
# _apply_chat_persona (chat-loop primitive)
# ---------------------------------------------------------------------------


class _RecorderSurface:
    def __init__(self) -> None:
        self.persona_events: list[tuple[str, str, str]] = []

    def emit_persona_changed(self, persona: str, effective_mode: str, source: str = "user") -> None:
        self.persona_events.append((persona, effective_mode, source))


class _RecorderStore:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object]]] = []

    def append(self, name: str, payload: dict[str, object]) -> None:
        self.events.append((name, payload))


class _FakeChatSession:
    def __init__(self, *, mode: str = "review", persona: str = "code") -> None:
        self.cfg = AppConfig()
        self.mode = mode
        self.persona = persona
        self.persona_restore_mode: str | None = None
        self.persona_restore_write_globs: list[str] | None = None
        self.allow_write_globs: list[str] | None = None
        self.persona_allow_write_globs: list[str] | None = None
        self.persona_registry: dict[str, object] | None = None
        self.surface = _RecorderSurface()
        self.store = _RecorderStore()
        self.messages: list[dict[str, object]] = []


@pytest.fixture()
def loop_mod(monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    from alysis_code.cli_impl.chat import loop as loop_module

    applied: list[str] = []

    def _fake_apply_mode(*, session, next_mode, persist_default_mode):  # type: ignore[no-untyped-def]
        assert persist_default_mode is False
        session.mode = next_mode
        applied.append(next_mode)

    monkeypatch.setattr(loop_module, "_apply_chat_effective_mode", _fake_apply_mode)
    loop_module._test_applied_modes = applied  # type: ignore[attr-defined]
    try:
        yield loop_module
    finally:
        del loop_module._test_applied_modes  # type: ignore[attr-defined]


def test_apply_persona_architect_scopes_writes_to_markdown(loop_mod) -> None:  # type: ignore[no-untyped-def]
    session = _FakeChatSession(mode="review")
    effective = loop_mod._apply_chat_persona(session=session, persona="architect")
    assert effective == "review"
    assert session.mode == "review"
    assert session.persona == "architect"
    assert session.persona_restore_mode == "review"
    assert session.allow_write_globs is None
    assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
    assert session.persona_restore_write_globs is None
    # Scope changed with mode unchanged: the rebuild path must still run so
    # the tool surface picks up the narrowed write scope.
    assert loop_mod._test_applied_modes == ["review"]
    assert session.surface.persona_events == [("architect", "review", "user")]
    assert session.store.events == [
        (
            "persona_switch_applied",
            {
                "persona": "architect",
                "effective_mode": "review",
                "source": "user",
                "model": "",
                "prompt_guidance_profile": None,
            },
        )
    ]


def test_apply_persona_architect_keeps_user_and_persona_scopes_independent(loop_mod) -> None:  # type: ignore[no-untyped-def]
    session = _FakeChatSession(mode="review")
    session.allow_write_globs = ["src/**"]
    loop_mod._apply_chat_persona(session=session, persona="architect")
    # Neither scope replaces the other. The tool gate requires both.
    assert session.allow_write_globs == ["src/**"]
    assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
    assert session.persona_restore_write_globs == ["src/**"]


def test_apply_persona_narrowing_chain_keeps_base(loop_mod) -> None:  # type: ignore[no-untyped-def]
    session = _FakeChatSession(mode="auto")
    loop_mod._apply_chat_persona(session=session, persona="architect")
    assert session.mode == "review"
    assert session.allow_write_globs is None
    assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
    effective = loop_mod._apply_chat_persona(session=session, persona="ask", source="model")
    assert effective == "readonly"
    assert session.persona == "ask"
    assert session.persona_restore_mode == "auto"
    # Ask brings no scope of its own; the base (unrestricted) applies while
    # ask is active — readonly removes the write tools anyway.
    assert session.allow_write_globs is None
    assert session.persona_allow_write_globs is None
    assert session.surface.persona_events[-1] == ("ask", "readonly", "model")


def test_apply_persona_back_to_code_restores_base(loop_mod) -> None:  # type: ignore[no-untyped-def]
    session = _FakeChatSession(mode="auto")
    loop_mod._apply_chat_persona(session=session, persona="architect")
    effective = loop_mod._apply_chat_persona(session=session, persona="code")
    assert effective == "auto"
    assert session.mode == "auto"
    assert session.persona == "code"
    assert session.persona_restore_mode is None
    assert session.persona_restore_write_globs is None
    assert session.allow_write_globs is None
    assert session.persona_allow_write_globs is None
    assert loop_mod._test_applied_modes == ["review", "auto"]


def test_apply_persona_never_raises_a_readonly_session(loop_mod) -> None:  # type: ignore[no-untyped-def]
    session = _FakeChatSession(mode="readonly")
    loop_mod._apply_chat_persona(session=session, persona="architect")
    # readonly user: architect's review default clamps DOWN to readonly.
    assert session.mode == "readonly"
    assert session.persona_restore_mode == "readonly"
    effective = loop_mod._apply_chat_persona(session=session, persona="code")
    # The user chose readonly; code keeps it. The clamp rule is not an escalator.
    assert effective == "readonly"
    assert session.mode == "readonly"


def test_clear_persona_restore_restores_user_scope(loop_mod) -> None:  # type: ignore[no-untyped-def]
    session = _FakeChatSession(mode="review")
    loop_mod._apply_chat_persona(session=session, persona="architect")
    assert session.allow_write_globs is None
    assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
    # Explicit /permissions <exec>: base redefined, persona scope must not leak.
    loop_mod._clear_persona_restore(session)
    assert session.allow_write_globs is None
    assert session.persona_allow_write_globs is None
    assert session.persona_restore_mode is None
    assert session.persona_restore_write_globs is None


def test_apply_persona_round_trip_preserves_fullaccess(loop_mod) -> None:  # type: ignore[no-untyped-def]
    # A fullaccess user cycling through personas: architect clamps DOWN to
    # review (so its markdown write scope binds — fullaccess would bypass it),
    # ask clamps to readonly, and debug/code restore fullaccess. The clamp
    # only ever lowers relative to the user's base — it never blocks the
    # user's own choice from coming back.
    session = _FakeChatSession(mode="fullaccess")
    effective = loop_mod._apply_chat_persona(session=session, persona="architect")
    assert effective == "review"
    assert session.persona_restore_mode == "fullaccess"
    assert session.allow_write_globs is None
    assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
    effective = loop_mod._apply_chat_persona(session=session, persona="ask")
    assert effective == "readonly"
    assert session.persona_restore_mode == "fullaccess"
    effective = loop_mod._apply_chat_persona(session=session, persona="debug")
    assert effective == "fullaccess"
    assert session.mode == "fullaccess"
    assert session.persona_restore_mode is None
    assert session.allow_write_globs is None
    assert session.persona_allow_write_globs is None
    assert loop_mod._test_applied_modes == ["review", "readonly", "fullaccess"]


def test_startup_persona_applies_only_non_default(
    loop_mod, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    calls: list[tuple[str, str]] = []

    def _fake_apply_persona(*, session, persona, source="user"):  # type: ignore[no-untyped-def]
        calls.append((persona, source))
        return "readonly"

    monkeypatch.setattr(loop_mod, "_apply_chat_persona", _fake_apply_persona)

    session = _FakeChatSession(persona="architect")
    loop_mod._apply_startup_persona(session=session)
    assert calls == [("architect", "config")]

    calls.clear()
    loop_mod._apply_startup_persona(session=_FakeChatSession(persona="code"))
    assert calls == []

    disabled = _FakeChatSession(persona="architect")
    disabled.cfg.persona_modes_enabled = False
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    loop_mod._apply_startup_persona(session=disabled)
    assert calls == []


# ---------------------------------------------------------------------------
# /permissions command handling
# ---------------------------------------------------------------------------


def _run_chat_command(
    input_text: str,
    session: _FakeChatSession,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[str, str]:
    import io

    from rich.console import Console

    from alysis_code.cli_impl import chat as chat_facade
    from alysis_code.cli_impl.chat.state import _ForgeChatState

    persona_calls: list[str] = []

    def _fake_apply_persona(*, session, persona, source="user"):  # type: ignore[no-untyped-def]
        persona_calls.append(persona)
        session.persona = persona
        return "readonly"

    def _fake_apply_mode(*, session, next_mode, persist_default_mode):  # type: ignore[no-untyped-def]
        session.mode = next_mode

    # Patch at the facade: its wrapper syncs these globals into the command
    # module on every dispatch, and the sync machinery preserves explicit
    # overrides (the documented compatibility-surface behavior).
    monkeypatch.setattr(chat_facade, "_apply_chat_persona", _fake_apply_persona, raising=False)
    monkeypatch.setattr(chat_facade, "_apply_chat_effective_mode", _fake_apply_mode, raising=False)

    from alysis_code import cli as cli_mod

    chat_facade._sync_cli_globals(cli_mod)

    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=200)
    result = chat_facade._handle_chat_command(
        input_text=input_text.strip(),
        root=Path("."),
        session=session,
        pending_images=[],
        console=console,
        forge_state=_ForgeChatState(),
    )
    assert result == "handled"
    session._persona_calls = persona_calls  # type: ignore[attr-defined]
    return buffer.getvalue(), str(session.persona)


def test_persona_command_switches_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    session = _FakeChatSession()
    output, persona = _run_chat_command("/persona architect", session, monkeypatch)
    assert persona == "architect"
    assert "Persona set for this session: architect" in output
    assert session._persona_calls == ["architect"]  # type: ignore[attr-defined]


def test_removed_plan_mode_command_never_touches_persona_or_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Chat plan mode is gone entirely (the architect persona is the planning
    # posture). Its old entry command is unknown in chat and must not switch
    # the persona or the execution mode.
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    session = _FakeChatSession()
    output, persona = _run_chat_command("/plan mode", session, monkeypatch)
    assert persona == "code"
    assert session.mode == "review"
    assert "Unknown command: /plan" in output
    assert "Plan Mode" not in output
    assert session._persona_calls == []  # type: ignore[attr-defined]


def test_persona_command_disabled_by_kill_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    session = _FakeChatSession()
    session.cfg.persona_modes_enabled = False
    output, persona = _run_chat_command("/persona ask", session, monkeypatch)
    assert persona == "code"
    assert "Persona modes are disabled" in output
    assert session._persona_calls == []  # type: ignore[attr-defined]


def test_persona_command_invalid_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    session = _FakeChatSession()
    output, persona = _run_chat_command("/persona wizard", session, monkeypatch)
    assert persona == "code"
    assert "Invalid persona" in output


def test_mode_command_points_persona_args_at_persona_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    session = _FakeChatSession()
    output, persona = _run_chat_command("/permissions architect", session, monkeypatch)
    assert persona == "code"
    assert "Personas have their own command: /persona architect" in output
    assert session._persona_calls == []  # type: ignore[attr-defined]


def test_permissions_command_defers_base_scope_change_until_activation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = _FakeChatSession()
    session.persona_restore_mode = "review"
    session.persona_restore_write_globs = None
    session.allow_write_globs = ["*.md", "**/*.md"]
    output, _persona = _run_chat_command("/permissions auto", session, monkeypatch)
    assert session.mode == "review"
    assert session.pending_permissions_mode == "auto"
    assert session.persona_restore_mode == "review"
    assert session.allow_write_globs == ["*.md", "**/*.md"]
    assert "Permissions for the next message" in output


def test_next_persona_cycle() -> None:
    from alysis_code.personas import next_persona

    assert next_persona("code") == "architect"
    assert next_persona("architect") == "ask"
    assert next_persona("ask") == "debug"
    assert next_persona("debug") == "code"
    # Unknown input starts the cycle from Code.
    assert next_persona("bogus") == "architect"
    assert next_persona(None) == "architect"


def test_mode_picker_rows_contain_no_personas() -> None:
    # Personas moved out of the /permissions picker (Tab cycles them in the TUI);
    # the picker surface is execution modes only.
    from alysis_code.cli_impl.commands.chat_terminal import _chat_mode_rows

    values = [value for value, _label, _desc in _chat_mode_rows()]
    assert values == ["review", "auto", "readonly", "fullaccess"]


# ---------------------------------------------------------------------------
# switch_mode tool (PR C)
# ---------------------------------------------------------------------------


def _interactive_session(tmp_path: Path, **kwargs: object):  # type: ignore[no-untyped-def]
    from alysis_code.agent_loop import create_session

    cfg = AppConfig(model="test-model")
    return create_session(
        cfg=cfg,
        root=tmp_path,
        mode=str(kwargs.pop("mode", "review")),
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
        **kwargs,
    )


def test_switch_mode_registered_in_interactive_chat(tmp_path: Path) -> None:
    session = _interactive_session(tmp_path)
    try:
        assert "switch_mode" in session.tools
        assert session.persona_switch_state is not None
    finally:
        session.close()


def test_switch_mode_survives_readonly_mode(tmp_path: Path) -> None:
    # ask/architect run readonly; the tool must stay so the model can propose
    # switching back to code.
    session = _interactive_session(tmp_path, mode="readonly")
    try:
        assert "switch_mode" in session.tools
    finally:
        session.close()


def test_switch_mode_absent_when_kill_switch_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_PERSONA_MODES", "off")
    session = _interactive_session(tmp_path)
    try:
        assert "switch_mode" not in session.tools
        assert session.persona_switch_state is None
    finally:
        session.close()


def test_switch_mode_approval_decline_and_dedupe(tmp_path: Path) -> None:
    from alysis_code.surface.types import ApprovalDecision

    session = _interactive_session(tmp_path)
    try:
        prompts: list[str] = []
        decisions: list[bool] = [False, True]

        def _fake_request_approval(request):  # type: ignore[no-untyped-def]
            prompts.append(request.kind)
            return ApprovalDecision(allow=decisions.pop(0))

        session.surface.request_approval = _fake_request_approval  # type: ignore[attr-defined]
        tool = session.tools["switch_mode"]

        result = tool.run({"persona": "ask", "reason": "pure explanation"})
        assert result["declined"] is True
        assert session.persona_switch_state.last_declined == "ask"
        assert prompts == ["persona_switch"]

        # Identical consecutive proposal: auto-declined without a new prompt.
        result = tool.run({"persona": "ask", "reason": "still explanation"})
        assert result["declined"] is True
        assert prompts == ["persona_switch"]

        # A different persona prompts again; approval parks the switch.
        result = tool.run({"persona": "architect", "reason": "plan first"})
        assert result["scheduled"] is True
        assert prompts == ["persona_switch", "persona_switch"]
        assert session.persona_switch_state.pending == ("architect", "plan first")
        assert session.persona_switch_state.last_declined is None

        # Unknown persona is a tool-level error, no prompt.
        result = tool.run({"persona": "orchestrator", "reason": "x"})
        assert result["ok"] is False
        assert prompts == ["persona_switch", "persona_switch"]
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Overlays and /chat retirement (PR D)
# ---------------------------------------------------------------------------


def test_persona_overlays_empty_for_code_and_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code.personas import persona_overlay_messages

    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    cfg = AppConfig()
    assert persona_overlay_messages(cfg=cfg, persona="code") == []
    assert persona_overlay_messages(cfg=cfg, persona="unknown") == []
    cfg.persona_modes_enabled = False
    assert persona_overlay_messages(cfg=cfg, persona="architect") == []


def test_persona_overlays_present_and_gate_deferring(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from alysis_code.personas import persona_overlay_messages

    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    cfg = AppConfig()
    for persona in ("architect", "ask", "debug"):
        messages = persona_overlay_messages(cfg=cfg, persona=persona)
        assert len(messages) == 1
        # Every overlay defers enforcement to the host — the overlay is a
        # convention, never the guard.
        assert "host" in messages[0].lower()
    assert "read-only" in persona_overlay_messages(cfg=cfg, persona="ask")[0]
    assert "reproduce" in persona_overlay_messages(cfg=cfg, persona="debug")[0].lower()


def test_chat_command_retired_with_pointer(monkeypatch: pytest.MonkeyPatch) -> None:
    import io

    from rich.console import Console

    from alysis_code import cli as cli_mod
    from alysis_code.cli_impl import chat as chat_facade
    from alysis_code.cli_impl.chat.state import _ForgeChatState

    chat_facade._sync_cli_globals(cli_mod)
    buffer = io.StringIO()
    console = Console(file=buffer, force_terminal=False, width=200)
    result = chat_facade._handle_chat_command(
        input_text="/chat hello",
        root=Path("."),
        session=_FakeChatSession(),
        pending_images=[],
        console=console,
        forge_state=_ForgeChatState(),
    )
    assert result == "handled"
    output = buffer.getvalue()
    assert "/chat is retired" in output
    assert "/persona ask" in output


def test_chat_removed_from_visible_surfaces() -> None:
    from alysis_code.cli_impl.chat_slash_completer import get_chat_specs
    from alysis_code.cli_impl.commands.cli_common import (
        _CHAT_GLOBAL_VISIBLE_COMMANDS,
        _CHAT_RETIRED_COMMANDS,
    )

    assert "/chat" not in _CHAT_GLOBAL_VISIBLE_COMMANDS
    assert "/chat" in _CHAT_RETIRED_COMMANDS
    assert "chat" not in [spec.name for spec in get_chat_specs()]


# ---------------------------------------------------------------------------
# IDE bridge degradation and switch_mode chain (P3)
# ---------------------------------------------------------------------------


def test_ide_bridge_rejects_persona_names_as_modes() -> None:
    # The IDE bridge speaks execution modes only; persona vocabulary must be
    # rejected cleanly (ProtocolError), never half-applied.
    from alysis_code.ide.health import SUPPORTED_MODES, capabilities_payload
    from alysis_code.ide.stdio_bridge import ProtocolError, _mode_param

    assert set(SUPPORTED_MODES) == {"readonly", "review", "auto", "fullaccess"}
    assert not set(PERSONA_NAMES) & set(SUPPORTED_MODES)
    assert set(capabilities_payload()["modes"]) == set(SUPPORTED_MODES)
    for persona in PERSONA_NAMES:
        if persona in SUPPORTED_MODES:
            continue
        with pytest.raises(ProtocolError):
            _mode_param({"mode": persona}, request_id=1)


def test_switch_mode_approval_to_application_chain(tmp_path: Path) -> None:
    # The full production path: tool proposal -> approval -> parked ->
    # turn-end application -> narrowed tool surface.
    from alysis_code import cli as cli_mod
    from alysis_code.cli_impl import chat as chat_facade
    from alysis_code.surface.types import ApprovalDecision

    chat_facade._sync_cli_globals(cli_mod)
    session = _interactive_session(tmp_path)
    try:
        session.surface.request_approval = lambda request: ApprovalDecision(allow=True)  # type: ignore[attr-defined]
        assert "fs_write" in session.tools
        result = session.tools["switch_mode"].run({"persona": "ask", "reason": "pure explanation"})
        assert result["scheduled"] is True
        assert session.persona == "code"  # nothing applied mid-turn
        chat_facade._apply_pending_persona_switch(session=session)
        assert session.persona == "ask"
        assert session.mode == "readonly"
        assert "fs_write" not in session.tools
        assert session.persona_switch_state.pending is None
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Config surfaces and prompt guidance (P2)
# ---------------------------------------------------------------------------


def test_tui_config_flow_persona_section_round_trip() -> None:
    from alysis_code.cli_impl.tui.config_flow import ConfigFlow

    flow = ConfigFlow(cfg=AppConfig(model="test-model"))
    flow._choose_menu("personas")
    screen = flow.screen()
    assert screen.stage == "personas"
    assert [r.value for r in screen.rows] == ["code", "architect", "ask", "debug"]
    flow._choose_personas("architect")
    assert flow.state.fields["default_persona"] == "architect"
    assert flow._short_personas() == "architect"


def test_system_prompt_persona_section_present_when_enabled(tmp_path: Path) -> None:
    session = _interactive_session(tmp_path)
    try:
        prompt = str(session.messages[0].get("content") or "")
        assert "Persona modes" in prompt
        assert "switch_mode" in prompt
    finally:
        session.close()


def test_system_prompt_persona_section_absent_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALYSIS_PERSONA_MODES", "off")
    session = _interactive_session(tmp_path)
    try:
        prompt = str(session.messages[0].get("content") or "")
        assert "Persona modes" not in prompt
    finally:
        session.close()


def test_chat_status_rows_include_persona(tmp_path: Path) -> None:
    from alysis_code.cli_impl.commands.chat_status import _chat_status_panel_spec

    session = _interactive_session(tmp_path)
    try:
        session.persona = "ask"
        spec = _chat_status_panel_spec(session=session, pending_images=[])
        session_rows = next(rows for title, rows in spec["sections"] if title == "Session")
        rows = {row[0]: row[1] for row in session_rows}
        assert rows.get("persona") == "ask"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Custom personas
# ---------------------------------------------------------------------------


def _write_custom_persona(root: Path, name: str, *, body: str = "Write docs only.") -> Path:
    directory = root / ".alysis_personas"
    directory.mkdir(exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(
        "---\n"
        f"name: {name}\n"
        "description: Documentation writer\n"
        "exec_mode: review\n"
        "model_role: coding\n"
        "allow_write_globs:\n"
        "  - docs/**\n"
        "---\n"
        f"{body}\n",
        encoding="utf-8",
    )
    return path


def test_load_custom_personas_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import alysis_code.personas as personas_mod

    monkeypatch.setattr(personas_mod, "canonical_user_config_dir", lambda: tmp_path / "no-user-dir")
    _write_custom_persona(tmp_path, "docs-writer")
    directory = tmp_path / ".alysis_personas"
    (directory / "broken.md").write_text("no frontmatter here", encoding="utf-8")
    (directory / "shadow.md").write_text(
        "---\nname: architect\n---\nI try to shadow a builtin.\n", encoding="utf-8"
    )
    (directory / "weird.md").write_text(
        "---\nname: weird\nexec_mode: fullaccess\nmodel_role: wizard\n---\nBody.\n",
        encoding="utf-8",
    )

    personas, warnings = personas_mod.load_custom_personas(tmp_path)
    assert set(personas) == {"docs-writer", "weird"}
    docs = personas["docs-writer"]
    assert docs.default_exec_mode == "review"
    assert docs.allow_write_globs == ("docs/**",)
    assert docs.prompt_trust == "untrusted"
    assert docs.source_scope == "project"
    assert docs.overlay_prompt.startswith("The following persona instructions")
    assert "Write docs only." in docs.overlay_prompt
    # fullaccess exec mode fails closed to readonly; unknown role to coding.
    assert personas["weird"].default_exec_mode == "readonly"
    assert personas["weird"].model_role == "coding"
    joined = "\n".join(warnings)
    assert "missing frontmatter" in joined
    assert "shadows a builtin" in joined
    assert "unsupported" in joined


def test_custom_persona_overlay_stays_at_user_priority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import alysis_code.personas as personas_mod

    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    monkeypatch.setattr(personas_mod, "canonical_user_config_dir", lambda: tmp_path / "no-user-dir")
    _write_custom_persona(tmp_path, "docs-writer", body="Treat source as documentation.")
    registry, warnings = personas_mod.load_custom_personas(tmp_path)

    assert warnings == ()
    assert (
        personas_mod.persona_overlay_messages(
            cfg=AppConfig(), persona="docs-writer", registry=registry
        )
        == []
    )
    user_messages = personas_mod.persona_overlay_user_messages(
        cfg=AppConfig(), persona="docs-writer", registry=registry
    )
    assert len(user_messages) == 1
    assert "workspace content, not host instructions" in user_messages[0]
    assert "Treat source as documentation." in user_messages[0]


def test_custom_persona_loader_rejects_symlinked_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import alysis_code.personas as personas_mod

    monkeypatch.setattr(personas_mod, "canonical_user_config_dir", lambda: tmp_path / "no-user-dir")
    directory = tmp_path / ".alysis_personas"
    directory.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text(
        "---\nname: escaped\nexec_mode: review\n---\nOutside body.\n",
        encoding="utf-8",
    )
    try:
        (directory / "escaped.md").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable on this platform")

    registry, warnings = personas_mod.load_custom_personas(tmp_path)

    assert "escaped" not in registry
    assert any("not a regular file" in warning for warning in warnings)


def test_custom_persona_collision_is_visible_and_project_wins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import alysis_code.personas as personas_mod

    project_path = _write_custom_persona(
        tmp_path,
        "docs-writer",
        body="Project-specific instructions.",
    )
    user_root = tmp_path / "user-config"
    user_directory = user_root / "personas"
    user_directory.mkdir(parents=True)
    (user_directory / project_path.name).write_text(
        project_path.read_text(encoding="utf-8").replace(
            "Project-specific instructions.",
            "User-wide instructions.",
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(personas_mod, "canonical_user_config_dir", lambda: user_root)

    registry, warnings = personas_mod.load_custom_personas(tmp_path)

    assert "Project-specific instructions." in registry["docs-writer"].overlay_prompt
    assert "User-wide instructions." not in registry["docs-writer"].overlay_prompt
    assert any(
        "name collision" in warning and "project definition wins" in warning for warning in warnings
    )


def test_one_shot_custom_persona_uses_user_context_and_separate_scope(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from alysis_code import cli as cli_mod
    from alysis_code.cli import app as alysis_app

    _write_custom_persona(tmp_path, "docs-writer", body="Write the requested guide.")
    captured: dict[str, object] = {}

    def fake_run_agent(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--path",
            str(tmp_path),
            "--allow-broad-workspace",
            "--model",
            "test-model",
            "--api-key",
            "key",
            "--persona",
            "docs-writer",
            "write docs/guide.md",
        ],
        env={
            "ALYSIS_CONFIG_DIR": str(tmp_path / "config"),
            "ALYSIS_DATA_DIR": str(tmp_path / "data"),
        },
    )

    assert result.exit_code == 0, result.output
    assert captured["persona_allow_write_globs"] == ["docs/**"]
    assert captured["ephemeral_system_messages"] is None
    user_messages = captured["ephemeral_user_messages"]
    assert isinstance(user_messages, list)
    assert len(user_messages) == 1
    assert "workspace content, not host instructions" in user_messages[0]
    assert "Write the requested guide." in user_messages[0]


def test_one_shot_persona_preserves_zero_role_temperature(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typer.testing import CliRunner

    from alysis_code import cli as cli_mod
    from alysis_code.cli import app as alysis_app

    cfg = AppConfig(model="test-model", coding_temperature=0.4, review_temperature=0.0)
    cfg = set_config_value(cfg, "persona_models.ask", "review")
    captured: dict[str, object] = {}

    monkeypatch.setattr(cli_mod, "load_config", lambda: cfg.model_copy(deep=True))

    def fake_run_agent(**kwargs: object) -> int:
        captured.update(kwargs)
        return 0

    monkeypatch.setattr(cli_mod, "run_agent", fake_run_agent)
    result = CliRunner().invoke(
        alysis_app,
        [
            "run",
            "--path",
            str(tmp_path),
            "--allow-broad-workspace",
            "--api-key",
            "key",
            "--persona",
            "ask",
            "explain the repository",
        ],
        env={
            "ALYSIS_CONFIG_DIR": str(tmp_path / "config"),
            "ALYSIS_DATA_DIR": str(tmp_path / "data"),
        },
    )

    assert result.exit_code == 0, result.output
    effective_cfg = captured["cfg"]
    assert isinstance(effective_cfg, AppConfig)
    assert effective_cfg.coding_temperature == 0.0


def test_custom_persona_applies_through_clamp_and_scope(loop_mod) -> None:  # type: ignore[no-untyped-def]
    from alysis_code.personas import (
        PersonaDefinition,
        next_persona,
        persona_overlay_messages,
        persona_overlay_user_messages,
    )

    definition = PersonaDefinition(
        name="docs-writer",
        description="Documentation writer",
        default_exec_mode="review",
        model_role="coding",
        overlay_prompt="Docs overlay.",
        allow_write_globs=("docs/**",),
    )
    session = _FakeChatSession(mode="auto")
    session.persona_registry = {"docs-writer": definition}
    effective = loop_mod._apply_chat_persona(session=session, persona="docs-writer")
    assert effective == "review"
    assert session.persona == "docs-writer"
    assert session.allow_write_globs is None
    assert session.persona_allow_write_globs == ["docs/**"]
    assert session.persona_restore_mode == "auto"
    # Definitions outside the host-owned builtin registry fail closed to user
    # priority even if a future caller constructs one directly.
    assert (
        persona_overlay_messages(
            cfg=session.cfg, persona="docs-writer", registry=session.persona_registry
        )
        == []
    )
    assert persona_overlay_user_messages(
        cfg=session.cfg, persona="docs-writer", registry=session.persona_registry
    ) == ["Docs overlay."]
    # The Tab cycle includes customs after the builtins.
    assert next_persona("debug", session.persona_registry) == "docs-writer"
    assert next_persona("docs-writer", session.persona_registry) == "code"


def test_create_session_loads_custom_personas(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import alysis_code.personas as personas_mod

    monkeypatch.setattr(personas_mod, "canonical_user_config_dir", lambda: tmp_path / "no-user-dir")
    _write_custom_persona(tmp_path, "docs-writer")
    session = _interactive_session(tmp_path)
    try:
        assert session.persona_registry is not None
        assert "docs-writer" in session.persona_registry
    finally:
        session.close()


def test_persona_command_accepts_custom_persona(monkeypatch: pytest.MonkeyPatch) -> None:
    from alysis_code.personas import PersonaDefinition

    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    session = _FakeChatSession()
    session.persona_registry = {
        "docs-writer": PersonaDefinition(
            name="docs-writer",
            description="Documentation writer",
            default_exec_mode="review",
            model_role="coding",
        )
    }
    output, persona = _run_chat_command("/persona docs-writer", session, monkeypatch)
    assert persona == "docs-writer"
    assert "Persona set for this session: docs-writer" in output


def test_persona_model_swap_skips_default_installs(loop_mod) -> None:  # type: ignore[no-untyped-def]
    from types import SimpleNamespace

    from alysis_code.personas import get_persona

    session = _FakeChatSession(mode="review")
    # No client at all: never swaps.
    assert loop_mod._resolve_persona_model_swap(session, get_persona("architect")) is None
    # Client on the default model with no role config: chain lands on
    # cfg.model for every persona -> no swap.
    session.cfg.model = "test-model"
    session.client = SimpleNamespace(model="test-model")
    assert loop_mod._resolve_persona_model_swap(session, get_persona("architect")) is None
    assert loop_mod._resolve_persona_model_swap(session, get_persona("code")) is None
    # A configured planner role model activates the swap for architect only.
    session.cfg.extra_fields = {"role_models": {"planner": "planner-model"}}
    assert loop_mod._resolve_persona_model_swap(session, get_persona("architect")) == (
        "planner",
        "planner-model",
        0.2,
    )
    assert loop_mod._resolve_persona_model_swap(session, get_persona("code")) is None


def test_sticky_persona_model_swaps_and_restores(tmp_path: Path) -> None:
    from alysis_code import cli as cli_mod
    from alysis_code.agent_loop import create_session
    from alysis_code.cli_impl import chat as chat_facade

    chat_facade._sync_cli_globals(cli_mod)
    cfg = AppConfig(model="test-model")
    cfg = set_config_value(cfg, "role_models.planner", "planner-model")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
    )
    try:
        base_client = session.client
        assert session.router_client is None
        assert str(base_client.model) == "test-model"
        chat_facade._apply_chat_persona(session=session, persona="architect")
        assert str(session.client.model) == "planner-model"
        assert session.client is not base_client
        assert session.router_client is None
        chat_facade._apply_chat_persona(session=session, persona="code")
        # The base client object itself is restored from the cache.
        assert session.client is base_client
        assert session.router_client is None
    finally:
        session.close()


def test_persona_client_cache_keys_same_model_by_role_temperature(tmp_path: Path) -> None:
    from alysis_code import cli as cli_mod
    from alysis_code.agent_loop import create_session
    from alysis_code.cli_impl import chat as chat_facade

    chat_facade._sync_cli_globals(cli_mod)
    cfg = AppConfig(model="test-model", coding_temperature=0.4, review_temperature=0.0)
    cfg = set_config_value(cfg, "persona_models.ask", "review")
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
    )
    try:
        base_client = session.client
        assert session.persona_client_key == ("test-model", 0.4)

        chat_facade._apply_chat_persona(session=session, persona="ask")
        assert session.persona_client_key == ("test-model", 0.0)
        assert session.client is not base_client
        assert float(session.client.temperature) == 0.0
        assert set(session.persona_client_cache or {}) == {
            ("test-model", 0.4),
            ("test-model", 0.0),
        }

        chat_facade._apply_chat_persona(session=session, persona="code")
        assert session.client is base_client
        assert session.persona_client_key == ("test-model", 0.4)
    finally:
        session.close()


def test_strict_metadata_policy_rejects_persona_model_before_state_change(
    tmp_path: Path,
) -> None:
    from alysis_code import cli as cli_mod
    from alysis_code.agent_loop import create_session
    from alysis_code.cli_impl import chat as chat_facade
    from alysis_code.model_metadata_policy import ModelMetadataPolicyError

    chat_facade._sync_cli_globals(cli_mod)
    cfg = AppConfig(model="known-model", model_metadata_policy="strict")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "known-model": {
                    "context_window_tokens": 65536,
                    "max_output_tokens": 4096,
                }
            }
        },
        "role_models": {"planner": "unknown-planner-model"},
    }
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=8,
        no_log=False,
        api_key_override="override-key",
        session_log_dir_override=tmp_path / "sessions",
        verification_enabled=False,
    )
    try:
        original_client = session.client
        original_tools = session.tools

        with pytest.raises(ModelMetadataPolicyError, match="model_metadata_policy=strict"):
            chat_facade._apply_chat_persona(session=session, persona="architect")

        assert session.persona == "code"
        assert session.mode == "review"
        assert session.allow_write_globs is None
        assert session.persona_allow_write_globs is None
        assert session.persona_restore_mode is None
        assert session.client is original_client
        assert session.tools is original_tools
    finally:
        session.close()


def test_resumed_persona_reapplies_from_session_log(tmp_path: Path) -> None:
    # Resume restores the base mode from session_start; the last
    # persona_switch_applied event in the log carries the persona, and
    # re-applying it reproduces the narrowed mode + write scope + restore
    # point exactly as the clamp left them.
    from alysis_code import cli as cli_mod
    from alysis_code.cli_impl import chat as chat_facade
    from alysis_code.cli_impl.chat import loop as loop_module

    chat_facade._sync_cli_globals(cli_mod)
    session = _interactive_session(tmp_path)
    try:
        chat_facade._apply_chat_persona(session=session, persona="ask")
        chat_facade._apply_chat_persona(session=session, persona="architect")
        assert loop_module._load_resumed_persona(session) == "architect"

        # Simulate the post-resume state swap: base restored, persona reset.
        session.persona = "code"
        session.persona_restore_mode = None
        session.persona_restore_write_globs = None
        session.allow_write_globs = None
        session.persona_allow_write_globs = None
        loop_module._reapply_resumed_persona(session=session, console=None)
        assert session.persona == "architect"
        assert session.mode == "review"
        assert session.allow_write_globs is None
        assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
        assert session.persona_restore_mode == "review"
    finally:
        session.close()


def test_load_resumed_persona_none_without_switches(tmp_path: Path) -> None:
    from alysis_code.cli_impl.chat import loop as loop_module

    session = _interactive_session(tmp_path)
    try:
        assert loop_module._load_resumed_persona(session) is None
    finally:
        session.close()


def test_persona_command_ignored_in_forge_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    # Forge chat owns the command surface while active; /persona must not
    # half-apply a persona underneath a forge session.
    monkeypatch.delenv("ALYSIS_PERSONA_MODES", raising=False)
    import io

    from rich.console import Console

    from alysis_code import cli as cli_mod
    from alysis_code.cli_impl import chat as chat_facade
    from alysis_code.cli_impl.chat.state import _ForgeChatState

    chat_facade._sync_cli_globals(cli_mod)
    session = _FakeChatSession()
    forge_state = _ForgeChatState()
    forge_state.ui_mode = "forge"
    console = Console(file=io.StringIO(), force_terminal=False, width=200)
    result = chat_facade._handle_chat_command(
        input_text="/persona ask",
        root=Path("."),
        session=session,
        pending_images=[],
        console=console,
        forge_state=forge_state,
    )
    assert result == "handled"
    assert session.persona == "code"


def test_architect_intersects_user_and_persona_scopes_end_to_end(tmp_path: Path) -> None:
    # Through a real session and tool rebuild, both independently supplied
    # scopes must match. Neither side may replace or widen the other.
    from alysis_code import cli as cli_mod
    from alysis_code.cli_impl import chat as chat_facade
    from alysis_code.surface.types import ApprovalDecision

    chat_facade._sync_cli_globals(cli_mod)
    session = _interactive_session(tmp_path)
    try:
        session.surface.request_approval = lambda request: ApprovalDecision(allow=True)  # type: ignore[attr-defined]
        session.allow_write_globs = ["src/**"]
        (tmp_path / "src").mkdir()
        (tmp_path / "output.txt").write_text("scratch\n", encoding="utf-8")
        effective = chat_facade._apply_chat_persona(session=session, persona="architect")
        assert effective == "review"
        assert session.allow_write_globs == ["src/**"]
        assert session.persona_allow_write_globs == ["*.md", "**/*.md"]
        assert "fs_write" in session.tools

        ok = session.tools["fs_write"].run({"path": "src/plan.md", "content": "# Plan\n"})
        assert (tmp_path / "src" / "plan.md").exists()
        assert not str(ok.get("error") or "")

        from alysis_code.agent.errors import AgentRuntimeError

        with pytest.raises(AgentRuntimeError, match="outside allowed scope"):
            session.tools["fs_write"].run({"path": "plan.md", "content": "# Outside user scope\n"})
        with pytest.raises(AgentRuntimeError, match="outside allowed scope"):
            session.tools["fs_write"].run({"path": "src/hack.py", "content": "x = 1\n"})
        with pytest.raises(AgentRuntimeError, match="persona write scope"):
            session.tools["shell_run"].run({"cmd": "touch src/hack.py"})
        with pytest.raises(AgentRuntimeError, match="outside allowed scope"):
            session.tools["fs_delete"].run({"path": "output.txt"})

        assert not (tmp_path / "plan.md").exists()
        assert not (tmp_path / "src" / "hack.py").exists()
        assert (tmp_path / "output.txt").exists()
    finally:
        session.close()


def test_apply_pending_persona_switch(loop_mod) -> None:  # type: ignore[no-untyped-def]
    from alysis_code.personas import PersonaSwitchState

    session = _FakeChatSession(mode="review")
    session.persona_switch_state = PersonaSwitchState(pending=("ask", "advice"))
    loop_mod._apply_pending_persona_switch(session=session)
    assert session.persona == "ask"
    assert session.mode == "readonly"
    assert session.persona_switch_state.pending is None
    assert session.surface.persona_events == [("ask", "readonly", "model")]

    # No pending: no-op.
    loop_mod._apply_pending_persona_switch(session=session)
    assert session.surface.persona_events == [("ask", "readonly", "model")]
