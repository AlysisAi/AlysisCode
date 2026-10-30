"""Shared execution admission without loading the interactive command shell."""

from __future__ import annotations

from dataclasses import dataclass

import typer

from ..branding import env_get
from ..config import AppConfig, ConfigError, load_config
from .commands import _patchable
from .commands._shared import _console


def _apply_temperature_override(cfg: AppConfig, temperature: float) -> None:
    cfg.temperature = temperature
    cfg.coding_temperature = temperature
    cfg.review_temperature = temperature
    cfg.planner_temperature = temperature
    cfg.conflict_review_temperature = temperature
    cfg.compactor_temperature = temperature
    cfg.chat_temperature = temperature


@dataclass(frozen=True)
class _SubscriptionAvailability:
    active: bool = False
    ready: bool = True
    provider_id: str = ""
    message: str = ""
    is_error: bool = False
    selection_required: bool = False


def _subscription_availability(cfg: AppConfig) -> _SubscriptionAvailability:
    """Return subscription readiness without deciding whether the UI may open."""

    try:
        from ..profiles import active_subscription_selection_ready, get_active_profile

        active_profile = get_active_profile(cfg)
        provider_id = str(active_profile.auth_provider or "").strip()
    except Exception as exc:
        return _SubscriptionAvailability(
            active=True,
            ready=False,
            message=f"Model access configuration is invalid: {exc}",
            is_error=True,
        )
    if not provider_id:
        return _SubscriptionAvailability()
    try:
        from ..provider_auth import create_provider_auth

        adapter = create_provider_auth(provider_id)
        if active_profile.protocol != adapter.protocol or active_profile.base_url.rstrip(
            "/"
        ) != adapter.base_url.rstrip("/"):
            return _SubscriptionAvailability(
                active=True,
                ready=False,
                provider_id=provider_id,
                message=(
                    "The subscription connection profile is incompatible with its provider "
                    "adapter. Reconnect it through `alysis setup`."
                ),
                is_error=True,
            )
        status = adapter.account_status()
    except Exception as exc:  # noqa: BLE001
        return _SubscriptionAvailability(
            active=True,
            ready=False,
            provider_id=provider_id,
            message=f"Subscription status check failed: {exc}",
            is_error=True,
        )
    if status.connected:
        if active_subscription_selection_ready(cfg):
            return _SubscriptionAvailability(
                active=True,
                ready=True,
                provider_id=provider_id,
            )
        return _SubscriptionAvailability(
            active=True,
            ready=False,
            provider_id=provider_id,
            message=(
                "Choose the subscription model and reasoning effort in "
                "/config → Default Model before sending a message."
            ),
            selection_required=True,
        )
    detail = f" ({status.detail})" if status.detail else ""
    if not status.verified:
        return _SubscriptionAvailability(
            active=True,
            ready=False,
            provider_id=provider_id,
            message=(
                "The AI subscription status is temporarily unavailable"
                f"{detail}. Try again shortly; your saved connection was not removed."
            ),
        )
    return _SubscriptionAvailability(
        active=True,
        ready=False,
        provider_id=provider_id,
        message=(
            "The selected AI subscription is not connected"
            f"{detail}. Type /login in the TUI and choose the connection, or run "
            f"`alysis auth login {provider_id}`; use /config to change model access."
        ),
    )


def _provider_auth_ready_for_chat() -> bool:
    """Report whether a model call can start; this must not gate opening the TUI."""

    try:
        availability = _subscription_availability(_patchable("load_config", load_config)())
    except Exception as exc:
        _console().print(f"[red]Model access configuration is invalid:[/red] {exc}")
        return False
    if availability.ready:
        return True
    style = "red" if availability.is_error else "yellow"
    _console().print(f"[{style}]{availability.message}[/{style}]")
    return False


def _require_active_subscription_ready(
    *,
    model: str | None,
    base_url: str | None,
    require_ready: bool = True,
) -> None:
    """Apply the subscription selection/account gate to any LLM-facing command."""

    try:
        from ..profiles import get_active_profile

        auth_profile = get_active_profile(_patchable("load_config", load_config)())
    except Exception:
        auth_profile = None
    if auth_profile is None or not auth_profile.auth_provider:
        return
    model_is_override = model is not None and str(model).strip() != auth_profile.default_model
    base_url_is_override = base_url is not None and (
        str(base_url).strip().rstrip("/") != auth_profile.base_url.rstrip("/")
    )
    if model_is_override or base_url_is_override:
        _console().print(
            "[red]Subscription model and endpoint overrides are managed in "
            "`/config` so model and reasoning stay compatible.[/red]"
        )
        raise typer.Exit(code=2)
    if (
        require_ready
        and not _patchable("_provider_auth_ready_for_chat", _provider_auth_ready_for_chat)()
    ):
        raise typer.Exit(code=1)


def _resolve_api_key_override(
    *,
    api_key: str | None,
    api_key_env: str | None,
    api_key_stdin: bool,
) -> str | None:
    # Per-command overrides stay in-memory; persistent storage is handled only
    # by explicit config credentials commands / setup flow.
    selected = sum(bool(x) for x in [api_key is not None, api_key_env is not None, api_key_stdin])
    if selected > 1:
        raise ConfigError("Use only one of: --api-key, --api-key-env, --api-key-stdin")

    if api_key is not None:
        v = api_key.strip()
        if not v:
            raise ConfigError("API key is empty.")
        return v

    if api_key_env is not None:
        name = api_key_env.strip()
        if not name:
            raise ConfigError("--api-key-env must be non-empty.")
        v = (env_get(name) or "").strip()
        if not v:
            raise ConfigError(f"Environment variable {name} is not set.")
        return v

    if api_key_stdin:
        v = typer.prompt("API key", hide_input=True).strip()
        if not v:
            raise ConfigError("API key is empty.")
        return v

    return None
