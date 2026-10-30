"""Exercise gate retries through real provider adapters with synthetic wire replies."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from alysis_code.agent_loop import create_session
from alysis_code.cli_impl.tui.app import _duplicate_assistant_indices
from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.config import AppConfig
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.session_store import read_session_events

_ROUTES = [
    ("compat", "deepseek-chat", OpenAICompatClient),
    ("compat", "mimo-v2.5", OpenAICompatClient),
    ("responses", "gpt-5.5", OpenAIResponsesClient),
    ("anthropic", "claude-sonnet-5-5", AnthropicMessagesClient),
    ("gemini", "gemini-3-flash-preview", GeminiGenerateContentClient),
]


def _wire_reply(route: str, text: str, *, stream: bool, request_id: int) -> httpx.Response:
    chunks = [text[: len(text) // 2], text[len(text) // 2 :]]
    if route == "compat":
        body = {
            "choices": [
                {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ]
        }
        events = [{"choices": [{"delta": {"content": chunk}}]} for chunk in chunks]
        events.append({"choices": [{"delta": {}, "finish_reason": "stop"}]})
    elif route == "responses":
        body = {
            "id": f"resp_{request_id}",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": f"msg_{request_id}",
                    "role": "assistant",
                    "phase": "final_answer",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
        }
        events = [
            {
                "type": "response.output_text.delta",
                "output_index": 0,
                "content_index": 0,
                "delta": chunk,
            }
            for chunk in chunks
        ]
        events.append({"type": "response.completed", "response": body})
    elif route == "anthropic":
        body = {
            "id": f"msg_{request_id}",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }
        events = [
            {"type": "message_start", "message": {**body, "content": [], "stop_reason": None}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
            *[
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": chunk},
                }
                for chunk in chunks
            ],
            {"type": "content_block_stop", "index": 0},
            {
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn"},
                "usage": {"output_tokens": 1},
            },
            {"type": "message_stop"},
        ]
    else:
        body = {
            "candidates": [
                {"content": {"role": "model", "parts": [{"text": text}]}, "finishReason": "STOP"}
            ]
        }
        events = [
            {"candidates": [{"content": {"role": "model", "parts": [{"text": chunk}]}}]}
            for chunk in chunks
        ]
        events[-1]["candidates"][0]["finishReason"] = "STOP"
    if not stream:
        return httpx.Response(200, json=body)
    wire = "".join(
        (f"event: {event['type']}\n" if "type" in event else "") + f"data: {json.dumps(event)}\n\n"
        for event in events
    )
    if route == "compat":
        wire += "data: [DONE]\n\n"
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire.encode())


@pytest.mark.parametrize("route,model,client_type", _ROUTES, ids=[r[1] for r in _ROUTES])
@pytest.mark.parametrize("stream", [False, True])
def test_gate_repeated_answers_are_visible_once_across_adapters(
    tmp_path: Path,
    route,
    model,
    client_type,
    stream,
):
    transcript = TuiTranscript()
    transcript.append_user("Create out.txt containing hello.")
    views = []

    def observe():
        entries, _, live = transcript.snapshot()
        hidden = _duplicate_assistant_indices(entries, live)
        views.append(
            [
                text
                for index, (role, text) in enumerate(entries)
                if role == "assistant" and text.strip() and index not in hidden
            ]
        )

    transcript.set_invalidate(observe)
    main_calls = 0
    request_count = 0

    def handler(request):
        nonlocal main_calls, request_count
        if str(request.url).endswith("/responses/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        if ":countTokens" in str(request.url):
            return httpx.Response(200, json={"totalTokens": 100})
        request_count += 1
        payload = json.loads(request.content)
        if route == "anthropic":
            assert payload["messages"][-1]["role"] == "user", "Claude rejects assistant prefill"
        disposition = "classify one assistant reply" in json.dumps(payload)
        if not disposition:
            main_calls += 1
            assert main_calls <= 3, "completion gate must not loop on unchanged evidence"
        streaming = bool(payload.get("stream")) or "streamGenerateContent" in str(request.url)
        return _wire_reply(
            route,
            "WORK_CLAIM" if disposition else "Created out.txt.",
            stream=streaming,
            request_id=request_count,
        )

    session = create_session(
        cfg=AppConfig(
            model="test-model", routing_mode="code_only", stream=stream, skills_enabled=False
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=6,
        no_log=False,
        api_key_override="test-key",
        verification_enabled=False,
        enable_compaction=False,
        enable_chat_turn_step_budget=True,
        surface=TuiSurface(transcript),
        session_log_dir_override=tmp_path / "sessions",
    )
    session.client = client_type(
        base_url="https://provider.invalid/v1",
        api_key="test-key",
        model=model,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert session.run_turn("Create out.txt containing hello.") == 0
    finally:
        session.close()
    assert main_calls >= 2, "fixture must actually exercise completion-gate rejection"
    assert not (tmp_path / "out.txt").exists()
    assert all(len(view) <= 1 for view in views), views
    final = next(
        e["payload"]["content"]
        for e in read_session_events(session.store.path)
        if e["type"] == "final"
    )
    assert "Not verified: some task requirements or checks remain unconfirmed." in final
    assert views[-1] == [final]


@pytest.mark.parametrize("trace", [False, True])
def test_two_transport_retries_replace_abandoned_streams(tmp_path: Path, trace):
    transcript = TuiTranscript()
    transcript.append_user("Answer the question")
    attempts = 0
    views = []

    def observe():
        entries, _, live = transcript.snapshot()
        hidden = _duplicate_assistant_indices(entries, live)
        views.append(
            [
                text
                for i, (role, text) in enumerate(entries)
                if role == "assistant" and text.strip() and i not in hidden
            ]
        )

    transcript.set_invalidate(observe)

    class InterruptedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"Abandoned answer."}}]}\n\n'
            if trace:
                transcript.append_trace("Background progress")
            raise httpx.RemoteProtocolError(
                "peer closed connection without sending complete message body"
            )

    def handler(request):
        nonlocal attempts
        attempts += 1
        assert attempts <= 3
        if attempts < 3:
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=InterruptedStream()
            )
        return _wire_reply("compat", "The complete answer.", stream=True, request_id=3)

    session = create_session(
        cfg=AppConfig(
            model="test-model", routing_mode="code_only", stream=True, skills_enabled=False
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=3,
        no_log=True,
        api_key_override="test-key",
        verification_enabled=False,
        enable_compaction=False,
        surface=TuiSurface(transcript),
    )
    session.client = OpenAICompatClient(
        base_url="https://provider.invalid/v1",
        api_key="test-key",
        model="deepseek-chat",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=2),
        provider_sleep_fn=lambda _: None,
        provider_random_fn=lambda: 0.5,
    )
    try:
        assert session.run_turn("Answer the question") == 0
    finally:
        session.close()
    assert attempts == 3
    assert all(len(view) <= 1 for view in views)
    assert views[-1] == ["The complete answer."]
