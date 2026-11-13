from __future__ import annotations

import pytest

import alysis_code.bots.runtime as runtime_module
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import Store
from alysis_code.request_estimation import estimate_request_tokens

TURNS = 40
# A request holds the instructions first, and last the task's state and its
# request; the settled turns are between them.


def long_chat(tmp_path, *, memory):
    """A chat with TURNS settled turns of different sizes and one new request."""
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot(
        "alice", BotInput(name="Atlas", purpose="Help organize notes", memory_enabled=memory)
    )
    chat = store.create_chat("alice", bot["id"], "New conversation")
    for index in range(TURNS):
        message = f"Question {index}: " + "detail " * (5 + index * 7 % 23)
        task = store.enqueue(
            "alice", chat["id"], TaskInput(message=message, request_id=f"request_{index}")
        )
        store.claim("alice")
        store.finish("alice", task["id"], "completed", content="answer " * (10 + index * 13 % 31))
    store.enqueue(
        "alice", chat["id"], TaskInput(message="Newest request", request_id="request_newest")
    )
    return store, store.claim("alice")


@pytest.fixture
def measured(monkeypatch):
    """Count how many messages the context code passes to the token estimate."""
    seen = []

    def counting(messages, tool_list):
        seen.append(len(messages))
        return estimate_request_tokens(messages, tool_list)

    monkeypatch.setattr(runtime_module, "estimate_request_tokens", counting)
    return seen


def test_history_budget_keeps_the_most_turns_that_fit_and_measures_each_turn_once(
    tmp_path, measured
):
    store, task = long_chat(tmp_path, memory=True)
    runtime = BotRuntime(store, "alice", lambda _: None, limits=RuntimeLimits(history_tokens=10**6))
    full = runtime._context(task)
    assert len(full) == 2 * TURNS + 3
    limit = estimate_request_tokens(full[1:-2], None) // 3
    runtime.limits = RuntimeLimits(history_tokens=limit)
    measured.clear()
    bounded = runtime._context(task)
    dropped = len(full) - len(bounded)
    assert dropped > 0 and dropped % 2 == 0
    assert bounded == [full[0], *full[dropped + 1 :]]
    assert bounded[-1]["content"] == "Newest request"
    # The same turns one-at-a-time eviction keeps: the history fits, and it
    # would not fit with one more older turn.
    assert estimate_request_tokens(bounded[1:-2], None) <= limit
    assert estimate_request_tokens(full[dropped - 1 : -2], None) > limit
    # Each turn is measured about once, not once per evicted turn.
    assert sum(measured) <= 3 * len(full)
    assert any(e["kind"] == "context_window" for e in store.task("alice", task["id"])["events"])


def test_request_budget_keeps_the_most_turns_that_fit_and_measures_each_turn_once(
    tmp_path, measured
):
    store, task = long_chat(tmp_path, memory=False)
    runtime = BotRuntime(store, "alice", lambda _: None, limits=RuntimeLimits())
    full = runtime._fit_context(task, runtime._context(task, mark_history=True), [])
    assert len(full) == 2 * TURNS + 3
    # The fixed part (instructions, the task's state and the new request) plus a
    # third of the history.
    fixed = estimate_request_tokens([full[0], *full[-2:]], [])
    limit = fixed + (estimate_request_tokens(full, []) - fixed) // 3
    runtime.limits = RuntimeLimits(context_tokens=limit)
    messages = runtime._context(task, mark_history=True)
    measured.clear()
    wire = runtime._fit_context(task, messages, [])
    dropped = len(full) - len(wire)
    assert dropped > 0 and dropped % 2 == 0
    assert wire == [full[0], *full[dropped + 1 :]]
    assert len(messages) == len(wire)
    assert estimate_request_tokens(wire, []) <= limit
    assert estimate_request_tokens([full[0], *full[dropped - 1 :]], []) > limit
    assert sum(measured) <= 5 * len(full)


def test_character_ceiling_evicts_by_exact_size(tmp_path):
    store, task = long_chat(tmp_path, memory=False)
    runtime = BotRuntime(store, "alice", lambda _: None, limits=RuntimeLimits())
    full = runtime._context(task)
    for keep in (2, 7, 31):
        ceiling = len(runtime_module.encode([full[0], *full[-(2 * keep + 2) :]]))
        runtime.limits = RuntimeLimits(context_characters=ceiling)
        assert runtime._context(task) == [full[0], *full[-(2 * keep + 2) :]]
        runtime.limits = RuntimeLimits(context_characters=ceiling - 1)
        assert runtime._context(task) == [full[0], *full[-(2 * keep) :]]
