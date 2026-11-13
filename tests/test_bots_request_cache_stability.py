"""A bot's request repeats the one before it, so a provider's prompt cache can be used.

Bots build their own requests. A provider caches a request by its leading
bytes, so while a task only adds messages, each request must begin with the
conversation the previous one sent. With saved memory the bot shows, beside a
reply, a receipt of the memory that reply was given; each reply keeps its own
receipt, so those stay where they are. From task to task, the instructions and
the earlier conversation come first and are the same; what belongs to one task
follows them. Real store, runtime and engine; scripted model.
"""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from bots_task_state import task_state
from test_bots_desktop import Executor

from alysis_code.bots.coding import WorkspaceRegistration
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.execution import TaskClient
from alysis_code.bots.models import BotInput, MemoryInput, TaskInput
from alysis_code.bots.proposals import ActionDecision, ActionProposal, ProposalStore
from alysis_code.bots.runtime import TASK_STATE_HEADING, BotRuntime
from alysis_code.bots.store import Store
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall
from alysis_code.memory_evidence import GUIDANCE, RECEIPT_ID_KEY, response_key

WIRE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")
CLOSING_NOTES = (
    "Historical receipt coverage",
    "Historical memory inputs for responses no longer present",
    "Current memory observation",
)


class _Client:
    model = "fixture"
    supports_tool_calling = True

    def __init__(self) -> None:
        self.responses: Any = iter(())
        self.requests: list[list[dict[str, Any]]] = []
        self.tools: list[str] = []
        self.before_reply: Any = None

    def chat(self, **kwargs):
        self.requests.append(copy.deepcopy(kwargs["messages"]))
        self.tools.append(json.dumps(kwargs.get("tools") or [], sort_keys=True))
        if self.before_reply is not None:
            self.before_reply()
        return next(self.responses)


def _call(call_id, text: str = "", name: str = "list_memories", arguments=None) -> LLMResponse:
    return LLMResponse(
        text, [ToolCall(str(call_id), name, arguments or {})], {}, usage=LLMUsage(10, 5, 15)
    )


def _answer(text: str) -> LLMResponse:
    return LLMResponse(text, [], {}, usage=LLMUsage(10, 5, 15))


def _is_receipt(message: dict[str, Any]) -> bool:
    return str(message.get("content") or "").startswith(GUIDANCE)


def _is_closing_note(message: dict[str, Any]) -> bool:
    return str(message.get("content") or "").startswith(CLOSING_NOTES)


def _wire(message: dict[str, Any]) -> str:
    return json.dumps({k: message[k] for k in WIRE_KEYS if k in message}, sort_keys=True)


def _conversation(request: list[dict[str, Any]]) -> list[str]:
    """The request without what it adds after the conversation."""
    end = len(request)
    while end and (request[end - 1].get("role") == "system" or _is_closing_note(request[end - 1])):
        end -= 1
    assert not any(_is_closing_note(message) for message in request[:end])
    return [_wire(message) for message in request[:end]]


def _assert_requests_repeat(requests: list[list[dict[str, Any]]], *, host_notes=True) -> None:
    """Each request begins with the conversation the one before it sent.

    Without ``host_notes`` the system messages are left out: the bot replaces
    its note about refreshed memory each time the memory changes.
    """
    if not host_notes:
        requests = [[m for m in request if m.get("role") != "system"] for request in requests]
    conversations = [_conversation(request) for request in requests]
    for before, after in zip(conversations, conversations[1:], strict=False):
        assert after[: len(before)] == before


