"""A request repeats the one before it, so a provider's prompt cache can be used.

A provider caches a request by its leading bytes: the tools, then the messages
in order. While a conversation only grows, each request must therefore begin
with everything the previous one sent before its own closing notes. These tests
check that as a property of the requests, with a scripted model.
"""

from __future__ import annotations

import copy
import json
import random
from types import SimpleNamespace
from typing import Any

import pytest
from test_agent_loop_failed_turn_history import _Surface

from alysis_code.agent.session import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.local_memory import MemoryStore
from alysis_code.memory_evidence import (
    GUIDANCE,
    HISTORY_BUDGET,
    MAX_RECEIPTS,
    RECEIPT_ID_KEY,
    bind_session_receipts,
    delivery,
    note_session_receipt,
    project_history,
    source,
)

WIRE_KEYS = ("role", "content", "tool_calls", "tool_call_id", "name")


def _wire(message: dict[str, Any]) -> str:
    return json.dumps({k: message[k] for k in WIRE_KEYS if k in message}, sort_keys=True)


def _conversation(request: list[dict[str, Any]]) -> list[str]:
    """The request without the notes it adds after the conversation."""
    end = len(request)
    while end and request[end - 1].get("role") == "system":
        end -= 1
    return [_wire(message) for message in request[:end]]


def _assert_grows(requests: list[list[dict[str, Any]]]) -> None:
    for earlier, later in zip(requests, requests[1:], strict=False):
        before, after = _conversation(earlier), _conversation(later)
        assert after[: len(before)] == before


class _Recorder:
    model = "test-model"
    temperature = 0.2

    def __init__(self) -> None:
        self.responses: Any = iter(())
        self.requests: list[list[dict[str, Any]]] = []
        self.tools: list[str] = []

    def chat(self, *, messages, tools=None, **kwargs):
        self.requests.append(copy.deepcopy(messages))
        self.tools.append(json.dumps(tools or [], sort_keys=True))
        return next(self.responses)


def _call(index: int, name: str, arguments: dict[str, Any], text: str = "") -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[ToolCall(f"c{index}", name, arguments)], raw={})


def _answer(text: str) -> LLMResponse:
    return LLMResponse(content=text, tool_calls=[], raw={})


@pytest.mark.parametrize("says_what_it_does", [False, True])
def test_a_chat_repeats_its_earlier_requests(tmp_path, monkeypatch, says_what_it_does):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    workspace = tmp_path / "workspace"
    (workspace / "pkg").mkdir(parents=True)
    for index in range(8):
        (workspace / "pkg" / f"mod_{index}.py").write_text("x = 1\n" * 40, encoding="utf-8")
    (workspace / "check_all.py").write_text("print('ok')\n", encoding="utf-8")

    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=True)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {"test-model": {"context_window_tokens": 200000, "max_output_tokens": 8000}}
        }
    }
    client = _Recorder()
    session = create_session(
        cfg=cfg,
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=40,
        no_log=False,
        client_override=client,
        compactor_client_override=client,
        hooks_enabled=False,
        surface=_Surface(),
        non_interactive=True,
        subagents_enabled=False,
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
    )
    reads = 12
    turns = [
        (
            "Read only pkg/mod_0.py and say what it holds.",
            [_call(1, "fs_read", {"path": "pkg/mod_0.py"}), _answer("It holds x = 1 lines.")],
        ),
        (
            "Now read the other modules one at a time and make `python3 check_all.py` pass.",
            [
                _call(
                    10 + index,
                    "fs_read",
                    {"path": f"pkg/mod_{index % 8}.py"},
                    text="Reading the next module." if says_what_it_does and index % 2 else "",
                )
                for index in range(reads)
            ]
            + [_answer("All modules read; nothing needed changing.")],
        ),
        (
            "Write REPORT.md with one line per module.",
            [
                _call(90, "fs_write", {"path": "REPORT.md", "content": "mod_0\n"}),
                _answer("REPORT.md written."),
            ],
        ),
    ]
    try:
        for text, responses in turns:
            client.responses = iter([*responses, *[_answer("Done.")] * 6])
            session.run_turn(text)
    finally:
        session.close()

    assert len(client.requests) >= reads + 4
    assert len(set(client.tools)) == 1  # the same tools, byte for byte, in every turn
    _assert_grows(client.requests)


# --- saved memory ---------------------------------------------------------------
#
# With saved memory the host shows, beside a reply, a receipt of the memory that
# reply was given. Those receipts are part of the conversation as it is sent, so
# they must stay where they are, and say what they said, while it grows.

