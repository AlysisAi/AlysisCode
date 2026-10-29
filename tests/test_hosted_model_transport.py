import httpx
import pytest

from alysis_code.llm.metadata import assistant_message_from_response
from alysis_code.llm.openai_compat import (
    OpenAICompatClient,
    _messages_for_transport,
    _transport_provider_key,
)

BASE = "https://backend.example.invalid/functions/v1/llm/v1"


@pytest.fixture(autouse=True)
def configured_gateway(monkeypatch):
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", BASE)


MODELS = [
    "minimax-m3.1-flash",
    "gpt-oss-120b",
    "reflection-beam",
    "nemotron-3.5-super-vl",
    "glm-5.3",
    "qwen3.8-2.4t",
    "kimi-k3",
    "muse-spark-open",
]


@pytest.mark.parametrize("model", MODELS)
def test_expanded_hosted_models_use_their_own_contract_and_replay_final_reasoning(model):
    assert _transport_provider_key(base_url=BASE, provider_key="alysis", model=model) == "alysis"
    response = OpenAICompatClient._parse_non_stream_response(
        httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "Done",
                            "reasoning_content": "opaque",
                        }
                    }
                ]
            },
        ),
        provider_key="alysis",
        model=model,
    )
    messages = [
        {"role": "system", "content": "original"},
        {"role": "user", "content": "task"},
        assistant_message_from_response(response),
        {"role": "system", "content": "verify"},
    ]
    result = _messages_for_transport(
        messages, provider_key="alysis", reasoning_provider_key="alysis", model=model
    )
    assistant = next(m for m in result if m["role"] == "assistant")
    assert assistant["reasoning_content"] == "opaque"
    assert "_alysis_provider_metadata" not in assistant
    assert messages[-1]["content"] == "verify"  # No history mutation.
    if model in {"qwen3.8-2.4t", "reflection-beam"}:
        assert result[0] == {"role": "system", "content": "original\n\nverify"}
        assert not any(m["role"] == "system" for m in result[1:])
    else:
        assert result[-1] == messages[-1]


def test_legacy_and_direct_provider_routing_are_unchanged():
    assert (
        _transport_provider_key(base_url=BASE, provider_key="alysis", model="deepseek-flash")
        == "deepseek"
    )
    assert (
        _transport_provider_key(
            base_url="https://api.openai.com/v1", provider_key="openai", model="gpt-oss-120b"
        )
        == "openai"
    )


@pytest.mark.parametrize("model", MODELS)
def test_hosted_models_send_only_the_verified_low_reasoning_effort(model):
    sent = []

    def handle(request):
        import json

        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": "ok",
                            "reasoning_content": "opaque",
                        }
                    }
                ]
            },
        )

    client = OpenAICompatClient(
        base_url=BASE,
        api_key="test",
        model=model,
        provider_key="alysis",
        reasoning_effort="low",
        transport=httpx.MockTransport(handle),
    )
    client.chat(messages=[{"role": "user", "content": "hello"}], max_tokens=64)
    assert sent[0]["reasoning_effort"] == "low"
    assert "thinking" not in sent[0]
