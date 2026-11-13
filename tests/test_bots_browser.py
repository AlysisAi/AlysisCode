"""Adapter boundaries with real DOM decoding and fake, provider-free executors."""

from __future__ import annotations

import copy
import io
import json
import threading
from types import SimpleNamespace

import pytest
from PIL import Image

from alysis_code.bots import browser_linux
from alysis_code.bots.browser import BrowserComputers, BrowserStartupCleanupError
from alysis_code.bots.browser_linux import LinuxBrowserComputer
from alysis_code.bots.computer import ComputerViews
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.ide.managed_browser import BrowserCancelledError, BrowserError

PREVIEW = "http://127.0.0.1:8123/"
PUBLIC = "https://example.org/"


def dom(url=PREVIEW, text="  Ελληνικά 日本語\n"):
    return {
        "strings": [
            url,
            "Synthetic page",
            "#document",
            "HTML",
            "HEAD",
            "BODY",
            "INPUT",
            "BUTTON",
            "#text",
            "aria-label",
            "入力",
            "Hidden input",
            "Visible input",
            text,
        ],
        "documents": [
            {
                "documentURL": 0,
                "title": 1,
                "nodes": {
                    "parentIndex": [-1, 0, 1, 1, 3, 3, 3, 6],
                    "nodeType": [9, 1, 1, 1, 1, 1, 1, 3],
                    "nodeName": [2, 3, 4, 5, 6, 6, 7, 8],
                    "nodeValue": [-1, -1, -1, -1, -1, -1, -1, 13],
                    "backendNodeId": list(range(101, 109)),
                    "attributes": [[], [], [], [], [], [9, 10], [], []],
                    "inputValue": {"index": [4, 5], "value": [11, 12]},
                },
                "layout": {"nodeIndex": [1, 3, 5, 6, 7]},
            }
        ],
    }


def task(bot="bot-a", identifier="task-a"):
    return {"id": identifier, "bot_id": bot}


def token():
    return EventCancellationToken(threading.Event())


class BrowserFixture:
    def __init__(self):
        self.data = dom()
        self.generation = 17
        self.calls = []
        self.failure = None
        self.truncated = False

    def _call(self, name, owner, session_id, *args, **kwargs):
        self.calls.append((name, owner, session_id, args, kwargs))
        if kwargs["cancel"]():
            raise BrowserCancelledError("Synthetic cancellation")
        if self.failure:
            raise self.failure

    def snapshot(self, owner, session_id, **kwargs):
        self._call("snapshot", owner, session_id, **kwargs)
        return {
            "data": copy.deepcopy(self.data),
            "command_generation": self.generation,
            "truncated": self.truncated,
        }

    def pages(self, owner, session_id, **kwargs):
        self._call("pages", owner, session_id, **kwargs)
        return {
            "pages": [dict(page_id="root", url=self.data["strings"][0], active=True)],
            "command_generation": self.generation,
        }

    def status(self, owner, session_id):
        return SimpleNamespace(command_generation=self.generation)

    def navigate(self, owner, session_id, url, **kwargs):
        self._call("navigate", owner, session_id, url, **kwargs)
        self.data["strings"][0] = url

    def click(self, owner, session_id, selector, **kwargs):
        self._call("click", owner, session_id, selector, **kwargs)

    def type_text(self, owner, session_id, selector, text, **kwargs):
        self._call("type", owner, session_id, selector, text, **kwargs)
        self.data["strings"][12] = text


class ExecutorFixture:
    def __init__(self, bot_id, views):
        self.owner = "executor-" + bot_id
        self.session_id = "session-" + bot_id
        self.browser = BrowserFixture()
        self.previews = [PREVIEW]
        self.preview_urls = lambda: tuple(self.previews)
        self.closed = False
        self.close_calls = 0
        self.close_failures = 0
        self.view_at_close = []
        self.views, self.bot_id = views, bot_id
        pixels = io.BytesIO()
        Image.new("RGB", (2, 2), color="blue").save(pixels, format="PNG")
        self.capture = lambda: pixels.getvalue()

    def alive(self):
        return not self.closed

    def close(self):
        self.close_calls += 1
        self.view_at_close.append(self.views.frame("owner", self.bot_id)["state"])
        if self.close_failures:
            self.close_failures -= 1
            raise RuntimeError("Synthetic cleanup failure")
        self.closed = True


class Harness:
    def __init__(self, *, views=None, max_computers=2):
        self.views = views or ComputerViews()
        self.now = 0
        self.executors = []
        self.factory_hook = lambda executor, cancel: None
        self.adapter = BrowserComputers(
            "owner",
            self.views,
            self.factory,
            idle_seconds=10,
            max_computers=max_computers,
            clock=lambda: self.now,
            interactive_previews=True,
        )

    def factory(self, bot_id, cancel):
        executor = ExecutorFixture(bot_id, self.views)
        self.executors.append(executor)
        self.factory_hook(executor, cancel)
        return executor

    def call(self, name="browser_observe", args=None, *, request=None, cancellation=None):
        return self.adapter.call(request or task(), name, args or {}, cancellation or token())


@pytest.fixture
def lab():
    fixture = Harness()
    yield fixture
    fixture.adapter.close()


def test_schema_discovery_and_invalid_arguments_never_allocate(lab):
    schemas = lab.adapter.schemas(task(), eligible=True)
    assert {schema["function"]["name"] for schema in schemas} == {
        "browser_navigate",
        "browser_pages",
        "browser_switch_page",
        "browser_close_page",
        "browser_observe",
        "browser_click",
        "browser_type",
        "browser_source",
        "browser_select",
        "browser_key",
        "browser_scroll",
    }
    assert lab.adapter.schemas(task(), eligible=False) == []
    with pytest.raises(ValueError):
        lab.call("browser_observe", {"session_id": "someone-elses-session"})
    with pytest.raises(ValueError):
        lab.call("browser_source", {"source_id": "unobserved"})
    assert lab.executors == []
    assert lab.views.frame("owner", "bot-a")["state"] == "not_running"


def test_definite_creation_refusal_has_structured_receipt_and_no_claim(lab):
    from alysis_code.bots.compute import ComputeCreationRefused

    def refuse(*_):
        raise ComputeCreationRefused("Ανεπαρκής χώρος 東京", code="insufficient_local_storage")

    lab.adapter.factory = refuse
    result = lab.call()
    assert result == {
        "error": "Ανεπαρκής χώρος 東京",
        "code": "insufficient_local_storage",
        "computer_created": False,
        "retryable": False,
    }
    assert lab.executors == []
    assert lab.views.current("owner", "bot-a")[1] is None


def test_default_adapter_is_read_only_even_for_forged_preview_mutation(lab):
    # Omit the option entirely to verify the shipped default, rather than an
    # explicit false value supplied only by this fixture.
    lab.adapter = BrowserComputers("owner", lab.views, lab.factory)
    schemas = lab.adapter.schemas(task(), eligible=True)
    assert {schema["function"]["name"] for schema in schemas} == {
        "browser_navigate",
        "browser_pages",
        "browser_switch_page",
        "browser_close_page",
        "browser_observe",
        "browser_source",
    }
    for name in (
        "browser_click",
        "browser_type",
        "browser_select",
        "browser_key",
        "browser_scroll",
    ):
        arguments = {"source_id": "forged", "node": 5}
        if name == "browser_type":
            arguments["text"] = "Must not be typed"
        with pytest.raises(ValueError):
            lab.call(name, arguments)
    assert lab.executors == []
    observation = lab.call("browser_navigate", {"url": PREVIEW})
    for name in (
        "browser_click",
        "browser_type",
        "browser_select",
        "browser_key",
        "browser_scroll",
    ):
        arguments = {"source_id": observation["source_id"], "node": 5}
        if name == "browser_type":
            arguments["text"] = "Must not be typed"
        with pytest.raises(ValueError):
            lab.call(name, arguments)
    assert lab.call("browser_source", {"source_id": observation["source_id"]})["content"]
    assert not any(call[0] in {"click", "type"} for call in lab.executors[0].browser.calls)


def test_lazy_computer_uses_trusted_session_and_reuses_only_same_bot(lab):
    observation = lab.call("browser_navigate", {"url": PUBLIC})
    lab.call()
    assert len(lab.executors) == 1
    executor = lab.executors[0]
    assert observation["untrusted_page_data"] is True
    assert observation["dom"]["documents"][0]["url"] == PUBLIC
    assert all(
        call[1:3] == (executor.owner, executor.session_id) for call in executor.browser.calls
    )
    assert lab.views.frame("owner", "bot-a")["state"] == "live"
    assert lab.views.frame("other-owner", "bot-a")["state"] == "not_running"
    lab.call(request=task("bot-b", "task-b"))
    assert len(lab.executors) == 2
    assert lab.executors[0].session_id != lab.executors[1].session_id