def _beside(request: list[dict[str, Any]]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Each receipt shown in a request, with the reply it is shown beside."""
    shown = []
    for index, message in enumerate(request):
        if _is_receipt(message):
            reply = request[index + 1]
            assert reply.get("role") == "assistant"
            shown.append((json.loads(message["content"].split("\n", 1)[1]), reply))
    return shown


def _no_host_fields(requests: list[list[dict[str, Any]]]) -> bool:
    return not any(key.startswith("_") for request in requests for m in request for key in m)


@pytest.fixture
def bot_chat(tmp_path):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("owner", BotInput(name="Research", purpose="Keep notes and answer"))
    chat = store.create_chat("owner", bot["id"], "Conversation")
    return store, bot, chat


def _save_memories(store, bot, count: int) -> None:
    for index in range(count):
        store.save_memory(
            "owner",
            bot["id"],
            MemoryInput(
                title=f"Preference {index}", content=f"Preference {index}: answers stay short."
            ),
        )


def _run_task(store, runtime, client, chat, message: str, responses) -> tuple[dict, list]:
    """Run one task to its end; returns the task and the requests it sent."""
    store.enqueue("owner", chat["id"], TaskInput(message=message, request_id=uuid4().hex))
    task = store.claim("owner")
    client.responses = iter(responses)
    start = len(client.requests)
    runtime.run(task, runtime.task_token(task["id"]))
    return store.task("owner", task["id"]), client.requests[start:]


@pytest.mark.parametrize("memories", [0, 6])
@pytest.mark.parametrize("says_what_it_does", [False, True])
def test_a_bot_task_repeats_its_earlier_requests(bot_chat, memories, says_what_it_does):
    store, bot, chat = bot_chat
    _save_memories(store, bot, memories)
    client = _Client()
    runtime = BotRuntime(store, "owner", lambda _: client)
    tasks, sent = [], []
    for turn in range(3):
        steps = 30 if turn == 1 else 3  # one task has more model calls than receipts are listed
        task, requests = _run_task(
            store,
            runtime,
            client,
            chat,
            f"Request {turn}: tell me item {turn}.",
            [
                _call(turn * 100 + i, "Checking." if says_what_it_does and i % 2 else "")
                for i in range(steps)
            ]
            + [_answer(f"Item {turn} is listed.")] * 3,
        )
        assert task["status"] == "completed"
        tasks.append(task)
        sent.append(requests)

    assert len(set(client.tools)) == 1  # the same tools in every request
    for requests in sent:
        _assert_requests_repeat(requests)
    shown = [sum(_is_receipt(m) for m in request) for request in client.requests]
    assert (max(shown) > 0) is bool(memories)
    assert _no_host_fields(client.requests)
    assert not runtime._kept  # nothing is held once the tasks have ended
    if memories:
        # A finished task's reply has, in a later task, the receipt recorded for it.
        receipt, reply = _beside(sent[2][-1])[0]
        assert reply["content"] == "Item 0 is listed."
        assert receipt["turn_id"] == tasks[0]["id"]
        assert receipt["response_key"] == response_key("Item 0 is listed.")


def _shared(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> int:
    """How many leading messages two requests have in common."""
    count = 0
    for one, other in zip(before, after, strict=False):
        if _wire(one) != _wire(other):
            break
        count += 1
    return count


@pytest.mark.parametrize("memory", ["none", "two short memories", "another before every task"])
def test_a_new_task_begins_as_the_task_before_it_did(bot_chat, memory):
    """The instructions are the same for every task of a bot, and the earlier
    conversation follows them unchanged. A provider that cached the last task's
    requests can therefore reuse them for the next one. What belongs to one
    task (its time, ids, saved memory, files) comes after, just before the
    task's own request."""
    store, bot, chat = bot_chat
    if memory == "two short memories":
        _save_memories(store, bot, 2)
    client = _Client()
    runtime = BotRuntime(store, "owner", lambda _: client)
    tasks, sent = [], []
    for turn in range(8):
        if memory == "another before every task":
            store.save_memory(
                "owner", bot["id"], MemoryInput(title=f"Added {turn}", content=f"Addition {turn}.")
            )
        task, requests = _run_task(
            store,
            runtime,
            client,
            chat,
            f"Request {turn}",
            [_call(f"c{turn}"), _answer(f"Answer {turn}.")],
        )
        assert task["status"] == "completed"
        tasks.append(task)
        sent.append(requests)

    assert len(set(client.tools)) == 1
    instructions = {request[0]["content"] for request in client.requests}
    assert len(instructions) == 1  # one text, whatever the task
    text = instructions.pop()
    assert "Task start UTC time" not in text and chat["id"] not in text
    assert not any(task["id"] in text for task in tasks)
    for turn, (task, requests) in enumerate(zip(tasks, sent, strict=True)):
        first = requests[0]
        # The task's state, then the notes about the present, then its request.
        at = next(i for i, m in enumerate(first) if m["content"] == task_state(first))
        assert task["id"] in first[at]["content"] and "Task start UTC time" in first[at]["content"]
        request_at = next(i for i, m in enumerate(first) if m["content"] == f"Request {turn}")
        assert at < request_at
        assert all(message["role"] == "system" for message in first[at:request_at])
        assert [m["content"] for m in first[1:at] if m["role"] == "system"] == []
        if turn == 0:
            continue
        shared = _shared(sent[turn - 1][-1], first)
        if memory == "none":
            assert shared == 1 + 2 * (turn - 1)  # everything before the last task's own part
        elif memory == "two short memories" and turn >= 3:
            # A receipt beside a finished reply reads the same in the next
            # task, so nothing before the last task's own part is sent again.
            assert shared >= 1 + 2 * (turn - 1)
        else:
            # The two newest finished replies show their receipts, so the
            # conversation is the same up to the turn before those.
            assert shared >= max(1, 2 * turn - 4)


def test_what_is_approved_for_a_task_is_in_its_state_not_in_the_instructions(bot_chat):
    store, bot, chat = bot_chat
    store.enqueue("owner", chat["id"], TaskInput(message="Read the page", request_id=uuid4().hex))
    task = store.claim("owner")
    runtime = BotRuntime(store, "owner", lambda _: _Client())
    # Building the context only asks whether the bot has these computers.
    runtime.browser_computers = runtime.desktop_computers = SimpleNamespace()
    computers = runtime._context(task)
    assert "Approved website origins for this task only: []" in task_state(computers)
    assert "Offline computer approved for this task: False" in task_state(computers)
    assert "Approved website origins for this task only: " not in computers[0]["content"]
    assert "Offline computer approved for this task: " not in computers[0]["content"]
    plain = BotRuntime(store, "owner", lambda _: _Client())._context(task)
    assert "Approved website origins" not in task_state(plain)
    assert "Offline computer approved" not in task_state(plain)


def test_a_note_about_the_present_follows_the_task_state():
    """The notes about pending questions and what can run are replaced where
    they are. Their place is after the task's state; a conversation saved
    before the state had a message of its own keeps them after the first
    message."""
    note = {"role": "system", "content": "Current clarification state (reference data):\n[]"}
    newer = {"role": "system", "content": "Current clarification state (reference data):\n[1]"}
    prefix = "Current clarification state (reference data):\n"
    messages = [
        {"role": "system", "content": "instructions"},
        {"role": "user", "content": "earlier"},
        {"role": "assistant", "content": "done"},
        {"role": "system", "content": TASK_STATE_HEADING, "_bot_task_state": True},
        {"role": "user", "content": "now"},
    ]
    BotRuntime._place_current_note(messages, prefix, note)
    assert messages.index(note) == 4 and messages[-1]["content"] == "now"
    BotRuntime._place_current_note(messages, prefix, newer)
    assert messages.index(newer) == 4 and note not in messages
    saved_earlier = [{"role": "system", "content": "instructions and state"}, messages[1]]
    BotRuntime._place_current_note(saved_earlier, prefix, note)
    assert saved_earlier.index(note) == 1


def test_a_discarded_bot_response_leaves_no_receipt_beside_a_reply(bot_chat):
    """Memory is edited while the model is answering: that response is thrown
    away. Its receipt stays in the ledger and is shown beside no reply."""
    store, bot, chat = bot_chat
    saved = store.save_memory("owner", bot["id"], MemoryInput(title="Style", content="Be brief."))
    client = _Client()
    runtime = BotRuntime(store, "owner", lambda _: client)

    class Editing:
        """The first response arrives after the user has changed the memory."""

        def __iter__(self):
            return self

        def __init__(self):
            self.calls = 0

        def __next__(self):
            self.calls += 1
            if self.calls == 1:
                store.save_memory(
                    "owner",
                    bot["id"],
                    MemoryInput(title="Style", content="Be thorough."),
                    memory_id=saved["id"],
                )
                return _answer("Hi.")
            if self.calls == 2:
                return _call(1)
            return _answer("Hello, in full.")

    task, requests = _run_task(store, runtime, client, chat, "Say hello.", Editing())
    assert task["status"] == "completed"
    assert len(store.memory_deliveries("owner", bot["id"])) == len(requests) == 3
    assert "Hi." not in [m.get("content") for m in requests[-1]]  # the discarded answer
    _beside(requests[-1])  # every receipt shown is beside a reply
    _assert_requests_repeat(requests[1:])


def test_the_task_being_worked_on_has_room_for_a_receipt_of_its_own(bot_chat):
    """Every task here is given another state of the memory. The replies of
    finished tasks may use half of the room beside replies, so the first reply
    of the task being worked on shows what memory it was given, and nothing
    about the replies still present is sent again after the conversation."""
    store, bot, chat = bot_chat
    client = _Client()
    runtime = BotRuntime(store, "owner", lambda _: client)
    for turn in range(5):
        store.save_memory(
            "owner",
            bot["id"],
            MemoryInput(title=f"Added {turn}", content=f"Preference added before task {turn}."),
        )
        task, requests = _run_task(
            store,
            runtime,
            client,
            chat,
            f"Request {turn}",
            [_call(f"c{turn}a"), _call(f"c{turn}b", "Checking."), _answer(f"Answer {turn}.")],
        )
        assert task["status"] == "completed"
        _assert_requests_repeat(requests)
        own = [
            receipt
            for receipt, reply in _beside(requests[-1])
            if (reply.get("tool_calls") or [{}])[0].get("id") == f"c{turn}a"
        ]
        assert [receipt["turn_id"] for receipt in own] == [task["id"]]
        assert not any(
            str(m.get("content")).startswith("Historical memory inputs for responses")
            for request in requests[1:]
            for m in request
        )


@pytest.mark.parametrize("changed_by", ["the bot's own note", "the user"])
def test_a_second_state_of_the_memory_within_a_task_is_shown_too(bot_chat, changed_by):
    """A task is given another state of the memory while it works: the bot
    saves a working note, or the user edits the memory (the response being
    made then is thrown away). The first reply made with the new state shows
    it, also when a finished task's reply already shows a receipt."""
    store, bot, chat = bot_chat
    _save_memories(store, bot, 3)
    client = _Client()
    runtime = BotRuntime(store, "owner", lambda _: client)
    earlier, _ = _run_task(
        store, runtime, client, chat, "Earlier", [_call("e0"), _answer("Earlier answer.")]
    )
    assert earlier["status"] == "completed"
    store.save_memory("owner", bot["id"], MemoryInput(title="Units", content="Use metric units."))

    class Script:
        def __init__(self):
            self.sent = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.sent += 1
            if self.sent == 2 and changed_by == "the user":
                store.save_memory(
                    "owner", bot["id"], MemoryInput(title="Late", content="A late preference.")
                )
                return _call("thrown-away")
            if self.sent == 2:
                return _call(
                    "c2",
                    name="save_working_note",
                    arguments={"title": "Trip plan", "content": "Decision 1."},
                )
            return _call(f"c{self.sent}") if self.sent < 7 else _answer("Planned.")

    task, requests = _run_task(store, runtime, client, chat, "Plan the trip", Script())
    assert task["status"] == "completed"
    shown = [
        (reply.get("tool_calls") or [{}])[0].get("id") or reply["content"]
        for _, reply in _beside(requests[-1])
    ]
    assert shown == ["Earlier answer.", "c1", "c3"]


def test_a_running_task_keeps_its_receipts_when_the_host_lets_go_of_them(bot_chat):
    """What the host holds about kept receipts is a copy for the length of a
    run. Whatever is missing from it (here, everything, before every reply) is
    read from the store again, so nothing moves."""
    store, bot, chat = bot_chat
    _save_memories(store, bot, 3)
    client = _Client()
    runtime = BotRuntime(store, "owner", lambda _: client)
    client.before_reply = runtime._kept.clear
    placements = []
    for turn in range(2):
        store.save_memory(
            "owner", bot["id"], MemoryInput(title=f"Added {turn}", content=f"Addition {turn}.")
        )
        task, requests = _run_task(
            store,
            runtime,
            client,
            chat,
            f"Request {turn}",
            # More calls than the store lists as a task's newest receipts.
            [_call(f"c{turn}-{i}") for i in range(40)] + [_answer(f"Answer {turn}.")],
        )
        assert task["status"] == "completed"
        _assert_requests_repeat(requests)
        placements.append([(r["id"], _wire(reply)) for r, reply in _beside(requests[-1])])
        # The first reply of the task shows what it was given, to the end.
        first = [reply for _, reply in _beside(requests[-1]) if reply.get("tool_calls")]
        assert [reply["tool_calls"][0]["id"] for reply in first] == [f"c{turn}-0"]
    assert len(placements[0]) == 1 and len(placements[1]) == 2


@pytest.mark.parametrize("calls", [2, 30])
@pytest.mark.parametrize("resumed_by", ["the same process", "another process"])
def test_a_task_resumed_from_its_checkpoint_keeps_its_receipts(bot_chat, resumed_by, calls):
    """A task that moves to the background is continued from a saved
    checkpoint, possibly by another process. Each reply in the checkpoint
    keeps the mark of its receipt; the receipt itself is in the store, where
    it is found by its id however many receipts the task has by then."""
    store, bot, chat = bot_chat
    _save_memories(store, bot, 3)
    client = _Client()
    runtime = BotRuntime(store, "owner", lambda _: client)
    first, _ = _run_task(
        store, runtime, client, chat, "First request", [_call("a1"), _answer("First answer.")]
    )
    assert first["status"] == "completed"
    # The memory changes, so the replies of the next task were given another state.
    store.save_memory("owner", bot["id"], MemoryInput(title="Units", content="Use metric units."))
    task, foreground = _run_task(
        store,
        runtime,
        client,
        chat,
        "Long work please",
        [
            _call("b1", "Looking."),
            *[_call(f"b{2 + i}") for i in range(calls - 1)],
            _call("hand-over", name="continue_in_background", arguments={"summary": "Carry on"}),
        ],
    )
    with store.connect() as db:
        checkpoint = json.loads(
            db.execute(
                "SELECT messages FROM background_turns WHERE task_id=?", (task["id"],)
            ).fetchone()[0]
        )
    assert sum(1 for message in checkpoint if message.get(RECEIPT_ID_KEY)) == calls + 2
    before = [(receipt["id"], _wire(reply)) for receipt, reply in _beside(foreground[-1])]
    assert [json.loads(reply)["content"] for _, reply in before] == ["First answer.", "Looking."]

    if resumed_by == "another process":
        runtime = BotRuntime(Store(store.path), "owner", lambda _: client)
    claimed = runtime.background.claim()
    client.responses = iter([_call("n1"), _call("n2"), _answer("Background finished.")])
    start = len(client.requests)
    runtime.run(claimed, runtime.task_token(task["id"]), background=True)
    background = client.requests[start:]
    assert store.task("owner", task["id"])["status"] == "completed"
    _assert_requests_repeat([foreground[-1], *background])
    for request in background:
        assert [(r["id"], _wire(reply)) for r, reply in _beside(request)] == before
    assert _no_host_fields(client.requests)


def test_a_handoff_conversation_is_as_before_and_keeps_host_fields_to_itself(bot_chat):
    """The conversation handed to a coding or background worker becomes quoted
    reference text. It carries no reply marks, and a coding job stores none of
    the fields the host keeps for itself."""
    store, bot, chat = bot_chat
    _save_memories(store, bot, 3)
    client = _Client()
    runtime = BotRuntime(store, "owner", lambda _: client)
    for turn in range(3):
        store.save_memory(
            "owner", bot["id"], MemoryInput(title=f"Added {turn}", content=f"Addition {turn}.")
        )
        _run_task(
            store,
            runtime,
            client,
            chat,
            f"Request {turn}",
            [_call(f"c{turn}"), _answer(f"Answer {turn}.")],
        )
    workspace, worker = uuid4().hex, uuid4().hex
    runtime.coding.register(WorkspaceRegistration(id=workspace, name="Project", worker_id=worker))
    runtime.coding.bind(bot["id"], workspace)
    store.enqueue("owner", chat["id"], TaskInput(message="Write the code", request_id=uuid4().hex))
    task = store.claim("owner")
    reference = runtime._context(task)
    assert not any(RECEIPT_ID_KEY in message for message in reference)
    # One receipt for each state of the memory, beside the reply that was given it.
    assert [reply["content"] for _, reply in _beside(reference)] == [
        "Answer 0.",
        "Answer 1.",
        "Answer 2.",
    ]

    job = runtime.coding.submit(task, "Change the project", reference)
    conversation = job["context"]["conversation"]
    assert [m["content"] for m in conversation] == [m["content"] for m in reference]
    assert not any(key.startswith("_") for message in conversation for key in message)
    assert '"_memory' not in json.dumps(job["context"])


def test_the_newest_state_of_a_task_is_in_the_request_when_the_room_is_used_up(bot_chat):
    """Two finished tasks, and a task that saves a note three times: each save
    is another state of the memory. The first states are beside replies; the
    newest, with no room there, is in the note after the conversation."""
    store, bot, chat = bot_chat
    _save_memories(store, bot, 3)
    client = _Client()
    runtime = BotRuntime(store, "owner", lambda _: client)
    for turn in range(2):
        store.save_memory(
            "owner", bot["id"], MemoryInput(title=f"Added {turn}", content=f"Addition {turn}.")
        )
        _run_task(
            store, runtime, client, chat, f"Earlier {turn}", [_call(f"e{turn}"), _answer("Done.")]
        )
    responses = []
    for save in range(3):
        responses += [
            _call(f"read-{save}"),
            _call(
                f"save-{save}",
                name="save_working_note",
                arguments={"title": f"Plan {save}", "content": f"Decision {save}."},
            ),
        ]
    task, requests = _run_task(
        store, runtime, client, chat, "Plan the trip", [*responses, _call("last"), _answer("Ok.")]
    )
    assert task["status"] == "completed"
    _assert_requests_repeat(requests, host_notes=False)
    newest = store.task_memory_deliveries("owner", bot["id"], [task["id"]])[task["id"]][-1]
    wanted = {source["source_ref"] for source in newest["sources"]}
    last = requests[-1]
    beside = {s["source_ref"] for receipt, _ in _beside(last) for s in receipt["sources"]}
    assert not wanted <= beside  # the room beside replies went to the earlier states
    note = next(m["content"] for m in last if str(m["content"]).startswith(CLOSING_NOTES[1]))
    after = {s["source_ref"] for r in json.loads(note.split("\n", 1)[1]) for s in r["sources"]}
    assert wanted <= beside | after


def test_a_receipt_is_found_in_the_store_by_its_id_whatever_the_id_looks_like(bot_chat):
    """Marks are looked up by id among a task's receipts, without reading the
    others. Ids written by another version need not look like today's."""
    store, bot, chat = bot_chat
    store.enqueue("owner", chat["id"], TaskInput(message="Prepare", request_id=uuid4().hex))
    task = store.claim("owner")
    ids = [uuid4().hex for _ in range(30)] + [str(uuid4()), "r-17", uuid4().hex + "feedface"]
    for receipt_id in ids:
        store.record_memory_delivery(
            "owner", task["id"], [], {"id": receipt_id, "response_key": "k", "sources": []}
        )
    wanted = [ids[0], ids[15], *ids[30:]]  # the oldest of more than the newest listed, and odd ids
    found = store.task_memory_deliveries("owner", bot["id"], [task["id"]], receipt_ids=wanted)
    assert [receipt["id"] for receipt in found[task["id"]]] == wanted
    assert len(store.task_memory_deliveries("owner", bot["id"], [task["id"]])[task["id"]]) == 24


def test_nothing_is_held_for_a_request_that_keeps_no_receipt(bot_chat):
    """As when a coding worker's model call passes through the bot: every
    message belongs to the task itself and no reply is marked."""
    store, bot, chat = bot_chat
    _save_memories(store, bot, 2)
    runtime = BotRuntime(store, "owner", lambda _: _Client())
    store.enqueue("owner", chat["id"], TaskInput(message="Write code", request_id=uuid4().hex))
    task = store.claim("owner")
    messages = [
        {"role": "system", "content": "worker", "_memory_turn_id": task["id"]},
        {"role": "user", "content": "Write code", "_memory_turn_id": task["id"]},
        {"role": "assistant", "content": "Working.", "_memory_turn_id": task["id"]},
    ]
    runtime._fit_context(task, messages, [])
    assert not runtime._kept


def test_a_request_that_leaves_out_older_engine_replies_still_marks_the_right_ones(bot_chat):
    """The engine's replies can say and call exactly the same thing (a model
    polling, a provider that numbers every call alike). A request holding only
    the newest of them is matched from the newest back."""
    store, bot, chat = bot_chat
    runtime = BotRuntime(store, "owner", lambda _: _Client())
    store.enqueue("owner", chat["id"], TaskInput(message="Prepare", request_id=uuid4().hex))
    task = store.claim("owner")
    adapter = TaskClient(runtime, task, _Client(), runtime.task_token(task["id"]))

    def reply():
        call = {"id": "call_0", "type": "function", "function": {"name": "wait", "arguments": "{}"}}
        return {"role": "assistant", "content": "", "tool_calls": [call]}

    class Session:
        messages = [{"role": "user", "content": "Prepare"}]

    adapter.follow(Session)
    kept = adapter.replies._memory_delivery_state["bound"]
    for index in range(5):
        receipt = {"id": f"{index:032x}", "response_key": response_key(""), "sources": []}
        Session.messages += [
            {**reply(), RECEIPT_ID_KEY: receipt["id"]},
            {"role": "tool", "tool_call_id": "call_0", "content": "waiting"},
        ]
        kept[receipt["id"]] = receipt
    request = [reply(), reply(), reply()]  # copies of the three newest replies only
    adapter._keep_receipts(request)
    assert [m[RECEIPT_ID_KEY] for m in request] == [f"{index:032x}" for index in (2, 3, 4)]


def _label(tool_calls, content) -> str:
    """What tells one scripted response from another: its command, or its text."""
    for call in tool_calls or []:
        arguments = call["function"]["arguments"] if isinstance(call, dict) else call.arguments
        if isinstance(arguments, str):
            arguments = json.loads(arguments)
        return arguments["command"]
    return content or ""


def test_the_engine_loop_of_a_bot_task_repeats_its_earlier_requests(bot_chat):
    """A computer task runs in the engine's loop behind the bot's task client.
    The provider gives every tool call the same id, the model answers with
    nothing once (the engine does not keep that reply), the memory is edited
    while the model is answering (that response is thrown away), and one
    response asks for a tool that was not offered (the task client answers it
    itself). Each reply still has the receipt of the request behind it."""
    store, bot, chat = bot_chat
    _save_memories(store, bot, 4)
    store.enqueue("owner", chat["id"], TaskInput(message="Prepare work", request_id=uuid4().hex))
    task = store.claim("owner")
    proposals = ProposalStore(store, "owner")
    proposal = proposals.propose(
        task,
        ActionProposal(
            question="Run the offline task?",
            instructions="Inspect the requested file.",
            offline_computer=True,
        ),
    )
    store.finish("owner", task["id"], "completed", content="Please review the scope.")
    proposals.decide(
        bot["id"],
        proposal["id"],
        ActionDecision(action="accept", expected_revision=1, offline_computer=True),
    )
    task = store.claim("owner")

    plan = ["tool", "tool and text", "nothing", "tool", "edited", "tool", "refused", "tool"]

    class Script:
        def __init__(self):
            self.sent = 0

        def __iter__(self):
            return self

        def __next__(self):
            self.sent += 1
            kind = plan[self.sent - 1] if self.sent <= len(plan) else "answer"
            if kind == "answer":
                return _answer("The work is prepared.")
            if kind == "nothing":
                return _answer("")
            if kind == "edited":
                store.save_memory(
                    "owner", bot["id"], MemoryInput(title="Late", content="A new preference.")
                )
            return _call(
                "call_0",
                "Next step." if kind == "tool and text" else "",
                name="not_offered" if kind == "refused" else "computer_run",
                arguments={"command": f"printf step{self.sent}"},
            )

    client = _Client()
    client.responses = Script()
    manager = DesktopComputers(
        "owner", ComputerViews(), factory=lambda *args, **kwargs: Executor(*args, **kwargs)
    )
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)

    made: dict[str, str] = {}  # receipt id -> the response it was recorded for
    record = runtime._record_memory_delivery

    def recording(task, inputs, response):
        receipt = record(task, inputs, response)
        if receipt is not None:
            made[receipt["id"]] = _label(response.tool_calls, response.content)
        return receipt

    marks: list[dict[str, str | None]] = []  # per request: reply -> the receipt it keeps
    fit = runtime._fit_context

    def fitting(task, messages, schemas=None, **options):
        marks.append(
            {
                _label(m.get("tool_calls"), m.get("content")): m.get(RECEIPT_ID_KEY)
                for m in messages
                if m.get("role") == "assistant"
            }
        )
        return fit(task, messages, schemas, **options)

    runtime._record_memory_delivery = recording
    runtime._fit_context = fitting
    try:
        runtime.run(task, runtime.task_token(task["id"]))
        claimed = runtime.background.claim()
        runtime.run(claimed, runtime.task_token(task["id"]), background=True)
    finally:
        manager.close()
    assert store.task("owner", task["id"])["status"] == "completed"
    assert len(client.requests) == len(plan) + 1

    # Each kept reply has the receipt recorded for its own response, and no other.
    kept = ["printf step1", "printf step2", "printf step4", "printf step6"]
    assert list(marks[-1]) == [*kept, "printf step8"]
    # The refused response is answered within the same call, with its own receipt.
    assert list(marks[-2]) == [*kept, "printf step7"]
    for seen in marks:
        for reply, receipt_id in seen.items():
            assert receipt_id is not None and made[receipt_id] == reply
    # The request that answers the refusal holds it for that one call only.
    answering = plan.index("refused") + 1
    _assert_requests_repeat([r for i, r in enumerate(client.requests) if i != answering])
    for request in client.requests:
        for receipt, reply in _beside(request):
            assert made[receipt["id"]] == _label(reply.get("tool_calls"), reply.get("content"))
    # The first reply shows what it was given, and so does the first after the edit.
    assert [
        _label(reply.get("tool_calls"), reply.get("content"))
        for _, reply in _beside(client.requests[-1])
    ] == ["printf step1", "printf step6"]
    assert _no_host_fields(client.requests)
    assert not any('"_memory' in json.dumps(request) for request in client.requests)
    # The engine is handed the bot's conversation as reference records: the
    # instructions first, and the note about what can run right after the
    # task's state, in every request.
    for request in client.requests:
        assert request[0]["content"].startswith("Reference conversation record")
        assert "You are a persistent bot" in request[0]["content"]
        assert task["id"] not in request[0]["content"]
        at = next(i for i, m in enumerate(request) if "Task state, recorded" in str(m["content"]))
        assert "Offline computer approved for this task: True" in request[at]["content"]
        assert request[at + 1]["content"].startswith("Current execution availability")
