"""Computer tool admission/ownership fixtures; Linux execution has separate QA."""

import base64
import io
import threading
from uuid import uuid4

import pytest
from PIL import Image

from alysis_code.bots.browser import BrowserImageResult
from alysis_code.bots.computer import ComputerViews
from alysis_code.bots.desktop import DesktopComputers
from alysis_code.bots.desktop_linux import DesktopError, DesktopStartupCleanupError
from alysis_code.cancellation import EventCancellationToken


class Executor:
    def __init__(self, owner, workspace, *, cancel):
        self.owner, self.workspace = owner, workspace
        self.closed = False
        self.calls = []
        self.fail_close = False
        image = io.BytesIO()
        Image.new("RGB", (2, 2), "blue").save(image, "PNG")
        self.png = image.getvalue()

    def alive(self):
        return not self.closed

    def capture(self):
        return self.png

    def close(self):
        if self.fail_close:
            raise DesktopError("Fixture cleanup failure")
        self.closed = True

    def call(self, owner, operation, args, *, cancel):
        assert owner == self.owner
        self.calls.append((operation, args))
        observation = {
            "frame_id": uuid4().hex,
            "width": 2,
            "height": 2,
            "png": base64.b64encode(self.png).decode(),
        }
        if operation == "observe":
            return observation
        if operation in {"click", "type", "key", "scroll"}:
            return {"dispatched": True, "verification_required": True, "observation": observation}
        return {"exit_code": 0, "output": "actual fixture result"}


@pytest.fixture
def lab():
    views = ComputerViews()
    instances = []

    def factory(*args, **kwargs):
        executor = Executor(*args, **kwargs)
        instances.append(executor)
        return executor

    manager = DesktopComputers("owner", views, factory=factory)
    yield manager, views, instances
    for executor in instances:
        executor.fail_close = False
    manager.close()


def task(id="task", bot="bot"):
    return {"id": id, "bot_id": bot}


def token():
    return EventCancellationToken(threading.Event())


def test_definite_creation_refusal_has_structured_receipt_and_no_claim(lab):
    from alysis_code.bots.compute import ComputeCreationRefused

    manager, views, instances = lab

    def refuse(*_, **__):
        raise ComputeCreationRefused("Ανεπαρκής χώρος 東京", code="insufficient_local_storage")

    manager.factory = refuse
    result = manager.call(task(), "computer_run", {"command": "true"}, token(), approved=True)
    assert result == {
        "error": "Ανεπαρκής χώρος 東京",
        "code": "insufficient_local_storage",
        "computer_created": False,
        "retryable": False,
    }
    assert instances == []
    assert views.current("owner", "bot")[1] is None
    manager.release("task", cancelled=False)


@pytest.mark.parametrize("first_tool", ["computer_run", "computer_observe"])
def test_replaced_computer_is_announced_and_old_frames_remain_invalid(lab, first_tool):
    manager, views, instances = lab

    def call(name, args=None):
        result = manager.call(task(), name, args or {}, token(), approved=True, image_input=True)
        return result.observation if isinstance(result, BrowserImageResult) else result

    first = call(first_tool, {"command": "true"} if first_tool == "computer_run" else {})
    assert first["computer_created"] is True
    assert first["applications_retained_after_task"] is False
    assert "Task completion or Stop closes all applications" in first["lifetime"]
    before = call("computer_observe")
    assert before["computer_created"] is False
    assert first["computer_session_id"] == before["computer_session_id"]
    assert before["computer_session_id"] == views.frame("owner", "bot")["session_id"]
    old_workspace = instances[0].workspace
    instances[0].closed = True  # Fixture process loss; actual Linux QA is separate.
    after = call("computer_observe")
    assert after["computer_created"] is True
    assert after["computer_session_id"] != before["computer_session_id"]
    assert not old_workspace.exists()
    with pytest.raises(DesktopError, match="expired"):
        call("computer_click", {"source_id": before["source_id"], "x": 0, "y": 0})
    clicked = call("computer_click", {"source_id": after["source_id"], "x": 0, "y": 0})
    assert clicked["computer_created"] is False
    assert clicked["computer_session_id"] == after["computer_session_id"]
    command = call("computer_run", {"command": "true"})
    assert command["computer_session_id"] == after["computer_session_id"]
    assert command["computer_created"] is False


