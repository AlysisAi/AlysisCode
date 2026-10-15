from __future__ import annotations

import builtins

import pytest

import alysis_code.litellm_static_provider as provider_mod
import alysis_code.model_registry as model_registry_mod
from alysis_code.config import AppConfig
from alysis_code.litellm_static_provider import (
    BUNDLED_MODEL_CATALOG_SOURCE,
    LiteLLMStaticMetadata,
    get_bundled_model_catalog_provenance,
    resolve_litellm_static_metadata,
)
from alysis_code.model_registry import (
    CANONICAL_MODEL_CATALOG_SOURCE,
    OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE,
    ModelRegistry,
)
from alysis_code.token_budget import compute_input_budget
from alysis_code.usage_tracker import compute_context_left


def _bundled_meta(
    *,
    context_window_tokens: int | None,
    max_output_tokens: int | None,
    supports_vision: bool | None = None,
    input_cost_per_token: float | None = None,
    output_cost_per_token: float | None = None,
    error: str | None = None,
) -> LiteLLMStaticMetadata:
    return LiteLLMStaticMetadata(
        model_key="gpt-5-nano",
        context_window_tokens=context_window_tokens,
        max_output_tokens=max_output_tokens,
        supports_vision=supports_vision,
        input_cost_per_token=input_cost_per_token,
        output_cost_per_token=output_cost_per_token,
        raw_metadata={},
        error=error,
    )


def _raise(exc: Exception) -> None:
    raise exc


def test_litellm_static_provider_handles_missing_bundled_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog",
        lambda: _raise(FileNotFoundError("missing")),
    )
    result = resolve_litellm_static_metadata("gpt-5-nano")
    assert result.error == "bundled model catalog missing"
    assert result.context_window_tokens is None
    assert result.max_output_tokens is None


def test_provider_scoped_catalog_lookup_never_borrows_another_host_route() -> None:
    cases = [
        ("MiniMax-M2.7", "https://api.minimax.io/v1", "minimax", None),
        ("yi-large", "https://api.lingyiwanwu.com/v1", "01ai", None),
        ("llama3.3-70b", "https://api.cerebras.ai/v1", "cerebras", None),
        (
            "sonar-pro",
            "https://api.perplexity.ai",
            "perplexity",
            "perplexity/sonar-pro",
        ),
        ("zai-org/GLM-5.1", "https://api.together.ai/v1", "together", None),
        (
            "accounts/fireworks/models/deepseek-v4-pro",
            "https://api.fireworks.ai/inference/v1",
            "fireworks",
            "fireworks_ai/deepseek-v4-pro",
        ),
        (
            "gpt-4o",
            "https://api.groq.com/openai/v1",
            "groq",
            None,
        ),
    ]

    for model, base_url, provider_hint, expected_key in cases:
        result = resolve_litellm_static_metadata(
            model,
            base_url=base_url,
            provider_hint=provider_hint,
        )
        assert result.model_key == expected_key
        if expected_key is None:
            assert result.error == "model not found in bundled model catalog"
        else:
            assert result.error is None
            assert result.raw_metadata["catalog_provider_hint"] == provider_hint


def test_litellm_static_provider_handles_invalid_bundled_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog",
        lambda: _raise(ValueError("bad json")),
    )
    result = resolve_litellm_static_metadata("gpt-5-nano")
    assert result.error == "bundled model catalog invalid"
    assert result.context_window_tokens is None
    assert result.max_output_tokens is None


def test_litellm_static_provider_treats_catalog_decode_errors_as_invalid(monkeypatch) -> None:
    provider_mod._load_bundled_model_catalog.cache_clear()

    class _FakeCatalogPath:
        def joinpath(self, _filename: str) -> _FakeCatalogPath:
            return self

        def read_text(self, *, encoding: str) -> str:
            _ = encoding
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(provider_mod.resources, "files", lambda _package: _FakeCatalogPath())
    result = resolve_litellm_static_metadata("gpt-5-nano")
    assert result.error == "bundled model catalog invalid"
    assert result.context_window_tokens is None
    assert result.max_output_tokens is None


def test_bundled_model_catalog_provenance_handles_missing_meta(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog_meta",
        lambda: _raise(FileNotFoundError("missing")),
    )
    provenance = get_bundled_model_catalog_provenance()
    assert provenance.error == "bundled model catalog provenance missing"
    assert provenance.upstream_commit_sha is None


def test_bundled_model_catalog_provenance_handles_invalid_meta(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog_meta",
        lambda: _raise(ValueError("bad meta")),
    )
    provenance = get_bundled_model_catalog_provenance()
    assert provenance.error == "bundled model catalog provenance invalid"
    assert provenance.fetched_at_utc is None


def test_litellm_static_provider_never_imports_litellm(monkeypatch) -> None:
    provider_mod._load_bundled_model_catalog.cache_clear()
    original_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):  # type: ignore[no-untyped-def]
        if name == "litellm":
            raise AssertionError("litellm import attempted")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)
    result = resolve_litellm_static_metadata("gpt-5-nano")
    assert result.error is None
    assert result.model_key is not None


def test_litellm_static_provider_uses_model_variants(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog",
        lambda: {
            "sample_spec": {"max_tokens": "ignore-me"},
            "openai/gpt-4o-2026-01-01": {
                "max_tokens": 256000,
                "max_output_tokens": 8192,
                "input_cost_per_token": 0.000001,
                "output_cost_per_token": 0.000002,
                "cache_read_input_token_cost": 0.0000001,
                "cache_creation_input_token_cost": 0.00000125,
                "cache_creation_input_token_cost_above_1hr": 0.000002,
                "output_cost_per_reasoning_token": 0.000003,
            },
            "ignored_string": "not-a-model",
        },
    )
    result = resolve_litellm_static_metadata("gpt-4o")
    assert result.error is None
    assert result.model_key == "openai/gpt-4o-2026-01-01"
    assert result.context_window_tokens == 256000
    assert result.max_output_tokens == 8192
    assert result.input_cost_per_token == 0.000001
    assert result.output_cost_per_token == 0.000002
    assert result.cache_read_input_cost_per_token == 0.0000001
    assert result.cache_creation_input_cost_per_token == 0.00000125
    assert result.cache_creation_1h_input_cost_per_token == 0.000002
    assert result.reasoning_output_cost_per_token == 0.000003


