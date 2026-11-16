"""Pro account units and every advertised TUI effort survive save/reload and transport."""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from alysis_code import account_login
from alysis_code.cli import app
from alysis_code.cli_impl import config_menu
from alysis_code.cli_impl.tui.config_flow import ConfigFlow
from alysis_code.config import AppConfig, load_config, save_persisted_profile_key
from alysis_code.llm.factory import make_llm_client
from alysis_code.model_reasoning import hosted_model_capabilities, model_reasoning_capabilities
from alysis_code.profile_presets import get_preset, make_profile_from_preset, model_display_name
from alysis_code.profiles import add_profile, set_active_profile

FIXTURES = Path(__file__).parent / "fixtures"
PRO = json.loads((FIXTURES / "pro-plan-catalog.json").read_text())
EFFORTS = {
    "glm-5.3-flash": ("low", "high", "max"),
    "deepseek-v4.1-flash": ("none", "low", "high", "max"),
    "minimax-m3.1-flash": ("low", "medium", "high", "xhigh", "max"),
    "claude-haiku-5-5": ("none", "low", "medium", "high", "xhigh", "max"),
    "reflection-beam": ("low", "medium", "high", "xhigh", "max"),
    "nemotron-3.5-super-vl": ("low", "medium", "high"),
    "gpt-6-luna": ("none", "low", "medium", "high", "xhigh", "max"),
    "gpt-6.1-terra": ("none", "low", "medium", "high", "xhigh", "max"),
    "kimi-k3": ("low",),
    "mistral-large-4": ("low", "medium", "high"),
    "glm-5.3": ("low", "high", "max"),
    "deepseek-v4-pro": ("none", "low", "high", "max"),
    "gemini-4-carbon": ("low", "medium", "high"),
    "gpt-6.1-sol": ("low", "medium", "high", "xhigh", "max"),
    "claude-sonnet-5-5": ("between_tools", "low", "medium", "high", "xhigh", "max"),
    "kimi-k3.1-pro": ("low",),
}


