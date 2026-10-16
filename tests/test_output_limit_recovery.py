from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from alysis_code import cli as cli_mod
from alysis_code.agent.subagent_execution import (
    _resolve_subagent_final_text,
    _subagent_stop_reason,
)
from alysis_code.agent_loop import create_session
from alysis_code.cancellation import InteractiveCancellationToken
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.metadata import assistant_message_from_response
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.types import LLMError, LLMOutputLimitError
from alysis_code.session_store import read_session_events
from alysis_code.surface import NoopSurface


def _incomplete(*, output: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "id": "resp_limit",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": output
        if output is not None
        else [
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "opaque-state",
                "summary": [{"type": "summary_text", "text": "Planning the change."}],
            }
        ],
        "usage": {
            "input_tokens": 100,
            "output_tokens": 8192,
            "total_tokens": 8292,
            "output_tokens_details": {"reasoning_tokens": 8192},
        },
    }


def _wire_response(data: dict[str, Any], *, stream: bool) -> httpx.Response:
    if not stream:
        return httpx.Response(200, json=data)
    event = "response." + data["status"]
    payload = {"type": event, "response": data}
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode(),
    )


def _client(handler: Any, *, hosted: bool = False) -> OpenAIResponsesClient:
    return OpenAIResponsesClient(
        base_url="https://provider.invalid/v1",
        api_key="test-key",
        model="test-model",
        provider_key="alysis" if hosted else "openai",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("output_kind", ["empty", "reasoning", "text", "partial_tool"])
def test_output_limit_preserves_usage_and_safe_state(stream: bool, output_kind: str) -> None:
    data = _incomplete()
    if output_kind == "empty":
        data["output"] = []
    elif output_kind == "text":
        data["output"].append(
            {
                "type": "message",
                "role": "assistant",
                "status": "incomplete",
                "content": [{"type": "output_text", "text": "The answer starts"}],
            }
        )
    elif output_kind == "partial_tool":
        data["output"].append(
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "fs_write",
                "status": "incomplete",
                "arguments": '{"path":"out.txt"',
            }
        )
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return _wire_response(data, stream=stream)

    with pytest.raises(LLMOutputLimitError, match="output token limit") as caught:
        _client(handler).chat(messages=[{"role": "user", "content": "hello"}], stream=stream)
    assert len(requests) == 1  # This is not a transport retry.
    response = caught.value.response
    assert response.usage is not None
    assert response.usage.completion_tokens == 8192
    assert response.usage.reasoning_tokens == 8192
    assert {key: response.raw[key] for key in data} == data
    assert response.tool_calls == []
    metadata = response.provider_metadata["openai_responses"]
    assert not any(item["type"] == "function_call" for item in metadata["output_items"])
    assert "request_plan" in metadata
    if output_kind == "partial_tool":
        assert "response_id" not in metadata
    if output_kind != "empty":
        assert metadata["output_items"][0]["encrypted_content"] == "opaque-state"
    if output_kind == "text":
        assert response.content == "The answer starts"


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("status,reason", [("failed", None), ("incomplete", "content_filter")])
def test_other_terminal_failures_are_not_recoverable(
    stream: bool, status: str, reason: str
) -> None:
    data = _incomplete()
    data.update(status=status, incomplete_details={"reason": reason})
    client = _client(lambda request: _wire_response(data, stream=stream))
    with pytest.raises(LLMError) as caught:
        client.chat(messages=[{"role": "user", "content": "hello"}], stream=stream)
    assert not isinstance(caught.value, LLMOutputLimitError)


