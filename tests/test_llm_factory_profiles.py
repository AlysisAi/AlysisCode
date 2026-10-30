from __future__ import annotations

from types import SimpleNamespace

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig, set_config_value
from alysis_code.llm.anthropic_messages import AnthropicMessagesClient
from alysis_code.llm.base import ChatClient
from alysis_code.llm.cache_capabilities import (
    CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
    OPENROUTER_SESSION_ID_FIELD,
    XAI_CONVERSATION_ID_HEADER_FIELD,
    CacheCapabilitySpec,
)
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.gemini_generate_content import GeminiGenerateContentClient
from alysis_code.llm.gemini_interactions import (
    GEMINI_INTERACTIONS_CONFIG_FLAG,
    GEMINI_INTERACTIONS_EXPERIMENT_ENV,
    GeminiInteractionsClient,
)
from alysis_code.llm.metadata import credential_scope_fingerprint
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.llm.protocols import (
    ANTHROPIC_MESSAGES_PROTOCOL,
    GEMINI_GENERATE_CONTENT_PROTOCOL,
    GEMINI_INTERACTIONS_PROTOCOL,
    OPENAI_COMPAT_PROTOCOL,
    OPENAI_RESPONSES_PROTOCOL,
    UnsupportedProtocolError,
)
from alysis_code.llm.types import BillingMode
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth import ProviderAuthError
from alysis_code.surface.noop_surface import NoopSurface


def _cfg_with_profile(profile: ProfileSpec, *, active: bool = True) -> AppConfig:
    cfg = AppConfig(model="gpt-test")
    cfg.extra_fields = {"profiles": {}, "active_profile": ""}
    add_profile(cfg, profile)
    if active:
        set_active_profile(cfg, profile.name)
    return cfg


class _WarningSurface(NoopSurface):
    def __init__(self) -> None:
        self.warnings: list[str] = []

    def on_warning(self, warning: str) -> None:
        self.warnings.append(warning)


def test_make_llm_client_uses_active_profile_base_url() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(name="anthropic", base_url="https://api.anthropic.com/v1/openai")
    )
    cfg.base_url = "https://api.openai.com/v1"

    client = make_llm_client(cfg=cfg, api_key="key", model="claude")

    assert client.base_url == "https://api.anthropic.com/v1/openai"


def test_factory_client_route_identity_includes_full_profile_credential_and_routing_scope() -> None:
    profile = ProfileSpec(
        name="work-route",
        protocol=OPENAI_COMPAT_PROTOCOL,
        base_url="https://gateway.example.test/v1/",
        default_model="route-model",
        extra_headers={
            "OpenAI-Project": "project-a",
            "X-Diagnostic": "diagnostic-a",
        },
        reasoning_trace_adapter="deepseek_reasoning",
    )
    cfg = _cfg_with_profile(profile)

    first = make_llm_client(
        cfg=cfg,
        api_key="credential-a",
        model="route-model",
        profile=profile,
    )
    second = make_llm_client(
        cfg=cfg,
        api_key="credential-b",
        model="route-model",
        profile=profile,
    )

    assert first.route_identity.profile_name == "work-route"
    assert first.route_identity.protocol == OPENAI_COMPAT_PROTOCOL
    assert first.route_identity.base_url == "https://gateway.example.test/v1"
    assert first.route_identity.model == "route-model"
    assert first.reasoning_trace_adapter == "deepseek_reasoning"
    assert first.route_identity.reasoning_state_adapter == "deepseek_reasoning"
    assert first.route_identity.credential_scope == credential_scope_fingerprint("credential-a")
    assert dict(first.route_identity.routing_headers) == {
        "openai-project": credential_scope_fingerprint("project-a"),
        "x-diagnostic": credential_scope_fingerprint("diagnostic-a"),
    }
    assert first.route_identity.fingerprint != second.route_identity.fingerprint


def test_make_llm_client_passes_extra_headers_for_anthropic_profile() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            base_url="https://api.anthropic.com/v1/openai",
            extra_headers={"anthropic-version": "2023-06-01"},
        )
    )
    cfg.base_url = "https://api.openai.com/v1"

    client = make_llm_client(cfg=cfg, api_key="key", model="claude")

    assert client.extra_headers["anthropic-version"] == "2023-06-01"


