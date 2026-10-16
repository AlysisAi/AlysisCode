from __future__ import annotations

import json
from types import SimpleNamespace

import httpx
import pytest

from alysis_code.cli_impl import config_menu
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.model_registry import ModelRegistry
from alysis_code.profile_presets import get_preset, make_profile_from_preset
from alysis_code.profiles import ProfileSpec, add_profile, get_active_profile, set_active_profile
from alysis_code.provider_auth.openai_codex import OpenAICodexSubscriptionAuth
from alysis_code.provider_auth.store import ProviderTokenRecord
from alysis_code.provider_model_catalog import ProviderModelCatalogError, ProviderModelOption

MODEL = "gpt-6.1-sol"
EFFORTS = ["auto", "low", "medium", "high", "xhigh", "max"]


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    record = ProviderTokenRecord(
        access_token="subscription-test",
        refresh_token="refresh-test",
        expires_at=9_999_999_999,
        account_id="fixture-account",
        client_id="oaiapp_fixture",
        subject="fixture-subject",
        scopes=("chatgpt.tokens.use.direct", "resource.invoke"),
    )
    monkeypatch.setattr(
        "alysis_code.provider_auth.openai_codex.load_provider_token", lambda _: record
    )
    monkeypatch.setattr(
        httpx.HTTPTransport, "handle_request", lambda *a, **k: pytest.fail("Real HTTP")
    )


def config(preset):
    profile = make_profile_from_preset(get_preset(preset))
    cfg = AppConfig(model=profile.default_model, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


@pytest.mark.parametrize("preset", ["openai", "openai-responses"])
def test_api_picker_saves_sol_with_correct_prices_and_efforts(preset):
    cfg = config(preset)
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert MODEL in [r.value for r in flow.screen().rows]
    flow.choose(MODEL)
    flow.submit_input("")
    assert set(r.value for r in flow.screen().rows) == set(EFFORTS)
    flow.choose("max")
    assert flow.state.commit_to(cfg).saved
    assert cfg.model == MODEL
    assert get_active_profile(cfg).reasoning_effort == "max"
    meta = ModelRegistry(cfg=cfg).get(MODEL)
    assert meta.context_window_tokens == 1_050_000
    assert meta.max_output_tokens == 128_000
    assert meta.input_cost_per_token == 2 / 1_000_000
    assert meta.output_cost_per_token == 10 / 1_000_000
    assert meta.cache_read_input_cost_per_token == pytest.approx(0.1 / 1_000_000)
    assert meta.cache_creation_input_cost_per_token == 2.5 / 1_000_000


@pytest.mark.parametrize("catalog", ["paid", "free", "offline"])
def test_hosted_picker_checks_account_catalog_and_never_broadens_free_access(
    monkeypatch, catalog, configured_hosted_api
):
    cfg = config("alysis")
    monkeypatch.setattr(
        config_menu, "resolve_api_key", lambda cfg: SimpleNamespace(key="slk_test", source="test")
    )
    calls = []

    def discover(**kwargs):
        calls.append(kwargs)
        if catalog == "offline":
            raise ProviderModelCatalogError("offline")
        models = ["deepseek-flash", "gpt-6-luna", "unreviewed-model"]
        if catalog == "paid":
            models.append(MODEL)
        return tuple(ProviderModelOption(id=m, label=m) for m in models)

    monkeypatch.setattr(config_menu, "discover_provider_models", discover)
    flow = ConfigFlow(cfg=cfg)
    # A saved paid model cannot grant access if the account is now Free/offline.
    flow.state.fields["model"] = MODEL
    flow.choose("default")
    ids = [r.value for r in flow.screen().rows]
    assert (MODEL in ids) is (catalog == "paid")
    assert "unreviewed-model" not in ids
    assert len(calls) == 1
    assert calls[0]["api_key"] == "slk_test"
    if catalog == "paid":
        flow.choose(MODEL)
        assert set(r.value for r in flow.screen().rows) == set(EFFORTS)
        flow.choose("high")
        assert flow.state.commit_to(cfg).saved
        assert cfg.model == MODEL
        assert cfg.base_url == f"{configured_hosted_api}/llm/v1"


@pytest.mark.parametrize("preset", ["alysis", "openai", "openai-responses"])
def test_sol_factory_uses_responses_and_preserves_tools_and_model(preset):
    cfg = config(preset)
    captured = []

    def handle(request):
        payload = json.loads(request.content)
        captured.append(payload)
        assert request.url.path.endswith("/v1/responses")
        assert payload["model"] == MODEL
        assert payload["reasoning"]["effort"] == "high"
        assert payload["tools"][0]["name"] == "read_file"
        assert "temperature" not in payload
        return httpx.Response(200, json={"status": "completed", "output": []})

    client = make_llm_client(
        cfg=cfg,
        api_key="slk_test" if preset == "alysis" else "test-key",
        model=MODEL,
        reasoning_effort="high",
        transport=httpx.MockTransport(handle),
    )
    assert isinstance(client, OpenAIResponsesClient)
    client.chat(
        messages=[{"role": "user", "content": "Read the file"}],
        tools=[
            {
                "type": "function",
                "function": {"name": "read_file", "parameters": {"type": "object"}},
            }
        ],
    )
    assert len(captured) == 1


def test_codex_catalog_selection_uses_subscription_capacity_and_wire_payload(monkeypatch):
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"models": [{"slug": MODEL}]})
        )
    )
    monkeypatch.setattr("alysis_code.provider_auth.create_provider_auth", lambda _: adapter)
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url=adapter.base_url,
        auth_provider="openai-codex",
        default_model="gpt-5.6-sol",
        reasoning_effort="low",
    )
    cfg = AppConfig(model=profile.default_model)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    # The account lists only Sol 6.1; the reviewed ChatGPT models follow it.
    assert [r.value for r in flow.screen().rows] == [
        MODEL,
        "gpt-6-astra",
        "gpt-6-sol",
        "gpt-6-luna",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    ]
    flow.choose(MODEL)
    assert set(r.value for r in flow.screen().rows) == set(EFFORTS)
    flow.choose("max")
    assert flow.state.commit_to(cfg).saved
    meta = ModelRegistry(cfg=cfg).get(MODEL)
    assert meta.context_window_tokens == 272_000
    assert meta.input_cost_per_token == 0.0
    assert adapter.list_models()[0].default_reasoning_effort == "medium"
    payload = adapter.adapt_responses_payload(
        {
            "model": MODEL,
            "reasoning": {"effort": "max"},
            "input": [{"role": "user", "content": "Hello"}],
            "max_output_tokens": 100,
        }
    )
    assert payload["model"] == MODEL
    assert payload["reasoning"] == {"effort": "max"}
    assert payload["store"] is False
    assert "max_output_tokens" not in payload


