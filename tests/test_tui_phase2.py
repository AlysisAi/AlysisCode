"""Phase 2 TUI tests: the conversation model, the agent surface, and a headless
end-to-end run that streams a fake agent turn into the transcript.

Turns run inline (``background_turns=False``) so ordering is deterministic.
"""

from __future__ import annotations

from alysis_code.cli_impl.tui import run_tui
from alysis_code.cli_impl.tui.state import TuiState
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.surface.types import (
    ApprovalDecision,
    ApprovalRequest,
    ToolEndEvent,
    ToolStartEvent,
)

# --------------------------- transcript model ---------------------------


def test_transcript_streams_assistant_into_one_block():
    t = TuiTranscript()
    t.append_user("hi")
    t.begin_turn()
    t.stream_assistant("Hel")
    t.stream_assistant("lo")
    t.finish_assistant("Hello")
    assert t.entries == [("user", "hi"), ("assistant", "Hello")]


def test_transcript_finish_uses_final_when_no_stream():
    t = TuiTranscript()
    t.begin_turn()
    t.finish_assistant("done")
    assert ("assistant", "done") in t.entries


def test_transcript_load_history_keeps_user_assistant_drops_tools():
    # /resume reload: prior conversation replaces the pane; only user/assistant
    # text turns survive (tool calls/results and blank turns are dropped).
    t = TuiTranscript()
    t.append_user("stale")  # pre-existing content must be cleared first
    t.load_history(
        [
            {"role": "user", "content": "first question"},
            {
                "role": "assistant",
                "content": "first answer",
                "reasoning_content": "raw provider reasoning must stay out of the transcript",
                "reasoning": "opaque provider continuation state",
                "_alysis_provider_metadata": {"provider": {"encrypted_content": "opaque-state"}},
            },
            {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
            {"role": "tool", "tool_call_id": "1", "content": "tool output"},
            {"role": "user", "content": "second question"},
            {"role": "assistant", "content": "second answer\n"},
        ]
    )
    assert t.entries == [
        ("user", "first question"),
        ("assistant", "first answer"),
        ("user", "second question"),
        ("assistant", "second answer"),
    ]
    # No assistant block is left "open", so every reply renders as completed.
    assert t.snapshot()[2] is None


def test_transcript_load_history_empty_clears():
    t = TuiTranscript()
    t.append_user("stale")
    t.load_history([])
    assert t.entries == []


def test_surface_replace_history_reloads_transcript():
    t = TuiTranscript()
    surface = TuiSurface(t)
    surface.replace_history(
        [
            {"role": "user", "content": "q"},
            {"role": "assistant", "content": "a"},
        ]
    )
    assert t.entries == [("user", "q"), ("assistant", "a")]


def test_surface_append_note_uses_given_role():
    # The resume outcome line picks its role so it can flip the welcome→chat pane
    # (assistant) or stay a dim status (system) as needed.
    t = TuiTranscript()
    surface = TuiSurface(t)
    surface.append_note("Resumed session: x (0 turns loaded).", role="assistant")
    surface.append_note("plain status")  # defaults to system
    surface.append_note("   ")  # blank is dropped
    assert t.entries == [
        ("assistant", "Resumed session: x (0 turns loaded)."),
        ("system", "plain status"),
    ]


def test_transcript_status_is_transient():
    t = TuiTranscript()
    t.set_status("Thinking…")
    assert t.status == "Thinking…"
    t.stream_assistant("x")
    assert t.status is None


def test_transcript_invalidate_fires_on_mutation():
    hits = {"n": 0}
    t = TuiTranscript(invalidate=lambda: hits.__setitem__("n", hits["n"] + 1))
    t.append_user("hi")
    assert hits["n"] >= 1


# --------------------------- surface ---------------------------


def test_surface_streams_tokens_and_done():
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_user_message("hi")
    s.on_assistant_token("Hello ")
    s.on_assistant_token("world")
    s.on_assistant_message_done("Hello world")
    assert ("assistant", "Hello world") in t.entries


def test_surface_renders_tool_trace():
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_tool_start(ToolStartEvent(tool_call_id="1", name="read_file", args={}, step=1))
    # While running, the tool shows via the live status (which drives the single
    # under-question activity indicator), not a committed "⚙ start" line.
    assert t.status
    assert not any(role == "trace" for role, _ in t.entries)
    s.on_tool_end(ToolEndEvent(tool_call_id="1", name="read_file", status="done", elapsed_ms=1200))
    roles = [role for role, _ in t.entries]
    assert roles.count("trace") == 1  # only the completion line is recorded
    assert any(text.startswith("✓") for _r, text in t.entries)
    assert t.status is None


def test_surface_tool_trace_shows_argument_detail():
    # Repeated tool lines must stay distinguishable: a web search shows its
    # query in both the live status and the committed "✓" line, so four
    # searches don't render as four identical "Search Web" rows.
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id="1",
            name="web_search",
            args={"query": "world cup bracket"},
            step=1,
        )
    )
    assert t.status is not None
    assert "world cup bracket" in t.status
    s.on_tool_end(ToolEndEvent(tool_call_id="1", name="web_search", status="done", elapsed_ms=1400))
    assert any(
        role == "trace" and text.startswith("✓") and "world cup bracket" in text
        for role, text in t.entries
    )
    # The stashed detail is consumed on completion.
    assert s._tool_details == {}


def _end_fetch(s: TuiSurface, *, status: str, meta: dict[str, object], call_id: str = "1") -> None:
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id=call_id,
            name="web_fetch",
            args={"url": "https://www.weather-atlas.com/en/greece/athens"},
            step=1,
        )
    )
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id=call_id, name="web_fetch", status=status, elapsed_ms=558, meta=meta
        )
    )


def test_surface_renders_remote_blocked_fetch_as_neutral_trace():
    # A site refusing automated clients (anti-bot/403) is normal web reality,
    # not an agent failure: a grey "◦ … site doesn't allow …" trace line, with
    # no red ✗ and no amber warning.
    t = TuiTranscript()
    s = TuiSurface(t)
    _end_fetch(
        s,
        status="failed",
        meta={
            "error": "HTTP error 403 while fetching …: remote site declined automated access.",
            "blocked_by_remote_site": True,
        },
    )
    assert t.entries == [
        (
            "trace",
            "◦ Fetch Web Page · https://www.weather-atlas.com/en/greece/athens · "
            "site doesn't allow automated access (558ms)",
        )
    ]


def test_surface_names_what_the_remote_site_did():
    t = TuiTranscript()
    s = TuiSurface(t)
    _end_fetch(
        s,
        status="failed",
        meta={
            "error": "web_fetch request to '…' timed out during response read.",
            "remote_site_reason": "site didn't respond",
        },
    )
    [(role, text)] = t.entries
    assert role == "trace"
    assert text.startswith("◦ Fetch Web Page · ")
    assert text.endswith(" · site didn't respond (558ms)")
    assert "timed out" not in text


def test_surface_hides_remote_site_notices_when_trace_is_off():
    t = TuiTranscript()
    s = TuiSurface(t)
    s.set_trace_level("off")
    _end_fetch(s, status="failed", meta={"remote_site_reason": "site requires sign-in"})
    assert t.entries == []


def test_surface_trace_off_does_not_render_successes_as_failures():
    # Regression: with the trace off, a successful call fell through to the
    # failure branch and printed "✗ … failed".
    t = TuiTranscript()
    s = TuiSurface(t)
    s.set_trace_level("off")
    s.on_tool_start(ToolStartEvent(tool_call_id="1", name="fs_read", args={"path": "a.py"}, step=1))
    s.on_tool_end(ToolEndEvent(tool_call_id="1", name="fs_read", status="done", elapsed_ms=12))
    assert t.entries == []


def test_surface_marks_withdrawn_tool_as_unavailable_not_done():
    # An unrecoverable web failure withdraws web_fetch for the turn; its result
    # reports "done" to the model, but the trace must not draw a success ✓.
    t = TuiTranscript()
    s = TuiSurface(t)
    _end_fetch(
        s,
        status="done",
        meta={
            "tool_unavailable": True,
            "unavailable_reason": "HTTP request failed: [Errno 101] Network is unreachable",
        },
    )
    [(role, text)] = t.entries
    assert role == "warn"
    assert text.startswith("⚠ Fetch Web Page · ")
    assert "unavailable (558ms): HTTP request failed: [Errno 101] Network is unreachable" in text
    assert not text.startswith("✓")


def test_surface_keeps_red_error_for_ordinary_fetch_failures():
    # Only remote-declined blocks are softened; other failures stay ✗ errors.
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id="1", name="web_fetch", args={"url": "https://x.example/a"}, step=1
        )
    )
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="1",
            name="web_fetch",
            status="failed",
            elapsed_ms=120,
            meta={"error": "HTTP error 500 while fetching 'https://x.example/a'."},
        )
    )
    assert any(role == "error" and text.startswith("✗") for role, text in t.entries)
    assert not any(role == "warn" for role, _text in t.entries)


def test_surface_groups_consecutive_same_tool_traces():
    # Four searches must not render four "✓ Search Web · …" rows: the first is
    # a full line, consecutive same-tool successes become "  ↳ <query>" rows.
    t = TuiTranscript()
    s = TuiSurface(t)
    for i, query in enumerate(["current date today", "today's date"], start=1):
        s.on_tool_start(
            ToolStartEvent(tool_call_id=str(i), name="web_search", args={"query": query}, step=i)
        )
        s.on_tool_end(
            ToolEndEvent(tool_call_id=str(i), name="web_search", status="done", elapsed_ms=100 * i)
        )
    trace_lines = [text for role, text in t.entries if role == "trace"]
    assert len(trace_lines) == 2
    assert trace_lines[0].startswith("✓ Search Web · current date today")
    assert trace_lines[1].startswith("  ↳ today's date")


