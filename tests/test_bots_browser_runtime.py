"""Runtime admission/lifecycle tests; no browser or model provider is started."""

from __future__ import annotations

import copy
import json
import threading
import time

import pytest

from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.runtime import BotRuntime
from alysis_code.bots.store import Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

BROWSER_NAMES = ("browser_navigate", "browser_observe", "browser_click", "browser_type")


class BrowserFixture:
    """Duck-typed executor boundary: only call() represents computer allocation."""

    def __init__(self, *, advertised=BROWSER_NAMES):
        self.advertised = advertised
        self.eligibility = []
        self.calls = []
        self.releases = []
        self.lifecycle = []
        self.released = threading.Event()
        self.on_close = lambda: None

    def schemas(self, task, eligible, *, approved_origins=(), image_input=False):
        self.eligibility.append((task["id"], eligible))
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": "Test executor operation.",
                    "parameters": {"type": "object", "properties": {}},
                },
            }
            for name in self.advertised
            if eligible
        ]

    def call(
        self, task, name, args, token, *, approved_origins=(), image_input=False, review_action=None
    ):
        self.calls.append((task, name, args, token))
        return {"title": "Synthetic page", "operation": name}

    def release(self, task_id, *, cancelled):
        self.releases.append((task_id, cancelled))
        self.lifecycle.append("release")
        self.released.set()

    def close(self):
        self.lifecycle.append("close")
        self.on_close()

    def reap(self):
        pass


class ClientFixture:
    model = "browser-runtime-fixture"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(
            {
                "messages": copy.deepcopy(kwargs["messages"]),
                "tools": copy.deepcopy(kwargs["tools"]),
                "token": kwargs["cancellation_token"],
            }
        )
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        return result


def response(content="Finished", *, name=None, arguments=None):
    calls = [ToolCall("browser-call", name, arguments or {})] if name else []
    return LLMResponse(content, calls, {}, usage=LLMUsage(10, 5, 15))


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "bots.sqlite3")


def enqueue(store):
    bot = store.save_bot("owner", BotInput(name="Research", purpose="Read requested pages"))
    chat = store.create_chat("owner", bot["id"], "Conversation")
    return store.enqueue(
        "owner", chat["id"], TaskInput(message="Read the page", request_id="browser-request")
    )


def runtime_for(store, client, browser):
    return BotRuntime(store, "owner", lambda _: client, browser_computers=browser)


def advertised_names(request):
    return {schema["function"]["name"] for schema in request["tools"] or []}


@pytest.mark.parametrize("name", BROWSER_NAMES)
def test_ordinary_turn_dispatches_advertised_browser_tools_with_task_token(store, name):
    enqueue(store)
    task = store.claim("owner")
    arguments = {"target": "synthetic-reference", "text": "Ελληνικά 日本語"}
    client = ClientFixture([response(name=name, arguments=arguments), response()])
    browser = BrowserFixture()
    runtime = runtime_for(store, client, browser)
    token = runtime.task_token(task["id"])

    runtime.run(task, token)

    assert store.task("owner", task["id"])["status"] == "completed"
    assert set(BROWSER_NAMES) <= advertised_names(client.requests[0])
    assert browser.eligibility == [(task["id"], True)]
    assert browser.calls == [(task, name, arguments, token)]
    assert all(request["token"] is token for request in client.requests)
    result = json.loads(
        next(m for m in reversed(client.requests[-1]["messages"]) if m["role"] == "tool")["content"]
    )
    assert result == {"title": "Synthetic page", "operation": name}
    # A completed durable task makes JobCancellationToken.is_cancelled true.
    # That must not misclassify a successful release as cancellation.
    assert browser.releases == [(task["id"], False)]


def test_plain_chat_does_not_call_or_allocate_a_browser(store):
    enqueue(store)
    task = store.claim("owner")
    browser = BrowserFixture()
    runtime = runtime_for(store, ClientFixture([response("Hello")]), browser)

    runtime.run(task, runtime.task_token(task["id"]))

    assert store.task("owner", task["id"])["status"] == "completed"
    assert browser.calls == []
    assert browser.releases == []


