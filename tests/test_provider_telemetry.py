from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from typer.testing import CliRunner

from alysis_code.cli import app as alysis_app
from alysis_code.config import AppConfig, save_config
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import (
    WebSearchCitation,
    WebSearchResponse,
    WebSearchSource,
)
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMResponse, LLMUsage
from alysis_code.provider_telemetry import (
    ProviderCallTelemetryRecorder,
    last_provider_call_summary,
    last_web_search_summary,
    provider_cache_diagnostics_snapshot,
    provider_cache_effectiveness_snapshot,
    provider_token_reconciliation_snapshot,
    record_web_search_call,
    reset_provider_telemetry_for_tests,
)
from alysis_code.tools.web_search import web_search


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        "ALYSIS_CONFIG_DIR": str(tmp_path),
        "ALYSIS_API_KEY": "",
        "OPENAI_API_KEY": "",
        "ANTHROPIC_API_KEY": "",
        "GEMINI_API_KEY": "",
        "TAVILY_API_KEY": "",
    }


def test_provider_call_telemetry_redacts_secrets_and_hidden_metadata() -> None:
    reset_provider_telemetry_for_tests()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "deepseek-chat",
                "choices": [
                    {
                        "message": {
                            "content": "visible answer",
                            "reasoning_content": "hidden provider reasoning",
                        }
                    }
                ],
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                },
            },
        )

    client = OpenAICompatClient(
        base_url="https://api.deepseek.com/v1",
        api_key="sk-secret-provider-key",
        model="deepseek-chat",
        provider_key="deepseek",
        transport=httpx.MockTransport(handler),
    )
    response = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "web_search",
                    "description": "look up sk-tool-argument-secret",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert response.provider_metadata
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["provider_key"] == "deepseek"
    assert summary["protocol"] == "openai_compat"
    assert summary["base_url_host"] == "api.deepseek.com"
    assert summary["web_search"]["backend_kind"] == "external"
    assert summary["provider_metadata_present"] is True
    assert summary["usage"]["total_tokens"] == 5
    rendered = json.dumps(summary, sort_keys=True)
    assert "sk-secret-provider-key" not in rendered
    assert "sk-tool-argument-secret" not in rendered
    assert "hidden provider reasoning" not in rendered
    assert "https://api.deepseek.com/v1" not in rendered


def test_provider_call_telemetry_records_safe_request_plan_only() -> None:
    reset_provider_telemetry_for_tests()
    recorder = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        request_plan={
            "input_mode": "previous_response_id",
            "continuation_strategy": "previous_response_id",
            "cache_strategy": "openai_prompt_cache",
            "cache_mode": "automatic",
            "request_message_count": 3,
            "message_count": 3,
            "tool_count": 1,
            "stable_prefix_message_count": 2,
            "dynamic_suffix_message_count": 1,
            "provider_metadata_message_count": 1,
            "stable_prefix_estimated_tokens": 20,
            "dynamic_suffix_estimated_tokens": 10,
            "tool_schema_tokens": 5,
            "total_estimated_tokens": 35,
            "serialized_request_estimate_tokens": 40,
            "sent_serialized_request_estimate_tokens": 12,
            "cacheable_prefix_hash": "abc123",
            "request_messages_signature": "def456",
            "tool_schema_hash": "fedcba",
            "full_input_item_count": 3,
            "sent_input_item_count": 1,
            "previous_response_id_used": True,
            "continuation_anchor_index": 1,
            "messages": [{"role": "user", "content": "hidden"}],
        },
        operation="responses_chat",
    )

    recorder.record_success(LLMResponse(content="ok", tool_calls=[], raw={}))

    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["request_plan"] == {
        "input_mode": "previous_response_id",
        "continuation_strategy": "previous_response_id",
        "cache_strategy": "openai_prompt_cache",
        "cache_mode": "automatic",
        "previous_response_id_used": True,
        "message_count": 3,
        "request_message_count": 3,
        "tool_count": 1,
        "stable_prefix_message_count": 2,
        "dynamic_suffix_message_count": 1,
        "provider_metadata_message_count": 1,
        "stable_prefix_estimated_tokens": 20,
        "dynamic_suffix_estimated_tokens": 10,
        "tool_schema_tokens": 5,
        "total_estimated_tokens": 35,
        "serialized_request_estimate_tokens": 40,
        "sent_serialized_request_estimate_tokens": 12,
        "full_input_item_count": 3,
        "sent_input_item_count": 1,
        "continuation_anchor_index": 1,
        "cacheable_prefix_hash": "abc123",
        "request_messages_signature": "def456",
        "tool_schema_hash": "fedcba",
    }
    rendered = json.dumps(summary, sort_keys=True)
    assert "hidden" not in rendered


