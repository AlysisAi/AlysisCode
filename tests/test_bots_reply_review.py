"""Accounted bot-service completion gate; scripted models, not live evidence."""

import json
import threading
from dataclasses import replace

import pytest

from alysis_code.bots.clarifications import AskQuestion, QuestionDecision
from alysis_code.bots.models import BotInput, MemoryInput, TaskInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview, validate_tool_response
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import LimitReached, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall


def response(text="", calls=(), raw=None):
    return LLMResponse(text, list(calls), raw or {}, usage=LLMUsage(10, 5, 15))


def verdict(decision, reason="The reply matches the receipts.", issue=None, false_claim=None):
    body = {"decision": decision, "reason": reason}
    if issue is not None:
        body["issue"] = issue
    if false_claim is not None:
        body["false_claim"] = false_claim
    return response(json.dumps(body))


class Model:
    model = "fixture"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        item = next(self.responses)
        return item(kwargs) if callable(item) else item


def study(tmp_path, responses, *, limit=12, message="Save a note called 東京.txt with Hello"):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Help"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    store.enqueue("owner", chat["id"], TaskInput(message=message, request_id="request-test"))
    task = store.claim("owner")
    model = Model(responses)
    runtime = BotRuntime(
        store, "owner", lambda _: model, limits=RuntimeLimits(calls_per_task=limit)
    )
    runtime.reply_review = ReplyReview(runtime)
    token = EventCancellationToken(threading.Event())
    return store, task, model, runtime, token


def test_false_completion_is_withheld_then_repaired_with_real_write(tmp_path):
    def inspect_review(kw):
        assert kw["tools"] is None
        assert kw["messages"][0]["content"] == REVIEW_INSTRUCTIONS
        payload = json.loads(kw["messages"][1]["content"])
        assert payload["draft_reply"] == "Saved 東京.txt."
        assert not any(m["role"] == "tool" for m in payload["context_and_receipts"])
        assert not store.list_files("owner", task["bot_id"])
        return verdict("repair", "The requested file was not saved; no successful receipt exists.")

    store, task, model, runtime, token = study(
        tmp_path,
        [
            response("Saved 東京.txt."),
            inspect_review,
            response(
                calls=[ToolCall("save", "save_file", {"name": "東京.txt", "content": "Hello"})]
            ),
            response("The note is saved."),
            verdict("ready"),
        ],
    )
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed"
    assert final["calls"] == 5
    assert len(store.list_files("owner", task["bot_id"])) == 1
    assert [
        m["content"]
        for m in store.conversation("owner", task["chat_id"])["messages"]
        if m["role"] == "assistant"
    ] == ["The note is saved."]
    assert store.usage("owner")["reported_input_tokens"] == 50
    assert len(model.requests) == 5
    assert [e["data"] for e in final["events"] if e["kind"] == "reply_review_finished"] == [
        {
            "decision": "repair",
            "reason": "The requested file was not saved; no successful receipt exists.",
        },
        {"decision": "ready", "reason": "The reply matches the receipts."},
    ]


@pytest.mark.parametrize("allowance", [512, 4096])
def test_review_honors_bot_output_allowance_including_reasoning(tmp_path, allowance):
    def reasoning_review(request):
        assert request["max_tokens"] == allowance
        return verdict("ready")

    store, task, model, runtime, token = study(
        tmp_path,
        [response("Hello!"), reasoning_review],
        message="Say hello.",
    )
    runtime.limits = replace(runtime.limits, output_tokens=allowance)
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed"
    assert final["output"] == "Hello!"
    assert final["calls"] == 2


def test_last_repaired_draft_is_checked_before_repair_exhaustion(tmp_path):
    store, task, model, runtime, token = study(
        tmp_path,
        [
            response("First draft"),
            verdict("repair", "A material requirement is missing."),
            response("Second draft"),
            verdict("repair", "A material requirement is still missing."),
            response("Final revised draft"),
            verdict("ready"),
        ],
        message="Explain the requested result.",
    )
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    assert final["calls"] == 6
    assert len(model.requests) == 6
    assert final["status"] == "completed"
    assert final["output"] == "Final revised draft"