def test_bundled_snapshot_resolves_1h_cache_write_rate() -> None:
    provider_mod._load_bundled_model_catalog.cache_clear()
    catalog = provider_mod._load_bundled_model_catalog()

    result = resolve_litellm_static_metadata("claude-opus-4-1")
    assert result.error is None
    raw = catalog[result.model_key]
    assert result.cache_creation_1h_input_cost_per_token is not None
    assert (
        result.cache_creation_1h_input_cost_per_token
        == raw["cache_creation_input_token_cost_above_1hr"]
    )
    assert result.cache_creation_input_cost_per_token == raw["cache_creation_input_token_cost"]
    assert (
        result.cache_creation_1h_input_cost_per_token > result.cache_creation_input_cost_per_token
    )


def test_bundled_snapshot_resolves_reasoning_output_rate() -> None:
    provider_mod._load_bundled_model_catalog.cache_clear()
    catalog = provider_mod._load_bundled_model_catalog()

    candidates = [
        (key, entry)
        for key, entry in catalog.items()
        if isinstance(entry, dict)
        and isinstance(entry.get("output_cost_per_reasoning_token"), (int, float))
        and entry.get("output_cost_per_reasoning_token") != entry.get("output_cost_per_token")
    ]
    assert candidates

    # Variant matching may resolve a dated key to its undated sibling, so only
    # keep candidates that round-trip to their own catalog entry.
    for key, entry in candidates:
        result = resolve_litellm_static_metadata(key)
        if result.model_key != key:
            continue
        assert result.error is None
        assert result.reasoning_output_cost_per_token is not None
        assert result.reasoning_output_cost_per_token == entry["output_cost_per_reasoning_token"]
        assert result.reasoning_output_cost_per_token != result.output_cost_per_token
        return
    raise AssertionError("no reasoning-rate catalog entry resolved to itself")


def test_litellm_static_provider_ignores_sample_spec_entry(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog",
        lambda: {
            "sample_spec": {
                "max_tokens": 123456,
                "max_output_tokens": 7890,
            }
        },
    )
    result = resolve_litellm_static_metadata("sample_spec")
    assert result.error == "model not found in bundled model catalog"
    assert result.model_key is None


def test_litellm_static_provider_derives_total_context_from_input_and_output(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog",
        lambda: {
            "dashscope/qwen3.5-plus": {
                "max_tokens": 65536,
                "max_input_tokens": 991808,
                "max_output_tokens": 65536,
                "supports_vision": True,
            }
        },
    )
    result = resolve_litellm_static_metadata(
        "qwen3.5-plus",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
    )
    assert result.error is None
    assert result.model_key == "dashscope/qwen3.5-plus"
    assert result.context_window_tokens == 1057344
    assert result.max_output_tokens == 65536
    assert result.supports_vision is True


def test_litellm_static_provider_accepts_integral_float_capacity_fields(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog",
        lambda: {
            "xai/grok-4-fast-reasoning": {
                "max_tokens": 2000000.0,
                "max_input_tokens": 1800000.0,
                "max_output_tokens": 200000.0,
            }
        },
    )

    result = resolve_litellm_static_metadata("xai/grok-4-fast-reasoning")

    assert result.error is None
    assert result.context_window_tokens == 2000000
    assert result.max_output_tokens == 200000


def test_litellm_static_provider_uses_max_tokens_when_only_output_cap_exists(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog",
        lambda: {
            "openai/gpt-5-nano": {
                "max_tokens": 128000,
                "input_cost_per_token": 0.1,
                "output_cost_per_token": 0.2,
            }
        },
    )
    result = resolve_litellm_static_metadata("gpt-5-nano")
    assert result.error is None
    assert result.context_window_tokens == 128000
    assert result.max_output_tokens is None


def test_litellm_static_provider_prefers_endpoint_matching_alias(monkeypatch) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog",
        lambda: {
            "openrouter/qwen3.5-plus": {
                "max_tokens": 128000,
                "max_output_tokens": 4096,
            },
            "dashscope/qwen3.5-plus": {
                "max_input_tokens": 991808,
                "max_output_tokens": 65536,
            },
        },
    )
    result = resolve_litellm_static_metadata(
        "qwen3.5-plus",
        base_url="https://coding-intl.dashscope.aliyuncs.com/v1",
    )
    assert result.error is None
    assert result.model_key == "dashscope/qwen3.5-plus"
    assert result.context_window_tokens == 1057344
    assert result.max_output_tokens == 65536


def test_litellm_static_provider_prefers_shallower_alias_when_provider_is_ambiguous(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        provider_mod,
        "_load_bundled_model_catalog",
        lambda: {
            "openrouter/z-ai/glm-5": {
                "max_input_tokens": 202752,
                "max_output_tokens": 128000,
            },
            "zai/glm-5": {
                "max_input_tokens": 200000,
                "max_output_tokens": 128000,
            },
        },
    )
    result = resolve_litellm_static_metadata("glm-5")
    assert result.error is None
    assert result.model_key == "zai/glm-5"
    assert result.context_window_tokens == 328000
    assert result.max_output_tokens == 128000


