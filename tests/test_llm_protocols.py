from __future__ import annotations

import pytest

from alysis_code.config import ConfigError
from alysis_code.llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    GEMINI_INTERACTIONS_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
    SUPPORTED_LLM_PROTOCOLS,
    UnsupportedProtocolError,
    default_usage_contract_for_protocol,
    get_provider_protocol_capabilities,
    resolve_reasoning_trace_capability,
)
from alysis_code.llm.types import BillingMode, ReasoningOutputKind


def test_supported_llm_protocols_include_native_foundation_values() -> None:
    assert SUPPORTED_LLM_PROTOCOLS == {
        OPENAI_COMPAT_PROTOCOL,
        OPENAI_RESPONSES_PROTOCOL,
        ANTHROPIC_MESSAGES_PROTOCOL,
        GEMINI_GENERATE_CONTENT_PROTOCOL,
        GEMINI_INTERACTIONS_PROTOCOL,
    }


def test_unsupported_protocol_error_is_config_error() -> None:
    assert issubclass(UnsupportedProtocolError, ConfigError)


def test_unknown_provider_routes_keep_protocol_specific_counting_strategy() -> None:
    expected = {
        OPENAI_COMPAT_PROTOCOL: "openai_compat_provider_payload",
        OPENAI_RESPONSES_PROTOCOL: "openai_responses",
        ANTHROPIC_MESSAGES_PROTOCOL: "anthropic_messages",
        GEMINI_GENERATE_CONTENT_PROTOCOL: "gemini_count_tokens",
        GEMINI_INTERACTIONS_PROTOCOL: "gemini_count_tokens_projection",
    }

    for protocol, strategy in expected.items():
        contract = default_usage_contract_for_protocol(protocol)
        assert contract.input_token_count_strategy == strategy
        assert contract.response_usage_confidence.value == "reported"


def test_provider_protocol_capabilities_describe_native_web_search_adapters() -> None:
    anthropic_compat = get_provider_protocol_capabilities(
        provider_key="anthropic",
        protocol=OPENAI_COMPAT_PROTOCOL,
    )
    openai_responses = get_provider_protocol_capabilities(
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
    )
    anthropic = get_provider_protocol_capabilities(
        provider_key="anthropic",
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
    )
    gemini = get_provider_protocol_capabilities(
        provider_key="gemini",
        protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
    )
    interactions = get_provider_protocol_capabilities(
        provider_key="gemini",
        protocol=GEMINI_INTERACTIONS_PROTOCOL,
    )

    assert anthropic_compat is not None
    assert anthropic_compat.supports_native_web_search is False
    assert anthropic_compat.supports_provider_hosted_web_search_adapter is False
    assert anthropic_compat.default_web_search_adapter == "anthropic_messages"

    assert openai_responses is not None
    assert openai_responses.supports_streaming is True
    assert openai_responses.supports_tool_calling is True
    assert openai_responses.supports_forced_tool_choice is True
    assert openai_responses.supports_provider_hosted_web_search_adapter is True
    assert openai_responses.default_web_search_adapter == "openai_responses"
    assert any("Native OpenAI Responses chat supports" in q for q in openai_responses.quirks)

    assert anthropic is not None
    assert anthropic.supports_streaming is True
    assert anthropic.supports_tool_calling is True
    assert anthropic.supports_forced_tool_choice is True
    assert anthropic.supports_native_web_search is True
    assert anthropic.supports_provider_hosted_web_search_adapter is True
    assert anthropic.default_web_search_adapter == "anthropic_messages"
    assert "reasoning_effort" not in anthropic.unsupported_parameters
    assert any("Native Anthropic Messages chat supports" in q for q in anthropic.quirks)

    assert gemini is not None
    assert gemini.supports_streaming is True
    assert gemini.supports_tool_calling is True
    assert gemini.supports_forced_tool_choice is True
    assert gemini.supports_native_web_search is True
    assert gemini.supports_provider_hosted_web_search_adapter is True
    assert gemini.default_web_search_adapter == "gemini_grounding"
    assert "reasoning_effort:xhigh" not in gemini.unsupported_parameters
    assert any("Native Gemini GenerateContent chat supports" in q for q in gemini.quirks)

    assert interactions is not None
    assert interactions.supports_streaming is False
    assert interactions.supports_tool_calling is False
    assert interactions.supports_forced_tool_choice is False
    assert interactions.supports_provider_hosted_web_search_adapter is False
    assert interactions.default_web_search_adapter == "gemini_grounding"
    assert "tools" in interactions.unsupported_parameters
    assert any("Experimental Gemini Interactions prototype" in q for q in interactions.quirks)


