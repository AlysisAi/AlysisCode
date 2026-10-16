"""A chosen effort on a ChatGPT plan model when the account's model list can't be reached.

Offline, the subscription adapter can't download the account's model list. With
Auto the chat starts and its first request fails at the provider like any
provider failure; a chosen effort used to stop the chat before that, with
"Could not load models for this ChatGPT account.". The engine ships a snapshot of
the plan's models (the model registry already falls back to it); an effort it
lists for the model now goes through, and the provider judges the request.
"""

from __future__ import annotations

import httpx
import pytest

import alysis_code.provider_auth as provider_auth_mod
from alysis_code.config import AppConfig
from alysis_code.model_reasoning import invocation_reasoning_effort
from alysis_code.profiles import ProfileSpec, add_profile, set_active_profile
from alysis_code.provider_auth.base import ProviderAuthError


def _subscription_cfg(model: str) -> AppConfig:
    cfg = AppConfig(model=model)
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url="https://chatgpt.com/backend-api/codex",
        auth_provider="openai-codex",
        default_model=model,
    )
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


class _Adapter:
    def __init__(self, error: ProviderAuthError) -> None:
        self.error = error

    def list_models(self, *, refresh: bool = False):  # type: ignore[no-untyped-def]
        raise self.error


def _offline() -> ProviderAuthError:
    # As the adapter raises it when the request can't reach the server.
    try:
        raise httpx.ConnectError("offline")
    except httpx.ConnectError as cause:
        try:
            raise ProviderAuthError("Could not load models for this ChatGPT account.") from cause
        except ProviderAuthError as error:
            return error


def _use(monkeypatch: pytest.MonkeyPatch, error: ProviderAuthError) -> None:
    monkeypatch.setattr(
        provider_auth_mod, "create_provider_auth", lambda *_args, **_kwargs: _Adapter(error)
    )


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max"])
def test_offline_an_effort_the_plan_lists_for_the_model_goes_through(monkeypatch, effort):
    _use(monkeypatch, _offline())
    assert invocation_reasoning_effort(_subscription_cfg("gpt-6-luna"), effort) == effort


def test_offline_an_effort_the_model_does_not_take_is_still_refused(monkeypatch):
    # gpt-6-luna's plan efforts are low to max: "ultra" is gpt-6-sol's alone.
    _use(monkeypatch, _offline())
    with pytest.raises(ProviderAuthError, match="Could not load models"):
        invocation_reasoning_effort(_subscription_cfg("gpt-6-luna"), "ultra")


def test_offline_a_model_the_snapshot_does_not_know_is_still_refused(monkeypatch):
    _use(monkeypatch, _offline())
    with pytest.raises(ProviderAuthError, match="Could not load models"):
        invocation_reasoning_effort(_subscription_cfg("gpt-unknown-subscription"), "medium")


def test_other_failures_to_load_the_list_are_still_refused(monkeypatch):
    # Not a network failure (a refused sign-in, a server error): nothing is assumed.
    _use(monkeypatch, ProviderAuthError("ChatGPT model discovery failed with status 403."))
    with pytest.raises(ProviderAuthError, match="status 403"):
        invocation_reasoning_effort(_subscription_cfg("gpt-6-luna"), "medium")


def test_auto_needs_no_model_list(monkeypatch):
    _use(monkeypatch, _offline())
    assert invocation_reasoning_effort(_subscription_cfg("gpt-6-luna"), "auto") is None
