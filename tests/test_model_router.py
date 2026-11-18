from __future__ import annotations

import pytest

from alysis_code.config import AppConfig, ConfigError
from alysis_code.model_router import (
    ROLE_CODING,
    ROLE_COMPACTOR,
    ROLE_COMPREHENSION,
    ROLE_REVIEW,
    ROLE_ROUTER,
    resolve_model_for_role,
)
from alysis_code.profiles import ProfileSpec
from alysis_code.provider_auth import ProviderModel


def test_resolve_model_for_role_env_overrides_plan_and_config(monkeypatch) -> None:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {
        "role_models": {
            ROLE_CODING: "config-coding-model",
            ROLE_REVIEW: "config-review-model",
        }
    }
    plan = {"role_models": {ROLE_CODING: "plan-coding-model"}}
    monkeypatch.setenv("ALYSIS_MODEL_CODING", "env-coding-model")

    resolved = resolve_model_for_role(cfg=cfg, role=ROLE_CODING, plan=plan)
    assert resolved == "env-coding-model"


def test_resolve_model_for_role_plan_overrides_config(monkeypatch) -> None:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {
        "role_models": {
            ROLE_CODING: "config-coding-model",
        }
    }
    plan = {"role_models": {ROLE_CODING: "plan-coding-model"}}
    monkeypatch.delenv("ALYSIS_MODEL_CODING", raising=False)

    resolved = resolve_model_for_role(cfg=cfg, role=ROLE_CODING, plan=plan)
    assert resolved == "plan-coding-model"


def test_subscription_profile_ignores_role_models_outside_account_catalog(monkeypatch) -> None:
    class _Catalog:
        def list_models(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
            assert refresh is False
            return (
                ProviderModel(id="sub-default", label="Default"),
                ProviderModel(id="sub-alt", label="Alt"),
            )

    monkeypatch.setattr(
        "alysis_code.provider_auth.create_provider_auth",
        lambda _provider_id: _Catalog(),
    )
    cfg = AppConfig(model="sub-default")
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model="sub-default",
        reasoning_effort="high",
    )
    cfg.extra_fields = {
        "profiles": {profile.name: profile.to_dict()},
        "active_profile": profile.name,
        "role_models": {ROLE_ROUTER: "claude-old"},
    }

    assert resolve_model_for_role(cfg=cfg, role=ROLE_ROUTER) == "sub-default"
    cfg.extra_fields["role_models"] = {ROLE_ROUTER: "sub-alt"}
    assert resolve_model_for_role(cfg=cfg, role=ROLE_ROUTER) == "sub-alt"


def test_resolve_model_for_role_falls_back_to_cfg_model(monkeypatch) -> None:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {}
    monkeypatch.delenv("ALYSIS_MODEL_REVIEW", raising=False)

    resolved = resolve_model_for_role(cfg=cfg, role=ROLE_REVIEW, plan=None)
    assert resolved == "default-model"


def test_inherited_router_tracks_default_until_explicitly_overridden(monkeypatch) -> None:
    monkeypatch.delenv("ALYSIS_MODEL_ROUTER", raising=False)
    cfg = AppConfig(model="first-setup-model")
    cfg.extra_fields = {"role_models": {}}

    assert resolve_model_for_role(cfg=cfg, role=ROLE_ROUTER) == "first-setup-model"

    cfg.model = "new-default-model"
    assert resolve_model_for_role(cfg=cfg, role=ROLE_ROUTER) == "new-default-model"

    cfg.extra_fields["role_models"][ROLE_ROUTER] = "explicit-router-model"
    cfg.model = "later-default-model"
    assert resolve_model_for_role(cfg=cfg, role=ROLE_ROUTER) == "explicit-router-model"


def test_resolve_model_for_role_raises_when_all_empty(monkeypatch) -> None:
    cfg = AppConfig(model="")
    cfg.extra_fields = {}
    monkeypatch.delenv("ALYSIS_MODEL_CODING", raising=False)

    with pytest.raises(ConfigError, match="Model is not set for role 'coding'"):
        resolve_model_for_role(cfg=cfg, role=ROLE_CODING, plan={})


def test_resolve_model_for_role_can_disable_default_fallback(monkeypatch) -> None:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {}
    monkeypatch.delenv("ALYSIS_MODEL_CONFLICT_RESOLVE", raising=False)

    with pytest.raises(ConfigError, match="conflict_resolve"):
        resolve_model_for_role(
            cfg=cfg,
            role="conflict_resolve",
            plan={},
            fallback_to_default=False,
        )


def test_resolve_model_for_role_rejects_unknown_role() -> None:
    cfg = AppConfig(model="default-model")
    with pytest.raises(ConfigError, match="Unknown model role"):
        resolve_model_for_role(cfg=cfg, role="unknown-role", plan=None)


def test_resolve_model_for_compactor_env_overrides_plan_and_config(monkeypatch) -> None:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {"role_models": {ROLE_COMPACTOR: "config-compactor-model"}}
    plan = {"role_models": {ROLE_COMPACTOR: "plan-compactor-model"}}
    monkeypatch.setenv("ALYSIS_MODEL_COMPACTOR", "env-compactor-model")

    resolved = resolve_model_for_role(cfg=cfg, role=ROLE_COMPACTOR, plan=plan)
    assert resolved == "env-compactor-model"


def test_resolve_model_for_comprehension_uses_dedicated_env_var(monkeypatch) -> None:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {
        "role_models": {
            ROLE_CODING: "config-coding-model",
            ROLE_COMPREHENSION: "config-comprehension-model",
        }
    }
    monkeypatch.setenv("ALYSIS_COMPREHENSION_MODEL", "env-comprehension-model")

    resolved = resolve_model_for_role(cfg=cfg, role=ROLE_COMPREHENSION, plan={})
    assert resolved == "env-comprehension-model"


def test_resolve_model_for_comprehension_falls_back_to_coding_role(monkeypatch) -> None:
    cfg = AppConfig(model="default-model")
    cfg.extra_fields = {"role_models": {ROLE_CODING: "config-coding-model"}}
    monkeypatch.delenv("ALYSIS_COMPREHENSION_MODEL", raising=False)
    monkeypatch.delenv("ALYSIS_MODEL_CODING", raising=False)

    resolved = resolve_model_for_role(cfg=cfg, role=ROLE_COMPREHENSION, plan={})
    assert resolved == "config-coding-model"