def test_idle_computer_cleanup_failure_does_not_kill_chat_worker(store):
    browser = BrowserFixture()
    pending, recovered = threading.Event(), threading.Event()
    skip, skipped = threading.Event(), threading.Event()

    def reap():
        if skip.is_set():
            skipped.set()
            return False
        if not recovered.is_set():
            pending.set()
            raise RuntimeError("Synthetic uncertain profile cleanup")

    browser.reap = reap
    runtime = runtime_for(store, ClientFixture([response("Still available")]), browser)
    runtime.start()
    try:
        assert pending.wait(3)
        request = enqueue(store)
        runtime.wake()
        deadline = time.monotonic() + 3
        while store.task("owner", request["id"])["status"] != "completed":
            assert time.monotonic() < deadline, "Chat worker stopped on cleanup failure"
            time.sleep(0.01)
        assert runtime.is_running
        assert runtime.browser_cleanup_pending
        skip.set()
        runtime.wake()
        assert skipped.wait(3)
        assert runtime.browser_cleanup_pending
        skip.clear()
        recovered.set()
        runtime.wake()
        deadline = time.monotonic() + 3
        while runtime.browser_cleanup_pending:
            assert time.monotonic() < deadline
            time.sleep(0.01)
    finally:
        runtime.stop()


@pytest.mark.parametrize("phase", ["activation", "schedule", "continuation"])
def test_restricted_phases_neither_advertise_nor_dispatch_browser(store, monkeypatch, phase):
    enqueue(store)
    task = store.claim("owner")
    browser = BrowserFixture()
    client = ClientFixture([response(name="browser_navigate"), response()])
    runtime = runtime_for(store, client, browser)
    if phase == "activation":
        task["config"]["activation"] = {"event": "file_added"}
    elif phase == "schedule":
        monkeypatch.setattr(store, "schedule_context", lambda *_: {"request": "Read the page"})
    elif phase == "continuation":
        continuation = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {"id": "coding-result", "type": "function", "function": {"name": "coding_task"}}
            ],
        }
        monkeypatch.setattr(store, "continuation", lambda *_: continuation)
        monkeypatch.setattr(
            runtime.coding,
            "for_task",
            lambda *_: {
                "status": "completed",
                "result": {"outcome": {"outcome": "verified_success", "verified_success": True}},
            },
        )
    runtime.run(task, runtime.task_token(task["id"]))

    # Verify a provider request actually occurred, rather than a setup error
    # accidentally making the no-dispatch assertions pass.
    assert client.requests
    assert all(not (set(BROWSER_NAMES) & advertised_names(r)) for r in client.requests)
    assert all(not eligible for _, eligible in browser.eligibility)
    assert browser.calls == []
    result = store.task("owner", task["id"])
    if phase == "continuation":
        assert result["status"] == "failed"  # Reporting-only turn rejects extra work.
    else:
        assert result["status"] == "completed"
        assert any(event["kind"] == "tool_failed" for event in result["events"])
    assert browser.releases == []


def test_browser_dispatch_is_limited_to_currently_advertised_names(store):
    enqueue(store)
    task = store.claim("owner")
    browser = BrowserFixture(advertised=("browser_observe",))
    client = ClientFixture([response(name="browser_type"), response()])
    runtime = runtime_for(store, client, browser)

    runtime.run(task, runtime.task_token(task["id"]))

    assert "browser_observe" in advertised_names(client.requests[0])
    assert "browser_type" not in advertised_names(client.requests[0])
    assert browser.calls == []
    assert "error" in json.loads(
        next(m for m in reversed(client.requests[-1]["messages"]) if m["role"] == "tool")["content"]
    )
    assert browser.releases == []


