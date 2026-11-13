"""A reply cut off by the output limit reaches the turn's recovery on every chat protocol.

The Responses client already reports it (test_output_limit_recovery.py). These cover the
Anthropic Messages and chat-completions clients. Their other callers keep what they got before:
a cut-off reply that used to come back as a response still does, and one that used to be an
error still is (now LLMOutputLimitError, carrying only the finished text). The agent turn
recognizes the cut-off either way: no tool call from the interrupted reply runs or is replayed,
only finished text is kept, and the turn continues instead of failing.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.anthropic_messages import (
    AnthropicMessagesClient,
    _anthropic_messages_from_messages,
)
from alysis_code.llm.metadata import assistant_message_from_response
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.output_limit import (
    catalogued_output_ceiling,
    response_cut_off_by_output_limit,
)
from alysis_code.llm.types import LLMError, LLMOutputLimitError
from alysis_code.model_registry import ModelMeta
from alysis_code.session_store import read_session_events

# ---------------------------------------------------------------- helpers


def _sse_event(name: str, data: dict[str, Any]) -> str:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n"


def _sse(*events: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream("".join(events).encode("utf-8")),
    )


def _anthropic(handler: Any) -> AnthropicMessagesClient:
    return AnthropicMessagesClient(
        base_url="https://api.anthropic.com/v1",
        api_key="test-key",
        model="claude-sonnet-4-6",
        transport=httpx.MockTransport(handler),
    )


def _anthropic_tool_stream(*, partial_json: str, stop_reason: str) -> httpx.Response:
    return _sse(
        _sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_cut",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [],
                },
            },
        ),
        _sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        _sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": "Writing the workbook script."},
            },
        ),
        _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "fs_write",
                    "input": {},
                },
            },
        ),
        _sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 1,
                "delta": {"type": "input_json_delta", "partial_json": partial_json},
            },
        ),
        _sse_event("content_block_stop", {"type": "content_block_stop", "index": 1}),
        _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason},
                "usage": {"output_tokens": 8192},
            },
        ),
        _sse_event("message_stop", {"type": "message_stop"}),
    )


def _anthropic_text_stream(text: str) -> httpx.Response:
    return _sse(
        _sse_event(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_text",
                    "role": "assistant",
                    "model": "claude-sonnet-4-6",
                    "content": [],
                },
            },
        ),
        _sse_event(
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        _sse_event(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        _sse_event("content_block_stop", {"type": "content_block_stop", "index": 0}),
        _sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 2},
            },
        ),
        _sse_event("message_stop", {"type": "message_stop"}),
    )


def _anthropic_json(
    content: list[dict[str, Any]], *, stop_reason: str, output_tokens: int = 8192
) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "id": "msg_json",
            "model": "claude-sonnet-4-6",
            "content": content,
            "stop_reason": stop_reason,
            "usage": {"input_tokens": 100, "output_tokens": output_tokens},
        },
    )


_ANTHROPIC_WRITE = {
    "type": "tool_use",
    "id": "toolu_2",
    "name": "fs_write",
    "input": {"path": "out.txt", "content": "partial"},
}


def _replayed_assistant_blocks(response: Any) -> list[list[dict[str, Any]]]:
    _system, wire = _anthropic_messages_from_messages(
        [
            {"role": "user", "content": "Build the budget."},
            assistant_message_from_response(response),
        ]
    )
    return [message["content"] for message in wire if message["role"] == "assistant"]


def _compat(handler: Any, **kwargs: Any) -> OpenAICompatClient:
    return OpenAICompatClient(
        base_url="https://llm.example.test/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def _completion(
    *,
    finish_reason: str,
    content: str = "",
    tool_calls: list[dict[str, Any]] | None = None,
    completion_tokens: int = 8192,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": "chatcmpl-1",
        "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": {
            "prompt_tokens": 100,
            "completion_tokens": completion_tokens,
            "total_tokens": 100 + completion_tokens,
        },
    }


def _compat_stream(finish_reason: str) -> httpx.Response:
    chunks = [
        {"choices": [{"index": 0, "delta": {"role": "assistant", "content": "Thinking"}}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}]},
    ]
    body = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks) + "data: [DONE]\n\n"
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        stream=httpx.ByteStream(body.encode("utf-8")),
    )


_WRITE_CALL = {
    "id": "call_1",
    "type": "function",
    "function": {"name": "fs_write", "arguments": '{"path": "bad.txt", "content": "partial"}'},
}


# ---------------------------------------------------------------- Anthropic Messages


def test_anthropic_tool_input_cut_off_mid_json_is_an_output_limit_error() -> None:
    client = _anthropic(
        lambda _request: _anthropic_tool_stream(
            partial_json='{"path": "budget.py", "content": "import openpyxl\\nwb = ',
            stop_reason="max_tokens",
        )
    )

    with pytest.raises(LLMOutputLimitError) as caught:
        client.chat(messages=[{"role": "user", "content": "Build the budget."}], stream=True)

    # Still an LLMError for callers that catch that, as before.
    assert isinstance(caught.value, LLMError)
    response = caught.value.response
    assert response.content == "Writing the workbook script."
    assert response.tool_calls == []
    assert _replayed_assistant_blocks(response) == [
        [{"type": "text", "text": "Writing the workbook script."}]
    ]


def test_anthropic_malformed_tool_input_without_a_cut_off_is_still_a_plain_error() -> None:
    client = _anthropic(
        lambda _request: _anthropic_tool_stream(partial_json='{"path": ', stop_reason="tool_use")
    )

    with pytest.raises(LLMError, match="malformed tool input JSON") as caught:
        client.chat(messages=[{"role": "user", "content": "Build the budget."}], stream=True)
    assert not isinstance(caught.value, LLMOutputLimitError)


def test_anthropic_reply_cut_off_while_thinking_is_an_output_limit_error_replaying_nothing() -> (
    None
):
    client = _anthropic(
        lambda _request: _anthropic_json(
            [{"type": "thinking", "thinking": "Planning the sheets", "signature": ""}],
            stop_reason="max_tokens",
        )
    )

    with pytest.raises(LLMOutputLimitError) as caught:
        client.chat(messages=[{"role": "user", "content": "Build the budget."}])

    response = caught.value.response
    assert response.content == ""
    assert response.tool_calls == []
    assert response.usage is not None and response.usage.completion_tokens == 8192
    assert _replayed_assistant_blocks(response) == []


def test_anthropic_cut_off_reply_with_a_complete_tool_call_is_returned_as_before() -> None:
    client = _anthropic(
        lambda _request: _anthropic_json(
            [{"type": "text", "text": "Saving it."}, _ANTHROPIC_WRITE], stop_reason="max_tokens"
        )
    )

    response = client.chat(messages=[{"role": "user", "content": "Save it."}])

    # Other callers get the reply they got before.
    assert [call.name for call in response.tool_calls] == ["fs_write"]
    # The agent turn keeps none of its tool calls and replays only the finished text.
    interrupted = response_cut_off_by_output_limit(response)
    assert interrupted is not None
    assert interrupted.content == "Saving it."
    assert interrupted.tool_calls == []
    assert _replayed_assistant_blocks(interrupted) == [[{"type": "text", "text": "Saving it."}]]
    # The original response is not modified.
    assert [call.name for call in response.tool_calls] == ["fs_write"]


def test_anthropic_short_reply_cut_off_at_a_small_explicit_limit_is_returned_as_before() -> None:
    # Like the prompt-cache keepalive, which asks for a handful of tokens.
    client = _anthropic(
        lambda _request: _anthropic_json(
            [{"type": "text", "text": "Hi"}], stop_reason="max_tokens", output_tokens=16
        )
    )

    response = client.chat(messages=[{"role": "user", "content": "Hi."}], max_tokens=16)

    assert response.content == "Hi"
    assert response.tool_calls == []


def test_anthropic_finished_reply_is_not_treated_as_cut_off() -> None:
    client = _anthropic(
        lambda _request: _anthropic_json(
            [{"type": "text", "text": "Saving it."}, _ANTHROPIC_WRITE], stop_reason="tool_use"
        )
    )

    response = client.chat(messages=[{"role": "user", "content": "Save it."}])

    assert [call.name for call in response.tool_calls] == ["fs_write"]
    assert response_cut_off_by_output_limit(response) is None


# ---------------------------------------------------------------- chat completions


def test_chat_completion_cut_off_is_returned_as_before_and_recognized() -> None:
    client = _compat(
        lambda _request: httpx.Response(
            200,
            json=_completion(
                finish_reason="length", content="Writing it.", tool_calls=[_WRITE_CALL]
            ),
        )
    )

    response = client.chat(messages=[{"role": "user", "content": "Write it."}])

    assert [call.name for call in response.tool_calls] == ["fs_write"]
    interrupted = response_cut_off_by_output_limit(response)
    assert interrupted is not None
    assert interrupted.content == "Writing it."
    assert interrupted.tool_calls == []


@pytest.mark.parametrize(("finish_reason", "cut_off"), [("length", True), ("stop", False)])
def test_streamed_chat_completion_records_why_it_stopped(finish_reason: str, cut_off: bool) -> None:
    response = _compat(lambda _request: _compat_stream(finish_reason)).chat(
        messages=[{"role": "user", "content": "Hi."}], stream=True
    )

    assert response.content == "Thinking"
    assert response.raw["finish_reason"] == finish_reason
    assert (response_cut_off_by_output_limit(response) is not None) is cut_off


def test_chat_completion_finished_normally_is_unchanged() -> None:
    client = _compat(
        lambda _request: httpx.Response(
            200, json=_completion(finish_reason="tool_calls", tool_calls=[_WRITE_CALL])
        )
    )

    response = client.chat(messages=[{"role": "user", "content": "Write it."}])
    assert [call.name for call in response.tool_calls] == ["fs_write"]
    assert response_cut_off_by_output_limit(response) is None


def _recording_compat(
    finish_reasons: list[str], *, completion_tokens: list[int], **kwargs: Any
) -> tuple[OpenAICompatClient, list[dict[str, Any]]]:
    payloads: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        index = min(len(payloads), len(finish_reasons)) - 1
        return httpx.Response(
            200,
            json=_completion(
                finish_reason=finish_reasons[index],
                content="ok",
                completion_tokens=completion_tokens[index],
            ),
        )

    return _compat(handler, **kwargs), payloads


def _named_limit(payload: dict[str, Any]) -> int | None:
    return payload.get("max_tokens", payload.get("max_completion_tokens"))


def test_after_a_cut_off_at_the_provider_default_later_requests_name_more_room() -> None:
    client, payloads = _recording_compat(
        ["length", "length", "stop"],
        completion_tokens=[8192, 16384, 10],
        output_token_ceiling=131072,
    )
    messages = [{"role": "user", "content": "Think hard."}]

    for _ in range(3):
        assert client.chat(messages=messages).content == "ok"

    assert [_named_limit(payload) for payload in payloads] == [None, 16384, 32768]


def test_a_raised_allowance_never_exceeds_the_models_ceiling() -> None:
    client, payloads = _recording_compat(
        ["length", "length", "stop"],
        completion_tokens=[8192, 12000, 10],
        output_token_ceiling=12000,
    )
    messages = [{"role": "user", "content": "Think hard."}]

    for _ in range(3):
        client.chat(messages=messages)

    assert [_named_limit(payload) for payload in payloads] == [None, 12000, 12000]


def test_allowance_stays_with_the_provider_without_a_known_ceiling() -> None:
    client, payloads = _recording_compat(["length", "stop"], completion_tokens=[8192, 10])

    for _ in range(2):
        client.chat(messages=[{"role": "user", "content": "Hi."}])

    assert [_named_limit(payload) for payload in payloads] == [None, None]


def test_a_cut_off_at_a_callers_own_limit_changes_nothing_else() -> None:
    # A small explicit limit (keepalive, diagnostics, reviewers) is the caller's choice.
    client, payloads = _recording_compat(
        ["length", "stop"], completion_tokens=[16, 10], output_token_ceiling=131072
    )

    assert client.chat(messages=[{"role": "user", "content": "Hi."}], max_tokens=16).content == (
        "ok"
    )
    client.chat(messages=[{"role": "user", "content": "Hi."}])

    assert [_named_limit(payload) for payload in payloads] == [16, None]


def test_a_ceiling_recorded_for_another_model_is_not_used() -> None:
    client, payloads = _recording_compat(
        ["length", "stop"], completion_tokens=[8192, 10], output_token_ceiling=131072
    )
    client.model = "another-model"

    for _ in range(2):
        client.chat(messages=[{"role": "user", "content": "Hi."}])

    assert [_named_limit(payload) for payload in payloads] == [None, None]


def test_a_response_that_is_not_an_llm_response_is_left_alone() -> None:
    # Test doubles and wrappers may return other objects; the turn treats them as before.
    double = SimpleNamespace(content="Hi", tool_calls=[], raw={"stop_reason": "max_tokens"})
    assert response_cut_off_by_output_limit(double) is None
    assert response_cut_off_by_output_limit(SimpleNamespace(content="Hi", tool_calls=[])) is None


def test_only_a_catalogued_output_ceiling_is_used() -> None:
    catalogued = ModelMeta(
        model_name="m",
        context_window_tokens=200_000,
        max_output_tokens=131_072,
        source="bundled_catalog",
        field_sources={"max_output_tokens": "bundled_catalog"},
    )
    guessed = ModelMeta(
        model_name="m",
        context_window_tokens=128_000,
        max_output_tokens=32_000,
        source="fallback",
        field_sources={"max_output_tokens": "fallback"},
    )

    assert catalogued_output_ceiling(catalogued) == 131_072
    assert catalogued_output_ceiling(guessed) is None
    assert catalogued_output_ceiling(None) is None


# ---------------------------------------------------------------- the agent turn


def _session(tmp_path: Path) -> Any:
    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", skills_enabled=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=12,
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=False,
        verification_enabled=False,
        enable_compaction=False,
        session_log_dir_override=tmp_path / "sessions",
    )


def _payloads(session: Any, kind: str) -> list[dict[str, Any]]:
    return [
        event["payload"]
        for event in read_session_events(session.store.path)
        if event["type"] == kind
    ]


def _run_turn(tmp_path: Path, client: Any) -> Any:
    session = _session(tmp_path)
    session.stream = False
    session.client = client
    try:
        assert session.run_turn("Hello") == 0
    finally:
        session.close()
    return session


def test_agent_turn_recovers_from_a_cut_off_chat_completion(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert len(requests) <= 2, "unbounded continuation"
        if len(requests) == 1:
            return httpx.Response(
                200, json=_completion(finish_reason="length", tool_calls=[_WRITE_CALL])
            )
        return httpx.Response(
            200, json=_completion(finish_reason="stop", content="Hello.", completion_tokens=2)
        )

    session = _run_turn(tmp_path, _compat(handler))

    assert len(requests) == 2
    assert not (tmp_path / "bad.txt").exists()
    assert not _payloads(session, "tool_result")
    assert [payload["content"] for payload in _payloads(session, "final")] == ["Hello."]
    assert "output token limit" in json.dumps(requests[1]["messages"])
    assert "call_1" not in json.dumps(requests[1]["messages"])


def test_agent_turn_adds_no_empty_message_for_a_reply_cut_off_while_thinking(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert len(requests) <= 2, "unbounded continuation"
        if len(requests) == 1:
            return httpx.Response(200, json=_completion(finish_reason="length", content=""))
        return httpx.Response(
            200, json=_completion(finish_reason="stop", content="Hello.", completion_tokens=2)
        )

    session = _run_turn(tmp_path, _compat(handler))

    assert len(requests) == 2
    assert [payload["content"] for payload in _payloads(session, "final")] == ["Hello."]
    assert "output token limit" in json.dumps(requests[1]["messages"])
    assert not [m for m in requests[1]["messages"] if m.get("role") == "assistant"]


def test_agent_turn_recovers_from_a_cut_off_anthropic_reply(tmp_path: Path) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        assert len(requests) <= 2, "unbounded continuation"
        if len(requests) == 1:
            return _anthropic_json(
                [{**_ANTHROPIC_WRITE, "id": "toolu_3", "input": {"path": "bad.txt"}}],
                stop_reason="max_tokens",
            )
        return _anthropic_json(
            [{"type": "text", "text": "Hello."}], stop_reason="end_turn", output_tokens=2
        )

    session = _run_turn(tmp_path, _anthropic(handler))

    assert len(requests) == 2
    assert not (tmp_path / "bad.txt").exists()
    assert not _payloads(session, "tool_result")
    assert [payload["content"] for payload in _payloads(session, "final")] == ["Hello."]
    assert "output token limit" in json.dumps(requests[1])
    assert "toolu_3" not in json.dumps(requests[1]["messages"])


def test_agent_turn_recovers_from_an_anthropic_tool_input_cut_off_mid_json(
    tmp_path: Path,
) -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/count_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        requests.append(json.loads(request.content))
        assert len(requests) <= 2, "unbounded continuation"
        if len(requests) == 1:
            return _anthropic_tool_stream(
                partial_json='{"path": "bad.txt", "content": "par', stop_reason="max_tokens"
            )
        return _anthropic_text_stream("Hello.")

    session = _session(tmp_path)
    session.stream = True
    session.client = _anthropic(handler)
    try:
        assert session.run_turn("Hello") == 0
    finally:
        session.close()

    assert len(requests) == 2
    assert not (tmp_path / "bad.txt").exists()
    assert not _payloads(session, "tool_result")
    assert "output token limit" in json.dumps(requests[1])
    assert "toolu_1" not in json.dumps(requests[1]["messages"])
    assert "Writing the workbook script." in json.dumps(requests[1]["messages"])
    assert [payload["content"] for payload in _payloads(session, "final")] == ["Hello."]