def test_full_screen_pro_picker_saves_reduced_sonnet_thinking(monkeypatch):
    import threading

    from test_tui_config import _FakeSession, _run_live_headless

    from alysis_code.cli_impl.tui.app import ConfigReloadOutcome

    model = "claude-sonnet-5-5"
    profile = replace(make_profile_from_preset(get_preset("alysis")), default_model=model)
    cfg = AppConfig(model=model, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    save_persisted_profile_key("alysis", "synthetic-test-only")
    monkeypatch.setattr(
        config_menu,
        "discover_provider_models",
        lambda **kwargs: tuple(
            config_menu.ProviderModelOption(id=m, label=model_display_name(m)) for m in EFFORTS
        ),
    )
    opened, reloaded = threading.Event(), threading.Event()
    flows = []

    def factory():
        flow = ConfigFlow(cfg=cfg)
        flow.choose("default")
        flow.choose(model)
        assert flow.screen().rows[0].label == "Between tools"
        flows.append(flow)
        opened.set()
        return flow

    def saved():
        current = load_config()
        assert current.model == model
        assert current.llm_reasoning_effort == "none"
        assert "Between tools" in config_menu._default_model_summary_text(flows[0].state)
        reloaded.set()
        return ConfigReloadOutcome.APPLIED

    def feed(pipe):
        assert opened.wait(5)
        pipe.send_text("1")
        pipe.send_text("s")
        assert reloaded.wait(5)
        pipe.send_text("/exit\r")

    result, _ = _run_live_headless(
        feed,
        config_flow_factory=factory,
        open_config_on_start=True,
        on_config_saved=saved,
        session_builder=_FakeSession,
        command_runner=lambda _s, text, _w: ("exit", "", None, None),
        background_turns=False,
    )
    assert result == "/exit"
    assert flows[0].saved


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_GATEWAY_URL", "https://backend.example.invalid/functions/v1/llm/v1")
    monkeypatch.setenv("ALYSIS_CLOUD_SITE_URL", "https://site.example.invalid")
    for key in ("ALYSIS_API_KEY", "OPENAI_API_KEY", "ALYSIS_RUN_REASONING_EFFORT"):
        monkeypatch.delenv(key, raising=False)


@pytest.mark.parametrize("plan", ["pro", "Pro", "plan_15", "alpha", "beta"])
def test_upgrade_opens_website_without_payment_or_local_entitlement(monkeypatch, plan):
    opened = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    result = CliRunner().invoke(app, ["upgrade", plan])
    assert result.exit_code == 0, result.output
    expected = {"alpha": "plan_5", "beta": "plan_1"}.get(plan, "plan_15")
    assert opened[0].endswith("?upgrade=" + expected)
    assert "Payment happens on the website" in result.output


def test_pro_status_uses_shared_base_units_and_separate_pending_cloud():
    raw = json.loads((FIXTURES / "account-status-paid.json").read_text())["billing"]
    paid = {**PRO, "burn_5h_microusd": 0, "burn_7d_microusd": 0}
    raw.update(paid=paid, tier="plan_15", balance_microusd=60_000_000)
    parsed = account_login._account_billing(raw)
    assert parsed["paid"]["cloud"] == PRO["cloud"]
    status = account_login.TrialStatus(
        plan="plan_15",
        email=None,
        trial_ends_at=None,
        tokens_total=None,
        tokens_used=None,
        tokens_remaining=None,
        billing=parsed,
    )
    assert account_login.format_trial_status_line(status).startswith("Pro: 1,500.0 credits left")
    text = "\n".join(account_login.format_paid_plan_details(status, detailed=True))
    assert "1,500 shared credits every 30 days" in text
    assert "0 of 300 credits · last 7 days: 0 of 600" in text
    assert "200 cloud credits included in a separate balance" in text
    assert "Cloud charging and bot limits are pending" in text
    assert "not separate model balances" in text
    for row in PRO["models"]:
        assert model_display_name(row["model"]) in text
        assert f"up to {row['usage_allowance_microusd'] / 10_000:,.0f}" in text
    assert set(EFFORTS) == {row["model"] for row in PRO["models"]}


@pytest.mark.parametrize(
    "model,effort", [(m, e) for m, es in EFFORTS.items() for e in (*es, "auto")]
)
def test_pro_tui_catalog_effort_save_reload_and_wire(monkeypatch, model, effort):
    profile = replace(make_profile_from_preset(get_preset("alysis")), default_model=model)
    cfg = AppConfig(model=model, base_url=profile.base_url)
    add_profile(cfg, profile)
    set_active_profile(cfg, profile.name)
    save_persisted_profile_key("alysis", "synthetic-test-only")
    monkeypatch.setattr(
        config_menu,
        "discover_provider_models",
        lambda **kwargs: tuple(
            config_menu.ProviderModelOption(id=m, label=model_display_name(m)) for m in EFFORTS
        ),
    )
    flow = ConfigFlow(cfg=cfg)
    flow.choose("default")
    assert {r.value for r in flow.screen().rows} == set(EFFORTS)
    flow.choose(model)
    if flow.stage == "model_base_url":
        flow.submit_input("")
    expected = {"off" if e in {"none", "between_tools"} else e for e in EFFORTS[model]} | {"auto"}
    assert {r.value for r in flow.screen().rows} == expected
    if model == "claude-sonnet-5-5":
        reduced = next(r for r in flow.screen().rows if r.value == "off")
        assert reduced.label == "Between tools"
        assert "thinking between tools stays active" in reduced.description
    selected = "off" if effort in {"none", "between_tools"} else effort
    flow.choose(selected)
    flow.choose("__save__")
    flow.run_busy()
    assert flow.saved, flow.status
    saved = load_config()
    assert saved.model == model
    caps = model_reasoning_capabilities(saved)
    assert tuple(r["id"] for r in caps["reasoning_efforts"]) == EFFORTS[model]
    assert caps["reasoning_efforts"] == hosted_model_capabilities(model)["reasoning_efforts"]
    reopened = ConfigFlow(cfg=saved)
    reopened.choose("default")
    reopened.choose(model)
    if reopened.stage == "model_base_url":
        reopened.submit_input("")
    assert next(r for r in reopened.screen().rows if r.value == selected).current
    sent = []

    def response(request):
        sent.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "OK"}}],
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "OK"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1},
                "status": "completed",
            },
        )

    client = make_llm_client(
        cfg=saved, api_key="synthetic", model=model, transport=httpx.MockTransport(response)
    )
    assert (
        client.chat(messages=[{"role": "user", "content": "fixture"}], max_tokens=8192).content
        == "OK"
    )
    body = sent[-1]
    assert body["model"] == model
    if effort != "auto":
        if model.startswith("gpt-"):
            assert body["reasoning"]["effort"] == effort
        elif model.startswith("claude-"):
            if effort == "between_tools":
                assert body["thinking"] == {"type": "between_tools"}
                assert body["output_config"]["effort"] == "high"
            elif effort == "none":
                assert body["thinking"] == {"type": "disabled"}
            else:
                assert body["output_config"]["effort"] == effort
        elif effort == "none":
            assert body["thinking"] == {"type": "disabled"}
        else:
            assert body["reasoning_effort"] == effort