def test_browser_claim_is_released_when_later_model_call_fails(store):
    enqueue(store)
    task = store.claim("owner")
    browser = BrowserFixture()
    client = ClientFixture([response(name="browser_observe"), RuntimeError("fixture failure")])
    runtime = runtime_for(store, client, browser)

    runtime.run(task, runtime.task_token(task["id"]))

    assert len(browser.calls) == 1
    assert store.task("owner", task["id"])["status"] == "failed"
    # An unexpected runtime failure retires the computer rather than retaining
    # a browser whose last task could not finish cleanly.
    assert browser.releases == [(task["id"], True)]


def test_foreground_cancellation_before_dispatch_does_not_release_unowned_computer(store):
    enqueue(store)
    task = store.claim("owner")
    browser = BrowserFixture()
    client = ClientFixture([])
    runtime = runtime_for(store, client, browser)
    cancelled = threading.Event()
    cancelled.set()

    runtime.run(task, EventCancellationToken(cancelled))

    assert client.requests == []
    assert browser.calls == []
    assert store.task("owner", task["id"])["status"] == "cancelled"
    assert browser.releases == []


@pytest.mark.parametrize("stop_runtime", [False, True], ids=["cancel-task", "stop-runtime"])
def test_inflight_browser_observes_cancellation_and_releases_before_close(store, stop_runtime):
    class BlockingBrowser(BrowserFixture):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.exited = threading.Event()

        def call(
            self,
            task,
            name,
            args,
            token,
            *,
            approved_origins=(),
            image_input=False,
            review_action=None,
        ):
            super().call(task, name, args, token, approved_origins=approved_origins)
            self.entered.set()
            try:
                token.wait(5)
                token.throw_if_cancelled()
                raise AssertionError("The browser operation did not receive cancellation")
            finally:
                self.exited.set()

    task = enqueue(store)
    browser = BlockingBrowser()
    client = ClientFixture([response(name="browser_navigate")])
    runtime = runtime_for(store, client, browser)
    workers_alive_at_close = []
    browser.on_close = lambda: workers_alive_at_close.append(
        any(
            worker is not None and worker.is_alive()
            for worker in (runtime._thread, runtime._background_thread, runtime._scheduler)
        )
    )
    runtime.start()
    stopped = False
    try:
        assert browser.entered.wait(5)
        assert browser.calls[0][3] is client.requests[0]["token"]
        if stop_runtime:
            runtime.stop()
            stopped = True
        else:
            runtime.cancel(task["id"])
        assert browser.released.wait(5)
        assert browser.exited.is_set()
        assert browser.releases == [(task["id"], True)]
        result = store.task("owner", task["id"])
        assert result["status"] == ("interrupted" if stop_runtime else "cancelled")
        assert result["output"] == ""
        assert not any(event["kind"] == "tool_completed" for event in result["events"])
    finally:
        if not stopped:
            runtime.stop()
    assert browser.lifecycle == ["release", "close"]
    assert workers_alive_at_close == [False]


def test_runtime_delivers_browser_image_after_tool_results_and_accounts_usage(store):
    from test_bots_browser_images import png_url

    from alysis_code.bots.browser import BrowserImageResult

    class ImageBrowser(BrowserFixture):
        def __init__(self):
            super().__init__(advertised=("browser_screenshot",))

        def schemas(self, task, eligible, *, approved_origins=(), image_input=False):
            assert image_input is True
            return super().schemas(
                task, eligible, approved_origins=approved_origins, image_input=image_input
            )

        def call(
            self,
            task,
            name,
            args,
            token,
            *,
            approved_origins=(),
            image_input=False,
            review_action=None,
        ):
            assert image_input is True
            return BrowserImageResult(
                {"source_id": "frame", "frame": {"width": 64, "height": 64}}, png_url()
            )

    enqueue(store)
    task = store.claim("owner")
    client = ClientFixture(
        [response(name="browser_screenshot"), response("Observed actual pixels")]
    )
    client._alysis_browser_image_input = True
    runtime = runtime_for(store, client, ImageBrowser())
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "completed"
    messages = client.requests[-1]["messages"]
    frame_index = next(i for i, m in enumerate(messages) if isinstance(m.get("content"), list))
    receipt, frame = messages[frame_index - 1 : frame_index + 1]
    assert receipt["role"] == "tool" and "image_url" not in receipt["content"]
    assert frame["role"] == "user"
    assert frame["content"][1]["image_url"]["url"] == png_url()
    assert store.usage("owner")["calls"] == 2


