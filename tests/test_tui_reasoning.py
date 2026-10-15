"""Phase 3 TUI tests: live model reasoning ("thinking").

Covers the full path — the openai_compat reasoning stream callback, the
transcript reasoning block (stream → collapse with elapsed seconds), the surface
opt-in handler, the dim collapsible rendering, and a headless run that streams
reasoning then an answer.
"""

from __future__ import annotations

import httpx

from alysis_code.cli_impl.tui.app import _activity_rows, _reasoning_rows
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.types import ReasoningOutputKind
from alysis_code.surface import ToolEndEvent, ToolStartEvent


def _row_text(row: list[tuple[str, str]]) -> str:
    return "".join(text for _style, text in row)


# ----------------------------- LLM stream layer -----------------------------


def test_openai_compat_streams_native_reasoning_to_callback():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"reasoning_content":"Let me "}}]}\n\n'
                b'data: {"choices":[{"delta":{"reasoning_content":"think."}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"Answer"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com/v1",
        api_key="test",
        model="deepseek-reasoner",
        temperature=0.0,
        transport=httpx.MockTransport(handler),
    )
    reasoning: list[str] = []
    content: list[str] = []
    resp = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        on_reasoning_delta=reasoning.append,
        on_text_delta=content.append,
    )
    assert reasoning == ["Let me ", "think."]
    assert "".join(content) == "Answer"
    assert resp.content == "Answer"
    assert [item.text for item in resp.reasoning] == ["Let me think."]
    assert resp.reasoning[0].kind == ReasoningOutputKind.PROVIDER_REASONING


def test_openai_compat_prefers_structured_summary_over_duplicate_field():
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"reasoning_content":"private",'
                b'"reasoning_details":[{"type":"reasoning.summary",'
                b'"summary":"Checked the constraints."}]}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"Answer"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    client = OpenAICompatClient(
        base_url="https://openrouter.ai/api/v1",
        api_key="test",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    reasoning: list[str] = []
    response = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        on_reasoning_delta=reasoning.append,
    )

    assert reasoning == ["Checked the constraints."]
    assert [(item.kind, item.text) for item in response.reasoning] == [
        (ReasoningOutputKind.SUMMARY, "Checked the constraints.")
    ]
    assert "private" not in "".join(reasoning)


def test_non_repo_chat_falls_back_when_client_lacks_reasoning_param():
    # A narrow client without on_reasoning_delta still streams content (the
    # responder must not crash on older/test-double signatures).
    from alysis_code.agent.llm_calls import _non_repo_chat
    from alysis_code.llm.types import LLMResponse

    class _NarrowClient:
        base_url = "x"
        model = "m"

        def chat(self, *, messages, tools=None, stream=False, on_text_delta=None, temperature=None):
            if on_text_delta:
                on_text_delta("ok")
            return LLMResponse(content="ok", tool_calls=[], raw={})

    text: list[str] = []
    resp = _non_repo_chat(
        client=_NarrowClient(),
        messages=[{"role": "user", "content": "hi"}],
        temperature=0.0,
        stream=True,
        on_text_delta=text.append,
        on_reasoning_delta=lambda _d: None,
    )
    assert resp.content == "ok"
    assert "".join(text) == "ok"


def test_openai_compat_reasoning_callback_is_optional():
    # No on_reasoning_delta passed → still streams content fine (back-compat).
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=(
                b'data: {"choices":[{"delta":{"reasoning_content":"hidden"}}]}\n\n'
                b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                b"data: [DONE]\n\n"
            ),
        )

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com/v1",
        api_key="test",
        model="deepseek-reasoner",
        temperature=0.0,
        transport=httpx.MockTransport(handler),
    )
    resp = client.chat(messages=[{"role": "user", "content": "hi"}], stream=True)
    assert resp.content == "ok"


def test_responses_reasoning_summary_and_answer_reach_tui_transcript():
    def event(event_type: str, payload: str) -> bytes:
        return f"event: {event_type}\ndata: {payload}\n\n".encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        content = b"".join(
            [
                event(
                    "response.reasoning_summary_text.delta",
                    '{"type":"response.reasoning_summary_text.delta",'
                    '"item_id":"rs_1","output_index":0,"summary_index":0,'
                    '"delta":"I will answer briefly."}',
                ),
                event(
                    "response.output_text.delta",
                    '{"type":"response.output_text.delta","item_id":"msg_1",'
                    '"output_index":1,"content_index":0,"delta":"Hello!"}',
                ),
                event(
                    "response.completed",
                    '{"type":"response.completed","response":{"id":"resp_1",'
                    '"status":"completed","output_text":"Hello!","output":[]}}',
                ),
            ]
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content,
        )

    transcript = TuiTranscript()
    transcript.begin_turn()
    surface = TuiSurface(transcript)
    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        api_key="test",
        model="gpt-test",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        stream=True,
        on_reasoning_delta=surface.on_reasoning_token,
        on_text_delta=surface.on_assistant_token,
    )
    surface.on_assistant_message_done(response.content)

    assert ("reasoning", "I will answer briefly.") in transcript.entries
    assert ("assistant", "Hello!") in transcript.entries