def test_make_llm_client_explicit_profile_arg_wins_over_active() -> None:
    cfg = _cfg_with_profile(ProfileSpec(name="openai", base_url="https://api.openai.com/v1"))
    explicit = ProfileSpec(name="custom", base_url="https://custom.example/v1")

    client = make_llm_client(cfg=cfg, api_key="key", model="model", profile=explicit)

    assert client.base_url == "https://custom.example/v1"


def test_active_profile_base_url_overrides_stale_top_level_base_url() -> None:
    cfg = _cfg_with_profile(ProfileSpec(name="openai", base_url="https://api.openai.com/v1"))
    cfg.base_url = "https://legacy.example/v1"

    client = make_llm_client(cfg=cfg, api_key="key", model="model")

    assert client.base_url == "https://api.openai.com/v1"


def test_make_llm_client_uses_configured_reasoning_effort() -> None:
    cfg = _cfg_with_profile(ProfileSpec(name="openai", base_url="https://api.openai.com/v1"))
    cfg.llm_reasoning_effort = "high"

    client = make_llm_client(cfg=cfg, api_key="key", model="gpt-5")

    assert client.reasoning_effort == "high"
    assert client.usage_counts_authoritative is True
    assert client.usage_contract.response_usage_authoritative is True
    assert client.usage_contract.supports_input_token_count is True
    assert client.usage_contract.input_token_count_strategy == ("openai_compat_provider_payload")


@pytest.mark.parametrize(
    ("protocol", "base_url", "message"),
    [
        (
            OPENAI_COMPAT_PROTOCOL,
            "https://chatgpt.com/backend-api/codex",
            "requires protocol",
        ),
        (
            OPENAI_RESPONSES_PROTOCOL,
            "https://override.example/v1",
            "owns its endpoint",
        ),
    ],
)
def test_make_llm_client_rejects_subscription_profile_transport_overrides(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    base_url: str,
    message: str,
) -> None:
    adapter = SimpleNamespace(
        protocol=OPENAI_RESPONSES_PROTOCOL,
        base_url="https://chatgpt.com/backend-api/codex",
    )
    monkeypatch.setattr(
        "alysis_code.llm.factory.create_provider_auth",
        lambda provider_id, *, transport=None: adapter,
    )
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="chatgpt-codex",
            protocol=protocol,
            base_url=base_url,
            auth_provider="openai-codex",
        )
    )

    with pytest.raises(UnsupportedProtocolError, match=message):
        make_llm_client(cfg=cfg, api_key="", model="gpt-test")


def test_make_llm_client_rejects_unconfirmed_subscription_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        protocol=OPENAI_RESPONSES_PROTOCOL,
        base_url="https://chatgpt.com/backend-api/codex",
    )
    monkeypatch.setattr(
        "alysis_code.llm.factory.create_provider_auth",
        lambda provider_id, *, transport=None: adapter,
    )
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="chatgpt-codex",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url=adapter.base_url,
            auth_provider="openai-codex",
            default_model="gpt-test",
        )
    )

    with pytest.raises(ProviderAuthError, match="/config → Default Model"):
        make_llm_client(cfg=cfg, api_key="", model="gpt-test")


def test_factory_marks_subscription_and_local_billing_modes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = SimpleNamespace(
        protocol=OPENAI_RESPONSES_PROTOCOL,
        base_url="https://chatgpt.com/backend-api/codex",
    )
    monkeypatch.setattr(
        "alysis_code.llm.factory.create_provider_auth",
        lambda provider_id, *, transport=None: adapter,
    )
    subscription_profile = ProfileSpec(
        name="chatgpt-codex",
        protocol=OPENAI_RESPONSES_PROTOCOL,
        base_url=adapter.base_url,
        auth_provider="openai-codex",
        default_model="gpt-test",
        reasoning_effort="medium",
    )
    subscription_cfg = _cfg_with_profile(subscription_profile)

    subscription_client = make_llm_client(
        cfg=subscription_cfg,
        api_key="",
        model="gpt-test",
    )

    # A known local runtime stays LOCAL even when it is given a placeholder key
    # (Ollama and LM Studio accept any string), because the preset key decides.
    preset_local_client = make_llm_client(
        cfg=_cfg_with_profile(
            ProfileSpec(
                name="ollama",
                protocol=OPENAI_COMPAT_PROTOCOL,
                base_url="http://localhost:11434/v1",
                default_model="local-model",
            )
        ),
        api_key="ollama",
        model="local-model",
    )

    # An unrecognised loopback endpoint with no credential is still local.
    unkeyed_local_client = make_llm_client(
        cfg=_cfg_with_profile(
            ProfileSpec(
                name="custom-local",
                protocol=OPENAI_COMPAT_PROTOCOL,
                base_url="http://127.0.0.1:8080/v1",
                default_model="local-model",
            )
        ),
        api_key="",
        model="local-model",
    )

    # But a keyed gateway on loopback (LiteLLM and friends) fronts a paid
    # upstream API — calling that free would under-report real spend.
    proxied_client = make_llm_client(
        cfg=_cfg_with_profile(
            ProfileSpec(
                name="litellm-proxy",
                protocol=OPENAI_COMPAT_PROTOCOL,
                base_url="http://localhost:4000/v1",
                default_model="gpt-4o",
            )
        ),
        api_key="sk-proxy-key",
        model="gpt-4o",
    )

    assert subscription_client.usage_contract.billing_mode == BillingMode.SUBSCRIPTION
    assert preset_local_client.usage_contract.billing_mode == BillingMode.LOCAL
    assert unkeyed_local_client.usage_contract.billing_mode == BillingMode.LOCAL
    assert proxied_client.usage_contract.billing_mode == BillingMode.METERED_API