def test_permission_and_vision_are_checked_before_allocation(lab):
    manager, _, instances = lab
    for approved in (False, 1):
        assert manager.schemas(task(), True, approved=approved, image_input=True) == []
        with pytest.raises(DesktopError, match="approval"):
            manager.call(task(), "computer_run", {"command": "true"}, token(), approved=approved)
    names = {
        s["function"]["name"]
        for s in manager.schemas(task(), True, approved=True, image_input=False)
    }
    assert names == {"computer_run", "computer_launch", "computer_process", "computer_stop_process"}
    with pytest.raises(DesktopError, match="unavailable"):
        manager.call(task(), "computer_observe", {}, token(), approved=True)
    assert instances == []


def test_frames_and_viewer_are_bound_to_bot_and_task(lab):
    manager, views, instances = lab
    result = manager.call(task(), "computer_observe", {}, token(), approved=True, image_input=True)
    assert isinstance(result, BrowserImageResult) and "png" not in result.observation
    assert views.frame("owner", "bot")["state"] == "live"
    assert views.frame("owner", "other")["state"] == "not_running"
    with pytest.raises(DesktopError, match="busy"):
        manager.call(
            task("other"), "computer_observe", {}, token(), approved=True, image_input=True
        )
    source = result.observation["source_id"]
    result = manager.call(
        task(),
        "computer_click",
        {"source_id": source, "x": 1, "y": 1},
        token(),
        approved=True,
        image_input=True,
    )
    assert result.observation["dispatched"]
    with pytest.raises(DesktopError, match="expired"):
        manager.call(
            task(),
            "computer_click",
            {"source_id": source, "x": 1, "y": 1},
            token(),
            approved=True,
            image_input=True,
        )
    source = result.observation["source_id"]
    manager.release("task", cancelled=False)
    with pytest.raises(DesktopError, match="first"):
        manager.call(
            task("next"),
            "computer_click",
            {"source_id": source, "x": 1, "y": 1},
            token(),
            approved=True,
            image_input=True,
        )
    manager.release("next", cancelled=True)
    assert instances[0].closed and not instances[0].workspace.exists()
    assert views.frame("owner", "bot")["state"] == "not_running"


def test_failed_release_retains_handles_and_reaper_retries(lab):
    manager, views, instances = lab
    manager.call(task(), "computer_run", {"command": "true"}, token(), approved=True)
    instances[0].fail_close = True
    with pytest.raises(DesktopError):
        manager.release("task", cancelled=False)
    assert manager._computers["bot"].retired and instances[0].workspace.exists()
    assert views.frame("owner", "bot")["state"] == "not_running"
    instances[0].fail_close = False
    manager.reap()
    assert not manager._computers and not instances[0].workspace.exists()


def test_startup_cleanup_failure_retains_executor_and_workspace_for_retry(lab):
    manager, _, instances = lab

    def fail(*args, **kwargs):
        executor = Executor(*args, **kwargs)
        executor.fail_close = True
        instances.append(executor)
        raise DesktopStartupCleanupError(executor)

    manager.factory = fail
    with pytest.raises(DesktopError, match="pending"):
        manager.call(task(), "computer_run", {"command": "true"}, token(), approved=True)
    assert manager._computers["bot"].retired and instances[0].workspace.exists()
    instances[0].fail_close = False
    manager.reap()
    assert instances[0].closed and not instances[0].workspace.exists()


def test_busy_capture_is_connecting_not_a_false_disconnection(lab):
    manager, views, instances = lab
    manager.call(task(), "computer_run", {"command": "true"}, token(), approved=True)

    def busy():
        raise BlockingIOError("busy")

    binding = manager._computers["bot"].generation
    views.revoke("owner", "bot", binding)
    manager._computers["bot"].generation = views.bind(
        "owner", "bot", capture=busy, alive=lambda: True
    )
    assert views.frame("owner", "bot") == {"state": "connecting"}