def test_env_overrides_beat_user_and_bundled_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: _bundled_meta(
            context_window_tokens=128000,
            max_output_tokens=4096,
            supports_vision=True,
            input_cost_per_token=0.1,
            output_cost_per_token=0.2,
        ),
    )
    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", "64000")
    monkeypatch.setenv("ALYSIS_MAX_OUTPUT_TOKENS", "4000")
    monkeypatch.setenv("ALYSIS_SUPPORTS_VISION", "1")
    monkeypatch.setenv("ALYSIS_INPUT_COST_PER_TOKEN", "0.01")
    monkeypatch.setenv("ALYSIS_OUTPUT_COST_PER_TOKEN", "0.02")

    cfg = AppConfig(base_url="https://api.openai.com/v1", model="gpt-5-nano")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "gpt-5-nano": {
                    "context_window_tokens": 32000,
                    "max_output_tokens": 3000,
                    "supports_vision": False,
                    "input_cost_per_token": 1.0,
                    "output_cost_per_token": 2.0,
                }
            }
        }
    }
    meta = ModelRegistry(cfg=cfg).get("gpt-5-nano")
    assert meta.context_window_tokens == 64000
    assert meta.max_output_tokens == 4000
    assert meta.supports_vision is True
    assert meta.input_cost_per_token == 0.01
    assert meta.output_cost_per_token == 0.02
    assert meta.field_sources["context_window_tokens"] == "env:ALYSIS_CONTEXT_WINDOW"
    assert meta.field_sources["max_output_tokens"] == "env:ALYSIS_MAX_OUTPUT_TOKENS"


def test_user_overrides_beat_bundled_catalog(monkeypatch) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: _bundled_meta(
            context_window_tokens=128000,
            max_output_tokens=4096,
            supports_vision=True,
            input_cost_per_token=0.000001,
            output_cost_per_token=0.000002,
        ),
    )
    cfg = AppConfig(base_url="https://api.openai.com/v1", model="gpt-5-nano")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "gpt-5-nano": {
                    "context_window_tokens": 99999,
                    "max_output_tokens": 4444,
                    "cache_read_input_cost_per_token": 0.0000001,
                    "cache_creation_input_cost_per_token": 0.0000002,
                    "cache_creation_1h_input_cost_per_token": 0.0000003,
                }
            }
        }
    }
    meta = ModelRegistry(cfg=cfg).get("gpt-5-nano")
    assert meta.context_window_tokens == 99999
    assert meta.max_output_tokens == 4444
    assert meta.input_cost_per_token == 0.000001
    assert meta.output_cost_per_token == 0.000002
    assert meta.cache_read_input_cost_per_token == 0.0000001
    assert meta.cache_creation_input_cost_per_token == 0.0000002
    assert meta.cache_creation_1h_input_cost_per_token == 0.0000003
    assert meta.field_sources["context_window_tokens"] == "user:models['gpt-5-nano']"
    assert meta.field_sources["max_output_tokens"] == "user:models['gpt-5-nano']"
    assert meta.field_sources["cache_read_input_cost_per_token"] == "user:models['gpt-5-nano']"
    assert meta.field_sources["input_cost_per_token"] == BUNDLED_MODEL_CATALOG_SOURCE


def test_reasoning_support_uses_catalog_and_model_override_precedence(monkeypatch) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: LiteLLMStaticMetadata(
            model_key="openai/gpt-test",
            context_window_tokens=128000,
            max_output_tokens=4096,
            supports_vision=False,
            input_cost_per_token=None,
            output_cost_per_token=None,
            raw_metadata={"supports_reasoning": True},
            error=None,
        ),
    )
    cfg = AppConfig(base_url="https://api.openai.com/v1", model="gpt-test")

    catalog_meta = ModelRegistry(cfg=cfg).get("gpt-test")
    assert catalog_meta.supports_reasoning is True
    assert catalog_meta.field_sources["supports_reasoning"] == BUNDLED_MODEL_CATALOG_SOURCE

    cfg.extra_fields = {
        "model_metadata_overrides": {"models": {"gpt-test": {"supports_reasoning": False}}}
    }
    overridden_meta = ModelRegistry(cfg=cfg).get("gpt-test")
    assert overridden_meta.supports_reasoning is False
    assert overridden_meta.field_sources["supports_reasoning"] == "user:models['gpt-test']"


def test_bundled_catalog_beats_fallback_when_available(monkeypatch) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: _bundled_meta(
            context_window_tokens=200000,
            max_output_tokens=8192,
            supports_vision=True,
            input_cost_per_token=0.000001,
            output_cost_per_token=0.000002,
        ),
    )
    cfg = AppConfig(model="gpt-5-nano")
    meta = ModelRegistry(cfg=cfg).get("gpt-5-nano")
    assert meta.context_window_tokens == 200000
    assert meta.max_output_tokens == 8192
    assert meta.supports_vision is True
    assert meta.input_cost_per_token == 0.000001
    assert meta.output_cost_per_token == 0.000002
    assert meta.field_sources["context_window_tokens"] == BUNDLED_MODEL_CATALOG_SOURCE
    assert meta.field_sources["max_output_tokens"] == BUNDLED_MODEL_CATALOG_SOURCE
    assert meta.field_sources["supports_vision"] == BUNDLED_MODEL_CATALOG_SOURCE


def test_official_deepseek_v4_metadata_beats_fallback_when_bundled_catalog_lags(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: _bundled_meta(
            context_window_tokens=None,
            max_output_tokens=None,
            error="model not found in bundled model catalog",
        ),
    )

    cfg = AppConfig(base_url="https://api.deepseek.com", model="deepseek-v4-pro")
    registry = ModelRegistry(cfg=cfg)
    meta = registry.get("deepseek-v4-pro")

    assert meta.model_name == "deepseek-v4-pro"
    assert meta.context_window_tokens == 1_000_000
    assert meta.max_output_tokens == 384_000
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token == 0.00000132
    assert meta.output_cost_per_token == 0.00000396
    assert meta.cache_read_input_cost_per_token == 0.000000044
    assert meta.field_sources["context_window_tokens"] == (
        f"{CANONICAL_MODEL_CATALOG_SOURCE}:deepseek-v4-pro"
    )
    assert meta.field_sources["max_output_tokens"] == (
        f"{CANONICAL_MODEL_CATALOG_SOURCE}:deepseek-v4-pro"
    )
    assert registry.last_error is None
    assert not any("fallback context/max_output" in warning for warning in meta.warnings)


