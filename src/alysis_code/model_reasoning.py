"""Capability-backed effort selection for the exact selected provider route."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from .provider_auth.base import (
    ProviderAuthError,
    ProviderReasoningEffort,
    reasoning_effort_label,
)
from .reasoning_contracts import (
    ALWAYS_ON,
    OFF_BETWEEN_TOOLS,
    OPTIONAL,
    UNKNOWN_CONTRACT,
    WIRE_BUDGET_TOKENS,
    WIRE_CHAT_TEMPLATE_ENABLE_THINKING,
    WIRE_ENABLE_THINKING,
    WIRE_REASONING_EFFORT,
    WIRE_REASONING_ENABLED,
    WIRE_REASONING_OBJECT,
    WIRE_THINKING_ADAPTIVE,
    WIRE_THINKING_LEVEL,
    WIRE_THINKING_TYPE,
    ReasoningContract,
    reasoning_contract_for,
    resolve_reasoning_provider,
)

_LOGGER = logging.getLogger("alysis_code.model_reasoning")


def official_openai_efforts(model: str) -> tuple[ProviderReasoningEffort, ...]:
    return tuple(
        ProviderReasoningEffort(id=value, label=reasoning_effort_label(value))
        for value in reasoning_contract_for("openai", model).values
    )


def _contract_efforts(contract: ReasoningContract) -> tuple[ProviderReasoningEffort, ...]:
    if contract.mode not in {ALWAYS_ON, OPTIONAL}:
        return ()
    values = list(contract.values)
    if contract.wire == WIRE_THINKING_TYPE:
        # A fixed thinking mode isn't a selectable depth. Optional type-based
        # providers expose their actual modes, not invented low/medium/high tiers.
        if not contract.toggleable:
            return ()
        values = list(contract.values or ("enabled", "disabled"))
    elif contract.wire in {
        WIRE_BUDGET_TOKENS,
        WIRE_ENABLE_THINKING,
        WIRE_CHAT_TEMPLATE_ENABLE_THINKING,
        WIRE_REASONING_ENABLED,
    }:
        values = ["enabled"] if contract.toggleable else []
    elif contract.wire == WIRE_REASONING_OBJECT and not values and contract.toggleable:
        values = ["enabled"]
    if contract.toggleable and "none" not in values and "disabled" not in values:
        values.insert(0, "none")
    efforts = tuple(
        ProviderReasoningEffort(id=value, label=reasoning_effort_label(value)) for value in values
    )
    if contract.off == OFF_BETWEEN_TOOLS:
        return (
            ProviderReasoningEffort(
                id="between_tools",
                label="Between tools",
                description="Turns off up-front thinking; keeps thinking between tools at High effort.",
            ),
            *efforts,
        )
    return efforts


def contract_reasoning_capabilities(contract: ReasoningContract) -> dict[str, Any]:
    """Serialize one route's allowlisted reasoning choices for catalog consumers."""
    efforts = _contract_efforts(contract)
    return {
        "reasoning_efforts": [
            {"id": item.id, "label": item.label, "description": item.description or ""}
            for item in efforts
        ],
        "default_reasoning_effort": (
            contract.default if any(item.id == contract.default for item in efforts) else ""
        ),
    }


def hosted_model_capabilities(model: str) -> dict[str, Any]:
    """The hosted route's capacities and choices for account JSON and terminal views."""
    from .model_registry import catalog_model_capabilities

    return {
        **catalog_model_capabilities(model, "alysis"),
        **contract_reasoning_capabilities(reasoning_contract_for("alysis", model)),
    }