@pytest.mark.parametrize(
    ("status_decision", "issue", "delivered"),
    [
        ("ready", None, True),
        # An omission or wording complaint never leaves the person with nothing.
        ("repair", "omission", True),
        ("repair", "wording", True),
        # A claim the receipts contradict still withholds the status ...
        ("repair", "contradiction", False),
        # ... and so does a repair that does not say what it is about.
        ("repair", None, False),
    ],
)
def test_after_repeated_repairs_a_plain_status_is_tried_before_giving_up(
    tmp_path, status_decision, issue, delivered
):
    from alysis_code.bots.runtime import REVIEW_STATUS_ONLY

    status = "I could not save 東京.txt: no file was written. Ask me again to retry."
    store, task, model, runtime, token = study(
        tmp_path,
        [
            response("Saved."),
            verdict("repair", "No receipt."),
            response("Saved."),
            verdict("repair", "No receipt."),
            response("Saved."),
            verdict("repair", "Still no save receipt."),
            response(status),
            verdict(status_decision, "Status matches the receipts.", issue),
            # A withheld status gets one more try, told which claim to leave out.
            response(status),
            verdict("repair", "Still contradicted.", issue),
        ][: 8 if delivered else 10],
    )
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    expected = 8 if delivered else 10
    assert len(model.requests) == expected and final["calls"] == expected
    status_request = model.requests[6]
    assert not status_request.get("tools")  # a status, not more work
    system = [m["content"] for m in status_request["messages"] if m["role"] == "system"]
    assert any(REVIEW_STATUS_ONLY in text and "Still no save receipt." in text for text in system)
    # The status is reviewed for truth, not held to the undelivered work.
    kinds = [
        json.JSONDecoder().raw_decode(model.requests[i]["messages"][1]["content"])[0]["reply_kind"]
        for i in range(1, expected, 2)
    ]
    assert kinds == ["reply", "reply", "reply"] + ["final_status"] * (1 if delivered else 2)
    assert 'reply_kind "final_status"' in REVIEW_INSTRUCTIONS
    if delivered:
        assert final["status"] == "completed" and final["output"] == status
    else:
        assert final["status"] == "failed" and final["output"] == ""
        assert "didn't match what it actually did" in final["error"]
    events = store.task("owner", task["id"])["events"]
    accepted = [e for e in events if e["kind"] == "reply_review_status_accepted"]
    assert bool(accepted) == (status_decision == "repair" and delivered)
    assert not store.list_files("owner", task["bot_id"])


def test_a_withheld_status_is_retried_once_without_the_contradicted_claim(tmp_path):
    claim = "The report is saved in your Library."
    honest = "I could not save the report: no file was written. Ask me again to retry."
    store, task, model, runtime, token = study(
        tmp_path,
        [
            response("Saved."),
            verdict("repair", "No receipt."),
            response("Saved."),
            verdict("repair", "No receipt."),
            response("Saved."),
            verdict("repair", "Still no save receipt."),
            response(claim),
            verdict("repair", "No save receipt.", "contradiction", claim),
            response(honest),
            verdict("ready", "Matches the receipts."),
        ],
    )
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed" and final["output"] == honest
    retry = model.requests[8]
    assert not retry.get("tools")
    system = [m["content"] for m in retry["messages"] if m["role"] == "system"]
    assert any("Leave out this claim" in text and claim in text for text in system)


def test_the_reviewer_sees_what_the_tools_used_in_this_task_do():
    from alysis_code.bots.reply_review import tool_documentation
    from alysis_code.bots.runtime import TOOL_DEFINITIONS

    context = [
        {
            "role": "assistant",
            "tool_calls": [
                {"id": "a", "function": {"name": "export_local_file", "arguments": "{}"}},
                {"id": "b", "function": {"name": "unknown_tool", "arguments": "{}"}},
            ],
        }
    ]
    docs = tool_documentation(context, [])
    assert [d["name"] for d in docs] == ["export_local_file"]
    assert docs[0]["description"] == TOOL_DEFINITIONS["export_local_file"][1][:700]
    offered = [{"function": {"name": "export_local_file", "description": "Offered text."}}]
    assert tool_documentation(context, offered)[0]["description"] == "Offered text."
    assert "tool_documentation" in REVIEW_INSTRUCTIONS


def test_the_reviewer_writes_its_internal_reason_in_english():
    assert "Write the reason in English" in REVIEW_INSTRUCTIONS
    assert "approved_by_request is not a card" in REVIEW_INSTRUCTIONS


