"""Native reasoning formats must reach the TUI without changing provider replay."""

from __future__ import annotations

import copy
import json

import httpx
import pytest

from alysis_code.cli_impl.tui.surface import TuiSurface
from alysis_code.cli_impl.tui.transcript import TuiTranscript
from alysis_code.llm.metadata import assistant_message_from_response
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.protocols import resolve_reasoning_trace_capability
from alysis_code.llm.reasoning_trace import CompatibleReasoningTrace
from alysis_code.llm.types import ReasoningOutputKind

_THOUGHT = "Check the input.\nThen answer."
_ANSWER = "Done."
_ROUTES = [
    ("deepseek", "https://api.deepseek.com/v1", "deepseek-reasoner", "field"),
    ("qwen", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "qwen3.7-plus", "field"),
    ("moonshot", "https://api.moonshot.ai/v1", "kimi-k2.5", "field"),
    ("kimi-code", "https://api.kimi.com/coding/v1", "kimi-for-coding", "field"),
    ("zhipu", "https://open.bigmodel.cn/api/paas/v4", "glm-4.7", "field"),
    ("zai_coding_plan", "https://api.z.ai/api/coding/paas/v4", "glm-5.3", "field"),
    ("nvidia", "https://integrate.api.nvidia.com/v1", "moonshotai/kimi-k2.5", "field"),
    ("mistral", "https://api.mistral.ai/v1", "mistral-medium-3-5", "thinking"),
    ("openrouter", "https://openrouter.ai/api/v1", "deepseek/deepseek-r1", "details"),
]


def _fields(format: str, text: str) -> dict:
    if format == "thinking":
        return {
            "content": [
                {
                    "type": "thinking",
                    "thinking": [{"type": "text", "text": text}],
                    "signature": "OPAQUE_SIGNATURE",
                }
            ]
        }
    if format == "details":
        return {
            "reasoning": text,
            "reasoning_details": [
                {"type": "reasoning.text", "index": 0, "text": text},
                {"type": "reasoning.encrypted", "index": 1, "data": "OPAQUE_ENCRYPTED"},
            ],
        }
    return {"reasoning_content": text}


def _client(route: tuple, requests: list, *, adapter: str = "auto") -> OpenAICompatClient:
    provider, url, model, format = route

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        requests.append(payload)
        if payload.get("stream"):
            events = [
                {"choices": [{"delta": _fields(format, piece)}]}
                for piece in ["Check the input.", "\n", "Then answer."]
            ]
            events.append({"choices": [{"delta": {"content": _ANSWER}, "finish_reason": "stop"}]})
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                text="".join(f"data: {json.dumps(event)}\n\n" for event in events)
                + "data: [DONE]\n\n",
            )
        message = {"role": "assistant", **_fields(format, _THOUGHT)}
        if format == "thinking":
            message["content"].append({"type": "text", "text": _ANSWER})
        else:
            message["content"] = _ANSWER
        return httpx.Response(
            200, json={"choices": [{"message": message, "finish_reason": "stop"}]}
        )

    client = OpenAICompatClient(
        base_url=url,
        api_key="synthetic",
        model=model,
        provider_key=provider,
        reasoning_effort="high",
        reasoning_trace_adapter=adapter,
        transport=httpx.MockTransport(handler),
    )
    client.reasoning_trace_capability = resolve_reasoning_trace_capability(
        provider_key=provider, protocol="openai_compat", adapter_override=adapter
    )
    return client


@pytest.mark.parametrize("route", _ROUTES, ids=lambda route: route[0])
@pytest.mark.parametrize("stream", [False, True])
def test_native_trace_visibility_does_not_change_requests_or_continuation(route, stream):
    wire_by_level = []
    for level in ["off", "compact", "full"]:
        requests = []
        client = _client(route, requests)
        transcript = TuiTranscript()
        surface = TuiSurface(transcript)
        surface.set_trace_level(level)
        messages = [{"role": "user", "content": "Inspect the input."}]
        response = client.chat(
            messages=messages,
            stream=stream,
            on_reasoning_delta=surface.on_reasoning_token
            if surface.reasoning_trace_enabled
            else None,
        )
        surface.on_reasoning_end("")
        assert response.content == _ANSWER
        assert [item.text for item in response.reasoning] == [_THOUGHT]
        assert response.reasoning[0].kind == ReasoningOutputKind.PROVIDER_REASONING
        assert [text for role, text in transcript.entries if role == "reasoning"] == (
            [_THOUGHT] if level != "off" else []
        )
        assert "OPAQUE" not in str(transcript.entries)
        assistant = assistant_message_from_response(response)
        client.chat(
            messages=[*messages, assistant, {"role": "user", "content": "Continue."}], stream=stream
        )
        wire_by_level.append(requests)
    assert wire_by_level[0] == wire_by_level[1] == wire_by_level[2]


@pytest.mark.parametrize("route", _ROUTES, ids=lambda route: route[0])
@pytest.mark.parametrize("stream", [False, True])
def test_explicit_none_disables_native_trace_at_transport_boundary(route, stream):
    client = _client(route, [], adapter="none")
    displayed = []
    response = client.chat(
        messages=[{"role": "user", "content": "Hello"}],
        stream=stream,
        on_reasoning_delta=displayed.append,
    )
    assert displayed == []
    assert response.reasoning == ()
    assert response.content == _ANSWER


