import json
from pathlib import Path

import httpx
import pytest

from alysis_code.failure_category import is_context_window_exceeded_error
from alysis_code.llm.hosted_request_limits import hosted_chat_input_bound
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.types import LLMError


def test_hosted_bound_matches_gateway_fixtures():
    fixtures = json.loads(
        (Path(__file__).parent / "fixtures/hosted_request_capacity.json").read_text(
            encoding="utf-8"
        )
    )
    for case in fixtures:
        assert hosted_chat_input_bound(case["payload"]) == case["bound"]


@pytest.mark.parametrize("model", ["deepseek-flash", "glm-5.3-flash"])
def test_hosted_preflight_rejects_context_before_http_without_inflating_token_usage(model):
    calls = []
    client = OpenAICompatClient(
        base_url="https://gateway.test/v1",
        api_key="test",
        model=model,
        provider_key="alysis",
        transport=httpx.MockTransport(lambda request: calls.append(request)),
    )
    messages = [{"role": "user", "content": "a" * 530000}]
    assert client.request_capacity_ratio(messages=messages) > 1
    assert client.count_input_tokens(messages=messages).input_tokens < 200000
    with pytest.raises(LLMError, match="context_length_exceeded") as exc:
        client.chat(messages=messages)
    assert is_context_window_exceeded_error(exc.value)
    assert calls == []


def test_direct_deepseek_route_keeps_its_native_capacity():
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com",
        api_key="test",
        model="deepseek-flash",
        transport=httpx.MockTransport(respond),
    )
    messages = [{"role": "user", "content": "a" * 530000}]
    assert client.request_capacity_ratio(messages=messages) == 0
    assert client.chat(messages=messages).content == "ok"
    assert len(calls) == 1


def test_body_overflow_triggers_recovery_but_generic_validation_does_not():
    assert is_context_window_exceeded_error('LLM error 413: {"code":"hosted_request_too_large"}')
    assert not is_context_window_exceeded_error(
        'LLM error 400: {"code":"invalid_request_error","message":"Unknown hosted model."}'
    )