def test_official_qwen38_max_metadata_fills_the_unbundled_catalog_gap() -> None:
    cfg = AppConfig(
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        model="qwen3.8-max",
    )

    meta = ModelRegistry(cfg=cfg).get("qwen3.8-max")

    assert meta.context_window_tokens == 1_000_000
    assert meta.max_output_tokens == 131_072
    assert meta.supports_vision is True
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token is None
    assert meta.output_cost_per_token is None
    assert meta.cache_read_input_cost_per_token is None
    assert meta.field_sources["context_window_tokens"] == (
        f"{CANONICAL_MODEL_CATALOG_SOURCE}:qwen3.8-max"
    )
    assert (
        "https://help.aliyun.com/en/model-studio/qwen3-8-max"
        in (meta.raw_metadata["catalog_sources"])
    )


def test_canonical_model_metadata_is_provider_independent() -> None:
    cfg = AppConfig(base_url="https://models.example/v1", model="deepseek-v4-pro")

    meta = ModelRegistry(cfg=cfg).get("deepseek-v4-pro")

    assert meta.context_window_tokens == 1_000_000
    assert meta.max_output_tokens == 384_000
    assert meta.supports_vision is False
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token is None
    assert meta.output_cost_per_token is None
    assert "input_cost_per_token" not in meta.raw_metadata
    assert "output_cost_per_token" not in meta.raw_metadata
    assert meta.field_sources["max_output_tokens"] == (
        f"{CANONICAL_MODEL_CATALOG_SOURCE}:deepseek-v4-pro"
    )
    assert meta.raw_metadata["canonical_model"] == "deepseek-v4-pro"
    assert not any("fallback context/max_output" in warning for warning in meta.warnings)


def test_noncanonical_catalog_pricing_does_not_cross_unknown_compatible_route() -> None:
    cfg = AppConfig(base_url="https://compatible.example/v1", model="claude-opus-4-1")

    meta = ModelRegistry(cfg=cfg).get("claude-opus-4-1")

    assert meta.context_window_tokens == 232_000
    assert meta.max_output_tokens == 32_000
    assert meta.input_cost_per_token is None
    assert meta.output_cost_per_token is None
    assert "input_cost_per_token" not in meta.raw_metadata
    assert "output_cost_per_token" not in meta.raw_metadata
    assert meta.field_sources["context_window_tokens"] == BUNDLED_MODEL_CATALOG_SOURCE


def test_catalog_route_hint_preserves_pricing_across_provider_aliases() -> None:
    cfg = AppConfig(
        base_url="https://api.fireworks.ai/inference/v1",
        model="accounts/fireworks/models/minimax-m3",
    )

    meta = ModelRegistry(cfg=cfg).get("accounts/fireworks/models/minimax-m3")

    assert meta.provider_key == "fireworks"
    assert meta.raw_metadata["catalog_provider_hint"] == "fireworks"
    assert meta.raw_metadata["litellm_provider"] == "fireworks_ai"
    assert meta.input_cost_per_token is not None
    assert meta.output_cost_per_token is not None
    assert meta.field_sources["input_cost_per_token"] == BUNDLED_MODEL_CATALOG_SOURCE
    assert meta.field_sources["output_cost_per_token"] == BUNDLED_MODEL_CATALOG_SOURCE


def test_explicit_endpoint_pricing_overrides_unknown_compatible_route() -> None:
    endpoint = "https://compatible.example/v1"
    cfg = AppConfig(base_url=endpoint, model="claude-opus-4-1")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "endpoints": {
                endpoint: {
                    "models": {
                        "claude-opus-4-1": {
                            "input_cost_per_token": 0.000_004,
                            "output_cost_per_token": 0.000_012,
                        }
                    }
                }
            }
        }
    }

    meta = ModelRegistry(cfg=cfg).get("claude-opus-4-1")

    assert meta.input_cost_per_token == 0.000_004
    assert meta.output_cost_per_token == 0.000_012
    assert meta.field_sources["input_cost_per_token"] == (
        "user:endpoints['https://compatible.example/v1'].models['claude-opus-4-1']"
    )
    assert meta.field_sources["output_cost_per_token"] == (
        "user:endpoints['https://compatible.example/v1'].models['claude-opus-4-1']"
    )


