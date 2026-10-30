from __future__ import annotations

import json

import httpx
import pytest

from alysis_code.config import AppConfig, set_config_value
from alysis_code.llm.gemini_interactions import (
    GEMINI_INTERACTIONS_CONFIG_FLAG,
    GEMINI_INTERACTIONS_EXPERIMENT_ENV,
    GeminiInteractionsClient,
    gemini_interactions_enabled,
)
from alysis_code.llm.metadata import (
    GEMINI_INTERACTIONS_PROVIDER_METADATA_KEY,
    ROUTE_IDENTITY_PROVIDER_METADATA_KEY,
)
from alysis_code.llm.types import LLMError, ReasoningOutputKind


def test_usage_from_response_folds_thoughts_into_completion() -> None:
    from alysis_code.llm.gemini_interactions import _usage_from_response

    usage = _usage_from_response(
        {
            "usageMetadata": {
                "promptTokenCount": 1000,
                "candidatesTokenCount": 200,
                "thoughtsTokenCount": 2000,
                "totalTokenCount": 3200,
            }
        }
    )
    assert usage is not None
    assert usage.completion_tokens == 200 + 2000
    assert usage.reasoning_tokens == 2000
    assert usage.prompt_tokens == 1000
    assert usage.total_tokens == 3200


def test_usage_from_interactions_schema_folds_total_thought_tokens_into_output() -> None:
    from alysis_code.llm.gemini_interactions import _usage_from_response

    usage = _usage_from_response(
        {
            "usage": {
                "total_input_tokens": 100,
                "total_output_tokens": 20,
                "total_thought_tokens": 80,
                "total_tokens": 200,
            }
        }
    )

    assert usage is not None
    assert usage.prompt_tokens == 100
    assert usage.completion_tokens == 100
    assert usage.reasoning_tokens == 80
    assert usage.total_tokens == 200


def test_usage_from_response_includes_tool_use_prompt_tokens() -> None:
    from alysis_code.llm.gemini_interactions import _usage_from_response

    usage = _usage_from_response(
        {
            "usageMetadata": {
                "promptTokenCount": 10,
                "toolUsePromptTokenCount": 20,
                "candidatesTokenCount": 4,
                "thoughtsTokenCount": 6,
                "totalTokenCount": 40,
            }
        }
    )

    assert usage is not None
    assert usage.prompt_tokens == 30
    assert usage.completion_tokens == 10
    assert usage.total_tokens == 40


def test_interactions_count_input_tokens_uses_model_tokenizer_endpoint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == (
            "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:countTokens"
        )
        body = json.loads(request.content.decode("utf-8"))
        assert body == {"contents": [{"role": "user", "parts": [{"text": "Say hello."}]}]}
        return httpx.Response(200, json={"totalTokens": 9})

    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )

    result = client.count_input_tokens(messages=[{"role": "user", "content": "Say hello."}])

    assert result is not None
    assert result.input_tokens == 9
    assert result.source.value == "provider_count"
    assert result.confidence.value == "reported"


def test_gemini_interactions_feature_flag_is_disabled_by_default(monkeypatch) -> None:
    monkeypatch.delenv(GEMINI_INTERACTIONS_EXPERIMENT_ENV, raising=False)

    assert gemini_interactions_enabled(AppConfig()) is False


def test_gemini_interactions_feature_flag_accepts_env(monkeypatch) -> None:
    cfg = AppConfig()
    cfg.experimental_gemini_interactions_enabled = False
    monkeypatch.setenv(GEMINI_INTERACTIONS_EXPERIMENT_ENV, "1")

    assert gemini_interactions_enabled(cfg) is True


def test_gemini_interactions_feature_flag_accepts_config(monkeypatch) -> None:
    monkeypatch.delenv(GEMINI_INTERACTIONS_EXPERIMENT_ENV, raising=False)
    cfg = set_config_value(AppConfig(), GEMINI_INTERACTIONS_CONFIG_FLAG, "true")

    assert cfg.experimental_gemini_interactions_enabled is True
    assert gemini_interactions_enabled(cfg) is True


