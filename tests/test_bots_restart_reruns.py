"""A turn an app restart cut off starts again once, when nothing it did reached outside the owl.

After a crash, an update or sleep (Store.recover), or while the runtime stops under it
(Store.restart_cut), a turn whose step receipts show only reading and the owl's own records
queues once more, told it restarted and which steps it took. A turn with a step that changed
something outside them, or may have, is interrupted as before and its failure names that step;
a second cut is never started again. Decided from receipts, never from words. Real persistence
and the real runtime loop; the model is a fixture; requests in Greek and Chinese.
"""

from __future__ import annotations

import threading
import time

import pytest
from test_bots_browser_runtime import ClientFixture, response

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Store
from alysis_code.cancellation import CooperativeCancellationError

OWNER = "owner"
GREEK = "Βρες τις πτήσεις για Θεσσαλονίκη και κράτα τις σε ένα αρχείο."
CHINESE = "查一下去上海的航班，然后告诉我最便宜的。"
GREEK_FILE = "Πτήσεις Θεσσαλονίκη.md"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "restarts.db")


@pytest.fixture
def bot(store):
    return store.save_bot(OWNER, BotInput(name="Ερμής", purpose="Ταξίδια"))["id"]


def send(store, bot, message, request_id):
    chats = store.list_chats(OWNER, bot)
    chat = chats[0] if chats else store.create_chat(OWNER, bot, "Ταξίδια")
    return store.enqueue(OWNER, chat["id"], TaskInput(message=message, request_id=request_id))


def step(store, task_id, name, *, done=True, **data):
    """A step's receipts, as the runtime records them."""
    store.event(OWNER, task_id, "tool_started", {"name": name, **data})
    if done:
        store.event(
            OWNER,
            task_id,
            "tool_completed",
            {"name": name, **({"effect": data["effect"]} if "effect" in data else {})},
        )


def note_of(client):
    return "\n".join(
        str(m["content"])
        for m in client.requests[0]["messages"]
        if m["role"] == "system" and "interrupted by a restart" in str(m["content"])
    )


# ---------------------------------------------------------------------------------------------
# Only reading and the owl's own records: it starts again, once, told what it did


def test_a_turn_that_only_read_and_kept_its_records_starts_again_once_told_so(store, bot):
    task = send(store, bot, GREEK, "greek-request-1")
    assert store.claim(OWNER)["id"] == task["id"]
    step(store, task["id"], "web_search", topic="πτήσεις Θεσσαλονίκη")
    step(store, task["id"], "web_fetch", host="flights.example")
    step(store, task["id"], "save_file", file=GREEK_FILE)
    step(store, task["id"], "save_working_note")
    # A model call was in flight when the app went away.
    store.reserve_call(OWNER, task["id"], "fixture", None)

    assert Store(store.path).recover(OWNER) == 1

    queued = store.task(OWNER, task["id"])
    assert queued["status"] == "queued" and queued["restarts"] == 1
    assert queued["events"][-1]["kind"] == "restarted"
    assert queued["usage"][0]["status"] == "unknown"
    claimed = store.claim(OWNER)
    assert claimed["id"] == task["id"]
    client = ClientFixture([response("Οι πτήσεις είναι στο αρχείο.")])
    runtime = BotRuntime(store, OWNER, lambda _: client)
    runtime.run(claimed, runtime.task_token(claimed["id"]))

    done = store.task(OWNER, task["id"])
    assert done["status"] == "completed" and done["output"] == "Οι πτήσεις είναι στο αρχείο."
    # Told it restarted, with its steps in their own words and script.
    note = note_of(client)
    assert note and "Check what you already did before acting" in note
    assert GREEK_FILE in note and "πτήσεις Θεσσαλονίκη" in note
    assert note.index("web_search") < note.index("save_file")
    # A later turn is not told.
    later = send(store, bot, CHINESE, "chinese-request-1")
    claimed = store.claim(OWNER)
    assert claimed["id"] == later["id"]
    next_client = ClientFixture([response("最便宜的是 89 欧元。")])
    BotRuntime(store, OWNER, lambda _: next_client).run(claimed, runtime.task_token(claimed["id"]))
    assert note_of(next_client) == ""


def test_a_turn_cut_off_twice_is_not_started_a_third_time(store, bot):
    task = send(store, bot, CHINESE, "chinese-request-2")
    store.claim(OWNER)
    assert store.recover(OWNER) == 1
    assert store.claim(OWNER)["id"] == task["id"]
    step(store, task["id"], "read_file")

    assert store.recover(OWNER) == 1

    stopped = store.task(OWNER, task["id"])
    assert stopped["status"] == "interrupted" and stopped["restarts"] == 1
    assert stopped["failure"] == {"kind": "service", "not_rerun": "already_rerun"}
    assert "already started again once" in stopped["error"]
    assert store.claim(OWNER) is None


