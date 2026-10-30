"""Phase 1 TUI tests: pure content builders + a headless Application smoke test.

The Application is driven with a prompt_toolkit pipe input and a dummy output so
no real terminal is required (works in CI / on Windows).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from alysis_code.cli_impl.tui import run_tui
from alysis_code.cli_impl.tui.app import _model_access_setup_hint
from alysis_code.cli_impl.tui.config import is_tui_enabled
from alysis_code.cli_impl.tui.content import pretty_model_label
from alysis_code.cli_impl.tui.footer import footer_fragments
from alysis_code.cli_impl.tui.owl import load_owl_animation
from alysis_code.cli_impl.tui.state import TuiState


def _plain(fragments) -> str:
    return "".join(text for _style, text in fragments)


# --------------------------- flag ---------------------------


def test_tui_enabled_by_default(monkeypatch):
    monkeypatch.delenv("ALYSIS_TUI", raising=False)
    assert is_tui_enabled() is True


@pytest.mark.parametrize(
    "value,expected", [("1", True), ("true", True), ("0", False), ("off", False), ("", True)]
)
def test_tui_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("ALYSIS_TUI", value)
    assert is_tui_enabled() is expected


# --------------------------- content ---------------------------


def test_disconnected_landing_uses_one_neutral_model_access_instruction():
    hint = _model_access_setup_hint("openai-codex")

    assert hint == "Set up model access: /login to choose a connection · /config for an API key"
    assert "subscription not connected" not in hint


@pytest.mark.parametrize(
    "model,expected",
    [
        # DeepSeek hyphenates the whole name ("DeepSeek-V4-Flash"); dated beta
        # ids drop their expiry suffix.
        ("deepseek-chat", "DeepSeek-Chat"),
        ("deepseek-flash", "DeepSeek-V4.1-Flash"),
        ("deepseek-v4-pro", "DeepSeek-V4-Pro"),
        ("deepseek-v4-flash-vision-exp", "DeepSeek-V4-Flash-Vision-Exp"),
        ("deepseek-v4.1-flash-expires-on-0910", "DeepSeek-V4.1-Flash"),
        ("deepseek/deepseek-v4-flash-0731", "DeepSeek-V4-Flash-0731"),
        # OpenAI and Zhipu hyphenate the version onto the family name.
        ("gpt-4o", "GPT-4o"),
        ("gpt-6-astra", "GPT-6 Astra"),
        ("openai/gpt-5.6-terra", "GPT-5.6 Terra"),
        ("glm-5.3-flash", "GLM-5.3 Flash"),
        # Other vendors do not, and the version is not glued to a non-family word.
        ("gemini-3.8-flash", "Gemini 3.8 Flash"),
        # Dash-encoded dots (Anthropic, Doubao) become one version number.
        ("claude-fable-5-1", "Claude Fable 5.1"),
        ("claude-opus-4-8", "Claude Opus 4.8"),
        ("doubao-seed-2-1-pro-260628", "Doubao Seed 2.1 Pro 260628"),
        ("kimi-k3", "Kimi K3"),
        ("xiaomi/mimo-v2.5-pro", "MiMo V2.5 Pro"),
        ("", "model"),
        ("openai/gpt-4o-mini", "GPT-4o Mini"),
    ],
)
def test_pretty_model_label(model, expected):
    assert pretty_model_label(model) == expected


# --------------------------- footer ---------------------------


def test_footer_core_fields():
    state = TuiState(model_name="deepseek-chat", username="perdikis", context_pct=100.0)
    text = _plain(footer_fragments(state, width=90))
    assert "alysis" in text
    assert "DeepSeek-Chat" in text
    assert "context: 100% left" in text
    assert "0 processed" in text and "$0.0000" in text
    assert "perdikis" in text
    assert "shift+tab" not in text
    # Omit "(0)", "▶▶", and the Plan/Act toggle from the footer.
    assert "(0)" not in text
    assert "▶▶" not in text
    assert "Plan" not in text and "Act" not in text and "(Tab)" not in text


def test_footer_has_no_separate_sensitive_indicator():
    # The approval policy is the execution mode and nothing else. A second
    # indicator could disagree with the mode badge, which is exactly the
    # "footer says safe while everything auto-approves" bug this removed.
    for mode in ("readonly", "review", "auto", "fullaccess"):
        text = _plain(
            footer_fragments(
                TuiState(model_name="deepseek-chat", exec_mode=mode, username="perdikis"),
                width=100,
            )
        )
        assert "sensitive:" not in text
        assert "auto-approve" not in text


def test_footer_mode_badge_is_the_approval_indicator():
    text = _plain(
        footer_fragments(
            TuiState(model_name="deepseek-chat", exec_mode="auto", username="perdikis"),
            width=100,
        )
    )
    assert "fast" in text
    assert "sensitive:" not in text


def test_footer_distinguishes_active_and_next_message_permissions():
    state = TuiState(
        model_name="deepseek-chat",
        exec_mode="review",
        pending_exec_mode="auto",
    )

    text = _plain(footer_fragments(state, width=100))

    assert "safe→fast next" in text


def test_footer_shows_workspace_and_branch():
    state = TuiState(
        model_name="m",
        username="perdikis",
        workspace="~/coder-plugin-install",
        branch="feat/tui-rebuild",
    )
    text = _plain(footer_fragments(state, width=120))
    assert "perdikis" in text
    assert "~/coder-plugin-install" in text
    assert "feat/tui-rebuild" in text


def test_footer_context_indicator_value():
    text = _plain(
        footer_fragments(TuiState(model_name="m", username="u", context_pct=42.0), width=90)
    )
    assert "context: 42% left" in text


def test_footer_context_unmeasured_reads_na_not_fabricated_100():
    # Default state has no measured value yet: the footer must say n/a rather
    # than a fabricated "100% left" (which would mislead if the compute failed).
    text = _plain(footer_fragments(TuiState(model_name="m", username="u"), width=90))
    assert "context: n/a" in text
    assert "% left" not in text.split("\n")[0]


def test_footer_context_never_rounds_up_to_full_or_down_to_empty():
    # A partially-used window reads 1–99, never a misleading 100 or premature 0.
    near_full = _plain(footer_fragments(TuiState(model_name="m", context_pct=99.6), width=90))
    assert "context: 99% left" in near_full
    near_empty = _plain(footer_fragments(TuiState(model_name="m", context_pct=0.4), width=90))
    assert "context: 1% left" in near_empty


def test_footer_context_metric_uses_provider_input_including_startup_and_cache():
    from alysis_code.cli_impl.commands.startup import _chat_context_percent_value
    from alysis_code.llm.types import LLMUsage
    from alysis_code.provider_usage import capture_provider_usage

    client = SimpleNamespace(model="model")
    measured = SimpleNamespace(
        client=client,
        last_provider_usage=capture_provider_usage(
            LLMUsage(8000, 500, 8500, cached_prompt_tokens=6000), client=client
        ),
        _hud_context_cache=SimpleNamespace(
            context_window_tokens=10000,
            dynamic_context_percent_left=100.0,
            effective_percent_left=99.2,
        ),
    )
    assert _chat_context_percent_value(measured) == 20.0
    # New local content and budget changes do not claim a fresh measurement.
    measured._hud_context_cache.dynamic_context_percent_left = 0.0
    assert _chat_context_percent_value(measured) == 20.0
    measured.last_provider_usage["input_tokens"] = 0
    assert _chat_context_percent_value(measured) == 100.0
    measured.last_provider_usage["input_scope"] = "interaction"
    assert _chat_context_percent_value(measured) is None
    measured.last_provider_usage["input_scope"] = "request"
    client.model = "changed"
    assert _chat_context_percent_value(measured) is None
    assert _chat_context_percent_value(SimpleNamespace(_hud_context_cache=None)) is None


def test_footer_usage_hud_off_hides_usage_metrics():
    text = _plain(
        footer_fragments(
            TuiState(
                model_name="deepseek-chat",
                username="perdikis",
                usage_hud_enabled=False,
                context_pct=42.0,
                tokens=1234,
                cost_usd=0.25,
            ),
            width=90,
        )
    )
    line1 = text.split("\n")[0]
    assert "DeepSeek-Chat" in line1
    assert "context" not in line1
    assert "tokens" not in line1
    assert "$" not in line1


def test_tui_session_state_sync_updates_local_command_footer_state():
    from alysis_code.cli_impl.chat.loop import _sync_tui_session_state

    state = TuiState(
        model_name="m",
        username="u",
        exec_mode="review",
        usage_hud_enabled=True,
    )
    session = SimpleNamespace(
        mode="auto",
        cfg=SimpleNamespace(),
        _usage_hud_enabled=False,
    )

    _sync_tui_session_state(state, session, include_exec_mode=True)

    assert state.exec_mode == "auto"
    assert state.usage_hud_enabled is False


def test_footer_forge_badge_hidden_by_default():
    text = _plain(footer_fragments(TuiState(model_name="m", username="u"), width=120))
    assert "FORGE" not in text


def test_footer_forge_badge_shown_when_active():
    state = TuiState(
        model_name="m",
        username="perdikis",
        exec_mode="review",
        forge_mode=True,
        forge_run_id="run-1a2b",
    )
    text = _plain(footer_fragments(state, width=120))
    assert "FORGE" in text
    assert "⚒" not in text  # no wide emoji (it threw off the width math)
    assert "run-1a2b" in text
    # The execution-mode badge still renders alongside it (with a separator).
    assert "safe" in text
    # Order: FORGE chip precedes the exec-mode badge on line 2.
    line2 = text.split("\n")[1]
    assert line2.index("FORGE") < line2.index("safe")


def test_footer_is_two_lines_and_right_aligned():
    state = TuiState(model_name="m", username="u", tokens=1234)
    lines = _plain(footer_fragments(state, width=100)).split("\n")
    assert len(lines) == 2
    assert lines[0].rstrip().endswith("$0.0000")  # cost right-aligned, line 1
    assert lines[0].startswith("◇ alysis")  # brand mark + wordmark, line 1 left
    assert "1,234 processed" in lines[0]  # comma-grouped cumulative usage
    assert lines[1].startswith("u")  # username remains on line 2
    # Line 2 has no right half — the mode badge on the left is the sole
    # approval indicator, so nothing is right-aligned here.
    assert lines[1].rstrip().endswith("u")


def test_footer_never_overflows_width():
    state = TuiState(
        model_name="some-very-long-model-name",
        username="averylongusername",
        workspace="~/a/very/long/workspace/path/that/keeps/going/and/going",
        branch="feature/a-really-quite-long-branch-name-here",
    )
    for width in (40, 60, 80, 120):
        lines = _plain(footer_fragments(state, width=width)).split("\n")
        assert len(lines) == 2
        for line in lines:
            assert len(line) <= width


# --------------------------- owl ---------------------------


def test_owl_frames_load():
    owl = load_owl_animation(color_enabled=False)
    # The repo ships 21 frames; loading must succeed and advancing must cycle.
    assert owl.available is True
    assert owl.frame_count >= 1
    first = owl.current_ansi()
    owl.advance()
    assert owl.current_ansi() is not None
    assert first is not None and first.value  # non-empty ASCII art


def test_neutral_owl_uses_the_terminal_foreground(monkeypatch) -> None:
    from alysis_code.cli_impl.commands import welcome as welcome_mod
    from alysis_code.cli_impl.tui import owl as owl_mod

    monkeypatch.setattr(welcome_mod, "_detect_owl_theme", lambda _stream: "neutral")

    frames = owl_mod._load_frames(color_enabled=True, stream=None)
    output = "\n".join(line for frame in frames for line in frame)

    assert "\x1b[" not in output
    assert "█" in output


def test_explicit_owl_theme_stays_in_sync_with_the_tui(monkeypatch) -> None:
    from alysis_code.cli_impl.commands import welcome as welcome_mod
    from alysis_code.cli_impl.tui import owl as owl_mod

    def _unexpected_detection(_stream):
        raise AssertionError("the owl must reuse the TUI's resolved theme")

    monkeypatch.setattr(welcome_mod, "_detect_owl_theme", _unexpected_detection)

    light_frames = owl_mod._load_frames(color_enabled=True, stream=None, theme="light")
    dark_frames = owl_mod._load_frames(color_enabled=True, stream=None, theme="dark")

    assert light_frames
    assert dark_frames


# --------------------------- headless app ---------------------------


def _run_headless(state: TuiState, keys: str, **kwargs):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return run_tui(state, owl_color=False, input=pipe, output=DummyOutput(), **kwargs)


def test_app_exits_on_exit_word():
    state = TuiState(model_name="deepseek-chat", username="t")
    result, transcript = _run_headless(state, "/exit\r")
    assert result == "/exit"
    assert transcript == []


def test_app_records_submission_then_exits():
    state = TuiState(model_name="deepseek-chat", username="t")
    result, transcript = _run_headless(state, "hello there\r/exit\r")
    assert ("user", "hello there") in transcript
    assert result == "/exit"


def test_app_runs_next_message_boundary_before_session_turn():
    state = TuiState(model_name="deepseek-chat", username="t", exec_mode="review")
    observed: list[tuple[str, str, str | None]] = []

    class Session:
        mode = "review"
        pending_permissions_mode = "auto"

        def run_turn(self, text: str, *, cancellation_token=None) -> int:
            _ = cancellation_token
            observed.append((text, self.mode, self.pending_permissions_mode))
            return 0

    session = Session()

    def before_turn(live_session, _run_kwargs):
        live_session.mode = str(live_session.pending_permissions_mode)
        live_session.pending_permissions_mode = None

    result, _transcript = _run_headless(
        state,
        "hello\r/exit\r",
        session_builder=lambda _surface: session,
        before_turn=before_turn,
        background_turns=False,
    )

    assert result == "/exit"
    assert observed == [("hello", "auto", None)]


def test_app_restores_temporary_turn_permissions_after_failure():
    state = TuiState(model_name="deepseek-chat", username="t", exec_mode="review")
    observed: list[tuple[str, str]] = []
    cleanup_calls: list[str] = []

    class Session:
        mode = "review"

        def run_turn(self, text: str, *, cancellation_token=None) -> int:
            _ = cancellation_token
            observed.append((text, self.mode))
            raise RuntimeError("turn failed")

    session = Session()

    def before_turn(live_session, _run_kwargs):
        # Pending Permissions have already selected the new base by this point;
        # the one-turn scope narrows only the turn itself.
        live_session.mode = "fullaccess"
        restore_mode = live_session.mode
        live_session.mode = "readonly"

        def cleanup() -> None:
            cleanup_calls.append(restore_mode)
            live_session.mode = restore_mode

        return cleanup

    result, transcript = _run_headless(
        state,
        "inspect safely\r/exit\r",
        session_builder=lambda _surface: session,
        before_turn=before_turn,
        background_turns=False,
    )

    assert result == "/exit"
    assert observed == [("inspect safely", "readonly")]
    assert cleanup_calls == ["fullaccess"]
    assert session.mode == "fullaccess"
    assert any("turn failed" in text for role, text in transcript if role == "error")


def test_app_without_session_surfaces_model_blocker():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    state = TuiState(
        model_name="gpt-codex-test",
        connection_status="subscription not connected",
    )
    with create_pipe_input() as pipe:
        pipe.send_text("hello there\r/exit\r")
        result, transcript = run_tui(
            state,
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            unavailable_message="Connect the selected subscription before sending a message.",
        )

    assert result == "/exit"
    assert ("user", "hello there") in transcript
    assert (
        "warn",
        "Connect the selected subscription before sending a message.",
    ) in transcript
    assert ("system", "TUI preview - no agent session attached.") not in transcript


def test_app_login_picker_exits_with_selected_connection():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    state = TuiState(connection_status="subscription not connected")
    with create_pipe_input() as pipe:
        pipe.send_text("/login\r2")
        result, transcript = run_tui(
            state,
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            picker_providers={
                "/login": lambda: {
                    "title": "Log in",
                    "rows": [
                        {"label": "Alysis Code", "value": "alysis"},
                        {"label": "ChatGPT Codex", "value": "openai-codex"},
                    ],
                    "on_select": lambda value: {
                        "exit": ("login_connection", value),
                    },
                }
            },
        )

    assert result == ("login_connection", "openai-codex")
    assert transcript == []


def test_app_explicit_login_connection_exits_for_browser_flow():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text("/login alysis\r")
        result, _transcript = run_tui(
            TuiState(),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
        )

    assert result == ("login_connection", "alysis")


def test_app_shift_tab_invokes_mode_cycle_then_exits():
    state = TuiState(model_name="deepseek-chat", username="t", exec_mode="review")
    calls: list[int] = []

    def _cycle() -> list[tuple[str, str]] | None:
        calls.append(1)
        state.pending_exec_mode = "auto"
        return [("system", "Permissions for the next message: fast (auto)")]

    _run_headless(state, "\x1b[Z/exit\r", mode_cycle=_cycle)
    assert calls == [1]
    assert state.exec_mode == "review"
    assert state.pending_exec_mode == "auto"


def test_app_shift_tab_is_inert_without_a_mode_cycle_callback():
    # No callback (Phase 1 shell) must not flip a footer-only field: the mode is
    # owned by the session, so a display-only change would be a lie.
    state = TuiState(model_name="deepseek-chat", username="t", exec_mode="review")
    _run_headless(state, "\x1b[Z/exit\r")
    assert state.exec_mode == "review"


def test_tui_state_has_no_auto_approve_axis():
    # The approval policy lives in exec_mode alone; a second toggle could
    # auto-answer every prompt review mode raised while the badge read "safe".
    state = TuiState(model_name="m")
    assert not hasattr(state, "auto_approve")
    assert not hasattr(state, "toggle_auto_approve")


def test_next_exec_mode_cycles_all_four_modes_and_wraps():
    from alysis_code.cli_impl.tui.state import EXEC_MODE_CYCLE, next_exec_mode

    assert EXEC_MODE_CYCLE == ("readonly", "review", "auto", "fullaccess")
    assert next_exec_mode("readonly") == "review"
    assert next_exec_mode("review") == "auto"
    assert next_exec_mode("auto") == "fullaccess"
    assert next_exec_mode("fullaccess") == "readonly"
    # Unknown/empty starts the cycle rather than raising.
    assert next_exec_mode("") == "readonly"
    assert next_exec_mode("nonsense") == "readonly"
    assert next_exec_mode("  REVIEW  ") == "auto"


# --------------------------- mouse wheel / status line ---------------------------


def test_tui_state_has_no_mouse_mode_toggle():
    state = TuiState(model_name="m")
    assert not hasattr(state, "terminal_selection_mode")
    assert not hasattr(state, "toggle_terminal_selection_mode")


def test_footer_omits_mouse_mode_chip():
    default = _plain(footer_fragments(TuiState(model_name="m", username="u"), width=120))
    line2 = default.split("\n")[1]
    assert line2.rstrip().endswith("u")
    assert "F2" not in line2


def test_footer_omits_repeated_connection_status():
    state = TuiState(
        model_name="gpt-codex-test",
        username="u",
        connection_status="subscription not connected",
    )
    line1 = _plain(footer_fragments(state, width=140)).split("\n")[0]
    assert "subscription not connected" not in line1
    assert "GPT Codex Test" in line1


def test_status_line_is_blank_when_idle_and_shows_interrupt_when_running():
    from alysis_code.cli_impl.tui.app import _status_line_fragments

    assert _plain(_status_line_fragments(running=False)) == ""
    assert "Copied 12 characters" in _plain(
        _status_line_fragments(running=False, notice="Copied 12 characters")
    )
    assert "ctrl+c to copy" in _plain(
        _status_line_fragments(running=False, selection_available=True)
    )
    assert "Esc or Ctrl+C to interrupt" in _plain(_status_line_fragments(running=True))


def test_footer_cost_unknown_shows_na():
    # Unmetered/free model with real usage: cost is None → honest "n/a", never $0.0000.
    state = TuiState(model_name="m", username="u", tokens=5000, cost_usd=None, cost_unknown_calls=3)
    line1 = _plain(footer_fragments(state, width=120)).split("\n")[0]
    assert "n/a" in line1
    assert "$0.0000" not in line1
    assert "+3" in line1  # unmetered-calls flag


def test_footer_cost_known_shows_dollars():
    state = TuiState(model_name="m", username="u", tokens=5000, cost_usd=0.1234, context_pct=100.0)
    line1 = _plain(footer_fragments(state, width=120)).split("\n")[0]
    assert "$0.1234" in line1
    assert "n/a" not in line1


# --------------------------- welcome landing vs startup notices ---------------------------


def test_has_conversation_ignores_startup_notices():
    # Startup notices (the streaming-disabled warning, system/trace lines) must
    # NOT count as a conversation, otherwise they dismiss the owl landing the
    # moment the app opens.
    from alysis_code.cli_impl.tui.app import _has_conversation

    assert _has_conversation([]) is False
    assert _has_conversation([("warn", "streaming is disabled")]) is False
    assert _has_conversation([("system", "x"), ("trace", "y")]) is False
    # A real turn dismisses the landing.
    assert _has_conversation([("warn", "w"), ("user", "hi")]) is True
    assert _has_conversation([("assistant", "yo")]) is True


def test_startup_warning_keeps_welcome_then_exits():
    # Regression: a streaming-disabled warning emitted while the session is built
    # used to flip the transcript to "has messages" and hide the owl landing.
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui.app import _has_conversation

    class _WarningSession:
        def __init__(self, surface) -> None:
            surface.emit_warning("streaming is disabled for this run")

        def run_turn(self, text, *, cancellation_token=None, **_kwargs):  # pragma: no cover
            return 0

        def close(self) -> None:  # pragma: no cover - parity with real session
            pass

    state = TuiState(model_name="gpt-5.5", username="t")
    with create_pipe_input() as pipe:
        pipe.send_text("/exit\r")
        result, transcript = run_tui(
            state,
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=_WarningSession,
        )
    assert result == "/exit"
    # The warning is retained (it surfaces once chatting) …
    assert ("warn", "streaming is disabled for this run") in transcript
    # … but it is not a turn, so the welcome landing stayed up.
    assert _has_conversation(transcript) is False


def test_footer_marks_usage_totals_with_local_fallbacks():
    text = _plain(
        footer_fragments(TuiState(model_name="m", tokens=100, tokens_estimated=True), width=150)
    )
    assert "100 processed (includes estimates)" in text
    assert "context: n/a" in text