def test_surface_tool_trace_grouping_breaks_on_interleaved_output():
    # Anything committed between two same-tool successes (an assistant message,
    # a different tool) restarts a full "✓" line — grouping is adjacency-only.
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_tool_start(
        ToolStartEvent(tool_call_id="1", name="web_search", args={"query": "first"}, step=1)
    )
    s.on_tool_end(ToolEndEvent(tool_call_id="1", name="web_search", status="done", elapsed_ms=10))
    s.on_assistant_message_done("let me refine that")
    s.on_tool_start(
        ToolStartEvent(tool_call_id="2", name="web_search", args={"query": "second"}, step=2)
    )
    s.on_tool_end(ToolEndEvent(tool_call_id="2", name="web_search", status="done", elapsed_ms=10))
    trace_lines = [text for role, text in t.entries if role == "trace"]
    assert len(trace_lines) == 2
    assert all(line.startswith("✓ Search Web · ") for line in trace_lines)


def test_surface_tool_trace_hides_shell_command_detail():
    # Shell command lines can carry secrets; they are never previewed on the
    # trace lines (unlike web/search/file tools).
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_tool_start(
        ToolStartEvent(
            tool_call_id="1",
            name="shell_run",
            args={"cmd": "export API_KEY=hunter2 && ./deploy.sh"},
            step=1,
        )
    )
    assert t.status is not None
    assert "hunter2" not in t.status
    s.on_tool_end(ToolEndEvent(tool_call_id="1", name="shell_run", status="done", elapsed_ms=10))
    assert not any("hunter2" in text for _r, text in t.entries)


def test_surface_tool_trace_without_preview_keeps_plain_label():
    # Tools without an input preview (unknown/custom names) keep the bare label.
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_tool_start(ToolStartEvent(tool_call_id="1", name="custom_tool", args={"x": 1}, step=1))
    assert t.status == "custom_tool…"
    s.on_tool_end(ToolEndEvent(tool_call_id="1", name="custom_tool", status="done", elapsed_ms=10))
    assert any(role == "trace" and text == "✓ custom_tool (10ms)" for role, text in t.entries)


def test_surface_refreshes_hud_mid_turn():
    # The footer HUD (context/tokens/cost) must advance DURING a long multi-step
    # turn, not only when it ends: the surface calls on_hud_refresh at safe points
    # (message-done, tool-end) on the worker thread, throttled to avoid re-running
    # on every step.
    t = TuiTranscript()
    calls = {"n": 0}
    s = TuiSurface(
        t,
        on_hud_refresh=lambda: calls.__setitem__("n", calls["n"] + 1),
    )
    s.on_user_message("go")
    s.on_assistant_message_done("calling a tool")
    assert calls["n"] >= 1  # refreshed mid-turn, before the turn completed
    after_msg = calls["n"]
    s._hud_last_refresh = 0.0  # step past the throttle window
    s.on_tool_end(ToolEndEvent(tool_call_id="1", name="shell_run", status="done", elapsed_ms=10))
    assert calls["n"] == after_msg + 1  # tool-end is another safe refresh point
    # Throttle: a second immediate tool-end must NOT re-fire.
    s.on_tool_end(ToolEndEvent(tool_call_id="2", name="shell_run", status="done", elapsed_ms=10))
    assert calls["n"] == after_msg + 1


def test_surface_hud_refresh_optional():
    # Without an on_hud_refresh callback the surface must behave exactly as before
    # (no crash, no extra work) — the hook is purely additive.
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_assistant_message_done("done")
    s.on_tool_end(ToolEndEvent(tool_call_id="1", name="read_file", status="done", elapsed_ms=5))
    assert any(text.startswith("✓") for _r, text in t.entries)


def test_surface_renders_failed_tool_as_error():
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="1",
            name="shell_run",
            status="error",
            elapsed_ms=50,
            meta={"error": "boom"},
        )
    )
    assert any(role == "error" and "boom" in text for role, text in t.entries)


def test_surface_renders_approval_declined_tool_as_declined():
    t = TuiTranscript()
    s = TuiSurface(t)
    s.on_tool_end(
        ToolEndEvent(
            tool_call_id="1",
            name="fs_edit",
            status="failed",
            elapsed_ms=50,
            meta={"approval_declined": True, "error": "User declined: fs_edit"},
        )
    )
    assert any(
        role == "error" and "approval declined" in text and "failed" not in text
        for role, text in t.entries
    )


def test_surface_defers_to_the_approval_ui():
    t = TuiTranscript()
    seen: list[str] = []

    def _ui(request: ApprovalRequest) -> ApprovalDecision:
        seen.append(request.kind)
        return ApprovalDecision(allow=True)

    s = TuiSurface(t, request_approval_ui=_ui)
    decision = s.request_approval(
        ApprovalRequest(kind="fs_write", reason="r", preview="p", files=["a.py"])
    )
    assert decision.allow is True
    assert seen == ["fs_write"]


def test_surface_never_auto_allows_without_asking():
    # Regression guard for the removed auto-approve axis: reaching
    # request_approval means the mode decided a human must answer, so the
    # surface must not grant the request on its own.
    t = TuiTranscript()
    s = TuiSurface(t, request_approval_ui=lambda _r: ApprovalDecision(allow=False))
    decision = s.request_approval(
        ApprovalRequest(kind="fs_write", reason="r", preview="p", files=["a.py"])
    )
    assert decision.allow is False


def test_surface_fails_closed_when_no_ui():
    t = TuiTranscript()
    s = TuiSurface(t, request_approval_ui=None)
    decision = s.request_approval(
        ApprovalRequest(kind="fs_write", reason="r", preview="p", files=["a.py"])
    )
    assert decision.allow is False
    assert any(role == "warn" for role, _ in t.entries)
    warning_text = "\n".join(text for role, text in t.entries if role == "warn")
    assert "no approval UI is available" in warning_text
    assert "auto-approve is off" not in warning_text


def test_surface_emit_error_warning_delegate_to_render():
    t = TuiTranscript()
    s = TuiSurface(t)
    s.emit_error("terminal_error", "boom", False)
    s.emit_warning("careful")
    assert any(role == "error" and "boom" in text for role, text in t.entries)
    assert any(role == "warn" and "careful" in text for role, text in t.entries)


def test_surface_emit_probe_not_mistaken_for_noop():
    # Regression for the high-severity bug: a synthesized no-op emit_error (e.g.
    # via __getattr__) makes the runtime's capability probe believe the surface
    # handles errors and skip the on_error render path. Real class-level methods
    # must differ from NoopSurface, and absent additive emit_* must stay absent.
    from alysis_code.surface.noop_surface import NoopSurface

    assert getattr(TuiSurface, "emit_error", None) is not getattr(NoopSurface, "emit_error", None)
    assert getattr(TuiSurface, "emit_warning", None) is not getattr(
        NoopSurface, "emit_warning", None
    )
    s = TuiSurface(TuiTranscript())
    assert getattr(s, "emit_message_delta", None) is None
    assert getattr(s, "emit", None) is None


def test_runtime_emit_surface_error_reaches_transcript():
    # End-to-end: drive the actual runtime helper that chooses emit_* vs on_*.
    from alysis_code.agent.turn.core import _emit_surface_error

    t = TuiTranscript()
    s = TuiSurface(t)
    _emit_surface_error(s, "terminal_error", "TOOL BLEW UP", False)
    assert any(role == "error" and "TOOL BLEW UP" in text for role, text in t.entries)


# --------------------------- headless end-to-end ---------------------------


class _FakeSession:
    """Minimal stand-in for AgentSession that drives the surface."""

    def __init__(self, surface: TuiSurface) -> None:
        self.surface = surface
        self.closed = False

    def run_turn(self, text: str, *, cancellation_token=None) -> int:
        self.surface.on_user_message(text)
        self.surface.on_assistant_token("Echo: ")
        self.surface.on_assistant_token(text)
        self.surface.on_assistant_message_done(f"Echo: {text}")
        return 0

    def close(self) -> None:
        self.closed = True


def _run_headless(state: TuiState, keys: str, **kwargs):
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        return run_tui(state, owl_color=False, input=pipe, output=DummyOutput(), **kwargs)


def _run_and_capture_input_geometry(monkeypatch, keys: str):
    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.containers import VSplit
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import app as app_module

    captured = {}

    def geometry(application):
        main = application.layout.container.content
        input_row = next(child for child in main.children if isinstance(child, VSplit))
        side = input_row.children[0]
        frame = input_row.children[1]
        input_inner = frame.children[1].children[1].get_container()
        width = side.width() if callable(side.width) else side.width
        height = side.height() if callable(side.height) else side.height
        inner_children = getattr(input_inner, "children", None)
        return width.preferred, height, len(inner_children) if inner_children is not None else 1

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application
        captured["welcome"] = geometry(application)
        return application

    monkeypatch.setattr(app_module, "Application", capture_application)
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
        )

    captured["after"] = geometry(captured["application"])
    return captured


def test_headless_welcome_input_frame_is_full_width_three_rows(monkeypatch):
    geometry = _run_and_capture_input_geometry(monkeypatch, "/exit\r")

    assert geometry["welcome"] == (0, 3, 1)


def test_headless_input_geometry_is_unchanged_after_first_turn(monkeypatch):
    geometry = _run_and_capture_input_geometry(monkeypatch, "hello\r/exit\r")

    assert geometry["welcome"] == (0, 3, 1)
    assert geometry["after"] == geometry["welcome"]


def _run_headless_with_plain_input_selection(monkeypatch, keys: str):
    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.selection import SelectionType

    from alysis_code.cli_impl.tui import app as app_module

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)

        def select_input_text():
            buffer = application.layout.current_buffer
            buffer.text = "abcd"
            buffer.cursor_position = 4
            buffer.start_selection(selection_type=SelectionType.CHARACTERS)
            buffer.cursor_position = 2
            assert buffer.selection_state is not None
            assert buffer.selection_state.shift_mode is False

        application.pre_run_callables.append(select_input_text)
        return application

    monkeypatch.setattr(app_module, "Application", capture_application)
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        _result, transcript = app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
        )
    return [text for role, text in transcript if role == "user"]


def test_input_plain_selection_backspace_cuts_selected_span(monkeypatch):
    assert _run_headless_with_plain_input_selection(monkeypatch, "\x7f\r/exit\r") == ["ab"]


def test_input_plain_selection_printable_key_replaces_selected_span(monkeypatch):
    assert _run_headless_with_plain_input_selection(monkeypatch, "x\r/exit\r") == ["abx"]


