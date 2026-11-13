"""Conversation-based routine authority with real persistence, no live model."""

from types import SimpleNamespace
from uuid import uuid4

import pytest

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import ROUTINE_TOOLS, BotRuntime
from alysis_code.bots.store import Conflict, Store


@pytest.mark.parametrize("tool", ["create_schedule", "propose_schedule"])
def test_relative_reminder_is_fixed_to_request_time_and_runs_once(tmp_path, monkeypatch, tool):
    from alysis_code.bots import store as store_module

    now = 1792356000.0
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: now))
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Remind"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    task = store.enqueue(
        "owner", chat["id"], TaskInput(message="Σε τρία λεπτά", request_id=uuid4().hex)
    )
    due = task["created_at"] + 180
    now += 30  # Queue delay must not move the requested deadline.
    task = store.claim("owner")
    runtime = BotRuntime(store, "owner", lambda _: None)
    args = {
        "name": "Review",
        "prompt": "Review notes",
        "timing": {"kind": "after", "delay_seconds": 180},
    }
    now += 60  # Model/tool latency must not move it either.
    result = runtime._tool(task, tool, args, [tool])
    now += 30
    assert runtime._tool(task, tool, args, [tool])["id"] == result["id"]
    store.finish("owner", task["id"], "completed", content="Saved")
    reopened = Store(store.path)
    schedule = reopened.list_schedules("owner", bot["id"])[0]
    assert schedule["timing"]["kind"] == "once"
    # Both a suggestion and an explicit request wait for the user's own click.
    assert schedule["state"] == "draft"
    assert schedule["next_run_at"] is None
    now += 30
    schedule = reopened.decide_schedule(
        "owner", bot["id"], schedule["id"], "active", schedule["revision"]
    )
    assert schedule["next_run_at"] == due
    assert reopened.enqueue_due_schedules("owner", max_calls=10, now=due - 0.001) == 0
    assert reopened.enqueue_due_schedules("owner", max_calls=10, now=due) == 1
    assert reopened.enqueue_due_schedules("owner", max_calls=10, now=due + 10000) == 0
    assert len(reopened.conversation("owner", chat["id"])["tasks"]) == 2


def test_relative_edit_uses_new_message_time_and_expired_delay_is_rejected(tmp_path, monkeypatch):
    from alysis_code.bots import store as store_module

    now = 1792356000.0
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: now))
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Mira", purpose="Remind"))
    chat = store.create_chat("owner", bot["id"], "Chat")
    runtime = BotRuntime(store, "owner", lambda _: None)

    def request():
        store.enqueue(
            "owner", chat["id"], TaskInput(message="Remind me later", request_id=uuid4().hex)
        )
        return store.claim("owner")

    task = request()
    args = {
        "name": "Review",
        "prompt": "Review notes",
        "timing": {"kind": "after", "delay_seconds": 180},
    }
    rule = runtime._tool(task, "create_schedule", args, list(ROUTINE_TOOLS))
    store.finish("owner", task["id"], "completed", content="Saved")
    now += 90
    edit = request()
    now += 30
    changed = runtime._tool(
        edit,
        "edit_schedule",
        dict(args, schedule_id=rule["id"], expected_revision=rule["revision"], enabled=True),
        list(ROUTINE_TOOLS),
    )
    assert changed["state"] == "paused"
    changed = store.decide_schedule(
        "owner", bot["id"], changed["id"], "active", changed["revision"]
    )
    assert changed["next_run_at"] == edit["created_at"] + 180
    store.finish("owner", edit["id"], "completed", content="Edited")
    stale = request()
    now += 181
    with pytest.raises(Conflict, match="future time"):
        runtime._tool(stale, "create_schedule", args, list(ROUTINE_TOOLS))
    assert len(store.list_schedules("owner", bot["id"])) == 1


