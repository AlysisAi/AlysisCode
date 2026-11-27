"""The provider presets offer each provider's current models with the limits, efforts and
request shape that provider documents, and models that left a list keep working for saved
configs."""

from __future__ import annotations

import json

import httpx
import pytest
from typer.testing import CliRunner

from alysis_code.cli import app
from alysis_code.llm.cache_capabilities import resolve_effective_cache_capability
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.openai_responses import _responses_reasoning
from alysis_code.llm.protocols import get_provider_protocol_capabilities
from alysis_code.llm.temperature_compat import documented_temperature_omit_reason
from alysis_code.llm.types import LLMError
from alysis_code.model_registry import catalog_model_capabilities
from alysis_code.profile_presets import get_preset
from alysis_code.prompt_guidance_catalog import get_model_prompt_catalog
from alysis_code.reasoning_contracts import OFF_IMPOSSIBLE, reasoning_contract_for

ALL_FIVE = ("low", "medium", "high", "xhigh", "max")
GEMINI_4_WITH_XHIGH = ("low", "medium", "high", "xhigh")
GEMINI_4_TO_HIGH = ("low", "medium", "high")
KIMI = ("low", "high", "max")

# preset, model, window, output, efforts, default effort
CURRENT_MODELS = (
    ("openai", "gpt-6.2-astra", 1_050_000, 128_000, ALL_FIVE, "medium"),
    ("openai", "gpt-6.2-sol", 1_050_000, 128_000, ALL_FIVE, "medium"),
    # Sonnet 5.5 can skip up-front thinking ("between tools") at high effort or below.
    ("anthropic", "claude-sonnet-5-5", 1_128_000, 128_000, ("between_tools", *ALL_FIVE), "high"),
    ("anthropic", "claude-fable-5-2", 1_128_000, 128_000, ALL_FIVE, "high"),
    ("gemini", "gemini-4-neon", 1_048_576, 65_536, GEMINI_4_WITH_XHIGH, "high"),
    ("gemini", "gemini-4-hydrogen", 1_048_576, 65_536, GEMINI_4_WITH_XHIGH, "high"),
    ("gemini", "gemini-4-carbon", 1_048_576, 65_536, GEMINI_4_TO_HIGH, "medium"),
    ("gemini", "gemini-4-argon", 1_048_576, 65_536, GEMINI_4_TO_HIGH, "medium"),
    # Qwen4 Max can also answer without thinking (enable_thinking=false).
    ("qwen-intl", "qwen4-max", 1_000_000, 131_072, ("none", "low", "high", "xhigh"), "high"),
    ("qwen-cn", "qwen4-max", 1_000_000, 131_072, ("none", "low", "high", "xhigh"), "high"),
    ("moonshot", "kimi-k3.1-pro", 1_048_576, 131_072, KIMI, "max"),
    ("moonshot", "kimi-k3.1-pro-highspeed", 1_048_576, 131_072, KIMI, "max"),
    ("moonshot-cn", "kimi-k3.1-pro", 1_048_576, 131_072, KIMI, "max"),
    # Kimi Code: the window every eligible membership tier has.
    ("kimi-code", "k3.1-pro", 262_144, 131_072, KIMI, "max"),
)


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("ALYSIS_RUN_REASONING_EFFORT", raising=False)


@pytest.fixture(scope="module")
def preset_rows():
    result = CliRunner().invoke(app, ["profile", "presets", "--json"])
    assert result.exit_code == 0, result.exception
    return {row["key"]: row for row in json.loads(result.stdout)}


def _row(preset_rows, preset_key, model):
    return next(row for row in preset_rows[preset_key]["models"] if row["id"] == model)


@pytest.mark.parametrize("preset_key,model,window,output,efforts,default", CURRENT_MODELS)
def test_the_app_preset_list_carries_each_current_model_with_its_documented_limits(
    preset_rows, preset_key, model, window, output, efforts, default
):
    row = _row(preset_rows, preset_key, model)
    assert row["context_window_tokens"] == window
    assert row["max_output_tokens"] == output
    assert row["supports_vision"] is True
    assert row["supports_reasoning"] is True
    assert tuple(item["id"] for item in row["reasoning_efforts"]) == efforts
    assert row["default_reasoning_effort"] == default
    # Every offered row has a release name, not just its routing id.
    assert row["label"] and row["label"] != model