def test_input_plain_selection_control_j_replaces_selected_span_with_newline(monkeypatch):
    assert _run_headless_with_plain_input_selection(monkeypatch, "\nZ\r/exit\r") == ["ab\nZ"]


def test_input_plain_selection_enter_submits_full_buffer(monkeypatch):
    assert _run_headless_with_plain_input_selection(monkeypatch, "\r/exit\r") == ["abcd"]


def test_input_plain_selection_delete_behavior_is_unchanged(monkeypatch):
    assert _run_headless_with_plain_input_selection(monkeypatch, "\x1b[3~\r/exit\r") == ["ab"]


def _bracketed_paste(text: str) -> str:
    return f"\x1b[200~{text}\x1b[201~"


def _run_headless_paste_submission(monkeypatch, keys: str):
    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import app as app_module

    captured = {}
    delivered = []
    registry_type = getattr(app_module, "_PasteRegistry", None)
    if registry_type is not None:

        class CapturingPasteRegistry(registry_type):
            def __init__(self):
                super().__init__()
                captured["registry"] = self

            def expand(self, text):
                captured.setdefault("buffer_before_submit", text)
                captured.setdefault("payloads_before_submit", self.snapshot())
                application = captured.get("application")
                if application is not None:
                    captured.setdefault(
                        "complete_state_before_submit",
                        application.layout.current_buffer.complete_state,
                    )
                return super().expand(text)

        monkeypatch.setattr(app_module, "_PasteRegistry", CapturingPasteRegistry)

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application
        return application

    class RecordingSession:
        def __init__(self, surface):
            self.surface = surface

        def run_turn(self, text, *, cancellation_token=None):
            delivered.append(text)
            return 0

    monkeypatch.setattr(app_module, "Application", capture_application)
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        _result, transcript = app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=RecordingSession,
            background_turns=False,
        )
    if "registry" in captured:
        captured["payloads_after_run"] = captured["registry"].snapshot()
    user_echoes = [text for role, text in transcript if role == "user"]
    return delivered, user_echoes, captured


def test_large_bracketed_paste_collapses_and_expands_once(monkeypatch):
    payload = "\n".join(f"line {number}" for number in range(1, 101))
    token = "[pasted #1 +100 lines]"

    delivered, user_echoes, captured = _run_headless_paste_submission(
        monkeypatch,
        _bracketed_paste(payload) + "\r/exit\r",
    )

    assert captured.get("buffer_before_submit", delivered[0]) == token
    assert captured["payloads_before_submit"] == {1: payload}
    assert captured["complete_state_before_submit"] is None
    assert delivered == [payload]
    assert user_echoes == [token]
    assert captured["payloads_after_run"] == {}


def test_small_bracketed_paste_inserts_verbatim(monkeypatch):
    payload = "first\nsecond\nthird"

    delivered, user_echoes, _captured = _run_headless_paste_submission(
        monkeypatch,
        _bracketed_paste(payload) + "\r/exit\r",
    )

    assert delivered == [payload]
    assert user_echoes == [payload]


def test_bracketed_paste_normalizes_crlf_in_small_and_large_payloads(monkeypatch):
    small_raw = "first\r\nsecond\rthird"
    small_normalized = "first\nsecond\nthird"
    large_raw = "\r\n".join(f"line {number}" for number in range(1, 10))
    large_normalized = "\n".join(f"line {number}" for number in range(1, 10))

    small_delivered, small_echoes, _small_captured = _run_headless_paste_submission(
        monkeypatch,
        _bracketed_paste(small_raw) + "\r/exit\r",
    )
    large_delivered, large_echoes, _large_captured = _run_headless_paste_submission(
        monkeypatch,
        _bracketed_paste(large_raw) + "\r/exit\r",
    )

    assert small_delivered == [small_normalized]
    assert small_echoes == [small_normalized]
    assert large_delivered == [large_normalized]
    assert large_echoes == ["[pasted #1 +9 lines]"]


def test_two_large_pastes_number_and_expand_independently(monkeypatch):
    first = "\n".join(f"first {number}" for number in range(1, 10))
    second = "\n".join(f"second {number}" for number in range(1, 10))
    display = "[pasted #1 +9 lines] between [pasted #2 +9 lines]"

    delivered, user_echoes, captured = _run_headless_paste_submission(
        monkeypatch,
        _bracketed_paste(first) + " between " + _bracketed_paste(second) + "\r/exit\r",
    )

    assert captured.get("buffer_before_submit", user_echoes[0]) == display
    assert captured["payloads_before_submit"] == {1: first, 2: second}
    assert delivered == [first + " between " + second]
    assert user_echoes == [display]


def test_backspace_at_paste_token_edge_removes_token_and_orphan(monkeypatch):
    payload = "\n".join(f"line {number}" for number in range(1, 10))

    delivered, user_echoes, captured = _run_headless_paste_submission(
        monkeypatch,
        "keep " + _bracketed_paste(payload) + "\x7f\r/exit\r",
    )

    # Trailing composer whitespace is trimmed on submit, so "keep " arrives as
    # "keep"; what this pins is that the token and its payload went with the
    # single backspace.
    assert delivered == ["keep"]
    assert user_echoes == ["keep"]
    assert captured["payloads_before_submit"] == {}


def test_delete_at_paste_token_edge_removes_token_and_orphan(monkeypatch):
    payload = "\n".join(f"line {number}" for number in range(1, 10))
    token = "[pasted #1 +9 lines]"

    delivered, user_echoes, captured = _run_headless_paste_submission(
        monkeypatch,
        "keep " + _bracketed_paste(payload) + "\x1b[D" * len(token) + "\x1b[3~\r/exit\r",
    )

    assert delivered == ["keep"]
    assert user_echoes == ["keep"]
    assert captured["payloads_before_submit"] == {}


def test_paste_editor_updates_payload_and_token_line_count(monkeypatch):
    import threading
    import time

    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import app as app_module

    payload = "\n".join(f"line {number}" for number in range(1, 10))
    edited = "edited first\nedited second"
    captured = {}
    delivered = []
    feeder_errors = []

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application
        captured["input_buffer"] = application.layout.current_buffer
        return application

    class RecordingSession:
        def __init__(self, surface):
            self.surface = surface

        def run_turn(self, text, *, cancellation_token=None):
            delivered.append(text)
            return 0

    def wait_for(predicate, message):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        feeder_errors.append(message)
        return False

    monkeypatch.setattr(app_module, "Application", capture_application)
    with create_pipe_input() as pipe:

        def feed():
            pipe.send_text(_bracketed_paste(payload) + "\x1bp")
            if not wait_for(
                lambda: (
                    captured.get("application") is not None
                    and captured["application"].layout.current_buffer
                    is not captured.get("input_buffer")
                ),
                "paste editor did not open",
            ):
                pipe.send_text("\x04")
                return
            editor_buffer = captured["application"].layout.current_buffer
            assert editor_buffer.text == payload
            editor_buffer.text = edited
            pipe.send_text("\x13")
            if not wait_for(
                lambda: (
                    captured["application"].layout.current_buffer is captured.get("input_buffer")
                ),
                "paste editor did not close after save",
            ):
                pipe.send_text("\x04")
                return
            captured["token_after_save"] = captured["input_buffer"].text
            pipe.send_text("\r/exit\r")

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        _result, transcript = app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=RecordingSession,
            background_turns=False,
        )
        feeder.join(timeout=3)

    assert not feeder.is_alive()
    assert feeder_errors == []
    assert captured["token_after_save"] == "[pasted #1 +2 lines]"
    assert delivered == [edited]
    assert [text for role, text in transcript if role == "user"] == ["[pasted #1 +2 lines]"]


def _run_paste_editor_with_plain_selection(monkeypatch, edit_keys: str):
    import threading
    import time

    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from prompt_toolkit.selection import SelectionType

    from alysis_code.cli_impl.tui import app as app_module

    payload = "\n".join(f"line {number}" for number in range(1, 10))
    captured = {}
    delivered = []
    feeder_errors = []

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application
        captured["input_buffer"] = application.layout.current_buffer
        return application

    class RecordingSession:
        def __init__(self, surface):
            self.surface = surface

        def run_turn(self, text, *, cancellation_token=None):
            delivered.append(text)
            return 0

    def wait_for(predicate, message):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        feeder_errors.append(message)
        return False

    monkeypatch.setattr(app_module, "Application", capture_application)
    with create_pipe_input() as pipe:

        def feed():
            pipe.send_text(_bracketed_paste(payload) + "\x1bp")
            if not wait_for(
                lambda: (
                    captured.get("application") is not None
                    and captured["application"].layout.current_buffer
                    is not captured.get("input_buffer")
                ),
                "paste editor did not open",
            ):
                pipe.send_text("\x04")
                return
            editor_buffer = captured["application"].layout.current_buffer
            editor_buffer.text = "abcd"
            editor_buffer.cursor_position = 4
            editor_buffer.start_selection(selection_type=SelectionType.CHARACTERS)
            editor_buffer.cursor_position = 2
            assert editor_buffer.selection_state is not None
            assert editor_buffer.selection_state.shift_mode is False
            pipe.send_text(edit_keys)
            time.sleep(0.05)
            pipe.send_text("\x13")
            if not wait_for(
                lambda: (
                    captured["application"].layout.current_buffer is captured.get("input_buffer")
                ),
                "paste editor did not close after save",
            ):
                pipe.send_text("\x04")
                return
            pipe.send_text("\r/exit\r")

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=RecordingSession,
            background_turns=False,
        )
        feeder.join(timeout=3)

    assert not feeder.is_alive()
    assert feeder_errors == []
    return delivered


def test_editor_plain_selection_backspace_cuts_selected_span(monkeypatch):
    assert _run_paste_editor_with_plain_selection(monkeypatch, "\x7f") == ["ab"]


def test_editor_plain_selection_printable_key_replaces_selected_span(monkeypatch):
    assert _run_paste_editor_with_plain_selection(monkeypatch, "x") == ["abx"]