@pytest.mark.parametrize("hosted", [False, True])
def test_interrupted_tool_calls_are_never_replayed(hosted: bool) -> None:
    data = _incomplete()
    # Even valid JSON must not execute when the response was interrupted.
    data["output"].append(
        {
            "type": "function_call",
            "id": "fc_1",
            "call_id": "call_1",
            "name": "fs_write",
            "status": "completed",
            "arguments": '{"path":"out.txt","content":"partial"}',
        }
    )
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=data
            if len(requests) == 1
            else {
                "status": "completed",
                "output_text": "Finished.",
                "output": [],
            },
        )

    client = _client(handler, hosted=hosted)
    messages = [{"role": "user", "content": "write a file"}]
    with pytest.raises(LLMOutputLimitError) as caught:
        client.chat(messages=messages)
    messages.extend(
        [
            assistant_message_from_response(caught.value.response),
            {"role": "system", "content": "Continue with complete calls."},
        ]
    )
    assert client.chat(messages=messages).content == "Finished."
    assert "previous_response_id" not in requests[1]
    assert not any(item.get("type") == "function_call" for item in requests[1]["input"])
    assert any(item.get("encrypted_content") == "opaque-state" for item in requests[1]["input"])


def _session(tmp_path: Path, **kwargs: Any) -> Any:
    return create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", skills_enabled=False),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=kwargs.pop("max_steps", 12),
        no_log=False,
        api_key_override="test-key",
        one_shot_execution=kwargs.pop("one_shot_execution", False),
        verification_enabled=False,
        enable_compaction=False,
        session_log_dir_override=tmp_path / "sessions",
        **kwargs,
    )


def _payloads(session: Any, kind: str) -> list[dict[str, Any]]:
    return [
        event["payload"]
        for event in read_session_events(session.store.path)
        if event["type"] == kind
    ]


@pytest.mark.parametrize("depth", [0, 1], ids=["parent", "child"])
@pytest.mark.parametrize("stream", [False, True])
def test_agent_continues_after_limit_without_executing_interrupted_call(
    tmp_path: Path,
    depth: int,
    stream: bool,
) -> None:
    session = _session(tmp_path, subagent_depth=depth)
    session.stream = stream
    requests: list[dict[str, Any]] = []
    partial = _incomplete()
    partial["output"].append(
        {
            "type": "function_call",
            "call_id": "bad",
            "name": "fs_write",
            "status": "incomplete",
            "arguments": '{"path":"bad.txt","content":"bad"}',
        }
    )

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert len(requests) <= 2, "unbounded continuation"
        data = (
            partial
            if len(requests) == 1
            else {
                "status": "completed",
                "output_text": "Hello.",
                "output": [],
                "usage": {"input_tokens": 100, "output_tokens": 2, "total_tokens": 102},
            }
        )
        return _wire_response(data, stream=bool(body.get("stream")))

    session.client = _client(handler, hosted=True)
    try:
        assert session.run_turn("Hello") == 0
    finally:
        session.close()
    assert len(requests) == 2
    assert not (tmp_path / "bad.txt").exists()
    assert not _payloads(session, "tool_result")
    assert [p["content"] for p in _payloads(session, "final")] == ["Hello."]
    assert _payloads(session, "output_limit_recovery")[0]["retry"] is True
    assert any(
        "output token limit" in str(item.get("content", "")) for item in requests[1]["input"]
    )
    assert any(item.get("encrypted_content") == "opaque-state" for item in requests[1]["input"])
    usage = _payloads(session, "llm_usage")
    assert [item["completion_tokens"] for item in usage] == [8192, 2]


@pytest.mark.parametrize("max_steps,expected_calls", [(1, 1), (12, 3)])
@pytest.mark.parametrize("one_shot", [False, True])
def test_agent_output_limit_recovery_is_bounded(
    tmp_path: Path,
    max_steps: int,
    expected_calls: int,
    one_shot: bool,
) -> None:
    session = _session(tmp_path, max_steps=max_steps, one_shot_execution=one_shot)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert calls <= expected_calls, "unbounded continuation"
        return _wire_response(
            _incomplete(output=[]), stream=bool(json.loads(request.content).get("stream"))
        )

    session.client = _client(handler)
    try:
        assert session.run_turn("Hello") == 0
    finally:
        session.close()
    assert calls == expected_calls
    assert _payloads(session, "output_limit_recovery")[-1]["retry"] is False
    final = _payloads(session, "final")[-1]
    assert final["internal_fallback"] is True
    assert final["degraded_reason"] == "output_token_limit"
    assert "incomplete" in final["content"]
    assert final["stop_reason"] == "output_token_limit"
    assert session.stop_reason == "output_token_limit"
    assert session.last_turn_outcome["outcome"] == "incomplete"