@pytest.mark.parametrize("name", ["browser_click", "browser_type"])
def test_task_approved_site_actions_require_exact_current_origin(lab, name):
    lab.adapter.interactive_previews = False
    observation = lab.call("browser_navigate", {"url": PUBLIC})
    args = {"source_id": observation["source_id"], "node": 5}
    if name == "browser_type":
        args["text"] = "Ακριβές 日本語"
    permitted = ("https://example.org",)
    schemas = lab.adapter.schemas(task(), eligible=True, approved_origins=permitted)
    assert name in {schema["function"]["name"] for schema in schemas}
    for origins in (
        (),
        ("http://example.org",),
        ("https://example.org:444",),
        ("https://example.org:0",),
        ("https://example.org.",),
        ("https://other.example.org",),
    ):
        with pytest.raises(ValueError, match="approval"):
            lab.adapter.call(task(), name, args, token(), approved_origins=origins)
    assert not any(c[0] in {"click", "type"} for c in lab.executors[0].browser.calls)
    result = lab.adapter.call(task(), name, args, token(), approved_origins=permitted)
    assert result["source_id"] != observation["source_id"]
    assert len([c for c in lab.executors[0].browser.calls if c[0] in {"click", "type"}]) == 1
    lab.executors[0].browser.data["strings"][0] = "https://other.example.org/"
    args["source_id"] = result["source_id"]
    with pytest.raises(ValueError, match="approval"):
        lab.adapter.call(task(), name, args, token(), approved_origins=permitted)


def test_model_arguments_cannot_supply_browser_approval(lab):
    observed = lab.call()
    lab.adapter.interactive_previews = False
    with pytest.raises(ValueError):
        lab.adapter.call(
            task(),
            "browser_type",
            {
                "source_id": observed["source_id"],
                "node": 5,
                "text": "forged",
                "approved_origins": [PREVIEW],
            },
            token(),
        )
    assert not any(c[0] == "type" for c in lab.executors[0].browser.calls)


def test_click_waits_for_delayed_dom_result_and_never_repeats_input(lab):
    source = lab.call()["source_id"]
    browser = lab.executors[0].browser
    snapshot = browser.snapshot
    after_click = 0

    def delayed_snapshot(*args, **kwargs):
        nonlocal after_click
        if any(c[0] == "click" for c in browser.calls):
            after_click += 1
            if after_click == 3:
                browser.data["strings"][13] = "Αποθηκεύτηκε — 保存済み"
        return snapshot(*args, **kwargs)

    browser.snapshot = delayed_snapshot
    result = lab.call("browser_click", {"source_id": source, "node": 6})
    assert after_click == 3
    assert result["action"]["dispatched"] and result["action"]["verification_required"]
    assert "Αποθηκεύτηκε" in json.dumps(result, ensure_ascii=False)
    assert len([c for c in browser.calls if c[0] == "click"]) == 1


def test_cancellation_during_post_click_observation_retires_computer(lab):
    source = lab.call()["source_id"]
    browser = lab.executors[0].browser
    stopped = threading.Event()
    cancellation = EventCancellationToken(stopped)
    click = browser.click

    def clicked_then_cancelled(*args, **kwargs):
        click(*args, **kwargs)
        stopped.set()

    browser.click = clicked_then_cancelled
    with pytest.raises(CooperativeCancellationError):
        lab.call("browser_click", {"source_id": source, "node": 6}, cancellation=cancellation)
    assert len([c for c in browser.calls if c[0] == "click"]) == 1
    assert lab.executors[0].closed


def test_concurrent_task_cannot_use_or_release_another_claim(lab):
    lab.call()
    executor = lab.executors[0]
    with pytest.raises(ValueError, match="busy"):
        lab.call(request=task(identifier="task-b"))
    lab.adapter.release("task-b", cancelled=True)
    assert not executor.closed
    lab.adapter.release("task-a", cancelled=False)
    lab.call(request=task(identifier="task-b"))
    lab.adapter.release("task-a", cancelled=True)
    assert not executor.closed
    assert len(lab.executors) == 1


@pytest.mark.parametrize("name", ["browser_source", "browser_click", "browser_type"])
def test_source_identity_cannot_cross_bots(lab, name):
    source = lab.call()["source_id"]
    other = task("bot-b", "task-b")
    lab.call(request=other)
    args = {"source_id": source}
    if name != "browser_source":
        args["node"] = 5
    if name == "browser_type":
        args["text"] = "Must not cross bots"
    with pytest.raises(ValueError, match="expired"):
        lab.call(name, args, request=other)
    assert all(call[0] in {"snapshot", "pages"} for call in lab.executors[1].browser.calls)


def test_original_source_is_retained_without_refresh_and_paged_without_loss(lab):
    lab.factory_hook = lambda executor, cancel: setattr(
        executor.browser, "data", dom(text="Ω日本語" * 2500)
    )
    observation = lab.call()
    assert observation["dom"]["clipped"] is True
    assert observation["source_chars"] > 4000
    browser = lab.executors[0].browser
    original = copy.deepcopy(browser.data)
    calls_before_reading = len(browser.calls)
    browser.data["strings"][13] = "Changed after observation"
    chunks, offset = [], 0
    while offset is not None:
        result = lab.call(
            "browser_source", {"source_id": observation["source_id"], "offset": offset}
        )
        assert result["offset"] == offset
        assert 0 < len(result["content"]) <= 4000
        chunks.append(result["content"])
        offset = result["next_offset"]
    retained = json.loads("".join(chunks))
    assert retained["data"] == original
    assert retained["command_generation"] == 17
    assert len(browser.calls) == calls_before_reading
    with pytest.raises(ValueError, match="offset"):
        lab.call(
            "browser_source",
            {"source_id": observation["source_id"], "offset": observation["source_chars"]},
        )


def test_source_window_expires_oldest_and_keeps_four_latest(lab):
    sources = [lab.call()["source_id"] for _ in range(5)]
    with pytest.raises(ValueError, match="expired"):
        lab.call("browser_source", {"source_id": sources[0]})
    for source_id in sources[1:]:
        assert lab.call("browser_source", {"source_id": source_id})["source_id"] == source_id


@pytest.mark.parametrize("name", ["browser_click", "browser_type"])
def test_preview_actions_forward_original_backend_generation_and_cancellation(lab, name):
    cancellation = token()
    original = lab.call(cancellation=cancellation)
    args = {"source_id": original["source_id"], "node": 5}
    if name == "browser_type":
        args["text"] = "Δοκιμή 日本語"
    updated = lab.call(name, args, cancellation=cancellation)
    browser = lab.executors[0].browser
    action = next(call for call in browser.calls if call[0] in {"click", "type"})
    assert action[3][0] == ":root > :nth-child(2) > :nth-child(2)"
    assert action[4]["expected_backend_node_id"] == 106
    assert action[4]["expected_generation"] == 17
    assert action[4]["cancel"]() is False
    assert updated["source_id"] != original["source_id"]
    if name == "browser_type":
        assert action[3][1] == args["text"]
        node = next(node for node in updated["dom"]["documents"][0]["nodes"] if node["node"] == 5)
        assert node["value"] == args["text"]


@pytest.mark.parametrize(
    "original_url,current_url,previews",
    [
        (PUBLIC, PUBLIC, [PREVIEW]),
        (PUBLIC, PREVIEW, [PREVIEW]),
        (PREVIEW, PUBLIC, [PREVIEW]),
        (PREVIEW, PREVIEW, []),
    ],
)
@pytest.mark.parametrize("name", ["browser_click", "browser_type"])
def test_external_mutation_denied_for_original_and_current_document(
    lab, original_url, current_url, previews, name
):
    source = lab.call("browser_navigate", {"url": original_url})["source_id"]
    executor = lab.executors[0]
    executor.browser.data["strings"][0] = current_url
    executor.previews = previews
    args = {"source_id": source, "node": 5}
    if name == "browser_type":
        args["text"] = "Denied"
    with pytest.raises(ValueError, match="preview|approval"):
        lab.call(name, args)
    assert not any(call[0] in {"click", "type"} for call in executor.browser.calls)


