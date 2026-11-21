"""One read lists everything waiting for the person across their owls."""

from __future__ import annotations

import json
import time
from uuid import uuid4

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Store


def setup(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    owls = []
    for name in ("Έλενα", "花子"):
        bot = store.save_bot("alice", BotInput(name=name))
        owls.append((bot, store.create_chat("alice", bot["id"], name)))
    return store, owls


def send(store, chat, text):
    task = store.enqueue(
        "alice", chat["id"], TaskInput(message=text, request_id="req_" + uuid4().hex)
    )
    assert store.claim("alice")["id"] == task["id"]
    return task


def kinds(store):
    return [(item["kind"], item["bot_name"]) for item in store.needs_you("alice")["items"]]


def background(store, task, state):
    with store.connect(write=True) as db:
        db.execute("UPDATE tasks SET status='waiting' WHERE id=?", (task["id"],))
        db.execute("INSERT INTO background_turns VALUES(?,?,'x','[]',0)", (task["id"], state))


def test_nothing_waits_on_a_new_account(tmp_path):
    store, _ = setup(tmp_path)
    assert store.needs_you("alice") == {"items": [], "count": 0}


def test_a_question_waits_once_the_message_asking_it_is_out(tmp_path):
    from alysis_code.bots.clarifications import AskQuestion, ClarificationStore

    store, [(bot, chat), _] = setup(tmp_path)
    task = send(store, chat, "Γράψε το email")
    question = ClarificationStore(store, "alice").ask(
        store.task("alice", task["id"]), AskQuestion(question="Ποιο θέμα να μπει;", options=[])
    )
    assert kinds(store) == []
    store.finish("alice", task["id"], "completed", content="Ποιο θέμα να μπει;")
    [item] = store.needs_you("alice")["items"]
    assert item["kind"] == "question" and item["text"] == "Ποιο θέμα να μπει;"
    assert item["id"] == question["id"] and item["task_id"] == task["id"]
    assert item["bot_id"] == bot["id"] and item["bot_name"] == "Έλενα"


def test_an_approval_lists_its_action_without_the_screenshot(tmp_path):
    store, [(bot, chat), _] = setup(tmp_path)
    task = send(store, chat, "Στείλε το")
    action = {
        "tool": "browser_click",
        "arguments": {"ref": "e4"},
        "observed_context": {"label": "送信", "screenshot": "data:image/png;base64,AAAA"},
    }
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO bot_action_reviews(id,bot_id,task_id,action,generation,status,revision,"
            "created_at,decided_at,reason) VALUES(?,?,?,?,'g','pending',1,?,NULL,'')",
            (uuid4().hex, bot["id"], task["id"], json.dumps(action), time.time()),
        )
    [item] = store.needs_you("alice")["items"]
    assert item["kind"] == "approval"
    assert item["action"] == {
        "tool": "browser_click",
        "arguments": {"ref": "e4"},
        "observed_context": {"label": "送信"},
    }
    store.finish("alice", task["id"], "cancelled")
    assert kinds(store) == []


def test_a_suggestion_waits_under_its_reply_for_the_owls_current_setup(tmp_path):
    store, [(bot, chat), _] = setup(tmp_path)
    task = send(store, chat, "Τι λες;")
    store.finish("alice", task["id"], "completed", content="Να το κάνω κάθε Δευτέρα;")
    now = time.time()
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO action_proposals(id,bot_id,source_task_id,anchor_task_id,question,"
            "instructions,status,revision,bot_updated_at,visible_after,task_id,created_at,"
            "updated_at) VALUES(?,?,?,?,?,'x','pending',1,?,?,NULL,?,?)",
            (
                uuid4().hex,
                bot["id"],
                task["id"],
                task["id"],
                "Κάθε Δευτέρα;",
                bot["updated_at"],
                now - 1,
                now,
                now,
            ),
        )
    assert [(i["kind"], i["text"]) for i in store.needs_you("alice")["items"]] == [
        ("proposal", "Κάθε Δευτέρα;")
    ]
    # A suggestion made under an earlier setup is out of date.
    store.save_bot("alice", BotInput(name="Έλενα", purpose="Νέος σκοπός"), bot["id"])
    assert kinds(store) == []


def test_help_on_its_computer_and_approval_on_this_computer_wait_for_the_person(tmp_path):
    store, [(bot, chat), (other, other_chat)] = setup(tmp_path)
    control = send(store, chat, "Συνδέσου")
    background(store, control, "computer_control")
    store.event("alice", control["id"], "computer_control_requested", {"message": "ログインして"})
    local = send(store, other_chat, "Άνοιξε το αρχείο")
    background(store, local, "local_computer")
    request = uuid4().hex
    with store.connect(write=True) as db:
        db.execute(
            "INSERT INTO bot_local_devices VALUES(?,'alice','PC','windows','s',1,?)",
            ("d" * 32, time.time()),
        )
        db.execute(
            "INSERT INTO bot_local_requests VALUES(?,?,?,?,'run','{}','g','pending',NULL,NULL,?)",
            (request, other["id"], local["id"], "d" * 32, time.time()),
        )
    items = {item["kind"]: item for item in store.needs_you("alice")["items"]}
    assert items["computer_control"]["text"] == "ログインして"
    assert items["computer_control"]["bot_name"] == "Έλενα"
    assert items["local_approval"]["task_id"] == local["id"]
    assert items["local_approval"]["bot_name"] == "花子"
    # Once this computer took the request, the owl waits for its result, not for the person.
    with store.connect(write=True) as db:
        db.execute("UPDATE bot_local_requests SET status='claimed' WHERE id=?", (request,))
    assert [item["kind"] for item in store.needs_you("alice")["items"]] == ["computer_control"]


def test_routines_that_paused_themselves_or_wait_to_be_turned_on(tmp_path):
    store, [(bot, chat), _] = setup(tmp_path)
    timing = {"kind": "interval", "every_minutes": 60}
    failed = store.save_schedule(
        "alice", bot["id"], ScheduleInput(name="Αναφορά", prompt="x", timing=timing)
    )
    mine = store.save_schedule(
        "alice", bot["id"], ScheduleInput(name="Δική μου", prompt="x", timing=timing)
    )
    store.decide_schedule("alice", bot["id"], mine["id"], "paused", mine["revision"])
    task = send(store, chat, "Φτιάξε ρουτίνα")
    suggested = store.save_schedule(
        "alice",
        bot["id"],
        ScheduleInput(name="毎朝", prompt="x", timing=timing),
        source_task_id=task["id"],
    )
    assert suggested["state"] == "draft"
    with store.connect(write=True) as db:
        db.execute(
            "UPDATE schedules SET state='paused',pause_reason='A run failed.' WHERE id=?",
            (failed["id"],),
        )
    # The suggestion waits until the reply suggesting it is out.
    assert [(i["text"], i["state"]) for i in store.needs_you("alice")["items"]] == [
        ("Αναφορά", "paused")
    ]
    store.finish("alice", task["id"], "completed", content="Έτοιμη")
    listed = sorted(
        (i["kind"], i["routine"], i["text"], i["state"]) for i in store.needs_you("alice")["items"]
    )
    assert listed == [
        ("routine", "schedule", "Αναφορά", "paused"),
        ("routine", "schedule", "毎朝", "draft"),
    ]


def test_a_failed_turn_waits_until_seen_or_followed_by_another(tmp_path):
    store, [(bot, chat), (other, other_chat)] = setup(tmp_path)
    task = send(store, chat, "Κλείσε το ραντεβού")
    store.finish("alice", task["id"], "failed", error="Example failure")
    [item] = store.needs_you("alice")["items"]
    assert (item["kind"], item["state"], item["text"]) == (
        "failed",
        "failed",
        "Κλείσε το ραντεβού",
    )
    seq = store.conversation("alice", chat["id"])["activity_seq"]
    store.mark_chat_read("alice", chat["id"], seq)
    assert kinds(store) == []

    interrupted = send(store, other_chat, "長い仕事")
    store.finish("alice", interrupted["id"], "interrupted")
    assert [(i["kind"], i["state"]) for i in store.needs_you("alice")["items"]] == [
        ("failed", "interrupted")
    ]
    newer = send(store, other_chat, "Ξανά")
    store.finish("alice", newer["id"], "completed", content="OK")
    assert kinds(store) == []


def test_a_failed_routine_run_shows_only_once_its_routine_needs_the_person(tmp_path):
    from alysis_code.bots.scheduling import RunFailure

    store, [(bot, _), _] = setup(tmp_path)
    store.save_schedule(
        "alice",
        bot["id"],
        ScheduleInput(
            name="Νυχτερινή", prompt="x", timing={"kind": "interval", "every_minutes": 5}
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules("alice", max_calls=None, now=1301) == 1
    run = store.claim("alice")
    # A run that will be tried again needs nothing from the person yet.
    store.finish("alice", run["id"], "failed", error="Example failure")
    assert store.needs_you("alice")["items"] == []
    assert store.enqueue_due_schedules("alice", max_calls=None, now=1601) == 1
    run = store.claim("alice")
    # One only the person can fix pauses the routine, and the routine is what they see, once.
    store.finish(
        "alice", run["id"], "failed", error="Example failure", routine_failure=RunFailure("sign_in")
    )
    assert [(i["kind"], i["text"]) for i in store.needs_you("alice")["items"]] == [
        ("routine", "Νυχτερινή")
    ]


def test_items_are_newest_first_and_only_the_owners(tmp_path):
    from alysis_code.bots.clarifications import AskQuestion, ClarificationStore

    store, [(bot, chat), (other, other_chat)] = setup(tmp_path)
    first = send(store, chat, "Πρώτο")
    store.finish("alice", first["id"], "failed", error="x")
    second = send(store, other_chat, "二番目")
    ClarificationStore(store, "alice").ask(
        store.task("alice", second["id"]), AskQuestion(question="どれ？", options=[])
    )
    store.finish("alice", second["id"], "completed", content="どれ？")
    assert kinds(store) == [("question", "花子"), ("failed", "Έλενα")]
    stranger = store.save_bot("mallory", BotInput(name="Ξένος"))
    stranger_chat = store.create_chat("mallory", stranger["id"], "x")
    task = store.enqueue(
        "mallory", stranger_chat["id"], TaskInput(message="x", request_id="req_" + uuid4().hex)
    )
    store.claim("mallory")
    store.finish("mallory", task["id"], "failed", error="x")
    assert len(store.needs_you("alice")["items"]) == 2
    assert [i["kind"] for i in store.needs_you("mallory")["items"]] == ["failed"]
