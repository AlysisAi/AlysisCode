"""Tests for the static reasoning contract table (capability report, Part A)."""

from __future__ import annotations

from alysis_code.profile_presets import PROFILE_PRESETS
from alysis_code.reasoning_contracts import (
    _CONTRACTS,
    ALWAYS_ON,
    NONE,
    OFF_BETWEEN_TOOLS,
    OFF_EXPLICIT,
    OFF_IMPOSSIBLE,
    OFF_OMIT,
    OFF_SWAPS_MODEL,
    OFF_UNKNOWN,
    OPTIONAL,
    UNKNOWN,
    UNKNOWN_CONTRACT,
    WIRE_NONE,
    reasoning_contract_for,
    reasoning_labels_allowed_by_contract,
    reasoning_off_hazard,
    reasoning_off_is_safe,
)

_VALID_MODES = {ALWAYS_ON, OPTIONAL, NONE, UNKNOWN}
_VALID_OFF = {
    OFF_OMIT,
    OFF_EXPLICIT,
    OFF_BETWEEN_TOOLS,
    OFF_IMPOSSIBLE,
    OFF_SWAPS_MODEL,
    OFF_UNKNOWN,
}


def test_table_integrity() -> None:
    for provider, rules in _CONTRACTS.items():
        assert rules, provider
        for prefix, contract in rules:
            assert contract.mode in _VALID_MODES, (provider, prefix)
            assert contract.off in _VALID_OFF, (provider, prefix)
            # always-on models must never advertise a safe off path.
            if contract.mode == ALWAYS_ON:
                assert contract.off in {OFF_IMPOSSIBLE, OFF_SWAPS_MODEL}, (provider, prefix)
            # models without a knob carry no allowed values.
            if contract.mode == NONE:
                assert contract.values == (), (provider, prefix)
            # a declared default must be an allowed value when values are known.
            if contract.default and contract.values:
                assert contract.default in contract.values, (provider, prefix)


def test_unknown_model_and_provider_fall_back_to_unknown() -> None:
    assert reasoning_contract_for("nope", "whatever") is UNKNOWN_CONTRACT
    assert reasoning_contract_for("openai", "some-future-model") is UNKNOWN_CONTRACT
    assert reasoning_contract_for(None, None) is UNKNOWN_CONTRACT
    assert not reasoning_off_is_safe("nope", "whatever")
    assert "unknown" in reasoning_off_hazard("nope", "whatever")


def test_kimi_code_off_swaps_model() -> None:
    contract = reasoning_contract_for("kimi-code", "k3")
    assert contract.mode == ALWAYS_ON
    assert contract.off == OFF_SWAPS_MODEL
    assert contract.default == "high"  # coding-surface default, not the platform 'max'
    assert not reasoning_off_is_safe("kimi-code", "k3")
    assert "substitutes" in reasoning_off_hazard("kimi-code", "k3")


def test_moonshot_platform_contracts() -> None:
    k27 = reasoning_contract_for("moonshot", "kimi-k2.7-code")
    assert k27.mode == ALWAYS_ON and k27.off == OFF_IMPOSSIBLE
    highspeed = reasoning_contract_for("moonshot", "kimi-k2.7-code-highspeed")
    assert highspeed is k27  # prefix rule covers the variant
    k3 = reasoning_contract_for("moonshot", "kimi-k3")
    assert k3.values == ("low", "high", "max") and k3.default == "max"
    assert reasoning_off_is_safe("moonshot", "kimi-k2.6")


def test_openai_codex_cannot_disable() -> None:
    codex = reasoning_contract_for("openai", "gpt-5.3-codex")
    assert codex.mode == ALWAYS_ON
    assert not codex.allows_value("none")
    # GPT-6 Astra documents low..max only — no "none" — so it is always-on too.
    astra = reasoning_contract_for("openai", "gpt-6-astra")
    assert astra.mode == ALWAYS_ON and astra.off == OFF_IMPOSSIBLE
    assert astra.values == ("low", "medium", "high", "xhigh", "max")
    assert not astra.allows_value("none")
    assert not reasoning_off_is_safe("openai", "gpt-6-astra")
    for model in ("gpt-6-sol", "gpt-6-luna"):
        contract = reasoning_contract_for("openai", model)
        assert contract.mode == OPTIONAL
        assert contract.off == OFF_EXPLICIT
        assert contract.default == "medium"
        assert contract.values == ("none", "low", "medium", "high", "xhigh", "max")
        assert reasoning_off_is_safe("openai", model)
    terra = reasoning_contract_for("openai", "gpt-5.6-terra")
    assert terra.allows_value("none") and terra.default == "medium"
    assert not terra.allows_value("minimal")  # dead on the 5.x families