CLOSING_NOTES = (
    "Historical receipt coverage",
    "Historical memory inputs for responses no longer present",
    "Current memory observation",
)


def _is_receipt(message: dict[str, Any]) -> bool:
    return str(message.get("content") or "").startswith(GUIDANCE)


def _is_closing_note(message: dict[str, Any]) -> bool:
    return str(message.get("content") or "").startswith(CLOSING_NOTES)


def _sent_conversation(request: list[dict[str, Any]]) -> list[str]:
    end = len(request)
    while end and (request[end - 1].get("role") == "system" or _is_closing_note(request[end - 1])):
        end -= 1
    assert not any(_is_closing_note(message) for message in request[:end])
    return [_wire(message) for message in request[:end]]


@pytest.mark.parametrize("says_what_it_does", [False, True])
def test_a_chat_with_saved_memory_repeats_its_earlier_requests(
    tmp_path, monkeypatch, says_what_it_does
):
    monkeypatch.setenv("ALYSIS_MEMORY_HOME", str(tmp_path / "memory"))
    workspace = tmp_path / "workspace"
    (workspace / "pkg").mkdir(parents=True)
    for index in range(8):
        (workspace / "pkg" / f"mod_{index}.py").write_text("x = 1\n" * 40, encoding="utf-8")
    (workspace / "check_all.py").write_text("print('ok')\n", encoding="utf-8")
    store = MemoryStore()
    for index in range(6):
        store.change({"action": "save", "content": f"Note {index}: checks live in check_all.py."})
    cfg = AppConfig(model="test-model", routing_mode="code_only", stream=True)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {"test-model": {"context_window_tokens": 200000, "max_output_tokens": 8000}}
        }
    }
    client = _Recorder()
    session = create_session(
        cfg=cfg,
        root=workspace,
        mode="auto",
        yes=True,
        max_steps=60,
        no_log=False,
        client_override=client,
        compactor_client_override=client,
        hooks_enabled=False,
        surface=_Surface(),
        non_interactive=True,
        subagents_enabled=False,
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
    )
    reads = MAX_RECEIPTS + 6  # more model calls than the recent window of receipts holds
    turns = [
        (
            "Read only pkg/mod_0.py and say what it holds.",
            [_call(1, "fs_read", {"path": "pkg/mod_0.py"}), _answer("It holds x = 1 lines.")],
        ),
        (
            "Now read the other modules one at a time and make `python3 check_all.py` pass.",
            [
                _call(
                    10 + index,
                    "memory_read" if index == 5 else "fs_read",
                    {} if index == 5 else {"path": f"pkg/mod_{index % 8}.py"},
                    text="Reading the next module." if says_what_it_does and index % 2 else "",
                )
                for index in range(reads)
            ]
            + [_answer("All modules read; nothing needed changing.")],
        ),
        (
            "Write REPORT.md with one line per module.",
            [
                _call(90, "fs_write", {"path": "REPORT.md", "content": "mod_0\n"}),
                _answer("REPORT.md written."),
            ],
        ),
    ]
    try:
        for text, responses in turns:
            client.responses = iter([*responses, *[_answer("Done.")] * 6])
            session.run_turn(text)
            if text.startswith("Now read"):
                # The saved memory changes between turns.
                store.change({"action": "save", "content": "Note 6: reports go in REPORT.md."})
    finally:
        session.close()

    assert len(client.requests) >= reads + 4
    assert len(set(client.tools)) == 1
    conversations = [_sent_conversation(request) for request in client.requests]
    for before, after in zip(conversations, conversations[1:], strict=False):
        assert after[: len(before)] == before
    shown = [sum(_is_receipt(m) for m in request) for request in client.requests]
    assert shown[1] == 1  # the first reply's receipt is there from the request after it
    # Beside replies: the memory as it was, and what memory_read returned. Not
    # one beside every reply.
    assert shown == sorted(shown) and shown[-1] == 2
    # The memory as changed between the turns found no room beside a reply and
    # is listed after the conversation.
    assert any(
        str(m.get("content") or "").startswith("Historical memory inputs")
        for m in client.requests[-1]
    )


def _snapshot(scope: str, version: int, *, records: bool = True) -> dict[str, Any]:
    rows = [{"id": "note", "revision": version, "content": f"body {version}"}] if records else []
    return {**source(scope, rows, version=version), "source_ref": f"ref-{version}-{records}"}