@pytest.mark.parametrize("reply_language", ["", "Greek"])
def test_agent_output_limit_does_not_extend_deadline(tmp_path: Path, reply_language: str) -> None:
    now = [10.0]
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=10.0,
        deadline_monotonic=110.0,
        configured_duration_seconds=100.0,
        clock=lambda: now[0],
    )
    session = _session(tmp_path, execution_deadline=deadline)
    session.cfg.reply_language = reply_language
    session.stream = False
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        now[0] = 111.0
        return httpx.Response(200, json=_incomplete())

    session.client = _client(handler)
    try:
        session.run_turn("Hello")
    finally:
        session.close()
    assert calls == 1
    assert not _payloads(session, "output_limit_recovery")
    assert _payloads(session, "deadline_exhausted")


def test_stream_limit_after_visible_delta_preserves_partial_text_without_replay() -> None:
    events = [
        {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "content": [],
            },
        },
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": "Partial answer",
        },
        {"type": "response.incomplete", "response": _incomplete(output=[])},
    ]
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
            ).encode(),
        )

    deltas: list[str] = []
    with pytest.raises(LLMOutputLimitError) as caught:
        _client(handler).chat(
            messages=[{"role": "user", "content": "hello"}],
            stream=True,
            on_text_delta=deltas.append,
        )
    assert calls == 1
    assert deltas == ["Partial answer"]
    assert caught.value.response.content == "Partial answer"
    assert caught.value.response.usage.completion_tokens == 8192


def test_reasoning_limit_can_continue_using_stored_response() -> None:
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json=_incomplete()
            if len(requests) == 1
            else {
                "status": "completed",
                "output_text": "Hello.",
                "output": [],
            },
        )

    client = _client(handler)
    messages = [{"role": "user", "content": "hello"}]
    with pytest.raises(LLMOutputLimitError) as caught:
        client.chat(messages=messages)
    messages.extend(
        [
            assistant_message_from_response(caught.value.response),
            {"role": "system", "content": "Continue."},
        ]
    )
    client.chat(messages=messages)
    assert requests[1]["previous_response_id"] == "resp_limit"
    assert requests[1]["input"] == [{"role": "system", "content": "Continue."}]


def test_agent_keeps_completed_write_when_following_response_hits_limit(tmp_path: Path) -> None:
    session = _session(tmp_path)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert calls <= 3
        data = {
            "status": "completed",
            "output": [],
            "usage": {"input_tokens": 100, "output_tokens": 10, "total_tokens": 110},
        }
        if calls == 1:
            data["output"] = [
                {
                    "type": "function_call",
                    "call_id": "write_once",
                    "name": "fs_write",
                    "status": "completed",
                    "arguments": json.dumps({"path": "kept.txt", "content": "saved"}),
                }
            ]
        elif calls == 2:
            data = _incomplete()
        else:
            data["output_text"] = "Saved kept.txt."
        return _wire_response(data, stream=bool(json.loads(request.content).get("stream")))

    session.client = _client(handler, hosted=True)
    try:
        assert session.run_turn("Write saved to kept.txt.") == 0
    finally:
        session.close()
    assert calls == 3
    assert (tmp_path / "kept.txt").read_text(encoding="utf-8") == "saved"
    assert len(_payloads(session, "tool_result")) == 1
    assert len(_payloads(session, "output_limit_recovery")) == 1