def test_make_llm_client_routes_openai_compat_to_existing_client() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol="openai_compat",
            base_url="https://api.openai.com/v1",
            extra_headers={"x-test": "yes"},
        )
    )

    client = make_llm_client(cfg=cfg, api_key="key", model="gpt-test")

    assert isinstance(client, ChatClient)
    assert isinstance(client, OpenAICompatClient)
    assert client.base_url == "https://api.openai.com/v1"
    assert client.extra_headers == {"x-test": "yes"}


def test_make_llm_client_resolves_nvidia_nim_provider_contract() -> None:
    model = "nvidia/nemotron-3-super-120b-a12b"
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="nvidia",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url="https://integrate.api.nvidia.com/v1",
            api_key_env="NVIDIA_API_KEY",
            default_model=model,
        )
    )

    client = make_llm_client(cfg=cfg, api_key="nvapi-test", model=model)

    assert isinstance(client, OpenAICompatClient)
    assert client.provider_key == "nvidia"
    assert client.reasoning_trace_capability.adapter == "nvidia_reasoning"
    assert client.reasoning_trace_capability.continuation_state == "sensitive"
    assert client.usage_contract.billing_mode == BillingMode.METERED_API
    assert client.usage_contract.response_usage_confidence.value == "reported"


def test_make_llm_client_resolves_zai_coding_plan_subscription_contract() -> None:
    model = "glm-5.3"
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="zai-coding-plan",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url="https://api.z.ai/api/coding/paas/v4",
            api_key_env="ZAI_API_KEY",
            default_model=model,
        )
    )

    client = make_llm_client(cfg=cfg, api_key="zai-test", model=model)

    assert isinstance(client, OpenAICompatClient)
    assert client.provider_key == "zai_coding_plan"
    assert client.reasoning_trace_capability.adapter == "glm_reasoning"
    assert client.reasoning_trace_capability.has_safe_summary is False
    assert client.usage_contract.billing_mode == BillingMode.SUBSCRIPTION
    assert client.usage_contract.response_usage_confidence.value == "reported"


def test_make_llm_client_passes_prompt_cache_fields_only_to_declared_openai_support() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url="https://api.openai.com/v1",
        )
    )

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="gpt-test",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
    )

    assert isinstance(client, OpenAICompatClient)
    assert client.prompt_cache_key == "repo-main"
    assert client.prompt_cache_retention == "24h"


def test_make_llm_client_derives_openai_prompt_cache_key_in_auto_mode() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url="https://api.openai.com/v1",
        )
    )
    cfg.prompt_cache_mode = "auto"

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="gpt-test",
        prompt_cache_namespace="workspace:repo role:coding",
    )

    assert isinstance(client, OpenAIResponsesClient)
    assert client.prompt_cache_key is not None
    assert client.prompt_cache_key.startswith("alysis:openai:")
    assert client.prompt_cache_retention is None


def test_make_llm_client_auto_mode_does_not_guess_openai_cache_key_without_scope() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url="https://api.openai.com/v1",
        )
    )
    cfg.prompt_cache_mode = "auto"

    client = make_llm_client(cfg=cfg, api_key="key", model="gpt-test")

    assert isinstance(client, OpenAIResponsesClient)
    assert client.prompt_cache_key is None