@pytest.mark.parametrize("node", [0, 4, 7, 999])
def test_unobserved_or_non_element_target_cannot_dispatch(lab, node):
    source = lab.call()["source_id"]
    with pytest.raises(ValueError):
        lab.call("browser_click", {"source_id": source, "node": node})
    assert not any(call[0] == "click" for call in lab.executors[0].browser.calls)


def test_covered_target_consumes_reference_without_disposing_browser(lab):
    from alysis_code.bots.browser import BrowserObservationExpired
    from alysis_code.ide.managed_browser import BrowserTargetObscuredError

    observation = lab.call()
    executor = lab.executors[0]
    original_click = executor.browser.click

    def covered(*args, **kwargs):
        raise BrowserTargetObscuredError("Fixture overlay")

    executor.browser.click = covered
    args = {"source_id": observation["source_id"], "node": 6}
    with pytest.raises(BrowserObservationExpired, match="No click was sent"):
        lab.call("browser_click", args)
    assert not executor.closed
    executor.browser.click = original_click
    with pytest.raises(BrowserObservationExpired, match="stale or already used"):
        lab.call("browser_click", args)
    args["source_id"] = lab.call()["source_id"]
    lab.call("browser_click", args)
    assert len(lab.executors) == 1
    assert len([c for c in executor.browser.calls if c[0] == "click"]) == 1


@pytest.mark.parametrize("element", ["IFRAME", "FRAME", "OBJECT", "EMBED"])
@pytest.mark.parametrize("when", ["original", "current"])
def test_even_omitted_embedded_content_blocks_preview_interaction(lab, element, when):
    def embed(executor, cancel=None):
        data = executor.browser.data
        data["documents"][0]["nodes"]["nodeName"][4] = len(data["strings"])
        data["strings"].append(element)

    if when == "original":
        lab.factory_hook = embed
    observation = lab.call()
    assert 4 not in [node["node"] for node in observation["dom"]["documents"][0]["nodes"]]
    assert not any(node["name"] == element for node in observation["dom"]["documents"][0]["nodes"])
    if when == "current":
        embed(lab.executors[0])
    with pytest.raises(ValueError):
        lab.call("browser_click", {"source_id": observation["source_id"], "node": 6})
    assert not any(call[0] == "click" for call in lab.executors[0].browser.calls)


def test_typing_non_editable_observed_element_is_denied(lab):
    source = lab.call()["source_id"]
    with pytest.raises(ValueError):
        lab.call("browser_type", {"source_id": source, "node": 6, "text": "No"})
    assert not any(call[0] == "type" for call in lab.executors[0].browser.calls)


@pytest.mark.parametrize("value", ["TRUE", "TrUe", "PLAINTEXT-ONLY", ""])
def test_contenteditable_values_follow_html_case_insensitivity(lab, value):
    lab.call()
    raw = lab.executors[0].browser.data
    raw["strings"].extend(["DIV", "contenteditable", value])
    raw["documents"][0]["nodes"]["nodeName"][5] = len(raw["strings"]) - 3
    raw["documents"][0]["nodes"]["attributes"][5] += [
        len(raw["strings"]) - 2,
        len(raw["strings"]) - 1,
    ]
    view = lab.call()
    lab.call("browser_type", {"source_id": view["source_id"], "node": 5, "text": "日本語"})
    assert any(call[0] == "type" for call in lab.executors[0].browser.calls)


def large_page(executor, cancel):
    browser = executor.browser
    original = browser.snapshot
    browser.truncated = True
    browser.snapshot = lambda *a, **kw: (
        {
            "text": "日本語\n" * 2500 + "Transport\nActual source text",
            "truncated": False,
            "command_generation": browser.generation,
        }
        if kw.get("kind") == "text"
        else original(*a, **kw)
    )
    browser.pages = lambda *a, **kw: {
        "pages": [{"page_id": "root", "active": True, "url": PUBLIC}],
        "command_generation": browser.generation,
    }


def test_large_page_text_is_readable_but_never_an_action_reference(lab):
    lab.factory_hook = large_page
    observation = lab.call()
    assert observation["read_only_text"] and not observation["truncated"]
    assert len(observation["content"]) == 8000 and observation["next_offset"] == 8000
    source = observation["source_id"]
    part = lab.call("browser_source", {"source_id": source, "find": "Transport"})
    assert part["content"] == "Transport\nActual source text"
    assert part["offset"] == 10000
    assert not lab.call("browser_source", {"source_id": source, "find": "not present"})["found"]
    with pytest.raises(ValueError):
        lab.call("browser_click", {"source_id": source, "node": 6})
    assert not any(c[0] == "click" for c in lab.executors[0].browser.calls)


def test_large_page_navigation_race_does_not_misattribute_text(lab):
    def changed(executor, cancel):
        large_page(executor, cancel)
        executor.browser.pages = lambda *a, **kw: {
            "pages": [{"page_id": "root", "active": True, "url": PUBLIC}],
            "command_generation": 99,
        }

    lab.factory_hook = changed
    with pytest.raises(ValueError, match="Page changed"):
        lab.call()


def test_idle_reaping_only_closes_unclaimed_and_viewing_does_not_extend_lifetime(lab):
    lab.call()
    other = task("bot-b", "task-b")
    lab.call(request=other)
    lab.now = 100
    lab.adapter.reap()
    assert all(not executor.closed for executor in lab.executors)
    lab.adapter.release("task-a", cancelled=False)
    lab.now = 109
    lab.adapter.reap()
    assert lab.views.frame("owner", "bot-a")["state"] == "live"
    lab.now = 110
    lab.adapter.reap()
    assert lab.executors[0].closed
    assert lab.executors[0].view_at_close == ["not_running"]
    assert not lab.executors[1].closed
    assert lab.views.frame("owner", "bot-b")["state"] == "live"


def test_capacity_is_not_recovered_by_evicting_another_claimed_computer(lab):
    lab.call()
    lab.call(request=task("bot-b", "task-b"))
    lab.now = 100
    with pytest.raises(ValueError, match="slots"):
        lab.call(request=task("bot-c", "task-c"))
    assert len(lab.executors) == 2
    assert all(not executor.closed for executor in lab.executors)


@pytest.mark.parametrize("failure", [BrowserCancelledError("cancelled"), BrowserError("failed")])
def test_browser_failure_retires_computer_and_revokes_view_before_close(lab, failure):
    lab.call()
    executor = lab.executors[0]
    executor.browser.failure = failure
    expected = (
        CooperativeCancellationError if isinstance(failure, BrowserCancelledError) else ValueError
    )
    with pytest.raises(expected):
        lab.call()
    assert executor.closed
    assert executor.view_at_close == ["not_running"]
    assert lab.views.frame("owner", "bot-a")["state"] == "not_running"
    lab.call()
    assert len(lab.executors) == 2


def test_cancelled_release_retires_only_matching_task(lab):
    lab.call()
    lab.call(request=task("bot-b", "task-b"))
    lab.adapter.release("task-a", cancelled=True)
    assert lab.executors[0].closed
    assert not lab.executors[1].closed
    assert lab.executors[0].view_at_close == ["not_running"]


def test_reap_retires_claim_when_task_cancellation_check_becomes_unavailable(lab):
    class UnavailableToken:
        broken = False

        @property
        def is_cancelled(self):
            if self.broken:
                raise RuntimeError("Synthetic state lookup failure")
            return False

        def throw_if_cancelled(self):
            assert not self.is_cancelled

    cancellation = UnavailableToken()
    lab.call(cancellation=cancellation)
    cancellation.broken = True
    lab.adapter.reap()
    assert lab.executors[0].closed
    assert lab.executors[0].view_at_close == ["not_running"]


def test_normal_release_detaches_old_token_before_next_task_claim(lab):
    previous = threading.Event()
    lab.call(cancellation=EventCancellationToken(previous))
    lab.adapter.release("task-a", cancelled=False)
    previous.set()
    lab.adapter.reap()
    assert not lab.executors[0].closed
    current = threading.Event()
    lab.call(request=task(identifier="task-b"), cancellation=EventCancellationToken(current))
    lab.adapter.reap()
    assert not lab.executors[0].closed
    current.set()
    lab.adapter.reap()
    assert lab.executors[0].closed