# ------------------------------ transcript model ------------------------------


def test_transcript_streams_reasoning_block():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_reasoning("I should ")
    t.stream_reasoning("check X.")
    idx, secs = t.reasoning_snapshot()
    assert idx is not None
    assert t.entries[idx] == ("reasoning", "I should check X.")
    assert idx not in secs  # still open → no recorded duration yet


def test_reasoning_collapses_when_answer_starts():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_reasoning("thinking...")
    r_idx, _secs = t.reasoning_snapshot()
    t.stream_assistant("Hello")
    idx_after, secs = t.reasoning_snapshot()
    assert idx_after is None  # the first answer token closes the block
    assert r_idx in secs and secs[r_idx] >= 0  # elapsed seconds recorded
    assert t.entries[0][0] == "reasoning"
    assert ("assistant", "Hello") in t.entries


def test_reasoning_closes_on_tool_trace():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_reasoning("plan the work")
    t.append("trace", "⚙ running a tool")
    idx_after, secs = t.reasoning_snapshot()
    assert idx_after is None  # a tool following thinking also collapses it
    assert 0 in secs


def test_reasoning_supports_multiple_blocks_per_turn():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_reasoning("step one thinking")
    t.append("trace", "⚙ tool")
    t.stream_reasoning("step two thinking")
    t.stream_assistant("done")
    reasoning_entries = [i for i, (role, _t) in enumerate(t.entries) if role == "reasoning"]
    assert len(reasoning_entries) == 2


def test_reasoning_lifecycle_keeps_provider_calls_in_stable_separate_blocks():
    t = TuiTranscript()

    t.begin_reasoning("call-1")
    t.stream_reasoning("first summary")
    t.end_reasoning("call-1")
    t.begin_reasoning("call-2")
    t.stream_reasoning("second summary")
    t.end_reasoning("call-2")

    assert t.entries == [
        ("reasoning", "first summary"),
        ("reasoning", "second summary"),
    ]
    assert t.reasoning_block_ids() == {0: "call-1", 1: "call-2"}
    active, elapsed = t.reasoning_snapshot()
    assert active is None
    assert set(elapsed) == {0, 1}


def test_reasoning_lifecycle_ignores_stale_end_event():
    t = TuiTranscript()
    t.begin_reasoning("current")
    t.stream_reasoning("still live")

    t.end_reasoning("stale")

    active, _elapsed = t.reasoning_snapshot()
    assert active == 0
    t.end_reasoning("current")
    assert t.reasoning_snapshot()[0] is None


def test_clear_resets_reasoning():
    t = TuiTranscript()
    t.stream_reasoning("x")
    t.clear()
    idx, secs = t.reasoning_snapshot()
    assert idx is None and secs == {}
    assert t.entries == []


# --------------------------------- surface ---------------------------------


def test_surface_routes_reasoning_token():
    t = TuiTranscript()
    surface = TuiSurface(t)
    surface.on_reasoning_token("hmm, let me see")
    idx, _secs = t.reasoning_snapshot()
    assert idx is not None
    assert t.entries[idx] == ("reasoning", "hmm, let me see")


def test_surface_routes_reasoning_lifecycle():
    t = TuiTranscript()
    surface = TuiSurface(t)

    surface.on_reasoning_start("provider-call")
    surface.on_reasoning_token("safe summary")
    surface.on_reasoning_end("provider-call")

    assert t.entries == [("reasoning", "safe summary")]
    assert t.reasoning_block_ids() == {0: "provider-call"}
    assert t.reasoning_snapshot()[0] is None


def test_surface_trace_off_suppresses_reasoning_without_changing_other_levels():
    off_transcript = TuiTranscript()
    off_surface = TuiSurface(off_transcript)
    assert off_surface.set_trace_level("off") == "off"
    off_surface.on_reasoning_token("hidden reasoning")
    assert not any(role == "reasoning" for role, _text in off_transcript.entries)

    for level in ("compact", "full"):
        transcript = TuiTranscript()
        surface = TuiSurface(transcript)
        assert surface.set_trace_level(level) == level
        surface.on_reasoning_token(f"{level} reasoning")
        assert ("reasoning", f"{level} reasoning") in transcript.entries


def test_switching_trace_off_closes_live_reasoning_and_status():
    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    surface.on_reasoning_token("active reasoning")
    surface.on_progress_update("active status")

    surface.set_trace_level("off")

    reasoning_index, _elapsed = transcript.reasoning_snapshot()
    assert reasoning_index is None
    assert transcript.status is None


def test_trace_off_hides_successful_tool_trace_but_keeps_failures_visible():
    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    surface.set_trace_level("off")

    surface.on_tool_end(ToolEndEvent("ok", "fs_read", "done", 5))
    surface.on_tool_end(ToolEndEvent("failed", "fs_write", "error", 8, meta={"error": "disk full"}))

    assert not any(role == "trace" for role, _text in transcript.entries)
    assert any(role == "error" and "disk full" in text for role, text in transcript.entries)