def test_the_reviewer_holds_work_to_the_persons_latest_correction():
    # A mid-task correction outranks the approved card's brief; the reviewer must not push
    # the bot back to the earlier wording or ask for the card to be re-proposed.
    assert "later corrections in this task define what the deliverable" in REVIEW_INSTRUCTIONS
    assert "permission to act, not a specification" in REVIEW_INSTRUCTIONS


def test_background_work_waits_while_this_tasks_card_is_pending(tmp_path):
    card = {
        "question": "Shall I draft it?",
        "instructions": "Draft the plan.",
        "offline_computer": False,
    }
    store, task, model, runtime, token = study(
        tmp_path,
        [
            response(calls=[ToolCall("p", "propose_action", card)]),
            response(calls=[ToolCall("b", "continue_in_background", {"summary": "Drafting"})]),
            response("The card is ready: choose Do it to start."),
            verdict("ready"),
        ],
        message="Plan the launch.",
    )
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed"
    assert final["output"] == "The card is ready: choose Do it to start."
    receipt = [m for m in model.requests[2]["messages"] if m.get("role") == "tool"][-1]["content"]
    assert "not_started" in receipt
    assert runtime.proposals.for_task(final)["status"] == "pending"


def test_internal_draft_does_not_replace_visible_answer_or_replay_saved_note(tmp_path):
    draft = "Σας περιμένουμε την Κυριακή στις 18:00 στο Studio Delta."

    def inspect_review(kw):
        payload = json.loads(kw["messages"][1]["content"])
        assert payload["draft_reply"] == "I've saved the project details."
        internal = next(m for m in payload["context_and_receipts"] if m.get("tool_calls"))
        assert internal["content"] == draft
        assert not [
            m
            for m in store.conversation("owner", task["chat_id"])["messages"]
            if m["role"] == "assistant"
        ]
        return verdict("repair", "The invitation is internal; include it in the final reply.")

    store, task, model, runtime, token = study(
        tmp_path,
        [
            response(
                draft,
                [
                    ToolCall(
                        "note",
                        "save_working_note",
                        {"title": "Opening", "content": "Sunday, 18:00, Studio Delta"},
                    )
                ],
            ),
            response("I've saved the project details."),
            inspect_review,
            response(draft),
            verdict("ready"),
        ],
        message="Draft an invitation in Greek for Sunday at 18:00 in Studio Delta, and note the details.",
    )
    runtime.run(task, token)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert [
        m["content"]
        for m in store.conversation("owner", task["chat_id"])["messages"]
        if m["role"] == "assistant"
    ] == [draft]
    assert len(store.list_notes("owner", task["bot_id"])) == 1
    assert len(model.requests) == 5


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_memory_change_during_review_discards_old_draft_and_reviews_current_state(tmp_path, change):
    def mutate_during_review(kw):
        snapshot = json.loads(kw["messages"][1]["content"])["current_memory_state"]
        assert snapshot["approved_memories"][0]["content"] == "Old preference"
        if change == "edit":
            store.save_memory(
                "owner",
                task["bot_id"],
                MemoryInput(title="Format", content="Current preference"),
                memory_id=memory["id"],
            )
        else:
            store.delete_item("owner", task["bot_id"], memory["id"], kind="memories")
        return verdict("ready")

    def check_current_review(kw):
        payload = json.loads(kw["messages"][1]["content"])
        snapshot = payload["current_memory_state"]
        receipts = payload["memory_change_receipts"]
        assert receipts["events"][0]["action"] == ("deleted" if change == "delete" else "edited")
        assert receipts["events"][0]["memory_id"] == memory["id"]
        assert "Old preference" not in json.dumps(receipts)
        assert snapshot["pending_count"] == 0
        if change == "delete":
            assert snapshot["approved_memories"] == []
        else:
            assert snapshot["approved_memories"][0]["content"] == "Current preference"
        return verdict("ready")

    store, task, model, runtime, token = study(
        tmp_path,
        [
            response("Old preference is saved."),
            mutate_during_review,
            response("Current saved state checked."),
            check_current_review,
        ],
        message="What preference is saved?",
    )
    memory = store.save_memory(
        "owner", task["bot_id"], MemoryInput(title="Format", content="Old preference")
    )
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed" and final["calls"] == 4
    assert final["output"] == "Current saved state checked."
    assert [
        m["content"]
        for m in store.conversation("owner", task["chat_id"])["messages"]
        if m["role"] == "assistant"
    ] == [final["output"]]