def test_provider_call_telemetry_records_safe_cache_policy_metadata() -> None:
    reset_provider_telemetry_for_tests()
    recorder = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        cache_policy={
            "status": "enabled",
            "strategy": "openai_prompt_cache",
            "mode": "automatic",
            "enabled": True,
            "capability_source": "profile",
            "source": "profile",
            "usage_schema": "openai",
            "allowed_fields": ["prompt_cache_key", "prompt_cache_retention"],
            "emitted_fields": ["prompt_cache_key"],
            "trusted_usage_fields": ["cache_read_input_tokens"],
            "warnings": ["custom profile declared prompt cache support"],
            "prompt_cache_key_hash": hashlib.sha256(b"repo-main").hexdigest(),
            "prompt_cache_key": "repo-main-should-not-be-recorded",
        },
        operation="responses_chat",
    )

    recorder.record_success(LLMResponse(content="ok", tool_calls=[], raw={}))

    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["cache_policy"] == {
        "status": "enabled",
        "strategy": "openai_prompt_cache",
        "mode": "automatic",
        "enabled": True,
        "capability_source": "profile",
        "source": "profile",
        "usage_schema": "openai",
        "allowed_fields": ["prompt_cache_key", "prompt_cache_retention"],
        "emitted_fields": ["prompt_cache_key"],
        "trusted_usage_fields": ["cache_read_input_tokens"],
        "warnings": ["custom profile declared prompt cache support"],
        "prompt_cache_key_hash": hashlib.sha256(b"repo-main").hexdigest(),
    }
    assert "repo-main-should-not-be-recorded" not in json.dumps(summary, sort_keys=True)


def test_provider_call_telemetry_records_safe_request_shape_only() -> None:
    reset_provider_telemetry_for_tests()
    recorder = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_compat",
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        request_shape={
            "schema_version": 1,
            "input_mode": "full",
            "message_count": 2,
            "tool_count": 1,
            "cache_enabled": True,
            "cache_eligible": True,
            "cache_used": True,
            "cache_fields_emitted": True,
            "top_level_cache_control_present": False,
            "cached_content_attached": False,
            "affinity_field_emitted": False,
            "cache_strategy": "qwen_cache_control_blocks",
            "cache_status": "enabled",
            "emitted_cache_fields": ["cache_control"],
            "cache_control_block_count": 1,
            "explicit_cache_control_block_count": 1,
            "cacheable_prefix_message_count": 1,
            "cacheable_prefix_estimated_tokens": 42,
            "cacheable_surface_estimated_tokens": 60,
            "min_cacheable_tokens": 1,
            "total_estimated_tokens": 100,
            "tool_schema_share": 0.18,
            "inline_tool_transcript_share": 0.0,
            "risk_reasons": ["safe-derived-warning"],
            "token_breakdown": {
                "bootstrap_prompt_tokens": 42,
                "tool_schema_tokens": 18,
                "live_conversation_history_tokens": 40,
                "total_tokens": 100,
                "content": "hidden prompt should not be recorded",
            },
            "messages": [{"role": "user", "content": "hidden"}],
        },
        operation="chat_completions",
    )

    recorder.record_success(LLMResponse(content="ok", tool_calls=[], raw={}))

    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["request_shape"]["cache_strategy"] == "qwen_cache_control_blocks"
    assert summary["request_shape"]["emitted_cache_fields"] == ["cache_control"]
    assert summary["request_shape"]["token_breakdown"] == {
        "bootstrap_prompt_tokens": 42,
        "tool_schema_tokens": 18,
        "live_conversation_history_tokens": 40,
        "total_tokens": 100,
    }
    rendered = json.dumps(summary, sort_keys=True)
    assert "hidden prompt" not in rendered
    assert "hidden" not in rendered