def test_agent_recovers_from_limit_on_non_streaming_fallback(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.stream = True
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        assert len(requests) <= 3
        if len(requests) == 1:
            return httpx.Response(400, json={"error": {"message": "streaming is not supported"}})
        data = (
            _incomplete()
            if len(requests) == 2
            else {
                "status": "completed",
                "output_text": "Hello.",
                "output": [],
                "usage": {"input_tokens": 100, "output_tokens": 2, "total_tokens": 102},
            }
        )
        return _wire_response(data, stream=bool(body.get("stream")))

    session.client = _client(handler)
    try:
        assert session.run_turn("Hello") == 0
    finally:
        session.close()
    assert len(requests) == 3
    assert requests[0]["stream"] is True
    assert not requests[1].get("stream")
    assert len(_payloads(session, "output_limit_recovery")) == 1
    assert _payloads(session, "final")[-1]["content"] == "Hello."


@pytest.mark.parametrize("hosted", [False, True])
def test_recovery_instruction_does_not_leak_into_later_turn(tmp_path: Path, hosted: bool) -> None:
    session = _session(tmp_path)
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        data = (
            _incomplete()
            if len(requests) == 1
            else {
                "id": "resp_after_recovery",
                "status": "completed",
                "output_text": "Hello.",
                "output": [],
                "usage": {"input_tokens": 100, "output_tokens": 2, "total_tokens": 102},
            }
        )
        return _wire_response(data, stream=bool(body.get("stream")))

    session.client = _client(handler, hosted=hosted)
    try:
        assert session.run_turn("Hello") == 0
        assert session.run_turn("What is two plus two?") == 0
    finally:
        session.close()
    assert len(requests) == 3
    assert "previous response stopped at the output token limit" in json.dumps(requests[1])
    assert "previous response stopped at the output token limit" not in json.dumps(requests[2])
    # A stored response also retains its input instructions. The next turn must
    # not chain from the recovery response after its temporary prompt is gone.
    assert requests[2].get("previous_response_id") != "resp_after_recovery"
    assert "previous response stopped at the output token limit" not in json.dumps(session.messages)


def test_limit_stop_does_not_make_translation_call(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.cfg.reply_language = "Greek"
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _wire_response(_incomplete(), stream=bool(json.loads(request.content).get("stream")))

    session.client = _client(handler)
    try:
        session.run_turn("Reply in Greek. Explain the options.")
    finally:
        session.close()
    assert calls == 3, "a local stop report must not start another model request"
    assert not _payloads(session, "final_summary_rewrite")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("details", [None, {}, "max_output_tokens"])
def test_unknown_incomplete_status_cannot_become_success(stream: bool, details: Any) -> None:
    data = _incomplete()
    data["incomplete_details"] = details
    data["output_text"] = "Looks complete, but the provider did not finish."
    client = _client(lambda request: _wire_response(data, stream=stream))
    with pytest.raises(LLMError) as caught:
        client.chat(messages=[{"role": "user", "content": "hello"}], stream=stream)
    assert not isinstance(caught.value, LLMOutputLimitError)


def test_recovery_obeys_user_cancellation(tmp_path: Path) -> None:
    session = _session(tmp_path)
    token = InteractiveCancellationToken()
    calls = 0

    class CancellingSurface(NoopSurface):
        def on_progress_update(self, text: str) -> None:
            if text.startswith("Response token limit reached"):
                token.cancel()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _wire_response(_incomplete(), stream=bool(json.loads(request.content).get("stream")))

    session.surface = CancellingSurface()
    session.client = _client(handler)
    try:
        with pytest.raises(KeyboardInterrupt):
            session.run_turn("Hello", cancellation_token=token)
    finally:
        session.close()
    assert calls == 1
    assert session.last_turn_outcome["outcome"] == "cancelled"


def test_incomplete_reasoning_state_is_not_sent_to_other_credentials() -> None:
    first_client = _client(lambda request: httpx.Response(200, json=_incomplete()))
    messages = [{"role": "user", "content": "hello"}]
    with pytest.raises(LLMOutputLimitError) as caught:
        first_client.chat(messages=messages)
    messages.extend(
        [
            assistant_message_from_response(caught.value.response),
            {"role": "system", "content": "Continue."},
        ]
    )
    requests: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"status": "completed", "output_text": "Hello."})

    other_client = OpenAIResponsesClient(
        base_url="https://provider.invalid/v1",
        api_key="other-test-key",
        model="test-model",
        provider_key="openai",
        transport=httpx.MockTransport(handler),
    )
    other_client.chat(messages=messages)
    assert "opaque-state" not in json.dumps(requests)
    assert "previous_response_id" not in requests[0]


