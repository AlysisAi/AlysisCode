from __future__ import annotations

import pytest

from alysis_code.llm.usage_normalization import parse_compatible_usage


def test_deepseek_hit_and_miss_tokens_are_normalized() -> None:
    usage = parse_compatible_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "total_tokens": 120,
            "prompt_cache_hit_tokens": 70,
            "prompt_cache_miss_tokens": 30,
        },
        provider_key="deepseek",
    )

    assert usage is not None
    assert usage.cache_read_input_tokens == 70
    assert usage.input_tokens_uncached == 30
    assert usage.cache_creation_input_tokens is None


def test_qwen_explicit_cache_creation_is_not_double_counted_as_uncached() -> None:
    usage = parse_compatible_usage(
        {
            "prompt_tokens": 130,
            "completion_tokens": 5,
            "total_tokens": 135,
            "prompt_tokens_details": {
                "cached_tokens": 80,
                "cache_creation_input_tokens": 40,
            },
        },
        provider_key="qwen",
    )

    assert usage is not None
    assert usage.cache_read_input_tokens == 80
    assert usage.cache_creation_input_tokens == 40
    assert usage.input_tokens_uncached == 10


@pytest.mark.parametrize("details_key", ["prompt_tokens_details", "input_tokens_details"])
def test_cache_write_tokens_are_decoded_from_compatible_detail_objects(
    details_key: str,
) -> None:
    usage = parse_compatible_usage(
        {
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            details_key: {"cached_tokens": 20, "cache_write_tokens": 50},
        },
        provider_key="openai",
        responses_shape=details_key == "input_tokens_details",
    )

    assert usage is not None
    assert usage.cache_read_input_tokens == 20
    assert usage.cache_creation_input_tokens == 50
    assert usage.input_tokens_uncached == 30


def test_openrouter_numeric_cost_is_provider_reported_usd() -> None:
    usage = parse_compatible_usage(
        {
            "prompt_tokens": 194,
            "completion_tokens": 2,
            "total_tokens": 196,
            "cost": 0.95,
            "prompt_tokens_details": {
                "cached_tokens": 0,
                "cache_write_tokens": 100,
            },
        },
        provider_key="openrouter",
    )

    assert usage is not None
    assert usage.provider_cost_usd == 0.95
    assert usage.provider_cost_currency == "USD"
    assert usage.cache_read_input_tokens == 0
    assert usage.cache_creation_input_tokens == 100


def test_generic_numeric_cost_is_ignored_when_the_unit_is_ambiguous() -> None:
    usage = parse_compatible_usage(
        {"prompt_tokens": 1, "completion_tokens": 1, "cost": 12},
        provider_key="custom",
    )

    assert usage is not None
    assert usage.provider_cost_usd is None


def test_perplexity_total_cost_includes_non_token_fees() -> None:
    usage = parse_compatible_usage(
        {
            "prompt_tokens": 8,
            "completion_tokens": 439,
            "total_tokens": 447,
            "cost": {
                "currency": "USD",
                "input_tokens_cost": 0.000024,
                "output_tokens_cost": 0.006585,
                "request_cost": 0.006,
                "total_cost": 0.012609,
            },
        },
        provider_key="perplexity",
    )

    assert usage is not None
    assert usage.provider_cost_usd == pytest.approx(0.012609)


def test_missing_cache_fields_remain_missing_but_reported_zero_is_preserved() -> None:
    missing = parse_compatible_usage(
        {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
    )
    reported_zero = parse_compatible_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 2,
            "total_tokens": 12,
            "prompt_tokens_details": {"cached_tokens": 0},
        }
    )

    assert missing is not None
    assert reported_zero is not None
    assert missing.cache_read_input_tokens is None
    assert missing.input_tokens_uncached is None
    assert reported_zero.cache_read_input_tokens == 0
    assert reported_zero.input_tokens_uncached == 10


def test_cohere_context_tokens_are_not_replaced_by_smaller_billed_units() -> None:
    usage = parse_compatible_usage(
        {
            "tokens": {"input_tokens": 71, "output_tokens": 418},
            "billed_units": {"input_tokens": 5, "output_tokens": 418},
        },
        provider_key="cohere",
    )

    assert usage is not None
    assert usage.prompt_tokens == 71
    assert usage.completion_tokens == 418
    assert usage.raw_provider_usage is not None
    assert usage.raw_provider_usage["billed_units"]["input_tokens"] == 5


def test_explicitly_reported_uncached_tokens_are_not_marked_derived() -> None:
    usage = parse_compatible_usage(
        {
            "prompt_tokens": 100,
            "completion_tokens": 20,
            "prompt_cache_hit_tokens": 70,
            "prompt_cache_miss_tokens": 30,
        },
        provider_key="deepseek",
    )

    assert usage is not None
    assert usage.input_tokens_uncached == 30
    assert usage.input_tokens_uncached_derived is False


def test_uncached_tokens_computed_from_cache_counts_are_marked_derived() -> None:
    usage = parse_compatible_usage(
        {
            "prompt_tokens": 130,
            "completion_tokens": 5,
            "prompt_tokens_details": {"cached_tokens": 80, "cache_creation_input_tokens": 40},
        },
        provider_key="qwen",
    )

    assert usage is not None
    # Still worth showing — it is arithmetic over reported counts — but the
    # provider never sent an uncached figure, so it is not reported.
    assert usage.input_tokens_uncached == 10
    assert usage.input_tokens_uncached_derived is True


@pytest.mark.parametrize("invalid", [True, False, -1, 1.5, float("nan"), float("inf"), [], {}])
def test_malformed_provider_counts_are_missing_not_truncated(invalid):
    from alysis_code.llm.anthropic_messages import _parse_usage as anthropic_usage
    from alysis_code.llm.gemini_generate_content import _parse_usage as gemini_usage
    from alysis_code.llm.types import parse_token_count

    assert parse_token_count(invalid) is None
    compatible = parse_compatible_usage({"prompt_tokens": invalid, "completion_tokens": 7})
    assert compatible.prompt_tokens is None
    assert compatible.completion_tokens == 7
    anthropic = anthropic_usage(
        {"input_tokens": invalid, "cache_read_input_tokens": 10, "output_tokens": 7}
    )
    assert anthropic.prompt_tokens is None
    assert anthropic.completion_tokens == 7
    gemini = gemini_usage({"promptTokenCount": invalid, "candidatesTokenCount": 7})
    assert gemini.prompt_tokens is None
    assert gemini.completion_tokens == 7


def test_invalid_cache_subtraction_is_not_a_reported_zero():
    usage = parse_compatible_usage(
        {"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 20}}
    )
    assert usage.prompt_tokens == 10
    assert usage.cached_prompt_tokens == 20
    assert usage.input_tokens_uncached is None
    assert not usage.input_tokens_uncached_derived
