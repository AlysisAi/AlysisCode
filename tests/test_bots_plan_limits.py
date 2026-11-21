"""An owl stopped by a limit of the person's Alysis plan says which limit and when it frees up.

The hosted gateway answers in fixed sentences written for people; the CLI shows them as sent
and the desktop app words them in the person's language. Before, an owl reported every such
stop as a busy model service or a broken connection.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest

import alysis_code.bots.runtime as runtime_module
from alysis_code.bots.cloud_billing import CloudBilling, CloudCreditsExhausted
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import PROVIDER_INTERRUPTED, BotRuntime
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.openai_compat import (
    OpenAICompatClient,
    alysis_plan_limit_message,
    alysis_trial_error_message,
)
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

GREEK_MESSAGE = "Γράψε μου μια σύντομη περίληψη της ημέρας."


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)


class ScriptedClient:
    model = "test-model"
    supports_tool_calling = True

    def __init__(self, steps):
        self.steps = list(steps)

    def chat(self, **kwargs):
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


def gateway_error(status, code, message, retry_after=None):
    headers = {"Retry-After": str(retry_after)} if retry_after else {}
    return OpenAICompatClient._error_from_response(
        httpx.Response(status, json={"error": {"code": code, "message": message}}, headers=headers)
    )


def run_owl(tmp_path, steps, message=GREEK_MESSAGE):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Ήλιος", purpose="Βοηθά με σημειώσεις"))
    chat = store.create_chat("alice", bot["id"], "Νέα συζήτηση")
    store.enqueue("alice", chat["id"], TaskInput(message=message, request_id="request_1"))
    task = store.claim("alice")
    BotRuntime(store, "alice", lambda _: ScriptedClient(steps)).run(
        task, EventCancellationToken(threading.Event())
    )
    return store, store.task("alice", task["id"])


def utc(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%d %H:%M")


# A rolling window, a paid window with the gateway's own time, and the plan's credits.
WEEKLY = "Weekly fair-use limit reached (your account credit allowance in 7 days). Retry as usage ages out."
PAID_5H = "Beta allows 20 credits per 5 hours. Retry after 14:05 UTC. No credits were charged."
PAID_WEEKLY = (
    "Alpha weekly usage allowance is exhausted. Retry when rolling usage ages out. "
    "No credits were charged."
)
FREE_CREDITS = "Free credits are exhausted. They refill on your 30-day cycle."
PAID_CREDITS = (
    "You've used this month's Beta credits. Upgrade to Alpha now with `alysis upgrade` "
    "(or at alysiscode.com/account), or wait for your credits to reset on 2026-11-21."
)


def test_a_rolling_window_says_when_the_account_lets_requests_through_again():
    error = gateway_error(429, "rate_limit_exceeded", WEEKLY, retry_after=3 * 3600)
    # The gateway's Retry-After is the account's own reset time for the window.
    assert abs(error.provider_reset_at - (time.time() + 3 * 3600)) <= 60
    shown = alysis_trial_error_message(error)
    assert shown == f"{WEEKLY} It frees up at {utc(error.provider_reset_at)} UTC."
    assert alysis_plan_limit_message(error) == shown
    # A paid window whose sentence already states its time, or one with no reset time, is kept as sent.
    paid = gateway_error(429, "plan_limit_reached", PAID_5H, retry_after=600)
    assert alysis_trial_error_message(paid) == PAID_5H
    assert alysis_trial_error_message(gateway_error(429, "rate_limit_exceeded", WEEKLY)) == WEEKLY
    weekly = gateway_error(429, "plan_limit_reached", PAID_WEEKLY, retry_after=7200)
    assert (
        alysis_trial_error_message(weekly)
        == f"{PAID_WEEKLY} It frees up at {utc(weekly.provider_reset_at)} UTC."
    )


def test_only_the_plans_own_limits_count_as_plan_limits():
    assert (
        alysis_plan_limit_message(gateway_error(402, "insufficient_quota", FREE_CREDITS))
        == FREE_CREDITS
    )
    assert (
        alysis_plan_limit_message(gateway_error(402, "plan_credits_exhausted", PAID_CREDITS))
        == PAID_CREDITS
    )
    for error in (
        gateway_error(
            429, "rate_limit_exceeded", "Your account has reached the 20 requests per minute limit."
        ),
        gateway_error(
            429, "rate_limit_exceeded", "Four hosted requests are already running for your account."
        ),
        gateway_error(
            503, "hosted_capacity_exceeded", "Hosted model capacity is busy. Retry in 5 seconds."
        ),
        gateway_error(
            429, "insufficient_quota", "You exceeded your current quota, please check your plan."
        ),
        gateway_error(401, "invalid_api_key", "Invalid or revoked Alysis key. Run `alysis login`."),
        RuntimeError("5-hour fair-use limit reached (copied text is not a gateway answer)"),
    ):
        assert alysis_plan_limit_message(error) is None


@pytest.mark.parametrize(
    "status,code,message",
    [
        (429, "rate_limit_exceeded", WEEKLY),
        (429, "plan_limit_reached", PAID_5H),
        (402, "insufficient_quota", FREE_CREDITS),
        (402, "plan_credits_exhausted", PAID_CREDITS),
    ],
    ids=["free_weekly", "paid_5h", "free_credits", "paid_credits"],
)
def test_an_owl_stopped_by_a_plan_limit_keeps_the_plans_answer(tmp_path, status, code, message):
    # A long wait: the owl does not retry, and the person is told about their plan, not an outage.
    _, task = run_owl(tmp_path, [gateway_error(status, code, message, retry_after=7200)] * 4)
    assert task["status"] == "failed"
    assert task["error"] != PROVIDER_INTERRUPTED
    assert task["error"].startswith(message.split(" (or at")[0])
    assert "Check the model connection" not in task["error"]


def test_other_failures_keep_their_previous_wording(tmp_path):
    busy = gateway_error(
        429,
        "rate_limit_exceeded",
        "Your account has reached its hosted request limit.",
        retry_after=600,
    )
    _, task = run_owl(tmp_path, [busy] * 4)
    assert task["error"] == PROVIDER_INTERRUPTED


def receipt(**changes):
    return dict(
        schema_version=1,
        credits_per_hour=25,
        free_credits=50,
        free_credits_remaining=0,
        shared_credits_remaining=0,
        storage_limit_bytes=3_000_000_000,
        resets_at="2026-12-13T00:00:00+00:00",
        can_run=False,
        **changes,
    )


def test_no_cloud_credits_names_the_reset_for_the_app():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=receipt()))
    ) as client:
        billing = CloudBilling("https://billing.invalid", "secret", client=client)
        account = billing.account(
            "account:" + "0" * 8 + "-0000-4000-8000-" + "0" * 12,
            SimpleNamespace(set_library_limit=lambda *a: None),
        )
        with pytest.raises(CloudCreditsExhausted) as refused:
            account.admit()
    # The wording for the model and the CLI stays; the app reads the closed fields.
    assert "No Cloud computer credits are available." in str(refused.value)
    assert refused.value.refusal == {
        "code": "cloud_credits_exhausted",
        "resets_at": "2026-12-13T00:00:00+00:00",
    }


def test_a_refused_cloud_computer_reaches_the_task_as_a_coded_tool_failure(tmp_path, monkeypatch):
    def refuse(*args, **kwargs):
        raise CloudCreditsExhausted(
            "No Cloud computer credits are available.", "2026-12-13T00:00:00Z"
        )

    monkeypatch.setattr(Store, "save_file", refuse)
    _, task = run_owl(
        tmp_path,
        [
            LLMResponse(
                "",
                [ToolCall("call_1", "save_file", {"name": "σημειώσεις.md", "content": "Γεια"})],
                {},
                usage=LLMUsage(10, 5, 15),
            ),
            LLMResponse("Δεν υπάρχουν μονάδες Cloud.", [], {}, usage=LLMUsage(10, 5, 15)),
        ],
    )
    failed = [event["data"] for event in task["events"] if event["kind"] == "tool_failed"]
    assert failed == [
        {
            "name": "save_file",
            "error": "No Cloud computer credits are available.",
            "refusal": {"code": "cloud_credits_exhausted", "resets_at": "2026-12-13T00:00:00Z"},
        }
    ]
    assert json.dumps(task["events"]).count("refusal") == 1