@pytest.mark.parametrize(
    (
        "base_url",
        "model",
        "context_window",
        "max_output",
        "canonical_model",
        "supports_vision",
        "input_cost",
        "output_cost",
    ),
    [
        *[
            (base_url, model, 1_048_576, 393_216, "deepseek-flash", True, 0.0000003, 0.0000012)
            for base_url in (
                "https://api.deepseek.com",
                "https://hosted.example.invalid/api/llm/v1",
            )
            for model in (
                "deepseek-flash",
                "deepseek-v4-flash",
                "deepseek-v4-flash-vision-exp",
                "deepseek-v4.1-flash-expires-on-0910",
            )
        ],
        (
            "https://openrouter.ai/api/v1",
            "qwen/qwen3.8-max",
            1_000_000,
            131_072,
            "qwen3.8-max",
            True,
            0.000002,
            0.000006,
        ),
        # OpenRouter gateway rates as listed 2026-09-02 (roughly halved since
        # the July snapshot).
        (
            "https://openrouter.ai/api/v1",
            "deepseek/deepseek-v4-pro-0813",
            1_048_576,
            384_000,
            "deepseek-v4-pro",
            False,
            0.00000066,
            0.00000198,
        ),
        (
            "https://openrouter.ai/api/v1",
            "deepseek/deepseek-v4-flash-0731",
            1_310_720,
            384_000,
            "deepseek-v4-flash",
            False,
            0.000000065,
            0.00000018,
        ),
        (
            "https://openrouter.ai/api/v1",
            "deepseek/deepseek-v4-flash-vision-exp",
            1_048_576,
            384_000,
            "deepseek-v4-flash-vision-exp",
            True,
            0.00000022,
            0.00000066,
        ),
        (
            "https://openrouter.ai/api/v1",
            "z-ai/glm-5.3",
            1_310_720,
            131_072,
            "glm-5.3",
            False,
            0.0000014,
            0.0000044,
        ),
        (
            "https://openrouter.ai/api/v1",
            "moonshotai/kimi-k3",
            1_048_576,
            131_072,
            "kimi-k3",
            True,
            0.000003,
            0.000015,
        ),
        (
            "https://api.together.ai/v1",
            "zai-org/GLM-5.3",
            1_000_000,
            131_072,
            "glm-5.3",
            False,
            0.0000014,
            0.0000044,
        ),
        (
            "https://api.fireworks.ai/inference/v1",
            "accounts/fireworks/models/glm-5p3-flash",
            1_000_000,
            131_072,
            "glm-5.3-flash",
            True,
            0.00000015,
            0.0000005,
        ),
        (
            "https://api.perplexity.ai/v1",
            "perplexity/kimi-k3",
            1_048_576,
            131_072,
            "kimi-k3",
            True,
            0.000003,
            0.000015,
        ),
        (
            "https://api.together.ai/v1",
            "deepseek-ai/DeepSeek-V4-Pro-0813",
            1_048_576,
            384_000,
            "deepseek-v4-pro",
            False,
            0.00000132,
            0.00000396,
        ),
        (
            "https://api.together.ai/v1",
            "deepseek-ai/DeepSeek-V4-Flash-0731",
            1_000_000,
            384_000,
            "deepseek-v4-flash",
            False,
            0.00000014,
            0.00000028,
        ),
        (
            "https://api.fireworks.ai/inference/v1",
            "accounts/fireworks/models/deepseek-v4-pro-0813",
            1_048_576,
            384_000,
            "deepseek-v4-pro",
            False,
            0.00000132,
            0.00000396,
        ),
        (
            "https://api.fireworks.ai/inference/v1",
            "accounts/fireworks/models/deepseek-v4-flash-0731",
            1_048_576,
            384_000,
            "deepseek-v4-flash",
            False,
            0.00000022,
            0.00000066,
        ),
    ],
)
def test_official_catalog_covers_current_qwen_and_deepseek_routes(
    base_url: str,
    model: str,
    context_window: int,
    max_output: int,
    canonical_model: str,
    supports_vision: bool,
    input_cost: float,
    output_cost: float,
    configured_hosted_api: str,
) -> None:
    meta = ModelRegistry(cfg=AppConfig(base_url=base_url, model=model)).get(model)

    assert meta.context_window_tokens == context_window
    assert meta.max_output_tokens == max_output
    assert meta.supports_vision is supports_vision
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token == input_cost
    assert meta.output_cost_per_token == output_cost
    assert meta.reasoning_output_cost_per_token == output_cost
    assert meta.field_sources["context_window_tokens"] != "fallback"
    assert meta.field_sources["max_output_tokens"] == (
        f"{CANONICAL_MODEL_CATALOG_SOURCE}:{canonical_model}"
    )
    assert meta.raw_metadata["canonical_model"] == canonical_model
    assert meta.raw_metadata["catalog_sources"]
    assert not any("fallback context/max_output" in warning for warning in meta.warnings)
    if model == "qwen/qwen3.8-max":
        assert meta.cache_creation_input_cost_per_token == 0.0000025


@pytest.mark.parametrize(
    ("model", "context_window"),
    [
        ("nvidia/nemotron-3-super-120b-a12b", 1_048_576),
        ("nvidia/nemotron-3-ultra-550b-a55b", 1_048_576),
        ("nvidia/nemotron-3-nano-30b-a3b", 262_144),
    ],
)
def test_official_nvidia_nim_metadata_covers_hosted_nemotron_models(
    model: str,
    context_window: int,
) -> None:
    cfg = AppConfig(base_url="https://integrate.api.nvidia.com/v1", model=model)

    meta = ModelRegistry(cfg=cfg).get(model)

    assert meta.context_window_tokens == context_window
    assert meta.max_output_tokens == 32_768
    assert meta.supports_vision is False
    assert meta.supports_reasoning is True
    # Free Endpoint access is an account/endpoint entitlement, not a durable
    # per-token price contract; unknown is more honest than encoding zero.
    assert meta.input_cost_per_token is None
    assert meta.output_cost_per_token is None
    assert meta.field_sources["context_window_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:nvidia"
    )
    assert meta.raw_metadata["catalog_sources"]
    assert model in meta.raw_metadata["catalog_sources"][0]


@pytest.mark.parametrize(
    "model",
    ["deepseek-ai/deepseek-v4-pro", "deepseek-ai/deepseek-v4-flash"],
)
def test_official_nvidia_catalog_covers_hosted_deepseek_models(model: str) -> None:
    cfg = AppConfig(base_url="https://integrate.api.nvidia.com/v1", model=model)

    meta = ModelRegistry(cfg=cfg).get(model)

    assert meta.context_window_tokens == 1_048_576
    assert meta.max_output_tokens == 16_384
    assert meta.supports_reasoning is True
    assert meta.field_sources["context_window_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:nvidia"
    )
    assert meta.field_sources["max_output_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:nvidia"
    )
    assert meta.raw_metadata["canonical_model"] == model.rsplit("/", 1)[-1]
    assert meta.raw_metadata["catalog_sources"] == [f"https://build.nvidia.com/{model}"]


