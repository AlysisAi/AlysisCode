"""A routine's run that checked and found nothing new ends quietly.

The owl decides with a structured step (nothing_to_report), offered only in routine runs, never
from the words of its reply. Such a run completes with no reply, no unread conversation (so no
notification) and its routine's history says it checked, with the time. A run with something to
say still posts it, and a run that changed something, or needs what its routine doesn't allow,
can't end quietly. Real persistence and the real runtime loop; the model and the computers are
fixtures; routines and replies in Greek and Chinese.
"""

from __future__ import annotations

import json
import time

import pytest
from test_bots_browser_runtime import BrowserFixture, ClientFixture, advertised_names, response
from test_bots_desktop import Executor

from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_toolset import Turn, tool_names
from alysis_code.bots.routine_access import RoutineAccessDecision
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.step_effects import NOTHING_TO_REPORT
from alysis_code.bots.store import Store
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

OWNER = "owner"
SITE = "https://shop.example"
GREEK_JOB = "Έλεγξε την τιμή του εισιτηρίου και πες μου μόνο αν αλλάξει."
CHINESE_JOB = "检查机票价格，只有价格变化时才告诉我。"
CHINESE_REASON = "价格仍然是 120 欧元，没有变化。"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "quiet-routines.db")


@pytest.fixture
def bot(store):
    return store.save_bot("owner", BotInput(name="Ήρα", purpose="Παρακολούθηση τιμών"))["id"]


def routine(store, bot, prompt=GREEK_JOB, *, needs=None):
    item = store.save_schedule(
        OWNER,
        bot,
        ScheduleInput.model_validate(
            {
                "name": "Τιμή εισιτηρίου",
                "prompt": prompt,
                "timing": {"kind": "interval", "every_minutes": 60},
                **({"needs": needs} if needs is not None else {}),
            }
        ),
        now=time.time() - 3600,
    )
    current = next(s for s in store.list_schedules(OWNER, bot) if s["id"] == item["id"])
    if needs:
        # The person allows what its runs use, once, and it is on.
        store.allow_routine_access(
            OWNER,
            bot,
            item["id"],
            RoutineAccessDecision(
                expected_revision=current["revision"],
                allow=current["access"]["to_allow"],
                turn_on=True,
            ),
        )
        current = next(s for s in store.list_schedules(OWNER, bot) if s["id"] == item["id"])
    assert current["state"] == "active"
    return current


def start_run(store, item):
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=item["next_run_at"]) == 1
    return store.claim(OWNER)


def call(name, arguments, call_id="call-1"):
    return LLMResponse("", [ToolCall(call_id, name, arguments)], {}, usage=LLMUsage(10, 5, 15))


def quiet(reason=CHINESE_REASON):
    return call(NOTHING_TO_REPORT, {"reason": reason})


def read_everything(store, bot):
    chat = store.list_chats(OWNER, bot)[0]
    store.mark_chat_read(OWNER, chat["id"], chat["activity_seq"])


def owl(store, bot):
    return next(b for b in store.list_bots(OWNER) if b["id"] == bot)


def last_run(store, bot, item):
    return next(s for s in store.list_schedules(OWNER, bot) if s["id"] == item["id"])["last_run"]


def tool_results(client):
    return [
        json.loads(m["content"])
        for m in client.requests[-1]["messages"]
        if m["role"] == "tool" and m["content"].startswith("{")
    ]


def say_hello(store, bot):
    """An earlier turn the person read, so the conversation starts read."""
    chat = store.list_chats(OWNER, bot)[0] if store.list_chats(OWNER, bot) else None
    chat = chat or store.create_chat(OWNER, bot, "Συζήτηση")
    task = store.enqueue(OWNER, chat["id"], TaskInput(message="Γεια", request_id="hello-request-1"))
    store.claim(OWNER)
    store.finish(OWNER, task["id"], "completed", content="Γεια σου!")
    read_everything(store, bot)


# ---------------------------------------------------------------------------------------------
# Nothing new: no reply, nothing unread, and the history says it checked