def test_output_limit_recovery_after_context_overflow(tmp_path: Path) -> None:
    session = _session(tmp_path)
    compactions: list[bool] = []

    def compact_for_overflow(**kwargs: Any) -> tuple[list[dict[str, Any]], bool]:
        compactions.append(True)
        return kwargs["messages"], True

    session.conversation_compactor = SimpleNamespace(
        maybe_compact=lambda **kwargs: (kwargs["messages"], False),
        compact_for_overflow=compact_for_overflow,
        request_fits_input_budget=lambda **kwargs: True,
    )
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        assert calls <= 3
        if calls == 1:
            return httpx.Response(400, json={"error": {"message": "context_length_exceeded"}})
        data = (
            _incomplete()
            if calls == 2
            else {
                "status": "completed",
                "output_text": "Hello.",
                "output": [],
                "usage": {"input_tokens": 100, "output_tokens": 2, "total_tokens": 102},
            }
        )
        return _wire_response(data, stream=bool(json.loads(request.content).get("stream")))

    session.client = _client(handler, hosted=True)
    try:
        assert session.run_turn("Hello") == 0
    finally:
        session.close()
    assert calls == 3
    assert compactions == [True]
    assert len(_payloads(session, "output_limit_recovery")) == 1
    assert [item["completion_tokens"] for item in _payloads(session, "llm_usage")] == [8192, 2]


def test_successful_next_turn_clears_previous_limit_stop(tmp_path: Path) -> None:
    session = _session(tmp_path, max_steps=1)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        data = (
            _incomplete()
            if calls == 1
            else {
                "status": "completed",
                "output_text": "Hello.",
                "output": [],
                "usage": {"input_tokens": 100, "output_tokens": 2, "total_tokens": 102},
            }
        )
        return _wire_response(data, stream=bool(json.loads(request.content).get("stream")))

    session.client = _client(handler)
    try:
        assert session.run_turn("Hello") == 0
        assert session.stop_reason == "output_token_limit"
        assert session.run_turn("Hello again") == 0
        assert session.stop_reason is None
        assert session.last_turn_outcome["outcome"] == "completed_unverified"
    finally:
        session.close()


def test_child_token_limit_is_an_incomplete_result_with_the_correct_cause(tmp_path: Path) -> None:
    session = _session(tmp_path, max_steps=1, subagent_depth=1)
    session.client = _client(
        lambda request: _wire_response(
            _incomplete(), stream=bool(json.loads(request.content).get("stream"))
        )
    )
    try:
        assert session.run_turn("Hello") == 0
        _, source = _resolve_subagent_final_text(
            sub_session=session, subagent_surface=SimpleNamespace(last_assistant_message_done="")
        )
        assert source == "internal_fallback"
        assert (
            _subagent_stop_reason(sub_session=session, deadline_exhausted=False)
            == "output_token_limit"
        )
    finally:
        session.close()


def test_resume_preserves_reasoning_without_orphan_calls_or_recovery_instructions(
    tmp_path: Path,
) -> None:
    session = _session(tmp_path, max_steps=1)
    partial = _incomplete()
    partial["output"].append(
        {
            "type": "function_call",
            "name": "fs_write",
            "call_id": "unfinished",
            "arguments": '{"path":"bad.txt"',
            "status": "incomplete",
        }
    )
    session.client = _client(
        lambda request: _wire_response(
            partial, stream=bool(json.loads(request.content).get("stream"))
        ),
        hosted=True,
    )
    try:
        session.run_turn("Hello")
    finally:
        session.close()
    synthesized: list[str] = []
    restored = cli_mod._load_chat_resume_messages(
        session.store.path, synthesized_tool_results=synthesized
    )
    assert "opaque-state" in json.dumps(restored)
    assert "unfinished" not in json.dumps(restored)
    assert synthesized == []
    assert not any(message.get("tool_calls") for message in restored)
    assert "previous response stopped at the output token limit" not in json.dumps(restored)
