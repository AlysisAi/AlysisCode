from __future__ import annotations

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.cli_impl import config_menu as config_menu_mod
from alysis_code.cli_impl.config_menu import ConfigMenuState
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import (
    AppConfig,
    ConfigError,
    load_config,
    resolve_llm_enable_thinking,
    resolve_llm_reasoning_effort,
    save_config,
    set_config_value,
)
from alysis_code.profiles import (
    ProfileSpec,
    add_profile,
    get_active_profile,
    update_active_profile_defaults,
    update_profile,
)
from alysis_code.provider_auth import (
    ProviderAccountStatus,
    ProviderModel,
    ProviderReasoningEffort,
)


class _CatalogAdapter:
    def account_status(self) -> ProviderAccountStatus:
        return ProviderAccountStatus(connected=True)

    def list_models(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
        return (
            ProviderModel(
                id="gpt-codex-a",
                label="GPT Codex A",
                description="Primary model",
                is_default=True,
                reasoning_efforts=(
                    ProviderReasoningEffort("low", "Low"),
                    ProviderReasoningEffort("high", "High"),
                ),
                default_reasoning_effort="high",
                context_window_tokens=272_000,
                max_output_tokens=32_000,
            ),
            ProviderModel(
                id="gpt-codex-b",
                label="GPT Codex B",
                reasoning_efforts=(
                    ProviderReasoningEffort("medium", "Medium"),
                    ProviderReasoningEffort("max", "Max"),
                    ProviderReasoningEffort("ultra", "Ultra"),
                ),
                default_reasoning_effort="medium",
                context_window_tokens=272_000,
                max_output_tokens=32_000,
            ),
        )


def _subscription_cfg() -> AppConfig:
    cfg = AppConfig(model="gpt-codex-a")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://api.openai.com/v1",
        auth_provider="openai-codex",
        default_model="gpt-codex-a",
    )
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
    }
    return cfg


def test_config_uses_subscription_catalog_for_models_and_effort(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = _CatalogAdapter()
    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda _provider_id: adapter,
    )
    state = ConfigMenuState.from_cfg(_subscription_cfg())

    rows = config_menu_mod._default_model_rows(state)

    assert [row[0] for row in rows] == ["gpt-codex-a", "gpt-codex-b"]
    assert config_menu_mod._thinking_labels_for_state(state, model="gpt-codex-a") == (
        "auto",
        "low",
        "high",
    )
    assert config_menu_mod._thinking_labels_for_state(state, model="gpt-codex-b") == (
        "auto",
        "medium",
        "max",
        "ultra",
    )


@pytest.mark.parametrize(
    ("base_url", "model", "expected"),
    [
        (
            "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
            "qwen3.8-max",
            ("off", "low", "medium", "xhigh", "auto"),
        ),
        (
            "https://api.deepseek.com",
            "deepseek-v4-pro",
            ("off", "low", "high", "max", "auto"),
        ),
        (
            "https://integrate.api.nvidia.com/v1",
            "nvidia/nemotron-3-super-120b-a12b",
            ("off", "low", "high", "auto"),
        ),
        (
            "https://integrate.api.nvidia.com/v1",
            "deepseek-ai/deepseek-v4-pro",
            ("off", "high", "max", "auto"),
        ),
    ],
)
def test_api_profile_reasoning_picker_uses_documented_model_contract(
    base_url: str,
    model: str,
    expected: tuple[str, ...],
) -> None:
    state = ConfigMenuState.from_cfg(AppConfig(base_url=base_url, model=model))

    assert config_menu_mod._thinking_labels_for_state(state) == expected


def test_tui_config_persists_subscription_model_and_supported_effort(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    adapter = _CatalogAdapter()
    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda _provider_id: adapter,
    )
    cfg = _subscription_cfg()
    flow = ConfigFlow(cfg=cfg)

    flow.choose("default")
    assert [row.value for row in flow.screen().rows] == ["gpt-codex-a", "gpt-codex-b"]
    flow.choose("gpt-codex-b")
    assert flow.stage == "model_thinking"
    assert [row.value for row in flow.screen().rows] == ["auto", "medium", "max", "ultra"]
    flow.choose("medium")
    assert flow.stage == "menu"
    result = flow.state.commit_to(cfg)

    assert result.saved is True
    assert cfg.execution.backend == "native"
    assert cfg.execution.runtime is None
    assert cfg.model == "gpt-codex-b"
    assert cfg.llm_reasoning_effort == "medium"
    assert get_active_profile(cfg).default_model == "gpt-codex-b"
    assert get_active_profile(cfg).auth_provider == "openai-codex"
    assert get_active_profile(cfg).reasoning_effort == "medium"
    assert "subscription_model_selection_required" not in cfg.extra_fields
    assert cfg.extra_fields["onboarded"] is True


def test_native_session_accepts_subscription_profile_without_api_key(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda _provider_id: _CatalogAdapter(),
    )
    cfg = _subscription_cfg()
    update_profile(
        cfg,
        "chatgpt-codex",
        reasoning_effort="high",
        allow_subscription_selection=True,
    )
    cfg.routing_mode = "code_only"
    cfg.web_search_mode = "off"

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="review",
        yes=True,
        max_steps=1,
        no_log=True,
        non_interactive=True,
    )

    try:
        assert session.api_key == ""
        assert session.api_key_source == "provider-auth:openai-codex"
        assert session.client.model == "gpt-codex-a"
        assert session.client.provider_auth.provider_id == "openai-codex"
    finally:
        session.close()


