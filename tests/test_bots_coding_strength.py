"""Coding work: how hard it thinks, and how it outlasts a temporary provider error.

The real controller endpoint (through a test client) and the real worker client, with a scripted
model. The model's declared capacities come from the shared model registry's own override layer.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from test_bots_browser_runtime import response
from test_bots_execution_strength import PLAIN, REASONER, declare

import alysis_code.bots.runtime as runtime_module
from alysis_code.bots.app import Settings, create_app
from alysis_code.bots.coding import WorkspaceRegistration
from alysis_code.bots.coding_worker import Broker, BrokerClient, ProviderRetry
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_models import WORK_REASONING_EFFORT
from alysis_code.bots.runtime import PROVIDER_RETRY_ATTEMPTS
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.llm.types import LLMError

WORKER_TOKEN = "w" * 40
REQUEST = "Διόρθωσε τον έλεγχο που αποτυγχάνει · 失敗するテストを直してください"
STEP = [{"role": "user", "content": REQUEST}]
REPLY = "Ο έλεγχος περνά · テストが通りました"
REJECTED = "The coding model attempt failed. No automatic retry was made."


def outage():
    error = LLMError("LLM error 503: temporarily unavailable")
    error.provider_status_code = 503
    return error


def rejection():
    error = LLMError("LLM error 400: the request was not accepted")
    error.provider_status_code = 400
    return error


class Connection:
    """A model connection's client as the app makes one: it carries the connection's reasoning
    effort. Each call records the thinking asked for and what it was sent, then plays the next
    scripted outcome."""

    supports_tool_calling = True

    def __init__(self, model, effort, script):
        self.model = model
        self.reasoning_effort = effort
        self.enable_thinking = bool(effort)
        self.script = list(script)
        self.heard = []

    def chat(self, **kwargs):
        self.heard.append(
            {
                "thinking": (self.enable_thinking, self.reasoning_effort),
                "messages": kwargs["messages"],
            }
        )
        outcome = self.script.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class Recording:
    """The worker's transport to the controller, keeping each request body it sent."""

    def __init__(self, transport):
        self.transport = transport
        self.base_url = transport.base_url
        self.bodies = []

    def post(self, path, **kwargs):
        self.bodies.append(kwargs.get("json"))
        return self.transport.post(path, **kwargs)

    def close(self):
        self.transport.close()


class StoppedWhileWaiting(EventCancellationToken):
    """The person presses Stop while the worker waits to send the step again."""

    def __init__(self):
        super().__init__(threading.Event())
        self.waits = []

    def wait(self, timeout=None):
        self.waits.append(timeout)
        self._event.set()
        return super().wait(timeout)


@pytest.fixture
def coding(tmp_path):
    """A controller with one running coding job for an owl, and its worker's client."""
    opened = []

    def start(connection, *, execution_limit=None):
        app = create_app(
            Settings(
                database=tmp_path / "controller.db",
                token="a" * 40,
                api_key="fixture",
                base_url="https://models.invalid/v1",
                model=connection.model,
                coding_token=WORKER_TOKEN,
            ),
            client_factory=lambda _task: connection,
        )
        runtime = app.state.runtime
        store, owner = runtime.store, runtime.owner
        bot = store.save_bot(owner, BotInput(name="Κώδικας", purpose="Έργο · 作業"))
        workspace, worker = uuid4().hex, uuid4().hex
        runtime.coding.register(
            WorkspaceRegistration(id=workspace, worker_id=worker, name="Έργο · 作業")
        )
        runtime.coding.bind(bot["id"], workspace)
        chat = store.create_chat(owner, bot["id"], "Συνομιλία")
        store.enqueue(owner, chat["id"], TaskInput(message=REQUEST, request_id=uuid4().hex))
        task = store.claim(owner)
        job = runtime.coding.submit(task, REQUEST, runtime._context(task))
        assert runtime.coding.claim(workspace, worker)["id"] == job["id"]
        transport = TestClient(app, headers={"Authorization": "Bearer " + WORKER_TOKEN})
        broker = Broker(str(transport.base_url), WORKER_TOKEN, worker)
        broker.client.close()
        broker.client = Recording(transport)
        opened.append(broker.client)
        return SimpleNamespace(
            store=store,
            owner=owner,
            task=task,
            job=job,
            worker=worker,
            transport=transport,
            sent=broker.client.bodies,
            client=BrokerClient(
                broker, job["id"], connection.model, execution_limit=execution_limit
            ),
        )

    yield start
    for transport in opened:
        transport.close()


