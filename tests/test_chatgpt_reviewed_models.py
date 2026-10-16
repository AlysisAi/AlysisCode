"""The ChatGPT model list offers the reviewed models the account's catalog does not mention.

OpenAI's /v1/models answer for a ChatGPT plan lists only part of what the plan can use on
this route. On 2026-10-04 an account whose answer listed gpt-6-astra, gpt-5.6-sol,
gpt-5.6-terra, gpt-5.6-luna and gpt-5.5 also completed /responses requests with gpt-6-luna,
gpt-6-sol and gpt-6.1-sol. The engine offers those reviewed models too, so every surface
(CLI, setup, desktop app) shows what the plan can use.
"""

from __future__ import annotations

import httpx
import pytest

from alysis_code.profiles import ProfileSpec, subscription_selection_supported
from alysis_code.provider_auth import openai_codex
from alysis_code.provider_auth.base import ProviderAuthError
from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth
from alysis_code.provider_auth.store import ProviderTokenRecord

LEVELS = [
    {"effort": effort, "description": effort}
    for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
]
# The visible entries and priorities of the account catalog seen on 2026-10-04.
ACCOUNT_CATALOG = [
    {
        "slug": "gpt-6-astra",
        "visibility": "list",
        "priority": 2,
        "supported_reasoning_levels": LEVELS,
        "default_reasoning_level": "medium",
    },
    {"slug": "gpt-reserve", "visibility": "hide", "priority": 4},
    {
        "slug": "gpt-5.6-sol",
        "visibility": "list",
        "priority": 5,
        "supported_reasoning_levels": LEVELS,
        "default_reasoning_level": "low",
    },
    {
        "slug": "gpt-5.6-terra",
        "visibility": "list",
        "priority": 8,
        "supported_reasoning_levels": LEVELS,
    },
    {
        "slug": "gpt-5.6-luna",
        "visibility": "list",
        "priority": 9,
        "supported_reasoning_levels": LEVELS[:5],
    },
    {
        "slug": "gpt-5.5",
        "visibility": "list",
        "priority": 13,
        "supported_reasoning_levels": LEVELS[:4],
    },
    {"slug": "codex-auto-review", "visibility": "hide", "priority": 43},
]
ADDED = {"gpt-6.1-sol", "gpt-6-sol", "gpt-6-luna"}


@pytest.fixture(autouse=True)
def signed_in(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    record = ProviderTokenRecord(
        client_id="test-client",
        subject="test-subject",
        scopes=("chatgpt.tokens.use.direct",),
        access_token="test-access",
        refresh_token="test-refresh",
        expires_at=9_999_999_999,
    )
    monkeypatch.setattr(openai_codex, "load_provider_token", lambda _id: record)


def _models(catalog, status=200):
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(status, json={"models": catalog})
        )
    )
    return adapter.list_models(refresh=True)


def test_account_catalog_gains_the_reviewed_models_it_does_not_list():
    models = _models(ACCOUNT_CATALOG)
    assert [model.id for model in models] == [
        "gpt-6.1-sol",
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    ]
    for model in models:
        assert model.description.endswith("access checked when used") is (model.id in ADDED)
    # The account's own first model stays the default; hidden entries stay hidden.
    assert [model.id for model in models if model.is_default] == ["gpt-6-astra"]


def test_added_models_bring_their_reviewed_efforts_without_ultra():
    by_id = {model.id: model for model in _models(ACCOUNT_CATALOG)}
    luna = by_id["gpt-6-luna"]
    assert [(effort.id, effort.label) for effort in luna.reasoning_efforts] == [
        ("low", "Low"),
        ("medium", "Medium"),
        ("high", "High"),
        ("xhigh", "Extra high"),
        ("max", "Max"),
    ]
    assert luna.default_reasoning_effort == "medium"
    assert by_id["gpt-6.1-sol"].default_reasoning_effort == "low"
    assert all(
        effort.id != "ultra" for model in by_id.values() for effort in model.reasoning_efforts
    )


def test_a_saved_choice_of_an_added_model_is_supported():
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url=OpenAICodexSubscriptionAuth.base_url,
        auth_provider="openai-codex",
        default_model="gpt-6-luna",
        reasoning_effort="high",
    )
    assert subscription_selection_supported(profile, _models(ACCOUNT_CATALOG))


def test_a_reviewed_model_the_catalog_hides_stays_hidden():
    catalog = [{"slug": "gpt-6-astra"}, {"slug": "gpt-6-luna", "visibility": "hide"}]
    assert "gpt-6-luna" not in [model.id for model in _models(catalog)]


@pytest.mark.parametrize(
    ("catalog", "expected"),
    [
        ([{"slug": "gpt-5.6-luna"}], ["gpt-5.6-luna"]),
        ([{"slug": "gpt-6-luna"}, {"slug": "gpt-5.6-luna"}], ["gpt-6-luna", "gpt-5.6-luna"]),
        ([{"slug": "model-a"}], ["model-a"]),
        ([], []),
    ],
)
def test_catalogs_without_a_reviewed_full_tier_model_are_not_broadened(catalog, expected):
    assert [model.id for model in _models(catalog)] == expected


def test_failed_discovery_offers_nothing():
    with pytest.raises(ProviderAuthError):
        _models([], status=500)
