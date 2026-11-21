"""How hard an owl thinks when it answers in a chat.

The actual Bots runtime and queue with a scripted model. The model's declared capacities come
from the shared model registry, set through its own override layer, as for its work.
"""

from __future__ import annotations

import pytest
from test_bots_execution_strength import PLAIN, REASONER, ConnectionClient, declare

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_models import WORK_REASONING_EFFORT
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store

# A question in more than one language; it must reach the model as written.
QUESTION = "Τι λέει το συμβόλαιο για την παράδοση; · 合同里关于交付是怎么说的？"


@pytest.fixture
def asked(tmp_path):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Νίκη", purpose="Συμβόλαια · 合同"))
    chat = store.create_chat("owner", bot["id"], "Συνομιλία")
    store.enqueue("owner", chat["id"], TaskInput(message=QUESTION, request_id="ask-a-question"))
    return store, store.claim("owner")


def answer(asked, client):
    store, task = asked
    runtime = BotRuntime(store, "owner", lambda _task: client)
    runtime.run(task, runtime.task_token(task["id"]))
    finished = store.task("owner", task["id"])
    assert finished["status"] == "completed", finished
    return finished


@pytest.mark.parametrize(
    "model, connection_effort, thinking",
    [
        # (a) The connection's own effort wins, whatever the model declares, lower ones too.
        (REASONER, "high", (True, "high")),
        (REASONER, "low", (True, "low")),
        (PLAIN, "high", (True, "high")),
        # (b) None set ("auto" is none of its own), and the model declares reasoning: the
        # owl's work effort.
        (REASONER, None, (True, WORK_REASONING_EFFORT)),
        (REASONER, "auto", (True, WORK_REASONING_EFFORT)),
        (REASONER, "Auto ", (True, WORK_REASONING_EFFORT)),
        # (c) None set, and the model declares none: no thinking.
        (PLAIN, None, (False, None)),
    ],
)
def test_a_chat_turn_thinks_with_the_connection_effort_else_the_declared_default(
    asked, monkeypatch, model, connection_effort, thinking
):
    declare(monkeypatch, model)
    heard = []
    connection = ConnectionClient(model, connection_effort, heard)
    answer(asked, connection)
    assert heard
    assert {call["thinking"] for call in heard} == {thinking}
    # The connection's own client is never changed; the owl thinks with a copy.
    assert (connection.enable_thinking, connection.reasoning_effort) == (
        bool(connection_effort),
        connection_effort,
    )


def test_the_question_reaches_the_thinking_model_as_written(asked, monkeypatch):
    declare(monkeypatch, REASONER)
    heard = []
    answer(asked, ConnectionClient(REASONER, None, heard))
    first = heard[0]
    assert first["thinking"] == (True, WORK_REASONING_EFFORT)
    assert any(
        message["role"] == "user" and QUESTION in str(message["content"])
        for message in first["messages"]
    )