def test_gemini_interactions_text_only_client_maps_request_and_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        captured["api_revision"] = request.headers.get("Api-Revision")
        captured["api_key_header"] = request.headers.get("x-goog-api-key")
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200,
            json={
                "id": "interaction-123",
                "model": "gemini-2.5-flash",
                "outputs": [{"type": "text", "text": "hello from interactions"}],
                "usage": {
                    "total_input_tokens": 3,
                    "total_output_tokens": 5,
                    "total_tokens": 8,
                },
            },
        )

    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[
            {"role": "user", "content": "Say hello."},
        ],
        temperature=0.0,
        max_tokens=20,
    )

    assert captured["method"] == "POST"
    assert captured["url"] == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert "gemini-secret-key" not in str(captured["url"])
    assert captured["api_revision"] == "2026-05-20"
    assert captured["api_key_header"] == "gemini-secret-key"
    assert captured["body"] == {
        "input": "Say hello.",
        "model": "gemini-2.5-flash",
        "generation_config": {
            "temperature": 0.0,
            "max_output_tokens": 20,
        },
    }
    assert response.content == "hello from interactions"
    assert response.tool_calls == []
    assert response.response_model == "gemini-2.5-flash"
    assert response.usage is not None
    assert response.usage.prompt_tokens == 3
    assert response.usage.completion_tokens == 5
    assert response.usage.total_tokens == 8
    assert response.provider_metadata is not None
    assert (
        response.provider_metadata[GEMINI_INTERACTIONS_PROVIDER_METADATA_KEY]["interaction_id"]
        == "interaction-123"
    )
    assert (
        response.provider_metadata[GEMINI_INTERACTIONS_PROVIDER_METADATA_KEY]["request_plan"][
            "input_mode"
        ]
        == "full"
    )


def test_gemini_interactions_requests_and_separates_documented_thought_summaries() -> None:
    captured: dict[str, object] = {}
    reasoning_deltas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "interaction-with-summary",
                "model": "gemini-3.5-flash",
                "steps": [
                    {
                        "type": "thought",
                        "signature": "opaque-provider-signature",
                        "summary": [
                            {"type": "text", "text": "Checked the constraints."},
                            {"type": "image", "data": "not-displayable"},
                        ],
                    },
                    {
                        "type": "thought",
                        "signature": "second-opaque-signature",
                        "text": "raw internal state",
                    },
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "Final answer."}],
                    },
                ],
            },
        )

    response = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-3.5-flash",
        transport=httpx.MockTransport(handler),
    ).chat(
        messages=[{"role": "user", "content": "Solve it."}],
        on_reasoning_delta=reasoning_deltas.append,
        max_tokens=20,
    )

    assert captured["generation_config"] == {
        "max_output_tokens": 20,
        "thinking_summaries": "auto",
    }
    assert response.content == "Final answer."
    assert reasoning_deltas == ["Checked the constraints."]
    assert [(item.kind, item.text) for item in response.reasoning] == [
        (ReasoningOutputKind.SUMMARY, "Checked the constraints.")
    ]
    visible = (
        response.content
        + "".join(reasoning_deltas)
        + "".join(item.text for item in response.reasoning)
    )
    assert "opaque-provider-signature" not in visible
    assert "second-opaque-signature" not in visible
    assert "raw internal state" not in visible


@pytest.mark.parametrize("rejection_status", [400, 422])
def test_gemini_interactions_summary_rejection_falls_back_once_and_caches(
    rejection_status: int,
) -> None:
    bodies: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content.decode("utf-8")))
        if len(bodies) == 1:
            return httpx.Response(
                rejection_status,
                json={
                    "error": {"message": "Unknown field thinking_summaries in generation_config"}
                },
            )
        return httpx.Response(
            200,
            json={
                "id": f"interaction-{len(bodies)}",
                "steps": [
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": "ok"}],
                    }
                ],
            },
        )

    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )
    first = client.chat(
        messages=[{"role": "user", "content": "hello"}],
        on_reasoning_delta=lambda _delta: None,
        temperature=0.0,
        max_tokens=20,
    )
    second = client.chat(
        messages=[{"role": "user", "content": "hello again"}],
        on_reasoning_delta=lambda _delta: None,
        temperature=0.0,
        max_tokens=20,
    )

    assert first.content == "ok"
    assert second.content == "ok"
    assert len(bodies) == 3
    assert bodies[0]["generation_config"] == {
        "temperature": 0.0,
        "max_output_tokens": 20,
        "thinking_summaries": "auto",
    }
    assert bodies[1]["generation_config"] == {
        "temperature": 0.0,
        "max_output_tokens": 20,
    }
    assert bodies[2]["generation_config"] == {
        "temperature": 0.0,
        "max_output_tokens": 20,
    }
    metadata = first.provider_metadata[GEMINI_INTERACTIONS_PROVIDER_METADATA_KEY]  # type: ignore[index]
    assert metadata["request_plan"]["input_mode"] == "retry_without_thinking_summaries"
    assert metadata["request_plan"]["fallback_used"] is True


def test_gemini_interactions_does_not_retry_unrelated_bad_request() -> None:
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(400, json={"error": {"message": "Prompt is invalid"}})

    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(handler),
    )

    with pytest.raises(LLMError, match="Prompt is invalid"):
        client.chat(
            messages=[{"role": "user", "content": "hello"}],
            on_reasoning_delta=lambda _delta: None,
        )
    assert calls == 1