def test_official_zai_coding_plan_metadata_covers_glm_53() -> None:
    cfg = AppConfig(
        base_url="https://api.z.ai/api/coding/paas/v4",
        model="glm-5.3",
    )

    meta = ModelRegistry(cfg=cfg).get("glm-5.3")

    assert meta.context_window_tokens == 1_000_000
    assert meta.max_output_tokens == 131_072
    assert meta.supports_vision is False
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token is None
    assert meta.output_cost_per_token is None
    assert meta.field_sources["context_window_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:zai_coding_plan"
    )
    assert meta.raw_metadata["catalog_sources"] == [
        "https://docs.z.ai/guides/llm/glm-5.3",
        "https://docs.z.ai/devpack/overview",
    ]

    flash = ModelRegistry(cfg=cfg).get("glm-5.3-flash")
    assert flash.context_window_tokens == 1_000_000
    assert flash.max_output_tokens == 131_072
    assert flash.supports_vision is True
    assert flash.input_cost_per_token is None  # plan credits are not token prices


def test_official_moonshot_k3_metadata_preserves_large_input_budget() -> None:
    cfg = AppConfig(base_url="https://api.moonshot.ai/v1", model="kimi-k3")

    meta = ModelRegistry(cfg=cfg).get("kimi-k3")

    assert meta.model_name == "kimi-k3"
    assert meta.context_window_tokens == 1_048_576
    assert meta.max_output_tokens == 131_072
    assert meta.supports_vision is True
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token == 0.000003
    assert meta.output_cost_per_token == 0.000015
    assert meta.cache_read_input_cost_per_token == 0.0000003
    assert compute_input_budget(meta) == 916_992


def test_official_moonshot_k26_metadata_overrides_stale_bundled_capacity() -> None:
    cfg = AppConfig(base_url="https://api.moonshot.cn/v1", model="kimi-k2.6")

    meta = ModelRegistry(cfg=cfg).get("kimi-k2.6")

    assert meta.context_window_tokens == 262_144
    assert meta.max_output_tokens == 32_768
    assert meta.field_sources["context_window_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:moonshot"
    )
    assert meta.field_sources["max_output_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:moonshot"
    )


def test_official_anthropic_metadata_covers_opus_5_absent_from_the_snapshot() -> None:
    # Opus 5 postdates the pinned litellm mirror. Without the official-provider
    # layer it would silently fall back to 128K/8K on a 1M/128K model.
    assert (
        resolve_litellm_static_metadata(
            "claude-opus-5",
            base_url="https://api.anthropic.com/v1",
            provider_hint="anthropic",
        ).context_window_tokens
        is None
    )

    cfg = AppConfig(base_url="https://api.anthropic.com/v1", model="claude-opus-5")
    meta = ModelRegistry(cfg=cfg).get("claude-opus-5")

    assert meta.context_window_tokens == 1_128_000
    assert meta.max_output_tokens == 128_000
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token == 0.000005
    assert meta.output_cost_per_token == 0.000025
    assert meta.field_sources["context_window_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:anthropic"
    )
    assert meta.warnings == ()

    # Same capacity as the sibling the bundled snapshot does know about.
    sibling = ModelRegistry(
        cfg=AppConfig(base_url="https://api.anthropic.com/v1", model="claude-opus-4-8")
    ).get("claude-opus-4-8")
    assert (meta.context_window_tokens, meta.max_output_tokens) == (
        sibling.context_window_tokens,
        sibling.max_output_tokens,
    )


def test_every_suggested_hosted_model_resolves_real_capacity(configured_hosted_api: str) -> None:
    """No model a preset recommends may fall back to the unknown 128K/8K shape.

    A fallback here means the TUI would show the model with a shrunken context
    budget and a metadata warning on every turn (qwen3.7-plus, Cohere's
    default and OpenRouter's default all did this before 2026-09-02).
    """
    from alysis_code.profile_presets import PROFILE_PRESETS, make_profile_from_preset

    local_or_custom = {"ollama", "lm-studio", "vllm", "custom"}
    fallbacks: list[tuple[str, str]] = []
    for preset in PROFILE_PRESETS:
        if preset.key in local_or_custom:
            continue
        for model in preset.suggested_models:
            cfg = AppConfig(base_url=make_profile_from_preset(preset).base_url, model=model)
            meta = ModelRegistry(cfg=cfg).get(model, include_provider_auth=False)
            if any("fallback context/max_output" in warning for warning in meta.warnings):
                fallbacks.append((preset.key, model))
    assert fallbacks == []


def test_official_anthropic_metadata_covers_fable_5_1_with_cheaper_cache_reads() -> None:
    cfg = AppConfig(base_url="https://api.anthropic.com/v1", model="claude-fable-5-1")
    meta = ModelRegistry(cfg=cfg).get("claude-fable-5-1")

    assert meta.context_window_tokens == 1_128_000
    assert meta.max_output_tokens == 128_000
    assert meta.supports_vision is True
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token == 0.00001
    assert meta.output_cost_per_token == 0.00005
    # 0.025x cache-read multiplier, not the usual 0.1x.
    assert meta.cache_read_input_cost_per_token == 0.00000025
    assert meta.cache_creation_5m_input_cost_per_token == 0.0000125
    assert meta.cache_creation_1h_input_cost_per_token == 0.00002
    assert meta.field_sources["context_window_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:anthropic"
    )
    assert meta.warnings == ()