def test_static_metadata_does_not_grant_codex_entitlement(monkeypatch):
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200, json={"models": [{"slug": MODEL, "visibility": "hide"}]}
            )
        )
    )
    assert adapter.list_models() == ()


@pytest.mark.parametrize(
    "catalog",
    [
        [{"slug": "gpt-6-sol"}, {"slug": MODEL, "visibility": "hide"}],
        [{"slug": "gpt-6-sol"}, {"slug": MODEL, "visibility": "hidden"}],
        [{"slug": "gpt-6-luna"}],
        [{"slug": "gpt-6-sol", "visibility": "hide"}],
        [],
    ],
)
def test_codex_rollout_fallback_respects_hidden_models_and_luna_only_catalogs(monkeypatch, catalog):
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"models": catalog}))
    )
    assert MODEL not in [m.id for m in adapter.list_models()]


def test_codex_catalog_omission_still_allows_sol_selection_and_authenticated_request(monkeypatch):
    requests = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer subscription-test"
        if request.method == "GET":
            return httpx.Response(200, json={"models": [{"slug": "gpt-6-sol", "priority": 3}]})
        payload = json.loads(request.content)
        requests.append(payload)
        assert request.url.path == "/v1/responses"
        assert payload["model"] == MODEL
        assert payload["reasoning"]["effort"] == "low"
        assert payload["store"] is False
        assert "max_output_tokens" not in payload
        response = {
            "status": "completed",
            "model": MODEL,
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "OK"}],
                }
            ],
        }
        return httpx.Response(
            200,
            text="data: "
            + json.dumps({"type": "response.completed", "response": response})
            + "\n\n",
            headers={"content-type": "text/event-stream"},
        )

    adapter = OpenAICodexSubscriptionAuth(transport=httpx.MockTransport(handler))
    monkeypatch.setattr("alysis_code.provider_auth.create_provider_auth", lambda _: adapter)
    profile = ProfileSpec(
        name="chatgpt-codex",
        protocol="openai_responses",
        base_url=adapter.base_url,
        auth_provider="openai-codex",
        default_model="gpt-6-sol",
        reasoning_effort="medium",
    )
    cfg = AppConfig(model=profile.default_model)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert [r.value for r in flow.screen().rows] == [
        MODEL,
        "gpt-6-astra",
        "gpt-6-luna",
        "gpt-6-sol",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-5.5",
    ]
    assert "access checked when used" in next(
        r.description for r in flow.screen().rows if r.value == MODEL
    )
    flow.choose(MODEL)
    flow.choose("low")
    assert flow.state.commit_to(cfg).saved
    assert cfg.model == MODEL
    assert ModelRegistry(cfg=cfg).get(MODEL).context_window_tokens == 272_000
    client = make_llm_client(
        cfg=cfg, api_key="", model=cfg.model, transport=httpx.MockTransport(handler)
    )
    result = client.chat(messages=[{"role": "user", "content": "Reply OK"}])
    assert result.content == "OK"
    assert len(requests) == 1


def test_codex_live_sol_metadata_takes_precedence_without_duplicate(monkeypatch):
    adapter = OpenAICodexSubscriptionAuth(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200,
                json={
                    "models": [
                        {"slug": "gpt-6-sol"},
                        {"slug": MODEL, "description": "Live Sol", "context_window": 300000},
                    ]
                },
            )
        )
    )
    models = [m for m in adapter.list_models() if m.id == MODEL]
    assert len(models) == 1
    assert models[0].description == "Live Sol"
    assert models[0].context_window_tokens == 300_000


@pytest.mark.parametrize(
    "settings",
    [
        {"reasoning_effort": "none"},
        {"reasoning_effort": "minimal"},
        {"reasoning_effort": "ultra"},
        {"enable_thinking": False},
    ],
)
def test_sol_rejects_unsupported_efforts_before_sending(settings):
    from alysis_code.llm.types import LLMError

    client = OpenAIResponsesClient(
        base_url="https://api.openai.com/v1",
        model=MODEL,
        api_key="test-key",
        transport=httpx.MockTransport(lambda _: pytest.fail("Invalid request sent")),
        **settings,
    )
    with pytest.raises(LLMError, match="GPT-6.1 Sol requires"):
        client.chat(messages=[{"role": "user", "content": "Hi"}])