def test_a_picture_kept_in_the_library_counts_as_the_owls_own_records(store, bot):
    task = send(store, bot, GREEK, "picture-request-1")
    store.claim(OWNER)
    step(store, task["id"], "create_image", file="Χάρτης πτήσεων.png")
    # The provider was still answering an edit when the app went away: nothing was kept.
    step(store, task["id"], "edit_image", done=False, file="Χάρτης πτήσεων.png")

    assert store.recover(OWNER) == 1

    queued = store.task(OWNER, task["id"])
    assert queued["status"] == "queued" and queued["restarts"] == 1


def test_looking_at_the_owls_computer_and_reading_its_clipboard_count_as_reading(store, bot):
    task = send(store, bot, CHINESE, "computer-reading-1")
    store.claim(OWNER)
    for name in ("computer_observe", "computer_process", "computer_read_clipboard"):
        step(store, task["id"], name)

    assert store.recover(OWNER) == 1

    queued = store.task(OWNER, task["id"])
    assert queued["status"] == "queued" and queued["restarts"] == 1


@pytest.mark.parametrize("effect", ["read", None])
def test_a_plugin_step_counts_as_reading_only_by_its_own_label(store, bot, effect):
    task = send(store, bot, CHINESE, f"plugin-request-{effect}")
    store.claim(OWNER)
    step(store, task["id"], "calendar__list_events", **({"effect": effect} if effect else {}))

    store.recover(OWNER)

    cut = store.task(OWNER, task["id"])
    if effect == "read":
        assert cut["status"] == "queued"
    else:
        assert cut["status"] == "interrupted"
        assert cut["failure"]["not_rerun"] == "acted"
        assert cut["failure"]["step"] == "calendar__list_events"


# ---------------------------------------------------------------------------------------------
# Something outside the owl's own records: interrupted, saying which step stopped the rerun


@pytest.mark.parametrize(
    ("name", "done", "data", "not_rerun"),
    [
        ("browser_click", True, {}, "acted"),
        ("browser_upload", True, {}, "acted"),
        ("calendar__create_event", True, {"effect": "own_change"}, "acted"),
        ("computer_run", True, {}, "acted"),
        ("computer_pointer", True, {}, "acted"),
        ("export_local_file", True, {}, "acted"),
        ("request_peer", False, {}, "unconfirmed"),
        ("propose_action", True, {}, "acted"),
        # A check-back shows in the chat and starts a turn there later by itself.
        ("check_back", True, {}, "acted"),
        # Under way when the app went away: it may have happened.
        ("local_command", False, {}, "unconfirmed"),
        ("browser_type", False, {}, "unconfirmed"),
        # A tool this check has never heard of may have changed something.
        ("some_future_tool", True, {}, "acted"),
    ],
)
def test_a_turn_that_changed_something_is_not_started_again_and_says_which_step(
    store, bot, name, done, data, not_rerun
):
    task = send(store, bot, GREEK, f"acted-request-{name}")
    store.claim(OWNER)
    step(store, task["id"], "web_search", topic="πτήσεις")
    step(store, task["id"], name, done=done, **data)
    step(store, task["id"], "read_file")

    assert store.recover(OWNER) == 1

    cut = store.task(OWNER, task["id"])
    assert cut["status"] == "interrupted" and cut["restarts"] == 0
    assert cut["failure"] == {"kind": "service", "not_rerun": not_rerun, "step": name}
    assert cut["events"][-1]["data"]["failure"] == cut["failure"]
    assert "did not start again on its own" in cut["error"]
    assert store.claim(OWNER) is None


def test_a_routine_run_starts_again_only_when_it_changed_nothing(store, bot):
    store.save_schedule(
        OWNER,
        bot,
        ScheduleInput(
            name="每日航班",
            prompt=CHINESE,
            timing={"kind": "interval", "every_minutes": 60},
        ),
        now=1000,
    )
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=4600) == 1
    reading = store.claim(OWNER)
    step(store, reading["id"], "web_fetch", host="flights.example")

    assert store.recover(OWNER) == 1

    # The same run starts again; the routine's next run waits for it, nothing is retried.
    assert store.task(OWNER, reading["id"])["status"] == "queued"
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=8200) == 0
    assert store.claim(OWNER)["id"] == reading["id"]
    store.finish(OWNER, reading["id"], "completed", content="最便宜的是 89 欧元。")
    assert store.enqueue_due_schedules(OWNER, max_calls=None, now=8200) == 1
    acting = store.claim(OWNER)
    step(store, acting["id"], "browser_click")

    assert store.recover(OWNER) == 1

    # A run that acted is never replayed; the routine keeps going with its next run.
    assert store.task(OWNER, acting["id"])["status"] == "interrupted"
    routine = store.list_schedules(OWNER, bot)[0]
    assert routine["state"] == "active" and routine["retry"] is None


# ---------------------------------------------------------------------------------------------
# The runtime stopping under a turn (an app update or quit) decides the same way