def test_live_paste_hint_tracks_tokens_on_welcome_delete_and_submit(monkeypatch):
    import threading
    import time

    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.formatted_text import fragment_list_to_text, to_formatted_text
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import app as app_module

    first = "\n".join(f"first {number}" for number in range(1, 10))
    second = "\n".join(f"second {number}" for number in range(1, 10))
    captured = {}
    delivered = []
    feeder_errors = []

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application
        captured["input_buffer"] = application.layout.current_buffer
        return application

    class RecordingSession:
        def __init__(self, surface):
            self.surface = surface

        def run_turn(self, text, *, cancellation_token=None):
            delivered.append(text)
            return 0

    def wait_for(predicate, message):
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        feeder_errors.append(message)
        return False

    def status_snapshot():
        root = captured["application"].layout.container.content
        status_container = root.children[1]
        status_window = status_container.content
        value = status_window.content.text
        fragments = value() if callable(value) else value
        return bool(status_container.filter()), fragment_list_to_text(to_formatted_text(fragments))

    monkeypatch.setattr(app_module, "Application", capture_application)
    with create_pipe_input() as pipe:

        def feed():
            pipe.send_text(_bracketed_paste(first))
            if not wait_for(
                lambda: (
                    captured.get("input_buffer") is not None
                    and captured["input_buffer"].text == "[pasted #1 +9 lines]"
                ),
                "first paste token did not appear",
            ):
                pipe.send_text("\x04")
                return
            captured["one"] = status_snapshot()

            pipe.send_text(_bracketed_paste(second))
            if not wait_for(
                lambda: captured["input_buffer"].text.endswith("[pasted #2 +9 lines]"),
                "second paste token did not appear",
            ):
                pipe.send_text("\x04")
                return
            captured["two"] = status_snapshot()

            pipe.send_text("\x7f\x7f")
            if not wait_for(
                lambda: captured["input_buffer"].text == "",
                "paste tokens were not deleted",
            ):
                pipe.send_text("\x04")
                return
            captured["deleted"] = status_snapshot()

            pipe.send_text(_bracketed_paste(first) + "\r")
            if not wait_for(lambda: delivered == [first], "paste payload was not submitted"):
                pipe.send_text("\x04")
                return
            captured["submitted"] = status_snapshot()
            pipe.send_text("/exit\r")

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=RecordingSession,
            background_turns=False,
        )
        feeder.join(timeout=3)

    assert not feeder.is_alive()
    assert feeder_errors == []
    assert captured["one"] == (True, "  pasted #1 +9 lines - alt+p to view")
    assert captured["two"] == (True, "  2 pasted blocks - cursor on one, alt+p to view")
    assert captured["deleted"] == (False, "")
    assert "pasted" not in captured["submitted"][1]


def _run_alt_p_away_from_paste_tokens(monkeypatch, payloads):
    import threading
    import time

    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import app as app_module

    captured = {}
    feeder_errors = []

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        # Pipe input delivers each escape sequence atomically. Keep both parser
        # delays short so cancelling the editor does not spend 1.5 seconds of
        # the two-second assertion deadline waiting for hypothetical more keys.
        application.ttimeoutlen = 0.05
        application.timeoutlen = 0.05
        captured["application"] = application
        captured["input_buffer"] = application.layout.current_buffer
        return application

    def wait_for(predicate, message, timeout=2):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        feeder_errors.append(message)
        return False

    monkeypatch.setattr(app_module, "Application", capture_application)
    with create_pipe_input() as pipe:

        def feed():
            keys = "lead " + " between ".join(_bracketed_paste(payload) for payload in payloads)
            pipe.send_text(keys + "\x1b[H\x1bp")
            if len(payloads) == 1:
                if not wait_for(
                    lambda: (
                        captured.get("application") is not None
                        and captured["application"].layout.current_buffer
                        is not captured.get("input_buffer")
                    ),
                    "single-token alt+p did not open the editor",
                    timeout=5,
                ):
                    pipe.send_text("\x04")
                    return
                captured["editor_text"] = captured["application"].layout.current_buffer.text
                pipe.send_text("\x1b")
                if not wait_for(
                    lambda: (
                        captured["application"].layout.current_buffer
                        is captured.get("input_buffer")
                    ),
                    "paste editor did not close",
                ):
                    pipe.send_text("\x04")
                    return
            else:
                if not wait_for(
                    lambda: captured.get("application") is not None,
                    "application was never constructed",
                ):
                    pipe.send_text("\x04")
                    return
                time.sleep(0.1)
                captured["editor_open"] = captured[
                    "application"
                ].layout.current_buffer is not captured.get("input_buffer")
                if captured["editor_open"]:
                    # An unexpectedly open editor float would swallow "/exit"
                    # as text and hang the app forever (the pre-hardening
                    # flaky-hang mechanism). Close it so the test can FAIL
                    # honestly on the assertion instead.
                    pipe.send_text("\x1b")
                    wait_for(
                        lambda: (
                            captured["application"].layout.current_buffer
                            is captured.get("input_buffer")
                        ),
                        "unexpected editor did not close",
                    )
            captured["input_buffer"].text = ""
            pipe.send_text("/exit\r")
            # Failsafe: if the app is still running shortly after /exit, force
            # an exit so a regression shows up as a failed assertion with
            # feeder_errors context, never as an infinite hang.
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                application = captured.get("application")
                if application is not None and not application.is_running:
                    return
                time.sleep(0.05)
            application = captured.get("application")
            if application is not None and application.is_running:
                feeder_errors.append("app did not exit after /exit; forced exit")
                try:
                    application.loop.call_soon_threadsafe(application.exit)
                except Exception:
                    pipe.send_text("\x04")

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
        )
        feeder.join(timeout=3)

    assert not feeder.is_alive()
    return captured, feeder_errors


def test_single_paste_alt_p_away_from_token_opens_editor(monkeypatch):
    payload = "\n".join(f"line {number}" for number in range(1, 10))

    captured, feeder_errors = _run_alt_p_away_from_paste_tokens(monkeypatch, [payload])

    assert feeder_errors == []
    assert captured["editor_text"] == payload


def test_two_pastes_alt_p_away_from_tokens_keeps_placeholder_noop(monkeypatch):
    first = "\n".join(f"first {number}" for number in range(1, 10))
    second = "\n".join(f"second {number}" for number in range(1, 10))

    captured, feeder_errors = _run_alt_p_away_from_paste_tokens(monkeypatch, [first, second])

    assert feeder_errors == []
    assert captured["editor_open"] is False


def test_headless_runs_agent_turn():
    state = TuiState(model_name="deepseek-chat", username="t")
    sessions: list[_FakeSession] = []

    def _builder(surface):
        sess = _FakeSession(surface)
        sessions.append(sess)
        return sess

    completed = {"n": 0}
    _result, transcript = _run_headless(
        state,
        "hi there\r/exit\r",
        session_builder=_builder,
        on_turn_complete=lambda: completed.__setitem__("n", completed["n"] + 1),
        background_turns=False,
    )
    assert ("user", "hi there") in transcript
    assert ("assistant", "Echo: hi there") in transcript
    assert completed["n"] == 1
    assert sessions and sessions[0].surface is not None


def test_user_band_rows_full_width_with_prompt():
    from alysis_code.cli_impl.tui.app import _user_band_rows

    width = 40
    rows = _user_band_rows("hi", width)
    assert len(rows) == 3  # blank pad + text + blank pad
    for row in rows:
        assert sum(len(t) for _s, t in row) == width  # every row spans full width
    text_row = "".join(t for _s, t in rows[1])
    assert text_row.startswith("› hi")


def test_user_band_rows_wraps_long_message():
    from alysis_code.cli_impl.tui.app import _user_band_rows

    width = 24
    rows = _user_band_rows("a fairly long message that wraps", width)
    for row in rows:
        assert sum(len(t) for _s, t in row) == width
    assert len(rows) >= 4  # pad + >=2 text rows + pad


def test_assistant_rows_have_marker():
    from alysis_code.cli_impl.tui.app import _assistant_rows

    rows = _assistant_rows("Hello\nworld")
    first = "".join(t for _s, t in rows[0])
    assert first.startswith("✦ Hello")
    assert "".join(t for _s, t in rows[1]) == "  world"


def _row_width(row) -> int:
    return sum(len(t) for _s, t in row)


def test_assistant_rows_plain_wrap_to_width_keeps_follow_accurate():
    # Regression: the transcript window wraps lines on screen (wrap_lines=True), so
    # an over-wide emitted row becomes extra UNcounted screen rows and the follow
    # math undershoots, hiding the live "thinking" line behind the footer. Every
    # emitted row must be <= width so logical rows == screen rows.
    from alysis_code.cli_impl.tui.app import _assistant_rows

    width = 30
    rows = _assistant_rows("word " * 40, width, markdown=False)
    assert len(rows) > 1
    assert all(_row_width(row) <= width for row in rows)
    assert "".join(t for _s, t in rows[0]).startswith("✦ ")


def test_assistant_rows_hard_break_long_url():
    from alysis_code.cli_impl.tui.app import _assistant_rows

    width = 24
    url = "https://www.fifa.com/fifaplus/en/tournaments/mens/worldcup/canadamexicousa2026"
    rows = _assistant_rows(url, width, markdown=False)
    assert all(_row_width(row) <= width for row in rows)


def test_plain_role_rows_wrap_long_line_to_width():
    from alysis_code.cli_impl.tui.app import _plain_role_rows

    width = 28
    text = "X Search Web failed (42.3s): OpenRouter web_search timed out during response read"
    rows = _plain_role_rows("class:tui.transcript.error", text, width)
    assert len(rows) > 1
    assert all(_row_width(row) <= width for row in rows)
    joined = " ".join("".join(t for _s, t in row) for row in rows)
    assert "OpenRouter" in joined and "timed" in joined


def test_wrap_line_preserves_blank_and_breaks_long_token():
    from alysis_code.cli_impl.tui.app import _wrap_line

    assert _wrap_line("", 10) == [""]
    chunks = _wrap_line("a" * 25, 10)
    assert chunks and all(len(c) <= 10 for c in chunks)
    assert "".join(chunks) == "a" * 25


