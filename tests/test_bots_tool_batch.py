"""Several reads in one model response run side by side; every other step runs alone, in order."""

from __future__ import annotations

import contextvars
import json
import threading
import time

import pytest

from alysis_code.bots import tool_batch
from alysis_code.bots.browser_tools import NO_WEBSITE_APPROVAL_TOOLS
from alysis_code.bots.desktop import DEFINITIONS as COMPUTER_TOOLS
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_documents import DOCUMENT_TOOLS
from alysis_code.bots.owl_tables import TABLE_TOOL
from alysis_code.bots.owl_tools import TOOL_DEFINITIONS
from alysis_code.bots.owl_toolset import COMPACT_TOOLS
from alysis_code.bots.repeat_guard import REPEAT_STOPPED, WATCHED
from alysis_code.bots.research_helpers import RESEARCH_TOOL
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.step_effects import OWN_RECORDS, READING, TURN_CONTROL
from alysis_code.bots.store import Conflict, Store
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall

READ_SECONDS = 0.6

PAGES = {
    "https://el.example/kairos": "Ο καιρός στην Αθήνα είναι ηλιόλουστος, 24 °C.",
    "https://zh.example/tianqi": "北京今天多云，气温十八度。",
    "https://en.example/weather": "Berlin: light rain, 12 °C.",
    "https://es.example/tiempo": "Mañana en Sevilla: soleado, 30 °C.",
}


class ScriptedClient:
    supports_tool_calling = True

    def __init__(self, steps, model="model-a"):
        self.model = model
        self.steps = list(steps)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs["messages"])))
        step = self.steps.pop(0) if len(self.steps) > 1 else self.steps[0]
        if isinstance(step, BaseException):
            raise step
        return step


def respond(content="", calls=()):
    return LLMResponse(content, list(calls), {}, usage=LLMUsage(10, 5, 15))


def fetch(url, index):
    return ToolCall(f"call_{index}", "web_fetch", {"url": url})


def search(query, index):
    return ToolCall(f"call_{index}", "web_search", {"query": query})


class SlowWeb:
    """The web, slowly: each read waits, and when it started and ended is recorded."""

    def __init__(self, seconds=READ_SECONDS, *, failing=()):
        self.seconds = seconds
        self.failing = set(failing)
        self.lock = threading.Lock()
        self.spans: list[tuple[str, float, float]] = []
        self.release = threading.Event()
        self.begun = threading.Event()

    def _wait(self, what):
        start = time.monotonic()
        self.begun.set()
        seconds = self.seconds(what) if callable(self.seconds) else self.seconds
        self.release.wait(seconds)
        with self.lock:
            self.spans.append((what, start, time.monotonic()))

    def fetch(self, url, max_chars):
        self._wait(url)
        if url in self.failing:
            raise Conflict(
                "This page could not be read (the site did not answer). Use another source."
            )
        return {"url": url, "title": url, "text": PAGES.get(url, "")}

    def search(self, query, max_results):
        self._wait(query)
        return {"query": query, "results": [{"title": query, "url": "https://en.example/weather"}]}

    def span(self, what, nth=0):
        return [s for s in self.spans if s[0] == what][nth][1:]


@pytest.fixture
def web(monkeypatch):
    slow = SlowWeb()
    monkeypatch.setattr("alysis_code.bots.runtime.web_tools_available", lambda: True)
    monkeypatch.setattr("alysis_code.bots.runtime.web_fetch", slow.fetch)
    monkeypatch.setattr("alysis_code.bots.runtime.web_search", slow.search)
    yield slow
    slow.release.set()


def chat_task(tmp_path, message="Πώς είναι ο καιρός;"):
    store = Store(tmp_path / "bots.sqlite3")
    bot = store.save_bot("alice", BotInput(name="Atlas", purpose="Look things up"))
    chat = store.create_chat("alice", bot["id"], "New conversation")
    store.enqueue("alice", chat["id"], TaskInput(message=message, request_id="request_1"))
    return store, store.claim("alice")