def _random_conversation(rng: random.Random, scope: str, *, strays: bool = False):
    """Yield (messages, receipts, made) as a conversation grows by one reply at a time.

    ``made`` lists what the host learns as it happens: for each receipt, the
    message that was last when it was made and the reply it produced (None for
    a response that was thrown away). With ``strays`` there are such responses,
    and replies made without any request.
    """
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    receipts: list[dict[str, Any]] = []
    made: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]] = []
    version, turn = 1, ""
    for step in range(rng.randint(30, 70)):
        if not turn or rng.random() < 0.15:
            turn = f"turn-{step}"
            messages.append({"role": "user", "content": f"request {step}", "_memory_turn_id": turn})
        if rng.random() < 0.1:
            version += 1
        sources = [_snapshot(scope, version, records=rng.random() > 0.1)]
        if rng.random() < 0.1:  # what a memory_read returned in this step
            sources.append(
                {
                    **_snapshot(scope, version),
                    "source_ref": f"read-{version}",
                    "tool_call_id": f"t{step}",
                    "delivery_kind": "tool_result",
                }
            )
        if rng.random() < 0.05:
            sources.append(_snapshot("another:scope", version))
        # Some replies say the same thing; many say nothing.
        content = rng.choice(["", "", f"reply {step}", "Done.", "Reading the next file."])
        if strays and rng.random() < 0.1:  # a reply the host wrote itself
            messages.append({"role": "assistant", "content": content})
        if strays and rng.random() < 0.1:  # a response that was thrown away
            thrown = delivery(sources, content, turn_id=turn)
            receipts.append(thrown)
            made.append((thrown, messages[-1], None))
            if rng.random() < 0.5:
                # ... and the turn ended there. The next one is given no memory
                # and its reply happens to say the same.
                messages.append({"role": "user", "content": f"again {step}"})
                messages.append({"role": "assistant", "content": content})
        receipt = delivery(sources, content, turn_id=turn)
        if receipts and rng.random() < 0.3:  # a link to an excerpt shown earlier
            earlier = rng.choice(receipts)
            receipt["historical_reads"] = [
                {
                    "scope": scope,
                    "receipt_id": earlier["id"],
                    "turn_id": earlier.get("turn_id"),
                    "content_range": {"start": 0, "end": 10, "total": 10},
                }
            ]
        last = messages[-1]
        reply: dict[str, Any] = {"role": "assistant", "content": content}
        if rng.random() < 0.7:
            reply["tool_calls"] = [{"id": f"c{step}", "name": "fs_read", "arguments": {}}]
            messages += [reply, {"role": "tool", "tool_call_id": f"c{step}", "content": "ok"}]
        else:
            messages.append(reply)
        receipts.append(receipt)
        made.append((receipt, last, reply))
        yield messages, receipts, made


def _session_with(messages, receipts):
    session = SimpleNamespace(messages=messages, store=None)
    session._memory_delivery_state = {
        "cursor": 0,
        "receipts": receipts,
        "bound": {},
        "made_after": {},
    }
    return session


def _look(session, messages, receipts, made, *, noted: set[str]):
    """What the host does on each request: note the new receipts, look at replies."""
    session.messages = messages
    state = session._memory_delivery_state
    state["receipts"] = receipts[-MAX_RECEIPTS:]
    for receipt, last, _ in made:
        if receipt["id"] not in noted:
            noted.add(receipt["id"])
            state["made_after"][receipt["id"]] = last
    return bind_session_receipts(session)


def _check_projection(messages, projected, scope) -> list[str]:
    """What must hold of any projection; returns the conversation as sent."""
    conversation = [
        m for m in projected if not _is_closing_note(m) or not m.get("_memory_evidence_context")
    ]
    # Without the receipts the conversation is exactly what was put in.
    assert [_wire(m) for m in conversation if not _is_receipt(m)] == [_wire(m) for m in messages]
    # A receipt is shown only directly before a reply.
    for index, message in enumerate(conversation):
        if _is_receipt(message):
            assert conversation[index + 1].get("role") == "assistant"
            assert f'"scope": "{scope}"' in message["content"]
    evidence = sum(len(m["content"]) for m in projected if m.get("_memory_evidence_context"))
    assert evidence <= HISTORY_BUDGET
    return [_wire(m) for m in conversation]


