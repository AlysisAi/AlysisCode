"""How hard an owl thinks while it works on its computer.

The actual Alysis Code loop and Bots queue with a scripted model and computer. The model's
declared capacities come from the shared model registry, set through its own override layer.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from test_bots_browser_runtime import response
from test_bots_desktop import Executor

from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_models import WORK_REASONING_EFFORT, work_client
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store

# The work the person approved, in more than one language; it must reach the model as written.
REQUEST = "Ετοίμασε την αναφορά του Μαρτίου από το αρχείο · 三月の報告書を作成してください"
REPORT = "Η αναφορά είναι έτοιμη · 報告書が完成しました"

# Two registry entries: what each declares is set with the registry's own override layer.
REASONER = "owl-fixture-declares-reasoning"
PLAIN = "owl-fixture-declares-no-reasoning"


def declare(monkeypatch, model):
    monkeypatch.setenv("ALYSIS_SUPPORTS_REASONING", "true" if model == REASONER else "false")


class ConnectionClient:
    """A model connection's client as the app makes one: it carries the connection's reasoning
    effort and asks for thinking exactly when the connection sets one. Each call records the
    thinking the provider would be asked for."""

    supports_tool_calling = True

    def __init__(self, model, effort, heard):
        self.model = model
        self.reasoning_effort = effort
        self.enable_thinking = bool(effort)
        self.heard = heard

    def chat(self, **kwargs):
        self.heard.append(
            {
                "thinking": (self.enable_thinking, self.reasoning_effort),
                "messages": kwargs["messages"],
            }
        )
        return response(REPORT)


@pytest.fixture
def approved_work(tmp_path):
    """An owl whose person approved work on its computer, asked for in Greek and Japanese."""
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Μίρα", purpose="Αναφορές · 報告"))
    chat = store.create_chat("owner", bot["id"], "Συνομιλία")
    store.enqueue("owner", chat["id"], TaskInput(message=REQUEST, request_id="ask-for-work"))
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    proposal = proposals.propose(
        task,
        ActionProposal(
            question="Να ξεκινήσω; · 始めますか？", instructions=REQUEST, offline_computer=True
        ),
    )
    store.finish("owner", task["id"], "completed", content="Περιμένω έγκριση · 承認待ち")
    proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(action="accept", expected_revision=1, offline_computer=True),
    )
    task = store.claim("owner")
    manager = DesktopComputers("owner", ComputerViews(), factory=Executor)
    yield store, task, manager
    manager.close()


def work(approved_work, client):
    """Hand the approved request to the computer and run the work to its report."""
    store, task, manager = approved_work
    runtime = BotRuntime(store, "owner", lambda _task: client, desktop_computers=manager)
    runtime.limits = replace(runtime.limits, compaction_enabled=False)
    runtime.run(task, runtime.task_token(task["id"]))
    queued = runtime.background.claim()
    assert queued["id"] == task["id"]
    runtime.run(queued, runtime.task_token(task["id"]), background=True)
    finished = store.task("owner", task["id"])
    assert finished["status"] == "completed", finished
    return finished


@pytest.mark.parametrize(
    "model, connection_effort, thinking",
    [
        # (a) The connection's own effort wins, whatever the model declares.
        (REASONER, "high", (True, "high")),
        (PLAIN, "high", (True, "high")),
        # (b) None set, and the model declares reasoning: the work default.
        (REASONER, None, (True, WORK_REASONING_EFFORT)),
        (REASONER, "auto", (True, WORK_REASONING_EFFORT)),
        # (c) None set, and the model declares none: no thinking.
        (PLAIN, None, (False, None)),
    ],
)
def test_work_thinks_with_the_connection_effort_else_the_declared_default(
    approved_work, monkeypatch, model, connection_effort, thinking
):
    declare(monkeypatch, model)
    heard = []
    connection = ConnectionClient(model, connection_effort, heard)
    finished = work(approved_work, connection)
    assert heard
    assert {call["thinking"] for call in heard} == {thinking}
    assert finished["output"] == REPORT
    # The connection's own client is never changed; the owl thinks with a copy.
    assert (connection.enable_thinking, connection.reasoning_effort) == (
        bool(connection_effort),
        connection_effort,
    )


def test_the_approved_request_reaches_the_thinking_model_as_written(approved_work, monkeypatch):
    declare(monkeypatch, REASONER)
    heard = []
    work(approved_work, ConnectionClient(REASONER, None, heard))
    first = heard[0]
    assert first["thinking"] == (True, WORK_REASONING_EFFORT)
    assert any(
        message["role"] == "user" and REQUEST in str(message["content"])
        for message in first["messages"]
    )


def test_a_connection_that_already_thinks_hard_enough_is_used_as_it_is(monkeypatch):
    declare(monkeypatch, REASONER)
    for effort in ("high", "none"):
        connection = ConnectionClient(REASONER, effort, [])
        assert work_client(connection) is connection
    declare(monkeypatch, PLAIN)
    connection = ConnectionClient(PLAIN, None, [])
    assert work_client(connection) is connection


# Coding work: a worker session whose every model call the controller makes.


def test_a_coding_worker_session_sets_no_thinking_level_of_its_own(tmp_path):
    from uuid import uuid4

    from alysis_code.bots.coding_worker import Broker, ScopedRunner, make_session
    from alysis_code.config import AppConfig

    workspace = (tmp_path / "έργο · 作業").resolve()
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    # A loopback address nothing listens on: building the session makes no request.
    broker = Broker("http://127.0.0.1:1", "fixture", uuid4().hex)
    session = make_session(
        workspace,
        state,
        {"id": uuid4().hex, "session_id": uuid4().hex},
        broker,
        {
            "base_url": AppConfig.model_fields["base_url"].default,
            "model": PLAIN,
            "image": "fixture-image",
            "verify": [],
        },
        ScopedRunner(workspace=workspace),
    )
    try:
        assert session.client.broker is broker
        assert (session.cfg.llm_enable_thinking, session.cfg.llm_reasoning_effort) == (None, None)
    finally:
        session.close()
        broker.client.close()