def run(store, task, steps, *, token=None, model="model-a"):
    client = ScriptedClient(steps, model=model)
    runtime = BotRuntime(store, "alice", lambda _: client, limits=RuntimeLimits(calls_per_task=40))
    runtime.reply_review = None
    runtime.run(task, token or EventCancellationToken(threading.Event()))
    return store.task("alice", task["id"]), client


def tool_messages(client, request):
    return [m for m in client.requests[request] if m.get("role") == "tool"]


def overlapped(*spans):
    """Every one started before any of them ended."""
    return max(start for start, _ in spans) < min(end for _, end in spans)


def receipts(task):
    return [
        (e["kind"], e["data"]["name"])
        for e in task["events"]
        if e["kind"] in ("tool_started", "tool_completed", "tool_failed")
    ]


# ---------------------------------------------------------------- which calls share a group


def test_only_declared_reads_run_side_by_side():
    assert tool_batch.SIDE_BY_SIDE <= READING
    assert tool_batch.SIDE_BY_SIDE <= set(TOOL_DEFINITIONS)  # all answered by the tool runner
    # What holds something the turn has only one of, or spends its allowances, runs alone.
    alone = {
        *NO_WEBSITE_APPROVAL_TOOLS,
        *COMPUTER_TOOLS,
        RESEARCH_TOOL,
        *DOCUMENT_TOOLS,
        TABLE_TOOL,
        "read_browser_draft",
        *COMPACT_TOOLS,
        # Answered by the turn loop itself, not the tool runner.
        "read_peer_exchange",
        "read_background_task",
        "read_coding_result",
    }
    assert not tool_batch.SIDE_BY_SIDE & alone
    assert not tool_batch.SIDE_BY_SIDE & (OWN_RECORDS | TURN_CONTROL)


def test_groups_keep_the_order_and_split_at_every_other_step():
    calls = [
        fetch("https://el.example/kairos", 0),
        search("Αθήνα", 1),
        ToolCall("call_2", "save_file", {"name": "σημειώσεις.txt", "content": "x"}),
        fetch("https://zh.example/tianqi", 3),
        ToolCall("call_4", "browser_observe", {}),
        fetch("https://en.example/weather", 5),
        fetch("https://en.example/weather", 6),  # the same read again waits for the first
        ToolCall("call_7", "list_files", {}),
    ]
    together = tool_batch.side_by_side(
        ["web_fetch", "web_search", "save_file", "list_files", "browser_observe"],
        elsewhere=[{"browser_observe"}],
    )
    grouped = tool_batch.groups(calls, together)
    assert [[c.id for c in group] for group in grouped] == [
        ["call_0", "call_1"],
        ["call_2"],
        ["call_3"],
        ["call_4"],
        ["call_5"],
        ["call_6", "call_7"],
    ]
    assert [c for group in grouped for c in group] == calls


def test_a_group_is_at_most_the_worker_count_and_needs_the_tool_runner():
    calls = [fetch(f"https://en.example/{n}", n) for n in range(tool_batch.WORKERS + 2)]
    together = tool_batch.side_by_side(["web_fetch"], elsewhere=[])
    assert [len(g) for g in tool_batch.groups(calls, together)] == [tool_batch.WORKERS, 2]
    # A plugin tool of the same name, or a read this step does not offer, runs alone.
    plugin = tool_batch.side_by_side(["web_fetch"], elsewhere=[{"web_fetch": object()}])
    assert [len(g) for g in tool_batch.groups(calls[:3], plugin)] == [1, 1, 1]
    unoffered = tool_batch.side_by_side(["read_file"], elsewhere=[])
    assert [len(g) for g in tool_batch.groups(calls[:3], unoffered)] == [1, 1, 1]


