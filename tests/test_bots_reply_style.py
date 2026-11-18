from __future__ import annotations

import json
import threading

import pytest

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_style import (
    ACK_REMINDER,
    MAX_FOLLOW_UPS,
    MILESTONE_REMINDER,
    PLAN_REMINDER,
    TurnUpdates,
    progress_note,
    split_follow_ups,
    update_lines,
)
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall


def test_follow_ups_are_removed_from_the_reply_and_cleaned():
    body, items = split_follow_ups(
        "Here is the plan.\n\n<follow_ups>\n- Make it shorter\n2. “Add a timeline”\n"
        "* make it shorter\n- Use <placeholder>\n-\n</follow_ups>"
    )
    assert body == "Here is the plan."
    assert items == ["Make it shorter", "Add a timeline"]


def test_follow_ups_keep_the_last_block_and_cap_the_count():
    body, items = split_follow_ups(
        "Done.<follow_ups>- Old</follow_ups>\n<FOLLOW_UPS>\n- One\n- Two\n- Three\n- Four\n</FOLLOW_UPS>"
    )
    assert body == "Done."
    assert items == ["One", "Two", "Three"][:MAX_FOLLOW_UPS]


def test_unterminated_follow_ups_block_never_reaches_the_reply():
    body, items = split_follow_ups("Saved the brief.\n<follow_ups>\n- Shorten the intro")
    assert body == "Saved the brief."
    assert items == ["Shorten the intro"]


def test_reply_without_follow_ups_is_unchanged():
    assert split_follow_ups("  Plain reply.\n") == ("  Plain reply.\n", [])


@pytest.mark.parametrize("text", [None, "", "<follow_ups>\n- Next\n</follow_ups>"])
def test_a_reply_with_no_text_splits_to_an_empty_body(text):
    body, _ = split_follow_ups(text)
    assert body == ""


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Checking this week's numbers now.", "Checking this week's numbers now."),
        ("  Let me check\n what I can   create. ", "Let me check what I can create."),
        ("Looking now.<follow_ups>- Next</follow_ups>", "Looking now."),
        ("", None),
        (None, None),
        ("x" * 241, None),
        ("<think>private</think>", None),
        ("Draft:\n```\ncode\n```", None),
    ],
)
def test_progress_note_shows_only_short_status_lines(text, expected):
    assert progress_note(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "Ψάχνω τώρα στο Ναύπλιο.\n---\nΘα πάρεις μια σύντομη λίστα με τιμές.",
            ["Ψάχνω τώρα στο Ναύπλιο.", "Θα πάρεις μια σύντομη λίστα με τιμές."],
        ),
        ("On it.\n  -----  \n\n---\nYou'll get a table.", ["On it.", "You'll get a table."]),
        ("Checking now.\n---\n" + "x" * 241, ["Checking now."]),
        ("Step one - step two", ["Step one - step two"]),
        ("", []),
    ],
)
def test_an_update_becomes_one_chat_message_per_part(text, expected):
    assert update_lines(text) == expected


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_updates_follow_the_work_as_it_goes():
    clock = Clock()
    updates = TurnUpdates(watched=True, started_at=clock.now, clock=clock)
    assert updates.reminder(0) == ACK_REMINDER
    assert updates.take("Συγκρίνω τιμές σε δύο καταστήματα.", 0) == (
        "ack",
        ["Συγκρίνω τιμές σε δύο καταστήματα."],
    )
    # Quick work never gets the second message, and narration beside a step stays private.
    clock.now += 5
    assert updates.reminder(1) is None
    assert updates.take("The parameter is selector, not ref.", 1) == (None, [])
    clock.now += TurnUpdates.PLAN_AFTER_SECONDS
    assert updates.reminder(2) == PLAN_REMINDER
    assert updates.take("You'll get both prices side by side.", 2) == (
        "plan",
        ["You'll get both prices side by side."],
    )
    # A milestone needs a quiet stretch and new steps.
    clock.now += TurnUpdates.QUIET_SECONDS - 1
    assert updates.reminder(9) is None
    clock.now += 1
    assert updates.reminder(3) is None
    assert updates.reminder(9) == MILESTONE_REMINDER
    assert updates.take("Δύο από τα τρία καταστήματα έτοιμα.", 9) == (
        "milestone",
        ["Δύο από τα τρία καταστήματα έτοιμα."],
    )
    assert updates.reminder(20) is None


def test_milestones_are_capped_and_a_silent_one_waits_another_stretch():
    clock = Clock()
    updates = TurnUpdates(watched=True, started_at=clock.now, said=["ack", "plan"], clock=clock)
    steps = 0
    asked = 0
    for _ in range(10):
        clock.now += TurnUpdates.QUIET_SECONDS
        steps += TurnUpdates.STEPS_BETWEEN_MILESTONES
        if updates.reminder(steps) == MILESTONE_REMINDER:
            asked += 1
            assert updates.take("", steps) == ("milestone", [])
            assert updates.reminder(steps) is None  # nothing said: wait for another stretch
    assert asked == TurnUpdates.MAX_MILESTONES