def test_config_preserves_saved_subscription_effort_when_catalog_is_offline(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    class _OfflineAdapter:
        def list_models(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
            raise RuntimeError("offline")

    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda _provider_id: _OfflineAdapter(),
    )
    cfg = _subscription_cfg()
    update_profile(
        cfg,
        "chatgpt-codex",
        reasoning_effort="high",
        allow_subscription_selection=True,
    )
    state = ConfigMenuState.from_cfg(cfg)

    assert config_menu_mod._thinking_labels_for_state(state) == ("auto", "high")


def test_offline_subscription_selection_remains_pending(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    class _OfflineAdapter:
        def account_status(self) -> ProviderAccountStatus:
            return ProviderAccountStatus(connected=False)

        def list_models(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
            raise RuntimeError("offline")

    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda _provider_id: _OfflineAdapter(),
    )
    cfg = _subscription_cfg()
    update_profile(
        cfg,
        "chatgpt-codex",
        reasoning_effort="high",
        allow_subscription_selection=True,
    )
    state = ConfigMenuState.from_cfg(cfg)
    assert state.subscription_selection_required is False
    state.set_thinking_label("low")

    result = state.commit_to(cfg)

    assert result.saved is True
    assert cfg.extra_fields["subscription_model_selection_required"] == "openai-codex"
    assert cfg.extra_fields.get("onboarded") is not True


def test_subscription_model_change_requires_paired_effort_confirmation(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda _provider_id: _CatalogAdapter(),
    )
    cfg = _subscription_cfg()
    update_profile(
        cfg,
        "chatgpt-codex",
        reasoning_effort="high",
        allow_subscription_selection=True,
    )
    state = ConfigMenuState.from_cfg(cfg)
    state.set_field("model", "gpt-codex-b")

    result = state.commit_to(cfg)

    assert result.saved is False
    assert result.error == "Choose a reasoning effort for the subscription model."
    assert get_active_profile(cfg).default_model == "gpt-codex-a"


def test_pending_confirmation_survives_profile_switch_and_subscription_reselection(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    adapter = _CatalogAdapter()
    adapter.profile_name = "chatgpt-codex"  # type: ignore[attr-defined]
    adapter.protocol = "openai_responses"  # type: ignore[attr-defined]
    adapter.base_url = "https://api.openai.com/v1"  # type: ignore[attr-defined]
    adapter.display_name = "ChatGPT Codex subscription"  # type: ignore[attr-defined]
    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda _provider_id: adapter,
    )
    cfg = _subscription_cfg()
    cfg.extra_fields["subscription_model_selection_required"] = "openai-codex"
    state = ConfigMenuState.from_cfg(cfg)
    state.add_profile_spec(ProfileSpec(name="api", base_url="https://api.example/v1"))
    state.set_active_profile_name("chatgpt-codex")

    assert state.subscription_selection_required is True

    state.set_execution_backend("delegated", runtime="openai-codex")

    assert state.subscription_selection_required is True


def test_legacy_subscription_profile_without_effort_does_not_inherit_stale_effort() -> None:
    cfg = _subscription_cfg()
    update_profile(
        cfg,
        "chatgpt-codex",
        reasoning_effort=None,
        allow_subscription_selection=True,
    )
    cfg.llm_enable_thinking = True
    cfg.llm_reasoning_effort = "high"

    state = ConfigMenuState.from_cfg(cfg)

    assert state.thinking_label == "auto"
    assert state.subscription_selection_required is True


def test_subscription_selection_cannot_be_mutated_outside_default_model() -> None:
    cfg = _subscription_cfg()
    original = get_active_profile(cfg)

    with pytest.raises(ConfigError, match="Default Model"):
        set_config_value(cfg, "model", "outside-config")
    with pytest.raises(ConfigError, match="Default Model"):
        update_active_profile_defaults(cfg, default_model="outside-config")
    with pytest.raises(ConfigError, match="Default Model"):
        update_profile(cfg, original.name, reasoning_effort="low")
    with pytest.raises(ConfigError, match="managed by the AI subscription"):
        add_profile(
            cfg,
            ProfileSpec(
                name=original.name,
                base_url="https://override.example/v1",
                default_model="outside-config",
            ),
        )

    assert get_active_profile(cfg) == original


def test_subscription_effort_ignores_environment_overrides(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    cfg = _subscription_cfg()
    update_profile(
        cfg,
        "chatgpt-codex",
        reasoning_effort="low",
        allow_subscription_selection=True,
    )
    monkeypatch.setenv("ALYSIS_LLM_REASONING_EFFORT", "ultra")
    monkeypatch.setenv("ALYSIS_LLM_ENABLE_THINKING", "false")

    assert resolve_llm_reasoning_effort(cfg) == "low"
    assert resolve_llm_enable_thinking(cfg) is True


def test_subscription_model_and_effort_round_trip_on_disk(
    monkeypatch,
    tmp_path,
) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path))
    cfg = _subscription_cfg()
    state = ConfigMenuState.from_cfg(cfg)
    state.set_field("model", "gpt-codex-b")
    state.set_thinking_label("ultra")

    result = state.commit_to(cfg)
    save_config(cfg)
    loaded = load_config()

    assert result.saved is True
    assert loaded.model == "gpt-codex-b"
    assert loaded.llm_reasoning_effort == "ultra"
    assert get_active_profile(loaded).default_model == "gpt-codex-b"
    assert get_active_profile(loaded).reasoning_effort == "ultra"