def test_explicit_event_request_creates_routine_the_user_turns_on_and_chat_can_edit_pause(tmp_path):
    store = Store(tmp_path / "bots.db")
    owner = "owner"
    bot = store.save_bot(owner, BotInput(name="Mira", purpose="Review notes"))
    chat = store.create_chat(owner, bot["id"], "Chat")

    def request(message):
        store.enqueue(owner, chat["id"], TaskInput(message=message, request_id=uuid4().hex))
        return store.claim(owner)

    runtime = BotRuntime(store, owner, lambda _: None)
    task = request("Watch new files and summarize them here")
    args = {"name": "Notes", "instructions": "Summarize the added notes"}
    rule = runtime._tool(task, "create_trigger", args, list(ROUTINE_TOOLS))
    assert rule["state"] == "draft" and "not on yet" in rule["message"]
    assert runtime._tool(task, "create_trigger", args, list(ROUTINE_TOOLS))["id"] == rule["id"]
    with pytest.raises(Conflict, match="Only the user"):
        runtime._tool(
            task,
            "set_trigger_state",
            {"trigger_id": rule["id"], "expected_revision": rule["revision"], "state": "active"},
            list(ROUTINE_TOOLS),
        )
    store.finish(owner, task["id"], "completed", content="Watching new files")
    edit = request("Use one sentence instead")
    rule = runtime._tool(
        edit,
        "edit_trigger",
        dict(
            args,
            trigger_id=rule["id"],
            expected_revision=rule["revision"],
            instructions="Summarize in one sentence",
        ),
        list(ROUTINE_TOOLS),
    )
    assert rule["state"] == "draft"
    with pytest.raises(Conflict):
        runtime._tool(
            edit,
            "edit_trigger",
            dict(args, trigger_id=rule["id"], expected_revision=1),
            list(ROUTINE_TOOLS),
        )
    rule = runtime._tool(
        edit,
        "set_trigger_state",
        {"trigger_id": rule["id"], "expected_revision": rule["revision"], "state": "paused"},
        list(ROUTINE_TOOLS),
    )
    assert rule["state"] == "paused"
    assert store.usage(owner)["calls"] == 0


def test_explicit_schedule_request_and_edit_preserve_provenance(tmp_path):
    store = Store(tmp_path / "bots.db")
    owner = "owner"
    bot = store.save_bot(owner, BotInput(name="Mira", purpose="Review notes"))
    chat = store.create_chat(owner, bot["id"], "Chat")
    store.enqueue(
        owner, chat["id"], TaskInput(message="Every hour review the notes", request_id=uuid4().hex)
    )
    task = store.claim(owner)
    runtime = BotRuntime(store, owner, lambda _: None)
    args = {
        "name": "Review",
        "prompt": "Review the notes",
        "timing": {"kind": "interval", "every_minutes": 60},
    }
    rule = runtime._tool(task, "create_schedule", args, list(ROUTINE_TOOLS))
    assert rule["state"] == "draft" and rule["next_run_at"] is None
    assert runtime._tool(task, "create_schedule", args, list(ROUTINE_TOOLS))["id"] == rule["id"]
    store.finish(owner, task["id"], "completed", content="Created")
    store.enqueue(
        owner, chat["id"], TaskInput(message="Every two hours instead", request_id=uuid4().hex)
    )
    edit = store.claim(owner)
    rule = runtime._tool(
        edit,
        "edit_schedule",
        dict(
            args,
            schedule_id=rule["id"],
            expected_revision=rule["revision"],
            enabled=True,
            timing={"kind": "interval", "every_minutes": 120},
        ),
        list(ROUTINE_TOOLS),
    )
    assert rule["source_task_id"] == edit["id"]
    assert rule["timing"]["every_minutes"] == 120
    assert rule["state"] == "paused"
    with pytest.raises(Conflict, match="Only the user"):
        runtime._tool(
            edit,
            "set_schedule_state",
            {"schedule_id": rule["id"], "expected_revision": rule["revision"], "state": "active"},
            list(ROUTINE_TOOLS),
        )
    store.finish(owner, edit["id"], "completed", content="Updated")
    rule = store.decide_schedule(owner, bot["id"], rule["id"], "active", rule["revision"])
    assert store.enqueue_due_schedules(owner, max_calls=10, now=rule["next_run_at"]) == 1
    scheduled = store.claim(owner)
    for name, payload in [
        ("create_trigger", {"name": "Bad", "instructions": "Repeat"}),
        ("create_schedule", args),
        (
            "set_schedule_state",
            {"schedule_id": rule["id"], "expected_revision": rule["revision"], "state": "paused"},
        ),
    ]:
        with pytest.raises(Conflict):
            runtime._tool(scheduled, name, payload, list(ROUTINE_TOOLS))
    assert (
        store.schedule_context(owner, scheduled["id"])["original_request"]
        == "Every two hours instead"
    )