def test_provider_cache_effectiveness_snapshot_aggregates_safe_metrics() -> None:
    reset_provider_telemetry_for_tests()

    first_openai = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        cache_policy={
            "strategy": "openai_prompt_cache",
            "enabled": True,
            "mode": "automatic",
        },
        operation="responses_chat",
    )
    first_openai.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(
                prompt_tokens=100,
                completion_tokens=2,
                total_tokens=102,
                cached_prompt_tokens=0,
                cache_read_input_tokens=0,
                input_tokens_uncached=100,
            ),
        )
    )
    second_openai = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        cache_policy={
            "strategy": "openai_prompt_cache",
            "enabled": True,
            "mode": "automatic",
        },
        operation="responses_chat",
    )
    second_openai.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(
                prompt_tokens=100,
                completion_tokens=2,
                total_tokens=102,
                cached_prompt_tokens=50,
                cache_read_input_tokens=50,
                input_tokens_uncached=50,
            ),
        )
    )
    anthropic = ProviderCallTelemetryRecorder(
        provider_key="anthropic",
        protocol="anthropic_messages",
        model="claude-haiku",
        base_url="https://api.anthropic.com/v1",
        stream=False,
        tools=None,
        cache_policy={
            "strategy": "anthropic_cache_control",
            "enabled": True,
            "mode": "automatic",
            "ttl": "5m",
        },
        operation="anthropic_messages_chat",
    )
    anthropic.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(
                prompt_tokens=100,
                completion_tokens=2,
                total_tokens=102,
                cached_prompt_tokens=0,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=70,
                cache_creation_5m_input_tokens=70,
                input_tokens_uncached=30,
            ),
        )
    )

    snapshot = provider_cache_effectiveness_snapshot()

    totals = snapshot["totals"]
    assert snapshot["window_call_count"] == 3
    assert totals["provider_call_count"] == 3
    assert totals["cache_enabled_call_count"] == 3
    assert totals["cache_read_call_count"] == 1
    assert totals["cache_write_call_count"] == 1
    assert totals["cache_miss_call_count"] == 1
    assert totals["strategy_counts"] == {
        "anthropic_cache_control": 1,
        "openai_prompt_cache": 2,
    }
    assert totals["token_totals"]["effective_cache_read_input_tokens"] == 50
    assert totals["token_totals"]["effective_cache_write_input_tokens"] == 70
    assert totals["cache_read_ratio"] == 0.1667
    openai_group = next(
        item for item in snapshot["by_provider_model"] if item["provider_key"] == "openai"
    )
    assert openai_group["provider_call_count"] == 2
    assert openai_group["cache_read_call_count"] == 1
    assert openai_group["token_totals"]["effective_cache_read_input_tokens"] == 50


