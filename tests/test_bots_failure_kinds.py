"""A turn that stops says why as one closed kind, decided from structured data where it fails.

The desktop app turns the kind into a plain line and the one action that helps (sign in, the
model connection, the plan, the owl's computer). The kind never depends on the wording of an
error, so a failure in any language, with any model, gets the same kind.
"""

from __future__ import annotations

import json
import sqlite3
import threading

import httpx
import pytest

import alysis_code.bots.runtime as runtime_module
from alysis_code.bots import failures
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import EMPTY_REPLY_ERROR, PROVIDER_INTERRUPTED, BotRuntime
from alysis_code.bots.store import LimitReached, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.types import LLMError, LLMResponse, LLMUsage

GREEK = "Γράψε μου μια σύντομη περίληψη της ημέρας."
JAPANESE = "今日の予定を短くまとめてください。"


@pytest.fixture(autouse=True)
def no_retry_pause(monkeypatch):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)


class ScriptedClient:
    model = "test-model"
    supports_tool_calling = True

    def __init__(self, steps):
        self.steps = list(steps)

    def chat(self, **kwargs):
        step = self.steps.pop(0) if self.steps else self.steps_exhausted()
        if isinstance(step, BaseException):
            raise step
        return step

    @staticmethod
    def steps_exhausted():
        raise AssertionError("The runtime asked the model more often than scripted.")


def provider_error(status, code, message="Refused."):
    return OpenAICompatClient._error_from_response(
        httpx.Response(status, json={"error": {"code": code, "message": message}})
    )


def run_owl(tmp_path, steps, *, message=GREEK, account=False, retries=1):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Ήλιος", purpose="Βοηθά με σημειώσεις"))
    chat = store.create_chat("alice", bot["id"], "Νέα συζήτηση")
    store.enqueue("alice", chat["id"], TaskInput(message=message, request_id="request_1"))
    task = store.claim("alice")
    runtime = BotRuntime(store, "alice", lambda _: ScriptedClient(steps * retries))
    runtime.model_account = account
    runtime.run(task, EventCancellationToken(threading.Event()))
    return store, store.task("alice", task["id"])


def kind_of(task):
    return (task["failure"] or {}).get("kind")


@pytest.mark.parametrize("message", [GREEK, JAPANESE])
def test_refused_credentials_mean_signing_in_on_the_account_and_the_model_key_otherwise(
    tmp_path, message
):
    refused = provider_error(401, "invalid_api_key", "Invalid API key provided.")
    _, account = run_owl(tmp_path / "account", [refused], message=message, account=True)
    assert account["status"] == "failed"
    assert account["failure"] == {"kind": "sign_in", "status": 401, "code": "invalid_api_key"}
    _, own_key = run_owl(tmp_path / "own", [refused], message=message, account=False)
    assert own_key["failure"] == {"kind": "model", "status": 401, "code": "invalid_api_key"}
    # The person-facing text is unchanged; the kind sits beside it and in the final event.
    assert own_key["error"] == account["error"]
    final = own_key["events"][-1]
    assert final["kind"] == "failed"
    assert final["data"]["failure"] == own_key["failure"]


def test_a_plan_limit_is_its_own_kind(tmp_path):
    limit = provider_error(
        429,
        "rate_limit_exceeded",
        "Weekly fair-use limit reached (your account credit allowance in 7 days). "
        "Retry as usage ages out.",
    )
    _, task = run_owl(tmp_path, [limit], account=True, retries=20)
    assert kind_of(task) == "plan_limit"


def test_a_model_that_is_not_there_points_at_the_model(tmp_path):
    _, task = run_owl(tmp_path, [provider_error(404, "model_not_found")])
    assert task["failure"] == {"kind": "model", "status": 404, "code": "model_not_found"}


def test_a_busy_model_service_is_a_service_cause_after_its_retries(tmp_path):
    busy = provider_error(503, "server_error", "Service unavailable.")
    _, task = run_owl(tmp_path, [busy], retries=20)
    assert task["error"] == PROVIDER_INTERRUPTED
    assert task["failure"] == {"kind": "service", "status": 503, "code": "server_error"}


def test_a_dropped_connection_is_a_network_cause(tmp_path):
    def dropped():
        try:
            raise httpx.ConnectError("connection refused")
        except httpx.ConnectError as cause:
            error = LLMError("The model service could not be reached.")
            error.__cause__ = cause
            return error

    _, task = run_owl(tmp_path, [dropped() for _ in range(20)])
    assert kind_of(task) == "network"


