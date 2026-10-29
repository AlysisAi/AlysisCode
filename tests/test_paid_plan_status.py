"""Paid plans (Plan 1 / Plan 5) in the account status and hosted error display.

The fixture is the real SQL output of get_device_account_status for a Plan 5
account (generated from supabase/migrations/202610210002_paid_plans.sql).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import httpx
import pytest

from alysis_code import account_login
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm_error_display import friendly_llm_error_message

PAID = json.loads(
    (Path(__file__).parent / "fixtures/account-status-paid.json").read_text(encoding="utf-8")
)
FREE = json.loads(
    (Path(__file__).parent / "fixtures/account-status.json").read_text(encoding="utf-8")
)


def _status(billing: dict) -> account_login.TrialStatus:
    parsed = account_login._account_billing(billing)
    assert parsed is not None
    return account_login.TrialStatus(
        plan=parsed["tier"],
        email=None,
        trial_ends_at=None,
        tokens_total=None,
        tokens_used=None,
        tokens_remaining=None,
        display_name=None,
        billing=parsed,
    )


def test_paid_section_is_parsed_from_the_server_contract():
    parsed = account_login._account_billing(PAID["billing"])
    assert parsed is not None
    assert parsed["tier"] == "plan_5"
    assert parsed["credits_reset_at"].startswith("2026-11-21")
    paid = parsed["paid"]
    assert paid["plan_name"] == "Plan 5"
    assert paid["monthly_credits_microusd"] == 20_000_000
    assert paid["premium_month_limit_microusd"] == 10_000_000
    sonnet = next(m for m in paid["models"] if m["model"] == "claude-sonnet-5-5")
    assert sonnet["microusd_per_million"] == {
        "input": 6_500_000,
        "cached_input": 325_000,
        "cache_write": 8_125_000,
        "output": 32_500_000,
    }


def test_free_contract_is_unchanged():
    parsed = account_login._account_billing(FREE["billing"])
    assert parsed is not None
    assert "paid" not in parsed and "credits_reset_at" not in parsed


def test_a_malformed_paid_section_never_hides_the_balance():
    billing = copy.deepcopy(PAID["billing"])
    billing["paid"]["models"][0]["microusd_per_million"]["input"] = -1
    parsed = account_login._account_billing(billing)
    assert parsed is not None and "paid" not in parsed
    assert parsed["balance_microusd"] == PAID["billing"]["balance_microusd"]


def test_whoami_lines_show_credits_reset_date_windows_and_rates():
    status = _status(PAID["billing"])
    assert account_login.format_trial_status_line(status) == (
        "Plan 5: 1,876.6 credits left · resets 2026-11-21"
    )
    details = account_login.format_paid_plan_details(status)
    assert details[0] == "Last 5 hours: 123 of 350 credits · last 24 hours: 123 of 600"
    assert details[1] == "Premium models this month: 0 of 1,000 credits · last 24 hours: 0 of 200"
    assert "  claude-sonnet-5-5: 650 / 32.5 / 3250" in details
    assert "  claude-haiku-5-5: 27.5 / 2.75 / 137.5 · up to 100,000 input tokens" in details
    assert not any("alysis upgrade" in line for line in details)


def test_plan_1_offers_the_upgrade():
    billing = copy.deepcopy(PAID["billing"])
    billing["tier"] = "plan_1"
    billing["paid"].update(
        plan="plan_1",
        plan_name="Plan 1",
        upgrade_to="plan_5",
        premium_month_limit_microusd=None,
        premium_day_limit_microusd=None,
    )
    details = account_login.format_paid_plan_details(_status(billing))
    assert details[-1] == "Need more? Run `alysis upgrade` to review available plans."
    assert not any(line.startswith("Premium") for line in details)


@pytest.mark.parametrize(
    "status, code, message",
    [
        (
            402,
            "plan_credits_exhausted",
            "You've used this month's Plan 1 credits. Upgrade to Plan 5 now with `alysis upgrade` "
            "(or at alysiscode.com/account), or wait for your credits to reset on 2026-11-21.",
        ),
        (
            429,
            "plan_limit_reached",
            "Plan 1 allows 100 credits per 5 hours. Retry after 14:05 UTC. No credits were charged.",
        ),
        (
            403,
            "model_not_allowed",
            "This model is not included in Plan 1. It is available on Plan 5: run `alysis upgrade` "
            "or visit alysiscode.com/account.",
        ),
        (
            402,
            "plan_credits_exhausted",
            "This request could use up to 43 credits and you have 3 left. Compact the conversation "
            "or lower the output limit, or upgrade with `alysis upgrade`. No credits were charged.",
        ),
        (
            403,
            "plan_unavailable",
            "Your plan needs attention before hosted models can be used. "
            "Contact products@alysisai.com. No credits were charged.",
        ),
        (
            503,
            "hosted_model_paused",
            "Claude Haiku 5.5 is paused for about 60 minutes: its provider account reached a "
            "spending limit. Choose another model with /model meanwhile. No generation was "
            "started and no credits were charged.",
        ),
        (
            403,
            "model_not_allowed",
            "DeepSeek models are no longer offered on Alysis hosted plans. Choose GLM 5.3 Flash "
            "(glm-5.3-flash), GPT-6 Luna (gpt-6-luna) or Claude Haiku 5.5 (claude-haiku-5-5) "
            "with /model, and update Alysis Code to get the new default. No credits were charged.",
        ),
    ],
)
def test_paid_plan_errors_are_shown_as_the_gateway_wrote_them(status, code, message):
    error = OpenAICompatClient._error_from_response(
        httpx.Response(status, json={"error": {"code": code, "message": message, "type": code}})
    )
    rendered = friendly_llm_error_message(error)
    assert message in rendered
    assert "API key was rejected" not in rendered