@pytest.mark.parametrize("when", ["factory", "bind"])
def test_startup_cancellation_revokes_and_closes_without_browser_dispatch(when):
    event = threading.Event()

    class BindingViews(ComputerViews):
        def __init__(self):
            super().__init__()
            self.bind_calls = 0

        def bind(self, *args, **kwargs):
            self.bind_calls += 1
            generation = super().bind(*args, **kwargs)
            if when == "bind":
                event.set()
            return generation

    views = BindingViews()
    lab = Harness(views=views)
    if when == "factory":
        lab.factory_hook = lambda executor, cancel: event.set()
    try:
        with pytest.raises(CooperativeCancellationError):
            lab.call(cancellation=EventCancellationToken(event))
        executor = lab.executors[0]
        assert executor.closed
        assert executor.browser.calls == []
        assert views.bind_calls == (0 if when == "factory" else 1)
        assert views.frame("owner", "bot-a")["state"] == "not_running"
    finally:
        lab.adapter.close()


def test_failed_view_binding_closes_new_executor_and_preserves_previous_binding():
    views = ComputerViews()
    previous = views.bind("owner", "bot-a", capture=lambda: b"", alive=lambda: False)
    lab = Harness(views=views)
    try:
        with pytest.raises(ValueError):
            lab.call()
        assert lab.executors[0].closed
        assert lab.executors[0].browser.calls == []
        assert views.frame("owner", "bot-a")["state"] == "disconnected"
    finally:
        lab.adapter.close()
        views.revoke("owner", "bot-a", previous)


@pytest.mark.parametrize("when", ["cancel-before-bind", "bind-fails"])
def test_failed_cleanup_before_binding_retains_executor_for_retry(when):
    views = ComputerViews()
    previous = None
    if when == "bind-fails":
        previous = views.bind("owner", "bot-a", capture=lambda: b"", alive=lambda: False)
    lab = Harness(views=views, max_computers=1)
    event = threading.Event()

    def prepare(executor, cancel):
        executor.close_failures = 2
        if when == "cancel-before-bind":
            event.set()

    lab.factory_hook = prepare
    try:
        with pytest.raises(RuntimeError):
            lab.call(cancellation=EventCancellationToken(event))
        executor = lab.executors[0]
        assert executor.close_calls == 1
        assert not executor.closed
        with pytest.raises(RuntimeError):
            lab.call(request=task("bot-b", "task-b"))
        assert len(lab.executors) == 1
        assert views.frame("owner", "bot-a")["state"] == (
            "disconnected" if previous else "not_running"
        )
        lab.adapter.close()
        assert executor.close_calls == 3
        assert executor.closed
        assert views.frame("owner", "bot-a")["state"] == (
            "disconnected" if previous else "not_running"
        )
    finally:
        lab.adapter.close()
        if previous:
            views.revoke("owner", "bot-a", previous)


def test_closed_adapter_never_allocates_again_and_close_is_idempotent(lab):
    lab.call()
    lab.adapter.close()
    lab.adapter.close()
    with pytest.raises(ValueError, match="stopped|closed"):
        lab.call(request=task("bot-b", "task-b"))
    assert len(lab.executors) == 1
    assert lab.executors[0].close_calls == 1


def test_cleanup_failure_retains_capacity_and_retries_before_replacement():
    lab = Harness(max_computers=1)
    lab.call()
    executor = lab.executors[0]
    executor.close_failures = 2
    try:
        with pytest.raises(RuntimeError):
            lab.adapter.release("task-a", cancelled=True)
        assert lab.views.frame("owner", "bot-a")["state"] == "not_running"
        with pytest.raises(RuntimeError):
            lab.call(request=task("bot-b", "task-b"))
        assert len(lab.executors) == 1
        lab.adapter.close()
        assert executor.close_calls == 3
        assert executor.closed
    finally:
        lab.adapter.close()


def test_close_attempts_every_executor_and_retries_failed_cleanup(lab):
    lab.call()
    lab.call(request=task("bot-b", "task-b"))
    first, second = lab.executors
    first.close_failures = 1
    with pytest.raises(RuntimeError):
        lab.adapter.close()
    assert first.close_calls == 1
    assert second.closed
    assert first.view_at_close == second.view_at_close == ["not_running"]
    with pytest.raises(ValueError, match="stopped|closed"):
        lab.call(request=task("bot-c", "task-c"))
    lab.adapter.close()
    assert first.closed
    assert first.close_calls == 2
    assert second.close_calls == 1


def test_factory_startup_cleanup_failure_retains_slot_until_immediate_reap_succeeds():
    lab = Harness(max_computers=1)

    def startup_failure(executor, cancel):
        if len(lab.executors) == 1:
            executor.close_failures = 2
            try:
                executor.close()
            except RuntimeError:
                raise BrowserStartupCleanupError(executor) from None

    lab.factory_hook = startup_failure
    try:
        with pytest.raises(ValueError, match="startup failed; cleanup is pending"):
            lab.call()
        executor = lab.executors[0]
        assert executor.close_calls == 1
        assert not executor.closed
        assert executor.browser.calls == []
        assert lab.views.frame("owner", "bot-a")["state"] == "not_running"
        with pytest.raises(RuntimeError):
            lab.call(request=task("bot-b", "task-b"))
        assert executor.close_calls == 2
        assert len(lab.executors) == 1
        # No idle interval has elapsed and the failed task was not released.
        # Retired executors still retry cleanup before admitting a replacement.
        assert lab.now == 0
        lab.adapter.reap()
        assert executor.close_calls == 3
        assert executor.closed
        assert lab.call(request=task("bot-b", "task-b"))["source_id"]
        assert len(lab.executors) == 2
        assert lab.views.frame("owner", "bot-a")["state"] == "not_running"
    finally:
        lab.adapter.close()


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_linux_constructor_failure_preserves_executor_if_cleanup_also_fails(
    tmp_path, monkeypatch, cleanup_fails
):
    class SetupFailure(ValueError):
        pass

    class Directory:
        name = str(tmp_path / "fake-owned-computer")

        def cleanup(self):
            pass

    def fail_start(*args, **kwargs):
        raise SetupFailure("private setup details")

    owned = []
    original_close = LinuxBrowserComputer.close

    def cleanup(computer):
        owned.append(computer)
        if cleanup_fails:
            raise RuntimeError("private cleanup details")
        original_close(computer)

    monkeypatch.setattr(browser_linux.tempfile, "TemporaryDirectory", lambda **_: Directory())
    monkeypatch.setattr(browser_linux.subprocess, "Popen", fail_start)
    monkeypatch.setattr(LinuxBrowserComputer, "close", cleanup)
    expected = BrowserStartupCleanupError if cleanup_fails else SetupFailure
    with pytest.raises(expected) as raised:
        LinuxBrowserComputer("bot-a", lambda: False)
    assert len(owned) == 1
    if cleanup_fails:
        assert raised.value.executor is owned[0]
        assert "private setup details" not in str(raised.value)
        assert "private cleanup details" not in str(raised.value)
    else:
        assert owned[0]._cleaned


def test_linux_close_retries_browser_cleanup_and_attempts_all_owned_processes():
    class Process:
        def __init__(self, failures=0):
            self.failures, self.running, self.terminations = failures, True, 0

        def poll(self):
            return None if self.running else 0

        def terminate(self):
            self.terminations += 1
            if self.failures:
                self.failures -= 1
                raise RuntimeError("Synthetic process cleanup failure")
            self.running = False

        def wait(self, timeout):
            assert not self.running

    class Browser:
        def __init__(self):
            self.calls = 0

        def close_all(self):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("Synthetic browser cleanup failure")

    class Directory:
        def __init__(self):
            self.cleaned = False

        def cleanup(self):
            self.cleaned = True

    # Constructor process launch is outside this provider-free lifecycle unit.
    computer = object.__new__(LinuxBrowserComputer)
    computer._closed = False
    computer._cleaned = False
    computer.browser = Browser()
    computer._processes = [Process(), Process(failures=1)]
    computer._directory = Directory()
    with pytest.raises(RuntimeError):
        computer.close()
    assert all(process.terminations == 1 for process in computer._processes)
    computer.close()
    assert computer.browser.calls == 2
    assert all(not process.running for process in computer._processes)
    assert computer._directory.cleaned