def test_empty_replies_are_the_models_and_the_allowance_is_the_tasks(tmp_path, monkeypatch):
    empty = LLMResponse("", [], {}, usage=LLMUsage(1, 0, 1))
    _, task = run_owl(tmp_path / "empty", [empty, empty])
    assert task["error"] == EMPTY_REPLY_ERROR
    assert task["failure"] == {"kind": "model"}

    store = Store(tmp_path / "allowance.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Άτλας"))
    chat = store.create_chat("alice", bot["id"], "Συζήτηση")
    store.enqueue("alice", chat["id"], TaskInput(message=JAPANESE, request_id="request_2"))
    task = store.claim("alice")
    runtime = BotRuntime(store, "alice", lambda _: ScriptedClient([]))

    def allowance(*args, **kwargs):
        raise LimitReached("The task reached its model-call allowance.")

    monkeypatch.setattr(store, "reserve_call", allowance)
    runtime.run(task, EventCancellationToken(threading.Event()))
    assert store.task("alice", task["id"])["failure"] == {"kind": "task_limit"}


def test_a_turn_that_finishes_or_is_stopped_carries_no_failure(tmp_path):
    store, task = run_owl(tmp_path, [LLMResponse("Έτοιμο.", [], {}, usage=LLMUsage(1, 1, 2))])
    assert task["status"] == "completed"
    assert task["failure"] is None
    assert "failure" not in task["events"][-1]["data"]

    chat = store.list_chats("alice", task["bot_id"])[0]
    second = store.enqueue(
        "alice", chat["id"], TaskInput(message=JAPANESE, request_id="stop_this_request")
    )
    store.finish("alice", second["id"], "cancelled", failure=failures.failure(failures.SERVICE))
    assert store.task("alice", second["id"])["failure"] is None


def test_a_restart_under_a_running_turn_is_a_service_cause(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Ήλιος"))
    chat = store.create_chat("alice", bot["id"], "Συζήτηση")
    store.enqueue("alice", chat["id"], TaskInput(message=GREEK, request_id="request_1"))
    running = store.claim("alice")
    # It ran a command on the person's computer, so a restart does not start it again.
    store.event("alice", running["id"], "tool_started", {"name": "local_command"})
    store.event("alice", running["id"], "tool_completed", {"name": "local_command"})
    assert store.recover("alice") == 1
    task = store.task("alice", running["id"])
    assert task["status"] == "interrupted"
    cause = {"kind": "service", "not_rerun": "acted", "step": "local_command"}
    assert task["failure"] == cause
    assert task["events"][-1]["data"]["failure"] == cause


def test_classification_reads_structure_never_wording():
    # The same refusal, worded in another language, gets the same kind.
    english = provider_error(404, "model_not_found", "The model does not exist.")
    greek = provider_error(404, "model_not_found", "Το μοντέλο δεν υπάρχει.")
    assert failures.classify(english, account=False, transient=False) == failures.classify(
        greek, account=False, transient=False
    )
    # Words that sound like a cause decide nothing: an unknown error stays unknown.
    assert failures.classify(
        RuntimeError("Invalid API key; sign in again; network timeout"),
        account=True,
        transient=False,
    ) == {"kind": "unknown"}
    # A kind set where a failure was raised wins, even under a wrapping model error.
    try:
        try:
            raise failures.with_kind(LimitReached("Too many actions."), failures.MODEL)
        except LimitReached as inner:
            raise LLMError("wrapped") from inner
    except LLMError as wrapped:
        assert failures.classify(wrapped, account=False, transient=False) == {"kind": "model"}
    # A database that was briefly unavailable is the service's, not the person's.
    assert failures.classify(
        sqlite3.OperationalError("database is locked"), account=False, transient=False
    ) == {"kind": "service"}
    with pytest.raises(ValueError):
        failures.failure("something_else")


def test_older_databases_gain_the_failure_column(tmp_path):
    path = tmp_path / "bots.sqlite3"
    store = Store(path)
    bot = store.save_bot("alice", BotInput(name="Ήλιος"))
    chat = store.create_chat("alice", bot["id"], "Συζήτηση")
    task = store.enqueue("alice", chat["id"], TaskInput(message=GREEK, request_id="request_1"))
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE tasks DROP COLUMN failure")
        db.execute("PRAGMA user_version=40")
    reopened = Store(path)
    assert reopened.task("alice", task["id"])["failure"] is None
    reopened.finish("alice", task["id"], "failed", error="x", failure={"kind": "network"})
    assert reopened.task("alice", task["id"])["failure"] == {"kind": "network"}
    with sqlite3.connect(path) as db:
        stored = db.execute("SELECT failure FROM tasks WHERE id=?", (task["id"],)).fetchone()[0]
    assert json.loads(stored) == {"kind": "network"}