def test_make_llm_client_drops_prompt_cache_fields_for_unknown_compat_gateway() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="custom",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url="https://gateway.example/v1",
        )
    )

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="gateway-model",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
    )

    assert isinstance(client, OpenAICompatClient)
    assert client.prompt_cache_key is None
    assert client.prompt_cache_retention is None
    assert client.usage_counts_authoritative is False
    assert client.usage_contract.response_usage_authoritative is False


def test_make_llm_client_projects_builtin_mistral_prompt_cache_key() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="mistral",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url="https://api.mistral.ai/v1",
        )
    )

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="mistral-medium-3-5",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
    )

    assert isinstance(client, OpenAICompatClient)
    assert client.prompt_cache_key == "repo-main"
    assert client.prompt_cache_retention is None
    assert client.prompt_cache_request_field_values == {"prompt_cache_key": "repo-main"}
    assert client.prompt_cache_policy_metadata is not None
    assert client.prompt_cache_policy_metadata["status"] == "enabled"
    assert client.prompt_cache_policy_metadata["strategy"] == "mistral_prompt_cache_key"
    assert client.prompt_cache_policy_metadata["capability_source"] == "preset"
    assert client.prompt_cache_policy_metadata["allowed_fields"] == ["prompt_cache_key"]
    assert client.prompt_cache_policy_metadata["emitted_fields"] == ["prompt_cache_key"]
    assert client.prompt_cache_policy_metadata["emits_request_fields"] is True


def test_make_llm_client_auto_projects_openrouter_session_id() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openrouter",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url="https://openrouter.ai/api/v1",
        )
    )
    cfg.prompt_cache_mode = "auto"

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="qwen/qwen3.7-plus",
        prompt_cache_namespace='[["workspace","/repo"]]',
    )

    assert isinstance(client, OpenAICompatClient)
    session_id = client.prompt_cache_request_field_values[OPENROUTER_SESSION_ID_FIELD]
    assert session_id.startswith("alysis:openrouter:")
    assert client.prompt_cache_key is None
    assert client.prompt_cache_policy_metadata is not None
    assert client.prompt_cache_policy_metadata["status"] == "enabled"
    assert client.prompt_cache_policy_metadata["strategy"] == "openrouter_sticky_session"
    assert client.prompt_cache_policy_metadata["allowed_fields"] == [OPENROUTER_SESSION_ID_FIELD]
    assert client.prompt_cache_policy_metadata["emitted_fields"] == [OPENROUTER_SESSION_ID_FIELD]
    assert dict(client.route_identity.routing_fields) == {
        OPENROUTER_SESSION_ID_FIELD: credential_scope_fingerprint(session_id)
    }
    assert session_id not in str(client.route_identity.as_metadata())


def test_make_llm_client_auto_projects_xai_conversation_header() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="xai",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url="https://api.x.ai/v1",
        )
    )
    cfg.prompt_cache_mode = "auto"

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="grok-4.3",
        prompt_cache_namespace='[["workspace","/repo"]]',
    )

    assert isinstance(client, OpenAICompatClient)
    conversation_id = client.prompt_cache_request_field_values[XAI_CONVERSATION_ID_HEADER_FIELD]
    assert conversation_id.startswith("alysis:xai:")
    assert client.prompt_cache_policy_metadata is not None
    assert client.prompt_cache_policy_metadata["status"] == "enabled"
    assert client.prompt_cache_policy_metadata["strategy"] == "xai_conversation_header"
    assert client.prompt_cache_policy_metadata["allowed_fields"] == [
        XAI_CONVERSATION_ID_HEADER_FIELD
    ]
    assert client.prompt_cache_policy_metadata["emitted_fields"] == [
        XAI_CONVERSATION_ID_HEADER_FIELD
    ]
    assert dict(client.route_identity.routing_fields) == {
        XAI_CONVERSATION_ID_HEADER_FIELD: credential_scope_fingerprint(conversation_id)
    }
    assert conversation_id not in str(client.route_identity.as_metadata())