def test_provider_cache_diagnostics_snapshot_aggregates_route_metrics() -> None:
    reset_provider_telemetry_for_tests()
    first = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        cache_policy={
            "strategy": "openai_prompt_cache",
            "enabled": True,
            "mode": "automatic",
            "status": "enabled",
            "emitted_fields": ["prompt_cache_key"],
        },
        request_shape={
            "cache_fields_emitted": True,
            "tool_schema_share": 0.25,
            "inline_tool_transcript_share": 0.1,
            "risk_reasons": ["large_tool_schema_share"],
            "compaction_trigger_reason": "cache_aware_budget",
        },
        token_reconciliation={
            "input_estimate_tokens": 90,
            "sent_input_estimate_tokens": 80,
            "estimator": "cl100k_base",
            "estimate_basis": "provider_prompt_payload",
            "input_mode": "full",
        },
        operation="responses_chat",
    )
    first.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(
                prompt_tokens=100,
                completion_tokens=2,
                total_tokens=102,
                cached_prompt_tokens=40,
                cache_read_input_tokens=40,
                input_tokens_uncached=60,
            ),
        )
    )
    second = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        cache_policy={
            "strategy": "openai_prompt_cache",
            "enabled": True,
            "mode": "automatic",
            "status": "create_rejected",
            "fallback": "stripped_rejected_cache_fields",
            "capability_downgrade": "profile_runtime_rejection",
        },
        request_shape={
            "cache_fields_emitted": False,
            "tool_schema_share": 0.05,
            "inline_tool_transcript_share": 0.3,
            "risk_reasons": ["large_inline_tool_transcript_share"],
            "compaction_trigger_reasons": ["cache_aware_high_water"],
        },
        operation="responses_chat",
    )
    second.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(
                prompt_tokens=80,
                completion_tokens=2,
                total_tokens=82,
                cached_prompt_tokens=0,
                cache_read_input_tokens=0,
                input_tokens_uncached=80,
            ),
        )
    )

    snapshot = provider_cache_diagnostics_snapshot()

    totals = snapshot["totals"]
    assert snapshot["window_call_count"] == 2
    assert totals["provider_call_count"] == 2
    assert totals["cache_field_emitted_call_count"] == 1
    assert totals["cache_field_emitted_rate"] == 0.5
    assert totals["cache_read_call_count"] == 1
    assert totals["cache_read_rate"] == 0.5
    assert totals["cache_fallback_call_count"] == 1
    assert totals["provider_rejection_or_downgrade_call_count"] == 1
    assert totals["provider_rejection_or_downgrade_rate"] == 0.5
    assert totals["cache_risk_reason_counts"] == {
        "large_inline_tool_transcript_share": 1,
        "large_tool_schema_share": 1,
    }
    assert totals["compaction_trigger_reason_counts"] == {
        "cache_aware_budget": 1,
        "cache_aware_high_water": 1,
    }
    assert totals["tool_schema_share_average"] == 0.15
    assert totals["inline_tool_transcript_share_average"] == 0.2
    assert totals["mean_input_estimate_abs_error_tokens"] == 10.0
    assert totals["mean_sent_input_estimate_abs_error_tokens"] == 20.0
    assert len(snapshot["by_route"]) == 1
    route = snapshot["by_route"][0]
    assert route["provider_key"] == "openai"
    assert route["protocol"] == "openai_responses"
    assert route["operation"] == "responses_chat"
    assert route["cache_read_rate"] == 0.5


def test_provider_token_reconciliation_records_and_aggregates_estimate_drift() -> None:
    reset_provider_telemetry_for_tests()
    first = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        token_reconciliation={
            "input_estimate_tokens": 90,
            "sent_input_estimate_tokens": 80,
            "estimator": "cl100k_base",
            "estimate_basis": "provider_prompt_payload",
            "input_mode": "full",
            "messages": [{"role": "user", "content": "hidden"}],
        },
        operation="responses_chat",
    )
    first.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(
                prompt_tokens=100,
                completion_tokens=2,
                total_tokens=102,
                cached_prompt_tokens=10,
                cache_read_input_tokens=10,
                input_tokens_uncached=90,
            ),
        )
    )
    second = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        token_reconciliation={
            "input_estimate_tokens": 120,
            "sent_input_estimate_tokens": 120,
            "estimator": "cl100k_base",
            "estimate_basis": "provider_prompt_payload",
            "input_mode": "full",
        },
        operation="responses_chat",
    )
    second.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(
                prompt_tokens=100,
                completion_tokens=2,
                total_tokens=102,
                cached_prompt_tokens=0,
                cache_read_input_tokens=0,
                input_tokens_uncached=100,
            ),
        )
    )

    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["token_reconciliation"]["input_estimate_error_tokens"] == -20
    assert summary["token_reconciliation"]["input_estimate_error_ratio"] == 0.8333
    rendered = json.dumps(summary, sort_keys=True)
    assert "hidden" not in rendered

    snapshot = provider_token_reconciliation_snapshot()
    totals = snapshot["totals"]
    assert snapshot["window_call_count"] == 2
    assert totals["reconciliation_call_count"] == 2
    assert totals["reported_prompt_call_count"] == 2
    assert totals["undercount_call_count"] == 1
    assert totals["overcount_call_count"] == 1
    assert totals["input_estimate_tokens_total"] == 210
    assert totals["reported_input_estimate_tokens_total"] == 210
    assert totals["reported_prompt_tokens_total"] == 200
    assert totals["input_estimate_abs_error_tokens_total"] == 30
    assert totals["mean_abs_error_tokens"] == 15.0
    assert totals["reported_to_estimate_ratio"] == 0.9524
    assert totals["estimator_counts"] == {"cl100k_base": 2}


