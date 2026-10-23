from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from alysis_code.llm.openai_responses import (
    OpenAIResponsesClient,
    _response_from_json,
    _responses_call_id,
    _stable_request_signature,
)
from alysis_code.unicode_text import well_formed_text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ελληνικά 🧾 é", "Ελληνικά 🧾 é"),
        ("\ud835", "�"),
        ("\udc00", "�"),
        ("a\ud835b\udc00c", "a�b�c"),
        ("\ud83e\uddfe", "🧾"),
    ],
)
def test_observation_text_only_replaces_isolated_surrogates(raw: str, expected: str) -> None:
    assert well_formed_text(raw) == expected


def test_signatures_and_opaque_call_ids_are_lossless_for_surrogates() -> None:
    values = ["\ud835", "\\ud835", "�", "Ελληνικά 🧾"]
    assert len({_stable_request_signature({"value": value}) for value in values}) == len(values)
    assert len({_responses_call_id(value, fallback="call") for value in values}) == len(values)
    raw = "Ελληνικά 🧾"
    assert (
        _stable_request_signature({"value": raw})
        == hashlib.sha256(
            json.dumps(
                {"value": raw}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
    )
    prefix = "alysis_"
    normalized = _responses_call_id(raw, fallback="call")
    assert (
        normalized == prefix + hashlib.sha256(raw.encode("utf-8")).hexdigest()[: 64 - len(prefix)]
    )
    assert len(normalized) == 64
    data = {"id": "opaque-\ud835", "text": "Ελληνικά 🧾 \udc00"}
    assert _response_from_json(data).json() == data


@pytest.mark.parametrize("stream", [False, True])
def test_responses_wire_serialization_and_sse_reconstruction_preserve_opaque_unicode(
    stream: bool,
) -> None:
    sent: list[dict] = []
    raw_id = "opaque-call-\ud835"
    argument = "opaque-argument-\udc00 Ελληνικά 🧾"
    item = {
        "type": "function_call",
        "id": "function-1",
        "call_id": raw_id,
        "name": "fixture",
        "arguments": json.dumps({"value": argument}),
    }
    response = {"id": "response-1", "model": "test-model", "output": [item]}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["content-type"] == "application/json"
        request.content.decode("utf-8", errors="strict")
        sent.append(json.loads(request.content))
        if stream:
            events = [
                {"type": "response.output_item.done", "output_index": 0, "item": item},
                {"type": "response.completed", "response": response},
            ]
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=b"".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode("ascii")
                    for event in events
                ),
            )
        return httpx.Response(
            200,
            content=json.dumps(response).encode("ascii"),
            headers={"content-type": "application/json"},
        )

    client = OpenAIResponsesClient(
        base_url="https://synthetic.invalid/v1",
        api_key="fixture",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    prompt = "untrusted text \ud835 / \udc00 Ελληνικά 🧾"
    result = client.chat(
        messages=[{"role": "user", "content": prompt}],
        tools=[
            {"type": "function", "function": {"name": "fixture", "parameters": {"type": "object"}}}
        ],
        stream=stream,
    )
    assert sent[0]["input"][0]["content"] == prompt
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == _responses_call_id(raw_id, fallback="call")
    assert result.tool_calls[0].arguments == {"value": argument}
