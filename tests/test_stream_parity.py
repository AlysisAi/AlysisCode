"""The streaming and non-streaming paths return the same result.

Each fixture in ``tests/fixtures/stream_parity`` is a pair: an SSE body and the
chat-completions JSON body a provider would return for the same reply. The
pairs are synthetic, written in the shape of MiMo token-plan responses; no live
provider was called to make them. Both bodies go through ``OpenAICompatClient.chat``
and the two ``LLMResponse`` objects must be equal in every field except ``raw``,
which by design holds the whole JSON body on one path and an event count on
the other.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import httpx
import pytest

from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.types import LLMError, LLMResponse

_FIXTURES = Path(__file__).parent / "fixtures" / "stream_parity"
_HOSTS = [
    pytest.param("https://example.com/v1", id="generic_host"),
    pytest.param("https://token-plan-ams.xiaomimimo.com/v1", id="mimo_token_plan"),
]


def _client(base_url: str, body: bytes) -> OpenAICompatClient:
    def handler(request: httpx.Request) -> httpx.Response:
        streaming = bool(json.loads(request.content).get("stream"))
        content_type = "text/event-stream" if streaming else "application/json"
        return httpx.Response(200, headers={"content-type": content_type}, content=body)

    return OpenAICompatClient(
        base_url=base_url,
        api_key="test",
        model="mimo-v2.5",
        enable_thinking=True,
        transport=httpx.MockTransport(handler),
        provider_sleep_fn=lambda _seconds: None,
    )


def _chat(base_url: str, fixture: str, *, stream: bool) -> LLMResponse:
    body = (_FIXTURES / fixture).read_bytes()
    return _client(base_url, body).chat(messages=[{"role": "user", "content": "hi"}], stream=stream)


def _without_raw(response: LLMResponse) -> LLMResponse:
    return dataclasses.replace(response, raw={})


@pytest.mark.parametrize("base_url", _HOSTS)
@pytest.mark.parametrize(
    "name",
    [
        "plain_text",
        "reasoning_and_text",
        "two_tool_calls",
        "long_tool_arguments",
        "usage_on_last_chunk",
    ],
)
def test_stream_and_json_bodies_produce_equal_responses(base_url: str, name: str) -> None:
    streamed = _chat(base_url, f"{name}.sse", stream=True)
    whole = _chat(base_url, f"{name}.json", stream=False)

    assert _without_raw(streamed) == _without_raw(whole)
    assert streamed.raw["stream"] is True


def test_fixture_contents_are_what_the_parity_cases_claim() -> None:
    base_url = "https://token-plan-ams.xiaomimimo.com/v1"
    long_args = (_FIXTURES / "long_tool_arguments.sse").read_text(encoding="utf-8")
    assert long_args.count('"arguments":') >= 51

    assert _chat(base_url, "plain_text.sse", stream=True).content == "Hello, world."

    reasoning = _chat(base_url, "reasoning_and_text.sse", stream=True)
    assert reasoning.content == "Hi there!"
    assert reasoning.provider_metadata

    calls = _chat(base_url, "two_tool_calls.sse", stream=True).tool_calls
    assert [(c.id, c.arguments) for c in calls] == [
        ("call_a", {"path": "a.py"}),
        ("call_b", {"path": "b.py", "max_bytes": 10}),
    ]

    written = _chat(base_url, "long_tool_arguments.sse", stream=True).tool_calls[0].arguments
    assert written["content"].count("        pass\n") == 2
    assert "LAUGH = 'haha'" in written["content"]
    assert "PAD = 'aaaaaaaa'" in written["content"]

    usage = _chat(base_url, "usage_on_last_chunk.sse", stream=True).usage
    assert usage is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.total_tokens) == (1234, 56, 1290)


@pytest.mark.parametrize("base_url", _HOSTS)
def test_a_truncated_reply_yields_no_response_on_either_path(base_url: str) -> None:
    with pytest.raises(LLMError, match="stream truncated"):
        _chat(base_url, "truncated_no_done.sse", stream=True)
    with pytest.raises(LLMError, match="non-JSON"):
        _chat(base_url, "truncated_no_done.partial.json", stream=False)