def test_token_reconciliation_ratio_ignores_calls_without_reported_usage() -> None:
    reset_provider_telemetry_for_tests()
    reported = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        token_reconciliation={
            "input_estimate_tokens": 1000,
            "estimator": "cl100k_base",
            "estimate_basis": "provider_prompt_payload",
        },
        operation="responses_chat",
    )
    reported.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(
                prompt_tokens=1000,
                completion_tokens=2,
                total_tokens=1002,
            ),
        )
    )
    unreported = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        token_reconciliation={
            "input_estimate_tokens": 1000,
            "estimator": "cl100k_base",
            "estimate_basis": "provider_prompt_payload",
        },
        operation="responses_chat",
    )
    unreported.record_success(LLMResponse(content="ok", tool_calls=[], raw={}, usage=None))

    snapshot = provider_token_reconciliation_snapshot()
    totals = snapshot["totals"]
    assert totals["reconciliation_call_count"] == 2
    assert totals["reported_prompt_call_count"] == 1
    assert totals["input_estimate_tokens_total"] == 2000
    assert totals["reported_input_estimate_tokens_total"] == 1000
    assert totals["reported_prompt_tokens_total"] == 1000
    assert totals["reported_to_estimate_ratio"] == 1.0
    route = snapshot["by_provider_model"][0]
    assert route["reported_to_estimate_ratio"] == 1.0


def test_cache_diagnostics_mean_sent_abs_error_uses_contributing_samples() -> None:
    reset_provider_telemetry_for_tests()
    with_sent = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        token_reconciliation={
            "input_estimate_tokens": 90,
            "sent_input_estimate_tokens": 80,
            "estimator": "cl100k_base",
            "estimate_basis": "provider_prompt_payload",
        },
        operation="responses_chat",
    )
    with_sent.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(prompt_tokens=100, completion_tokens=2, total_tokens=102),
        )
    )
    without_sent = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-4.1-nano",
        base_url="https://api.openai.com/v1",
        stream=False,
        tools=None,
        token_reconciliation={
            "input_estimate_tokens": 100,
            "estimator": "cl100k_base",
            "estimate_basis": "provider_prompt_payload",
        },
        operation="responses_chat",
    )
    without_sent.record_success(
        LLMResponse(
            content="ok",
            tool_calls=[],
            raw={},
            usage=LLMUsage(prompt_tokens=130, completion_tokens=2, total_tokens=132),
        )
    )

    totals = provider_cache_diagnostics_snapshot()["totals"]
    assert totals["token_estimate_error_sample_count"] == 2
    assert totals["sent_token_estimate_error_sample_count"] == 1
    assert totals["input_estimate_abs_error_tokens_total"] == 40
    assert totals["sent_input_estimate_abs_error_tokens_total"] == 20
    assert totals["mean_input_estimate_abs_error_tokens"] == 20.0
    assert totals["mean_sent_input_estimate_abs_error_tokens"] == 20.0


def test_streaming_telemetry_counts_events_deltas_and_first_token_latency(monkeypatch) -> None:
    reset_provider_telemetry_for_tests()
    timestamps = iter([1000.0, 1012.0, 1050.0])
    monkeypatch.setattr(
        "alysis_code.provider_telemetry.telemetry_clock_ms",
        lambda: next(timestamps),
    )
    deltas: list[str] = []

    def sse_event(payload: dict[str, Any]) -> str:
        return f"data: {json.dumps(payload)}\n\n"

    body = "".join(
        [
            sse_event({"model": "test-model", "choices": [{"delta": {"content": "Hel"}}]}),
            sse_event(
                {
                    "choices": [{"delta": {"content": "lo"}}],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 2,
                        "total_tokens": 3,
                    },
                }
            ),
            "data: [DONE]\n\n",
        ]
    )
    client = OpenAICompatClient(
        base_url="https://example.com/v1",
        api_key="test-key",
        model="test-model",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)),
    )

    response = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        on_text_delta=deltas.append,
    )

    assert response.content == "Hello"
    assert deltas == ["Hel", "lo"]
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["stream"] is True
    assert summary["streaming"]["event_count"] == 2
    assert summary["streaming"]["text_delta_count"] == 2
    assert summary["streaming"]["first_token_latency_ms"] == 12
    assert summary["streaming"]["final_latency_ms"] == 50
    assert summary["usage"]["total_tokens"] == 3