def test_followup_placeholder_is_short_and_distinct():
    from alysis_code.cli_impl.tui.content import (
        INPUT_PLACEHOLDER,
        INPUT_PLACEHOLDER_FOLLOWUP,
    )

    assert INPUT_PLACEHOLDER_FOLLOWUP != INPUT_PLACEHOLDER
    assert len(INPUT_PLACEHOLDER_FOLLOWUP) < len(INPUT_PLACEHOLDER)
    assert INPUT_PLACEHOLDER_FOLLOWUP.lower().strip(" .…") != "ask anything"


def test_scroll_target_clamps_and_reports_follow():
    from alysis_code.cli_impl.tui.app import _scroll_target

    assert _scroll_target(20, 20, -10) == (10, False)  # scroll up off the tail
    assert _scroll_target(10, 20, 10) == (20, True)  # back to the tail → follow
    assert _scroll_target(3, 20, -10) == (0, False)  # cannot pass the top
    assert _scroll_target(0, 0, -10) == (0, True)  # content fits → always tail


def test_wheel_scroll_speed_defaults_and_clamps_environment_values(monkeypatch):
    from alysis_code.cli_impl.tui.app import _resolve_wheel_step_rows

    assert _resolve_wheel_step_rows("") == 3
    assert _resolve_wheel_step_rows("invalid") == 3
    assert _resolve_wheel_step_rows("4") == 4
    assert _resolve_wheel_step_rows("0") == 1
    assert _resolve_wheel_step_rows("200") == 20
    monkeypatch.setenv("ALYSIS_SCROLL_SPEED", "7")
    assert _resolve_wheel_step_rows() == 7


def test_tui_input_prefers_controlling_terminal_when_input_is_implicit(monkeypatch):
    from alysis_code.cli_impl.tui import app as app_module

    created = object()
    calls: list[bool] = []

    def _create_input(*, always_prefer_tty: bool):
        calls.append(always_prefer_tty)
        return created

    monkeypatch.setattr(app_module, "create_input", _create_input)

    resolved, owned = app_module._resolve_tui_input(None)

    assert resolved is created
    assert owned is created
    assert calls == [True]


def test_tui_input_preserves_explicit_pipe_input(monkeypatch):
    from alysis_code.cli_impl.tui import app as app_module

    explicit = object()
    monkeypatch.setattr(
        app_module,
        "create_input",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("must not create input")),
    )

    resolved, owned = app_module._resolve_tui_input(explicit)

    assert resolved is explicit
    assert owned is None


def test_prompt_toolkit_decodes_wsl_style_sgr_wheel_packets():
    from prompt_toolkit.input.vt100_parser import Vt100Parser
    from prompt_toolkit.keys import Keys

    decoded = []
    parser = Vt100Parser(decoded.append)
    parser.feed("\x1b[<64;10;10M\x1b[<65;10;10M")
    parser.flush()

    assert [key_press.key for key_press in decoded] == [
        Keys.Vt100MouseEvent,
        Keys.Vt100MouseEvent,
    ]
    assert [key_press.data for key_press in decoded] == [
        "\x1b[<64;10;10M",
        "\x1b[<65;10;10M",
    ]


def test_transcript_selection_extracts_forward_and_reverse_multiline_text():
    from prompt_toolkit.data_structures import Point

    from alysis_code.cli_impl.tui.app import _selected_text

    rows = ["alpha beta   ", "second line", "third"]
    expected = "beta\nsecond line\nthi"

    assert _selected_text(rows, Point(x=6, y=0), Point(x=2, y=2)) == expected
    assert _selected_text(rows, Point(x=2, y=2), Point(x=6, y=0)) == expected
    assert _selected_text(rows, Point(x=2, y=1), Point(x=2, y=1)) == ""


def test_transcript_selection_omits_visual_message_markers_and_outer_padding():
    from prompt_toolkit.data_structures import Point

    from alysis_code.cli_impl.tui.app import _selected_text

    rows = [
        "                                        ",
        "› hello mate                            ",
        "                                        ",
        "✦ Hey mate — how can I help?             ",
        "▸ thought                                ",
        "│ internal detail                        ",
        "                                        ",
    ]

    copied = _selected_text(
        rows,
        Point(x=0, y=0),
        Point(x=len(rows[-1]) - 1, y=len(rows) - 1),
    )

    assert copied == ("hello mate\n\nHey mate — how can I help?\nthought\ninternal detail")


def test_transcript_selection_preserves_content_that_resembles_markup():
    from prompt_toolkit.data_structures import Point

    from alysis_code.cli_impl.tui.app import _selected_text

    rows = ["› > quoted content", "✦ ✦ literal star", "  indented code"]

    copied = _selected_text(
        rows,
        Point(x=0, y=0),
        Point(x=len(rows[-1]) - 1, y=len(rows) - 1),
    )

    assert copied == "> quoted content\n✦ literal star\n  indented code"


def test_transcript_semantic_copy_excludes_reasoning_and_tool_chrome():
    from prompt_toolkit.data_structures import Point

    from alysis_code.cli_impl.tui.app import _selected_text

    raw_reasoning_sentinel = "private-chain-of-thought"
    rows = [
        "› question",
        "",
        "▾ reasoning summary",
        f"│ {raw_reasoning_sentinel}",
        "",
        "▸ Read File · README.md",
        "✓ Read File (5ms)",
        "",
        "✦ answer",
        "",
        "✗ Write File failed: disk full",
    ]
    row_roles = [
        "user",
        "spacer",
        "reasoning",
        "reasoning",
        "chrome",
        "trace",
        "trace",
        "chrome",
        "assistant",
        "spacer",
        "error",
    ]

    copied = _selected_text(
        rows,
        Point(x=0, y=0),
        Point(x=len(rows[-1]) - 1, y=len(rows) - 1),
        row_roles=row_roles,
    )

    assert copied == "question\n\nanswer\n\nWrite File failed: disk full"
    assert raw_reasoning_sentinel not in copied
    assert "Read File" not in copied


def test_transcript_semantic_copy_of_only_reasoning_is_empty():
    from prompt_toolkit.data_structures import Point

    from alysis_code.cli_impl.tui.app import _selected_text

    rows = ["▾ reasoning summary", "│ safe summary that is display-only"]

    assert (
        _selected_text(
            rows,
            Point(x=0, y=0),
            Point(x=len(rows[-1]) - 1, y=1),
            row_roles=["reasoning", "reasoning"],
        )
        == ""
    )


def test_transcript_selection_highlights_only_selected_characters():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.formatted_text import fragment_list_to_text

    from alysis_code.cli_impl.tui.app import _highlight_selection_in_row

    row = [("class:a", "hello "), ("class:b", "world")]
    highlighted = _highlight_selection_in_row(
        row,
        row_index=0,
        anchor=Point(x=3, y=0),
        active=Point(x=7, y=0),
    )

    assert fragment_list_to_text(highlighted) == "hello world"
    selected = "".join(text for style, text in highlighted if "selection" in style)
    assert selected == "lo wo"


def test_copying_transcript_selection_uses_system_clipboard(monkeypatch):
    from alysis_code.cli_impl.tui import app as app_module

    copied: list[str] = []
    monkeypatch.setattr(app_module, "copy_text_to_clipboard", copied.append)

    notice = app_module._copy_selection_notice("selected text")

    assert copied == ["selected text"]
    assert notice == "Copied 13 characters"


def test_completed_transcript_selection_reports_unavailable_clipboard(monkeypatch):
    from alysis_code.cli_impl.tui import app as app_module

    def _fail(_text: str) -> None:
        raise app_module.ClipboardError("unavailable")

    monkeypatch.setattr(app_module, "copy_text_to_clipboard", _fail)

    assert app_module._copy_selection_notice("selected") == (
        "Selected text · clipboard unavailable"
    )


def test_completion_menu_size_stays_above_bottom_chrome():
    from alysis_code.cli_impl.tui.app import (
        _completion_menu_height,
        _completion_menu_width,
    )

    assert _completion_menu_height(32) == 8
    assert _completion_menu_height(12) == 2
    assert _completion_menu_width(120) == 84
    assert _completion_menu_width(45) == 37


def test_scrollable_control_routes_wheel_events():
    from prompt_toolkit.mouse_events import MouseEventType

    from alysis_code.cli_impl.tui.app import _ScrollableControl

    seen: list = []
    ctrl = _ScrollableControl(lambda: [], on_scroll=lambda d: seen.append(d))

    class _Evt:
        def __init__(self, et):
            self.event_type = et

    assert ctrl.mouse_handler(_Evt(MouseEventType.SCROLL_UP)) is None
    assert ctrl.mouse_handler(_Evt(MouseEventType.SCROLL_DOWN)) is None
    assert ctrl.mouse_handler(_Evt(MouseEventType.MOUSE_UP)) is NotImplemented
    assert seen == [-1, 1]


def test_scrollable_control_routes_drag_events_without_disabling_wheel():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from alysis_code.cli_impl.tui.app import _ScrollableControl

    scrolls: list[int] = []
    mouse_events: list[MouseEventType] = []
    ctrl = _ScrollableControl(
        lambda: [],
        on_scroll=scrolls.append,
        on_mouse_event=lambda event: mouse_events.append(event.event_type),
    )

    down = MouseEvent(Point(x=1, y=2), MouseEventType.MOUSE_DOWN, MouseButton.LEFT, frozenset())
    move = MouseEvent(Point(x=4, y=2), MouseEventType.MOUSE_MOVE, MouseButton.LEFT, frozenset())
    wheel = MouseEvent(Point(x=4, y=2), MouseEventType.SCROLL_DOWN, MouseButton.NONE, frozenset())

    assert ctrl.mouse_handler(down) is None
    assert ctrl.mouse_handler(move) is None
    assert ctrl.mouse_handler(wheel) is None
    assert mouse_events == [MouseEventType.MOUSE_DOWN, MouseEventType.MOUSE_MOVE]
    assert scrolls == [1]


def test_drag_scroll_direction_uses_both_viewport_edges():
    from alysis_code.cli_impl.tui.app import _drag_scroll_direction

    assert _drag_scroll_direction(screen_y=4, window_top=5, window_height=6) == -1
    assert _drag_scroll_direction(screen_y=5, window_top=5, window_height=6) == -1
    assert _drag_scroll_direction(screen_y=7, window_top=5, window_height=6) == 0
    assert _drag_scroll_direction(screen_y=10, window_top=5, window_height=6) == 1
    assert _drag_scroll_direction(screen_y=12, window_top=5, window_height=6) == 1