def test_a_run_with_nothing_new_ends_without_a_reply_or_a_notification(store, bot):
    say_hello(store, bot)
    item = routine(store, bot)
    task = start_run(store, item)
    client = ClientFixture([quiet()])
    runtime = BotRuntime(store, OWNER, lambda _: client)

    runtime.run(task, runtime.task_token(task["id"]))

    # The owl was offered the step, and one model call ended the run.
    assert NOTHING_TO_REPORT in advertised_names(client.requests[0])
    assert len(client.requests) == 1
    done = store.task(OWNER, task["id"])
    assert done["status"] == "completed" and done["output"] == "" and done["error"] is None
    kinds = [event["kind"] for event in done["events"]]
    assert kinds[-3:] == ["tool_completed", "nothing_new", "completed"]
    assert done["events"][-2]["data"] == {"reason": CHINESE_REASON}
    assert done["events"][-1]["data"] == {"nothing_new": True}

    # No reply in the conversation: only the routine's own request and the earlier hello.
    chat = store.list_chats(OWNER, bot)[0]
    conversation = store.conversation(OWNER, chat["id"])
    assert [m["role"] for m in conversation["messages"] if m["task_id"] == task["id"]] == ["user"]
    run_task = next(t for t in conversation["tasks"] if t["id"] == task["id"])
    assert run_task["nothing_new_at"] == done["events"][-2]["created_at"]
    # Nothing new to read: the person is not called to it (the app notifies on unread replies).
    assert conversation["unread"] is False
    assert owl(store, bot)["unread_chats"] == 0
    assert owl(store, bot)["latest_activity"]["nothing_new_at"] == run_task["nothing_new_at"]

    # The routine's history and its last run say it checked, with the time.
    runs = store.schedule_runs(OWNER, bot, item["id"])["runs"]
    assert runs[0]["task_id"] == task["id"] and runs[0]["current_status"] == "completed"
    assert runs[0]["nothing_new_at"] == run_task["nothing_new_at"]
    assert last_run(store, bot, item)["nothing_new_at"] == run_task["nothing_new_at"]
    # A quiet run is a run that worked: the routine stays on with nothing to retry.
    current = next(s for s in store.list_schedules(OWNER, bot) if s["id"] == item["id"])
    assert current["state"] == "active" and current["retry"] is None


def test_a_run_with_something_new_still_posts_and_is_unread(store, bot):
    say_hello(store, bot)
    item = routine(store, bot, CHINESE_JOB)
    task = start_run(store, item)
    client = ClientFixture([response("价格从 120 欧元降到了 95 欧元。")])
    runtime = BotRuntime(store, OWNER, lambda _: client)

    runtime.run(task, runtime.task_token(task["id"]))

    done = store.task(OWNER, task["id"])
    assert done["status"] == "completed" and done["output"] == "价格从 120 欧元降到了 95 欧元。"
    assert "nothing_new" not in [event["kind"] for event in done["events"]]
    assert owl(store, bot)["unread_chats"] == 1
    assert "nothing_new_at" not in owl(store, bot)["latest_activity"]
    assert store.schedule_runs(OWNER, bot, item["id"])["runs"][0]["nothing_new_at"] is None


def test_an_earlier_unread_reply_stays_unread(store, bot):
    """Only a conversation the person had read stays read: an earlier reply is never hidden."""
    item = routine(store, bot)
    first = start_run(store, item)
    store.finish(OWNER, first["id"], "completed", content="Η τιμή έπεσε στα 95 €.")
    second = start_run(store, next(s for s in store.list_schedules(OWNER, bot)))
    client = ClientFixture([quiet()])
    runtime = BotRuntime(store, OWNER, lambda _: client)

    runtime.run(second, runtime.task_token(second["id"]))

    assert store.task(OWNER, second["id"])["status"] == "completed"
    assert owl(store, bot)["unread_chats"] == 1