def _api_contract(
    cfg: Any, profile: Any, model: str, transport: httpx.BaseTransport | None
) -> ReasoningContract:
    from dataclasses import replace

    from .llm.factory import resolve_model_protocol
    from .profile_presets import find_preset_for_profile
    from .profiles import resolve_effective_base_url
    from .provider_model_catalog import (
        ProviderModelCatalogError,
        discover_provider_models,
        openrouter_reasoning_contract,
    )

    profile = replace(profile, base_url=resolve_effective_base_url(cfg=cfg, profile=profile))
    preset = find_preset_for_profile(profile)
    provider_key = preset.provider_key if preset else resolve_reasoning_provider(None, model)
    protocol = resolve_model_protocol(
        provider_key=provider_key, model=model, protocol=profile.protocol
    )
    if protocol == "openai_compat" and profile.reasoning_trace_adapter != "auto":
        from .llm.openai_compat import (
            _reasoning_transport_provider_key,
            _transport_provider_key,
        )

        provider = _transport_provider_key(
            base_url=profile.base_url, provider_key=provider_key, model=model
        )
        if (
            _reasoning_transport_provider_key(
                transport_provider_key=provider,
                reasoning_trace_adapter=profile.reasoning_trace_adapter,
            )
            != provider
        ):
            return UNKNOWN_CONTRACT
    if provider_key == "openrouter" and protocol == "openai_compat":
        # Explicit gateway capabilities override model defaults. Missing
        # metadata can use the same catalog model as a custom endpoint.
        try:
            models = discover_provider_models(
                profile=replace(profile, extra_headers={}), api_key=None, transport=transport
            )
        except ProviderModelCatalogError as exc:
            contract = openrouter_reasoning_contract(None, model=model)
            if contract is None:
                raise ProviderAuthError(
                    "Reasoning capabilities are temporarily unavailable."
                ) from exc
            return contract
        candidate = next((item for item in models if item.id == model), None)
        if candidate and candidate.reasoning_contract is not None:
            return candidate.reasoning_contract
        return openrouter_reasoning_contract(None, model=model) or UNKNOWN_CONTRACT
    contract = reasoning_contract_for(
        provider_key, model, preset_key=preset.key if preset else None
    )
    if protocol == "anthropic_messages":
        supported = {WIRE_THINKING_ADAPTIVE, WIRE_BUDGET_TOKENS}
    elif protocol == "gemini_generate_content":
        supported = {WIRE_THINKING_LEVEL, WIRE_BUDGET_TOKENS}
    elif protocol == "openai_responses":
        supported = {WIRE_REASONING_EFFORT, WIRE_REASONING_OBJECT}
    elif protocol == "openai_compat":
        # Official OpenAI reasoning with tools requires Responses on several
        # catalog models. Legacy compatibility routes must not promise it.
        if provider_key == "openai":
            return UNKNOWN_CONTRACT
        supported = {
            WIRE_REASONING_EFFORT,
            WIRE_REASONING_OBJECT,
            WIRE_THINKING_LEVEL,
            WIRE_THINKING_TYPE,
            WIRE_ENABLE_THINKING,
            WIRE_CHAT_TEMPLATE_ENABLE_THINKING,
            WIRE_REASONING_ENABLED,
        }
        if contract.wire == WIRE_REASONING_EFFORT and not contract.values:
            return UNKNOWN_CONTRACT
    else:
        return UNKNOWN_CONTRACT
    return contract if contract.wire in supported else UNKNOWN_CONTRACT


def model_reasoning_capabilities(
    cfg: Any, *, transport: httpx.BaseTransport | None = None
) -> dict[str, Any]:
    from .model_registry import catalog_model_capabilities
    from .profile_presets import find_preset_for_profile
    from .profiles import get_active_profile
    from .provider_auth import create_provider_auth

    profile = get_active_profile(cfg)
    model = str(cfg.model or profile.default_model or "").strip()
    efforts = ()
    default = ""
    capacity: dict[str, Any] = {}
    if profile.auth_provider:
        candidate = next(
            (
                item
                for item in create_provider_auth(
                    profile.auth_provider, transport=transport
                ).list_models()
                if item.id == model
            ),
            None,
        )
        if candidate:
            efforts, default = candidate.reasoning_efforts, candidate.default_reasoning_effort or ""
            # A subscription owns its limits; API model facts must not expand them.
            capacity = {
                "context_window_tokens": candidate.context_window_tokens,
                "max_output_tokens": candidate.max_output_tokens,
                "input_modalities": list(candidate.input_modalities),
            }
    else:
        contract = _api_contract(cfg, profile, model, transport)
        efforts = _contract_efforts(contract)
        default = contract.default if any(item.id == contract.default for item in efforts) else ""
        preset = find_preset_for_profile(profile)
        capacity = catalog_model_capabilities(model, preset.provider_key if preset else None)
    return {
        **capacity,
        "id": model,
        "profile_name": profile.name,
        "reasoning_efforts": [
            {"id": effort.id, "label": effort.label, "description": effort.description}
            for effort in efforts
        ],
        "default_reasoning_effort": default,
    }