def retries_recorded(work):
    events = work.store.task(work.owner, work.task["id"])["events"]
    return [event["data"] for event in events if event["kind"] == "provider_retry"]


def stop_token():
    return EventCancellationToken(threading.Event())


# How hard coding work thinks


@pytest.mark.parametrize(
    "model, connection_effort, thinking",
    [
        (REASONER, None, (True, WORK_REASONING_EFFORT)),
        (REASONER, "high", (True, "high")),
        (PLAIN, None, (False, None)),
    ],
)
def test_the_coding_controller_thinks_with_the_connection_effort_else_the_declared_default(
    coding, monkeypatch, model, connection_effort, thinking
):
    declare(monkeypatch, model)
    connection = Connection(model, connection_effort, [response(REPLY)])
    work = coding(connection)
    assert work.client.chat(messages=STEP, cancellation_token=stop_token()).content == REPLY
    assert [call["thinking"] for call in connection.heard] == [thinking]
    assert REQUEST in str(connection.heard[0]["messages"])
    # The connection's own client, used by the owl's chat, keeps its setting.
    assert (connection.enable_thinking, connection.reasoning_effort) == (
        bool(connection_effort),
        connection_effort,
    )


# Outlasting a temporary provider error


def test_a_temporary_provider_error_is_sent_again_and_the_work_continues(coding, monkeypatch):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    declare(monkeypatch, PLAIN)
    connection = Connection(PLAIN, None, [outage(), TimeoutError("read timeout"), response(REPLY)])
    work = coding(connection, execution_limit=5)
    assert work.client.chat(messages=STEP, cancellation_token=stop_token()).content == REPLY
    assert len(connection.heard) == 3
    assert connection.heard[0]["messages"] == connection.heard[2]["messages"]
    # The first attempt is the same request as before retries; each repeat says which it is.
    assert [body.get("provider_retries") for body in work.sent] == [None, 1, 2]
    assert len({body["request_id"] for body in work.sent}) == 3
    assert [r["exception_type"] for r in retries_recorded(work)] == ["LLMError", "TimeoutError"]
    # Every attempt is charged, on the controller and in the worker's own count.
    assert work.store.task(work.owner, work.task["id"])["calls"] == 3
    assert work.client.execution_calls == 3


def test_a_repeat_past_the_work_allowance_becomes_the_reserved_report(coding, monkeypatch):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    declare(monkeypatch, PLAIN)
    connection = Connection(PLAIN, None, [outage(), response(REPLY)])
    work = coding(connection, execution_limit=1)
    tools = [{"type": "function", "function": {"name": "shell_run", "parameters": {}}}]
    reply = work.client.chat(messages=STEP, tools=tools, cancellation_token=stop_token())
    assert reply.content == REPLY
    # The failed attempt used the only work call, so the repeat is the report and cannot act.
    assert [body["phase"] for body in work.sent] == ["execute", "report"]
    assert work.sent[1]["tools"] is None
    assert "Execution is now closed" in str(work.sent[1]["messages"][-1])
    assert work.client.reporting_started


def test_a_rejected_request_fails_at_once_without_another_attempt(coding, monkeypatch):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    declare(monkeypatch, PLAIN)
    connection = Connection(PLAIN, None, [rejection(), response(REPLY)])
    work = coding(connection)
    with pytest.raises(LLMError, match="HTTP 502") as failed:
        work.client.chat(messages=STEP, cancellation_token=stop_token())
    assert not isinstance(failed.value, ProviderRetry)
    assert len(connection.heard) == len(work.sent) == 1
    assert retries_recorded(work) == []


