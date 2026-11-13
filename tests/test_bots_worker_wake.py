"""Real runtime handoffs must notify sleeping workers, not wait for polling."""

import threading
from uuid import uuid4

import pytest

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.llm.types import LLMResponse, ToolCall


class SignalOnlyWait(threading.Event):
    def __init__(self):
        super().__init__()
        self.waiting = threading.Event()

    def wait(self, timeout=None):
        self.waiting.set()
        # Deliberately suppress the fallback poll. A real notification must
        # dispatch this test's handoff; stop also releases this bounded wait.
        return super().wait(20)


@pytest.mark.parametrize("lane", ["background", "peer"])
def test_handoff_notifies_worker_before_its_next_poll(tmp_path, lane):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Source", purpose="Fixture work"))
    peer = store.save_bot("owner", BotInput(name="Reviewer", purpose="Fixture review"))
    chat = store.create_chat("owner", bot["id"], "Continuous")
    calls = []
    handed_off = threading.Event()

    class Client:
        model = "explicit-wakeup-fixture"
        supports_tool_calling = True

        def chat(self, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                if lane == "background":
                    tool, args = "continue_in_background", {"summary": "Fixture work"}
                else:
                    tool, args = (
                        "request_peer",
                        {
                            "recipient_bot_id": peer["id"],
                            "brief": "Requested fixture review",
                        },
                    )
                return LLMResponse("", [ToolCall("handoff", tool, args)], {})
            handed_off.set()
            return LLMResponse("Explicit fixture response.", [], {})

    runtime = BotRuntime(store, "owner", lambda _: Client())
    sleeper = SignalOnlyWait()
    setattr(runtime, f"_{lane}_wake", sleeper)
    runtime.start()
    try:
        assert sleeper.waiting.wait(5)
        store.enqueue(
            "owner", chat["id"], TaskInput(message="Please do this work", request_id=uuid4().hex)
        )
        # Wake only foreground here to isolate notification by the actual
        # handoff, rather than the public wake() convenience method.
        runtime._wake.set()
        assert handed_off.wait(5), "Handoff depended on the disabled fallback poll."
    finally:
        runtime.stop()
    assert not runtime.is_running