def test_reads_on_workers_see_the_turns_context():
    allowance = contextvars.ContextVar("allowance", default=None)
    allowance.set("the turn's own")
    seen = []

    def read(name, arguments):
        seen.append((threading.current_thread().name, allowance.get()))
        return arguments["url"]

    calls = [fetch("https://el.example/kairos", 0), fetch("https://zh.example/tianqi", 1)]
    batch = tool_batch.Batch(
        calls,
        together=lambda _: True,
        admit=lambda _: None,
        started=lambda _: None,
        run=read,
        token=EventCancellationToken(threading.Event()),
    )
    assert [batch.result(call) for call, _ in batch] == [c.arguments["url"] for c in calls]
    assert all(
        thread.startswith("owl-read") and value == "the turn's own" for thread, value in seen
    )


# ---------------------------------------------------------------- in a turn


def test_four_slow_reads_take_about_one_reads_time(tmp_path, web):
    store, task = chat_task(tmp_path)
    urls = list(PAGES)
    reads = [
        fetch(urls[0], 0),
        search("καιρός Αθήνα σήμερα", 1),
        fetch(urls[1], 2),
        fetch(urls[2], 3),
    ]
    task, client = run(store, task, [respond(calls=reads), respond("Λιακάδα στην Αθήνα.")])
    assert task["status"] == "completed"
    spans = [
        web.span(urls[0]),
        web.span("καιρός Αθήνα σήμερα"),
        web.span(urls[1]),
        web.span(urls[2]),
    ]
    assert overlapped(*spans)
    took = max(end for _, end in spans) - min(start for start, _ in spans)
    assert took < 2 * READ_SECONDS < 4 * READ_SECONDS


def test_another_step_between_reads_waits_for_them_and_runs_before_the_next(
    tmp_path, web, monkeypatch
):
    store, task = chat_task(tmp_path)
    saving = []
    save_file = store.save_file

    def timed_save(*args, **kwargs):
        start = time.monotonic()
        saved = save_file(*args, **kwargs)
        saving.append((start, time.monotonic()))
        return saved

    monkeypatch.setattr(store, "save_file", timed_save)
    urls = list(PAGES)
    calls = [
        fetch(urls[0], 0),
        fetch(urls[1], 1),
        ToolCall("call_2", "save_file", {"name": "καιρός.txt", "content": "Αθήνα: ήλιος"}),
        fetch(urls[2], 3),
        fetch(urls[3], 4),
    ]
    task, client = run(store, task, [respond(calls=calls), respond("Αποθηκεύτηκε.")])
    assert task["status"] == "completed"
    (save,) = saving
    before, after = [web.span(urls[0]), web.span(urls[1])], [web.span(urls[2]), web.span(urls[3])]
    assert overlapped(*before) and overlapped(*after)
    assert max(end for _, end in before) <= save[0]
    assert save[1] <= min(start for start, _ in after)
    results = tool_messages(client, 1)
    assert [m["tool_call_id"] for m in results] == [c.id for c in calls]
    assert json.loads(results[2]["content"])["saved"] is True


@pytest.mark.parametrize("model", ["model-a", "small-model-b"])
def test_results_reach_the_model_in_the_order_asked(tmp_path, web, model):
    # The first read is the slowest, so the others finish first.
    urls = list(PAGES)
    web.seconds = lambda url: READ_SECONDS * (len(urls) - urls.index(url)) / len(urls)
    store, task = chat_task(tmp_path)
    calls = [fetch(url, n) for n, url in enumerate(urls)]
    task, client = run(store, task, [respond(calls=calls), respond("Έτοιμο.")], model=model)
    assert task["status"] == "completed"
    assert web.span(urls[-1])[1] < web.span(urls[0])[1]
    results = tool_messages(client, 1)
    assert [m["tool_call_id"] for m in results] == [c.id for c in calls]
    # Text in any language and script comes back as it was read.
    assert [json.loads(m["content"])["text"] for m in results] == [PAGES[url] for url in urls]
    assert "Ο καιρός στην Αθήνα είναι ηλιόλουστος" in results[0]["content"]