def test_make_llm_client_allows_profile_declared_custom_cache_capability() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="custom",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url="https://gateway.example/v1",
            cache_capability=CacheCapabilitySpec(
                strategy=CACHE_STRATEGY_OPENAI_PROMPT_CACHE,
                enabled=True,
                supports_prompt_cache_key=True,
                reports_cache_read_tokens=True,
            ),
        )
    )

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="gateway-model",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
    )

    assert isinstance(client, OpenAICompatClient)
    assert client.prompt_cache_key == "repo-main"
    assert client.prompt_cache_retention is None
    assert client.prompt_cache_policy_metadata is not None
    assert client.prompt_cache_policy_metadata["status"] == "enabled"
    assert client.prompt_cache_policy_metadata["capability_source"] == "profile"
    assert client.prompt_cache_policy_metadata["allowed_fields"] == ["prompt_cache_key"]
    assert client.prompt_cache_policy_metadata["emitted_fields"] == ["prompt_cache_key"]
    assert client.prompt_cache_policy_metadata["trusted_usage_fields"] == [
        "cache_read_input_tokens"
    ]


def test_make_llm_client_profile_override_can_disable_builtin_cache_capability() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url="https://api.openai.com/v1",
            cache_capability=CacheCapabilitySpec(enabled=False),
        )
    )
    cfg.prompt_cache_mode = "auto"

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="gpt-test",
        prompt_cache_namespace="workspace:repo role:coding",
    )

    assert isinstance(client, OpenAIResponsesClient)
    assert client.prompt_cache_key is None
    assert client.prompt_cache_retention is None


def test_make_llm_client_routes_openai_responses_to_native_client() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-native",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url="https://api.openai.com/v1",
            extra_headers={"x-test": "yes"},
            web_search_adapter="openai_responses",
        )
    )
    cfg.llm_reasoning_effort = "low"
    cfg.llm_stream_no_progress_timeout_s = 12.5
    cfg.subagent_orchestration.inflight_deadline_grace_s = 3.0
    cfg.web_search_mode = "native"

    client = make_llm_client(cfg=cfg, api_key="key", model="gpt-test")

    assert isinstance(client, ChatClient)
    assert isinstance(client, OpenAIResponsesClient)
    assert client.base_url == "https://api.openai.com/v1"
    assert client.extra_headers == {"x-test": "yes"}
    assert client.reasoning_effort == "low"
    assert client.stream_no_progress_timeout_s == 12.5
    assert client.inflight_deadline_grace_s == 3.0
    assert client.usage_contract.response_usage_authoritative is True
    assert client.usage_contract.supports_input_token_count is True
    assert client.web_search_mode == "native"
    assert client.web_search_adapter == "openai_responses"
    assert client.reasoning_trace_capability.adapter == "openai_responses_summary"
    assert client.reasoning_trace_capability.has_safe_summary is True


def test_make_llm_client_gates_auto_trace_with_model_metadata() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-native",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url="https://api.openai.com/v1",
        )
    )
    cfg.extra_fields["model_metadata_overrides"] = {
        "models": {"gpt-test": {"supports_reasoning": False}}
    }

    client = make_llm_client(cfg=cfg, api_key="key", model="gpt-test")

    assert client.reasoning_trace_capability.adapter == "none"
    assert client.reasoning_trace_capability.model_supports_reasoning is False
    assert client.reasoning_trace_capability.resolution_source.startswith("model_metadata:user:")


def test_make_llm_client_routes_anthropic_messages_to_native_client() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
            base_url="https://api.anthropic.com/v1",
            extra_headers={"anthropic-beta": "test-beta"},
            web_search_adapter="anthropic_messages",
        )
    )
    cfg.web_search_mode = "native"

    client = make_llm_client(cfg=cfg, api_key="key", model="claude-sonnet-4-6")

    assert isinstance(client, ChatClient)
    assert isinstance(client, AnthropicMessagesClient)
    assert client.base_url == "https://api.anthropic.com/v1"
    assert client.extra_headers == {"anthropic-beta": "test-beta"}
    assert client.web_search_mode == "native"
    assert client.web_search_adapter == "anthropic_messages"
    assert client.usage_contract.response_usage_authoritative is True
    assert client.usage_contract.supports_input_token_count is True
    assert client.reasoning_trace_capability.adapter == "anthropic_messages_summary"
    assert client.reasoning_trace_capability.has_safe_summary is True


def test_make_llm_client_drops_openai_prompt_cache_fields_for_anthropic_native() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
            base_url="https://api.anthropic.com/v1",
        )
    )

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="claude-sonnet-4-6",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
    )

    assert isinstance(client, AnthropicMessagesClient)
    assert client.prompt_cache_key is None
    assert client.prompt_cache_retention is None


