"""Paid CLI availability and Mistral Large 4 settings reach the intended request."""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from alysis_code.cli import app
from alysis_code.cli_impl import config_menu
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import (
    AppConfig,
    load_config,
    save_config,
    save_persisted_profile_key,
)
from alysis_code.llm.factory import make_llm_client
from alysis_code.llm.openai_compat import _transport_provider_key
from alysis_code.model_reasoning import (
    hosted_model_capabilities,
    invocation_reasoning_effort,
    model_reasoning_capabilities,
)
from alysis_code.model_registry import ModelRegistry, catalog_model_for_provider
from alysis_code.profile_presets import (
    get_preset,
    make_profile_from_preset,
    model_options_for_preset,
)
from alysis_code.profiles import add_profile, set_active_profile
from alysis_code.provider_auth.base import ProviderAuthError
from alysis_code.reasoning_contracts import reasoning_contract_for

FIXTURES = Path(__file__).parent / "fixtures"
MODEL = "mistral-large-4"
GATEWAY = "https://backend.example.invalid/functions/v1/llm/v1"


@pytest.fixture(autouse=True)
def disposable_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", GATEWAY)
    for key in (
        "ALYSIS_API_KEY",
        "OPENAI_API_KEY",
        "MISTRAL_API_KEY",
        "ALYSIS_RUN_REASONING_EFFORT",
    ):
        monkeypatch.delenv(key, raising=False)


def config(preset_key, *, effort=None):
    profile = replace(make_profile_from_preset(get_preset(preset_key)), default_model=MODEL)
    cfg = AppConfig(model=MODEL, base_url=profile.base_url, llm_reasoning_effort=effort)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    return cfg


@pytest.mark.parametrize("catalog_status", [200, 401])
def test_terminal_account_availability_matches_json(monkeypatch, catalog_status):
    catalog = json.loads((FIXTURES / "paid-live-catalog-20261030.json").read_text())["plans"][0]
    billing = json.loads((FIXTURES / "account-status-paid.json").read_text())["billing"]
    billing["paid"] = copy.deepcopy(catalog)
    billing["paid"].update(burn_5h_microusd=0, burn_7d_microusd=0)
    billing.update(tier="plan_1", balance_microusd=catalog["monthly_credits_microusd"])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):  # noqa: N802
            models = self.path.endswith("/models")
            self.send_response(catalog_status if models else 200)
            self.end_headers()
            body = (
                {"data": [{"id": row["model"]} for row in catalog["models"]]}
                if models
                else {"schema_version": 1, "billing": billing}
            )
            self.wfile.write(json.dumps(body).encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("ALYSIS_SUPABASE_URL", base)
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", base + "/v1")
    save_config(config("alysis"))
    save_persisted_profile_key("alysis", "synthetic-disposable-test-only")
    try:
        runner = CliRunner()
        result = runner.invoke(app, ["whoami"], terminal_width=160)
        assert result.exit_code == 0, result.exception
        payload = json.loads(runner.invoke(app, ["whoami", "--json"]).stdout)
        count = sum(row["available"] for row in payload["billing"]["paid"]["models"])
        assert count == (len(catalog["models"]) if catalog_status == 200 else 0)
        assert f"{len(catalog['models'])} models · {count} available" in result.stdout
        assert "100.0 credits left" in result.stdout
        assert "synthetic-disposable-test-only" not in result.stdout
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)


def test_mistral_large_4_catalog_and_aliases_have_native_metadata():
    rows = dict((row[0], row[1]) for row in model_options_for_preset(get_preset("mistral")))
    assert rows[MODEL] == "Mistral Large 4 (Preview)"
    assert catalog_model_for_provider("mistral-large-4-0", "mistral") == MODEL
    metadata = ModelRegistry(cfg=config("mistral")).get(MODEL)
    assert metadata.context_window_tokens == 1_000_000
    assert metadata.supports_vision is True
    assert metadata.supports_reasoning is True
    assert metadata.input_cost_per_token == 0.0000007
    # Adding Large 4 must not give older Large models unsupported controls.
    assert reasoning_contract_for("mistral", "mistral-large-2512").values == ()


@pytest.mark.parametrize("preset", ["alysis", "mistral"])
@pytest.mark.parametrize("effort", ["low", "medium", "high"])
def test_mistral_effort_picker_save_reload_and_actual_request(monkeypatch, preset, effort):
    cfg = config(preset, effort="max")  # An inherited unsupported level must disappear.
    monkeypatch.setattr(
        config_menu,
        "discover_provider_models",
        lambda **kwargs: (config_menu.ProviderModelOption(id=MODEL, label=MODEL),),
    )
    save_persisted_profile_key(preset, "synthetic-disposable-test-only")
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert MODEL in {row.value for row in flow.screen().rows}
    flow.choose(MODEL)
    if flow.stage == "model_base_url":
        flow.submit_input("")
    assert {row.value for row in flow.screen().rows} == {"auto", "low", "medium", "high"}
    flow.choose(effort)
    flow.choose("__save__")
    flow.run_busy()
    assert flow.saved, flow.status
    cfg = load_config()
    assert invocation_reasoning_effort(cfg, effort) == effort
    assert [row["id"] for row in model_reasoning_capabilities(cfg)["reasoning_efforts"]] == [
        "low",
        "medium",
        "high",
    ]
    reopened = ConfigFlow(cfg=cfg)
    reopened.choose("default")
    reopened.choose(MODEL)
    if reopened.stage == "model_base_url":
        reopened.submit_input("")
    assert next(row for row in reopened.screen().rows if row.value == effort).current
    sent = []

    def handle(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        )

    client = make_llm_client(
        cfg=cfg, api_key="synthetic-test-only", model=MODEL, transport=httpx.MockTransport(handle)
    )
    assert client.chat(messages=[{"role": "user", "content": "fixture"}]).content == "OK"
    assert sent[0]["reasoning_effort"] == effort
    assert "thinking" not in sent[0]  # Never borrow DeepSeek's toggle for this hosted model.
    assert (
        _transport_provider_key(base_url=cfg.base_url, provider_key=preset, model=MODEL) == preset
    )


@pytest.mark.parametrize("preset", ["alysis", "mistral"])
@pytest.mark.parametrize("invalid", ["none", "minimal", "xhigh", "max", "ultra"])
def test_mistral_rejects_unsupported_efforts_before_request(preset, invalid):
    with pytest.raises(ProviderAuthError, match="not supported"):
        invocation_reasoning_effort(config(preset), invalid)


def test_hosted_mistral_capabilities_and_unknown_models_fail_closed():
    assert [row["id"] for row in hosted_model_capabilities(MODEL)["reasoning_efforts"]] == [
        "low",
        "medium",
        "high",
    ]
    state = ConfigFlow(cfg=config("alysis")).state
    assert config_menu._thinking_labels_for_state(state, model="unverified-future-model") == (
        "auto",
    )