def test_browser_bar_button_opens_a_window_of_the_managed_browser_only(tmp_path):
    # Pressing Browser on the bar leaves a request file; the controller opens the window
    # through the owl's managed browser. The bar itself is not part of alive().
    import threading
    import time

    class Browser:
        def __init__(self):
            self.opened = []

        def open_window(self, owner, session_id):
            self.opened.append((owner, session_id))
            return True

    class Directory:
        def cleanup(self):
            pass

    class Bar:
        def poll(self):
            return 1  # the bar crashed; the computer is still alive

    computer = object.__new__(LinuxBrowserComputer)
    computer.owner, computer.session_id = "bot-a", "session-1"
    computer.browser = Browser()
    computer._closed = computer._cleaned = False
    computer._processes, computer._decorations = [], [Bar()]
    computer._directory = Directory()
    computer._stop_watching = threading.Event()
    computer._open_request = tmp_path / "open-browser"
    watcher = threading.Thread(target=computer._watch_open_requests, daemon=True)
    watcher.start()
    computer._open_request.touch()
    deadline = time.monotonic() + 5
    while not computer.browser.opened and time.monotonic() < deadline:
        time.sleep(0.05)
    assert computer.browser.opened == [("bot-a", "session-1")]
    assert not computer._open_request.exists()
    computer.browser.close_all = lambda: None
    computer.close()
    watcher.join(timeout=2)
    assert not watcher.is_alive()


def test_consumed_observation_stays_readable_but_cannot_replay_input(lab):
    source = lab.call()["source_id"]
    lab.call("browser_type", {"source_id": source, "node": 5, "text": "one"})
    with pytest.raises(ValueError, match="stale or already used"):
        lab.call("browser_type", {"source_id": source, "node": 5, "text": "two"})
    assert lab.call("browser_source", {"source_id": source})["content"]
    assert len([c for c in lab.executors[0].browser.calls if c[0] == "type"]) == 1


def test_observe_and_task_release_invalidate_prior_action_reference(lab):
    source = lab.call()["source_id"]
    current = lab.call()["source_id"]
    with pytest.raises(ValueError, match="stale"):
        lab.call("browser_click", {"source_id": source, "node": 6})
    lab.adapter.release("task-a", cancelled=False)
    with pytest.raises(ValueError, match="stale"):
        lab.call(
            "browser_click", {"source_id": current, "node": 6}, request=task(identifier="next")
        )


def select_dom():
    source = dom()
    source["strings"] += ["SELECT", "OPTION", "OPTGROUP", "Ελλάδα", "日本語"]
    nodes = source["documents"][0]["nodes"]
    nodes["nodeName"][6] = 14
    nodes["nodeName"][7] = 15
    nodes["nodeType"][7] = 1
    nodes["nodeValue"][7] = -1
    for key, values in {
        "parentIndex": [7, 6, 9, 10],
        "nodeType": [3, 1, 1, 3],
        "nodeName": [8, 16, 15, 8],
        "nodeValue": [17, -1, -1, 18],
        "backendNodeId": [109, 110, 111, 112],
        "attributes": [[], [], [], []],
    }.items():
        nodes[key].extend(values)
    nodes["optionSelected"] = {"index": [7]}
    source["documents"][0]["layout"]["nodeIndex"].remove(7)
    return source


def test_observed_native_select_preserves_nonlayout_options_and_identity(lab):
    lab.factory_hook = lambda executor, cancel: setattr(executor.browser, "data", select_dom())
    source = lab.call()
    nodes = {n["node"]: n for n in source["dom"]["documents"][0]["nodes"]}
    assert nodes[7]["selected"] is True
    assert nodes[8]["text"] == "Ελλάδα" and nodes[11]["text"] == "日本語"
    browser = lab.executors[0].browser

    def select(owner, sid, selector, option, **kwargs):
        browser._call("select", owner, sid, selector, option, **kwargs)
        browser.data["documents"][0]["nodes"]["optionSelected"] = {"index": [10]}

    browser.select_option = select
    result = lab.call(
        "browser_select", {"source_id": source["source_id"], "node": 6, "option_node": 10}
    )
    action = next(c for c in browser.calls if c[0] == "select")
    assert action[4]["expected_backend_node_id"] == 107
    assert action[4]["expected_option_backend_node_id"] == 111
    assert action[4]["expected_generation"] == 17
    assert result["action"]["dispatched"] is True
    assert any(
        n.get("selected") and n["node"] == 10 for n in result["dom"]["documents"][0]["nodes"]
    )


@pytest.mark.parametrize("node,option", [(5, 10), (6, 11), (5, 7), (6, 999)])
def test_select_requires_observed_select_and_own_option(lab, node, option):
    lab.factory_hook = lambda executor, cancel: setattr(executor.browser, "data", select_dom())
    source = lab.call()["source_id"]
    with pytest.raises(ValueError, match="observed"):
        lab.call("browser_select", {"source_id": source, "node": node, "option_node": option})


@pytest.mark.parametrize(
    "name,extra,method",
    [
        ("browser_key", {"key": "Enter", "shift": True}, "press_key"),
        ("browser_scroll", {"delta_y": 600, "delta_x": -10}, "scroll"),
    ],
)
def test_new_input_tools_use_observed_reference_and_return_new_observation(
    lab, name, extra, method
):
    source = lab.call()["source_id"]
    browser = lab.executors[0].browser

    def action(owner, sid, *args, **kwargs):
        browser._call(method, owner, sid, *args, **kwargs)
        browser.data["strings"][13] = "Actual fixture update"

    setattr(browser, method, action)
    result = lab.call(name, {"source_id": source, "node": 5, **extra})
    call = next(c for c in browser.calls if c[0] == method)
    assert call[4]["expected_backend_node_id"] == 106
    assert call[4]["expected_generation"] == 17
    assert result["source_id"] != source and result["action"]["verification_required"]


@pytest.mark.parametrize(
    "name,extra",
    [
        ("browser_key", {"key": "Ctrl+L"}),
        ("browser_key", {"key": "Enter", "shift": 1}),
        ("browser_scroll", {"delta_y": True}),
        ("browser_scroll", {"delta_y": 2001}),
        ("browser_select", {"option_node": "10"}),
    ],
)
def test_new_control_arguments_fail_before_allocation(lab, name, extra):
    with pytest.raises(ValueError):
        lab.call(name, {"source_id": "made-up", "node": 5, **extra})
    assert lab.executors == []


def install_visual_fixture(lab):
    from alysis_code.ide.managed_browser import BrowserVisualFrame

    lab.call()
    browser = lab.executors[0].browser

    def capture(owner, sid, **kwargs):
        browser._call("capture", owner, sid, **kwargs)
        return BrowserVisualFrame(owner, sid, 17, 0, PREVIEW, 2, 2, 2, 2, b"fixture-pixels")

    browser.capture_visual_frame = capture

    def click(owner, sid, frame, x, y, **kwargs):
        browser._call("visual-click", owner, sid, frame, x, y, **kwargs)
        browser.data["strings"][13] = "Actual fixture click"

    browser.click_visual_frame = click
    return browser


def test_visual_tools_are_gated_and_return_image_separately_from_tool_text(lab):
    from alysis_code.bots.browser import BrowserImageResult

    for eligible in (False, True):
        names = {
            s["function"]["name"] for s in lab.adapter.schemas(task(), True, image_input=eligible)
        }
        assert ("browser_screenshot" in names) == eligible
        assert ("browser_visual_click" in names) == eligible
    with pytest.raises(ValueError, match="image-capable"):
        lab.call("browser_screenshot")
    assert lab.executors == []
    install_visual_fixture(lab)
    image = lab.adapter.call(task(), "browser_screenshot", {}, token(), image_input=True)
    assert isinstance(image, BrowserImageResult)
    assert image.image_url.startswith("data:image/png;base64,")
    assert "image_url" not in image.observation
    result = lab.adapter.call(
        task(),
        "browser_visual_click",
        {"source_id": image.observation["source_id"], "x": 1, "y": 1},
        token(),
        image_input=True,
    )
    assert isinstance(result, BrowserImageResult)
    assert result.observation["action"]["dispatched"]
    assert result.observation["source_id"] != image.observation["source_id"]
    with pytest.raises(ValueError, match="stale"):
        lab.adapter.call(
            task(),
            "browser_visual_click",
            {"source_id": image.observation["source_id"], "x": 1, "y": 1},
            token(),
            image_input=True,
        )


def test_visual_rejection_requires_refresh_but_retains_computer(lab):
    from alysis_code.ide.managed_browser import BrowserStaleFrameError

    browser = install_visual_fixture(lab)
    image = lab.adapter.call(task(), "browser_screenshot", {}, token(), image_input=True)

    def stale(*args, **kwargs):
        raise BrowserStaleFrameError("changed")

    browser.click_visual_frame = stale
    with pytest.raises(ValueError, match="No click was sent"):
        lab.adapter.call(
            task(),
            "browser_visual_click",
            {"source_id": image.observation["source_id"], "x": 1, "y": 1},
            token(),
            image_input=True,
        )
    assert not lab.executors[0].closed
    assert lab.adapter._computers["bot-a"].visual_frame is None