@pytest.mark.parametrize("strays", [False, True])
@pytest.mark.parametrize("seed", range(30))
def test_receipts_keep_their_place_as_a_conversation_grows(seed, strays):
    """Random conversations: replies with and without text and with the same
    text, several turns, memory changing at random points, tool reads, links to
    earlier receipts, a second scope, more replies than the window holds, and
    (with ``strays``) thrown-away responses and replies made without a request.
    The host's own bookkeeping, as in a session, with only the recent window."""
    scope = "local:test"
    session, noted, previous = _session_with([], []), set(), None
    conversation = _random_conversation(random.Random(seed), scope, strays=strays)
    for messages, receipts, made in conversation:
        bound = _look(session, messages, receipts, made, noted=noted)
        window = receipts[-MAX_RECEIPTS:]
        projected = project_history(
            messages, window, scope=scope, include_current=False, bound=bound
        )
        again = project_history(messages, window, scope=scope, include_current=False, bound=bound)
        assert [_wire(m) for m in again] == [_wire(m) for m in projected]
        sent = _check_projection(messages, projected, scope)
        # Each reply keeps the receipt of the request that produced it, and no other.
        owner = {id(reply): receipt["id"] for receipt, _, reply in made if reply is not None}
        for message in messages:
            if message.get("role") == "assistant":
                assert message.get(RECEIPT_ID_KEY) == owner.get(id(message))
        if previous is not None:
            assert sent[: len(previous)] == previous
        previous = sent


@pytest.mark.parametrize("seed", range(20))
def test_receipts_keep_their_place_again_after_old_messages_are_removed(seed):
    """Compaction takes messages from the front. The receipts may then be shown
    beside other replies, once; after that they stay where they are again."""
    rng = random.Random(1000 + seed)
    scope = "local:test"
    session, noted = _session_with([], []), set()
    removed, previous = 0, None
    for messages, receipts, made in _random_conversation(rng, scope, strays=True):
        # As in a turn: the host looks at the new reply when it builds the next
        # request, and only then may the conversation be compacted.
        _look(session, [messages[0], *messages[max(1, removed) :]], receipts, made, noted=noted)
        if len(messages) - removed > 12 and rng.random() < 0.1:
            cut = removed + rng.randint(1, len(messages) - removed - 2)
            while messages[cut].get("role") == "tool":  # a result stays with its call
                cut += 1
            removed, previous = cut, None
        kept = [messages[0], *messages[max(1, removed) :]]
        bound = _look(session, kept, receipts, made, noted=noted)
        projected = project_history(
            kept, receipts[-MAX_RECEIPTS:], scope=scope, include_current=False, bound=bound
        )
        sent = _check_projection(kept, projected, scope)
        owner = {id(reply): receipt["id"] for receipt, _, reply in made if reply is not None}
        for message in kept:
            if message.get("role") == "assistant":
                assert message.get(RECEIPT_ID_KEY) == owner.get(id(message))
        if previous is not None:
            assert sent[: len(previous)] == previous
        previous = sent


def test_a_state_is_shown_beside_the_first_reply_that_was_given_it():
    scope = "local:test"
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "first", "_memory_turn_id": "t1"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "second", "_memory_turn_id": "t2"},
        {"role": "assistant", "content": "two"},
        {"role": "user", "content": "third", "_memory_turn_id": "t3"},
        {"role": "assistant", "content": "three"},
    ]
    receipts = [
        delivery([_snapshot(scope, 1)], "one", turn_id="t1"),
        delivery([_snapshot(scope, 1)], "two", turn_id="t2"),
        delivery([_snapshot(scope, 2)], "three", turn_id="t3"),
    ]

    session = _session_with(messages, receipts)

    def beside(history):
        session.messages = history
        bound = bind_session_receipts(session)
        out = project_history(history, receipts, scope=scope, include_current=False, bound=bound)
        return [out[i + 1]["content"] for i, m in enumerate(out) if _is_receipt(m)]

    # The memory as it was, beside the first reply given it; the changed memory
    # beside the reply that was first given that.
    assert beside(messages) == ["one", "three"]
    # Once the first reply has been summarised away, the next one carries it.
    assert beside([messages[0], *messages[3:]]) == ["two", "three"]


def test_a_caller_that_keeps_nothing_gets_the_newest_reply_of_each_state():
    """A conversation handed over as reference text keeps nothing beside its
    messages; only the recent receipts are passed."""
    scope = "local:test"
    messages = [
        {"role": "user", "content": "first", "_memory_turn_id": "t1"},
        {"role": "assistant", "content": "one"},
        {"role": "user", "content": "second", "_memory_turn_id": "t2"},
        {"role": "assistant", "content": "two"},
    ]
    receipts = [
        delivery([_snapshot(scope, 1)], "one", turn_id="t1"),
        delivery([_snapshot(scope, 1)], "two", turn_id="t2"),
    ]
    out = project_history(messages, receipts, scope=scope, include_current=False)
    assert [out[i + 1]["content"] for i, m in enumerate(out) if _is_receipt(m)] == ["two"]
    assert all(RECEIPT_ID_KEY not in message for message in messages)