def test_anthropic_adaptive_vs_haiku() -> None:
    fable = reasoning_contract_for("anthropic", "claude-fable-5")
    assert fable.mode == ALWAYS_ON and fable.off == OFF_IMPOSSIBLE
    haiku = reasoning_contract_for("anthropic", "claude-haiku-4-5")
    assert haiku.wire == "budget_tokens"
    sonnet = reasoning_contract_for("anthropic", "claude-sonnet-5")
    assert sonnet.off == OFF_OMIT and sonnet.toggleable


def test_anthropic_fable_5_1_forbids_forced_tool_choice() -> None:
    fable51 = reasoning_contract_for("anthropic", "claude-fable-5-1")
    fable5 = reasoning_contract_for("anthropic", "claude-fable-5")

    assert fable51 is not fable5  # exact id sorts ahead of the family prefix
    assert fable51.mode == ALWAYS_ON and fable51.off == OFF_IMPOSSIBLE
    assert fable51.default == "high"
    assert fable51.accepts_tool_choice_while_reasoning is False
    assert fable5.accepts_tool_choice_while_reasoning is True


def test_sonnet_55_reduced_thinking_is_not_literal_off() -> None:
    for provider in ("anthropic", "alysis"):
        contract = reasoning_contract_for(provider, "claude-sonnet-5-5")
        assert contract.off == OFF_BETWEEN_TOOLS
        assert not contract.toggleable
        assert not contract.accepts_tool_choice_while_reasoning
        assert not reasoning_off_is_safe(provider, "claude-sonnet-5-5")
        assert "up-front" in reasoning_off_hazard(provider, "claude-sonnet-5-5")


def test_gemini_3_8_flash_matches_3_7_contract() -> None:
    flash38 = reasoning_contract_for("gemini", "gemini-3.8-flash")

    assert flash38.mode == ALWAYS_ON and flash38.off == OFF_IMPOSSIBLE
    assert flash38.values == ("low", "medium", "high")
    assert not flash38.allows_value("minimal")


def test_glm_5_3_cannot_disable_thinking_on_either_zhipu_surface() -> None:
    for provider in ("zhipu", "zai_coding_plan"):
        for model in ("glm-5.3", "glm-5.3-flash"):
            contract = reasoning_contract_for(provider, model)
            assert contract.mode == ALWAYS_ON, (provider, model)
            assert contract.off == OFF_IMPOSSIBLE, (provider, model)
            assert contract.values == ("low", "high", "max"), (provider, model)
            assert contract.emits_flat_reasoning_effort, (provider, model)
    # The general API still lets glm-5.2 toggle thinking off.
    assert reasoning_off_is_safe("zhipu", "glm-5.2")
    # Every plan id routes to a 5.3 model server-side, so none may claim "off".
    assert not reasoning_off_is_safe("zai_coding_plan", "glm-4.7")


def test_kimi_code_k3_256k_shares_the_k3_contract() -> None:
    k3 = reasoning_contract_for("moonshot", "k3", preset_key="kimi-code")
    k3_256k = reasoning_contract_for("moonshot", "k3-256k", preset_key="kimi-code")

    assert k3_256k is k3
    assert k3.default == "high" and k3.off == OFF_SWAPS_MODEL


def test_perplexity_agent_api_routes_send_nothing_until_probed() -> None:
    sonar = reasoning_contract_for("perplexity", "perplexity/sonar")
    assert sonar.mode == NONE and sonar.wire == WIRE_NONE

    glm = reasoning_contract_for("perplexity", "perplexity/glm-5.3")
    assert glm.mode == UNKNOWN
    assert not glm.toggleable
    assert not reasoning_off_is_safe("perplexity", "perplexity/kimi-k3")


def test_groq_qwen_3_8_exposes_the_full_effort_ladder() -> None:
    contract = reasoning_contract_for("groq", "qwen/qwen3.8-27b")

    assert contract.mode == OPTIONAL and contract.off == OFF_EXPLICIT
    assert contract.allows_value("none") and contract.allows_value("high")
    assert contract.emits_flat_reasoning_effort