def test_what_the_run_keeps_for_next_time_does_not_stop_a_quiet_end(store, bot):
    """Reading and the owl's own notes change nothing outside it, so the run can end quietly."""
    item = routine(store, bot)
    task = start_run(store, item)
    client = ClientFixture(
        [
            call("list_files", {}, "call-1"),
            call(
                "save_working_note",
                {"title": "Τιμή εισιτηρίου", "content": "120 €", "expected_revision": 0},
                "call-2",
            ),
            quiet(),
        ]
    )
    runtime = BotRuntime(store, OWNER, lambda _: client)

    runtime.run(task, runtime.task_token(task["id"]))

    assert store.task(OWNER, task["id"])["status"] == "completed"
    assert store.task(OWNER, task["id"])["output"] == ""
    assert [n["title"] for n in store.list_notes(OWNER, bot)] == ["Τιμή εισιτηρίου"]


def test_the_next_turn_reads_a_quiet_run_as_a_check_not_an_unanswered_request(store, bot):
    item = routine(store, bot)
    task = start_run(store, item)
    runtime = BotRuntime(store, OWNER, lambda _: ClientFixture([quiet()]))
    runtime.run(task, runtime.task_token(task["id"]))

    chat = store.list_chats(OWNER, bot)[0]
    follow = store.enqueue(
        OWNER, chat["id"], TaskInput(message="Τι βρήκες;", request_id="ask-request-1")
    )
    claimed = store.claim(OWNER)
    assert claimed["id"] == follow["id"]
    client = ClientFixture([response("Τίποτα νέο ακόμα.")])
    BotRuntime(store, OWNER, lambda _: client).run(claimed, runtime.task_token(claimed["id"]))

    records = [
        m["content"]
        for m in client.requests[0]["messages"]
        if m["role"] == "user" and task["id"] in str(m["content"])
    ]
    assert len(records) == 1 and records[0].startswith("Historical task record")
    assert "nothing new to report" in records[0] and GREEK_JOB in records[0]
    # The next routine run is offered the step again; the chat turn was not.
    assert NOTHING_TO_REPORT not in advertised_names(client.requests[0])


# ---------------------------------------------------------------------------------------------
# Only routine runs, only alone, and never after a step that changed something


def test_the_step_is_offered_only_in_routine_runs(store, bot):
    assert NOTHING_TO_REPORT in tool_names(Turn(answers_person=False, scheduled=True))
    assert NOTHING_TO_REPORT in tool_names(
        Turn(answers_person=False, scheduled=True, background=True)
    )
    for turn in (
        Turn(answers_person=True),
        Turn(answers_person=True, room=True),
        Turn(answers_person=False, background=True),
        Turn(answers_person=False, activation=True),
    ):
        assert NOTHING_TO_REPORT not in tool_names(turn)

    # A chat turn that calls it anyway gets an error and still replies to the person.
    chat = store.create_chat(OWNER, bot, "Συζήτηση")
    store.enqueue(OWNER, chat["id"], TaskInput(message=CHINESE_JOB, request_id="chat-request-1"))
    task = store.claim(OWNER)
    client = ClientFixture([quiet(), response("好的，我会检查。")])
    runtime = BotRuntime(store, OWNER, lambda _: client)
    runtime.run(task, runtime.task_token(task["id"]))
    assert NOTHING_TO_REPORT not in advertised_names(client.requests[0])
    # Named as not offered (tool_errors.ToolNotOffered): nothing ran.
    [result] = tool_results(client)
    assert result["executed"] is False
    assert result["error"].startswith(f"The tool {NOTHING_TO_REPORT} is not offered now")
    offered = result["error"].split("Tools you can call now: ", 1)[1].rstrip(".").split(", ")
    assert NOTHING_TO_REPORT not in offered and "web_search" in offered
    done = store.task(OWNER, task["id"])
    assert done["status"] == "completed" and done["output"] == "好的，我会检查。"
    assert "nothing_new" not in [event["kind"] for event in done["events"]]


def test_a_finished_task_that_is_not_a_routine_run_cannot_end_with_nothing_new(store, bot):
    chat = store.create_chat(OWNER, bot, "Συζήτηση")
    task = store.enqueue(OWNER, chat["id"], TaskInput(message="Γεια", request_id="chat-request-2"))
    store.claim(OWNER)
    with pytest.raises(ValueError):
        store.finish(OWNER, task["id"], "completed", nothing_new=CHINESE_REASON)
    assert store.task(OWNER, task["id"])["status"] == "running"


