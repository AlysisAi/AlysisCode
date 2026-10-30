from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.config import AppConfig
from alysis_code.litellm_static_provider import BUNDLED_MODEL_CATALOG_SOURCE
from alysis_code.llm.types import BillingMode, CostSource, LLMUsage
from alysis_code.model_registry import ModelMeta, ModelRegistry
from alysis_code.request_estimation import (
    estimate_request_token_breakdown,
    request_contains_media,
    request_message_signatures,
    tool_schema_signature,
)
from alysis_code.token_budget import compute_input_budget
from alysis_code.usage_tracker import (
    RequestContextMeasurement,
    UsageRecord,
    UsageSummary,
    aggregate_usage_from_session_logs,
    build_usage_record,
    compute_context_left,
    estimate_completion_tokens,
    estimate_prompt_tokens,
    format_usd,
    usage_context_from_client_response,
)


class _FakeRegistry:
    def __init__(self, by_model: dict[str, ModelMeta]) -> None:
        self._by_model = by_model

    def get(self, model_name: str) -> ModelMeta:
        return self._by_model[model_name]


def test_build_usage_record_computes_cost_from_registry_rates() -> None:
    registry = _FakeRegistry(
        {
            "gpt-5-nano": ModelMeta(
                model_name="gpt-5-nano",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=0.1,
                output_cost_per_token=0.2,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    record = build_usage_record(
        role="main",
        requested_model="gpt-5-nano",
        response_model="gpt-5-nano",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=10,
        api_completion_tokens=5,
        api_total_tokens=15,
        registry=registry,  # type: ignore[arg-type]
    )
    assert record.usage_source == "api"
    assert record.prompt_tokens == 10
    assert record.completion_tokens == 5
    assert record.total_tokens == 15
    assert record.cost_usd == 2.0


def test_build_usage_record_preserves_reported_counts_even_when_similar_to_text_length() -> None:
    registry = _FakeRegistry(
        {
            "qwen3.5-plus": ModelMeta(
                model_name="qwen3.5-plus",
                context_window_tokens=1000000,
                max_output_tokens=8192,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    messages = [
        {"role": "system", "content": "You are a coding agent.\n" * 200},
        {"role": "user", "content": "hi"},
    ]
    response_content = (
        "I see you have an interesting repository with two projects. "
        "Let me know what you'd like to work on.\n"
        "1 AI News Hub - Web app for AI news.\n"
        "2 Asteroids Game - Pygame implementation.\n"
    )
    api_prompt_char_count = len(
        json.dumps({"messages": messages}, ensure_ascii=False, sort_keys=True)
    )
    api_completion_char_count = len(response_content)

    record = build_usage_record(
        role="main",
        requested_model="qwen3.5-plus",
        response_model="qwen3.5-plus",
        messages=messages,
        response_content=response_content,
        response_tool_calls=[],
        api_prompt_tokens=api_prompt_char_count,
        api_completion_tokens=api_completion_char_count,
        api_total_tokens=api_prompt_char_count + api_completion_char_count,
        api_cached_prompt_tokens=api_prompt_char_count // 2,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.usage_source == "api"
    assert record.prompt_tokens == api_prompt_char_count
    assert record.completion_tokens == api_completion_char_count
    assert record.total_tokens == api_prompt_char_count + api_completion_char_count
    assert record.cached_prompt_tokens == api_prompt_char_count // 2
    assert record.uncached_prompt_tokens == api_prompt_char_count - api_prompt_char_count // 2
    assert record.raw_api_prompt_tokens == api_prompt_char_count
    assert record.raw_api_completion_tokens == api_completion_char_count
    assert record.raw_api_total_tokens == api_prompt_char_count + api_completion_char_count
    assert record.usage_correction_reason is None

    payload = record.to_payload()
    assert payload["raw_api_prompt_tokens"] == api_prompt_char_count
    assert payload["usage_correction_reason"] == record.usage_correction_reason

    summary = UsageSummary()
    summary.add_event_payload(payload)
    assert summary.records()[0].raw_api_prompt_tokens == api_prompt_char_count
    assert summary.totals()["corrected_usage_calls"] == 0
    assert summary.totals()["estimate_usage_calls"] == 0


def test_build_usage_record_prefers_transformed_provider_payload_estimate_for_fallback() -> None:
    registry = _FakeRegistry(
        {
            "provider-shaped": ModelMeta(
                model_name="provider-shaped",
                context_window_tokens=8000,
                max_output_tokens=1000,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source="test",
            )
        }
    )

    record = build_usage_record(
        role="main",
        requested_model="provider-shaped",
        response_model="provider-shaped",
        messages=[{"role": "user", "content": "hello"}],
        response_content="done",
        response_tool_calls=[],
        api_prompt_tokens=None,
        api_completion_tokens=None,
        api_total_tokens=None,
        request_plan={
            "input_mode": "full",
            "serialized_request_estimate_tokens": 321,
        },
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.usage_source == "estimate"
    assert record.prompt_tokens == 321
    assert record.request_plan is not None
    assert record.request_plan["serialized_request_estimate_tokens"] == 321


def test_local_preflight_measurement_never_calibrates_itself_as_provider_truth() -> None:
    registry = _FakeRegistry(
        {
            "compat-model": ModelMeta(
                model_name="compat-model",
                context_window_tokens=8000,
                max_output_tokens=1000,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source="test",
            )
        }
    )
    record = build_usage_record(
        role="main",
        requested_model="compat-model",
        response_model="compat-model",
        messages=[{"role": "user", "content": "hello"}],
        response_content="done",
        response_tool_calls=[],
        api_prompt_tokens=123,
        api_completion_tokens=None,
        api_total_tokens=None,
        api_usage_source_detail="local_estimate",
        api_usage_confidence="estimated",
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.prompt_tokens == 123
    assert record.usage_source == "estimate"
    assert record.usage_source_detail == "local_estimate"
    assert record.usage_confidence == "estimated"
    assert record.prompt_estimate_error_ratio is None


def test_build_usage_record_keeps_api_completion_for_reasoning_models() -> None:
    # Reasoning models report output_tokens that INCLUDE hidden reasoning, so the
    # API completion count legitimately dwarfs the visible answer text. The
    # character-count heuristic must not overwrite it when reasoning is present.
    registry = _FakeRegistry(
        {
            "gpt-5": ModelMeta(
                model_name="gpt-5",
                context_window_tokens=400000,
                max_output_tokens=8192,
                input_cost_per_token=0.0,
                output_cost_per_token=0.0,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    messages = [{"role": "user", "content": "Answer briefly."}]
    response_content = "The answer is 42.\n" * 8  # short visible answer
    visible_completion_chars = len(response_content)
    # API completion INCLUDES reasoning, so it lands near the char count of the
    # visible text -- exactly the shape the char-count heuristic flags as bogus.
    api_completion_tokens = visible_completion_chars
    api_reasoning_tokens = visible_completion_chars

    record = build_usage_record(
        role="main",
        requested_model="gpt-5",
        response_model="gpt-5",
        messages=messages,
        response_content=response_content,
        response_tool_calls=[],
        api_prompt_tokens=50,
        api_completion_tokens=api_completion_tokens,
        api_total_tokens=50 + api_completion_tokens,
        api_reasoning_tokens=api_reasoning_tokens,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.usage_source == "api"
    assert record.completion_tokens == api_completion_tokens
    assert record.reasoning_tokens == api_reasoning_tokens
    assert "completion_tokens" not in (record.usage_correction_reason or "")


def test_authoritative_provider_usage_is_never_replaced_by_visible_text_heuristics() -> None:
    registry = _FakeRegistry(
        {
            "gpt-5": ModelMeta(
                model_name="gpt-5",
                context_window_tokens=400000,
                max_output_tokens=8192,
                input_cost_per_token=0.0,
                output_cost_per_token=0.0,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    messages = [{"role": "user", "content": "Answer briefly."}]
    response_content = "A" * 144

    # These values deliberately resemble character counts. Native OpenAI usage
    # remains authoritative even when the response omits a reasoning breakdown:
    # output tokens can include provider-side structure that is not visible text.
    record = build_usage_record(
        role="main",
        requested_model="gpt-5",
        response_model="gpt-5",
        messages=messages,
        response_content=response_content,
        response_tool_calls=[],
        api_prompt_tokens=50,
        api_completion_tokens=144,
        api_total_tokens=194,
        api_reasoning_tokens=0,
        api_usage_counts_authoritative=True,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.usage_source == "api"
    assert record.prompt_tokens == 50
    assert record.completion_tokens == 144
    assert record.total_tokens == 194
    assert record.usage_correction_reason is None
    assert record.usage_source_detail == "provider_response"
    assert record.usage_confidence == "authoritative"
    assert record.output_includes_reasoning is True


def test_partial_authoritative_response_keeps_reported_prompt_count_confidence() -> None:
    registry = _FakeRegistry(
        {
            "projection-model": ModelMeta(
                model_name="projection-model",
                context_window_tokens=100000,
                max_output_tokens=8192,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )

    record = build_usage_record(
        role="main",
        requested_model="projection-model",
        response_model="projection-model",
        messages=[{"role": "user", "content": "hello"}],
        response_content="done",
        response_tool_calls=[],
        api_prompt_tokens=100,
        api_completion_tokens=5,
        api_total_tokens=105,
        api_usage_counts_authoritative=True,
        api_prompt_tokens_authoritative=False,
        api_usage_confidence="reported",
        api_usage_source_detail="provider_count",
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.usage_source == "api"
    assert record.usage_source_detail == "provider_count"
    assert record.usage_confidence == "reported"


def test_usage_normalization_supports_provider_output_that_excludes_reasoning() -> None:
    from alysis_code.llm.types import LLMUsage

    registry = _FakeRegistry(
        {
            "future-provider-model": ModelMeta(
                model_name="future-provider-model",
                context_window_tokens=100000,
                max_output_tokens=8192,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    usage = LLMUsage(
        prompt_tokens=100,
        completion_tokens=20,
        total_tokens=120,
        reasoning_tokens=30,
        output_includes_reasoning=False,
        total_includes_reasoning=False,
    )

    record = build_usage_record(
        role="main",
        requested_model="future-provider-model",
        response_model="future-provider-model",
        messages=[{"role": "user", "content": "Solve it."}],
        response_content="Done.",
        response_tool_calls=[],
        api_prompt_tokens=usage.prompt_tokens,
        api_completion_tokens=usage.completion_tokens,
        api_total_tokens=usage.total_tokens,
        api_usage=usage,
        api_usage_counts_authoritative=True,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.prompt_tokens == 100
    assert record.completion_tokens == 50
    assert record.reasoning_tokens == 30
    assert record.total_tokens == 150
    assert record.output_includes_reasoning is True


def test_build_usage_record_preserves_reported_total_when_components_disagree() -> None:
    registry = _FakeRegistry(
        {
            "gpt-5-nano": ModelMeta(
                model_name="gpt-5-nano",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=0.1,
                output_cost_per_token=0.2,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )

    record = build_usage_record(
        role="main",
        requested_model="gpt-5-nano",
        response_model="gpt-5-nano",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=10,
        api_completion_tokens=5,
        api_total_tokens=8,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.usage_source == "api"
    assert record.prompt_tokens == 10
    assert record.completion_tokens == 5
    assert record.total_tokens == 8
    assert record.raw_api_total_tokens == 8
    assert record.usage_correction_reason is None

    summary = UsageSummary()
    summary.add_event_payload(record.to_payload())
    assert summary.totals()["corrected_usage_calls"] == 0
    assert summary.totals()["total_tokens"] == 8


def test_build_usage_record_preserves_large_reported_total() -> None:
    registry = _FakeRegistry(
        {
            "qwen3.5-plus": ModelMeta(
                model_name="qwen3.5-plus",
                context_window_tokens=1000000,
                max_output_tokens=8192,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    messages = [
        {"role": "system", "content": "You are a coding agent.\n" * 200},
        {"role": "user", "content": "hi"},
    ]
    response_content = "Short answer."
    api_total_char_count = len(
        json.dumps({"messages": messages}, ensure_ascii=False, sort_keys=True)
    ) + len(response_content)

    record = build_usage_record(
        role="main",
        requested_model="qwen3.5-plus",
        response_model="qwen3.5-plus",
        messages=messages,
        response_content=response_content,
        response_tool_calls=[],
        api_prompt_tokens=estimate_prompt_tokens(messages),
        api_completion_tokens=estimate_completion_tokens(response_content, []),
        api_total_tokens=api_total_char_count,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.usage_source == "api"
    assert record.total_tokens == api_total_char_count
    assert record.raw_api_total_tokens == api_total_char_count
    assert record.usage_correction_reason is None


def test_build_usage_record_captures_cached_prompt_and_request_breakdown() -> None:
    registry = _FakeRegistry(
        {
            "gpt-5-nano": ModelMeta(
                model_name="gpt-5-nano",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=0.1,
                output_cost_per_token=0.2,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    messages = [
        {"role": "system", "content": "Core prompt"},
        {"role": "system", "content": "Repo prompt"},
        {"role": "user", "content": "Investigate the failing test."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_1", "name": "fs_read", "arguments": {"path": "app.py"}}],
        },
        {
            "role": "tool",
            "tool_call_id": "call_1",
            "content": '{"path":"app.py","content":"print(1)"}',
        },
        {
            "role": "user",
            "content": '<<<ALYSIS_CONVERSATION_MEMORY_JSON>>>\n{"summary":"keep this"}',
        },
        {
            "role": "user",
            "content": '<<<ALYSIS_CONVERSATION_PINS_JSON>>>\n[{"path":"tests/test_app.py"}]',
        },
    ]
    tool_list = [
        {
            "type": "function",
            "function": {
                "name": "fs_read",
                "description": "Read file",
                "parameters": {"type": "object", "properties": {"path": {"type": "string"}}},
            },
        }
    ]

    record = build_usage_record(
        role="main",
        requested_model="gpt-5-nano",
        response_model="gpt-5-nano",
        messages=messages,
        response_content="Patched the issue.",
        response_tool_calls=[],
        api_prompt_tokens=120,
        api_completion_tokens=12,
        api_total_tokens=132,
        api_cached_prompt_tokens=48,
        tool_list=tool_list,
        pinned_prefix_len=2,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.cached_prompt_tokens == 48
    assert record.uncached_prompt_tokens == 72
    assert record.request_token_estimate is not None
    assert record.request_token_estimate.bootstrap_prompt_tokens > 0
    assert record.request_token_estimate.tool_schema_tokens > 0
    assert record.request_token_estimate.live_conversation_history_tokens > 0
    assert record.request_token_estimate.inline_tool_transcript_tokens > 0
    assert record.request_token_estimate.memory_summary_tokens > 0
    assert record.request_token_estimate.pins_tokens > 0


def test_build_usage_record_separates_cache_read_write_and_marks_cost_unknown() -> None:
    registry = _FakeRegistry(
        {
            "claude-sonnet-4-6": ModelMeta(
                model_name="claude-sonnet-4-6",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=0.1,
                output_cost_per_token=0.2,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )

    record = build_usage_record(
        role="main",
        requested_model="claude-sonnet-4-6",
        response_model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=100,
        api_completion_tokens=5,
        api_total_tokens=105,
        api_input_tokens_uncached=10,
        api_cache_read_input_tokens=70,
        api_cache_creation_input_tokens=20,
        api_cache_creation_5m_input_tokens=8,
        api_cache_creation_1h_input_tokens=12,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.prompt_tokens == 100
    assert record.completion_tokens == 5
    assert record.total_tokens == 105
    assert record.cached_prompt_tokens == 70
    assert record.cache_read_input_tokens == 70
    assert record.cache_creation_input_tokens == 20
    assert record.cache_creation_5m_input_tokens == 8
    assert record.cache_creation_1h_input_tokens == 12
    assert record.input_tokens_uncached == 10
    assert record.uncached_prompt_tokens == 30
    assert record.cost_usd is None
    assert record.cache_cost_pricing_missing is True

    payload = record.to_payload()
    assert payload["usage_schema_version"] == 7
    assert payload["usage_source_detail"] == "provider_response"
    assert payload["usage_confidence"] == "reported"
    assert payload["output_includes_reasoning"] is True
    assert payload["cache_read_input_tokens"] == 70
    assert payload["cache_creation_input_tokens"] == 20

    summary = UsageSummary()
    summary.add_event_payload(payload)
    totals = summary.totals()
    assert totals["cache_read_input_tokens"] == 70
    assert totals["cache_creation_input_tokens"] == 20
    assert totals["cache_creation_5m_input_tokens"] == 8
    assert totals["cache_creation_1h_input_tokens"] == 12
    assert totals["input_tokens_uncached"] == 10
    assert totals["cache_cost_pricing_missing_calls"] == 1


def test_build_usage_record_computes_cache_aware_cost_when_pricing_is_known() -> None:
    registry = _FakeRegistry(
        {
            "claude-sonnet-4-6": ModelMeta(
                model_name="claude-sonnet-4-6",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=0.1,
                output_cost_per_token=0.2,
                cache_read_input_cost_per_token=0.01,
                cache_creation_input_cost_per_token=0.12,
                cache_creation_5m_input_cost_per_token=0.11,
                cache_creation_1h_input_cost_per_token=0.15,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )

    record = build_usage_record(
        role="main",
        requested_model="claude-sonnet-4-6",
        response_model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=100,
        api_completion_tokens=5,
        api_total_tokens=105,
        api_input_tokens_uncached=10,
        api_cache_read_input_tokens=70,
        api_cache_creation_input_tokens=20,
        api_cache_creation_5m_input_tokens=8,
        api_cache_creation_1h_input_tokens=12,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.cache_cost_pricing_missing is False
    assert record.cost_usd == (10 * 0.1) + (70 * 0.01) + (8 * 0.11) + (12 * 0.15) + (5 * 0.2)
    payload = record.to_payload()
    assert payload["cache_read_input_cost_per_token"] == 0.01
    assert payload["cache_creation_1h_input_cost_per_token"] == 0.15


def test_kimi_k3_cached_tokens_use_global_moonshot_discount() -> None:
    registry = ModelRegistry(cfg=AppConfig(base_url="https://api.moonshot.ai/v1", model="kimi-k3"))

    record = build_usage_record(
        role="main",
        requested_model="kimi-k3",
        response_model="kimi-k3",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=100,
        api_completion_tokens=10,
        api_total_tokens=110,
        api_input_tokens_uncached=20,
        api_cache_read_input_tokens=80,
        registry=registry,
    )

    assert record.cache_cost_pricing_missing is False
    assert record.cost_usd == (20 * 0.000003) + (80 * 0.0000003) + (10 * 0.000015)


def test_provider_reported_total_cost_overrides_catalog_token_estimate() -> None:
    registry = _FakeRegistry(
        {
            "sonar-pro": ModelMeta(
                model_name="sonar-pro",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=0.000003,
                output_cost_per_token=0.000015,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    api_usage = LLMUsage(
        prompt_tokens=8,
        completion_tokens=439,
        total_tokens=447,
        provider_cost_usd=0.012609,
    )

    record = build_usage_record(
        role="main",
        requested_model="sonar-pro",
        response_model="sonar-pro",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=8,
        api_completion_tokens=439,
        api_total_tokens=447,
        api_usage=api_usage,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.cost_usd == 0.012609
    assert record.provider_cost_usd == 0.012609
    assert record.cost_source == CostSource.PROVIDER_REPORTED.value


def test_subscription_usage_is_not_presented_as_zero_cost() -> None:
    registry = _FakeRegistry(
        {
            "gpt-subscription": ModelMeta(
                model_name="gpt-subscription",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=0.0,
                output_cost_per_token=0.0,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    record = build_usage_record(
        role="main",
        requested_model="gpt-subscription",
        response_model="gpt-subscription",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=100,
        api_completion_tokens=10,
        api_total_tokens=110,
        registry=registry,  # type: ignore[arg-type]
        billing_mode=BillingMode.SUBSCRIPTION.value,
    )

    assert record.cost_usd is None
    assert record.billing_mode == BillingMode.SUBSCRIPTION.value
    assert record.cost_source == CostSource.SUBSCRIPTION.value

    summary = UsageSummary()
    summary.add_record(record)
    totals = summary.totals()
    assert totals["subscription_calls"] == 1
    assert totals["unknown_cost_calls"] == 0
    assert totals["known_cost_calls"] == 0


def test_included_and_local_usage_ignore_catalog_rates() -> None:
    registry = _FakeRegistry(
        {
            "nonmetered-model": ModelMeta(
                model_name="nonmetered-model",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=0.5,
                output_cost_per_token=1.0,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )

    for billing_mode, expected_source, counter in (
        (BillingMode.INCLUDED, CostSource.INCLUDED, "included_calls"),
        (BillingMode.LOCAL, CostSource.LOCAL, "local_calls"),
    ):
        record = build_usage_record(
            role="main",
            requested_model="nonmetered-model",
            response_model="nonmetered-model",
            messages=[{"role": "user", "content": "hello"}],
            response_content="world",
            response_tool_calls=[],
            api_prompt_tokens=100,
            api_completion_tokens=10,
            api_total_tokens=110,
            registry=registry,  # type: ignore[arg-type]
            billing_mode=billing_mode.value,
        )

        assert record.cost_usd is None
        assert record.billing_mode == billing_mode.value
        assert record.cost_source == expected_source.value

        summary = UsageSummary()
        summary.add_record(record)
        totals = summary.totals()
        assert totals[counter] == 1
        assert totals["unknown_cost_calls"] == 0
        assert totals["known_cost_calls"] == 0


def test_usage_summary_preserves_cache_reporting_coverage() -> None:
    summary = UsageSummary()
    base = dict(
        timestamp="2026-07-31T00:00:00+00:00",
        role="main",
        requested_model="model",
        response_model="model",
        prompt_tokens=10,
        completion_tokens=1,
        total_tokens=11,
        input_cost_per_token=None,
        output_cost_per_token=None,
        cost_usd=None,
        usage_source="api",
    )
    summary.add_record(UsageRecord(**base))
    summary.add_record(
        UsageRecord(
            **base,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
            input_tokens_uncached=10,
        )
    )

    row = summary.by_model_rows()[0]
    assert row["api_usage_calls"] == 2
    assert row["cache_read_input_tokens"] == 0
    assert row["cache_read_reported_calls"] == 1
    assert row["cache_creation_reported_calls"] == 1
    assert row["input_tokens_uncached_reported_calls"] == 1


def test_build_usage_record_records_prompt_estimate_calibration() -> None:
    registry = _FakeRegistry(
        {
            "calibrated-model": ModelMeta(
                model_name="calibrated-model",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )

    record = build_usage_record(
        role="main",
        requested_model="calibrated-model",
        response_model="calibrated-model",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=500,
        api_completion_tokens=5,
        api_total_tokens=505,
        registry=registry,  # type: ignore[arg-type]
    )

    assert record.prompt_estimate_tokens is not None
    assert record.prompt_estimate_error_tokens == 500 - record.prompt_estimate_tokens
    assert record.prompt_estimate_error_ratio == 500 / record.prompt_estimate_tokens
    payload = record.to_payload()
    assert payload["prompt_estimate_tokens"] == record.prompt_estimate_tokens
    assert payload["prompt_estimate_error_tokens"] == record.prompt_estimate_error_tokens

    summary = UsageSummary()
    summary.add_record(record)
    totals = summary.totals()
    assert totals["prompt_estimate_calibration_calls"] == 1
    assert totals["prompt_estimate_underestimate_calls"] == 1
    snapshot = summary.recent_calibration_snapshot(requested_model="calibrated-model")
    assert snapshot["calibration_calls"] == 1
    assert snapshot["calibration_stable"] is False
    assert snapshot["prompt_estimate_error_ratio_p90"] is None
    assert snapshot["prompt_estimate_error_ratio_raw_max"] == (record.prompt_estimate_error_ratio)


def test_calibration_requires_coherent_samples_and_preserves_real_ratios_above_cap() -> None:
    summary = UsageSummary()

    def add_ratio(ratio: float) -> None:
        summary.add_record(
            UsageRecord(
                timestamp="2026-07-12T00:00:00+00:00",
                role="main",
                requested_model="route-model",
                response_model="route-model",
                prompt_tokens=160,
                completion_tokens=1,
                total_tokens=161,
                input_cost_per_token=None,
                output_cost_per_token=None,
                cost_usd=None,
                usage_source="api",
                usage_source_detail="provider_response",
                usage_confidence="authoritative",
                provider_key="route",
                protocol="openai_compat",
                base_url_host="route.example",
                operation="main_llm",
                request_mode="full",
                cache_strategy="none",
                prompt_estimate_tokens=100,
                prompt_estimate_error_tokens=60,
                prompt_estimate_error_ratio=ratio,
            )
        )

    add_ratio(1.50)
    first = summary.recent_calibration_snapshot(provider_key="route")
    assert first["calibration_stable"] is False
    assert first["prompt_estimate_error_ratio_p90"] is None

    add_ratio(1.55)
    add_ratio(1.60)
    stable = summary.recent_calibration_snapshot(provider_key="route")
    assert stable["calibration_stable"] is True
    assert stable["prompt_estimate_error_ratio_p90"] is not None
    assert stable["prompt_estimate_error_ratio_p90"] > 1.40

    add_ratio(100.0)
    robust = summary.recent_calibration_snapshot(provider_key="route")
    assert robust["calibration_outlier_calls"] == 1
    assert robust["prompt_estimate_error_ratio_p90"] < 2.0


def test_calibration_rejects_evenly_split_bimodal_samples() -> None:
    summary = UsageSummary()
    for ratio in (1.0, 1.0, 100.0, 100.0):
        summary.add_record(
            UsageRecord(
                timestamp="2026-07-12T00:00:00+00:00",
                role="main",
                requested_model="route-model",
                response_model="route-model",
                prompt_tokens=100,
                completion_tokens=1,
                total_tokens=101,
                input_cost_per_token=None,
                output_cost_per_token=None,
                cost_usd=None,
                usage_source="api",
                provider_key="route",
                prompt_estimate_tokens=100,
                prompt_estimate_error_ratio=ratio,
            )
        )

    snapshot = summary.recent_calibration_snapshot(provider_key="route")

    assert snapshot["calibration_stable"] is False
    assert snapshot["calibration_usable_calls"] == 0
    assert snapshot["prompt_estimate_error_ratio_p90"] is None


def test_calibration_excludes_media_before_learning_text_ratio() -> None:
    summary = UsageSummary()

    def add_ratio(ratio: float, *, request_has_media: bool) -> None:
        summary.add_record(
            UsageRecord(
                timestamp="2026-07-12T00:00:00+00:00",
                role="main",
                requested_model="route-model",
                response_model="route-model",
                prompt_tokens=100,
                completion_tokens=1,
                total_tokens=101,
                input_cost_per_token=None,
                output_cost_per_token=None,
                cost_usd=None,
                usage_source="api",
                provider_key="route",
                prompt_estimate_tokens=100,
                prompt_estimate_error_ratio=ratio,
                request_has_media=request_has_media,
            )
        )

    for ratio in (20.0, 21.0, 22.0):
        add_ratio(ratio, request_has_media=True)
    add_ratio(1.50, request_has_media=False)
    add_ratio(1.55, request_has_media=False)
    unstable = summary.recent_calibration_snapshot(provider_key="route")
    add_ratio(1.60, request_has_media=False)
    stable = summary.recent_calibration_snapshot(provider_key="route")

    assert unstable["calibration_excluded_media_calls"] == 3
    assert unstable["calibration_calls"] == 2
    assert unstable["calibration_stable"] is False
    assert stable["calibration_excluded_media_calls"] == 3
    assert stable["calibration_stable"] is True
    assert stable["prompt_estimate_error_ratio_p90"] is not None
    assert stable["prompt_estimate_error_ratio_p90"] < 2.0


def test_calibration_excludes_legacy_records_without_media_marker() -> None:
    summary = UsageSummary()
    summary.add_event_payload(
        {
            "event_type": "llm_usage",
            "usage_schema_version": 4,
            "timestamp": "2026-07-11T00:00:00+00:00",
            "role": "main",
            "requested_model": "route-model",
            "response_model": "route-model",
            "prompt_tokens": 10_000,
            "completion_tokens": 1,
            "total_tokens": 10_001,
            "usage_source": "api",
            "provider_key": "route",
            "prompt_estimate_tokens": 100,
            "prompt_estimate_error_ratio": 100.0,
        }
    )

    legacy_only = summary.recent_calibration_snapshot(provider_key="route")
    assert legacy_only["calibration_excluded_unknown_media_calls"] == 1
    assert legacy_only["calibration_calls"] == 0
    assert legacy_only["prompt_estimate_error_ratio_p90"] is None

    for ratio in (1.50, 1.55, 1.60):
        summary.add_record(
            UsageRecord(
                timestamp="2026-07-12T00:00:00+00:00",
                role="main",
                requested_model="route-model",
                response_model="route-model",
                prompt_tokens=160,
                completion_tokens=1,
                total_tokens=161,
                input_cost_per_token=None,
                output_cost_per_token=None,
                cost_usd=None,
                usage_source="api",
                provider_key="route",
                prompt_estimate_tokens=100,
                prompt_estimate_error_ratio=ratio,
            )
        )

    stable = summary.recent_calibration_snapshot(provider_key="route")
    assert stable["calibration_excluded_unknown_media_calls"] == 1
    assert stable["calibration_calls"] == 3
    assert stable["calibration_stable"] is True
    assert stable["prompt_estimate_error_ratio_p90"] == 1.60


def test_usage_summary_groups_calibration_by_provider_request_shape() -> None:
    registry = _FakeRegistry(
        {
            "calibrated-model": ModelMeta(
                model_name="calibrated-model",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    summary = UsageSummary()
    openai_record = build_usage_record(
        role="main",
        requested_model="calibrated-model",
        response_model="calibrated-model",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=500,
        api_completion_tokens=5,
        api_total_tokens=505,
        registry=registry,  # type: ignore[arg-type]
        provider_key="openai",
        protocol="openai_responses",
        base_url_host="api.openai.com",
        operation="main_llm",
        request_mode="previous_response_id",
        cache_strategy="openai_prompt_cache",
        request_plan={
            "input_mode": "previous_response_id",
            "cache_strategy": "openai_prompt_cache",
            "request_message_count": 3,
            "messages": [{"role": "user", "content": "must-not-persist"}],
        },
    )
    anthropic_record = build_usage_record(
        role="main",
        requested_model="calibrated-model",
        response_model="calibrated-model",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=300,
        api_completion_tokens=5,
        api_total_tokens=305,
        registry=registry,  # type: ignore[arg-type]
        provider_key="anthropic",
        protocol="anthropic_messages",
        base_url_host="api.anthropic.com",
        operation="main_llm",
        request_mode="full",
        cache_strategy="anthropic_cache_control",
    )
    summary.add_record(openai_record)
    summary.add_record(anthropic_record)

    snapshot = summary.recent_calibration_snapshot(
        requested_model="calibrated-model",
        provider_key="openai",
        protocol="openai_responses",
        request_mode="previous_response_id",
        cache_strategy="openai_prompt_cache",
    )

    assert snapshot["records"] == 1
    assert snapshot["calibration_calls"] == 1
    assert snapshot["group_filter"]["provider_key"] == "openai"
    assert summary.calibration_group_rows()
    openai_rows = [
        row for row in summary.calibration_group_rows() if row["provider_key"] == "openai"
    ]
    assert len(openai_rows) == 1
    assert openai_rows[0]["request_mode"] == "previous_response_id"
    assert openai_record.request_plan == {
        "input_mode": "previous_response_id",
        "cache_strategy": "openai_prompt_cache",
        "request_message_count": 3,
    }
    assert "must-not-persist" not in json.dumps(openai_record.to_payload(), sort_keys=True)


def test_build_usage_record_preserves_raw_provider_usage_for_audit() -> None:
    registry = _FakeRegistry(
        {
            "claude-sonnet-4-6": ModelMeta(
                model_name="claude-sonnet-4-6",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    raw_usage = {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_input_tokens": 30,
        "cache_creation": {"ephemeral_5m_input_tokens": 7},
    }

    record = build_usage_record(
        role="main",
        requested_model="claude-sonnet-4-6",
        response_model="claude-sonnet-4-6",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=40,
        api_completion_tokens=2,
        api_total_tokens=42,
        api_usage=SimpleNamespace(raw_provider_usage=raw_usage),
        registry=registry,  # type: ignore[arg-type]
    )
    raw_usage["cache_creation"]["ephemeral_5m_input_tokens"] = 999

    assert record.raw_provider_usage == {
        "input_tokens": 10,
        "output_tokens": 2,
        "cache_read_input_tokens": 30,
        "cache_creation": {"ephemeral_5m_input_tokens": 7},
    }
    payload = record.to_payload()
    assert payload["raw_provider_usage"] == record.raw_provider_usage
    payload["raw_provider_usage"]["cache_creation"]["ephemeral_5m_input_tokens"] = 123
    assert record.raw_provider_usage["cache_creation"]["ephemeral_5m_input_tokens"] == 7

    summary = UsageSummary()
    summary.add_event_payload(record.to_payload())
    assert summary.records()[0].raw_provider_usage == record.raw_provider_usage


def test_usage_summary_accepts_legacy_usage_payload_without_schema_version() -> None:
    summary = UsageSummary()

    summary.add_event_payload(
        {
            "event_type": "llm_usage",
            "timestamp": "2026-02-24T00:00:00Z",
            "role": "main",
            "requested_model": "legacy-model",
            "response_model": "legacy-model",
            "prompt_tokens": 11,
            "completion_tokens": 4,
            "total_tokens": 15,
            "input_cost_per_token": None,
            "output_cost_per_token": None,
            "cost_usd": None,
            "usage_source": "api",
            "cached_prompt_tokens": 6,
        }
    )

    record = summary.records()[0]
    assert record.usage_schema_version == 1
    assert record.usage_source_detail == "provider_response"
    assert record.usage_confidence == "reported"
    assert record.cache_read_input_tokens == 6
    assert record.raw_provider_usage is None
    assert summary.totals()["cache_read_input_tokens"] == 6


def test_usage_summary_reports_cache_efficiency_and_largest_uncached_call() -> None:
    summary = UsageSummary()
    for position, (cached, uncached) in enumerate(((80, 20), (25, 75), (90, 10)), start=1):
        summary.add_event_payload(
            {
                "event_type": "llm_usage",
                "timestamp": f"2026-08-20T00:00:0{position}Z",
                "role": "main" if position != 2 else "main:subagent:verifier",
                "requested_model": "test-model",
                "prompt_tokens": cached + uncached,
                "completion_tokens": 1,
                "total_tokens": cached + uncached + 1,
                "cached_prompt_tokens": cached,
                "uncached_prompt_tokens": uncached,
                "usage_source": "api",
            }
        )

    cache = summary.cache_efficiency_summary()

    assert cache == {
        "reported_calls": 3,
        "cached_prompt_tokens": 195,
        "uncached_prompt_tokens": 105,
        "hit_ratio": 0.65,
        "largest_uncached_call": {
            "call_position": 2,
            "uncached_prompt_tokens": 75,
            "role": "main:subagent:verifier",
            "requested_model": "test-model",
            "timestamp": "2026-08-20T00:00:02Z",
        },
    }
    assert summary.totals()["cache_efficiency"] == cache


def test_usage_context_uses_normalized_provider_contract() -> None:
    from alysis_code.llm.types import UsageConfidence, UsageContract

    client = SimpleNamespace(
        model="provider-model",
        base_url="https://provider.example/v1",
        provider_key="provider",
        usage_contract=UsageContract(
            response_usage_confidence=UsageConfidence.AUTHORITATIVE,
            input_token_count_strategy="provider_count",
        ),
    )

    context = usage_context_from_client_response(
        client=client,
        response=None,
        operation="main_llm",
    )

    assert context["api_usage_counts_authoritative"] is True
    assert context["api_prompt_tokens_authoritative"] is True
    assert context["api_usage_confidence"] == "authoritative"
    assert context["api_usage_source_detail"] == "provider_response"
    assert context["api_output_includes_reasoning"] is True


def test_usage_summary_aggregates_multi_model_and_unknown_cost() -> None:
    summary = UsageSummary()
    summary.add_event_payload(
        {
            "event_type": "llm_usage",
            "timestamp": "2026-02-24T00:00:00Z",
            "role": "main",
            "requested_model": "model-a",
            "response_model": "model-a",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "input_cost_per_token": 0.001,
            "output_cost_per_token": 0.002,
            "cost_usd": 0.2,
            "usage_source": "api",
        }
    )
    summary.add_event_payload(
        {
            "event_type": "llm_usage",
            "timestamp": "2026-02-24T00:00:01Z",
            "role": "main",
            "requested_model": "model-b",
            "response_model": "model-b",
            "prompt_tokens": 10,
            "completion_tokens": 5,
            "total_tokens": 15,
            "input_cost_per_token": None,
            "output_cost_per_token": None,
            "cost_usd": None,
            "usage_source": "estimate",
        }
    )
    rows = summary.by_model_rows()
    assert len(rows) == 2
    totals = summary.totals()
    assert totals["prompt_tokens"] == 110
    assert totals["completion_tokens"] == 55
    assert totals["total_tokens"] == 165
    assert totals["cost_usd"] == 0.2
    assert totals["known_cost_usd"] == 0.2
    assert totals["known_cost_calls"] == 1
    assert totals["unknown_cost_calls"] == 1
    assert totals["api_usage_calls"] == 1
    assert totals["estimate_usage_calls"] == 1


def test_usage_summary_round_trips_cached_prompt_and_request_breakdown() -> None:
    summary = UsageSummary()
    summary.add_event_payload(
        {
            "event_type": "llm_usage",
            "timestamp": "2026-02-24T00:00:00Z",
            "role": "main",
            "requested_model": "model-a",
            "response_model": "model-a",
            "prompt_tokens": 100,
            "completion_tokens": 50,
            "total_tokens": 150,
            "cached_prompt_tokens": 60,
            "uncached_prompt_tokens": 40,
            "request_token_estimate": {
                "bootstrap_prompt_tokens": 25,
                "tool_schema_tokens": 15,
                "live_conversation_history_tokens": 30,
                "inline_tool_transcript_tokens": 20,
                "memory_summary_tokens": 5,
                "pins_tokens": 3,
                "total_tokens": 98,
                "tool_schema_budget": {
                    "tool_count": 2,
                    "total_tokens": 15,
                    "budget_tokens": 10,
                    "over_budget_tokens": 5,
                    "over_budget": True,
                    "signature": "abc123",
                    "largest_tools": [
                        {
                            "name": "big_tool",
                            "token_estimate": 12,
                            "family": "mcp",
                        }
                    ],
                },
            },
            "input_cost_per_token": 0.001,
            "output_cost_per_token": 0.002,
            "cost_usd": 0.2,
            "usage_source": "api",
        }
    )

    totals = summary.totals()
    assert totals["cached_prompt_tokens"] == 60
    assert totals["uncached_prompt_tokens"] == 40
    assert totals["cache_read_input_tokens"] == 60
    assert totals["request_token_estimate"]["bootstrap_prompt_tokens"] == 25
    assert totals["request_token_estimate"]["tool_schema_tokens"] == 15
    assert totals["request_token_estimate"]["total_tokens"] == 98
    assert totals["request_token_estimate"]["tool_schema_budget_reported_calls"] == 1
    assert totals["request_token_estimate"]["tool_schema_budget_exceeded_calls"] == 1
    assert totals["request_token_estimate"]["tool_schema_budget_overage_tokens"] == 5
    assert totals["request_token_estimate"]["tool_schema_largest_tool_tokens"] == 12


def test_usage_summary_exposes_and_merges_raw_records() -> None:
    summary = UsageSummary()
    records = [
        UsageRecord(
            timestamp="2026-03-09T00:00:00Z",
            role="main",
            requested_model="model-a",
            response_model="model-a",
            prompt_tokens=9,
            completion_tokens=4,
            total_tokens=13,
            input_cost_per_token=0.1,
            output_cost_per_token=0.2,
            cost_usd=1.7,
            usage_source="api",
        ),
        UsageRecord(
            timestamp="2026-03-09T00:00:01Z",
            role="main:subagent:reviewer",
            requested_model="model-b",
            response_model="model-b",
            prompt_tokens=5,
            completion_tokens=2,
            total_tokens=7,
            input_cost_per_token=None,
            output_cost_per_token=None,
            cost_usd=None,
            usage_source="estimate",
        ),
    ]

    merged = summary.merge_records(records)

    assert merged == 2
    assert summary.records() == records
    totals = summary.totals()
    assert totals["prompt_tokens"] == 14
    assert totals["completion_tokens"] == 6
    assert totals["total_tokens"] == 20
    assert totals["calls"] == 2
    assert totals["api_usage_calls"] == 1
    assert totals["estimate_usage_calls"] == 1


def test_compute_context_left_returns_percent() -> None:
    meta = ModelMeta(
        model_name="gpt-5-nano",
        context_window_tokens=1000,
        max_output_tokens=256,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="fallback",
    )
    registry = _FakeRegistry(
        {
            "gpt-5-nano": meta,
        }
    )
    ctx = compute_context_left(
        messages=[
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Explain this module."},
        ],
        model_name="gpt-5-nano",
        registry=registry,  # type: ignore[arg-type]
        safety_margin_tokens=100,
    )
    assert ctx.max_input_tokens == meta.context_window_tokens
    assert ctx.context_window_tokens == meta.context_window_tokens
    assert ctx.effective_input_budget == compute_input_budget(meta, safety_margin=100)
    assert ctx.used_input_tokens > 0
    assert ctx.remaining_tokens is not None
    assert ctx.remaining_tokens < ctx.max_input_tokens
    assert ctx.percent_left is not None
    assert 0.0 <= ctx.percent_left <= 100.0


def test_compute_context_left_applies_conservative_provider_calibration() -> None:
    meta = ModelMeta(
        model_name="calibrated-model",
        context_window_tokens=4000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"calibrated-model": meta})
    messages = [{"role": "user", "content": "word " * 400}]
    estimated = estimate_request_token_breakdown(
        messages=messages,
        tool_list=None,
    ).total_tokens

    ctx = compute_context_left(
        messages=messages,
        model_name="calibrated-model",
        registry=registry,  # type: ignore[arg-type]
        safety_margin_tokens=100,
        prompt_estimate_multiplier=1.65,
    )

    assert ctx.used_input_tokens == math.ceil(estimated * 1.65)
    assert ctx.remaining_tokens == meta.context_window_tokens - ctx.used_input_tokens


def test_compute_context_left_projects_from_provider_visible_request_measurement() -> None:
    meta = ModelMeta(
        model_name="measured-model",
        context_window_tokens=8000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"measured-model": meta})
    anchor_messages = [{"role": "user", "content": "initial request"}]
    current_messages = [
        *anchor_messages,
        {"role": "assistant", "content": "provider response"},
    ]
    anchor_estimate = estimate_request_token_breakdown(
        messages=anchor_messages,
        tool_list=None,
    ).total_tokens
    current_estimate = estimate_request_token_breakdown(
        messages=current_messages,
        tool_list=None,
    ).total_tokens

    ctx = compute_context_left(
        messages=current_messages,
        model_name="measured-model",
        registry=registry,  # type: ignore[arg-type]
        safety_margin_tokens=100,
        prompt_estimate_multiplier=9.0,
        request_measurement=RequestContextMeasurement(
            input_tokens=1200,
            anchor_estimate_tokens=anchor_estimate,
            persistent_anchor_estimate_tokens=anchor_estimate,
            source="provider_response",
            confidence="authoritative",
            requested_model="measured-model",
            request_message_signatures=request_message_signatures(anchor_messages),
            persistent_message_signatures=request_message_signatures(anchor_messages),
        ),
    )

    assert ctx.used_input_tokens == 1200 + math.ceil(
        (current_estimate - anchor_estimate) * (1200 / anchor_estimate)
    )
    assert ctx.local_request_estimate_tokens == current_estimate
    assert ctx.token_count_source == "mixed"
    assert ctx.token_count_confidence == "estimated"
    assert ctx.anchor_token_count_source == "provider_response"
    assert ctx.anchor_token_count_confidence == "authoritative"
    assert ctx.provider_projection_applied is True
    assert ctx.dynamic_context_used_tokens == ctx.used_input_tokens
    assert ctx.dynamic_context_remaining_tokens == ctx.effective_remaining_tokens


def test_context_measurement_rejects_nonpersistent_media_overhead() -> None:
    meta = ModelMeta(
        model_name="measured-model",
        context_window_tokens=8000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"measured-model": meta})
    persistent = [{"role": "user", "content": "persistent request"}]
    provider_request = [
        *persistent,
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "inspect"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
            ],
        },
    ]
    current = [*persistent, {"role": "assistant", "content": "done"}]
    full_estimate = estimate_request_token_breakdown(
        messages=provider_request,
        tool_list=None,
    ).total_tokens
    persistent_estimate = estimate_request_token_breakdown(
        messages=persistent,
        tool_list=None,
    ).total_tokens

    ctx = compute_context_left(
        messages=current,
        model_name="measured-model",
        registry=registry,  # type: ignore[arg-type]
        request_measurement=RequestContextMeasurement(
            input_tokens=3000,
            anchor_estimate_tokens=full_estimate,
            persistent_anchor_estimate_tokens=persistent_estimate,
            source="provider_count",
            confidence="authoritative",
            requested_model="measured-model",
            request_message_signatures=request_message_signatures(provider_request),
            persistent_message_signatures=request_message_signatures(persistent),
            request_has_media=True,
            persistent_has_media=False,
        ),
    )

    assert ctx.token_count_source == "local_estimate"
    assert ctx.used_input_tokens == ctx.local_request_estimate_tokens


def test_request_message_signatures_distinguish_inline_media_payloads() -> None:
    first = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
        }
    ]
    second = [
        {
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}],
        }
    ]

    assert request_message_signatures(first) != request_message_signatures(second)


def test_context_measurement_rejects_media_added_after_the_anchor() -> None:
    meta = ModelMeta(
        model_name="measured-model",
        context_window_tokens=8000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"measured-model": meta})
    first_image = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}],
    }
    second_image = {
        "role": "user",
        "content": [{"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}],
    }

    for anchor, current in (
        (
            [{"role": "user", "content": "text anchor"}],
            [{"role": "user", "content": "text anchor"}, second_image],
        ),
        ([first_image], [first_image, second_image]),
    ):
        anchor_estimate = estimate_request_token_breakdown(
            messages=anchor,
            tool_list=None,
        ).total_tokens
        ctx = compute_context_left(
            messages=current,
            model_name="measured-model",
            registry=registry,  # type: ignore[arg-type]
            request_measurement=RequestContextMeasurement(
                input_tokens=3000,
                anchor_estimate_tokens=anchor_estimate,
                persistent_anchor_estimate_tokens=anchor_estimate,
                source="provider_count",
                confidence="authoritative",
                requested_model="measured-model",
                request_message_signatures=request_message_signatures(anchor),
                persistent_message_signatures=request_message_signatures(anchor),
                request_has_media=request_contains_media(anchor),
                persistent_has_media=request_contains_media(anchor),
            ),
        )

        assert ctx.token_count_source == "local_estimate"
        assert ctx.used_input_tokens == ctx.local_request_estimate_tokens


def test_context_measurement_is_authoritative_only_for_the_identical_request() -> None:
    meta = ModelMeta(
        model_name="measured-model",
        context_window_tokens=8000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"measured-model": meta})
    messages = [{"role": "user", "content": "same request"}]
    estimate = estimate_request_token_breakdown(
        messages=messages,
        tool_list=None,
    ).total_tokens

    ctx = compute_context_left(
        messages=messages,
        model_name="measured-model",
        registry=registry,  # type: ignore[arg-type]
        request_measurement=RequestContextMeasurement(
            input_tokens=321,
            anchor_estimate_tokens=estimate,
            source="provider_count",
            confidence="authoritative",
            requested_model="measured-model",
            request_message_signatures=request_message_signatures(messages),
            persistent_message_signatures=request_message_signatures(messages),
        ),
    )

    assert ctx.used_input_tokens == 321
    assert ctx.token_count_source == "provider_count"
    assert ctx.token_count_confidence == "authoritative"
    assert ctx.provider_projection_applied is False


def test_context_measurement_rejects_tool_or_prefix_changes() -> None:
    meta = ModelMeta(
        model_name="measured-model",
        context_window_tokens=8000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"measured-model": meta})
    anchor_messages = [{"role": "user", "content": "original"}]
    anchor_tools = [{"type": "function", "function": {"name": "one"}}]
    changed_tools = [{"type": "function", "function": {"name": "two"}}]
    measurement = RequestContextMeasurement(
        input_tokens=7000,
        anchor_estimate_tokens=100,
        source="provider_response",
        confidence="authoritative",
        requested_model="measured-model",
        request_message_signatures=request_message_signatures(anchor_messages),
        persistent_message_signatures=request_message_signatures(anchor_messages),
        tool_schema_signature=tool_schema_signature(anchor_tools),
    )

    tool_changed = compute_context_left(
        messages=anchor_messages,
        model_name="measured-model",
        registry=registry,  # type: ignore[arg-type]
        tool_list=changed_tools,
        request_measurement=measurement,
    )
    prefix_changed = compute_context_left(
        messages=[{"role": "user", "content": "rewritten"}],
        model_name="measured-model",
        registry=registry,  # type: ignore[arg-type]
        tool_list=anchor_tools,
        request_measurement=measurement,
    )

    assert tool_changed.token_count_source == "local_estimate"
    assert prefix_changed.token_count_source == "local_estimate"
    assert tool_changed.used_input_tokens == tool_changed.local_request_estimate_tokens
    assert prefix_changed.used_input_tokens == prefix_changed.local_request_estimate_tokens


def test_context_measurement_rejects_same_model_on_a_different_route() -> None:
    measurement = RequestContextMeasurement(
        input_tokens=100,
        anchor_estimate_tokens=80,
        source="provider_response",
        confidence="authoritative",
        requested_model="shared-model-name",
        provider_key="provider-a",
        protocol="openai_compat",
        base_url_host="api.provider-a.example",
    )

    assert measurement.matches_route(
        requested_model="shared-model-name",
        provider_key="provider-a",
        protocol="openai_compat",
        base_url_host="api.provider-a.example",
    )
    assert not measurement.matches_route(
        requested_model="shared-model-name",
        provider_key="provider-b",
        protocol="openai_compat",
        base_url_host="api.provider-a.example",
    )
    assert not measurement.matches_route(
        requested_model="shared-model-name",
        provider_key="provider-a",
        protocol="anthropic_messages",
        base_url_host="api.provider-a.example",
    )
    assert not measurement.matches_route(
        requested_model="shared-model-name",
        provider_key="provider-a",
        protocol="openai_compat",
        base_url_host="api.provider-b.example",
    )


def test_compute_context_left_uses_current_request_not_cumulative_cached_usage() -> None:
    meta = ModelMeta(
        model_name="cache-heavy-model",
        context_window_tokens=10000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"cache-heavy-model": meta})
    usage_summary = UsageSummary()
    usage_summary.add_event_payload(
        {
            "event_type": "llm_usage",
            "usage_schema_version": 2,
            "timestamp": "2026-02-24T00:00:00Z",
            "role": "main",
            "requested_model": "cache-heavy-model",
            "response_model": "cache-heavy-model",
            "prompt_tokens": 8000,
            "completion_tokens": 100,
            "total_tokens": 8100,
            "cache_read_input_tokens": 7600,
            "cached_prompt_tokens": 7600,
            "input_cost_per_token": None,
            "output_cost_per_token": None,
            "cost_usd": None,
            "usage_source": "api",
        }
    )
    messages = [{"role": "user", "content": "small current request"}]

    ctx = compute_context_left(
        messages=messages,
        model_name="cache-heavy-model",
        registry=registry,  # type: ignore[arg-type]
        safety_margin_tokens=100,
    )
    expected_used = estimate_request_token_breakdown(messages=messages, tool_list=None).total_tokens

    assert usage_summary.totals()["prompt_tokens"] == 8000
    assert usage_summary.totals()["cache_read_input_tokens"] == 7600
    assert ctx.used_input_tokens == expected_used
    assert ctx.used_input_tokens < 100
    assert ctx.remaining_tokens == meta.context_window_tokens - expected_used


def test_compute_context_left_uses_effective_input_budget() -> None:
    meta = ModelMeta(
        model_name="tiny-budget",
        context_window_tokens=1000,
        max_output_tokens=200,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"tiny-budget": meta})
    messages = [{"role": "user", "content": "word " * 650}]

    ctx = compute_context_left(
        messages=messages,
        model_name="tiny-budget",
        registry=registry,  # type: ignore[arg-type]
        safety_margin_tokens=100,
    )

    assert ctx.max_input_tokens == meta.context_window_tokens
    assert ctx.percent_left is not None
    assert ctx.percent_left > 25.0
    assert ctx.effective_input_budget == compute_input_budget(meta, safety_margin=100)
    assert ctx.effective_percent_left is not None
    assert ctx.effective_percent_left < 10.0


def test_compute_context_left_starts_dynamic_context_at_100_percent() -> None:
    meta = ModelMeta(
        model_name="fresh-chat",
        context_window_tokens=5000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"fresh-chat": meta})
    messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "<environment_context>\nrepo\n</environment_context>"},
    ]
    tool_list = [{"type": "function", "function": {"name": "read_file"}}]
    baseline = estimate_request_token_breakdown(
        messages=messages,
        tool_list=tool_list,
        pinned_prefix_len=len(messages),
    ).total_tokens

    ctx = compute_context_left(
        messages=messages,
        model_name="fresh-chat",
        registry=registry,  # type: ignore[arg-type]
        tool_list=tool_list,
        pinned_prefix_len=len(messages),
        safety_margin_tokens=100,
        startup_baseline_tokens=baseline,
    )

    assert ctx.used_input_tokens == baseline
    assert ctx.startup_baseline_tokens == baseline
    assert ctx.dynamic_context_used_tokens == 0
    assert ctx.dynamic_context_budget_tokens == ctx.effective_input_budget - baseline
    assert ctx.dynamic_context_percent_left == 100.0


def test_compute_context_left_dynamic_context_tracks_compacted_active_request() -> None:
    meta = ModelMeta(
        model_name="compacted-chat",
        context_window_tokens=6000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"compacted-chat": meta})
    baseline_messages = [
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "<environment_context>\nrepo\n</environment_context>"},
    ]
    baseline = estimate_request_token_breakdown(
        messages=baseline_messages,
        tool_list=None,
        pinned_prefix_len=len(baseline_messages),
    ).total_tokens
    before_compaction = [
        *baseline_messages,
        {"role": "user", "content": "word " * 1200},
        {"role": "assistant", "content": "answer " * 700},
    ]
    after_compaction = [
        *baseline_messages,
        {
            "role": "system",
            "content": "Compacted conversation summary: " + ("summary " * 80),
        },
    ]
    before_estimate = estimate_request_token_breakdown(
        messages=before_compaction, tool_list=None, pinned_prefix_len=len(baseline_messages)
    ).total_tokens
    measurement = RequestContextMeasurement(
        input_tokens=before_estimate * 2,
        anchor_estimate_tokens=before_estimate,
        persistent_anchor_estimate_tokens=before_estimate,
        source="provider_response",
        confidence="authoritative",
        request_message_signatures=request_message_signatures(before_compaction),
        persistent_message_signatures=request_message_signatures(before_compaction),
    )

    before = compute_context_left(
        messages=before_compaction,
        model_name="compacted-chat",
        registry=registry,  # type: ignore[arg-type]
        pinned_prefix_len=len(baseline_messages),
        safety_margin_tokens=100,
        startup_baseline_tokens=baseline,
        request_measurement=measurement,
    )
    after = compute_context_left(
        messages=after_compaction,
        model_name="compacted-chat",
        registry=registry,  # type: ignore[arg-type]
        pinned_prefix_len=len(baseline_messages),
        safety_margin_tokens=100,
        startup_baseline_tokens=baseline,
        request_measurement=measurement,
        prompt_estimate_multiplier=2.0,
    )

    # Compaction invalidates the provider anchor. Apply the calibrated local
    # estimate to both the new request and its baseline rather than carrying
    # the previous request's measured size into the compacted conversation.
    assert before.token_count_source == "provider_response"
    assert after.token_count_source == "local_estimate"
    assert after.startup_baseline_tokens == before.startup_baseline_tokens == baseline * 2
    assert before.dynamic_context_remaining_tokens == before.effective_remaining_tokens
    assert after.dynamic_context_remaining_tokens == after.effective_remaining_tokens
    assert before.dynamic_context_used_tokens > after.dynamic_context_used_tokens
    assert after.dynamic_context_used_tokens > 0
    assert before.dynamic_context_percent_left is not None
    assert after.dynamic_context_percent_left is not None
    assert after.dynamic_context_percent_left > before.dynamic_context_percent_left
    assert after.dynamic_context_percent_left < 100.0


@pytest.mark.parametrize("capacity_delta", [-400, 0, 700])
@pytest.mark.parametrize("append_reply", [False, True])
def test_conversation_gauge_preserves_provider_measured_headroom(
    capacity_delta: int, append_reply: bool
) -> None:
    from alysis_code.cli_impl.commands.startup import _chat_context_percent_value
    from alysis_code.cli_impl.tui.footer import footer_fragments
    from alysis_code.cli_impl.tui.state import TuiState

    meta = ModelMeta(
        model_name="measured-chat",
        context_window_tokens=8000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    startup = [{"role": "system", "content": "Coding assistant."}]
    request = [*startup, {"role": "user", "content": "input " * 5000}]
    baseline = estimate_request_token_breakdown(messages=startup, tool_list=None).total_tokens
    estimate = estimate_request_token_breakdown(messages=request, tool_list=None).total_tokens
    budget = compute_input_budget(meta, safety_margin=100)
    measured_tokens = budget + capacity_delta
    measurement = RequestContextMeasurement(
        input_tokens=measured_tokens,
        anchor_estimate_tokens=estimate,
        persistent_anchor_estimate_tokens=estimate,
        source="provider_response",
        confidence="authoritative",
        request_message_signatures=request_message_signatures(request),
        persistent_message_signatures=request_message_signatures(request),
    )
    messages = [*request]
    if append_reply:
        messages.append({"role": "assistant", "content": "response " * 80})
    ctx = compute_context_left(
        messages=messages,
        model_name=meta.model_name,
        registry=_FakeRegistry({meta.model_name: meta}),  # type: ignore[arg-type]
        startup_baseline_tokens=baseline,
        safety_margin_tokens=100,
        request_measurement=measurement,
    )

    # Budgeting projects the next request, while the visible gauge keeps the
    # last provider input against the full window (including startup and cache).
    assert ctx.local_request_estimate_tokens < budget
    assert ctx.used_input_tokens >= measured_tokens
    assert ctx.dynamic_context_remaining_tokens == max(0, budget - ctx.used_input_tokens)
    assert ctx.dynamic_context_remaining_tokens == ctx.effective_remaining_tokens
    assert ctx.startup_baseline_tokens == math.ceil(baseline * measured_tokens / estimate)
    assert ctx.dynamic_context_used_tokens + ctx.startup_baseline_tokens == ctx.used_input_tokens
    from alysis_code.provider_usage import capture_provider_usage

    client = SimpleNamespace(model=meta.model_name)
    session = SimpleNamespace(
        client=client,
        _hud_context_cache=ctx,
        last_provider_usage=capture_provider_usage(
            LLMUsage(measured_tokens, 10, measured_tokens + 10), client=client
        ),
    )
    percent = _chat_context_percent_value(session)
    assert percent == max(0, (8000 - measured_tokens) / 8000 * 100)
    footer = "".join(text for _, text in footer_fragments(TuiState(context_pct=percent), width=100))
    assert "context:" in footer


@pytest.mark.parametrize("scale", [0.65, 1.0, 1.8])
def test_measured_startup_and_calibrated_fallback_keep_the_same_baseline(scale: float) -> None:
    meta = ModelMeta(
        model_name="startup-chat",
        context_window_tokens=5000,
        max_output_tokens=500,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    startup = [{"role": "system", "content": "Instructions and environment. " * 40}]
    tools = [{"type": "function", "function": {"name": "read_file"}}]
    baseline = estimate_request_token_breakdown(messages=startup, tool_list=tools).total_tokens
    measurement = RequestContextMeasurement(
        input_tokens=math.ceil(baseline * scale),
        anchor_estimate_tokens=baseline,
        source="provider_count",
        confidence="authoritative",
        request_message_signatures=request_message_signatures(startup),
        tool_schema_signature=tool_schema_signature(tools),
    )
    for request_measurement in (measurement, None):
        ctx = compute_context_left(
            messages=startup,
            model_name=meta.model_name,
            registry=_FakeRegistry({meta.model_name: meta}),  # type: ignore[arg-type]
            tool_list=tools,
            startup_baseline_tokens=baseline,
            prompt_estimate_multiplier=scale,
            request_measurement=request_measurement,
        )
        assert ctx.startup_baseline_tokens == ctx.used_input_tokens
        assert ctx.dynamic_context_used_tokens == 0
        assert ctx.dynamic_context_remaining_tokens == ctx.effective_remaining_tokens
        assert ctx.dynamic_context_percent_left == 100


def test_compute_context_left_includes_tool_schema_tokens() -> None:
    meta = ModelMeta(
        model_name="tool-heavy",
        context_window_tokens=1000,
        max_output_tokens=200,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"tool-heavy": meta})
    messages = [{"role": "user", "content": "hello"}]
    tool_list = [
        {
            "type": "function",
            "function": {
                "name": "big_tool",
                "description": "large schema " * 200,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "payload": {
                            "type": "string",
                            "description": "x " * 1000,
                        },
                    },
                },
            },
        }
    ]

    ctx = compute_context_left(
        messages=messages,
        model_name="tool-heavy",
        registry=registry,  # type: ignore[arg-type]
        tool_list=tool_list,
        safety_margin_tokens=100,
    )
    expected_used = estimate_request_token_breakdown(
        messages=messages,
        tool_list=tool_list,
    ).total_tokens

    assert ctx.used_input_tokens == expected_used
    assert ctx.effective_remaining_tokens == 0
    assert ctx.effective_percent_left == 0.0


def test_compute_context_left_sanitizes_image_urls() -> None:
    meta = ModelMeta(
        model_name="vision-model",
        context_window_tokens=1000,
        max_output_tokens=200,
        input_cost_per_token=None,
        output_cost_per_token=None,
        raw_metadata={},
        source="test",
    )
    registry = _FakeRegistry({"vision-model": meta})
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "describe this"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64," + ("A" * 20_000)},
                },
            ],
        }
    ]

    ctx = compute_context_left(
        messages=messages,
        model_name="vision-model",
        registry=registry,  # type: ignore[arg-type]
        safety_margin_tokens=100,
    )
    expected_used = estimate_request_token_breakdown(
        messages=messages,
        tool_list=None,
    ).total_tokens

    assert estimate_prompt_tokens(messages) > 1000
    assert ctx.used_input_tokens == expected_used
    assert ctx.used_input_tokens < 200


def test_aggregate_usage_from_session_logs_reads_llm_usage_events(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    events = [
        {
            "type": "llm_usage",
            "payload": {
                "event_type": "llm_usage",
                "timestamp": "2026-02-24T00:00:00Z",
                "role": "main",
                "requested_model": "model-a",
                "response_model": "model-a",
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
                "input_cost_per_token": 1.0,
                "output_cost_per_token": 1.0,
                "cost_usd": 5.0,
                "usage_source": "api",
            },
        },
        {"type": "assistant_message", "payload": {"content": "ignored"}},
    ]
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    summary = aggregate_usage_from_session_logs([path])
    totals = summary.totals()
    assert totals["total_tokens"] == 5
    assert totals["cost_usd"] == 5.0


def test_format_usd_hud_rounding() -> None:
    assert format_usd(None, style="hud") == "n/a"
    assert format_usd(0.00004, style="hud") == "$0.000"
    assert format_usd(0.00149, style="hud") == "$0.001"
    assert format_usd(1.239, style="hud") == "$1.239"


def test_derived_uncached_tokens_are_not_counted_as_provider_reported() -> None:
    summary = UsageSummary()
    base = dict(
        timestamp="2026-07-31T00:00:00+00:00",
        role="main",
        requested_model="model",
        response_model="model",
        prompt_tokens=100,
        completion_tokens=1,
        total_tokens=101,
        input_cost_per_token=None,
        output_cost_per_token=None,
        cost_usd=None,
        usage_source="api",
    )
    summary.add_record(UsageRecord(**base, input_tokens_uncached=30))
    summary.add_record(
        UsageRecord(**base, input_tokens_uncached=20, input_tokens_uncached_derived=True)
    )

    totals = summary.totals()
    assert totals["input_tokens_uncached"] == 50
    assert totals["input_tokens_uncached_reported_calls"] == 1
    assert totals["input_tokens_uncached_derived_calls"] == 1


def test_build_usage_record_marks_host_derived_uncached_tokens() -> None:
    registry = _FakeRegistry(
        {
            "model": ModelMeta(
                model_name="model",
                context_window_tokens=200000,
                max_output_tokens=8192,
                input_cost_per_token=None,
                output_cost_per_token=None,
                raw_metadata={},
                source=BUNDLED_MODEL_CATALOG_SOURCE,
            )
        }
    )
    record = build_usage_record(
        role="main",
        requested_model="model",
        response_model="model",
        messages=[{"role": "user", "content": "hello"}],
        response_content="world",
        response_tool_calls=[],
        api_prompt_tokens=100,
        api_completion_tokens=10,
        api_total_tokens=110,
        api_cache_read_input_tokens=40,
        registry=registry,  # type: ignore[arg-type]
    )

    # The provider reported a cache read but never an uncached figure; the host
    # computed it, so it must not be presented as provider-reported.
    assert record.input_tokens_uncached == 60
    assert record.input_tokens_uncached_derived is True
    assert record.to_payload()["input_tokens_uncached_derived"] is True


@pytest.mark.parametrize("prompt,completion", [(None, 5), (10, None), (None, None), (0, None)])
def test_partial_usage_preserves_reported_total_and_missing_cache_denominator(prompt, completion):
    record = build_usage_record(
        role="main",
        requested_model="test",
        response_model=None,
        messages=[{"role": "user", "content": "some unmeasured input"}],
        response_content="some output",
        response_tool_calls=[],
        api_prompt_tokens=prompt,
        api_completion_tokens=completion,
        api_total_tokens=123,
        api_cached_prompt_tokens=8,
        registry=_FakeRegistry(
            {
                "test": ModelMeta(
                    model_name="test",
                    context_window_tokens=10000,
                    max_output_tokens=1000,
                    input_cost_per_token=None,
                    output_cost_per_token=None,
                    raw_metadata={},
                    source="test",
                )
            }
        ),
    )
    assert record.total_tokens == 123
    assert record.usage_source_detail == "mixed"
    assert record.usage_confidence == "estimated"
    assert record.raw_api_prompt_tokens == prompt
    assert record.raw_api_completion_tokens == completion
    if prompt is None or prompt < 8:
        assert record.input_tokens_uncached is None
        assert record.uncached_prompt_tokens is None
    if prompt is not None:
        assert record.prompt_tokens == prompt
    if completion is not None:
        assert record.completion_tokens == completion
    summary = UsageSummary()
    summary.add_event_payload(record.to_payload())
    assert summary.totals()["total_tokens"] == 123
    assert summary.records()[0].raw_api_prompt_tokens == prompt


@pytest.mark.parametrize("duration", ["5m", "1h"])
def test_zero_cache_write_price_is_not_replaced_by_default(duration):
    meta = ModelMeta(
        model_name="test",
        context_window_tokens=10000,
        max_output_tokens=1000,
        input_cost_per_token=1.0,
        output_cost_per_token=1.0,
        cache_creation_input_cost_per_token=2.0,
        **{f"cache_creation_{duration}_input_cost_per_token": 0.0},
        raw_metadata={},
        source="test",
    )
    record = build_usage_record(
        role="main",
        requested_model="test",
        response_model=None,
        messages=[],
        response_content="",
        response_tool_calls=[],
        api_prompt_tokens=100,
        api_completion_tokens=0,
        api_total_tokens=100,
        api_input_tokens_uncached=0,
        api_cache_creation_input_tokens=100,
        **{f"api_cache_creation_{duration}_input_tokens": 100},
        registry=_FakeRegistry({"test": meta}),
    )
    assert record.cost_usd == 0


@pytest.mark.parametrize(
    "protocol,raw",
    [
        ("gemini_interactions", {}),
        ("gemini_generate_content", {"toolUsePromptTokenCount": 2000}),
    ],
)
def test_legacy_aggregate_records_cannot_calibrate_a_request(protocol, raw):
    summary = UsageSummary()
    for _ in range(3):
        summary.add_event_payload(
            {
                "event_type": "llm_usage",
                "usage_schema_version": 6,
                "requested_model": "gemini",
                "protocol": protocol,
                "prompt_tokens": 90000,
                "completion_tokens": 10,
                "total_tokens": 90010,
                "prompt_estimate_error_ratio": 20,
                "raw_provider_usage": raw,
            }
        )
    snapshot = summary._calibration_snapshot_from_records(summary.records())
    assert snapshot["calibration_usable_calls"] == 0
    assert snapshot["calibration_excluded_aggregate_calls"] == 3
    assert summary.totals()["prompt_tokens"] == 270000