def test_streaming_telemetry_counts_reasoning_without_storing_content(monkeypatch) -> None:
    reset_provider_telemetry_for_tests()
    timestamps = iter([1000.0, 1015.0, 1060.0])
    monkeypatch.setattr(
        "alysis_code.provider_telemetry.telemetry_clock_ms",
        lambda: next(timestamps),
    )
    received: list[str] = []
    recorder = ProviderCallTelemetryRecorder(
        provider_key="openai",
        protocol="openai_responses",
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        stream=True,
        tools=None,
        operation="responses_chat",
    )
    callback = recorder.wrap_reasoning_delta(received.append)
    assert callback is not None
    callback("secret reasoning one ")
    callback("secret reasoning two")
    recorder.record_success(
        LLMResponse(
            content="visible",
            tool_calls=[],
            raw={"stream_metadata": {"events": 4}},
        )
    )

    summary = last_provider_call_summary()
    assert summary is not None
    assert received == ["secret reasoning one ", "secret reasoning two"]
    assert summary["streaming"]["reasoning_delta_count"] == 2
    assert summary["streaming"]["first_reasoning_latency_ms"] == 15
    assert summary["streaming"]["final_latency_ms"] == 60
    assert "secret reasoning" not in json.dumps(summary)


class _TelemetryTruncatedSseStream(httpx.SyncByteStream):
    def __iter__(self):  # type: ignore[no-untyped-def]
        yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        raise httpx.RemoteProtocolError("incomplete chunked read")


def test_stream_retry_fires_stream_restart_hook_after_emitted_tokens() -> None:
    # The duck-typed reset channel: when a retry re-runs a streamed request
    # AFTER tokens already reached the delta callback, the client must invoke
    # callback.stream_restart before the restream so the surface can clear
    # its live block (the visible half of the doubled-reply fix).
    reset_provider_telemetry_for_tests()
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(200, stream=_TelemetryTruncatedSseStream())
        body = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=body)

    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-test",
        provider_key="openai",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _seconds: None,
        provider_random_fn=lambda: 0.5,
    )

    deltas: list[str] = []
    restarts: list[int] = []

    def on_delta(text: str) -> None:
        deltas.append(text)

    on_delta.stream_restart = lambda: restarts.append(len(deltas))  # type: ignore[attr-defined]

    response = client.chat(
        messages=[{"role": "user", "content": "hi"}],
        stream=True,
        on_text_delta=on_delta,
    )
    assert response.content == "ok"
    # "partial" streamed before the failure: the hook fired exactly once,
    # between the abandoned tokens and the restreamed reply.
    assert deltas == ["partial", "ok"]
    assert restarts == [1]


@pytest.mark.parametrize("retry_reason", ["provider_stream_truncated", "transport_connection_drop"])
def test_stream_retry_telemetry_records_restart_without_raw_content(retry_reason: str) -> None:
    reset_provider_telemetry_for_tests()
    attempts = 0

    class EofStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            if retry_reason == "transport_connection_drop":
                raise httpx.RemoteProtocolError("peer closed connection mid-response")

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            # Clean premature EOF exercises provider_stream_truncated. A raised
            # RemoteProtocolError belongs to the existing connection-drop path.
            return httpx.Response(200, stream=EofStream())
        body = 'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
        return httpx.Response(200, content=body)

    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-test",
        provider_key="openai",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(max_retries=1),
        provider_sleep_fn=lambda _seconds: None,
        provider_random_fn=lambda: 0.5,
    )

    assert client.chat(messages=[{"role": "user", "content": "hi"}], stream=True).content == "ok"

    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["retry_reasons"] == [retry_reason]
    assert summary["streaming"]["stream_restart_count"] == 1
    assert summary["streaming"]["stream_restart_reason"] == retry_reason
    rendered = json.dumps(summary, sort_keys=True)
    assert "partial" not in rendered