def test_drag_capture_projects_outside_pointer_to_nearest_visible_content_row():
    from types import SimpleNamespace

    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.layout.containers import Window
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from alysis_code.cli_impl.tui.app import _project_mouse_event_to_window

    window = Window()
    window.render_info = SimpleNamespace(
        window_height=4,
        window_width=10,
        _y_offset=5,
        _x_offset=3,
        visible_line_to_row_col={
            0: (20, 2),
            1: (21, 2),
            2: (22, 2),
            3: (23, 2),
        },
    )
    event = MouseEvent(
        Point(x=8, y=12),
        MouseEventType.MOUSE_MOVE,
        MouseButton.LEFT,
        frozenset(),
    )

    projected = _project_mouse_event_to_window(event, window)

    assert projected is not None
    target_event, direction = projected
    assert target_event.position == Point(x=7, y=23)
    assert target_event.event_type == MouseEventType.MOUSE_MOVE
    assert target_event.button == MouseButton.LEFT
    assert direction == 1


def test_mouse_capture_window_preserves_raw_screen_coordinates():
    from prompt_toolkit.data_structures import Point
    from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType

    from alysis_code.cli_impl.tui.app import _MouseCaptureWindow

    seen = []
    capture = _MouseCaptureWindow(lambda event: seen.append(event.position))
    event = MouseEvent(
        Point(x=19, y=11),
        MouseEventType.MOUSE_UP,
        MouseButton.LEFT,
        frozenset(),
    )

    assert capture._mouse_handler(event) is None
    assert seen == [Point(x=19, y=11)]


def test_transcript_selection_drag_autoscrolls_at_top_edge(monkeypatch):
    import threading
    import time

    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import app as app_module

    captured = {}
    feeder_errors = []

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application
        return application

    class LongReplySession:
        def __init__(self, surface):
            self.surface = surface

        def run_turn(self, text, *, cancellation_token=None):
            self.surface.on_user_message(text)
            self.surface.on_assistant_message_done(
                "\n".join(f"transcript row {number}" for number in range(80))
            )
            return 0

    def wait_for(predicate, message):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        feeder_errors.append(message)
        return False

    def stop_app():
        application = captured.get("application")
        if application is not None and application.loop is not None:
            application.loop.call_soon_threadsafe(application.exit)

    monkeypatch.setattr(app_module, "Application", capture_application)
    monkeypatch.setattr(app_module, "copy_text_to_clipboard", lambda _text: None)
    with create_pipe_input() as pipe:

        def feed():
            pipe.send_text("hello\r")

            def transcript_window():
                application = captured.get("application")
                if application is None:
                    return None
                return next(
                    (
                        window
                        for window in application.layout.find_all_windows()
                        if isinstance(window.content, app_module._ScrollableControl)
                        and window.render_info is not None
                        and window.vertical_scroll > 0
                    ),
                    None,
                )

            if not wait_for(lambda: transcript_window() is not None, "transcript did not render"):
                stop_app()
                return
            window = transcript_window()
            assert window is not None and window.render_info is not None
            info = window.render_info
            before = window.vertical_scroll
            x = info._x_offset + 2
            bottom = info._y_offset + info.window_height - 2
            top = info._y_offset
            pipe.send_text(f"\x1b[<0;{x + 1};{bottom + 1}M")
            if not wait_for(
                lambda: any(
                    isinstance(candidate, app_module._MouseCaptureWindow)
                    and candidate.render_info is not None
                    for candidate in captured["application"].layout.find_all_windows()
                ),
                "transcript drag capture did not activate",
            ):
                stop_app()
                return
            pipe.send_text(f"\x1b[<32;{x + 1};{top + 1}M")
            if not wait_for(
                lambda: window.vertical_scroll < before,
                "transcript selection did not auto-scroll upward",
            ):
                stop_app()
                return
            pipe.send_text(f"\x1b[<0;{x + 1};{top + 1}m")
            stop_app()

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=LongReplySession,
            background_turns=False,
        )
        feeder.join(timeout=4)

    assert not feeder.is_alive()
    assert feeder_errors == []


def test_editor_selection_drag_autoscrolls_at_bottom_edge(monkeypatch):
    import threading
    import time

    from prompt_toolkit.application import Application as PromptToolkitApplication
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import app as app_module

    payload = "\n".join(f"editor row {number}" for number in range(100))
    captured = {}
    feeder_errors = []

    def capture_application(*args, **kwargs):
        application = PromptToolkitApplication(*args, **kwargs)
        captured["application"] = application
        captured["input_buffer"] = application.layout.current_buffer
        return application

    def wait_for(predicate, message):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        feeder_errors.append(message)
        return False

    def stop_app():
        application = captured.get("application")
        if application is not None and application.loop is not None:
            application.loop.call_soon_threadsafe(application.exit)

    monkeypatch.setattr(app_module, "Application", capture_application)
    with create_pipe_input() as pipe:

        def feed():
            pipe.send_text(_bracketed_paste(payload) + "\x1bp")
            if not wait_for(
                lambda: (
                    captured.get("application") is not None
                    and captured["application"].layout.current_buffer
                    is not captured.get("input_buffer")
                    and captured["application"].layout.current_window.render_info is not None
                ),
                "paste editor did not render",
            ):
                stop_app()
                return
            application = captured["application"]
            window = application.layout.current_window
            editor_buffer = application.layout.current_buffer
            info = window.render_info
            assert info is not None
            x = info._x_offset + 2
            top = info._y_offset + 1
            bottom = info._y_offset + info.window_height - 1
            pipe.send_text(f"\x1b[<0;{x + 1};{top + 1}M")
            if not wait_for(
                lambda: any(
                    isinstance(candidate, app_module._MouseCaptureWindow)
                    and candidate.render_info is not None
                    for candidate in application.layout.find_all_windows()
                ),
                "editor drag capture did not activate",
            ):
                stop_app()
                return
            pipe.send_text(f"\x1b[<32;{x + 1};{bottom + 1}M")
            if not wait_for(
                lambda: editor_buffer.document.cursor_position_row >= info.window_height,
                "editor selection did not auto-scroll downward",
            ):
                stop_app()
                return
            pipe.send_text(f"\x1b[<0;{x + 1};{bottom + 1}m")
            stop_app()

        feeder = threading.Thread(target=feed, daemon=True)
        feeder.start()
        app_module.run_tui(
            TuiState(model_name="test-model"),
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
        )
        feeder.join(timeout=4)

    assert not feeder.is_alive()
    assert feeder_errors == []


def test_headless_pageup_pagedown_do_not_crash():
    state = TuiState(model_name="m", username="t")
    result, transcript = _run_headless(
        state,
        "hello\r\x1b[5~\x1b[6~/exit\r",  # message, PageUp, PageDown, exit
        session_builder=_FakeSession,
        command_runner=_fake_command_runner([]),
        background_turns=False,
    )
    assert result == "/exit"
    assert ("user", "hello") in transcript


def _fake_command_runner(calls):
    def runner(session, text, width):
        calls.append((text, width))
        low = text.strip().lower()
        if low in ("/exit", "exit"):
            return ("exit", "", None, None)
        if low == "/status":
            return ("handled", "Status: ok", None, None)
        return ("run", "", text, {})

    return runner


def test_headless_slash_command_handled_renders_output():
    state = TuiState(model_name="m", username="t")
    calls: list = []
    _result, transcript = _run_headless(
        state,
        "/status\r/exit\r",
        session_builder=_FakeSession,
        command_runner=_fake_command_runner(calls),
        background_turns=False,
    )
    assert ("user", "/status") in transcript
    assert any(role == "system" and "Status:" in text for role, text in transcript)
    assert not any(role == "assistant" for role, _ in transcript)
    assert calls and calls[0][0] == "/status"


def test_headless_slash_help_opens_popup_not_routed_to_runner():
    # /help is intercepted natively: it opens the centered popup instead of being
    # echoed as a user line or routed to the command runner. Pressing q closes it,
    # then /exit leaves.
    state = TuiState(model_name="m", username="t")
    calls: list = []
    _result, transcript = _run_headless(
        state,
        "/help\rq/exit\r",
        session_builder=_FakeSession,
        command_runner=_fake_command_runner(calls),
        background_turns=False,
    )
    assert ("user", "/help") not in transcript
    assert not any("/help" in text for _role, text in transcript)
    assert all(text.strip().lower() != "/help" for text, _w in calls)


def test_headless_tui_audits_native_and_routed_commands_without_prompt_content():
    class _Store:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def append(self, event_type: str, payload: dict[str, object]) -> None:
            self.events.append((event_type, dict(payload)))

    sessions: list[_FakeSession] = []

    class _AuditedSession(_FakeSession):
        def __init__(self, surface: TuiSurface) -> None:
            super().__init__(surface)
            self.store = _Store()
            sessions.append(self)

    panel_args: list[str] = []

    def _status_provider(arg=""):
        panel_args.append(arg)
        return {
            "title": "Session Status",
            "sections": [("Session", [("mode", "auto", "accent")])],
        }

    calls: list = []
    _run_headless(
        TuiState(model_name="m", username="t"),
        "/help\rq/status private panel argument\rqordinary private prompt\r/exit\r",
        session_builder=_AuditedSession,
        command_runner=_fake_command_runner(calls),
        panel_providers={"/status": _status_provider},
        background_turns=False,
    )

    assert panel_args == ["private panel argument"]
    assert sessions[0].store.events == [
        ("chat_local_command", {"command": "/help", "has_argument": False}),
        ("chat_local_command", {"command": "/status", "has_argument": True}),
        ("chat_local_command", {"command": "/exit", "has_argument": False}),
    ]
    assert not any("private" in repr(payload) for _kind, payload in sessions[0].store.events)