def test_make_llm_client_passes_anthropic_cache_control_settings_to_native_client() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
            base_url="https://api.anthropic.com/v1",
        )
    )
    cfg.anthropic_prompt_cache_enabled = True
    cfg.anthropic_prompt_cache_ttl = "1h"

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="claude-sonnet-4-6",
    )

    assert isinstance(client, AnthropicMessagesClient)
    assert client.prompt_cache_control_enabled is True
    assert client.prompt_cache_control_ttl == "1h"


def test_make_llm_client_auto_mode_enables_anthropic_cache_control() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
            base_url="https://api.anthropic.com/v1",
        )
    )
    cfg.prompt_cache_mode = "auto"
    cfg.anthropic_prompt_cache_ttl = "1h"

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="claude-sonnet-4-6",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_namespace="workspace:repo role:coding",
    )

    assert isinstance(client, AnthropicMessagesClient)
    assert client.prompt_cache_key is None
    assert client.prompt_cache_retention is None
    assert client.prompt_cache_control_enabled is True
    assert client.prompt_cache_control_ttl == "1h"
    assert client.prompt_cache_policy_metadata is not None
    assert client.prompt_cache_policy_metadata["status"] == "enabled"
    assert client.prompt_cache_policy_metadata["allowed_fields"] == ["cache_control"]
    assert client.prompt_cache_policy_metadata["emitted_fields"] == ["cache_control"]
    assert client.prompt_cache_policy_metadata["ttl"] == "1h"


def test_make_llm_client_cache_mode_off_suppresses_all_request_cache_hints() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
            base_url="https://api.anthropic.com/v1",
        )
    )
    cfg.prompt_cache_mode = "off"
    cfg.anthropic_prompt_cache_enabled = True
    cfg.anthropic_prompt_cache_ttl = "1h"

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="claude-sonnet-4-6",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
        prompt_cache_namespace="workspace:repo role:coding",
    )

    assert isinstance(client, AnthropicMessagesClient)
    assert client.prompt_cache_key is None
    assert client.prompt_cache_retention is None
    assert client.prompt_cache_control_enabled is False
    assert client.prompt_cache_control_ttl == "1h"
    assert client.prompt_cache_policy_metadata is not None
    assert client.prompt_cache_policy_metadata["status"] == "disabled"
    assert client.prompt_cache_policy_metadata["allowed_fields"] == ["cache_control"]
    assert client.prompt_cache_policy_metadata["emitted_fields"] == []


def test_make_llm_client_routes_gemini_generate_content_to_native_client() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            extra_headers={"x-test": "yes"},
            web_search_adapter="gemini_grounding",
        )
    )
    cfg.web_search_mode = "native"
    cfg.llm_reasoning_effort = "low"

    client = make_llm_client(cfg=cfg, api_key="key", model="gemini-3-flash-preview")

    assert isinstance(client, ChatClient)
    assert isinstance(client, GeminiGenerateContentClient)
    assert client.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert client.extra_headers == {"x-test": "yes"}
    assert client.reasoning_effort == "low"
    assert client.usage_contract.response_usage_authoritative is True
    assert client.usage_contract.supports_input_token_count is True
    assert client.web_search_mode == "native"
    assert client.web_search_adapter == "gemini_grounding"
    assert client.reasoning_trace_capability.adapter == "gemini_thought_summary"
    assert client.reasoning_trace_capability.has_safe_summary is True


def test_make_llm_client_drops_openai_prompt_cache_fields_for_gemini_native() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
        )
    )

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="gemini-3-flash-preview",
        prompt_cache_key="repo-main",
        prompt_cache_retention="24h",
    )

    assert isinstance(client, GeminiGenerateContentClient)
    assert client.prompt_cache_key is None
    assert client.prompt_cache_retention is None
    assert client.explicit_cached_content_enabled is False