def test_review_does_not_dispatch_tools_or_publish_an_invalid_verdict(tmp_path):
    store, task, _, runtime, token = study(
        tmp_path,
        [
            response("Nothing happened."),
            response(calls=[ToolCall("bad", "save_file", {"name": "bad.txt", "content": "No"})]),
        ],
    )
    runtime.run(task, token)
    assert store.task("owner", task["id"])["status"] == "failed"
    assert not store.list_files("owner", task["bot_id"])
    assert store.usage("owner")["calls"] == 2


@pytest.mark.parametrize("limit", [1, 3])
def test_budget_cannot_publish_an_unchecked_reply(tmp_path, limit):
    store, task, model, runtime, token = study(
        tmp_path,
        [response("Saved."), verdict("repair", "Missing file receipt."), response("Still saved.")],
        limit=limit,
    )
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    assert final["status"] == "failed"
    assert len(model.requests) <= limit
    assert not [
        m
        for m in store.conversation("owner", task["chat_id"])["messages"]
        if m["role"] == "assistant"
    ]


def test_plain_conversation_needs_no_action_and_cancellation_wins_review(tmp_path):
    def cancel_review(kw):
        store.cancel("owner", task["id"])
        token._event.set()
        return verdict("ready")

    store, task, _, runtime, token = study(
        tmp_path, [response("437"), cancel_review], message="What is 19×23?"
    )
    runtime.run(task, token)
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert store.usage("owner")["calls"] == 2
    assert not [
        m
        for m in store.conversation("owner", task["chat_id"])["messages"]
        if m["role"] == "assistant"
    ]


def test_unrecorded_dependency_can_be_repaired_with_a_real_pending_question(tmp_path):
    def initial(request):
        payload = json.loads(request["messages"][1]["content"])
        assert payload["current_clarifications"] == []
        return verdict(
            "repair", "Unfinished work needs an actual pending question for the sign-off."
        )

    def checked(request):
        payload = json.loads(request["messages"][1]["content"])
        state = payload["current_clarifications"]
        assert len(state) == 1 and state[0]["status"] == "pending"
        assert state[0]["question"] == "Which sign-off sentence should I use?"
        return verdict("ready")

    store, task, _, runtime, token = study(
        tmp_path,
        [
            response("I am waiting for your sign-off."),
            initial,
            response(
                calls=[
                    ToolCall(
                        "question",
                        "ask_clarification",
                        {"question": "Which sign-off sentence should I use?"},
                    )
                ]
            ),
            response("Which sign-off sentence should I use?"),
            checked,
        ],
        message="Prepare a draft and ask for my sign-off sentence before finishing it.",
    )
    runtime.run(task, token)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert runtime.clarifications.list(task["bot_id"])[0]["status"] == "pending"


def test_question_closed_during_review_discards_the_stale_waiting_reply(tmp_path):
    def close_during_review(request):
        runtime.clarifications.decide(
            task["bot_id"], q["id"], QuestionDecision(action="close", expected_revision=1)
        )
        return verdict("ready")

    store, task, _, runtime, token = study(
        tmp_path,
        [
            response("I am still waiting for your sign-off."),
            close_during_review,
            response("The question is closed. I have not assumed a sign-off."),
            verdict("ready"),
        ],
        message="Prepare a draft and ask for my sign-off.",
    )
    q = runtime.clarifications.ask(task, AskQuestion(question="Which sign-off?"))
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed" and final["calls"] == 4
    assert final["output"] == "The question is closed. I have not assumed a sign-off."
    replies = [
        m["content"]
        for m in store.conversation("owner", task["chat_id"])["messages"]
        if m["role"] == "assistant"
    ]
    assert replies == [final["output"]]


def test_repeated_repair_is_bounded_without_synthesizing_a_success_reply(tmp_path):
    store, task, model, runtime, token = study(
        tmp_path,
        [
            response("Saved."),
            verdict("repair", "No receipt."),
            response("Saved."),
            verdict("repair", "No receipt."),
            response("Saved."),
            verdict("repair", "No receipt."),
            response("Saved."),
            verdict("repair", "No receipt."),
            response("Saved."),
            verdict("repair", "No receipt."),
        ],
    )
    runtime.run(task, token)
    assert store.task("owner", task["id"])["status"] == "failed"
    # Three drafts, then a plain status and one retry of it, each reviewed; never more.
    assert len(model.requests) == 10
    assert not store.list_files("owner", task["bot_id"])