@pytest.mark.parametrize(
    "preset_key,default_model",
    (
        ("openai", "gpt-6.2-astra"),
        ("openai-responses", "gpt-6.2-astra"),
        ("anthropic", "claude-sonnet-5-5"),
        ("anthropic-compat", "claude-sonnet-5-5"),
        # The Gemini 4 model every key can call, free tier included.
        ("gemini", "gemini-4-hydrogen"),
        ("qwen-intl", "qwen4-max"),
        ("moonshot", "kimi-k3.1-pro"),
        ("kimi-code", "k3.1-pro"),
        ("mistral", "mistral-large-4"),
        ("openrouter", "anthropic/claude-sonnet-5.5"),
    ),
)
def test_new_connections_start_on_the_providers_current_model(
    preset_rows, preset_key, default_model
):
    assert preset_rows[preset_key]["default_model"] == default_model


@pytest.mark.parametrize(
    "preset_key,gone",
    (
        # Closed to accounts created from 2026-11-01, retiring late December.
        ("moonshot", ("kimi-k2.7-code", "kimi-k2.7-code-highspeed", "kimi-k2.6")),
        ("minimax", ("MiniMax-M2.7", "MiniMax-M2.7-highspeed", "MiniMax-M2.5")),
        ("xiaomi-mimo", ("mimo-v2.5-pro", "mimo-v2.5")),
        ("cerebras", ("gemma-4-31b",)),
        ("mistral", ("mistral-medium-3-5", "mistral-large-2512", "zai-glm-5-2")),
        ("perplexity", ("perplexity/kimi-k2.7-code", "perplexity/deepseek-v4-flash-0731")),
        ("together", ("deepseek-ai/DeepSeek-V4-Flash-0731", "MiniMaxAI/MiniMax-M3")),
        ("openrouter", ("deepseek/deepseek-v4-flash-0731",)),
    ),
)
def test_models_providers_stopped_serving_are_no_longer_offered_but_keep_their_guidance(
    preset_key, gone
):
    preset = get_preset(preset_key)
    catalog = get_model_prompt_catalog()
    for model in gone:
        assert model not in preset.suggested_models
        # A saved config that still names the model gets the same prompt guidance.
        assert model in catalog


@pytest.mark.parametrize(
    "preset_key", ("moonshot", "moonshot-cn", "minimax", "perplexity", "openrouter", "together")
)
def test_a_new_key_is_checked_with_a_model_the_preset_still_offers(preset_key):
    preset = get_preset(preset_key)
    assert preset.validation_model in preset.suggested_models


@pytest.mark.parametrize(
    "preset_key,retired,replacement",
    (
        ("mistral", "zai-glm-5-2", "zai-glm-5-3"),
        ("xiaomi-mimo", "mimo-v2.5-pro", "mimo-v2.6-pro"),
        ("together", "deepseek-ai/DeepSeek-V4-Flash-0731", "deepseek-ai/DeepSeek-V4.1-Flash-1017"),
        ("openrouter", "deepseek/deepseek-v4-flash-0731", "deepseek/deepseek-v4.1-flash"),
    ),
)
def test_retired_ids_follow_the_providers_named_replacement(preset_key, retired, replacement):
    assert get_preset(preset_key).model_aliases[retired] == replacement


@pytest.mark.parametrize(
    "model",
    (
        "gpt-6.2-astra",
        "gpt-6.2-sol",
        "openai/gpt-6.2-sol",
        "GPT-6.2-Sol",
        "gpt-6.1-terra",
        "gpt-6-luna",
    ),
)
def test_gpt_6_family_omits_temperature_unless_reasoning_is_off(model):
    assert documented_temperature_omit_reason(model) is not None
    assert documented_temperature_omit_reason(model, reasoning_effort="high") is not None
    assert documented_temperature_omit_reason(model, reasoning_effort="none") is None


@pytest.mark.parametrize(
    "model,omitted",
    (
        ("gemini-4-neon", True),
        ("google/gemini-4-carbon", True),
        ("gemini-3.8-flash", True),
        ("gemini-2.5-flash", False),
        ("gpt-5.6-sol", False),
    ),
)
def test_gemini_4_requests_leave_out_the_removed_sampling_parameters(model, omitted):
    assert (documented_temperature_omit_reason(model) is not None) is omitted


def test_gpt_6_2_tool_turns_use_the_responses_route_on_the_openai_connection(preset_rows):
    for model in ("gpt-6.2-astra", "gpt-6.2-sol", "gpt-6.1-sol", "gpt-6.1-terra"):
        assert _row(preset_rows, "openai", model)["protocol"] == "openai_responses"
    # Astra 6 supports tools with reasoning on Chat Completions and stays there.
    assert _row(preset_rows, "openai", "gpt-6-astra")["protocol"] == "openai_compat"


