"""Skipping a question carries the owl's work on without the answer."""

from __future__ import annotations

import json
import threading
from uuid import uuid4

from alysis_code.bots.clarifications import AskQuestion, ClarificationStore, QuestionDecision
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_style import skipped_question_request
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage

QUESTION = "Ποιο είναι το θέμα σου και ποιους θέλεις να φτάσεις;"


class ScriptedClient:
    model = "test-model"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(
            json.loads(json.dumps({"messages": kwargs["messages"], "tools": kwargs.get("tools")}))
        )
        return next(self.responses)


def response(content):
    return LLMResponse(content, [], {}, usage=LLMUsage(10, 5, 15))


def ended_on_a_question(tmp_path):
    """A turn that asked the person something and then finished, waiting for the answer."""
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("owner", BotInput(name="Κουκουβάγια", purpose="", memory_enabled=False))
    chat = store.create_chat("owner", bot["id"], "Αναρτήσεις")
    store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message="Σχεδίασε μια εβδομάδα αναρτήσεων", request_id=uuid4().hex),
    )
    source = store.claim("owner")
    questions = ClarificationStore(store, "owner")
    question = questions.ask(source, AskQuestion(question=QUESTION))
    store.finish("owner", source["id"], "completed", content="Πες μου πρώτα το θέμα σου.")
    return store, bot, chat, source, questions, question


def skip(questions, bot, question):
    return questions.decide(
        bot["id"], question["id"], QuestionDecision(action="close", expected_revision=1)
    )


def run(store, client):
    task = store.claim("owner")
    BotRuntime(store, "owner", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    return store.task("owner", task["id"])


def test_skipping_the_question_a_finished_turn_ended_on_carries_the_request_on(tmp_path):
    store, bot, chat, source, questions, question = ended_on_a_question(tmp_path)
    closed = skip(questions, bot, question)
    assert closed["status"] == "closed" and closed["answer"] is None
    tasks = store.conversation("owner", chat["id"])["tasks"]
    assert [t["id"] for t in tasks][0] == source["id"] and len(tasks) == 2
    carried_on = tasks[1]
    assert closed["answer_task_id"] == carried_on["id"]
    assert carried_on["status"] == "queued" and carried_on["message"] == ""
    assert carried_on["request_id"] == "clarification_skip_" + question["id"]
    assert carried_on["config"]["skipped_question"] == {"question": QUESTION}
    # Skipping again, or from a second window, never starts a second turn.
    assert skip(questions, bot, question)["answer_task_id"] == carried_on["id"]
    assert len(store.conversation("owner", chat["id"])["tasks"]) == 2

    client = ScriptedClient([response("Ορίστε μια εβδομάδα, για γενικό κοινό τεχνολογίας.")])
    finished = run(store, client)
    assert finished["id"] == carried_on["id"] and finished["status"] == "completed"
    request = client.requests[0]
    # The work goes on as any turn does, with its tools, and the app's request stands in for
    # the person: it names the skipped question, in the person's own words.
    assert request.get("tools")
    asked = [m["content"] for m in request["messages"] if m["role"] == "user"]
    assert asked == [
        "Σχεδίασε μια εβδομάδα αναρτήσεων",
        skipped_question_request(finished["config"]),
    ]
    assert json.dumps(QUESTION, ensure_ascii=False) in asked[1]
    # The chat shows the owl's reply after its question, and no message the person didn't send.
    conversation = store.conversation("owner", chat["id"])
    assert [(m["role"], m["content"]) for m in conversation["messages"]] == [
        ("user", "Σχεδίασε μια εβδομάδα αναρτήσεων"),
        ("assistant", "Πες μου πρώτα το θέμα σου."),
        ("assistant", "Ορίστε μια εβδομάδα, για γενικό κοινό τεχνολογίας."),
    ]


def test_after_a_newer_request_skipping_only_closes_the_old_question(tmp_path):
    store, bot, chat, source, questions, question = ended_on_a_question(tmp_path)
    store.enqueue(
        "owner",
        chat["id"],
        TaskInput(message="Άσ' το, γράψε μου ένα βιογραφικό", request_id="newer_request"),
    )
    closed = skip(questions, bot, question)
    assert closed["status"] == "closed" and closed["answer_task_id"] is None
    assert [t["message"] for t in store.conversation("owner", chat["id"])["tasks"]] == [
        "Σχεδίασε μια εβδομάδα αναρτήσεων",
        "Άσ' το, γράψε μου ένα βιογραφικό",
    ]


def test_the_next_turn_keeps_the_carried_on_work_in_order(tmp_path):
    store, bot, chat, source, questions, question = ended_on_a_question(tmp_path)
    skip(questions, bot, question)
    carried_on = run(store, ScriptedClient([response("Ορίστε η εβδομάδα.")]))
    store.enqueue(
        "owner", chat["id"], TaskInput(message="Πιο σύντομες", request_id="shorter_please")
    )
    client = ScriptedClient([response("Έγινε.")])
    run(store, client)
    turns = [
        (m["role"], m["content"])
        for m in client.requests[0]["messages"]
        if m["role"] in ("user", "assistant")
    ]
    assert turns == [
        ("user", "Σχεδίασε μια εβδομάδα αναρτήσεων"),
        ("assistant", "Πες μου πρώτα το θέμα σου."),
        ("user", skipped_question_request(carried_on["config"])),
        ("assistant", "Ορίστε η εβδομάδα."),
        ("user", "Πιο σύντομες"),
    ]