def test_pending_task_cancellation_reaps_launched_apps_without_another_tool(lab):
    manager, views, instances = lab
    stopped = threading.Event()
    manager.call(
        task(),
        "computer_launch",
        {"command": "fixture"},
        EventCancellationToken(stopped),
        approved=True,
    )
    stopped.set()
    assert manager.reap() is True
    assert instances[0].closed and not instances[0].workspace.exists()
    assert views.frame("owner", "bot")["state"] == "not_running"


def test_successful_release_also_stops_applications_and_removes_scratch(lab):
    manager, _, instances = lab
    stopped = threading.Event()
    manager.call(
        task(),
        "computer_run",
        {"command": "fixture"},
        EventCancellationToken(stopped),
        approved=True,
    )
    manager.release("task", cancelled=False)
    stopped.set()  # completed durable task tokens are terminal too
    assert manager.reap() is True
    assert instances[0].closed and not instances[0].workspace.exists()


def test_slow_background_computer_does_not_block_chat_or_other_tool_admission(lab, tmp_path):
    from alysis_code.bots.models import BotInput, TaskInput
    from alysis_code.bots.runtime import BotRuntime
    from alysis_code.bots.store import Store
    from alysis_code.llm.types import LLMResponse, LLMUsage

    manager, _, instances = lab
    manager.call(task(), "computer_run", {"command": "fixture"}, token(), approved=True)
    entered, finish = threading.Event(), threading.Event()
    errors = []
    original = instances[0].call

    def slow(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return original(*args, **kwargs)

    def work():
        try:
            manager.call(task(), "computer_run", {"command": "slow"}, token(), approved=True)
        except BaseException as exc:
            errors.append(exc)

    instances[0].call = slow
    worker = threading.Thread(target=work)
    worker.start()
    runtime = None
    try:
        assert entered.wait(2)
        assert manager.reap() is False
        result = manager.call(
            task("other", "other"), "computer_run", {"command": "true"}, token(), approved=True
        )
        assert result["exit_code"] == 0
        store = Store(tmp_path / "chat.sqlite3")
        bot = store.save_bot("owner", BotInput(name="Chat", purpose="Keep replying"))
        chat = store.create_chat("owner", bot["id"], "Conversation")
        answered = threading.Event()

        class Client:
            supports_tool_calling = True
            model = "fixture"

            def chat(self, **kwargs):
                answered.set()
                return LLMResponse("Still here", [], {}, usage=LLMUsage(10, 2, 12))

        runtime = BotRuntime(store, "owner", lambda _: Client(), desktop_computers=manager)
        runtime.start()
        store.enqueue("owner", chat["id"], TaskInput(message="Hello", request_id="chat-request"))
        runtime.wake()
        assert answered.wait(2), "Foreground housekeeping blocked chat on computer I/O"
        assert not finish.is_set()
    finally:
        finish.set()
        worker.join(3)
        if runtime:
            runtime.stop()
    assert not worker.is_alive() and not errors


def test_selective_stop_requires_existing_owned_authorized_computer(lab):
    manager, _, instances = lab
    args = {"process_id": "owned-job"}
    with pytest.raises(DesktopError, match="approval"):
        manager.call(task(), "computer_stop_process", args, token())
    with pytest.raises(DesktopError, match="first"):
        manager.call(task(), "computer_stop_process", args, token(), approved=True)
    assert not instances
    frame = manager.call(task(), "computer_observe", {}, token(), approved=True, image_input=True)
    with pytest.raises(DesktopError, match="busy"):
        manager.call(task("other"), "computer_stop_process", args, token(), approved=True)
    manager.call(task(), "computer_stop_process", args, token(), approved=True)
    assert instances[0].calls[-1] == ("stop_process", args)
    with pytest.raises(DesktopError, match="expired"):
        manager.call(
            task(),
            "computer_click",
            {"source_id": frame.observation["source_id"], "x": 0, "y": 0},
            token(),
            approved=True,
            image_input=True,
        )