def test_post_action_capture_change_keeps_honest_dispatch_receipt(lab):
    from alysis_code.ide.managed_browser import BrowserStaleFrameError

    browser = install_visual_fixture(lab)
    image = lab.adapter.call(task(), "browser_screenshot", {}, token(), image_input=True)

    def changed(*args, **kwargs):
        raise BrowserStaleFrameError("changed after action")

    browser.capture_visual_frame = changed
    result = lab.adapter.call(
        task(),
        "browser_visual_click",
        {"source_id": image.observation["source_id"], "x": 1, "y": 1},
        token(),
        image_input=True,
    )
    assert result["action"]["dispatched"]
    assert "Input was sent" in result["image_unavailable"]
    assert not lab.executors[0].closed


def test_visual_action_rechecks_website_authority(lab):
    install_visual_fixture(lab)
    image = lab.adapter.call(task(), "browser_screenshot", {}, token(), image_input=True)
    lab.adapter.interactive_previews = False
    for origins in [(), ("https://example.org",)]:
        with pytest.raises(ValueError, match="approval"):
            lab.adapter.call(
                task(),
                "browser_visual_click",
                {"source_id": image.observation["source_id"], "x": 1, "y": 1},
                token(),
                image_input=True,
                approved_origins=origins,
            )
    assert not any(c[0] == "visual-click" for c in lab.executors[0].browser.calls)


@pytest.mark.parametrize("changed", ["url", "command_generation"])
def test_screenshot_must_match_observed_document_and_ownership(lab, changed):
    browser = install_visual_fixture(lab)
    capture = browser.capture_visual_frame

    def changed_frame(*args, **kwargs):
        frame = capture(*args, **kwargs)
        setattr(frame, changed, "https://different.example/" if changed == "url" else 18)
        return frame

    browser.capture_visual_frame = changed_frame
    with pytest.raises(ValueError, match="frame changed"):
        lab.adapter.call(task(), "browser_screenshot", {}, token(), image_input=True)
    assert lab.adapter._computers["bot-a"].visual_frame is None
    assert lab.adapter._computers["bot-a"].action_source_id is None
    assert not lab.executors[0].closed


def test_upload_requires_owned_file_permission_and_observed_file_control(tmp_path):
    from alysis_code.bots.models import BotInput
    from alysis_code.bots.store import NotFound, Store

    store = Store(tmp_path / "bots.db")
    bot = store.save_bot("owner", BotInput(name="Uploader", purpose="Selected file"))
    peer = store.save_bot("owner", BotInput(name="Peer", purpose="Other files"))
    file = store.save_bytes("owner", bot["id"], "report.bin", b"\x00selected bytes")
    foreign = store.save_bytes("owner", peer["id"], "private.bin", b"private")
    work = {**task(bot["id"]), "config": {"files_enabled": True}}
    h = Harness()
    h.adapter.store = store
    page = h.adapter.call(work, "browser_observe", {}, token())
    args = {"source_id": page["source_id"], "node": 5, "file_id": file["id"]}
    with pytest.raises(ValueError, match="file input"):
        h.adapter.call(work, "browser_upload", args, token())
    with pytest.raises(NotFound):
        h.adapter.call(work, "browser_upload", {**args, "file_id": foreign["id"]}, token())
    browser = h.executors[0].browser
    browser.data["strings"] += ["type", "file"]
    browser.data["documents"][0]["nodes"]["attributes"][5] += [14, 15]
    received = []

    def upload(owner, sid, selector, **kwargs):
        received.append(b"".join(kwargs["chunks"]))

    browser.upload_file = upload
    page = h.adapter.call(work, "browser_observe", {}, token())
    args["source_id"] = page["source_id"]
    result = h.adapter.call(work, "browser_upload", args, token())
    assert received == [b"\x00selected bytes"]
    assert result["action"]["verification_required"]
    with pytest.raises(ValueError, match="stale"):
        h.adapter.call(work, "browser_upload", args, token())
    with store.connect(write=True) as db:
        db.execute("UPDATE bots SET files_enabled=0 WHERE id=?", (bot["id"],))
    with pytest.raises(ValueError, match="unavailable"):
        h.adapter.call(work, "browser_upload", args, token())
    h.adapter.close()


def test_page_switch_consumes_inventory_and_old_actions_without_expanding_approval(lab):
    root = lab.call()
    browser = lab.executors[0].browser
    pages = [
        dict(page_id="root", url=PREVIEW, active=True),
        dict(page_id="popup", url=PUBLIC, active=False),
    ]
    browser.pages = lambda *a, **k: dict(pages=pages, command_generation=browser.generation)

    def switch(owner, session, page_id, **kwargs):
        browser._call("switch", owner, session, page_id, **kwargs)
        assert kwargs["expected_generation"] == browser.generation
        browser.generation += 1
        browser.data = dom(PUBLIC)

    browser.switch_page = switch
    old = lab.call("browser_pages")
    inventory = lab.call("browser_pages")
    with pytest.raises(ValueError, match="inventory expired"):
        lab.call("browser_switch_page", dict(source_id=old["source_id"], page_id="popup"))
    with pytest.raises(ValueError, match="not in the observed"):
        lab.call("browser_switch_page", dict(source_id=inventory["source_id"], page_id="invented"))
    popup = lab.call("browser_switch_page", dict(source_id=inventory["source_id"], page_id="popup"))
    assert popup["source_id"] != root["source_id"]
    with pytest.raises(ValueError, match="stale"):
        lab.call("browser_click", dict(source_id=root["source_id"], node=6))
    with pytest.raises(ValueError, match="approval"):
        lab.call("browser_click", dict(source_id=popup["source_id"], node=6))
    assert not any(c[0] == "click" for c in browser.calls)
    with pytest.raises(ValueError, match="inventory expired"):
        lab.call("browser_switch_page", dict(source_id=inventory["source_id"], page_id="root"))
    inventory = lab.call("browser_pages")
    with pytest.raises(ValueError, match="busy"):
        lab.call("browser_pages", request=task(identifier="other-task"))
    lab.adapter.release("task-a", cancelled=False)
    with pytest.raises(ValueError, match="inventory expired"):
        lab.call("browser_switch_page", dict(source_id=inventory["source_id"], page_id="popup"))


def test_close_tab_uses_current_inventory_and_invalidates_action_refs(lab):
    lab.adapter = BrowserComputers("owner", lab.views, lab.factory)
    root = lab.call()
    browser = lab.executors[0].browser
    pages = [dict(page_id="root", active=True), dict(page_id="popup", active=False)]
    browser.pages = lambda *a, **k: dict(pages=list(pages), command_generation=browser.generation)

    def close(owner, session, page_id, **kwargs):
        browser._call("close_page", owner, session, page_id, **kwargs)
        assert kwargs["expected_generation"] == browser.generation
        browser.generation += 1
        pages.remove(next(p for p in pages if p["page_id"] == page_id))
        return dict(closed=True, reason="closed")

    browser.close_page = close
    inventory = lab.call("browser_pages")
    args = dict(source_id=inventory["source_id"], page_id="popup")
    with pytest.raises(ValueError, match="not in the observed"):
        lab.call("browser_close_page", {**args, "page_id": "foreign"})
    with pytest.raises(ValueError, match="busy"):
        lab.call("browser_close_page", args, request=task(identifier="other-task"))
    observed = lab.call("browser_observe")
    with pytest.raises(ValueError, match="inventory expired"):
        lab.call("browser_close_page", args)
    args = dict(source_id=observed["source_id"], page_id="popup")
    result = lab.call("browser_close_page", args)
    assert result["closed"] and result["pages"] == pages
    assert result["source_id"] != inventory["source_id"]
    with pytest.raises(ValueError, match="inventory expired"):
        lab.call("browser_close_page", args)
    # Cleanup is available without website approval; it does not grant input.
    with pytest.raises(ValueError, match="approval"):
        lab.call("browser_click", dict(source_id=root["source_id"], node=6))
    assert lab.adapter._computers["bot-a"].action_source_id is None
    lab.adapter.release("task-a", cancelled=False)
    with pytest.raises(ValueError, match="inventory expired"):
        lab.call("browser_close_page", dict(source_id=result["source_id"], page_id="root"))