@pytest.mark.parametrize(
    "raw_calls, parsed, finish",
    [
        ([{"id": "dropped"}], [], "tool_calls"),
        ([], [], "tool_calls"),
        ("invalid", [], "stop"),
        (
            [{"id": "x", "function": {"name": "save_file", "arguments": "{bad"}}],
            [ToolCall("x", "save_file", {"_raw_arguments": "{bad"})],
            "tool_calls",
        ),
    ],
)
def test_malformed_provider_calls_are_not_silently_treated_as_replies(raw_calls, parsed, finish):
    result = response(
        "I've done it",
        parsed,
        {"choices": [{"finish_reason": finish, "message": {"tool_calls": raw_calls}}]},
    )
    with pytest.raises(LimitReached, match="incomplete action request"):
        validate_tool_response(result)


def test_dropped_call_stops_before_dispatch_and_is_accounted(tmp_path):
    valid = {
        "id": "a",
        "function": {"name": "save_file", "arguments": '{"name":"a","content":"b"}'},
    }
    store, task, _, runtime, token = study(
        tmp_path,
        [
            response(
                "Done",
                [ToolCall("a", "save_file", {"name": "a", "content": "b"})],
                {"choices": [{"message": {"tool_calls": [valid, {"id": "dropped"}]}}]},
            )
        ],
    )
    runtime.run(task, token)
    assert store.task("owner", task["id"])["status"] == "failed"
    assert not store.list_files("owner", task["bot_id"])
    assert store.usage("owner")["calls"] == 1
    assert store.usage("owner")["unconfirmed_calls"] == 0


def test_reply_repair_withdraws_unneeded_card_instead_of_only_hiding_it_in_text(tmp_path):
    def inspect_pending(request):
        payload = json.loads(request["messages"][1]["content"])
        assert payload["current_action_proposal"]["status"] == "pending"
        assert "withdraw_action" in payload["available_tools"]
        return verdict(
            "repair",
            "The greeting is done. Withdraw the unnecessary action proposal; omitting it from the answer leaves its card actionable.",
        )

    def inspect_withdrawn(request):
        payload = json.loads(request["messages"][1]["content"])
        assert payload["current_action_proposal"]["status"] == "outdated"
        return verdict("ready")

    store, task, model, runtime, token = study(
        tmp_path,
        [
            response(
                calls=[
                    ToolCall(
                        "propose",
                        "propose_action",
                        {
                            "question": "Say hello again?",
                            "instructions": "Repeat the greeting.",
                            "offline_computer": False,
                        },
                    )
                ]
            ),
            response("Hello!"),
            inspect_pending,
            response(calls=[ToolCall("withdraw", "withdraw_action", {})]),
            response("Hello!"),
            inspect_withdrawn,
        ],
        message="Say hello.",
    )
    runtime.run(task, token)
    assert store.task("owner", task["id"])["status"] == "completed"
    assert runtime.proposals.for_task(task)["status"] == "outdated"
    assert store.claim("owner") is None
    assert store.task("owner", task["id"])["output"] == "Hello!"


def test_resumed_background_browser_reply_still_requires_completion_review(tmp_path):
    store, task, model, runtime, token = study(
        tmp_path,
        [response("Work completed after handoff."), verdict("ready")],
        message="Inspect the current page after I give control back",
    )
    runtime.background.submit(
        task, "Waiting for human control", [{"role": "user", "content": task["message"]}]
    )
    resumed = runtime.background.claim()
    runtime.run(resumed, token, background=True)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed", final["error"]
    assert len(model.requests) == 2
    assert any(e["kind"] == "reply_review_started" for e in final["events"])


def test_one_transient_error_in_the_check_is_retried_once(tmp_path, monkeypatch):
    from alysis_code.bots import runtime as runtime_module

    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.01)

    def timeout(_):
        raise TimeoutError("read timed out")

    store, task, model, runtime, token = study(
        tmp_path,
        [response("Paris is the capital of France."), timeout, verdict("ready")],
        message="What is the capital of France?",
    )
    runtime.run(task, token)
    final = store.task("owner", task["id"])
    assert final["status"] == "completed"
    assert [e["kind"] for e in final["events"]].count("provider_retry") == 1
    assert store.usage("owner")["calls"] == 3
