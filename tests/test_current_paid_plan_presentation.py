"""Current live paid contract, including retired daily windows and readable model rows."""

import copy
import json
from pathlib import Path
from urllib.error import HTTPError

import pytest
from click import unstyle
from typer.testing import CliRunner

from alysis_code import account_login
from alysis_code.cli import app

FIXTURES = Path(__file__).parent / "fixtures"
CATALOG = json.loads((FIXTURES / "paid-live-catalog-20261030.json").read_text(encoding="utf-8"))


def billing(index=0):
    result = json.loads((FIXTURES / "account-status-paid.json").read_text(encoding="utf-8"))[
        "billing"
    ]
    paid = copy.deepcopy(CATALOG["plans"][index])
    paid.update(burn_5h_microusd=0, burn_7d_microusd=0, burn_day_microusd=None)
    result.update(
        paid=paid,
        tier=paid["plan"],
        balance_microusd=paid["monthly_credits_microusd"],
        cap_5h_microusd=paid["cap_5h_microusd"],
        cap_7d_microusd=paid["cap_7d_microusd"],
    )
    return result


def status(index=0):
    parsed = account_login._account_billing(billing(index))
    assert parsed is not None and "paid" in parsed
    return account_login.TrialStatus(
        plan=parsed["tier"],
        email=None,
        trial_ends_at=None,
        tokens_total=None,
        tokens_used=None,
        tokens_remaining=None,
        billing=parsed,
    )


@pytest.mark.parametrize(
    "index,name,count,base,five,week",
    [(0, "Beta", 7, 100, 20, 40), (1, "Alpha", 12, 500, 100, 200)],
)
def test_current_contract_has_readable_models_base_units_and_real_windows(
    index, name, count, base, five, week
):
    current = status(index)
    paid = current.billing["paid"]
    assert paid["plan_name"] == name
    assert len(paid["models"]) == count
    assert "cap_day_microusd" not in paid
    assert paid["credit_period_days"] == 30
    assert account_login.format_trial_status_line(current).startswith(
        f"{name}: {base:,.1f} credits left"
    )
    simple = "\n".join(account_login.format_paid_plan_details(current))
    assert f"{base} shared credits every 30 days" in simple
    assert f"Last 5 hours: 0 of {five} credits · last 7 days: 0 of {week}" in simple
    assert f"{count} models · {count} available" in simple
    assert "GPT-6 Luna" in simple and "Claude Haiku 5.5" in simple
    assert "Unavailable" not in simple
    assert "not separate model balances" in simple
    for old in [
        "last 24 hours",
        "monthly shared credits",
        "Credit rate:",
        "claude-sonnet",
        "gpt-6.1-sol",
        "qwen3.8",
        "ID:",
    ]:
        assert old not in simple
    if index:
        assert "GPT-6.1 Terra" in simple
    detailed = "\n".join(account_login.format_paid_plan_details(current, detailed=True))
    for row in paid["models"]:
        assert f"ID: {row['model']}" in detailed
        assert row["label"] in simple
    assert "Credits per 1M tokens" in detailed and "Effort: Auto" in detailed


@pytest.mark.parametrize(
    "field,value",
    [
        ("cap_7d_microusd", -1),
        ("burn_7d_microusd", None),
        ("cap_day_microusd", 1),
        ("credit_period_days", 0),
        ("credit_period_days", True),
    ],
)
def test_malformed_optional_windows_do_not_hide_account_balance(field, value):
    raw = billing()
    raw["paid"][field] = value
    parsed = account_login._account_billing(raw)
    assert parsed["balance_microusd"] == raw["balance_microusd"]
    assert "paid" not in parsed


def test_whoami_exposes_optional_details_without_network_or_login():
    result = CliRunner().invoke(app, ["whoami", "--help"])
    assert result.exit_code == 0, result.output
    assert "--details" in unstyle(result.output)


@pytest.mark.parametrize("index,count", [(0, 7), (1, 12)])
def test_terminal_and_json_whoami_close_model_availability_after_gateway_denial(
    monkeypatch, index, count
):
    from alysis_code import cli
    from alysis_code.config import AppConfig

    current = status(index)
    monkeypatch.setattr(cli, "load_config", lambda: AppConfig())
    monkeypatch.setattr(
        account_login,
        "login_status",
        lambda _cfg: account_login.LoginStatus(
            True, "alysis", "https://example.invalid/v1", True, "test"
        ),
    )
    monkeypatch.setattr(account_login, "fetch_trial_status", lambda _cfg: current)
    monkeypatch.setattr(
        account_login, "load_persisted_profile_keys", lambda: {"alysis": "synthetic"}
    )

    class DeniedGateway:
        def open(self, request, timeout):
            assert request.get_method() == "GET"
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(
        account_login.urllib.request, "build_opener", lambda *_args: DeniedGateway()
    )
    terminal = CliRunner().invoke(app, ["whoami"], terminal_width=180)
    assert terminal.exit_code == 0, terminal.output
    assert f"{count} models · 0 available" in terminal.output
    assert terminal.output.count("Unavailable") == count
    result = CliRunner().invoke(app, ["whoami", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert not any(model["available"] for model in payload["billing"]["paid"]["models"])
    assert payload["billing"]["balance_microusd"] == current.billing["balance_microusd"]


def test_empty_or_unknown_catalog_names_never_invent_or_break_a_route():
    current = status()
    current.billing["paid"]["models"] = []
    assert "0 models · 0 available" in "\n".join(account_login.format_paid_plan_details(current))
    raw = billing()
    raw["paid"]["models"][0]["model"] = "future-model-id"
    parsed = account_login._account_billing(raw)
    assert parsed["paid"]["models"][0]["label"] == "future-model-id"