class WaitsForStop:
    """A model call that is still thinking when the runtime stops."""

    model = "restart-fixture"
    supports_tool_calling = True

    def __init__(self):
        self.started = threading.Event()

    def chat(self, **kwargs):
        self.started.set()
        token = kwargs["cancellation_token"]
        while not token.is_cancelled:
            time.sleep(0.01)
        raise CooperativeCancellationError("runtime_stopped")


def test_stopping_the_runtime_under_a_turn_queues_it_to_start_again(store, bot):
    task = send(store, bot, GREEK, "greek-request-stop")
    thinking = WaitsForStop()
    runtime = BotRuntime(store, OWNER, lambda _: thinking)
    runtime.start()
    try:
        assert thinking.started.wait(30)
    finally:
        runtime.stop()

    stopped = store.task(OWNER, task["id"])
    assert stopped["status"] == "queued" and stopped["restarts"] == 1

    client = ClientFixture([response("Βρήκα τρεις πτήσεις.")])
    restarted = BotRuntime(Store(store.path), OWNER, lambda _: client)
    restarted.start()
    try:
        deadline = time.monotonic() + 30
        while store.task(OWNER, task["id"])["status"] != "completed":
            assert time.monotonic() < deadline
            time.sleep(0.05)
    finally:
        restarted.stop()
    assert store.task(OWNER, task["id"])["output"] == "Βρήκα τρεις πτήσεις."
    assert "interrupted by a restart" in note_of(client)


def test_stopping_the_runtime_under_a_turn_that_acted_interrupts_it(store, bot):
    task = send(store, bot, CHINESE, "chinese-request-stop")
    thinking = WaitsForStop()
    runtime = BotRuntime(store, OWNER, lambda _: thinking)
    runtime.start()
    try:
        assert thinking.started.wait(30)
        step(store, task["id"], "browser_click")
    finally:
        runtime.stop()

    stopped = store.task(OWNER, task["id"])
    assert stopped["status"] == "interrupted"
    assert stopped["failure"] == {"kind": "service", "not_rerun": "acted", "step": "browser_click"}


def test_another_worker_never_takes_a_turn_the_stopping_runtime_queued_again(store, bot):
    # The runtime stops under a turn and queues it to start again after the restart; another
    # of its workers reaching its claim just then leaves it for that restart.
    task = send(store, bot, GREEK, "greek-request-sibling")
    thinking = WaitsForStop()
    runtime = BotRuntime(store, OWNER, lambda _: thinking)
    claim, lock = store.claim, threading.Lock()
    first, sibling = [], threading.Event()

    def claim_after_the_requeue(owner, **options):
        with lock:
            if not first:
                claimed = claim(owner, **options)
                if claimed is not None:
                    first.append(threading.current_thread())
                return claimed
        if threading.current_thread() is first[0]:
            return claim(owner, **options)
        # Another worker: its claim goes through once the stop queued the turn again.
        sibling.set()
        runtime._stop.wait()
        deadline = time.monotonic() + 10
        while store.task(OWNER, task["id"])["status"] != "queued":
            assert time.monotonic() < deadline
            time.sleep(0.01)
        return claim(owner, **options)

    store.claim = claim_after_the_requeue
    runtime.start()
    try:
        assert thinking.started.wait(30) and sibling.wait(30)
    finally:
        runtime.stop()

    stopped = store.task(OWNER, task["id"])
    assert stopped["status"] == "queued" and stopped["restarts"] == 1
    assert [event["kind"] for event in stopped["events"]].count("running") == 1


def test_a_claim_the_runtime_does_not_admit_leaves_the_turn_as_it_was(store, bot):
    from alysis_code.bots.background import BackgroundStore

    task = send(store, bot, CHINESE, "chinese-request-admit")
    assert store.claim(OWNER, admit=lambda: False) is None
    assert store.task(OWNER, task["id"])["status"] == "queued"
    claimed = store.claim(OWNER, admit=lambda: True)
    assert claimed["id"] == task["id"]
    # Work off the reply loop, too.
    background = BackgroundStore(store, OWNER)
    background.submit(claimed, "查航班", [])
    assert background.claim(admit=lambda: False) is None
    assert background.claim()["id"] == task["id"]


def test_an_existing_database_gains_the_restart_count(tmp_path):
    import sqlite3

    from alysis_code.bots.schema import SCHEMA_VERSION

    path = tmp_path / "earlier.db"
    store = Store(path)
    bot = store.save_bot(OWNER, BotInput(name="Ερμής", purpose="Ταξίδια"))["id"]
    task = send(store, bot, CHINESE, "earlier-request-1")
    with sqlite3.connect(path) as db:
        db.execute("ALTER TABLE tasks DROP COLUMN restarts")
        db.execute("PRAGMA user_version=46")
    migrated = Store(path)
    with migrated.connect() as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION >= 48
    assert migrated.task(OWNER, task["id"])["restarts"] == 0