def test_each_read_keeps_its_own_started_and_finished_receipts_in_order(tmp_path, web):
    store, task = chat_task(tmp_path)
    calls = [
        fetch("https://el.example/kairos", 0),
        ToolCall("call_1", "list_files", {}),
        search("Θεσσαλονίκη", 2),
        ToolCall("call_3", "list_memories", {}),
    ]
    task, _ = run(store, task, [respond(calls=calls), respond("Τίποτα ακόμα.")])
    names = [c.name for c in calls]
    # All started before the first finished: they ran side by side. Each read's started receipt
    # comes before its finished one, and both come in the order asked.
    assert receipts(task) == [("tool_started", n) for n in names] + [
        ("tool_completed", n) for n in names
    ]
    started = [e for e in task["events"] if e["kind"] == "tool_started"]
    assert started[0]["data"]["host"] == "el.example"
    assert started[2]["data"]["topic"] == "Θεσσαλονίκη"


def test_stop_ends_the_turn_while_reads_run_and_starts_nothing_after(tmp_path, web):
    web.seconds = 3 * READ_SECONDS
    store, task = chat_task(tmp_path)
    count = tool_batch.WORKERS + 2
    calls = [fetch(f"https://en.example/page-{n}", n) for n in range(count)]
    stop = threading.Event()
    pressed = []

    def press_stop():
        # The person presses Stop while the first reads are running.
        web.begun.wait(10)
        time.sleep(READ_SECONDS / 3)
        pressed.append(time.monotonic())
        stop.set()

    threading.Thread(target=press_stop, daemon=True).start()
    task, _ = run(store, task, [respond(calls=calls)], token=EventCancellationToken(stop))
    took = time.monotonic() - pressed[0]
    assert task["status"] == "cancelled"
    # The turn ended without waiting for the reads it had started ...
    assert took < READ_SECONDS < web.seconds
    # ... and the reads after them never started.
    started = [e for e in task["events"] if e["kind"] == "tool_started"]
    assert len(started) == tool_batch.WORKERS
    assert not [e for e in task["events"] if e["kind"] in ("tool_completed", "tool_failed")]
    web.release.set()
    deadline = time.monotonic() + 5
    while len(web.spans) < tool_batch.WORKERS and time.monotonic() < deadline:
        time.sleep(0.02)
    time.sleep(0.2)
    assert len(web.spans) == tool_batch.WORKERS


def test_a_stopped_batch_raises_the_turns_cancellation_without_waiting():
    stop = threading.Event()
    hold = threading.Event()
    calls = [fetch("https://el.example/kairos", 0), fetch("https://zh.example/tianqi", 1)]
    batch = tool_batch.Batch(
        calls,
        together=lambda _: True,
        admit=lambda _: None,
        started=lambda _: None,
        run=lambda name, arguments: hold.wait(10),
        token=EventCancellationToken(stop),
    )
    calls_in_order = iter(batch)
    call, _ = next(calls_in_order)
    threading.Timer(0.2, stop.set).start()
    began = time.monotonic()
    with pytest.raises(CooperativeCancellationError):
        batch.result(call)
    assert time.monotonic() - began < 2
    hold.set()
    calls_in_order.close()


def test_reads_that_keep_failing_still_run_side_by_side_then_are_refused(tmp_path, web):
    urls = ["https://el.example/λείπει", "https://zh.example/缺失"]
    web.failing.update(urls)
    store, task = chat_task(tmp_path)
    stuck = respond(calls=[fetch(urls[0], 0), fetch(urls[1], 1)])
    task, client = run(store, task, [stuck])
    for url in urls:
        assert len([s for s in web.spans if s[0] == url]) == WATCHED.failures
    for round in range(WATCHED.failures):
        assert overlapped(web.span(urls[0], round), web.span(urls[1], round))
    ran = [json.loads(m["content"]) for m in tool_messages(client, WATCHED.failures)]
    assert all("could not be read" in r["error"] for r in ran)
    refused = [json.loads(m["content"]) for m in tool_messages(client, WATCHED.failures + 1)]
    assert len(refused) == 2 * (WATCHED.failures + 1)
    assert all(r["executed"] is False for r in refused[2 * WATCHED.failures :])
    assert task["status"] == "failed" and task["error"] == REPEAT_STOPPED