def test_reasoning_trace_capabilities_only_mark_safe_summaries_as_displayable() -> None:
    openai = resolve_reasoning_trace_capability(
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
    )
    anthropic = resolve_reasoning_trace_capability(
        provider_key="anthropic",
        protocol=ANTHROPIC_MESSAGES_PROTOCOL,
    )
    gemini = resolve_reasoning_trace_capability(
        provider_key="gemini",
        protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
    )
    interactions = resolve_reasoning_trace_capability(
        provider_key="gemini",
        protocol=GEMINI_INTERACTIONS_PROTOCOL,
    )
    deepseek = resolve_reasoning_trace_capability(
        provider_key="deepseek",
        protocol=OPENAI_COMPAT_PROTOCOL,
    )
    mistral = resolve_reasoning_trace_capability(
        provider_key="mistral",
        protocol=OPENAI_COMPAT_PROTOCOL,
    )
    moonshot = resolve_reasoning_trace_capability(
        provider_key="moonshot",
        protocol=OPENAI_COMPAT_PROTOCOL,
    )
    nvidia = resolve_reasoning_trace_capability(
        provider_key="nvidia",
        protocol=OPENAI_COMPAT_PROTOCOL,
    )
    unknown = resolve_reasoning_trace_capability(
        provider_key="custom",
        protocol=OPENAI_COMPAT_PROTOCOL,
    )

    assert openai.has_safe_summary is True
    assert anthropic.has_safe_summary is True
    assert gemini.has_safe_summary is True
    assert interactions.adapter == "gemini_interactions_summary"
    assert interactions.has_safe_summary is True
    assert interactions.supports_buffered is True
    assert interactions.supports_streaming is False
    assert deepseek.output_kind == ReasoningOutputKind.PROVIDER_REASONING
    assert deepseek.has_safe_summary is False
    assert deepseek.requestable is False
    assert mistral.has_safe_summary is False
    assert mistral.continuation_state == "sensitive"
    assert moonshot.adapter == "moonshot_reasoning"
    assert moonshot.continuation_state == "sensitive"
    assert nvidia.adapter == "nvidia_reasoning"
    assert nvidia.output_kind == ReasoningOutputKind.PROVIDER_REASONING
    assert nvidia.continuation_state == "sensitive"
    assert nvidia.has_safe_summary is False
    assert unknown.adapter == "openai_compat_passive"
    assert unknown.has_safe_summary is False


def test_nvidia_nim_protocol_capabilities_are_conservative() -> None:
    capabilities = get_provider_protocol_capabilities(
        provider_key="nvidia",
        protocol=OPENAI_COMPAT_PROTOCOL,
    )

    assert capabilities is not None
    assert capabilities.supports_streaming is True
    assert capabilities.supports_tool_calling is True
    assert capabilities.supports_forced_tool_choice is True
    assert capabilities.supports_structured_outputs is False
    assert capabilities.supports_provider_hosted_web_search_adapter is False
    assert capabilities.default_web_search_adapter == "auto"
    assert capabilities.usage_contract.response_usage_confidence.value == "reported"


def test_zai_coding_plan_capabilities_preserve_subscription_semantics() -> None:
    capabilities = get_provider_protocol_capabilities(
        provider_key="zai_coding_plan",
        protocol=OPENAI_COMPAT_PROTOCOL,
    )

    assert capabilities is not None
    assert capabilities.supports_streaming is True
    assert capabilities.supports_tool_calling is True
    assert capabilities.supports_structured_outputs is True
    assert capabilities.supports_provider_hosted_web_search_adapter is False
    assert capabilities.reasoning_trace.adapter == "glm_reasoning"
    assert capabilities.reasoning_trace.has_safe_summary is False
    assert capabilities.cache_strategy == "implicit_provider"
    assert capabilities.usage_contract.billing_mode == BillingMode.SUBSCRIPTION
    assert capabilities.usage_contract.response_usage_confidence.value == "reported"


def test_custom_profile_can_select_a_safe_structured_summary_adapter() -> None:
    capability = resolve_reasoning_trace_capability(
        provider_key="custom",
        protocol=OPENAI_COMPAT_PROTOCOL,
        adapter_override="openrouter_reasoning",
    )

    assert capability.adapter == "openrouter_reasoning"
    assert capability.has_safe_summary is True
    assert capability.requestable is False


def test_auto_trace_capability_respects_explicit_model_reasoning_metadata() -> None:
    native = resolve_reasoning_trace_capability(
        provider_key="openai",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        model_supports_reasoning=False,
        model_capability_source="catalog",
    )
    compatible = resolve_reasoning_trace_capability(
        provider_key="deepseek",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model_supports_reasoning=False,
        model_capability_source="catalog",
    )
    unknown = resolve_reasoning_trace_capability(
        provider_key="custom",
        protocol=OPENAI_COMPAT_PROTOCOL,
        model_supports_reasoning=True,
        model_capability_source="catalog",
    )

    assert native.adapter == "none"
    assert native.model_supports_reasoning is False
    assert native.resolution_source == "model_metadata:catalog"
    assert compatible.adapter == "openai_compat_passive"
    assert compatible.has_safe_summary is False
    assert unknown.adapter == "openai_compat_passive"
    assert unknown.has_safe_summary is False


def test_explicit_trace_adapter_override_wins_over_model_metadata() -> None:
    capability = resolve_reasoning_trace_capability(
        provider_key="custom",
        protocol=OPENAI_COMPAT_PROTOCOL,
        adapter_override="openrouter_reasoning",
        model_supports_reasoning=False,
        model_capability_source="catalog",
    )

    assert capability.adapter == "openrouter_reasoning"
    assert capability.has_safe_summary is True
    assert capability.model_supports_reasoning is False
    assert capability.resolution_source == "profile_override"


def test_gemini_interactions_summary_adapter_is_protocol_scoped() -> None:
    capability = resolve_reasoning_trace_capability(
        provider_key="gemini",
        protocol=GEMINI_INTERACTIONS_PROTOCOL,
        adapter_override="gemini_interactions_summary",
    )

    assert capability.has_safe_summary is True
    assert capability.supports_streaming is False
    assert capability.supports_buffered is True

    with pytest.raises(ConfigError, match="not valid for protocol"):
        resolve_reasoning_trace_capability(
            provider_key="gemini",
            protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
            adapter_override="gemini_interactions_summary",
        )
