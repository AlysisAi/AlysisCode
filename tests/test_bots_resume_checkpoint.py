"""A task paused for the person continues from its own checkpoint; other tasks still wait."""

import os
from uuid import uuid4

import pytest

from alysis_code.bots.browser_state import BrowserStates
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.object_storage import LocalObjectStorage
from alysis_code.bots.store import Conflict, Store
from alysis_code.bots.workspace_objects import WorkspaceObjects


@pytest.fixture
def paused(tmp_path):
    store = Store(tmp_path / "db", objects=LocalObjectStorage(tmp_path / "objects"))
    bot = store.save_bot("owner", BotInput(name="Resume", purpose="Checkpoints"))["id"]
    chat = store.create_chat("owner", bot, "Chat")
    task = store.enqueue(
        "owner", chat["id"], TaskInput(message="Work that pauses", request_id=uuid4().hex)
    )["id"]
    assert store.claim("owner")["id"] == task
    return store, bot, task


def test_workspace_checkpoint_of_a_paused_task_is_its_own_starting_point(paused):
    store, bot, task = paused
    workspace = WorkspaceObjects(store)
    first = workspace.stage("owner", bot, task, [b"at the pause"], 12)
    for opener in (None, "another-task"):
        with pytest.raises(Conflict, match="finalizing"):
            workspace.current("owner", bot, task_id=opener)
    assert workspace.current("owner", bot, task_id=task) == first
    second = workspace.stage("owner", bot, task, [b"after approval"], 14)  # paused again
    assert workspace.current("owner", bot, task_id=task) == second
    with store.connect() as db:
        rows = db.execute(
            "SELECT COUNT(*) FROM workspace_objects WHERE task_id=? AND settled=0", (task,)
        ).fetchone()[0]
    assert rows == 1
    store.finish("owner", task, "completed", publish_reply=False)
    assert workspace.current("owner", bot) == second


def test_browser_storage_of_a_paused_task_is_its_own_starting_point(paused):
    store, bot, task = paused
    states = BrowserStates(store, os.urandom(32))
    states.stage("owner", bot, task, b"cookies at the pause")
    with pytest.raises(Conflict, match="finalizing"):
        states.current("owner", bot)
    with pytest.raises(Conflict, match="finalizing"):
        states.current("owner", bot, task_id="another-task")
    assert states.current("owner", bot, task_id=task) == b"cookies at the pause"
    states.stage("owner", bot, task, b"cookies after approval")
    assert states.current("owner", bot, task_id=task) == b"cookies after approval"
    store.finish("owner", task, "completed", publish_reply=False)
    assert states.current("owner", bot) == b"cookies after approval"