def test_make_llm_client_enables_gemini_explicit_cached_content_in_auto_mode() -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
        )
    )
    cfg.prompt_cache_mode = "auto"

    client = make_llm_client(
        cfg=cfg,
        api_key="key",
        model="gemini-3-flash-preview",
        prompt_cache_retention="15m",
    )

    assert isinstance(client, GeminiGenerateContentClient)
    assert client.prompt_cache_key is None
    assert client.prompt_cache_retention is None
    assert client.explicit_cached_content_enabled is True
    assert client.cached_content_ttl == "900s"
    assert client.cached_content_min_tokens == 4096
    assert client.prompt_cache_policy_metadata is not None
    assert client.prompt_cache_policy_metadata["status"] == "enabled"
    assert client.prompt_cache_policy_metadata["allowed_fields"] == ["cached_content"]
    assert client.prompt_cache_policy_metadata["emitted_fields"] == ["cached_content"]
    assert client.prompt_cache_policy_metadata["ttl"] == "900s"


def test_make_llm_client_rejects_gemini_interactions_without_feature_flag(
    monkeypatch,
) -> None:
    monkeypatch.delenv(GEMINI_INTERACTIONS_EXPERIMENT_ENV, raising=False)
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini-interactions",
            protocol=GEMINI_INTERACTIONS_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            extra_headers={"x-test": "yes"},
        )
    )

    try:
        make_llm_client(cfg=cfg, api_key="key", model="gemini-2.5-flash")
    except Exception as exc:
        error = exc
    else:  # pragma: no cover - assertion failure branch.
        raise AssertionError("expected experimental Gemini Interactions profile to be rejected")

    assert type(error).__name__ == "UnsupportedProtocolError"
    assert "experimental and disabled by default" in str(error)
    assert GEMINI_INTERACTIONS_EXPERIMENT_ENV in str(error)


def test_make_llm_client_routes_gemini_interactions_when_env_flag_enabled(
    monkeypatch,
) -> None:
    monkeypatch.setenv(GEMINI_INTERACTIONS_EXPERIMENT_ENV, "1")
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini-interactions",
            protocol=GEMINI_INTERACTIONS_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            extra_headers={"x-test": "yes"},
        )
    )

    client = make_llm_client(cfg=cfg, api_key="key", model="gemini-2.5-flash")

    assert isinstance(client, ChatClient)
    assert isinstance(client, GeminiInteractionsClient)
    assert client.base_url == "https://generativelanguage.googleapis.com/v1beta"
    assert client.extra_headers == {"x-test": "yes"}
    assert client.reasoning_trace_capability.adapter == "gemini_interactions_summary"
    assert client.reasoning_trace_capability.supports_buffered is True
    assert client.reasoning_trace_capability.supports_streaming is False


def test_make_llm_client_routes_gemini_interactions_when_config_flag_enabled(
    monkeypatch,
) -> None:
    monkeypatch.delenv(GEMINI_INTERACTIONS_EXPERIMENT_ENV, raising=False)
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini-interactions",
            protocol=GEMINI_INTERACTIONS_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
        )
    )
    cfg = set_config_value(cfg, GEMINI_INTERACTIONS_CONFIG_FLAG, "true")

    client = make_llm_client(cfg=cfg, api_key="key", model="gemini-2.5-flash")

    assert isinstance(client, GeminiInteractionsClient)


def test_make_llm_client_defaults_legacy_profile_without_protocol_to_openai_compat() -> None:
    profile = ProfileSpec.from_dict(
        "legacy",
        {
            "base_url": "https://legacy.example/v1",
            "default_model": "legacy-model",
        },
    )
    cfg = _cfg_with_profile(profile)

    client = make_llm_client(cfg=cfg, api_key="key", model="legacy-model")

    assert isinstance(client, OpenAICompatClient)
    assert client.base_url == "https://legacy.example/v1"


def test_create_session_accepts_gemini_generate_content_profile(tmp_path) -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            web_search_adapter="gemini_grounding",
        )
    )
    cfg.model = "gemini-3-flash-preview"
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="key",
        non_interactive=True,
    )

    assert isinstance(session.client, GeminiGenerateContentClient)
    assert session.client.model == "gemini-3-flash-preview"


def test_create_session_accepts_openai_responses_profile(tmp_path) -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-native",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url="https://api.openai.com/v1",
            web_search_adapter="openai_responses",
        )
    )
    cfg.model = "gpt-5.5"
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="key",
        non_interactive=True,
    )

    assert isinstance(session.client, OpenAIResponsesClient)
    assert session.client.model == "gpt-5.5"