@pytest.mark.parametrize(
    ("model", "context_window", "input_cost", "output_cost", "cache_write_cost"),
    [
        ("gpt-6-astra", 1_050_000, 0.00001, 0.00005, 0.0000125),
        ("gpt-5.6-sol", 1_050_000, 0.000004, 0.00002, 0.000005),
        ("gpt-5.6-terra", 1_050_000, 0.000002, 0.000012, 0.0000025),
        ("gpt-5.6-luna", 1_050_000, 0.0000002, 0.0000012, 0.00000025),
        ("gpt-5.4-mini", 400_000, 0.00000075, 0.0000045, None),
        ("gpt-5.3-codex", 400_000, 0.00000175, 0.000014, None),
    ],
)
def test_official_openai_metadata_overrides_pre_cut_snapshot_prices(
    model: str,
    context_window: int,
    input_cost: float,
    output_cost: float,
    cache_write_cost: float | None,
) -> None:
    # The pinned LiteLLM snapshot carries GPT-5.6 launch prices and a 1M
    # window for the 400K 5.4-mini / 5.3-codex ids; the official layer wins.
    cfg = AppConfig(base_url="https://api.openai.com/v1", model=model)
    meta = ModelRegistry(cfg=cfg).get(model)

    assert meta.context_window_tokens == context_window
    assert meta.max_output_tokens == 128_000
    assert meta.input_cost_per_token == input_cost
    assert meta.output_cost_per_token == output_cost
    assert meta.cache_creation_input_cost_per_token == cache_write_cost
    assert meta.field_sources["input_cost_per_token"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:openai"
    )


@pytest.mark.parametrize(
    ("model", "vision", "input_cost", "output_cost", "cache_read_cost"),
    [
        ("mimo-v2.6-pro", True, 0.000000435, 0.00000087, 0.0000000036),
        ("mimo-v2.6-flash", True, 0.00000014, 0.00000028, 0.0000000028),
        ("mimo-v2.6-pro-ultraspeed", True, 0.00000435, 0.0000087, 0.000000036),
        ("mimo-v2.5-pro", False, 0.000000435, 0.00000087, 0.0000000036),
        ("mimo-v2.5", True, 0.00000014, 0.00000028, 0.0000000028),
    ],
)
def test_built_in_mimo_metadata_uses_current_list_prices(
    model: str,
    vision: bool,
    input_cost: float,
    output_cost: float,
    cache_read_cost: float,
) -> None:
    cfg = AppConfig(base_url="https://api.xiaomimimo.com/v1", model=model)
    meta = ModelRegistry(cfg=cfg).get(model)

    assert meta.context_window_tokens == 1_000_000
    assert meta.max_output_tokens == 131_072
    assert meta.supports_vision is vision
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token == input_cost
    assert meta.output_cost_per_token == output_cost
    assert meta.cache_read_input_cost_per_token == cache_read_cost
    assert not any("fallback context/max_output" in warning for warning in meta.warnings)


def test_official_perplexity_agent_api_metadata_covers_hosted_routes() -> None:
    cfg = AppConfig(base_url="https://api.perplexity.ai/v1", model="perplexity/sonar")

    sonar = ModelRegistry(cfg=cfg).get("perplexity/sonar")
    glm = ModelRegistry(cfg=cfg).get("perplexity/glm-5.3")

    assert sonar.input_cost_per_token == 0.00000025
    assert sonar.output_cost_per_token == 0.0000025
    assert sonar.supports_reasoning is False
    assert not any("fallback context/max_output" in warning for warning in sonar.warnings)
    # Hosted GLM-5.3 inherits the canonical model's capacity.
    assert glm.context_window_tokens == 1_000_000
    assert glm.max_output_tokens == 131_072
    assert glm.input_cost_per_token == 0.0000014
    assert glm.raw_metadata["canonical_model"] == "glm-5.3"


@pytest.mark.parametrize(
    ("model", "input_cost", "output_cost", "cache_read_cost"),
    [
        ("gemini-3.8-flash", 0.00000075, 0.00000375, 0.000000075),
        ("gemini-3.7-flash", 0.00000075, 0.00000375, 0.000000075),
        ("gemini-3.6-flash", 0.00000075, 0.00000375, 0.000000075),
        ("gemini-3.5-flash-lite", 0.0000003, 0.0000025, 0.00000003),
    ],
)
def test_official_gemini_metadata_covers_models_newer_than_snapshot(
    model: str,
    input_cost: float,
    output_cost: float,
    cache_read_cost: float,
) -> None:
    cfg = AppConfig(
        base_url="https://generativelanguage.googleapis.com/v1beta",
        model=model,
    )

    meta = ModelRegistry(cfg=cfg).get(model)

    assert meta.context_window_tokens == 1_048_576
    assert meta.max_output_tokens == 65_536
    assert meta.supports_vision is True
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token == input_cost
    assert meta.output_cost_per_token == output_cost
    assert meta.cache_read_input_cost_per_token == cache_read_cost
    assert meta.reasoning_output_cost_per_token == output_cost
    assert "https://ai.google.dev/gemini-api/docs/pricing" in (meta.raw_metadata["catalog_sources"])
    assert meta.field_sources["context_window_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:gemini"
    )
    assert not any("fallback context/max_output" in warning for warning in meta.warnings)