def test_headless_tab_persona_cycle_audits_only_the_shortcut_action():
    class _Store:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def append(self, event_type: str, payload: dict[str, object]) -> None:
            self.events.append((event_type, dict(payload)))

    sessions: list[_FakeSession] = []

    class _AuditedSession(_FakeSession):
        def __init__(self, surface: TuiSurface) -> None:
            super().__init__(surface)
            self.store = _Store()
            sessions.append(self)

    _run_headless(
        TuiState(model_name="m", username="t"),
        "\t/exit\r",
        session_builder=_AuditedSession,
        command_runner=_fake_command_runner([]),
        persona_cycle=lambda: [("system", "Persona -> private-persona-name")],
        background_turns=False,
    )

    assert sessions[0].store.events == [
        ("chat_local_interaction", {"action": "persona_cycle"}),
        ("chat_local_command", {"command": "/exit", "has_argument": False}),
    ]


def test_headless_shift_tab_permissions_cycle_audits_only_the_shortcut_action():
    class _Store:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, object]]] = []

        def append(self, event_type: str, payload: dict[str, object]) -> None:
            self.events.append((event_type, dict(payload)))

    sessions: list[_FakeSession] = []

    class _AuditedSession(_FakeSession):
        def __init__(self, surface: TuiSurface) -> None:
            super().__init__(surface)
            self.store = _Store()
            sessions.append(self)

    _run_headless(
        TuiState(model_name="m", username="t"),
        "\x1b[Z/exit\r",
        session_builder=_AuditedSession,
        command_runner=_fake_command_runner([]),
        mode_cycle=lambda: [("system", "Permissions -> private-mode-value")],
        background_turns=False,
    )

    assert sessions[0].store.events == [
        ("chat_local_interaction", {"action": "permissions_cycle"}),
        ("chat_local_command", {"command": "/exit", "has_argument": False}),
    ]


def test_help_popup_rows_render_green_commands_and_descriptions():
    from alysis_code.cli_impl.tui.app import (
        _help_content_width_for,
        _help_rows_for_sections,
    )

    sections = [
        ("Getting Started", [("/help", "commands & config"), ("/status", "session details")]),
        ("Execution", [("/permissions", "change execution mode")]),
    ]
    width = _help_content_width_for(100)
    rows = _help_rows_for_sections(sections, width)
    # Every row is padded to the panel width (solid background block).
    assert all(sum(len(t) for _s, t in row) == width for row in rows)
    # Commands render in the green command style, left-aligned in a shared column.
    cmd_rows = [row for row in rows if any(s == "class:tui.help.cmd" for s, _t in row)]
    assert len(cmd_rows) == 3  # /help, /status, /permissions
    cmd_texts = ["".join(t for s, t in row if s == "class:tui.help.cmd") for row in cmd_rows]
    assert any(c.startswith("/help") for c in cmd_texts)
    # Shared left column width → every command cell is padded to the same length.
    assert len({len(c) for c in cmd_texts}) == 1
    # Section headers and a closing hint are present.
    assert any(any(s == "class:tui.help.section" for s, _t in row) for row in rows)
    assert any(any(s == "class:tui.help.hint" for s, _t in row) for row in rows)


def test_runtime_help_exposes_permissions_and_never_mode():
    from alysis_code.cli_impl.commands.welcome import _chat_command_sections

    commands = [command for _section, rows in _chat_command_sections() for command, _ in rows]

    assert "/permissions" in commands
    assert "/mode" not in commands


def test_kv_panel_rows_render_toned_values_and_full_width():
    from alysis_code.cli_impl.tui.app import _render_kv_panel_rows

    sections = [
        ("Session", [("mode", "fast (auto)", "accent"), ("dirty", "no", "accent")]),
        ("Web search", [("status", "unavailable", "err"), ("note", "x" * 80, "plain")]),
    ]
    rows = _render_kv_panel_rows(sections, 50)
    # Every row padded to the panel width (solid background block).
    assert all(sum(len(t) for _s, t in row) == 50 for row in rows)
    # Keys render in the dim key column; healthy values in green; errors in red.
    assert any(any(s == "class:tui.help.key" for s, _t in row) for row in rows)
    assert any(any(s == "class:tui.help.accent" for s, _t in row) for row in rows)
    assert any(any(s == "class:tui.help.err" for s, _t in row) for row in rows)
    # Long values wrap with a hanging indent (more body rows than logical values).
    assert any(any(s == "class:tui.help.section" for s, _t in row) for row in rows)
    assert any(any(s == "class:tui.help.hint" for s, _t in row) for row in rows)


def test_headless_status_panel_opens_via_provider_not_routed_to_runner():
    # /status is intercepted by its panel provider: it opens the centered popup
    # instead of being echoed as a user line or routed to the command runner.
    # Pressing q closes it, then /exit leaves.
    state = TuiState(model_name="m", username="t")
    calls: list = []
    opened = {"n": 0}

    def _status_provider(arg=""):
        opened["n"] += 1
        return {
            "title": "Session Status",
            "sections": [("Session", [("mode", "auto", "accent")])],
        }

    _result, transcript = _run_headless(
        state,
        "/status\rq/exit\r",
        session_builder=_FakeSession,
        command_runner=_fake_command_runner(calls),
        panel_providers={"/status": _status_provider},
        background_turns=False,
    )
    assert opened["n"] == 1  # provider was invoked → panel opened
    assert ("user", "/status") not in transcript
    assert not any(text.strip().lower() == "/status" for text, _w in calls)


def test_headless_forge_only_panel_falls_through_outside_forge():
    state = TuiState(model_name="m", username="t")
    calls: list = []
    opened = {"n": 0}

    def _assets_provider(arg=""):
        opened["n"] += 1
        return {"title": "Assets", "sections": []}

    _result, transcript = _run_headless(
        state,
        "/assets\r/exit\r",
        session_builder=_FakeSession,
        command_runner=_fake_command_runner(calls),
        panel_providers={"/assets": _assets_provider},
        background_turns=False,
    )

    assert opened["n"] == 0
    assert any(text.strip().lower() == "/assets" for text, _w in calls)
    assert ("user", "/assets") in transcript


def test_slash_completer_lists_commands_including_stream():
    # The dropdown content includes the restored /stream control, while prefix
    # filtering still narrows the list.
    from prompt_toolkit.document import Document

    from alysis_code.cli_impl.chat_slash_completer import ChatSlashCompleter

    completer = ChatSlashCompleter(mode_provider=lambda: "chat")

    def comps(text: str) -> list[str]:
        return [c.text for c in completer.get_completions(Document(text, len(text)), None)]

    top = comps("/")
    assert "/status" in top
    assert "/help" in top
    assert "/stream" in top
    narrowed = comps("/st")
    assert "/status" in narrowed
    assert all(c.startswith("/st") for c in narrowed)


def test_cancellation_token_contract_raises_keyboardinterrupt():
    # run_turn's _throw_if_cancelled calls token.throw_if_cancelled(...) and relies
    # on it raising to abort mid-stream. Lock that contract so interrupt can't
    # silently regress.
    import pytest

    from alysis_code.cli_impl.tui.app import _Cancellation

    tok = _Cancellation()
    assert tok.is_cancelled is False
    tok.throw_if_cancelled("noop")  # no-op before cancel
    tok.cancel()
    assert tok.is_cancelled is True
    with pytest.raises(KeyboardInterrupt):
        tok.throw_if_cancelled("cancelled_by_user")


def test_cancellation_before_provider_abort_registration_closes_immediately():
    from alysis_code.cancellation import InteractiveCancellationToken

    aborted: list[bool] = []
    token = InteractiveCancellationToken()
    token.cancel()

    token.set_abort_callback(lambda: aborted.append(True))

    assert aborted == [True]


def test_repeated_interactive_cancel_delivers_each_registered_callback_once():
    from alysis_code.cancellation import InteractiveCancellationToken

    deliveries: list[str] = []
    token = InteractiveCancellationToken()
    token.set_abort_callback(lambda: deliveries.append("provider"))
    token.subscribe(lambda: deliveries.append("child"))

    token.cancel()
    token.cancel()

    assert deliveries == ["provider", "child"]


def test_interactive_cancel_records_request_before_provider_abort():
    from alysis_code.cancellation import InteractiveCancellationToken

    deliveries: list[str] = []
    token = InteractiveCancellationToken()
    token.set_request_callback(lambda: deliveries.append("request"))
    token.set_abort_callback(lambda: deliveries.append("provider"))
    token.subscribe(lambda: deliveries.append("child"))

    token.cancel()
    token.cancel()

    assert deliveries == ["request", "provider", "child"]


def test_nonblocking_cancel_fans_out_child_without_waiting_for_provider_abort():
    import threading

    from alysis_code.cancellation import InteractiveCancellationToken

    deliveries: list[str] = []
    provider_started = threading.Event()
    release_provider = threading.Event()
    provider_finished = threading.Event()
    child_cancelled = threading.Event()

    def _blocking_provider_abort() -> None:
        provider_started.set()
        release_provider.wait()
        deliveries.append("provider")
        provider_finished.set()

    def _cancel_child() -> None:
        deliveries.append("child")
        child_cancelled.set()

    token = InteractiveCancellationToken()
    token.set_request_callback(lambda: deliveries.append("request"))
    token.set_abort_callback(_blocking_provider_abort)
    token.subscribe(_cancel_child)

    returned = threading.Event()

    def _cancel() -> None:
        token.cancel_nonblocking()
        returned.set()

    caller = threading.Thread(target=_cancel)
    caller.start()
    try:
        assert provider_started.wait(1.0)
        assert returned.wait(1.0)
        assert child_cancelled.wait(1.0)
        assert deliveries[0] == "request"
        assert "provider" not in deliveries
    finally:
        release_provider.set()
        caller.join(timeout=1.0)
        assert provider_finished.wait(1.0)


def test_subscriber_registered_after_interactive_cancel_runs_once():
    from alysis_code.cancellation import InteractiveCancellationToken

    deliveries: list[str] = []
    token = InteractiveCancellationToken()
    token.cancel()

    unsubscribe = token.subscribe(lambda: deliveries.append("late"))
    token.cancel()
    unsubscribe()

    assert deliveries == ["late"]


