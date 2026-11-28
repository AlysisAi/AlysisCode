import json
import threading
from uuid import uuid4

import pytest

from alysis_code.bots.clarifications import QuestionDecision
from alysis_code.bots.models import AssistantOwlCreateInput, BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, ToolCall


@pytest.mark.parametrize(
    ("model", "question", "answer"),
    [
        ("fixture-fr", "Propose une tâche de rédaction.", "La proposition attend votre accord."),
        ("fixture-el", "Πρότεινε μια εργασία συγγραφής.", "Η πρόταση περιμένει την έγκρισή σου."),
    ],
)
def test_managed_assistant_only_proposes_and_waits_for_the_person(
    tmp_path, model, question, answer
):
    owner = "person"
    store = Store(tmp_path / "assist.db")
    worker = store.save_bot(owner, BotInput(name="Writer"))
    assistant = store.set_assistant(owner, True)["bot"]
    assert store.claim(owner) is None
    chat = store.create_chat(owner, assistant["id"], "Assist")
    task = store.enqueue(owner, chat["id"], TaskInput(message=question, request_id=uuid4().hex))
    seen = []

    class Client:
        supports_tool_calling = True

        def chat(self, *, messages, tools, **kwargs):
            names = {tool["function"]["name"] for tool in tools}
            assert names == {
                "ask_clarification",
                "answer_clarification",
                "list_peer_bots",
                "propose_owl_assignment",
                "propose_new_owl",
                "read_owl_assignments",
                "read_working_note",
                "save_working_note",
                "search_working_notes",
            }
            assert any(
                message["role"] == "system"
                and json.dumps(assistant["instructions"], ensure_ascii=False)
                in message.get("content", "")
                for message in messages
            )
            seen.append(names)
            if len(seen) == 1:
                proposal = next(
                    tool["function"]
                    for tool in tools
                    if tool["function"]["name"] == "propose_owl_assignment"
                )
                assert "Assignments" not in proposal["description"]
                return LLMResponse(
                    "",
                    [
                        ToolCall(
                            "assignment",
                            "propose_owl_assignment",
                            {
                                "recipient_bot_id": worker["id"],
                                "brief": question,
                                "expected_result": answer,
                            },
                        )
                    ],
                    {},
                )
            receipt = json.loads(next(m["content"] for m in messages if m["role"] == "tool"))
            assert receipt["status"] == "proposed"
            assert "this chat" in receipt["message"] and "Assignments" not in receipt["message"]
            return LLMResponse(answer, [], {})

    client = Client()
    client.model = model

    def connected_apps(_):
        pytest.fail("The managed assistant opened connected app tools")

    runtime = BotRuntime(store, owner, lambda _: client, connections=connected_apps)
    runtime.run(store.claim(owner), EventCancellationToken(threading.Event()))
    assert store.task(owner, task["id"])["output"] == answer
    assignment = store.list_assignments(owner, assistant["id"])["items"][0]
    assert assignment["status"] == "proposed" and assignment["child_task_id"] is None
    assert store.claim(owner) is None


def test_assist_collects_grouped_answers_then_drafts_an_owl_in_the_same_chat(tmp_path):
    owner = "person"
    store = Store(tmp_path / "assist.db")
    assistant = store.set_assistant(owner, True)["bot"]
    store.create_chat(owner, assistant["id"], "Unrelated chat")
    chat = store.create_chat(owner, assistant["id"], "月末の照合")
    store.enqueue(
        owner, chat["id"], TaskInput(message="Help with month-end", request_id=uuid4().hex)
    )
    questions = "1. Which period are you closing?\n2. Who reads the result?"
    answer = "October. The finance lead."
    follow_up = "What should the finance lead receive?"
    expected = "A short discrepancy report."
    draft = {
        "name": "Reconciliation Reviewer",
        "purpose": "Review monthly discrepancies",
        "instructions": "Separate confirmed differences from questions.",
        "brief": "Draft the October discrepancy report for the finance lead.",
        "expected_result": expected,
    }
    script = iter(
        [
            ToolCall("questions", "ask_clarification", {"question": questions}),
            "Please answer both above.",
            ToolCall(
                "note",
                "save_working_note",
                {
                    "title": "Month-end reconciliation",
                    "content": "October is the period. The finance lead reads the result.",
                    "expected_revision": 0,
                },
            ),
            ToolCall("follow-up", "ask_clarification", {"question": follow_up}),
            "One more detail.",
            ToolCall("peers", "list_peer_bots", {}),
            ToolCall("draft", "propose_new_owl", draft),
            "Review the new owl in this chat.",
        ]
    )

    class Client:
        model = "fixture-ja"
        supports_tool_calling = True

        def chat(self, *, messages, tools, **kwargs):
            reply = next(script)
            if isinstance(reply, ToolCall):
                if reply.name == "propose_new_owl":
                    assert any(
                        "October is the period." in str(message["content"]) for message in messages
                    )
                    assert any(expected in str(message["content"]) for message in messages)
                return LLMResponse("", [reply], {})
            return LLMResponse(reply, [], {})

    runtime = BotRuntime(store, owner, lambda _: Client())

    def run_turn():
        task = store.claim(owner)
        assert task["chat_id"] == chat["id"]
        runtime.run(task, EventCancellationToken(threading.Event()))
        assert store.task(owner, task["id"])["status"] == "completed"
        return task

    def respond(text, question):
        [pending] = [
            item
            for item in runtime.clarifications.list(assistant["id"])
            if item["status"] == "pending"
        ]
        assert pending["question"] == question
        assert pending["options"] == []
        decision = QuestionDecision(
            action="answer", answer=text, expected_revision=pending["revision"]
        )
        runtime.clarifications.decide(assistant["id"], pending["id"], decision)

    run_turn()
    respond(answer, questions)
    run_turn()
    respond(expected, follow_up)
    source = run_turn()
    assert len(store.list_bots(owner)) == 1
    turns = store.conversation(owner, chat["id"])["tasks"]
    assert (
        next(turn for turn in turns if turn["id"] == source["id"])["assistant_owl_draft"] == draft
    )
    [saved] = store.list_notes(owner, assistant["id"])
    assert "finance lead" in saved["content"]
    approval = AssistantOwlCreateInput(
        request_id="chat-owl-" + source["id"], start_task=True, **draft
    )
    created = store.approve_assistant_owl(owner, assistant["id"], approval)
    assert created["bot"]["name"] == draft["name"]
    assert created["assignment"]["brief"] == draft["brief"]
    assert store.approve_assistant_owl(owner, assistant["id"], approval) == created


def test_turning_off_assist_before_a_queued_request_prevents_model_work(tmp_path):
    owner = "person"
    store = Store(tmp_path / "assist.db")
    assistant = store.set_assistant(owner, True)["bot"]
    chat = store.create_chat(owner, assistant["id"], "Assist")
    task = store.enqueue(owner, chat["id"], TaskInput(message="Draft it", request_id=uuid4().hex))
    claimed = store.claim(owner)
    store.set_assistant(owner, False)

    def client(_):
        pytest.fail("A disabled assistant started a model call")

    runtime = BotRuntime(store, owner, client)
    runtime.run(claimed, EventCancellationToken(threading.Event()))
    assert store.task(owner, task["id"])["status"] == "cancelled"