def test_a_call_that_offers_no_tools_asks_for_no_update():
    clock = Clock()
    updates = TurnUpdates(watched=True, started_at=clock.now, clock=clock)
    assert updates.reminder(0, tools_offered=False) is None
    assert updates.take("Η απάντηση.", 0) == (None, [])
    assert updates.reminder(0) == ACK_REMINDER


def test_a_card_keeps_the_update_due_and_unwatched_turns_get_none():
    clock = Clock()
    updates = TurnUpdates(watched=True, started_at=clock.now, clock=clock)
    assert updates.reminder(0) == ACK_REMINDER
    assert updates.take("Which tone do you want?", 0, card=True) == (None, [])
    assert updates.reminder(0) == ACK_REMINDER
    quiet = TurnUpdates(watched=False, started_at=clock.now, clock=clock)
    clock.now += 1000
    assert quiet.reminder(50) is None
    assert quiet.take("Working on it.", 50) == (None, [])
    resumed = TurnUpdates(watched=True, started_at=clock.now - 60, said=["ack"], clock=clock)
    assert resumed.reminder(1) == PLAN_REMINDER


class ScriptedClient:
    model = "test-model"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"])))
        return next(self.responses)


def response(content="Done", calls=()):
    return LLMResponse(content, list(calls), {}, usage=LLMUsage(10, 5, 15))


def run_task(tmp_path, responses, message="Write my notes down"):
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Atlas", purpose="Help organize notes"))
    chat = store.create_chat("alice", bot["id"], "New conversation")
    store.enqueue("alice", chat["id"], TaskInput(message=message, request_id="request_1"))
    task = store.claim("alice")
    client = ScriptedClient(responses)
    BotRuntime(store, "alice", lambda _: client).run(
        task, EventCancellationToken(threading.Event())
    )
    return store, chat, store.task("alice", task["id"]), client


def save_call(call_id="save", name="notes.md"):
    return ToolCall(call_id, "save_file", {"name": name, "content": "Notes"})


def test_first_status_line_and_suggestions_travel_beside_the_reply(tmp_path):
    store, chat, task, client = run_task(
        tmp_path,
        [
            response("Saving your notes now.", [save_call()]),
            response("Also tidying the title.", [save_call("again", "notes-2.md")]),
            response(
                "Saved notes.md.\n<follow_ups>\n- Add a summary\n- Make it shorter\n</follow_ups>"
            ),
        ],
    )
    assert task["status"] == "completed"
    kinds = [e["kind"] for e in task["events"]]
    # Only the first line was asked for; text beside the next step stays private.
    assert kinds.count("progress_note") == 1
    assert kinds.index("progress_note") < kinds.index("tool_started")
    note = next(e["data"] for e in task["events"] if e["kind"] == "progress_note")
    assert note == {"text": "Saving your notes now.", "kind": "ack"}
    suggestions = next(e["data"] for e in task["events"] if e["kind"] == "reply_suggestions")
    assert suggestions == {"items": ["Add a summary", "Make it shorter"]}
    conversation = store.conversation("alice", chat["id"])
    assert conversation["messages"][-1]["content"] == "Saved notes.md."
    assert conversation["tasks"][-1]["progress_note"] == "Saving your notes now."
    assert conversation["tasks"][-1]["progress_notes"] == ["Saving your notes now."]
    system = client.requests[0][0]["content"]
    assert "How to write the reply the user reads" in system
    assert "<follow_ups>" in system
    assert "Short updates while you work" in system
    reminder = {"role": "system", "content": ACK_REMINDER}
    assert client.requests[0][-1] == reminder
    assert all(reminder not in request for request in client.requests[1:])


def test_tool_call_without_text_or_with_a_card_posts_no_status_line(tmp_path):
    store, chat, quiet, _ = run_task(
        tmp_path / "quiet", [response("", [save_call()]), response("Saved.")]
    )
    assert not any(e["kind"] == "progress_note" for e in quiet["events"])
    assert store.conversation("alice", chat["id"])["tasks"][-1]["progress_note"] is None
    assert store.conversation("alice", chat["id"])["tasks"][-1]["progress_notes"] == []
    assert not any(e["kind"] == "reply_suggestions" for e in quiet["events"])
    _, _, asked, _ = run_task(
        tmp_path / "asked",
        [
            response(
                "Asking which tone you want.",
                [
                    ToolCall(
                        "ask",
                        "ask_clarification",
                        {"question": "Which tone?", "options": ["Formal", "Casual"]},
                    )
                ],
            ),
            response("The tone decides the wording."),
        ],
    )
    assert not any(e["kind"] == "progress_note" for e in asked["events"])


def test_a_routine_run_asks_for_no_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(Store, "schedule_context", lambda self, owner, task_id: {"id": "r"})
    _, _, task, client = run_task(
        tmp_path, [response("Saving the report.", [save_call()]), response("Saved.")]
    )
    assert not any(e["kind"] == "progress_note" for e in task["events"])
    assert not any(
        message.get("content") in (ACK_REMINDER, PLAN_REMINDER, MILESTONE_REMINDER)
        for request in client.requests
        for message in request
    )
