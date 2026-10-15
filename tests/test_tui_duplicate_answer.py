"""Regression tests for the TUI rendering the same assistant answer 2-3 times.

A multi-step agent turn can re-emit the same answer across continuation / tool
steps. Each streamed answer builds its own ``("assistant", text)`` transcript
block, and once the live block was closed by an intervening tool/trace line the
final ``on_assistant_message_done`` used to append the whole reply AGAIN — so the
identical answer stacked up in the pane. Two layers now guard against it:

- ``TuiTranscript.finish_assistant`` no longer appends a fresh block that merely
  repeats the turn's most recent assistant block (so the duplicate never enters
  the model at all), and
- ``_duplicate_assistant_indices`` collapses any verbatim repeat at render time
  (the safety net for still-open streamed duplicates).

Both are content-based and provider-agnostic (no model-specific logic).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.cli_impl.tui.app import _duplicate_assistant_indices
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import LLMResponse
from alysis_code.llm.types import AssistantResponsePhase, LLMError, LLMOutputLimitError


def _assistant_texts(t: TuiTranscript) -> list[str]:
    return [text for role, text in t.entries if role == "assistant"]


# ------------------------- finish_assistant idempotency -------------------------


def test_restart_assistant_clears_live_block_for_restream():
    # The live path: the client's retry recorder fires the stream_restart hook
    # BEFORE the retry restreams, so the abandoned tokens vanish immediately
    # instead of doubling until finish_assistant's collapse.
    gen_a = "Hi! Ready to help with your ci-gate repo. What would you like to work on?"
    gen_b = "Hi! Ready to help with the ci-gate repo. What would you like to work on?"
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant(gen_a)
    t.restart_assistant()
    assert _assistant_texts(t) == [""]
    t.stream_assistant(gen_b)
    t.finish_assistant(gen_b)
    assert _assistant_texts(t) == [gen_b]


def test_restart_assistant_without_open_block_is_a_noop():
    t = TuiTranscript()
    t.begin_turn()
    t.restart_assistant()
    assert _assistant_texts(t) == []


def test_provider_retry_restream_collapses_to_final_reply():
    # A mid-stream transport failure abandons generation A after its tokens
    # rendered; the provider retry re-runs the request and generation B streams
    # into the SAME open block. Wordings differ ("your" vs "the"), so the
    # verbatim dedupe can never catch it — finish_assistant must snap the
    # block to the authoritative final text.
    gen_a = "Hi! Ready to help with your ci-gate repo. What would you like to work on?"
    gen_b = "Hi! Ready to help with the ci-gate repo. What would you like to work on?"
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant(gen_a)
    t.stream_assistant(gen_b)
    t.finish_assistant(gen_b)
    assert _assistant_texts(t) == [gen_b]


def test_double_retry_restream_collapses_to_final_reply():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("Attempt one. ")
    t.stream_assistant("Attempt two. ")
    t.stream_assistant("Attempt three.")
    t.finish_assistant("Attempt three.")
    assert _assistant_texts(t) == ["Attempt three."]


def test_tail_fragment_final_does_not_truncate_streamed_answer():
    # Pathological provider: final text is only the last chunk of the stream.
    # The >=25% guard must keep the full streamed block intact.
    t = TuiTranscript()
    t.begin_turn()
    long_reply = "Step one does A. Step two does B. Step three does C. Finally, run the tests."
    t.stream_assistant(long_reply)
    t.finish_assistant("run the tests.")
    assert _assistant_texts(t) == [long_reply]


def test_matching_final_leaves_streamed_block_untouched():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("The answer is X.")
    t.finish_assistant("The answer is X.")
    assert _assistant_texts(t) == ["The answer is X."]


def test_finish_after_trace_does_not_duplicate_streamed_answer():
    # stream answer -> tool trace closes the block -> message_done re-emits it.
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("The answer is X.")
    t.append("trace", "✓ some_tool")
    t.finish_assistant("The answer is X.")
    assert _assistant_texts(t) == ["The answer is X."]  # exactly one copy


def test_repeated_model_response_is_hidden_while_streaming_and_removed_at_end():
    t = TuiTranscript()
    t.append_user("Answer the question")
    t.stream_assistant("The answer is X.")
    t.begin_assistant_response()
    t.stream_assistant("The answer")
    entries, _, streaming_index = t.snapshot()
    assert _duplicate_assistant_indices(entries, streaming_index) == {2}

    t.stream_assistant(" is X.")
    entries, _, streaming_index = t.snapshot()
    assert _duplicate_assistant_indices(entries, streaming_index) == {2}
    t.finish_assistant("The answer is X.")
    assert _assistant_texts(t) == ["The answer is X."]


def test_new_model_response_appears_when_it_diverges_from_previous_answer():
    t = TuiTranscript()
    t.append_user("Answer the question")
    t.stream_assistant("The answer is X.")
    t.begin_assistant_response()
    t.stream_assistant("The answer is Y.")
    entries, _, streaming_index = t.snapshot()
    assert _duplicate_assistant_indices(entries, streaming_index) == set()
    t.finish_assistant("The answer is Y.")
    assert _assistant_texts(t) == ["The answer is X.", "The answer is Y."]


def test_agent_continuation_does_not_show_repeated_reply_twice(tmp_path: Path):
    transcript = TuiTranscript()
    transcript.append_user("Answer the question")

    class RepeatedClient:
        model = "test-model"
        temperature = 0.2

        def __init__(self):
            self.calls = 0
            self.visible_duplicates = []

        def chat(self, **kwargs):
            self.calls += 1
            assert self.calls <= 2
            callback = kwargs.get("on_text_delta")
            if callback is not None:
                for chunk in ("The answer", " is X."):
                    callback(chunk)
                    entries, _, streaming_index = transcript.snapshot()
                    hidden = _duplicate_assistant_indices(entries, streaming_index)
                    visible = [
                        text
                        for index, (role, text) in enumerate(entries)
                        if role == "assistant" and index not in hidden
                    ]
                    self.visible_duplicates.append(visible)
            return LLMResponse(
                content="The answer is X.",
                tool_calls=[],
                raw={},
                assistant_phase=(
                    AssistantResponsePhase.COMMENTARY
                    if self.calls == 1
                    else AssistantResponsePhase.FINAL_ANSWER
                ),
            )

    client = RepeatedClient()
    session = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", stream=True),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="override-key",
        enable_chat_turn_step_budget=True,
        verification_enabled=False,
        surface=TuiSurface(transcript),
    )
    session.client = client  # type: ignore[assignment]
    try:
        assert session.run_turn("Answer the question") == 0
    finally:
        session.close()

    assert client.calls == 2
    assert all(visible == ["The answer is X."] for visible in client.visible_duplicates[2:])
    assert _assistant_texts(transcript) == ["The answer is X."]


def test_finish_ignores_incidental_whitespace_when_deduping():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("Line one\nLine two")
    t.append("trace", "✓ tool")
    t.finish_assistant("Line one   Line two")  # same words, different spacing
    assert len(_assistant_texts(t)) == 1


def test_finish_still_appends_a_genuinely_different_answer():
    t = TuiTranscript()
    t.begin_turn()
    t.stream_assistant("First answer.")
    t.append("trace", "✓ tool")
    t.finish_assistant("Second, different answer.")
    assert _assistant_texts(t) == ["First answer.", "Second, different answer."]


def test_identical_answers_in_separate_turns_are_kept():
    # A user boundary resets the turn — the same reply to a repeated question
    # must NOT be suppressed across turns.
    t = TuiTranscript()
    t.append_user("what is 2+2?")
    t.finish_assistant("4")
    t.append_user("what is 2+2?")
    t.finish_assistant("4")
    assert _assistant_texts(t) == ["4", "4"]


# ---------------------- render-layer collapse (safety net) ----------------------


def test_render_collapses_verbatim_repeat_within_turn():
    entries = [
        ("user", "do the thing"),
        ("assistant", "Done — here is the result."),
        ("trace", "✓ tool"),
        ("assistant", "Done — here is the result."),
    ]
    skip = _duplicate_assistant_indices(entries, streaming_index=None)
    assert skip == {3}


def test_render_keeps_a_distinct_live_streaming_block():
    entries = [
        ("assistant", "partial"),
        ("trace", "✓ tool"),
        ("assistant", "different"),
    ]
    skip = _duplicate_assistant_indices(entries, streaming_index=2)
    assert skip == set()


def test_render_keeps_repeat_across_user_boundary():
    entries = [
        ("user", "q"),
        ("assistant", "same reply"),
        ("user", "q"),
        ("assistant", "same reply"),
    ]
    skip = _duplicate_assistant_indices(entries, streaming_index=None)
    assert skip == set()


def test_render_collapses_three_way_repeat():
    entries = [
        ("assistant", "repeat me"),
        ("trace", "t1"),
        ("assistant", "repeat me"),
        ("trace", "t2"),
        ("assistant", "repeat me"),
    ]
    skip = _duplicate_assistant_indices(entries, streaming_index=None)
    assert skip == {2, 4}


def test_render_ignores_blank_assistant_entries():
    entries = [("assistant", "   "), ("assistant", "real")]
    skip = _duplicate_assistant_indices(entries, streaming_index=None)
    assert skip == set()


def _visible_assistant_texts(t: TuiTranscript) -> list[str]:
    entries, _, streaming_index = t.snapshot()
    hidden = _duplicate_assistant_indices(entries, streaming_index)
    return [
        text
        for index, (role, text) in enumerate(entries)
        if role == "assistant" and text.strip() and index not in hidden
    ]


def _recovery_session(tmp_path: Path, transcript: TuiTranscript, client, *, stream=True):
    session = create_session(
        cfg=AppConfig(
            model="test-model", routing_mode="code_only", stream=stream, skills_enabled=False
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=5,
        no_log=True,
        api_key_override="test-key",
        verification_enabled=False,
        enable_compaction=False,
        surface=TuiSurface(transcript),
    )
    session.client = client
    return session


@pytest.mark.parametrize("trace", [False, True])
@pytest.mark.parametrize("empty_attempts", [1, 2, 3])
def test_empty_aggregation_discards_all_abandoned_deltas(tmp_path, trace, empty_attempts):
    t = TuiTranscript()
    t.append_user("Answer the question")

    class Client:
        model = "test-model"
        temperature = 0.2
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            assert self.calls <= empty_attempts + 1
            if self.calls <= empty_attempts:
                kwargs["on_text_delta"]("The abandoned answer begins")
                if trace:
                    t.append_trace("Background progress")
                    kwargs["on_text_delta"](" then still abandoned")
                return LLMResponse(content="", tool_calls=[], raw={})
            kwargs["on_text_delta"]("The answer is X.")
            return LLMResponse(content="The answer is X.", tool_calls=[], raw={})

    session = _recovery_session(tmp_path, t, Client())
    try:
        assert session.run_turn("Answer the question") == 0
    finally:
        session.close()
    assert _visible_assistant_texts(t) == ["The answer is X."]


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("restates_partial", [False, True])
def test_token_limit_continuation_keeps_both_answer_parts(tmp_path, stream, restates_partial):
    t = TuiTranscript()
    t.append_user("Answer the question")

    class Client:
        model = "test-model"
        temperature = 0.2
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            assert self.calls <= 2
            text = "The first part." if self.calls == 1 else "The second part."
            if self.calls == 2 and restates_partial:
                text = "The first part. The second part."
            if kwargs.get("on_text_delta"):
                kwargs["on_text_delta"](text)
            response = LLMResponse(content=text, tool_calls=[], raw={})
            if self.calls == 1:
                raise LLMOutputLimitError(response)
            assert any(m.get("content") == "The first part." for m in kwargs["messages"])
            return response

    session = _recovery_session(tmp_path, t, Client(), stream=stream)
    try:
        assert session.run_turn("Answer the question") == 0
    finally:
        session.close()
    assert _visible_assistant_texts(t) == (
        ["The first part. The second part."]
        if restates_partial
        else ["The first part.", "The second part."]
    )


def test_stream_fallback_discards_partial_before_buffered_answer(tmp_path):
    t = TuiTranscript()
    t.append_user("Answer the question")

    class Client:
        model = "test-model"
        temperature = 0.2
        calls = 0

        def chat(self, **kwargs):
            self.calls += 1
            assert self.calls <= 2
            if self.calls == 1:
                kwargs["on_text_delta"]("Abandoned partial answer")
                raise LLMError("stream is not supported")
            assert kwargs["stream"] is False
            return LLMResponse(content="The answer is X.", tool_calls=[], raw={})

    session = _recovery_session(tmp_path, t, Client())
    try:
        assert session.run_turn("Answer the question") == 0
    finally:
        session.close()
    assert _visible_assistant_texts(t) == ["The answer is X."]


@pytest.mark.parametrize("trace", [False, True])
def test_finalization_keeps_host_added_notice(trace):
    t = TuiTranscript()
    t.append_user("Run the tests")
    t.stream_assistant("Tests failed.")
    if trace:
        t.append_trace("Background progress")
    final = "Tests failed.\n\nVerification status: unverified."
    t.finish_assistant(final)
    assert _visible_assistant_texts(t) == [final]


def test_repeated_candidates_with_final_notice_render_once_at_every_delta():
    t = TuiTranscript()
    t.append_user("Run the tests")
    for _ in range(3):
        t.begin_assistant_response()
        for chunk in ("Tests ", "failed."):
            t.stream_assistant(chunk)
            assert len(_visible_assistant_texts(t)) == 1
    final = "Tests failed.\n\nVerification status: unverified."
    t.finish_assistant(final)
    assert _assistant_texts(t) == [final]


def test_duplicate_removal_preserves_later_reasoning_metadata():
    t = TuiTranscript()
    t.append_user("Answer")
    t.stream_assistant("The answer is X.")
    t.begin_assistant_response()
    t.stream_assistant("The answer is X.")
    t.begin_reasoning("summary-2")
    t.stream_reasoning("Checking the answer")
    t.end_reasoning("summary-2")
    t.finish_assistant("The answer is X.")
    assert t.entries[2] == ("reasoning", "Checking the answer")
    assert t.reasoning_block_ids() == {2: "summary-2"}
    assert set(t.reasoning_snapshot()[1]) == {2}


def test_split_response_finalization_keeps_complete_answer_and_notice():
    t = TuiTranscript()
    t.append_user("Run the tests")
    t.stream_assistant("Tests ")
    t.append_trace("Background progress")
    t.stream_assistant("failed.")
    final = "Tests failed.\n\nVerification status: unverified."
    t.finish_assistant(final)
    assert _visible_assistant_texts(t) == [final]
    assert ("trace", "Background progress") in t.entries


def test_trace_split_gate_candidate_is_reconciled_before_the_next_response():
    t = TuiTranscript()
    t.append_user("Run the tests")
    t.stream_assistant("Tests ")
    t.append_trace("Background progress")
    t.stream_assistant("failed.")
    t.begin_assistant_response()
    t.stream_assistant("Tests ")
    assert _visible_assistant_texts(t) == ["Tests failed."]
    t.stream_assistant("failed.")
    t.finish_assistant("Tests failed.")
    assert _assistant_texts(t) == ["Tests failed."]