@pytest.mark.parametrize("chat_only", [False, True])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("level", ["off", "compact", "full"])
def test_native_reasoning_reaches_real_agent_turn_and_tui(tmp_path, chat_only, stream, level):
    from test_agent_loop_provider_behaviors import _session

    from alysis_code.config import AppConfig

    transcript = TuiTranscript()
    surface = TuiSurface(transcript)
    surface.set_trace_level(level)
    session = _session(tmp_path, cfg=AppConfig(model="test-model", stream=stream), surface=surface)
    session.client = _client(_ROUTES[0], [])
    try:
        assert session.run_turn("Hello", chat_only=chat_only) == 0
    finally:
        session.close()
    assert [text for role, text in transcript.entries if role == "reasoning"] == (
        [_THOUGHT] if level != "off" else []
    )
    assert ("assistant", _ANSWER) in transcript.entries
    assert transcript.reasoning_snapshot()[0] is None


@pytest.mark.parametrize("chat_only", [False, True])
def test_interrupted_native_reasoning_closes_its_block(tmp_path, chat_only):
    from test_agent_loop_provider_behaviors import _ReasoningCaptureSurface, _session

    class InterruptedClient:
        model = "deepseek-reasoner"
        reasoning_trace_capability = resolve_reasoning_trace_capability(
            provider_key="deepseek", protocol="openai_compat"
        )

        def chat(self, *, on_reasoning_delta=None, **kwargs):
            on_reasoning_delta("Partial reasoning.")
            raise KeyboardInterrupt()

    surface = _ReasoningCaptureSurface()
    session = _session(tmp_path, surface=surface)
    session.client = InterruptedClient()
    try:
        with pytest.raises(KeyboardInterrupt):
            session.run_turn("Hello", chat_only=chat_only)
    finally:
        session.close()
    assert surface.reasoning_deltas == ["Partial reasoning."]
    assert len(surface.reasoning_starts) == 1
    assert surface.reasoning_ends == surface.reasoning_starts


@pytest.mark.parametrize("assembly", ["concat", "cumulative"])
def test_native_fragments_preserve_repeated_words_and_whitespace(assembly):
    trace = CompatibleReasoningTrace(
        "deepseek_reasoning", provider="deepseek", cumulative=assembly == "cumulative"
    )
    pieces = ["Check", " ", "Check", "\n", "again."]
    accumulated = ""
    displayed = []
    for piece in pieces:
        accumulated += piece
        displayed.extend(
            item.text
            for item in trace.feed(
                {"reasoning_content": accumulated if assembly == "cumulative" else piece}
            )
        )
    assert displayed == pieces
    assert [item.text for item in trace.outputs] == ["Check Check\nagain."]


@pytest.mark.parametrize("structured_first", [False, True])
def test_alternate_reasoning_carriers_do_not_duplicate_text(structured_first):
    trace = CompatibleReasoningTrace("openrouter_reasoning", provider="openrouter")
    field = {"reasoning": _THOUGHT, "reasoning_content": _THOUGHT, "reasoning_text": _THOUGHT}
    structured = {"reasoning_details": [{"type": "reasoning.text", "text": _THOUGHT, "index": 0}]}
    packets = [structured, field] if structured_first else [field, structured]
    before = copy.deepcopy(packets)
    displayed = [item.text for packet in packets for item in trace.feed(packet)]
    assert displayed == [_THOUGHT]
    assert packets == before


def test_opaque_details_do_not_hide_fallback_text_or_enter_trace():
    trace = CompatibleReasoningTrace("openrouter_reasoning", provider="openrouter")
    packet = {
        "reasoning": _THOUGHT,
        "reasoning_details": [
            {"type": "reasoning.encrypted", "data": "opaque"},
            {"type": "reasoning.redacted", "text": "redacted"},
        ],
    }
    assert [item.text for item in trace.feed(packet)] == [_THOUGHT]
    assert [item.text for item in trace.outputs] == [_THOUGHT]


def test_summary_takes_precedence_over_simultaneous_reasoning_representations():
    trace = CompatibleReasoningTrace("openrouter_reasoning", provider="openrouter")
    packet = {
        "reasoning": _THOUGHT,
        "reasoning_details": [
            {"type": "reasoning.text", "text": _THOUGHT, "index": 0},
            {"type": "reasoning.summary", "summary": "Checked input.", "index": 1},
        ],
    }
    output = trace.feed(packet)
    assert [item.text for item in output] == ["Checked input."]
    assert output[0].kind == ReasoningOutputKind.SUMMARY
    assert trace.feed({"reasoning_details": [{"type": "reasoning.text", "text": _THOUGHT}]}) == ()


@pytest.mark.parametrize("adapter", ["none", "openai_compat_passive", "unknown"])
def test_unknown_or_disabled_adapters_do_not_guess_reasoning_from_answers(adapter):
    trace = CompatibleReasoningTrace(adapter, provider="custom")
    assert (
        trace.feed(
            {
                "content": "<think>Answer content</think>",
                "reasoning_content": "Native text",
                "reasoning": "Alias text",
            }
        )
        == ()
    )
    assert trace.outputs == ()
