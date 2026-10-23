from __future__ import annotations

from typing import Any

from .branding import env_get
from .config import AppConfig, ConfigError

ROLE_CODING = "coding"
ROLE_REVIEW = "review"
ROLE_CONFLICT_REVIEW = "conflict_review"
ROLE_CONFLICT_RESOLVE = "conflict_resolve"
ROLE_PLANNER = "planner"
ROLE_COMPREHENSION = "comprehension"
ROLE_COMPACTOR = "compactor"
ROLE_ROUTER = "router"
# Answers approvals in "Approve for me" (auto) mode; defaults to the chat's own model.
ROLE_APPROVAL_REVIEW = "approval_review"
PREFER_CONTEXT_FORGE = "forge"

ROLE_ENV_VARS: dict[str, str] = {
    ROLE_CODING: "ALYSIS_MODEL_CODING",
    ROLE_REVIEW: "ALYSIS_MODEL_REVIEW",
    ROLE_CONFLICT_REVIEW: "ALYSIS_MODEL_CONFLICT_REVIEW",
    ROLE_CONFLICT_RESOLVE: "ALYSIS_MODEL_CONFLICT_RESOLVE",
    ROLE_PLANNER: "ALYSIS_MODEL_PLANNER",
    ROLE_COMPREHENSION: "ALYSIS_COMPREHENSION_MODEL",
    ROLE_COMPACTOR: "ALYSIS_MODEL_COMPACTOR",
    ROLE_ROUTER: "ALYSIS_MODEL_ROUTER",
    ROLE_APPROVAL_REVIEW: "ALYSIS_MODEL_APPROVAL_REVIEW",
}


def _role_models_from_plan(plan: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(plan, dict):
        return {}
    raw = plan.get("role_models")
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        role = str(key).strip().lower()
        model = str(value).strip()
        if role and model:
            out[role] = model
    return out


def _role_models_from_cfg(cfg: AppConfig) -> dict[str, str]:
    raw = cfg.extra_fields.get("role_models") if isinstance(cfg.extra_fields, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        role = str(key).strip().lower()
        model = str(value).strip()
        if role and model:
            out[role] = model
    return out


def _forge_role_models_from_cfg(cfg: AppConfig) -> dict[str, str]:
    raw = cfg.extra_fields.get("forge_role_models") if isinstance(cfg.extra_fields, dict) else None
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        role = str(key).strip().lower()
        model = str(value).strip()
        if role and model:
            out[role] = model
    return out


def _allowed_model_for_active_profile(cfg: AppConfig, candidate: str) -> str:
    """Keep auth-profile role overrides inside the connected account catalog."""

    normalized = str(candidate or "").strip()
    if not normalized:
        return ""
    try:
        from .profiles import get_active_profile

        profile = get_active_profile(cfg)
    except ConfigError:
        return normalized
    if not profile.auth_provider or normalized == profile.default_model:
        return normalized
    try:
        from .provider_auth import create_provider_auth

        models = create_provider_auth(profile.auth_provider).list_models(refresh=False)
    except Exception:
        return ""
    return normalized if any(item.id == normalized for item in models) else ""


def resolve_model_for_role(
    *,
    cfg: AppConfig,
    role: str,
    plan: dict[str, Any] | None = None,
    fallback_to_default: bool = True,
    prefer_context: str = "default",
) -> str:
    role_key = role.strip().lower()
    if role_key not in ROLE_ENV_VARS:
        raise ConfigError(f"Unknown model role: {role}")

    env_key = ROLE_ENV_VARS[role_key]
    env_value = _allowed_model_for_active_profile(cfg, str(env_get(env_key) or ""))
    if env_value:
        return env_value

    plan_models = _role_models_from_plan(plan)
    plan_model = _allowed_model_for_active_profile(cfg, plan_models.get(role_key) or "")
    if plan_model:
        return plan_model

    if prefer_context == PREFER_CONTEXT_FORGE:
        forge_cfg_models = _forge_role_models_from_cfg(cfg)
        forge_cfg_model = _allowed_model_for_active_profile(
            cfg,
            forge_cfg_models.get(role_key) or "",
        )
        if forge_cfg_model:
            return forge_cfg_model

    cfg_models = _role_models_from_cfg(cfg)
    cfg_model = _allowed_model_for_active_profile(cfg, cfg_models.get(role_key) or "")
    if cfg_model:
        return cfg_model

    if role_key == ROLE_COMPREHENSION:
        return resolve_model_for_role(
            cfg=cfg,
            role=ROLE_CODING,
            plan=plan,
            fallback_to_default=fallback_to_default,
            prefer_context=prefer_context,
        )

    if fallback_to_default:
        fallback = (cfg.model or "").strip()
        if fallback:
            return fallback

    raise ConfigError(
        "Model is not set for role "
        f"'{role_key}'. Set {env_key}, role_models.{role_key}, or default model."
    )