@pytest.mark.parametrize("siblings", [True, False])
def test_detached_page_invalidates_references_without_replaying_input(lab, siblings):
    from alysis_code.ide.managed_browser import BrowserPageUnavailableError

    original = lab.call()
    executor = lab.executors[0]
    browser = executor.browser
    pages = [dict(page_id="sibling", active=False)] if siblings else []
    browser.pages = lambda *a, **k: dict(pages=pages, command_generation=browser.generation)
    inventory = lab.call("browser_pages")

    def detached_click(owner, session, selector, **kwargs):
        browser._call("click", owner, session, selector, **kwargs)
        raise BrowserPageUnavailableError("Target detached during input")

    browser.click = detached_click
    with pytest.raises(ValueError, match="Do not repeat"):
        lab.call("browser_click", dict(source_id=original["source_id"], node=6))
    assert sum(c[0] == "click" for c in browser.calls) == 1
    assert executor.closed is (not siblings)
    if siblings:
        with pytest.raises(ValueError, match="stale"):
            lab.call("browser_click", dict(source_id=original["source_id"], node=6))
        with pytest.raises(ValueError, match="inventory expired"):
            lab.call(
                "browser_switch_page", dict(source_id=inventory["source_id"], page_id="sibling")
            )
        assert lab.call("browser_pages")["pages"] == pages
    else:
        assert lab.views.frame("owner", "bot-a")["state"] == "not_running"
        assert lab.call()["source_id"] != original["source_id"]


def test_observation_does_not_join_dom_and_pages_from_different_generations(lab):
    lab.call()
    browser = lab.executors[0].browser
    browser.pages = lambda *a, **kw: dict(
        pages=[dict(page_id="root", active=True)], command_generation=browser.generation + 1
    )
    with pytest.raises(ValueError, match="Page changed"):
        lab.call("browser_observe")
    computer = lab.adapter._computers["bot-a"]
    assert computer.action_source_id is None
    assert computer.page_inventory is None


def test_bot_observation_distinguishes_filtered_nodes_from_missing_rendered_nodes(lab):
    observation = lab.call()
    document = observation["dom"]["documents"][0]
    assert document["filtered_non_layout_nodes"] == 2  # HEAD and the hidden input.
    assert "omitted_nodes" not in document
    assert not document["truncated"]
    assert [node["node"] for node in document["nodes"]] == [0, 1, 3, 5, 6, 7]
    # Filtering is not deletion or authorization to act on hidden controls.
    raw = lab.call("browser_source", {"source_id": observation["source_id"]})
    assert "Hidden input" in raw["content"]
    with pytest.raises(ValueError):
        lab.call("browser_type", {"source_id": observation["source_id"], "node": 4, "text": "x"})


def test_bot_observation_keeps_real_budget_omissions_explicit(lab, monkeypatch):
    from alysis_code.bots import browser
    from alysis_code.ide.browser_observation import compact_dom_snapshot

    monkeypatch.setattr(
        browser, "compact_dom_snapshot", lambda data: compact_dom_snapshot(data, max_chars=1000)
    )
    lab.factory_hook = lambda executor, cancel: executor.browser.data["strings"].__setitem__(
        10, "入力" * 500
    )
    observation = lab.call()
    document = observation["dom"]["documents"][0]
    assert observation["dom"]["clipped"] and document["truncated"]
    assert document["omitted_nodes"] > 2
    assert "filtered_non_layout_nodes" not in document
    assert (
        "入力" * 500
        in lab.call("browser_source", {"source_id": observation["source_id"]})["content"]
    )


def test_failed_step_keeps_a_responsive_browser_and_its_pages(lab, monkeypatch):
    """One failed step must not cost the owl its open pages, sign-ins and downloads."""
    from alysis_code.bots.browser import BrowserObservationExpired
    from alysis_code.ide.managed_browser import BrowserError

    lab.call()
    executor = lab.executors[0]
    recorded = []
    monkeypatch.setattr(
        "alysis_code.bots.browser.diagnostics.record",
        lambda event, error=None, **facts: recorded.append((event, type(error).__name__, facts)),
    )

    def fail(*args, **kwargs):
        raise BrowserError("private upstream detail")

    monkeypatch.setattr(executor.browser, "navigate", fail)
    with pytest.raises(BrowserObservationExpired, match="browser is still open") as failure:
        lab.call("browser_navigate", {"url": "https://example.com/attachment"})
    assert "private upstream detail" not in str(failure.value)
    assert "do not repeat" in str(failure.value)
    assert not executor.closed
    assert recorded == [("browser_step_failed", "BrowserError", {"tool": "browser_navigate"})]
    monkeypatch.undo()
    assert lab.call()["source_id"]  # the same computer answers the next step
    assert len(lab.executors) == 1


def test_an_unresponsive_browser_is_closed_and_says_so(lab, monkeypatch):
    from alysis_code.bots.browser import BrowserObservationExpired
    from alysis_code.ide.managed_browser import BrowserError

    lab.call()
    executor = lab.executors[0]

    def fail(*args, **kwargs):
        raise BrowserError("private upstream detail")

    monkeypatch.setattr(executor.browser, "navigate", fail)
    monkeypatch.setattr(executor.browser, "pages", fail)
    with pytest.raises(BrowserObservationExpired, match="stopped responding") as failure:
        lab.call("browser_navigate", {"url": "https://example.com/attachment"})
    assert "private upstream detail" not in str(failure.value)
    assert executor.closed


@pytest.mark.parametrize(
    "failure,expected",
    [
        ("validation", "Browser URL host is required. Nothing was opened or sent"),
        (
            "security",
            "Local and private browser destinations are disabled. Nothing was opened or sent",
        ),
        ("timeout", "did not finish in time"),
        ("limit", "Close a page you no longer need"),
    ],
)
def test_refused_slow_or_full_steps_keep_the_computer(lab, monkeypatch, failure, expected):
    from alysis_code.ide.managed_browser import (
        BrowserLimitError,
        BrowserSecurityError,
        BrowserTimeoutError,
        BrowserValidationError,
    )

    error = {
        "validation": BrowserValidationError("Browser URL host is required."),
        "security": BrowserSecurityError("Local and private browser destinations are disabled."),
        "timeout": BrowserTimeoutError("Navigation timed out."),
        "limit": BrowserLimitError("Browser page limit reached (8 pages)."),
    }[failure]
    lab.call()
    executor = lab.executors[0]

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(executor.browser, "navigate", fail)
    with pytest.raises(ValueError, match=expected):
        lab.call("browser_navigate", {"url": PUBLIC + "next"})
    assert not executor.closed
    monkeypatch.undo()
    assert lab.call()["source_id"]
    assert len(lab.executors) == 1


def test_download_navigation_preserves_browser_and_returns_actionable_receipt(lab, monkeypatch):
    from alysis_code.ide.managed_browser import BrowserDownloadRequiredError

    initial = lab.call()
    executor = lab.executors[0]

    def attachment(*args, **kwargs):
        raise BrowserDownloadRequiredError("Attachment")

    monkeypatch.setattr(executor.browser, "navigate", attachment)
    result = lab.call("browser_navigate", {"url": PUBLIC + "download"})
    assert result["download_required"] and not result["file_saved"]
    assert not executor.closed
    assert lab.adapter._computers["bot-a"].action_source_id is None
    assert initial["source_id"] != lab.call()["source_id"]


def test_download_is_discoverable_without_grant_but_cannot_execute(lab):
    from unittest.mock import Mock

    from alysis_code.bots.browser import BrowserObservationExpired

    lab.adapter.interactive_previews = False
    lab.adapter.store = Mock()
    request = {**task(), "config": {"files_enabled": True}}
    names = {s["function"]["name"] for s in lab.adapter.schemas(request, True)}
    assert "browser_download" in names and "browser_upload" not in names
    with pytest.raises(BrowserObservationExpired, match="No website input was sent"):
        lab.adapter.call(
            request, "browser_download", {"source_id": "fake", "node": 0, "name": "x.csv"}, token()
        )
    assert lab.executors == []