def test_replies_of_other_turns_leave_room_for_the_turn_being_worked_on():
    """A bot's conversation also holds the replies of finished tasks, each
    given another state of the memory. They are settled first, the newest
    first, within half of the room, so the turn being worked on shows what it
    was given; and what a present reply of another turn was given is not sent
    again after the conversation."""
    scope = "local:test"
    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    bound: dict[str, dict[str, Any]] = {}

    def reply(turn, state, text, *, finished):
        receipt = delivery([_snapshot(scope, state)], text, turn_id=turn)
        bound[receipt["id"]] = receipt
        messages.append(
            {
                "role": "assistant",
                "content": text,
                RECEIPT_ID_KEY: receipt["id"],
                **({"_memory_turn_id": turn} if finished else {}),
            }
        )

    for n in range(4):
        messages.append({"role": "user", "content": f"request {n}", "_memory_turn_id": f"t{n}"})
        reply(f"t{n}", n + 1, f"reply {n}", finished=True)
    messages.append({"role": "user", "content": "request now", "_memory_turn_id": "now"})
    reply("now", 9, "working", finished=False)

    def beside(**options):
        out = project_history(
            messages,
            list(bound.values()),
            scope=scope,
            include_current=False,
            bound=bound,
            **options,
        )
        return [out[i + 1]["content"] for i, m in enumerate(out) if _is_receipt(m)]

    # In conversation order alone, the earlier turns take all of the room.
    assert beside() == ["reply 0", "reply 1", "reply 2"]
    assert beside(current_turn="now") == ["reply 1", "reply 2", "reply 3", "working"]
    # Nothing moves while the turn grows, and a second state has room too.
    reply("now", 10, "more work", finished=False)
    assert beside(current_turn="now") == ["reply 1", "reply 2", "reply 3", "working", "more work"]
    # The state of "reply 0" found no room. Its reply is present, so it is not
    # sent again with every request in the note after the conversation.
    out = project_history(
        messages,
        list(bound.values()),
        scope=scope,
        include_current=False,
        bound=bound,
        current_turn="now",
    )
    assert not any(
        str(m.get("content")).startswith("Historical memory inputs for responses") for m in out
    )


def test_a_filed_message_keeps_no_field_the_host_holds_about_memory():
    """Compaction files the messages it removes where history_search can read
    them back to the model, a line at a time."""
    from alysis_code.compaction.conversation_compactor import ConversationCompactor
    from alysis_code.llm.metadata import PROVIDER_METADATA_KEY

    message = {
        "role": "assistant",
        "content": "Done.",
        RECEIPT_ID_KEY: "0" * 32,
        "_memory_turn_id": "turn-1",
        PROVIDER_METADATA_KEY: {"anthropic_messages": {"message_id": "m1"}},
    }
    payload = ConversationCompactor._history_chunk_payload(idx=4, message=message)
    assert payload["message"] == {"role": "assistant", "content": "Done."}
    assert payload["internal_message"][PROVIDER_METADATA_KEY] == message[PROVIDER_METADATA_KEY]
    assert "_memory" not in json.dumps(payload)
    assert RECEIPT_ID_KEY in message  # the conversation's own message is untouched


def test_a_newer_state_of_the_memory_stays_visible_when_there_are_many_notes():
    scope = "local:test"

    def notes(version):
        rows = [
            {"id": f"note-{i}", "revision": version, "updated_at": "2026-11-11T10:00:00Z"}
            for i in range(12)
        ]
        return {**source(scope, rows, version=version), "source_ref": f"ref-{version}"}

    messages, receipts = [{"role": "system", "content": "s"}], []
    for version in range(1, 7):  # the memory is edited before every turn
        turn = f"t{version}"
        messages += [
            {"role": "user", "content": f"request {version}", "_memory_turn_id": turn},
            {"role": "assistant", "content": f"reply {version}"},
        ]
        receipts.append(delivery([notes(version)], f"reply {version}", turn_id=turn))
    bound = bind_session_receipts(_session_with(messages, receipts))
    out = project_history(messages, receipts, scope=scope, include_current=False, bound=bound)
    sent = json.dumps([m["content"] for m in out if m.get("_memory_evidence_context")])
    beside = [out[i + 1]["content"] for i, m in enumerate(out) if _is_receipt(m)]
    # The first states keep their places beside their replies ...
    assert beside[:2] == ["reply 1", "reply 2"] and len(beside) >= 3
    # ... and the newest one, with no room there, is shown after the conversation.
    assert "ref-6" in sent
    assert sum(len(m["content"]) for m in out if m.get("_memory_evidence_context")) <= (
        HISTORY_BUDGET
    )