def test_provider_retry_telemetry_uses_fake_clock_without_sleep(monkeypatch) -> None:
    reset_provider_telemetry_for_tests()
    timestamps = iter([2000.0, 2033.0])
    monkeypatch.setattr(
        "alysis_code.provider_telemetry.telemetry_clock_ms",
        lambda: next(timestamps),
    )
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(429, text="rate limit")
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    client = OpenAICompatClient(
        base_url="https://api.openai.com/v1",
        api_key="test-key",
        model="gpt-test",
        provider_key="openai",
        transport=httpx.MockTransport(handler),
        provider_retry_settings=ProviderRetrySettings(
            max_retries=1,
            base_delay_seconds=0.0,
            max_delay_seconds=0.0,
        ),
        provider_sleep_fn=lambda _seconds: None,
        provider_random_fn=lambda: 0.5,
    )

    response = client.chat(messages=[{"role": "user", "content": "hi"}])

    assert response.content == "ok"
    assert attempts == 2
    summary = last_provider_call_summary()
    assert summary is not None
    assert summary["retry_count"] == 1
    assert summary["retry_reasons"] == ["provider_throttled"]
    assert summary["status_category"] == "success"
    assert summary["latency_ms"] == 33


def test_web_search_telemetry_records_native_and_external_labels(monkeypatch) -> None:
    reset_provider_telemetry_for_tests()
    monkeypatch.delenv("ALYSIS_WEB_SEARCH_PROVIDER", raising=False)

    class _FakeClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def web_search(self, **_kwargs: object) -> WebSearchResponse:
            return WebSearchResponse(
                answer="answer",
                citations=[
                    WebSearchCitation(title="Docs", url="https://example.test/docs"),
                ],
                sources=[WebSearchSource(url="https://example.test/docs", title="Docs")],
                queries=["docs query"],
                raw={"id": "resp_search"},
                response_id="resp_search",
                model="search-model",
            )

    result = web_search(
        query="docs query",
        cfg=AppConfig(
            model="gpt-test",
            base_url="https://api.openai.com/v1",
            web_search_mode="native",
            web_search_adapter="openai_responses",
        ),
        api_key="main-key",
        client_factory=_FakeClient,
    )

    assert result["provider_hosted_search"] is True
    native = last_web_search_summary()
    assert native is not None
    assert native["provider_hosted_search"] is True
    assert native["external_provider_name"] == ""
    assert native["source_count"] == 1
    assert native["citation_count"] == 1
    assert native["query_count"] == 1

    record_web_search_call(
        protocol="openai_compat",
        provider_key="tavily",
        model=None,
        web_search_mode="auto",
        web_search_adapter="tavily",
        provider_hosted_search=False,
        external_provider_name="tavily",
        source_count=2,
        citation_count=2,
        query_count=1,
        fallback_occurred=True,
    )
    external = last_web_search_summary()
    assert external is not None
    assert external["provider_hosted_search"] is False
    assert external["external_provider_name"] == "tavily"
    assert external["fallback_occurred"] is True


def test_doctor_bundle_is_redacted_and_excludes_hidden_values(
    monkeypatch,
    tmp_path: Path,
) -> None:
    reset_provider_telemetry_for_tests()
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path))
    cfg = AppConfig(
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        api_key_env="OPENAI_API_KEY",
    )
    save_config(cfg)
    record_web_search_call(
        protocol="openai_compat",
        provider_key="tavily",
        model="gpt-test",
        web_search_mode="external",
        web_search_adapter="tavily",
        provider_hosted_search=False,
        external_provider_name="tavily",
        source_count=1,
        citation_count=1,
        query_count=1,
        fallback_occurred=False,
    )

    result = CliRunner().invoke(
        alysis_app,
        ["doctor", "bundle", "--redacted"],
        env={**_env(tmp_path), "OPENAI_API_KEY": "sk-openai-secret-value"},
    )

    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["redacted"] is True
    assert "provider_diagnostics" in payload
    assert "cache_effectiveness" in payload
    assert "cache_diagnostics" in payload
    assert "token_reconciliation" in payload
    assert "recent_web_search_calls" in payload
    rendered = json.dumps(payload, sort_keys=True)
    assert "sk-openai-secret-value" not in rendered
    assert "provider_metadata" not in rendered
    assert "tool arguments" in rendered