def test_gemini_3_interactions_uses_default_temperature() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={
                "id": "interaction-gemini-3",
                "model": "gemini-3.5-flash",
                "outputs": [{"type": "text", "text": "ok"}],
            },
        )

    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-3.5-flash",
        temperature=0.2,
        transport=httpx.MockTransport(handler),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "Say hello."}],
        temperature=0.0,
        max_tokens=20,
    )

    assert response.content == "ok"
    assert captured["generation_config"] == {"max_output_tokens": 20}
    assert response.provider_metadata is not None
    request_plan = response.provider_metadata[GEMINI_INTERACTIONS_PROVIDER_METADATA_KEY][
        "request_plan"
    ]
    assert request_plan["temperature_omitted"] is True
    assert request_plan["temperature_omit_reason"] == "gemini_3_default_temperature"


def test_gemini_interactions_text_only_client_rejects_unimplemented_surfaces() -> None:
    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )

    with pytest.raises(LLMError, match="does not support streaming"):
        client.chat(messages=[{"role": "user", "content": "hi"}], stream=True)

    with pytest.raises(LLMError, match="does not support tools"):
        client.chat(
            messages=[{"role": "user", "content": "hi"}],
            tools=[{"type": "function", "function": {"name": "lookup"}}],
        )


def test_gemini_interactions_client_raises_clear_provider_error() -> None:
    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                400,
                json={"error": {"message": "Api-Revision header is required"}},
            )
        ),
    )

    with pytest.raises(LLMError, match="Api-Revision header is required"):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_gemini_interactions_client_rejects_multi_turn_history() -> None:
    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )

    with pytest.raises(LLMError, match="previous_interaction_id continuation"):
        client.chat(
            messages=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "answer"},
                {"role": "user", "content": "second"},
            ]
        )


def test_gemini_interactions_client_rejects_system_instructions_until_mapped() -> None:
    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, json={})),
    )

    with pytest.raises(LLMError, match="system/developer instructions"):
        client.chat(
            messages=[
                {"role": "system", "content": "Be brief."},
                {"role": "user", "content": "hi"},
            ]
        )


def test_gemini_interactions_client_rejects_unhandled_actions() -> None:
    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="gemini-secret-key",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "interaction-123",
                    "outputs": [{"type": "text", "text": "I need a tool."}],
                    "actions": [{"type": "function_call", "name": "lookup", "args": {}}],
                },
            )
        ),
    )

    with pytest.raises(LLMError, match="only supports text output"):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_gemini_interactions_response_metadata_is_route_stamped_without_secrets() -> None:
    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="credential-secret",
        model="gemini-2.5-flash",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "id": "interaction-123",
                    "outputs": [{"type": "text", "text": "Done."}],
                },
            )
        ),
    )

    response = client.chat(messages=[{"role": "user", "content": "hello"}])

    assert response.provider_metadata is not None
    route_stamp = response.provider_metadata[ROUTE_IDENTITY_PROVIDER_METADATA_KEY]
    assert set(route_stamp) == {"version", "fingerprint"}
    serialized = json.dumps(route_stamp)
    assert "credential-secret" not in serialized
    assert client.route_identity.protocol_revision == "2026-05-20"


def test_gemini_interactions_extra_headers_override_defaults_case_insensitively() -> None:
    client = GeminiInteractionsClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="fallback-key",
        model="gemini-2.5-flash",
        extra_headers={
            "X-Goog-Api-Key": "override-key",
            "Api-Revision": "2099-01-01",
            "Content-Type": "application/custom+json",
        },
    )

    headers = client._headers()

    assert headers["x-goog-api-key"] == "override-key"
    assert headers["api-revision"] == "2099-01-01"
    assert headers["content-type"] == "application/custom+json"
    assert len({name.casefold() for name in headers}) == len(headers)


def test_interaction_usage_preserves_zero_cache_and_tool_counts_without_claiming_context_size():
    from types import SimpleNamespace

    from alysis_code.llm.gemini_interactions import _usage_from_response
    from alysis_code.provider_usage import capture_provider_usage

    for cached in (0, 60):
        usage = _usage_from_response(
            {
                "usage": {
                    "total_input_tokens": 100,
                    "total_output_tokens": 0,
                    "total_thought_tokens": 20,
                    "total_tool_use_tokens": 30,
                    "total_cached_tokens": cached,
                    "total_tokens": 150,
                }
            }
        )
        reading = capture_provider_usage(usage, client=SimpleNamespace(model="gemini"))
        assert reading["input_scope"] == "interaction"
        assert reading["input_tokens"] == 130
        assert reading["cached_tokens"] == cached
        assert reading["uncached_tokens"] == 130 - cached
        assert reading["uncached_derived"] is True
        assert reading["output_tokens"] == 20
        assert reading["reasoning_tokens"] == 20
        assert reading["total_tokens"] == 150
    zero = _usage_from_response(
        {"usage": {"total_input_tokens": 0, "total_output_tokens": 0, "total_tokens": 0}}
    )
    assert zero.prompt_tokens == zero.completion_tokens == zero.total_tokens == 0