def test_it_must_be_called_alone(store, bot):
    item = routine(store, bot)
    task = start_run(store, item)
    both = LLMResponse(
        "",
        [
            ToolCall("call-1", "list_files", {}),
            ToolCall("call-2", NOTHING_TO_REPORT, {"reason": CHINESE_REASON}),
        ],
        {},
        usage=LLMUsage(10, 5, 15),
    )
    client = ClientFixture([both, response("Η τιμή είναι ίδια: 120 €.")])
    runtime = BotRuntime(store, OWNER, lambda _: client)

    runtime.run(task, runtime.task_token(task["id"]))

    assert all(not r.get("executed", True) for r in tool_results(client))
    done = store.task(OWNER, task["id"])
    assert done["output"] == "Η τιμή είναι ίδια: 120 €."
    assert not [e for e in done["events"] if e["kind"] in ("tool_started", "nothing_new")]


def test_a_run_that_acted_on_a_website_must_say_what_it_did(store, bot):
    item = routine(store, bot, needs={"sites": [SITE]})
    task = start_run(store, item)
    browser = BrowserFixture()
    client = ClientFixture(
        [
            response(name="browser_click", arguments={"target": "Κράτηση"}),
            quiet(),
            response("Έκανα την κράτηση στα 95 €."),
        ]
    )
    runtime = BotRuntime(store, OWNER, lambda _: client, browser_computers=browser)

    runtime.run(task, runtime.task_token(task["id"]))

    refusal = tool_results(client)[-1]
    assert refusal["ended"] is False and refusal["step"] == "browser_click"
    done = store.task(OWNER, task["id"])
    assert done["status"] == "completed" and done["output"] == "Έκανα την κράτηση στα 95 €."
    assert "nothing_new" not in [event["kind"] for event in done["events"]]
    assert owl(store, bot)["unread_chats"] == 1


def test_a_run_that_needs_more_than_its_routine_allows_must_say_so(store, bot):
    item = routine(store, bot)
    task = start_run(store, item)
    client = ClientFixture(
        [
            call(
                "propose_action",
                {
                    "question": "需要打开网站吗？",
                    "instructions": "打开航空公司网站查看价格。",
                    "offline_computer": False,
                    "browser_origin": SITE,
                },
            ),
            quiet(),
            response("这个例行任务需要你允许访问该网站。"),
        ]
    )
    runtime = BotRuntime(store, OWNER, lambda _: client, browser_computers=BrowserFixture())

    runtime.run(task, runtime.task_token(task["id"]))

    assert tool_results(client)[-1]["ended"] is False
    assert store.task(OWNER, task["id"])["output"] == "这个例行任务需要你允许访问该网站。"
    paused = next(s for s in store.list_schedules(OWNER, bot) if s["id"] == item["id"])
    assert paused["state"] == "paused" and paused["pause_code"] == "access"


# ---------------------------------------------------------------------------------------------
# Work on the owl's computer ends quietly the same way


def test_a_run_on_the_owls_computer_ends_quietly_too(store, bot):
    say_hello(store, bot)
    item = routine(store, bot, CHINESE_JOB, needs={"offline_computer": True})
    task = start_run(store, item)
    manager = DesktopComputers(OWNER, ComputerViews(), factory=Executor)
    client = ClientFixture([quiet(), response("不应该发送。")])
    runtime = BotRuntime(store, OWNER, lambda _: client, desktop_computers=manager)
    try:
        runtime.run(task, runtime.task_token(task["id"]))
        queued = runtime.background.claim()
        assert queued is not None and queued["id"] == task["id"]
        runtime.run(queued, runtime.task_token(queued["id"]), background=True)
    finally:
        manager.close()

    assert NOTHING_TO_REPORT in advertised_names(client.requests[0])
    done = store.task(OWNER, task["id"])
    assert done["status"] == "completed" and done["output"] == ""
    assert "nothing_new" in [event["kind"] for event in done["events"]]
    assert owl(store, bot)["unread_chats"] == 0
    assert store.schedule_runs(OWNER, bot, item["id"])["runs"][0]["nothing_new_at"] is not None