def test_official_xai_metadata_covers_grok_46_newer_than_snapshot() -> None:
    assert (
        resolve_litellm_static_metadata(
            "grok-4.6",
            base_url="https://api.x.ai/v1",
            provider_hint="xai",
        ).context_window_tokens
        is None
    )

    cfg = AppConfig(base_url="https://api.x.ai/v1", model="grok-4.6")
    meta = ModelRegistry(cfg=cfg).get("grok-4.6")

    assert meta.context_window_tokens == 500_000
    assert meta.max_output_tokens == 62_500
    assert meta.supports_vision is True
    assert meta.supports_reasoning is True
    assert meta.input_cost_per_token == 0.000002
    assert meta.output_cost_per_token == 0.000006
    assert meta.cache_read_input_cost_per_token == 0.0000005
    assert meta.reasoning_output_cost_per_token == 0.000006
    assert meta.raw_metadata["catalog_sources"] == [
        "https://docs.x.ai/developers/models/grok-4.6",
        "https://docs.x.ai/developers/pricing",
    ]
    assert meta.field_sources["context_window_tokens"] == (
        f"{OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE}:xai"
    )
    assert any("shared window" in warning for warning in meta.warnings)
    assert not any("fallback context/max_output" in warning for warning in meta.warnings)


def test_official_moonshot_metadata_is_scoped_to_moonshot_routes() -> None:
    cfg = AppConfig(base_url="https://custom.example/v1", model="kimi-k3")

    meta = ModelRegistry(cfg=cfg).get("kimi-k3")

    assert meta.provider_key is None
    assert all(
        not source.startswith(OFFICIAL_PROVIDER_MODEL_CATALOG_SOURCE)
        for source in meta.field_sources.values()
    )


def test_per_field_mixing_sets_source_to_mixed(monkeypatch) -> None:
    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", "64000")
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: _bundled_meta(
            context_window_tokens=128000,
            max_output_tokens=4096,
            supports_vision=True,
            input_cost_per_token=0.000001,
            output_cost_per_token=0.000002,
        ),
    )
    cfg = AppConfig(model="gpt-5-nano")
    meta = ModelRegistry(cfg=cfg).get("gpt-5-nano")
    assert meta.context_window_tokens == 64000
    assert meta.max_output_tokens == 4096
    assert meta.source == "mixed"


def test_model_registry_uses_bundled_total_context_for_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: LiteLLMStaticMetadata(
            model_key="dashscope/qwen3.5-plus",
            context_window_tokens=1057344,
            max_output_tokens=65536,
            supports_vision=False,
            input_cost_per_token=None,
            output_cost_per_token=None,
            raw_metadata={
                "max_input_tokens": 991808,
                "max_output_tokens": 65536,
            },
            error=None,
        ),
    )
    cfg = AppConfig(base_url="https://coding-intl.dashscope.aliyuncs.com/v1", model="qwen3.5-plus")
    meta = ModelRegistry(cfg=cfg).get("qwen3.5-plus")
    assert meta.context_window_tokens == 1057344
    assert meta.max_output_tokens == 65536
    assert compute_input_budget(meta) == 991296


def test_endpoint_scoped_overrides_take_precedence(monkeypatch) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: _bundled_meta(
            context_window_tokens=128000,
            max_output_tokens=4096,
        ),
    )
    cfg = AppConfig(base_url="https://example.com/v1", model="gpt-5-nano")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "default": {"context_window_tokens": 16000},
            "models": {"gpt-5-nano": {"context_window_tokens": 24000}},
            "endpoints": {
                "https://example.com/v1/": {
                    "default": {"context_window_tokens": 32000},
                    "models": {"gpt-5-nano": {"context_window_tokens": 64000}},
                }
            },
        }
    }
    meta = ModelRegistry(cfg=cfg).get("gpt-5-nano")
    assert meta.context_window_tokens == 64000
    assert meta.field_sources["context_window_tokens"] == (
        "user:endpoints['https://example.com/v1/'].models['gpt-5-nano']"
    )


def test_override_alias_matching_supports_provider_and_version_variants(monkeypatch) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: _bundled_meta(
            context_window_tokens=None,
            max_output_tokens=None,
        ),
    )
    cfg = AppConfig(base_url="https://api.openai.com/v1", model="gpt-4o")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "openai/gpt-4o": {
                    "context_window_tokens": 123456,
                    "max_output_tokens": 3456,
                }
            }
        }
    }
    plain = ModelRegistry(cfg=cfg).get("gpt-4o")
    dated = ModelRegistry(cfg=cfg).get("gpt-4o-2026-01-01")
    assert plain.context_window_tokens == 123456
    assert dated.context_window_tokens == 123456
    assert plain.max_output_tokens == 3456
    assert dated.max_output_tokens == 3456
    assert plain.field_sources["context_window_tokens"] == "user:models['openai/gpt-4o']"
    assert dated.field_sources["context_window_tokens"] == "user:models['openai/gpt-4o']"


def test_registry_records_bundled_catalog_error_and_fallback_warning(monkeypatch) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: _bundled_meta(
            context_window_tokens=None,
            max_output_tokens=None,
            error="bundled model catalog missing",
        ),
    )
    cfg = AppConfig(model="gpt-5-nano")
    registry = ModelRegistry(cfg=cfg)
    meta = registry.get("gpt-5-nano")
    assert meta.context_window_tokens == 128000
    assert meta.max_output_tokens == 32_000
    assert registry.last_error == "bundled model catalog missing"
    assert any("fallback context/max_output" in warning for warning in meta.warnings)


def test_unknown_model_fallback_window_keeps_startup_context_gauge_healthy(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        model_registry_mod,
        "resolve_litellm_static_metadata",
        lambda _model, *, base_url=None, provider_hint=None: _bundled_meta(
            context_window_tokens=None,
            max_output_tokens=None,
            error="model not found in bundled model catalog",
        ),
    )
    cfg = AppConfig(model="custom-live-model")
    registry = ModelRegistry(cfg=cfg)

    ctx = compute_context_left(
        messages=[{"role": "system", "content": "startup context " * 20_000}],
        model_name="custom-live-model",
        registry=registry,
    )

    assert ctx.context_window_tokens == 128000
    assert ctx.context_window_percent_left is not None
    assert ctx.context_window_percent_left > 60.0