def test_review_runs_after_site_scope_check_before_input_with_observed_context(lab):
    from alysis_code.bots.action_review import ApprovalRequired

    lab.adapter.interactive_previews = False
    observation = lab.call("browser_navigate", {"url": PUBLIC})
    args = {"source_id": observation["source_id"], "node": 5, "text": "Synthetic input"}
    seen = []

    def review(name, arguments, context):
        seen.append((name, arguments, context))
        raise ApprovalRequired("Ask human")

    with pytest.raises(ValueError):
        lab.adapter.call(
            task(),
            "browser_type",
            args,
            token(),
            approved_origins=("https://other.example.org",),
            review_action=review,
        )
    assert not seen
    with pytest.raises(ApprovalRequired):
        lab.adapter.call(
            task(),
            "browser_type",
            args,
            token(),
            approved_origins=("https://example.org",),
            review_action=review,
        )
    assert len(seen) == 1 and seen[0][1]["text"] == "Synthetic input"
    assert seen[0][2]["target"]["node"] == 5 and seen[0][2]["page"]["documents"]
    assert not any(c[0] == "type" for c in lab.executors[0].browser.calls)


def test_page_mutation_while_reviewer_thinks_invalidates_allow(lab):
    from alysis_code.bots.browser import BrowserObservationExpired

    source = lab.call()["source_id"]
    browser = lab.executors[0].browser
    # The changed suffix is beyond the normal compact observation's clipping.
    original = "東京" * 400 + "17.25"
    browser.data["strings"][12] = original

    def review(name, arguments, context):
        assert context["form"]["fields"][0]["value"] == original
        browser.data["strings"][12] = "東京" * 400 + "999.00"

    with pytest.raises(BrowserObservationExpired, match="changed during action review"):
        lab.adapter.call(
            task(),
            "browser_click",
            {"source_id": source, "node": 6},
            token(),
            review_action=review,
        )
    assert not any(c[0] == "click" for c in browser.calls)


def test_element_label_reads_visible_text_then_accessible_names():
    from alysis_code.bots.browser import element_label

    nodes = [
        {"node": 1, "parent": 0, "name": "A", "selector": "a", "attributes": {"href": "/more"}},
        {"node": 2, "parent": 1, "name": "SPAN"},
        {"node": 3, "parent": 2, "name": "#text", "text": "  Learn\n more "},
        {"node": 4, "parent": 1, "name": "#text", "text": "→ Ελληνικά"},
        {"node": 5, "parent": 0, "name": "BUTTON", "attributes": {"aria-label": "Close dialog"}},
        {"node": 6, "parent": 0, "name": "INPUT", "attributes": {"placeholder": "Email"}},
        {"node": 7, "parent": 0, "name": "DIV"},
    ]
    assert element_label(nodes, nodes[0]) == "Learn more → Ελληνικά"
    assert element_label(nodes, nodes[4]) == "Close dialog"
    assert element_label(nodes, nodes[5]) == "Email"
    assert element_label(nodes, nodes[6]) == ""
    long = [{"node": 1, "parent": 0, "name": "P"}, {"node": 2, "parent": 1, "text": "x" * 500}]
    assert len(element_label(long, long[0])) == 120


def test_element_label_keeps_scripts_without_spaces_intact():
    from alysis_code.bots.browser import element_label

    nodes = [
        {"node": 1, "parent": 0, "name": "A"},
        {"node": 2, "parent": 1, "name": "SPAN"},
        {"node": 3, "parent": 2, "name": "#text", "text": "東"},
        {"node": 4, "parent": 1, "name": "SPAN"},
        {"node": 5, "parent": 4, "name": "#text", "text": "京へ"},
        {"node": 6, "parent": 1, "name": "#text", "text": " 行く\n"},
    ]
    assert element_label(nodes, nodes[0]) == "東京へ 行く"


def test_css_animation_during_review_does_not_block_dom_control(lab):
    source = lab.call()["source_id"]
    browser = lab.executors[0].browser
    document = browser.data["documents"][0]
    document["layout"]["bounds"] = [[0, 0, 20, 20]] * 5
    document["textBoxes"] = {"bounds": [[0, 0, 20, 20]], "layoutIndex": [4]}

    def review(*args):
        # Geometry of a CSS-animated element changes while the reviewer thinks.
        document["layout"]["bounds"][1] = [120, 50, 20, 20]
        document["textBoxes"]["bounds"][0] = [120, 50, 20, 20]

    result = lab.adapter.call(
        task(), "browser_click", {"source_id": source, "node": 6}, token(), review_action=review
    )
    assert result["action"]["dispatched"] is True
    click = next(c for c in browser.calls if c[0] == "click")
    assert click[-1]["expected_backend_node_id"] == 107


@pytest.mark.parametrize("change", ["text", "identity", "attributes", "visibility"])
def test_dom_review_still_rejects_content_or_target_changes(lab, change):
    source = lab.call()["source_id"]
    browser = lab.executors[0].browser

    def review(*args):
        document = browser.data["documents"][0]
        if change == "text":
            browser.data["strings"][13] = "Different instruction"
        elif change == "identity":
            document["nodes"]["backendNodeId"][6] = 999
        elif change == "attributes":
            document["nodes"]["attributes"][6] = [9, 10]
        else:
            document["layout"]["nodeIndex"].remove(6)

    with pytest.raises(ValueError, match="changed during action review"):
        lab.adapter.call(
            task(), "browser_click", {"source_id": source, "node": 6}, token(), review_action=review
        )
    assert not any(c[0] == "click" for c in browser.calls)


def test_changing_visual_page_switches_to_dom_and_bounds_failed_input(lab):
    from alysis_code.bots.browser import BrowserInteractionStalled, BrowserObservationExpired
    from alysis_code.ide.managed_browser import BrowserStaleFrameError

    browser = install_visual_fixture(lab)

    def stale(*args, **kwargs):
        raise BrowserStaleFrameError("animation")

    browser.click_visual_frame = stale
    image = lab.adapter.call(task(), "browser_screenshot", {}, token(), image_input=True)
    with pytest.raises(BrowserObservationExpired, match="browser_click"):
        lab.adapter.call(
            task(),
            "browser_visual_click",
            {"source_id": image.observation["source_id"], "x": 1, "y": 1},
            token(),
            image_input=True,
        )
    names = {s["function"]["name"] for s in lab.adapter.schemas(task(), True, image_input=True)}
    assert "browser_click" in names and "browser_visual_click" not in names
    for attempt in range(2):
        source = lab.call()["source_id"]  # Reading a fresh page does not reset failed input.

        def changing(*args):
            browser.data["strings"][13] += "!"

        expected = BrowserInteractionStalled if attempt == 1 else BrowserObservationExpired
        with pytest.raises(expected):
            lab.adapter.call(
                task(),
                "browser_click",
                {"source_id": source, "node": 6},
                token(),
                review_action=changing,
            )
    assert not any(c[0] == "click" for c in browser.calls)


def test_successful_dom_action_resets_failed_input_budget(lab):
    from alysis_code.ide.managed_browser import BrowserStaleFrameError

    browser = install_visual_fixture(lab)
    image = lab.adapter.call(task(), "browser_screenshot", {}, token(), image_input=True)

    def stale(*args, **kwargs):
        raise BrowserStaleFrameError("animation")

    browser.click_visual_frame = stale
    with pytest.raises(ValueError):
        lab.adapter.call(
            task(),
            "browser_visual_click",
            {"source_id": image.observation["source_id"], "x": 1, "y": 1},
            token(),
            image_input=True,
        )
    source = lab.call()["source_id"]
    result = lab.adapter.call(task(), "browser_click", {"source_id": source, "node": 6}, token())
    assert result["action"]["dispatched"] is True
    assert lab.adapter._computers[task()["bot_id"]].stale_actions == 0


def test_human_handback_resets_failed_input_budget_and_visual_fallback(lab):
    lab.call()
    computer = lab.adapter._computers[task()["bot_id"]]
    computer.stale_actions = 3
    computer.prefer_dom = True
    lab.adapter._invalidate(computer)
    assert computer.stale_actions == 0
    assert not computer.prefer_dom
    assert computer.action_source_id is None


def test_stalled_review_returns_unused_approval_before_handoff(lab):
    from alysis_code.bots.browser import BrowserInputNotSent, BrowserInteractionStalled

    released = []
    for attempt in range(3):
        source = lab.call()["source_id"]

        def review(*args, attempt=attempt):
            lab.executors[0].browser.data["strings"][13] += "!"
            return SimpleNamespace(release=lambda: released.append(attempt))

        expected = BrowserInteractionStalled if attempt == 2 else BrowserInputNotSent
        with pytest.raises(expected):
            lab.adapter.call(
                task(),
                "browser_click",
                {"source_id": source, "node": 6},
                token(),
                review_action=review,
            )
        assert released == list(range(attempt + 1))
    assert not any(c[0] == "click" for c in lab.executors[0].browser.calls)