def test_nvidia_and_together_kimi_k3_never_emit_a_disable() -> None:
    for provider, model in (("nvidia", "moonshotai/kimi-k3"), ("together", "moonshotai/Kimi-K3")):
        contract = reasoning_contract_for(provider, model)
        assert contract.mode == ALWAYS_ON, (provider, model)
        assert contract.off == OFF_IMPOSSIBLE, (provider, model)
        assert contract.values == (), (provider, model)  # no allowlisted efforts


def test_anthropic_opus_5_omitting_the_param_is_not_off() -> None:
    # Opus 5 thinks by default, so 'off' needs an explicit disable value —
    # the OFF_OMIT semantics its 4.x predecessors carry would be a silent lie.
    opus5 = reasoning_contract_for("anthropic", "claude-opus-5")
    opus48 = reasoning_contract_for("anthropic", "claude-opus-4-8")

    assert opus5 is not opus48
    assert opus5.mode == OPTIONAL
    assert opus5.wire == "thinking_adaptive"
    assert opus5.off == OFF_EXPLICIT
    assert opus48.off == OFF_OMIT
    assert opus5.toggleable and reasoning_off_is_safe("anthropic", "claude-opus-5")
    assert opus5.allows_value("xhigh") and opus5.allows_value("max")
    assert opus5.default == "high"
    assert "high" in opus5.notes  # the disable-only-at-effort<=high constraint


def test_gemini_pro_lacks_minimal() -> None:
    pro = reasoning_contract_for("gemini", "gemini-3.1-pro-preview")
    assert not pro.allows_value("minimal")
    flash = reasoning_contract_for("gemini", "gemini-3.5-flash")
    assert flash.allows_value("minimal")
    assert flash.off == OFF_IMPOSSIBLE

    flash37 = reasoning_contract_for("gemini", "gemini-3.7-flash")
    assert flash37.default == "medium"
    assert not flash37.allows_value("minimal")
    assert flash37.values == ("low", "medium", "high")


def test_grok_46_supports_xhigh_but_cannot_disable_reasoning() -> None:
    contract = reasoning_contract_for("xai", "grok-4.6")

    assert contract.mode == ALWAYS_ON
    assert contract.values == ("low", "medium", "high", "xhigh")
    assert contract.default == "high"
    assert contract.off == OFF_IMPOSSIBLE
    assert contract.allows_value("xhigh")
    assert not contract.allows_value("none")


def test_qwen38_and_deepseek_use_their_exact_documented_effort_values() -> None:
    qwen = reasoning_contract_for("qwen", "qwen3.8-max")
    qwen37 = reasoning_contract_for("qwen", "qwen3.7-plus")
    deepseek = reasoning_contract_for("deepseek", "deepseek-v4-pro")
    deepseek_vision = reasoning_contract_for("deepseek", "deepseek-v4-flash-vision-exp")
    deepseek_v41 = reasoning_contract_for("deepseek", "deepseek-flash")

    assert qwen.wire == "reasoning_effort"
    assert qwen.values == ("low", "medium", "xhigh")
    assert qwen.default == "xhigh"
    assert qwen.replay_reasoning_content is True
    assert not qwen.allows_value("high")
    assert qwen37.replay_reasoning_content is False

    assert deepseek.wire == "reasoning_effort"
    assert deepseek.values == ("low", "high", "max")
    assert deepseek.default == "high"
    assert deepseek.replay_reasoning_content is True
    assert deepseek.accepts_tool_choice_while_reasoning is False
    assert deepseek.allows_value("low")
    assert not deepseek.allows_value("medium")
    assert deepseek_vision is deepseek
    assert deepseek_v41 is deepseek


def test_dated_deepseek_gateway_routes_use_surface_specific_contracts() -> None:
    together = reasoning_contract_for("together", "deepseek-ai/DeepSeek-V4-Pro-0813")
    together_flash = reasoning_contract_for(
        "together",
        "deepseek-ai/DeepSeek-V4-Flash-0731",
    )
    fireworks = reasoning_contract_for(
        "fireworks",
        "accounts/fireworks/models/deepseek-v4-flash-0731",
    )

    assert together.mode == OPTIONAL
    assert together.values == ("high", "max")
    assert together.default == "high"
    assert together.off == OFF_EXPLICIT
    assert not together.emits_flat_reasoning_effort
    assert together_flash is UNKNOWN_CONTRACT

    assert fireworks.mode == OPTIONAL
    assert fireworks.values == ("none", "high", "max")
    assert fireworks.default == "high"
    assert fireworks.off == OFF_EXPLICIT
    assert fireworks.emits_flat_reasoning_effort