@pytest.mark.parametrize("as_a_bot", [False, True])
def test_the_newest_state_is_shown_when_all_the_room_is_taken(as_a_bot):
    """Every turn is given another state of a memory with many notes. Whatever
    the replies have used, the newest state that none of them shows has a
    place after the conversation, and the whole stays within its size."""
    scope = "local:test"

    def notes(version):
        rows = [
            {"id": f"note-{i}", "revision": version, "updated_at": "2026-11-11T10:00:00Z"}
            for i in range(12)
        ]
        return {**source(scope, rows, version=version), "source_ref": f"ref-{version}"}

    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    bound: dict[str, dict[str, Any]] = {}
    for version in range(1, 13):
        receipt = delivery([notes(version)], f"reply {version}", turn_id="now")
        bound[receipt["id"]] = receipt
        messages += [
            {"role": "user", "content": f"request {version}"},
            {"role": "assistant", "content": f"reply {version}", RECEIPT_ID_KEY: receipt["id"]},
        ]
        out = project_history(
            messages,
            list(bound.values()),
            scope=scope,
            include_current=False,
            bound=bound,
            **({"current_turn": "now"} if as_a_bot else {}),
        )
        evidence = [m["content"] for m in out if m.get("_memory_evidence_context")]
        assert f"ref-{version}" in json.dumps(evidence)
        assert sum(map(len, evidence)) <= HISTORY_BUDGET
    beside = [out[i + 1]["content"] for i, m in enumerate(out) if _is_receipt(m)]
    assert "reply 12" not in beside  # no room beside its reply: it is in the note


@pytest.mark.parametrize("as_a_bot", [False, True])
@pytest.mark.parametrize("tool_reads", [3, 40])
def test_the_place_of_the_newest_state_never_costs_the_size_or_the_comparison(as_a_bot, tool_reads):
    """The newest state was given with several memory reads, so its receipt is
    large, and the room for receipts is already used. A few reads: it has its
    place, taken from the room for source text. Very many: it is left out, as
    any receipt without room. Either way the whole keeps its size and the
    comparison with current memory is sent."""
    scope = "local:test"

    def state(version, reads=0):
        rows = [
            {"id": f"note-{i}", "revision": version, "updated_at": "2026-11-11T10:00:00Z"}
            for i in range(12)
        ]
        read = [
            {
                **source(scope, rows[:1], version=version),
                "source_ref": f"ref-{version}-read-{n}",
                "tool_call_id": f"call-{version}-{n}",
                "delivery_kind": "tool_result",
            }
            for n in range(reads)
        ]
        return [{**source(scope, rows, version=version), "source_ref": f"ref-{version}"}, *read]

    def archived(*, receipt_id, offset=0, max_chars=12000):
        end = min(20000, offset + max_chars)
        return {
            "receipt_id": receipt_id,
            "scope": scope,
            "turn_id": "now",
            "content": ("memory text " * 2000)[offset:end],
            "content_range": {"start": offset, "end": end, "total": 20000},
            "next_offset": end if end < 20000 else None,
        }

    messages: list[dict[str, Any]] = [{"role": "system", "content": "s"}]
    bound: dict[str, dict[str, Any]] = {}
    for version in range(1, 9):
        sources = state(version, tool_reads if version == 8 else 0)
        receipt = delivery(sources, f"reply {version}", turn_id="now")
        bound[receipt["id"]] = receipt
        messages += [
            {"role": "user", "content": f"request {version}"},
            {"role": "assistant", "content": f"reply {version}", RECEIPT_ID_KEY: receipt["id"]},
        ]
    out = project_history(
        messages,
        list(bound.values()),
        scope=scope,
        current=state(8)[0],
        source_reader=archived,
        bound=bound,
        **({"current_turn": "now"} if as_a_bot else {}),
    )
    evidence = [m["content"] for m in out if m.get("_memory_evidence_context")]
    assert sum(map(len, evidence)) <= HISTORY_BUDGET
    assert any(text.startswith("Current memory observation") for text in evidence)
    assert ('"ref-8"' in json.dumps(evidence).replace("\\", "")) is (tool_reads == 3)