def test_the_same_read_twice_in_one_response_waits_for_the_first(tmp_path, web):
    url = "https://el.example/λείπει"
    web.failing.add(url)
    store, task = chat_task(tmp_path)
    task, client = run(store, task, [respond(calls=[fetch(url, 0), fetch(url, 1)])])
    first, second = web.span(url, 0), web.span(url, 1)
    assert first[1] <= second[0]
    # It failed three times, then was refused unrun, as when the calls ran one by one.
    assert len([s for s in web.spans if s[0] == url]) == WATCHED.failures
    results = [json.loads(m["content"]) for m in tool_messages(client, 2)]
    assert "could not be read" in results[2]["error"]
    assert results[3]["executed"] is False


# ---------------------------------------------------------------- the checks, as one by one


def test_a_read_asked_after_another_is_never_a_back_to_back_repeat(tmp_path, web):
    # The same search, back to back, until one more would stop the turn; then a page and the
    # same search again. One by one, the page comes between them, so the search runs (with the
    # guard's note); side by side it must too, and the turn goes on.
    web.seconds = 0.01
    store, task = chat_task(tmp_path)
    same = [respond(calls=[search("Αθήνα καιρός", index)]) for index in range(WATCHED.stop)]
    mixed = respond(calls=[fetch("https://zh.example/tianqi", 90), search("Αθήνα καιρός", 91)])
    task, client = run(store, task, [*same, mixed, respond("Ηλιόλουστος καιρός, 24 °C.")])
    assert task["status"] == "completed", task["error"]
    assert len([s for s in web.spans if s[0] == "Αθήνα καιρός"]) == WATCHED.stop + 1
    last = [json.loads(m["content"]) for m in tool_messages(client, WATCHED.stop + 1)][-1]
    assert "repeat_notice" in last and last["query"] == "Αθήνα καιρός"


def test_a_refused_read_runs_again_once_a_read_before_it_works(tmp_path, web):
    # A page failed the same way until the guard refuses it. Asked after a read that works, it
    # runs once more, as one by one: it is checked after that read, not beside it.
    url = "https://el.example/λείπει"
    web.failing.add(url)
    store, task = chat_task(tmp_path)
    failing = [respond(calls=[fetch(url, index)]) for index in range(WATCHED.failures)]
    after_work = respond(calls=[fetch("https://zh.example/tianqi", 90), fetch(url, 91)])
    task, client = run(store, task, [*failing, after_work, respond("Η σελίδα δεν απαντά.")])
    assert task["status"] == "completed", task["error"]
    assert len([s for s in web.spans if s[0] == url]) == WATCHED.failures + 1
    worked, again = web.span("https://zh.example/tianqi"), web.span(url, WATCHED.failures)
    assert worked[1] <= again[0]  # checked and run after the read that worked
    last = [json.loads(m["content"]) for m in tool_messages(client, WATCHED.failures + 1)][-1]
    assert "could not be read" in last["error"] and "executed" not in last


def test_reads_the_guard_lets_run_still_run_side_by_side_with_a_working_one(tmp_path, web):
    url = "https://el.example/λείπει"
    web.failing.add(url)
    store, task = chat_task(tmp_path)
    failing = [respond(calls=[fetch(url, index)]) for index in range(WATCHED.failures - 1)]
    together = respond(calls=[fetch("https://zh.example/tianqi", 90), fetch(url, 91)])
    task, client = run(store, task, [*failing, together, respond("Η σελίδα δεν απαντά.")])
    assert task["status"] == "completed", task["error"]
    assert overlapped(web.span("https://zh.example/tianqi"), web.span(url, WATCHED.failures - 1))
