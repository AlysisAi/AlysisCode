"""Plan presentation shares the hosted route contract and server conversion rates."""

import copy
import json
import urllib.error
from pathlib import Path

import pytest

from alysis_code import account_login
from alysis_code.config import load_config
from alysis_code.model_reasoning import hosted_model_capabilities

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("index,count", [(0, 6), (1, 13)])
def test_paid_plan_json_and_terminal_show_all_models_with_verified_efforts(index, count):
    catalog = json.loads((FIXTURES / "paid-launch-catalog.json").read_text(encoding="utf-8"))
    raw = catalog["plans"][index]
    raw.update(burn_5h_microusd=120000, burn_day_microusd=340000)
    paid = account_login._account_paid(raw)
    assert len(paid["models"]) == count
    for source, model in zip(raw["models"], paid["models"], strict=True):
        assert model["credit_conversion_multiplier"] == source["credit_conversion_multiplier"]
        assert model["value_multiplier"] == source["value_multiplier"]
        assert model["usage_allowance_microusd"] == source["usage_allowance_microusd"]
        assert (
            model["reasoning_efforts"]
            == hosted_model_capabilities(model["model"])["reasoning_efforts"]
        )
    assert next(m for m in paid["models"] if m["model"] == "reflection-beam")[
        "reasoning_efforts"
    ] == [{"id": "low", "label": "Low", "description": ""}]
    status = account_login.TrialStatus(
        plan=paid["plan"],
        email=None,
        trial_ends_at=None,
        tokens_total=None,
        tokens_used=None,
        tokens_remaining=None,
        billing={"paid": paid},
    )
    output = "\n".join(account_login.format_paid_plan_details(status))
    assert f"{count} included models" in output
    assert "Switching models uses the same credit balance." in output
    assert "Effort: Auto, None, Low, Medium, High, Extra high, Max" in output
    for model in paid["models"]:
        assert f"  {model['model']}: " in output


def test_legacy_rates_do_not_invent_conversion_and_unknown_efforts_are_not_guessed():
    catalog = json.loads((FIXTURES / "paid-launch-catalog.json").read_text(encoding="utf-8"))
    raw = copy.deepcopy(catalog["plans"][0])
    raw.update(burn_5h_microusd=0, burn_day_microusd=0)
    for row in raw["models"]:
        row.pop("credit_conversion_multiplier", None)
        row.pop("multiplier", None)
    paid = account_login._account_paid(raw)
    assert all("credit_conversion_multiplier" not in row for row in paid["models"])
    assert hosted_model_capabilities("future-unverified-model")["reasoning_efforts"] == []


@pytest.mark.parametrize(
    "index,name,base,maximum", [(0, "Beta", 100, 500), (1, "Alpha", 500, 2000)]
)
def test_normalized_base_credits_keep_the_shared_usage_value(index, name, base, maximum):
    catalog = json.loads((FIXTURES / "paid-launch-catalog.json").read_text(encoding="utf-8"))
    raw = copy.deepcopy(catalog["plans"][index])
    raw.update(
        plan_name=name,
        base_monthly_credits_microusd=base * 10000,
        ledger_microusd_per_base_credit=maximum * 10000 // base,
        credit_unit_version="paid-base-display-v1",
        burn_5h_microusd=0,
        burn_day_microusd=0,
    )
    paid = account_login._account_paid(raw)
    assert paid["plan_name"] == name
    assert paid["monthly_credits_microusd"] == maximum * 10000
    assert paid["base_monthly_credits_microusd"] == base * 10000
    assert paid["max_usage_value_microusd"] == maximum * 10000
    status = account_login.TrialStatus(
        plan=paid["plan"],
        email=None,
        trial_ends_at=None,
        tokens_total=None,
        tokens_used=None,
        tokens_remaining=None,
        billing={"paid": paid},
    )
    output = "\n".join(account_login.format_paid_plan_details(status))
    assert f"{base:,} monthly shared credits" in output
    assert f"Up to {maximum:,} usage credits, depending on the model." in output
    assert "Model value:" in output
    raw["max_usage_value_microusd"] = True
    raw["models"][0]["value_multiplier"] = True
    invalid = account_login._account_paid(raw)
    assert "max_usage_value_microusd" not in invalid
    assert "value_multiplier" not in invalid["models"][0]
    assert invalid["monthly_credits_microusd"] == maximum * 10000
    raw["ledger_microusd_per_base_credit"] = 1
    assert "ledger_microusd_per_base_credit" not in account_login._account_paid(raw)


@pytest.mark.parametrize(
    "index,name,base,maximum,count", [(0, "Beta", 100, 500, 8), (1, "Alpha", 500, 2000, 15)]
)
def test_current_managed_catalog_preserves_base_units_and_claude_access(
    index, name, base, maximum, count
):
    catalog = json.loads(
        (FIXTURES / "paid-current-catalog-20261029.json").read_text(encoding="utf-8")
    )
    raw = copy.deepcopy(catalog["plans"][index])
    raw.update(burn_5h_microusd=0, burn_day_microusd=0)
    paid = account_login._account_paid(raw)
    assert paid["plan_name"] == name
    assert paid["monthly_credits_microusd"] == maximum * 10000
    assert paid["base_monthly_credits_microusd"] == base * 10000
    assert len(paid["models"]) == count
    assert any(m["model"] == "claude-sonnet-5-5" for m in paid["models"]) == (index == 1)
    for model in paid["models"]:
        assert model["usage_allowance_microusd"] / 10000 == base * model["value_multiplier"]
    status = account_login.TrialStatus(
        plan=paid["plan"],
        email=None,
        trial_ends_at=None,
        tokens_total=None,
        tokens_used=None,
        tokens_remaining=None,
        billing={"paid": paid, "balance_microusd": maximum * 10000, "unlimited_credits": False},
    )
    assert account_login.format_trial_status_line(status) == f"{name}: {base:,.1f} credits left"
    output = "\n".join(account_login.format_paid_plan_details(status))
    assert f"{base:,} monthly shared credits" in output
    assert f"Up to {maximum:,} usage credits" in output
    assert "claude-haiku-5-5" in output