def _plan_lists_effort_offline(cfg: Any, effort: str, *, error: ProviderAuthError) -> bool:
    """Whether a ChatGPT plan model takes ``effort``, when its account's model list can't be reached.

    Offline, refusing would stop a chat before its first request, while Auto
    starts and fails at the provider like any provider failure. The engine ships
    a snapshot of the plan's models (the model registry already falls back to
    it); an effort it lists for the model goes through, and the provider judges
    the request as usual. Any other failure to load the list (a refused sign-in,
    an error status), and every other route, keeps refusing.
    """
    from .chatgpt_codex_static_provider import resolve_chatgpt_codex_static_model
    from .profiles import get_active_profile

    if not isinstance(error.__cause__, httpx.HTTPError):
        return False
    profile = get_active_profile(cfg)
    if profile.auth_provider != "openai-codex":
        return False
    model = str(cfg.model or profile.default_model or "").strip()
    snapshot = resolve_chatgpt_codex_static_model(model)
    if snapshot is None or effort not in {value for value, _ in snapshot.reasoning_efforts}:
        return False
    _LOGGER.warning(
        "Reasoning effort %s for %s was checked against the bundled plan snapshot: "
        "the account's model list could not be reached.",
        effort,
        model,
    )
    return True


def invocation_reasoning_effort(
    cfg: Any, value: str, *, transport: httpx.BaseTransport | None = None
) -> str | None:
    """Validate an invocation override; never rewrite process-global saved profiles."""
    normalized = str(value).strip().lower()
    if normalized == "auto":
        return None
    try:
        capabilities = model_reasoning_capabilities(cfg, transport=transport)
    except ProviderAuthError as exc:
        if not _plan_lists_effort_offline(cfg, normalized, error=exc):
            raise
        return normalized
    supported = {item["id"] for item in capabilities["reasoning_efforts"]}
    if normalized not in supported:
        raise ProviderAuthError(
            "The selected reasoning effort is not supported by this provider and model."
        )
    return normalized


def invocation_reasoning_settings(
    cfg: Any, value: str, *, transport: httpx.BaseTransport | None = None
) -> tuple[str | None, bool | None]:
    """Translate a validated UI choice to client settings, including config reloads."""
    effort = invocation_reasoning_effort(cfg, value, transport=transport)
    enabled = None if effort is None else effort not in {"none", "disabled"}
    # Toggle-only choices are not provider effort levels.
    if effort == "enabled":
        effort = None
    elif effort in {"disabled", "between_tools"}:
        # The native client maps the reduced-thinking setting to the model's
        # wire mode. For between_tools this is not a literal reasoning-off flag.
        effort = "none"
        enabled = False
    return effort, enabled


def client_reasoning_settings(
    cfg: Any,
    *,
    model: str,
    profile: Any = None,
    reasoning_effort: str | None = None,
    enable_thinking: bool | None = None,
    transport: httpx.BaseTransport | None = None,
) -> tuple[str | None, bool | None]:
    """Resolve settings for a client without leaking a chat choice to other models."""
    from .branding import env_get
    from .config import resolve_llm_enable_thinking, resolve_llm_reasoning_effort
    from .profiles import get_active_profile

    invocation = env_get("ALYSIS_RUN_REASONING_EFFORT")
    if invocation is not None:
        selected_profile = get_active_profile(cfg)
        selected_model = str(cfg.model or selected_profile.default_model or "").strip()
        if model != selected_model or (profile is not None and profile != selected_profile):
            # Role clients may receive arguments already resolved for the main
            # model. Those hints belong to the chat choice too; use this route's
            # provider defaults instead of validating or forwarding them.
            return None, None
        return invocation_reasoning_settings(cfg, invocation, transport=transport)
    return (
        resolve_llm_reasoning_effort(cfg) if reasoning_effort is None else reasoning_effort,
        resolve_llm_enable_thinking(cfg) if enable_thinking is None else enable_thinking,
    )