def test_nonvisual_browser_refresh_retires_prior_image(store):
    from test_bots_browser_images import png_url

    from alysis_code.bots.browser import BrowserImageResult

    class ImageBrowser(BrowserFixture):
        def __init__(self):
            super().__init__(advertised=("browser_screenshot", "browser_navigate"))

        def call(
            self,
            task,
            name,
            args,
            token,
            *,
            approved_origins=(),
            image_input=False,
            review_action=None,
        ):
            if name == "browser_screenshot":
                return BrowserImageResult(
                    {"source_id": "frame", "frame": {"width": 64, "height": 64}}, png_url()
                )
            return {"source_id": "fresh", "dom": "different page"}

    enqueue(store)
    task = store.claim("owner")
    client = ClientFixture(
        [response(name="browser_screenshot"), response(name="browser_navigate"), response("Done")]
    )
    client._alysis_browser_image_input = True
    runtime = runtime_for(store, client, ImageBrowser())
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "completed"

    def count(request):
        return sum(
            1
            for m in request["messages"]
            if isinstance(m.get("content"), list)
            for p in m["content"]
            if p.get("type") == "image_url"
        )

    assert [count(r) for r in client.requests] == [0, 1, 0]


def test_background_checkpoint_requires_fresh_pixels_without_persisting_image_blocks(store):
    from test_bots_browser_images import png_url

    from alysis_code.bots.browser import BrowserImageResult

    class ImageBrowser(BrowserFixture):
        def __init__(self):
            super().__init__(advertised=("browser_screenshot",))

        def call(
            self,
            task,
            name,
            args,
            token,
            *,
            approved_origins=(),
            image_input=False,
            review_action=None,
        ):
            return BrowserImageResult(
                {"source_id": "frame", "frame": {"width": 64, "height": 64}}, png_url()
            )

    enqueue(store)
    task = store.claim("owner")
    client = ClientFixture(
        [
            response(name="browser_screenshot"),
            response(
                name="continue_in_background", arguments={"summary": "Continue the visual task"}
            ),
        ]
    )
    client._alysis_browser_image_input = True
    browser = ImageBrowser()
    runtime = runtime_for(store, client, browser)
    runtime.run(task, runtime.task_token(task["id"]))
    assert store.task("owner", task["id"])["status"] == "waiting"
    assert runtime.background.claim()["id"] == task["id"]
    checkpoint = runtime.background.checkpoint(task["id"])
    assert "data:image/" not in json.dumps(checkpoint)
    assert "request a current frame" in json.dumps(checkpoint)
    assert browser.releases == []


@pytest.mark.parametrize(
    "failure,seen",
    [
        ("refusal", "This website is outside the task's approval. Request approval first."),
        ("raw", "Invalid tool arguments."),
    ],
)
def test_the_model_reads_a_browser_refusal_but_never_a_raw_value_error(store, failure, seen):
    from alysis_code.bots.browser import BrowserRefusal

    class Refusing(BrowserFixture):
        def call(self, task, name, args, token, **kwargs):
            if failure == "refusal":
                raise BrowserRefusal(
                    "This website is outside the task's approval. Request approval first."
                )
            raise ValueError("invalid literal for int() with base 10: 'secret-123'")

    enqueue(store)
    task = store.claim("owner")
    client = ClientFixture([response(name="browser_click", arguments={}), response()])
    runtime = runtime_for(store, client, Refusing())
    runtime.run(task, runtime.task_token(task["id"]))
    result = json.loads(
        next(m for m in reversed(client.requests[-1]["messages"]) if m["role"] == "tool")["content"]
    )
    assert result["error"] == seen
    failed = [e for e in store.task("owner", task["id"])["events"] if e["kind"] == "tool_failed"]
    assert failed and "secret-123" not in json.dumps(failed)