def test_surface_drops_output_for_cancelled_worker():
    # After a soft-interrupt the worker's token is cancelled; the surface must drop
    # its (late) streamed output and auto-deny approvals so an abandoned turn can't
    # paint into the transcript or pop a modal.
    from alysis_code.cli_impl.tui.surface import set_active_cancellation

    class _CancelledTok:
        is_cancelled = True

    t = TuiTranscript()
    s = TuiSurface(t)
    set_active_cancellation(_CancelledTok())
    try:
        s.on_reasoning_token("thinking")
        s.on_assistant_token("hello")
        s.on_assistant_message_done("hello")
        assert all("hello" not in text for _role, text in t.entries)
        assert all("thinking" not in text for _role, text in t.entries)
        decision = s.request_approval(
            ApprovalRequest(kind="fs_write", reason="r", preview="p", files=["a.py"])
        )
        assert decision.allow is False
    finally:
        set_active_cancellation(None)  # reset thread-local; don't leak to other tests


def test_approval_modal_rows_render_colored_keys_and_full_width():
    from types import SimpleNamespace

    from alysis_code.cli_impl.tui.app import _render_approval_rows

    req = SimpleNamespace(
        kind="fs_write", command="", files=["approval_demo.txt"], reason="review mode"
    )
    rows = _render_approval_rows(req, 60)
    # Solid background block + colour-coded y/a/n keys + bright target.
    assert all(sum(len(t) for _s, t in row) == 60 for row in rows)
    styles = {s for row in rows for s, _t in row}
    assert "class:tui.approve.head" in styles  # amber headline (non-destructive)
    assert "class:tui.approve.target" in styles
    assert {"class:tui.approve.key.yes", "class:tui.approve.key.no"} <= styles
    # A destructive command turns the headline red.
    danger = SimpleNamespace(kind="shell_run", command="rm -rf build", files=[], reason="x")
    danger_styles = {s for row in _render_approval_rows(danger, 60) for s, _t in row}
    assert "class:tui.approve.head.danger" in danger_styles


def _mode_picker_spec(on_select):
    return {
        "title": "Mode",
        "rows": [
            {"label": "safe (review)", "description": "d", "value": "review", "current": True},
            {"label": "fast (auto)", "description": "d", "value": "auto", "current": False},
            {"label": "read (readonly)", "description": "d", "value": "readonly", "current": False},
        ],
        "on_select": on_select,
    }


def test_headless_mode_picker_digit_selects_and_applies():
    # Bare /permissions opens the picker (not routed to the runner, not echoed); pressing
    # the number applies that option via on_select and echoes its messages.
    state = TuiState(model_name="m", username="t")
    calls: list = []
    picked = {"value": None}

    def on_select(value):
        picked["value"] = value
        return [("system", f"Mode -> {value}")]

    _result, transcript = _run_headless(
        state,
        "/permissions\r2/exit\r",  # open picker, press "2", then exit
        session_builder=_FakeSession,
        command_runner=_fake_command_runner(calls),
        picker_providers={"/permissions": lambda: _mode_picker_spec(on_select)},
        background_turns=False,
    )
    assert picked["value"] == "auto"  # digit 2 chose the second option
    assert ("system", "Mode -> auto") in transcript
    assert ("user", "/permissions") not in transcript
    assert not any(text.strip().lower() == "/permissions" for text, _w in calls)


def test_headless_mode_picker_arrow_then_enter_selects():
    # Down arrow moves the highlight off the current row; Enter applies it.
    state = TuiState(model_name="m", username="t")
    picked = {"value": None}

    def on_select(value):
        picked["value"] = value
        return [("system", f"Mode -> {value}")]

    _result, _transcript = _run_headless(
        state,
        "/permissions\r\x1b[B\r/exit\r",  # open, Down (review->auto), Enter, exit
        session_builder=_FakeSession,
        command_runner=_fake_command_runner([]),
        picker_providers={"/permissions": lambda: _mode_picker_spec(on_select)},
        background_turns=False,
    )
    assert picked["value"] == "auto"


def test_headless_mode_with_arg_falls_through_to_runner():
    # "/permissions fast" (with an arg) must NOT open the picker — it routes to the runner.
    state = TuiState(model_name="m", username="t")
    calls: list = []
    opened = {"n": 0}

    def provider():
        opened["n"] += 1
        return _mode_picker_spec(lambda v: None)

    _run_headless(
        state,
        "/permissions fast\r/exit\r",
        session_builder=_FakeSession,
        command_runner=_fake_command_runner(calls),
        picker_providers={"/permissions": provider},
        background_turns=False,
    )
    assert opened["n"] == 0  # picker never opened
    assert any(text.strip().lower() == "/permissions fast" for text, _w in calls)


def test_headless_with_completer_does_not_crash():
    # Attaching the slash completer (fires on every keystroke via
    # complete_while_typing) must not break normal input/command routing.
    from alysis_code.cli_impl.chat_slash_completer import ChatSlashCompleter

    state = TuiState(model_name="m", username="t")
    calls: list = []
    result, transcript = _run_headless(
        state,
        "/status\r/exit\r",
        session_builder=_FakeSession,
        command_runner=_fake_command_runner(calls),
        completer=ChatSlashCompleter(mode_provider=lambda: "chat"),
        background_turns=False,
    )
    assert result == "/exit"
    assert any(role == "system" and "Status:" in text for role, text in transcript)


def test_headless_plain_message_runs_turn_via_runner():
    state = TuiState(model_name="m", username="t")
    _result, transcript = _run_headless(
        state,
        "hello\r/exit\r",
        session_builder=_FakeSession,
        command_runner=_fake_command_runner([]),
        background_turns=False,
    )
    assert ("user", "hello") in transcript
    assert ("assistant", "Echo: hello") in transcript


def test_headless_slash_clear_resets_session_and_clears_prior_transcript(monkeypatch):
    import io
    from pathlib import Path

    from rich.console import Console

    from alysis_code import cli as cli_mod
    from alysis_code.compaction.conversation_compactor import CompactionState

    state = TuiState(model_name="m", username="t")
    clear_calls: list[str] = []
    cleared_sessions: list[_FakeSession] = []
    pending_images = ["queued.png"]

    class _Store:
        def __init__(self) -> None:
            self.session_id = "tui_clear_session"
            self.events: list[tuple[str, dict[str, object]]] = []

        def append(self, event_type: str, payload: dict[str, object]) -> None:
            self.events.append((event_type, dict(payload)))

    startup_messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "Repo summary"},
        {
            "role": "user",
            "content": (
                "<workspace_binding_context>\n"
                "active_workdir_relpath: .\n"
                "</workspace_binding_context>\n"
            ),
        },
        {
            "role": "user",
            "content": "<environment_context>\nmode: review\n</environment_context>\n",
        },
    ]

    class _ClearSession(_FakeSession):
        def __init__(self, surface: TuiSurface) -> None:
            super().__init__(surface)
            self.messages = [
                *startup_messages,
                {"role": "user", "content": "old private question"},
                {"role": "assistant", "content": "old private answer"},
            ]
            self.startup_messages = startup_messages
            self.pinned_prefix_len = len(startup_messages)
            self.mode = "review"
            self.yes = False
            self.non_interactive = False
            self.one_shot_execution = False
            self.verification_enabled = True
            self.deny_write_prefixes = []
            self.allow_write_globs = None
            self.effective_verification_commands = []
            self.authoritative_verification_commands = None
            self.verification_selection_source = ""
            self.verification_selection_reason = ""
            self.verification_contract_type = ""
            self.verification_authoritative = False
            self.root = Path("/tmp/demo")
            self.focus_dir = self.root
            self.focus_relpath = "."
            self.workspace_kind = "plain_dir"
            self.binding_requested_path = None
            self.binding_source = None
            self.binding_risk_level = None
            self.binding_created_path = None
            self.active_workdir_relpath = "."
            self.store = _Store()
            self.request_context_measurement = object()
            self.conversation_compactor = type(
                "Compactor",
                (),
                {
                    "state": CompactionState(
                        summary={"topic": "old"},
                        history_chunk_index=4,
                        memory_message_index=3,
                        pinned_prefix_len=len(startup_messages),
                        pins=[{"title": "old"}],
                        pins_message_index=2,
                    )
                },
            )()
            cleared_sessions.append(self)

    monkeypatch.setattr(cli_mod, "_refresh_chat_hud_context_cache", lambda _session: None)

    def command_runner(session, text, _width):
        if text.strip().lower() == "/clear":
            clear_calls.append("/clear")
        stream = io.StringIO()
        result = cli_mod._handle_chat_command(
            input_text=text,
            root=Path("/tmp/demo"),
            session=session,
            pending_images=pending_images,
            console=Console(file=stream, force_terminal=False),
            forge_state=cli_mod._ForgeChatState(),
            record_local_command=False,
        )
        return (result, stream.getvalue().rstrip("\n"), None, None)

    _result, transcript = _run_headless(
        state,
        "hello\r/clear\r/exit\r",
        session_builder=_ClearSession,
        command_runner=command_runner,
        background_turns=False,
    )
    assert clear_calls == ["/clear"]
    assert len(cleared_sessions[0].messages) == len(startup_messages)
    assert cleared_sessions[0].messages[:2] == startup_messages[:2]
    assert cleared_sessions[0].messages[2]["content"].startswith("<workspace_binding_context>")
    assert cleared_sessions[0].messages[3]["content"].startswith("<environment_context>")
    assert all(
        "old private" not in str(message.get("content", ""))
        for message in cleared_sessions[0].messages
    )
    assert cleared_sessions[0].conversation_compactor.state.summary == {}
    assert pending_images == []
    assert cleared_sessions[0].store.events == [
        ("chat_local_command", {"command": "/clear", "has_argument": False}),
        ("conversation_cleared", {"trigger": "user_command"}),
        ("chat_local_command", {"command": "/exit", "has_argument": False}),
    ]
    assert ("user", "hello") not in transcript
    assert ("assistant", "Echo: hello") not in transcript
    assert ("system", "Conversation cleared.") in transcript


def test_headless_without_session_uses_stub():
    state = TuiState(model_name="deepseek-chat", username="t")
    _result, transcript = _run_headless(state, "hello\r/exit\r")
    assert ("user", "hello") in transcript
    assert any(role == "system" for role, _ in transcript)