def test_a_resumed_chat_shows_each_receipt_where_it_was():
    """After a compaction the saved conversation carries, on each reply, the id
    of the receipt it kept; a resumed chat gets its recent receipts back in a
    marker message. Together they put every receipt back beside its reply."""
    from alysis_code.memory_evidence import history_marker

    scope = "local:test"
    rng = random.Random(7)
    session, noted = _session_with([], []), set()
    for messages, receipts, made in _random_conversation(rng, scope):  # noqa: B007
        bound = _look(session, messages, receipts, made, noted=noted)
        if len(receipts) == MAX_RECEIPTS - 4:
            break
    before = project_history(messages, receipts, scope=scope, include_current=False, bound=bound)
    restored = [history_marker(receipts), *copy.deepcopy(messages)]
    resumed = SimpleNamespace(messages=restored, store=None)
    after = project_history(
        restored,
        receipts,
        scope=scope,
        include_current=False,
        bound=bind_session_receipts(resumed),
    )
    assert [_wire(m) for m in after] == [_wire(m) for m in before]
    assert any(_is_receipt(m) for m in after)


def test_a_reply_that_never_had_a_receipt_is_not_given_one_later():
    scope = "local:test"
    turn = {"role": "user", "content": "go", "_memory_turn_id": "t1"}
    first, second = {"role": "assistant", "content": ""}, {"role": "assistant", "content": ""}
    session = _session_with([turn, first], [])  # a reply made before any memory was recorded
    bind_session_receipts(session)
    discarded = delivery([_snapshot(scope, 1)], "", turn_id="t1")  # its reply was thrown away
    kept = delivery([_snapshot(scope, 1)], "", turn_id="t1")
    for receipt in (discarded, kept):
        session._memory_delivery_state["receipts"].append(receipt)
        note_session_receipt(session, receipt)
    session.messages.append(second)
    bound = bind_session_receipts(session)
    assert RECEIPT_ID_KEY not in first
    assert second[RECEIPT_ID_KEY] == kept["id"] and list(bound) == [kept["id"]]
    # The receipt nobody answered stays out of the conversation for good.
    session.messages.append({"role": "assistant", "content": ""})  # the host's own, later
    assert list(bind_session_receipts(session)) == [kept["id"]]
    assert RECEIPT_ID_KEY not in first and RECEIPT_ID_KEY not in session.messages[-1]
    projected = project_history(
        session.messages, [discarded, kept], scope=scope, include_current=False, bound=bound
    )
    at = next(i for i, m in enumerate(projected) if _is_receipt(m))
    assert projected[at + 1] is second and kept["id"] in projected[at]["content"]


def test_a_reply_in_a_turn_given_no_memory_does_not_take_the_receipt_before_it():
    """Two replies with the same text, the second made in a turn without memory:
    the receipt belongs to the first, whenever the host comes to look."""
    scope = "local:test"
    turn = {"role": "user", "content": "go", "_memory_turn_id": "t1"}
    session = _session_with([turn], [])
    receipt = delivery([_snapshot(scope, 1)], "Done.", turn_id="t1")
    session._memory_delivery_state["receipts"].append(receipt)
    note_session_receipt(session, receipt)
    real = {"role": "assistant", "content": "Done."}
    later = {"role": "assistant", "content": "Done."}
    session.messages += [real, {"role": "user", "content": "and again"}, later]
    bound = bind_session_receipts(session)
    assert real[RECEIPT_ID_KEY] == receipt["id"] and RECEIPT_ID_KEY not in later
    assert list(bound) == [receipt["id"]]