def test_create_session_accepts_anthropic_messages_profile(tmp_path) -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
            base_url="https://api.anthropic.com/v1",
            web_search_adapter="anthropic_messages",
        )
    )
    cfg.model = "claude-sonnet-4-6"
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="key",
        non_interactive=True,
    )

    assert isinstance(session.client, AnthropicMessagesClient)
    assert session.client.model == "claude-sonnet-4-6"


def test_create_session_keeps_streaming_for_openai_responses_profile(tmp_path) -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai-native",
            protocol=OPENAI_RESPONSES_PROTOCOL,
            base_url="https://api.openai.com/v1",
            web_search_adapter="openai_responses",
        )
    )
    cfg.model = "gpt-5.5"
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"
    cfg.stream = True
    surface = _WarningSurface()

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="key",
        non_interactive=True,
        surface=surface,
    )

    assert isinstance(session.client, OpenAIResponsesClient)
    assert session.stream is True
    assert session.cfg.stream is True
    assert surface.warnings == []


def test_create_session_keeps_streaming_for_anthropic_messages_profile(tmp_path) -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="anthropic",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
            base_url="https://api.anthropic.com/v1",
            web_search_adapter="anthropic_messages",
        )
    )
    cfg.model = "claude-sonnet-4-6"
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"
    cfg.stream = True
    surface = _WarningSurface()

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="key",
        non_interactive=True,
        surface=surface,
    )

    assert isinstance(session.client, AnthropicMessagesClient)
    assert session.client.usage_contract.response_usage_authoritative is True
    assert session.client.usage_contract.supports_input_token_count is True
    assert session.stream is True
    assert session.cfg.stream is True
    assert surface.warnings == []


def test_create_session_keeps_streaming_for_gemini_generate_content_profile(tmp_path) -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini",
            protocol=GEMINI_GENERATE_CONTENT_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            web_search_adapter="gemini_grounding",
        )
    )
    cfg.model = "gemini-3-flash-preview"
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"
    cfg.stream = True
    surface = _WarningSurface()

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="key",
        non_interactive=True,
        surface=surface,
    )

    assert isinstance(session.client, GeminiGenerateContentClient)
    assert session.stream is True
    assert session.cfg.stream is True
    assert surface.warnings == []


def test_create_session_keeps_streaming_for_openai_compat_profile(tmp_path) -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="openai",
            protocol=OPENAI_COMPAT_PROTOCOL,
            base_url="https://api.openai.com/v1",
        )
    )
    cfg.model = "gpt-test"
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"
    cfg.stream = True
    surface = _WarningSurface()

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="key",
        non_interactive=True,
        surface=surface,
    )

    assert isinstance(session.client, OpenAICompatClient)
    assert session.stream is True
    assert session.cfg.stream is True
    assert not any("streaming is disabled" in warning for warning in surface.warnings)


def test_create_session_keeps_streaming_for_unknown_native_provider_profile(tmp_path) -> None:
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="custom-native",
            protocol=ANTHROPIC_MESSAGES_PROTOCOL,
            base_url="https://llm.internal.example/v1",
            web_search_adapter="anthropic_messages",
        )
    )
    cfg.model = "custom-model"
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"
    cfg.stream = True
    surface = _WarningSurface()

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="key",
        non_interactive=True,
        surface=surface,
    )

    assert isinstance(session.client, AnthropicMessagesClient)
    assert session.client.usage_contract.response_usage_authoritative is False
    assert session.client.usage_contract.supports_input_token_count is True
    assert session.client.usage_contract.input_token_count_strategy == ("anthropic_messages")
    assert session.stream is True
    assert session.cfg.stream is True
    assert not any("streaming is disabled" in warning for warning in surface.warnings)


def test_create_session_disables_streaming_for_gemini_interactions_profile(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv(GEMINI_INTERACTIONS_EXPERIMENT_ENV, "1")
    cfg = _cfg_with_profile(
        ProfileSpec(
            name="gemini-interactions",
            protocol=GEMINI_INTERACTIONS_PROTOCOL,
            base_url="https://generativelanguage.googleapis.com/v1beta",
            web_search_adapter="gemini_grounding",
        )
    )
    cfg.model = "gemini-2.5-flash"
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"
    cfg.stream = True
    surface = _WarningSurface()

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="key",
        non_interactive=True,
        surface=surface,
    )

    assert isinstance(session.client, GeminiInteractionsClient)
    assert session.stream is False
    assert session.cfg.stream is False
    assert any("streaming is disabled" in warning for warning in surface.warnings)