def test_repeats_stop_at_the_shared_limit(coding, monkeypatch):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    declare(monkeypatch, PLAIN)
    attempts = PROVIDER_RETRY_ATTEMPTS + 1
    connection = Connection(PLAIN, None, [outage() for _ in range(attempts)] + [response(REPLY)])
    work = coding(connection)
    with pytest.raises(LLMError, match="HTTP 502"):
        work.client.chat(messages=STEP, cancellation_token=stop_token())
    assert len(connection.heard) == len(work.sent) == attempts
    assert work.sent[-1]["provider_retries"] == PROVIDER_RETRY_ATTEMPTS
    assert len(retries_recorded(work)) == PROVIDER_RETRY_ATTEMPTS


def test_stop_during_the_wait_ends_the_step_without_another_call(coding, monkeypatch):
    declare(monkeypatch, PLAIN)
    connection = Connection(PLAIN, None, [outage(), response(REPLY)])
    work = coding(connection)
    token = StoppedWhileWaiting()
    with pytest.raises(CooperativeCancellationError):
        work.client.chat(messages=STEP, cancellation_token=token)
    # The worker waited the controller's pause itself and Stop ended that wait.
    assert token.waits == [retries_recorded(work)[0]["wait_seconds"]]
    assert token.waits[0] > 0
    assert len(connection.heard) == len(work.sent) == 1


def test_a_call_that_cannot_be_stopped_is_not_waited_for(coding, monkeypatch):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    declare(monkeypatch, PLAIN)
    connection = Connection(PLAIN, None, [outage(), response(REPLY)])
    work = coding(connection)
    # A summary call carries no cancellation token: the caller gets the error instead.
    with pytest.raises(ProviderRetry):
        work.client.chat(messages=STEP)
    assert len(connection.heard) == len(work.sent) == 1


def test_a_request_from_a_worker_before_retries_is_handled_as_before(coding, monkeypatch):
    declare(monkeypatch, PLAIN)
    connection = Connection(PLAIN, None, [response(REPLY), rejection()])
    work = coding(connection)
    route = f"/internal/coding/jobs/{work.job['id']}/model"
    old_style = {
        "worker_id": work.worker,
        "request_id": uuid4().hex,
        "phase": "execute",
        "messages": STEP,
    }
    answered = work.transport.post(route, json=old_style)
    assert answered.status_code == 200, answered.text
    assert answered.json()["content"] == REPLY
    # The same request keeps its identity: sent again, it gets the saved answer, no new call.
    again = work.transport.post(route, json=old_style)
    assert again.status_code == 200 and again.json() == answered.json()
    assert len(connection.heard) == 1
    refused = work.transport.post(route, json={**old_style, "request_id": uuid4().hex})
    assert (refused.status_code, refused.json()["detail"]) == (502, REJECTED)
    assert retries_recorded(work) == []


def test_a_503_that_is_not_the_controller_offering_a_repeat_is_an_ordinary_refusal():
    class Answer:
        def __init__(self, status, body):
            self.status_code, self.is_success, self._body = status, False, body

        def json(self):
            if isinstance(self._body, Exception):
                raise self._body
            return self._body

    for body in (
        ValueError("not JSON"),
        ["not", "an", "object"],
        {"detail": "Service unavailable"},
        {"detail": {"retry_after": -1}},
        {"detail": {"retry_after": True}},
    ):
        broker = Broker("http://127.0.0.1:1", "fixture", uuid4().hex)
        broker.client.close()
        broker.client = SimpleNamespace(post=lambda *args, body=body, **kwargs: Answer(503, body))
        with pytest.raises(LLMError, match="HTTP 503") as refused:
            broker.post("jobs/fixture/model")
        assert not isinstance(refused.value, ProviderRetry)
