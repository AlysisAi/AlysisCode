"""Every reserved model call says what it was for, so a turn's time and spend can be split."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.store import CALL_PURPOSES, Store

BOTS = Path(__file__).resolve().parents[1] / "src" / "alysis_code" / "bots"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bots.sqlite3")


def _running_task(store, owner="alice"):
    bot = store.save_bot(owner, BotInput(name="Άτλας", purpose="Τακτοποίηση σημειώσεων"))
    chat = store.create_chat(owner, bot["id"], "Νέα συζήτηση")
    task = store.enqueue(
        owner,
        chat["id"],
        TaskInput(message="Οργάνωσε τις σημειώσεις μου", request_id="request_" + chat["id"]),
    )
    assert store.claim(owner)["id"] == task["id"]
    return task


def test_model_started_names_the_call_and_its_purpose(store):
    task = _running_task(store)
    turn = store.reserve_call("alice", task["id"], "model-a", None)
    review = store.reserve_call("alice", task["id"], "model-b", None, purpose="action_review")
    started = [
        event["data"]
        for event in store.task("alice", task["id"])["events"]
        if event["kind"] == "model_started"
    ]
    assert started == [
        {"model": "model-a", "call_id": turn, "purpose": "turn"},
        {"model": "model-b", "call_id": review, "purpose": "action_review"},
    ]
    assert [call["id"] for call in store.task("alice", task["id"])["usage"]] == [turn, review]


def test_an_unknown_purpose_reserves_nothing(store):
    task = _running_task(store)
    with pytest.raises(ValueError):
        store.reserve_call("alice", task["id"], "model-a", None, purpose="something else")
    current = store.task("alice", task["id"])
    assert current["usage"] == []
    assert current["calls"] == 0
    assert "model_started" not in [event["kind"] for event in current["events"]]


def _reserve_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("reserve_call", "_reserve_call")
        ):
            yield node


def test_every_call_site_in_owls_names_a_known_purpose():
    """A new place that calls a model must say what for, or its time lands nowhere."""
    sites = []
    for path in sorted(BOTS.glob("*.py")):
        if path.name == "store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _reserve_calls(tree):
            # The peer exchange's own wrapper (cooperation.py) takes no purpose: its store call
            # below it does.
            if path.name == "cooperation.py" and len(call.args) == 4 and not call.keywords:
                continue
            purpose = next((kw.value for kw in call.keywords if kw.arg == "purpose"), None)
            assert isinstance(purpose, ast.Constant), f"{path.name}:{call.lineno} names no purpose"
            assert purpose.value in CALL_PURPOSES, f"{path.name}:{call.lineno}: {purpose.value}"
            sites.append(purpose.value)
    # Every purpose is used somewhere, and the owl's own turn exactly once.
    assert set(sites) == CALL_PURPOSES
    assert sites.count("turn") == 1