@pytest.mark.parametrize(
    "model,label", (("gpt-6.2-sol", "GPT-6.2 Sol"), ("gpt-6-astra", "GPT-6 Astra"))
)
def test_always_reasoning_openai_models_refuse_reasoning_off_in_plain_words(model, label):
    for effort, thinking in (("none", None), ("", False), ("minimal", None)):
        with pytest.raises(LLMError, match=f"^{label} requires reasoning effort low, medium"):
            _responses_reasoning(enable_thinking=thinking, reasoning_effort=effort, model=model)
    assert _responses_reasoning(enable_thinking=None, reasoning_effort="xhigh", model=model) == {
        "effort": "xhigh"
    }
    # A model that can turn reasoning off still can.
    assert _responses_reasoning(
        enable_thinking=False, reasoning_effort=None, model="gpt-6.1-terra"
    ) == {"effort": "none"}


@pytest.mark.parametrize("model", ("claude-fable-5-2", "claude-sonnet-5-5"))
def test_new_claude_models_never_get_a_forced_tool_choice_while_thinking(model):
    contract = reasoning_contract_for("anthropic", model)
    assert contract.accepts_tool_choice_while_reasoning is False


def test_fable_5_2_cannot_turn_thinking_off():
    assert reasoning_contract_for("anthropic", "claude-fable-5-2").off == OFF_IMPOSSIBLE


def _gemini_capture(model, **kwargs):
    captured: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content.decode("utf-8")))
        return httpx.Response(
            200,
            json={"candidates": [{"content": {"role": "model", "parts": [{"text": "Γεια σου"}]}}]},
        )

    response = GeminiGenerateContentClient(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        api_key="synthetic-test-only",
        model=model,
        transport=httpx.MockTransport(handle),
        **kwargs,
    ).chat(messages=[{"role": "user", "content": "Πες γεια"}])
    return captured[-1], response


@pytest.mark.parametrize(
    "model,effort,level",
    (
        ("gemini-4-neon", "xhigh", "xhigh"),
        ("gemini-4-hydrogen", "low", "low"),
        ("gemini-4-carbon", "high", "high"),
        ("gemini-4-argon", "medium", "medium"),
        # Carbon and Argon stop at high; a higher effort falls back to their default.
        ("gemini-4-carbon", "xhigh", "medium"),
        ("gemini-3.8-flash", "high", "high"),
    ),
)
def test_gemini_models_with_thinking_levels_get_a_level_never_a_token_budget(model, effort, level):
    payload, response = _gemini_capture(model, reasoning_effort=effort)
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingLevel": level}
    assert "temperature" not in payload["generationConfig"]
    assert response.content == "Γεια σου"


def test_gemini_models_without_thinking_levels_keep_the_token_budget():
    payload, _ = _gemini_capture("gemini-2.5-flash", reasoning_effort="low")
    assert payload["generationConfig"]["thinkingConfig"] == {"thinkingBudget": 1024}


@pytest.mark.parametrize(
    "model,explicit",
    (("gemini-4-neon", False), ("gemini-4-argon", False), ("gemini-3.8-flash", True)),
)
def test_gemini_4_never_creates_explicit_caches(model, explicit):
    preset = get_preset("gemini")
    capability = resolve_effective_cache_capability(
        provider_key=preset.provider_key,
        protocol=preset.protocol,
        model=model,
        base_url=preset.base_url,
        transport_capabilities=get_provider_protocol_capabilities(
            provider_key=preset.provider_key, protocol=preset.protocol
        ),
        preset_cache_capability=preset.cache_capability,
    )
    assert capability.supports_explicit_cached_content is explicit


@pytest.mark.parametrize(
    "provider_key,model,window",
    (
        ("openrouter", "openai/gpt-6.2-sol", 1_050_000),
        ("openrouter", "anthropic/claude-sonnet-5.5", 1_000_000),
        ("openrouter", "google/gemini-4-neon", 1_048_576),
        ("openrouter", "moonshotai/kimi-k3.1-pro", 1_048_576),
        ("openrouter", "qwen/qwen4-max", 1_000_000),
        ("together", "moonshotai/Kimi-K3.1-Pro", 1_048_576),
        ("together", "Qwen/Qwen4-Max", 1_000_000),
        ("together", "deepseek-ai/DeepSeek-V4.1-Flash-1017", 1_000_000),
        ("perplexity", "perplexity/glm-5.3-flash", 1_000_000),
        ("perplexity", "perplexity/deepseek-v4-pro-0813", 1_000_000),
        ("mistral", "zai-glm-5-3-flash", 1_000_000),
    ),
)
def test_gateway_routes_inherit_the_models_documented_window(provider_key, model, window):
    capabilities = catalog_model_capabilities(model, provider_key)
    assert capabilities["context_window_tokens"] == window
    assert capabilities["max_output_tokens"]