def test_full_trace_adds_tool_start_detail_while_compact_keeps_milestones_only():
    compact_transcript = TuiTranscript()
    compact = TuiSurface(compact_transcript)
    compact.set_trace_level("compact")
    compact.on_tool_start(
        ToolStartEvent(
            tool_call_id="call-1",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )
    compact.on_tool_end(
        ToolEndEvent(tool_call_id="call-1", name="fs_read", status="done", elapsed_ms=5)
    )

    full_transcript = TuiTranscript()
    full = TuiSurface(full_transcript)
    full.set_trace_level("full")
    full.on_tool_start(
        ToolStartEvent(
            tool_call_id="call-2",
            name="fs_read",
            args={"path": "README.md"},
            step=1,
        )
    )
    full.on_tool_end(
        ToolEndEvent(tool_call_id="call-2", name="fs_read", status="done", elapsed_ms=5)
    )

    compact_trace = [text for role, text in compact_transcript.entries if role == "trace"]
    full_trace = [text for role, text in full_transcript.entries if role == "trace"]
    assert len(compact_trace) == 1
    assert compact_trace[0].startswith("✓")
    assert len(full_trace) == 2
    assert full_trace[0].startswith("▸")
    assert full_trace[1].startswith("✓")


# ------------------------------ _reasoning_rows ------------------------------


def test_reasoning_rows_collapsed_summary():
    rows = _reasoning_rows("safe provider summary", 60, live=False, secs=12, expanded=False)
    assert len(rows) == 1  # just the chevron header, no body
    text = _row_text(rows[0])
    assert text.startswith("▸ ")  # collapsed chevron
    assert "safe provider summary" in text


def test_reasoning_rows_live_shows_full_text():
    rows = _reasoning_rows("line one\nline two", 60, live=True, secs=0, expanded=False)
    joined = "\n".join(_row_text(r) for r in rows)
    assert "line one" in joined and "line two" in joined
    assert _row_text(rows[0]).startswith("▾ ")  # open chevron header ("thinking…")
    # Body lines hang off the dim left rail.
    assert any(_row_text(r).startswith("│ ") for r in rows[1:])


def test_reasoning_rows_live_header_animates_with_spinner_and_elapsed():
    rows = _reasoning_rows(
        "partial thought", 60, live=True, secs=0, expanded=False, spinner="⠋", elapsed=3
    )
    header = _row_text(rows[0])
    assert header.startswith("⠋ ")  # the spinner leads the live header
    assert "thinking… 3s" in header


def test_activity_indicator_under_question():
    # The single live activity line shown under the question while busy — either
    # "thinking…" or a running tool name.
    rows = _activity_rows("⠙", "thinking…", 5)
    assert len(rows) == 1
    text = _row_text(rows[0])
    assert text.startswith("⠙ ")
    assert "thinking… 5s" in text
    # Works for a running tool label too, and shows no seconds before any elapse.
    assert _row_text(_activity_rows("⠋", "Search Web…", 0)[0]) == "⠋ Search Web…"


def test_reasoning_rows_expanded_shows_full_when_closed():
    collapsed = _reasoning_rows("detail here", 60, live=False, secs=5, expanded=False)
    expanded = _reasoning_rows("detail here", 60, live=False, secs=5, expanded=True)
    assert len(collapsed) == 1
    assert not any(_row_text(row).startswith("│ ") for row in collapsed)
    assert "detail here" in "\n".join(_row_text(r) for r in expanded)
    assert any(_row_text(row).startswith("│ ") for row in expanded)


# --------------------------- headless integration ---------------------------


class _ReasoningSession:
    """Fake session that streams reasoning, then a short answer."""

    def __init__(self, surface) -> None:
        self.surface = surface

    def run_turn(self, text: str, *, cancellation_token=None, **_kwargs) -> int:
        self.surface.on_user_message(text)
        self.surface.on_reasoning_token("I will greet them politely.")
        self.surface.on_assistant_token("Hi!")
        self.surface.on_assistant_message_done("Hi!")
        return 0

    def close(self) -> None:  # pragma: no cover - parity with real session
        pass


def test_headless_reasoning_then_answer_renders():
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput

    from alysis_code.cli_impl.tui import run_tui
    from alysis_code.cli_impl.tui.state import TuiState

    state = TuiState(model_name="deepseek-reasoner", username="t")
    with create_pipe_input() as pipe:
        pipe.send_text("hi\r/exit\r")
        _result, transcript = run_tui(
            state,
            owl_color=False,
            input=pipe,
            output=DummyOutput(),
            session_builder=_ReasoningSession,
            background_turns=False,
        )
    roles = [role for role, _text in transcript]
    assert "reasoning" in roles  # the thinking block was recorded
    assert ("assistant", "Hi!") in transcript  # and the answer followed it