def test_cerebras_glm_exposes_only_its_documented_disable_value() -> None:
    contract = reasoning_contract_for("cerebras", "zai-glm-4.7")

    assert contract.mode == OPTIONAL
    assert contract.values == ("none",)
    assert contract.off == OFF_EXPLICIT
    assert contract.emits_flat_reasoning_effort is True
    assert contract.allows_value("none")
    assert not contract.allows_value("high")


def test_flat_reasoning_effort_emission_requires_explicit_transport_verification() -> None:
    assert reasoning_contract_for("groq", "openai/gpt-oss-120b").emits_flat_reasoning_effort
    assert reasoning_contract_for(
        "cohere", "command-a-reasoning-08-2025"
    ).emits_flat_reasoning_effort
    assert reasoning_contract_for("xai", "grok-4.6").emits_flat_reasoning_effort
    assert not reasoning_contract_for(
        "fireworks", "accounts/fireworks/models/minimax-m3"
    ).emits_flat_reasoning_effort
    assert reasoning_contract_for(
        "fireworks", "accounts/fireworks/models/deepseek-v4-pro-0813"
    ).emits_flat_reasoning_effort


def test_nvidia_hosted_models_expose_surface_specific_reasoning_contracts() -> None:
    super_contract = reasoning_contract_for(
        "nvidia",
        "nvidia/nemotron-3-super-120b-a12b",
    )
    ultra = reasoning_contract_for("nvidia", "nvidia/nemotron-3-ultra-550b-a55b")
    nano = reasoning_contract_for("nvidia", "nvidia/nemotron-3-nano-30b-a3b")
    deepseek = reasoning_contract_for("nvidia", "deepseek-ai/deepseek-v4-pro")

    assert super_contract.mode == OPTIONAL
    assert super_contract.wire == "reasoning_effort"
    assert super_contract.values == ("low", "high")
    assert ultra.values == ("medium", "high")
    assert nano.wire == "chat_template_enable_thinking"
    assert nano.values == ()
    assert deepseek.values == ("high", "max")
    assert all(contract.off == OFF_EXPLICIT for contract in (super_contract, ultra, nano, deepseek))
    assert reasoning_labels_allowed_by_contract(
        deepseek,
        ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"],
        current="auto",
    ) == ["off", "high", "max", "auto"]


def test_zai_coding_plan_exposes_its_reasoning_floor_without_inventing_off() -> None:
    contract = reasoning_contract_for("zai_coding_plan", "glm-5.3")

    assert contract.mode == ALWAYS_ON
    assert contract.values == ("low", "high", "max")
    assert contract.default == "max"
    assert contract.off == OFF_IMPOSSIBLE
    assert not contract.allows_value("none")
    assert not contract.allows_value("medium")
    assert reasoning_labels_allowed_by_contract(
        contract,
        ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"],
        current="auto",
    ) == ["low", "high", "max", "auto"]


def test_catalog_models_resolve_beyond_unknown_where_researched() -> None:
    # Every suggested model on the core presets should hit a real rule (the
    # probe-gated providers legitimately stay unknown; direct BYOK MiMo shares
    # the same unresearched reasoning surface as the hosted proxy).
    probe_gated = {"bytedance", "fireworks", "openrouter", "alysis", "xiaomi"}
    probe_gated_models = {
        ("together", "deepseek-ai/DeepSeek-V4-Flash-0731"),
    }
    for preset in PROFILE_PRESETS:
        provider = preset.provider_key or preset.key
        if provider in probe_gated or preset.key in {"ollama", "lm-studio", "vllm", "custom"}:
            continue
        for model in preset.suggested_models:
            if (provider, model) in probe_gated_models:
                continue
            contract = reasoning_contract_for(provider, model, preset_key=preset.key)
            assert contract is not UNKNOWN_CONTRACT, (preset.key, model)


def test_preset_key_scopes_the_surface() -> None:
    # Same vendor, different surface, different contract: platform k3 defaults
    # to 'max' and errors on disable; membership k3 defaults to 'high' and
    # silently swaps models on disable.
    platform = reasoning_contract_for("moonshot", "kimi-k3")
    membership = reasoning_contract_for("moonshot", "k3", preset_key="kimi-code")
    assert platform.default == "max" and platform.off == OFF_IMPOSSIBLE
    assert membership.default == "high" and membership.off == OFF_SWAPS_MODEL