def test_unavailable_members_keep_their_public_hold_status_in_json_and_terminal():
    catalog = json.loads(
        (FIXTURES / "paid-current-catalog-20261029.json").read_text(encoding="utf-8")
    )
    raw = copy.deepcopy(catalog["plans"][0])
    raw.update(burn_5h_microusd=0, burn_day_microusd=0)
    model = next(row for row in raw["models"] if row["model"] == "qwen3.8-flash")
    model.update(available=False, availability_reason="Provider connection pending")
    paid = account_login._account_paid(raw)
    parsed = next(row for row in paid["models"] if row["model"] == "qwen3.8-flash")
    assert parsed["available"] is False
    assert parsed["availability_reason"] == "Provider connection pending"
    status = account_login.TrialStatus(
        plan=paid["plan"],
        email=None,
        trial_ends_at=None,
        tokens_total=None,
        tokens_used=None,
        tokens_remaining=None,
        billing={"paid": paid},
    )
    assert "Unavailable: Provider connection pending" in "\n".join(
        account_login.format_paid_plan_details(status)
    )


def test_app_catalog_intersects_authenticated_routes_and_fails_closed_without_losing_the_balance(
    monkeypatch,
):
    catalog = json.loads(
        (FIXTURES / "paid-current-catalog-20261029.json").read_text(encoding="utf-8")
    )
    raw = copy.deepcopy(catalog["plans"][0])
    raw.update(burn_5h_microusd=0, burn_day_microusd=0)
    paid = account_login._account_paid(raw)
    billing = {"paid": paid, "balance_microusd": 5000000}
    original = copy.deepcopy(billing)
    allowed = paid["models"][0]["model"]
    monkeypatch.setattr(
        account_login, "load_persisted_profile_keys", lambda: {"alysis": "slk_synthetic"}
    )
    monkeypatch.setattr(
        account_login.cloud, "models_url", lambda: "https://models.example.invalid/v1/models"
    )

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, limit):
            return json.dumps({"data": [{"id": allowed}, {"id": "outside-the-plan"}]}).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer slk_synthetic"
            return Response()

    monkeypatch.setattr(account_login.urllib.request, "build_opener", lambda *args: Opener())
    result = account_login.billing_with_model_availability(load_config(), billing)
    assert result["balance_microusd"] == 5000000
    assert [m["model"] for m in result["paid"]["models"] if m["available"]] == [allowed]
    assert billing == original

    class Offline:
        def open(self, *args, **kwargs):
            raise urllib.error.URLError("offline")

    monkeypatch.setattr(account_login.urllib.request, "build_opener", lambda *args: Offline())
    offline = account_login.billing_with_model_availability(load_config(), billing)
    assert offline["balance_microusd"] == 5000000
    assert all(m["available"] is False for m in offline["paid"]["models"])
    for old, model in zip(original["paid"]["models"], offline["paid"]["models"], strict=True):
        assert (
            model["availability_reason"] == old["availability_reason"]
            if old.get("available") is False
            else "Retry" in model["availability_reason"]
        )


@pytest.mark.parametrize(
    "value", [10**400, -(10**400), True, False, float("nan"), float("inf"), -float("inf"), 1.5]
)
def test_invalid_paid_numbers_preserve_the_base_billing_balance(value):
    raw = json.loads((FIXTURES / "account-status-paid.json").read_text(encoding="utf-8"))["billing"]
    balance = raw["balance_microusd"]
    raw["paid"]["monthly_credits_microusd"] = value
    parsed = account_login._account_billing(raw)
    assert parsed is not None
    assert parsed["balance_microusd"] == balance
    assert "paid" not in parsed
    assert account_login._account_int(value) is False


@pytest.mark.parametrize("value", [0, 2**53 - 1, -(2**53 - 1), 42.0])
def test_json_safe_whole_numbers_stay_valid(value):
    result = account_login._account_int(value)
    assert type(result) is int
    assert result == value


@pytest.mark.parametrize("cap", [-1, -(10**400), True, 1.5])
def test_invalid_model_input_cap_drops_paid_details_without_hiding_balance(cap):
    raw = json.loads((FIXTURES / "account-status-paid.json").read_text(encoding="utf-8"))["billing"]
    raw["paid"]["models"][0]["max_input_tokens"] = cap
    parsed = account_login._account_billing(raw)
    assert parsed["balance_microusd"] == raw["balance_microusd"]
    assert "paid" not in parsed


@pytest.mark.parametrize("cap", [None, 0, 100000])
def test_nonnegative_optional_model_input_caps_match_the_app(cap):
    raw = json.loads((FIXTURES / "account-status-paid.json").read_text(encoding="utf-8"))["billing"]
    raw["paid"]["models"][0]["max_input_tokens"] = cap
    assert account_login._account_billing(raw)["paid"]["models"][0]["max_input_tokens"] == cap