@pytest.mark.parametrize("seed", range(20))
def test_receipts_keep_their_place_across_a_restart(seed):
    """A resumed chat has its messages again but not what the host kept beside
    them, only the recent receipts. While those cover the whole conversation it
    is sent exactly as before; in any case it grows as before from then on."""
    rng = random.Random(3000 + seed)
    scope = "local:test"
    session, noted = _session_with([], []), set()
    restart_at, previous, before_restart = rng.randint(5, 40), None, None
    for step, (messages, receipts, made) in enumerate(_random_conversation(rng, scope)):
        window = receipts[-MAX_RECEIPTS:]
        if step == restart_at:
            # Messages as a resumed chat rebuilds them: the same text, new objects.
            messages[:] = [
                {k: v for k, v in message.items() if k != RECEIPT_ID_KEY} for message in messages
            ]
            session, before_restart, previous = _session_with(messages, window), previous, None
            # Nothing is known any more of where the earlier receipts were made.
            noted = {receipt["id"] for receipt in receipts}
        bound = _look(session, messages, receipts, made, noted=noted)
        projected = project_history(
            messages, window, scope=scope, include_current=False, bound=bound
        )
        sent = _check_projection(messages, projected, scope)
        # No reply keeps another reply's receipt, before or after the restart.
        owner = {
            reply["content"] + str(i): receipt["id"] for i, (receipt, _, reply) in enumerate(made)
        }
        replies = [m for m in messages if m.get("role") == "assistant"]
        for index, message in enumerate(replies):
            if RECEIPT_ID_KEY in message:
                assert message[RECEIPT_ID_KEY] == owner[message["content"] + str(index)]
        if previous is not None:
            assert sent[: len(previous)] == previous
        if step == restart_at and before_restart is not None and len(receipts) <= MAX_RECEIPTS:
            assert sent[: len(before_restart)] == before_restart
        previous = sent


class _Log:
    """A session log, as far as the receipts read it."""

    def __init__(self, events):
        self.events = list(events)

    def events_since(self, cursor):
        return self.events[cursor:], len(self.events)


def test_a_resumed_chat_that_was_once_cleared_still_shows_receipts_where_they_were():
    from alysis_code.memory_evidence import EVENT, history_marker

    scope = "local:test"
    before_clear = delivery([_snapshot(scope, 1)], "old", turn_id="t0")
    receipt = delivery([_snapshot(scope, 2)], "one", turn_id="t1")
    reply = {"role": "assistant", "content": "one", RECEIPT_ID_KEY: receipt["id"]}
    messages = [
        history_marker([receipt]),
        {"role": "user", "content": "first", "_memory_turn_id": "t1"},
        reply,
    ]
    log = _Log(
        [
            {"type": EVENT, "payload": before_clear},
            {"type": "conversation_cleared", "payload": {}},
            {"type": EVENT, "payload": receipt},
        ]
    )
    resumed = SimpleNamespace(messages=messages, store=log)
    bound = bind_session_receipts(resumed)
    assert list(bound) == [receipt["id"]]
    out = project_history(messages, [receipt], scope=scope, include_current=False, bound=bound)
    at = next(i for i, m in enumerate(out) if _is_receipt(m))
    assert out[at + 1] is reply
    # A clearing in the running chat does take them away.
    log.events.append({"type": "conversation_cleared", "payload": {}})
    assert bind_session_receipts(resumed) == {}


def test_a_thrown_away_response_gives_its_receipt_to_no_later_reply():
    """The response was thrown away and the turn ended. The next turn is given
    no memory, and its first reply has the same (empty) text."""
    scope = "local:test"
    turn = {"role": "user", "content": "go", "_memory_turn_id": "t1"}
    session = _session_with([turn], [])
    thrown = delivery([_snapshot(scope, 1)], "", turn_id="t1")
    session._memory_delivery_state["receipts"].append(thrown)
    note_session_receipt(session, thrown)
    later = {"role": "assistant", "content": ""}
    session.messages += [{"role": "user", "content": "now without memory"}, later]
    assert bind_session_receipts(session) == {}
    assert RECEIPT_ID_KEY not in later
    # What the host held to tell them apart is let go once it has looked.
    assert session._memory_delivery_state["made_after"] == {}


def test_the_newest_state_comes_first_after_the_conversation():
    """Three states found no room beside a reply. The newest is listed first
    there, also when its reply said nothing and older replies did."""
    scope = "local:test"

    def notes(version):
        rows = [{"id": f"note-{i}", "revision": version} for i in range(12)]
        return {**source(scope, rows, version=version), "source_ref": f"ref-{version}"}

    messages, receipts = [{"role": "system", "content": "s"}], []
    for version in range(1, 9):
        turn, text = f"t{version}", "" if version == 8 else f"reply {version}"
        messages += [
            {"role": "user", "content": f"request {version}", "_memory_turn_id": turn},
            {"role": "assistant", "content": text},
        ]
        receipts.append(delivery([notes(version)], text, turn_id=turn))
    bound = bind_session_receipts(_session_with(messages, receipts))
    out = project_history(messages, receipts, scope=scope, include_current=False, bound=bound)
    block = next(m["content"] for m in out if m["content"].startswith("Historical memory inputs"))
    listed = json.loads(block.split("\n", 1)[1])
    assert listed[-1]["id"] == receipts[-1]["id"]  # the block lists oldest to newest
