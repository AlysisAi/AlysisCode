"""Research helpers: an owl looks into separate questions at once (research_helpers.py).

Real store and runtime; a scripted model that plays both the owl and its helpers (a request
that starts with the helpers' instructions is a helper's), scripted search and pages, no
network. Questions in Greek, Chinese and English; two models with different declared
capabilities.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest

from alysis_code.bots import research_helpers, web_lookup
from alysis_code.bots.context_summary import (
    TURN_PROGRESS_KEY,
    TurnProgress,
    result_receipt,
)
from alysis_code.bots.models import BotCreateInput, BotInput, TaskInput
from alysis_code.bots.owl_models import ModelConnection, PlaceModels
from alysis_code.bots.owl_toolset import Turn, tool_names
from alysis_code.bots.research_helpers import (
    HELPER_INSTRUCTIONS,
    HELPER_PAGE_CHARS,
    KEEP_FOR_OWL,
    MAX_HELPERS,
    REPORT_NOW,
    RESEARCH_TOOL,
    ResearchRequest,
)
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits, provider_retry_wait
from alysis_code.bots.store import Store, encode
from alysis_code.bots.task_results import TaskResults
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.llm.types import LLMError, LLMResponse, LLMUsage, ToolCall
from alysis_code.request_estimation import estimate_request_tokens

OWNER = "owner"
QUESTIONS = (
    "What does a museum day ticket cost for adults?",
    "Ποιες ώρες είναι ανοιχτό το μουσείο τις Κυριακές;",
    "博物馆周一开放吗？",
)


def page(n: int) -> str:
    return f"https://pages.example/{n}"


def response(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


def report(answer, facts, not_found=()):
    return response(
        json.dumps(
            {
                "answer": answer,
                "facts": [{"fact": fact, "link": link} for fact, link in facts],
                "not_found": list(not_found),
            },
            ensure_ascii=False,
        )
    )


class Model:
    """Plays the owl (``owl``: its responses in order) and every helper (``helper``: called
    with the helper's question, how many steps it took, the request and the tools offered)."""

    supports_tool_calling = True

    def __init__(self, owl, helper, *, model="research-fixture"):
        self.model = model
        self.owl = list(owl)
        self.helper = helper
        self.lock = threading.Lock()
        self.owl_requests: list[dict] = []
        self.helper_requests: list[dict] = []

    def chat(self, *, messages, tools=None, **kwargs):
        sent = json.loads(json.dumps(messages))
        seen = {
            "messages": sent,
            "tools": [t["function"]["name"] for t in tools or []],
            "schemas": tools,
            "max_tokens": kwargs.get("max_tokens"),
            "effort": getattr(self, "reasoning_effort", None),
            "thread": threading.current_thread().name,
        }
        first = sent[0]["content"]
        if isinstance(first, str) and first == HELPER_INSTRUCTIONS:
            brief = json.loads(sent[1]["content"])
            seen["question"] = brief["question"]
            with self.lock:
                self.helper_requests.append(seen)
            steps = sum(m["role"] == "assistant" for m in sent)
            return self.helper(brief["question"], steps, seen, kwargs)
        with self.lock:
            self.owl_requests.append(seen)
            return self.owl.pop(0)

    def helper_steps(self, question):
        return [r for r in self.helper_requests if r["question"] == question]


@pytest.fixture
def web(monkeypatch):
    """Scripted search and pages; every read and search is recorded."""
    seen = {"searches": [], "reads": [], "texts": {}}
    monkeypatch.setattr(web_lookup, "web_tools_available", lambda: True)
    monkeypatch.setattr("alysis_code.bots.runtime.web_tools_available", lambda: True)

    def search(query, limit):
        seen["searches"].append(query)
        return {
            "query": query,
            "results": [{"title": "Result", "url": page(n)} for n in range(1, 4)],
        }

    def fetch(url, chars):
        seen["reads"].append((url, chars))
        text = seen["texts"].get(url, f"Page {url[-1]}: open 9:00-17:00, ticket 12 EUR.")
        return {"url": url, "title": "Page", "text": text[:chars]}

    monkeypatch.setattr("alysis_code.bots.runtime.web_search", search)
    monkeypatch.setattr("alysis_code.bots.runtime.web_fetch", fetch)
    return seen


def owl_task(tmp_path, message="Find what the museum costs and when it opens.", **bot):
    store = Store(tmp_path / "owls.db")
    owl = store.save_bot(OWNER, BotInput(name="Γλαύκα", purpose="Research", **bot))
    chat = store.create_chat(OWNER, owl["id"], "Chat")
    store.enqueue(OWNER, chat["id"], TaskInput(message=message, request_id=uuid4().hex))
    return store, store.claim(OWNER)


def research_call(*questions, links=None):
    return ToolCall(
        "research",
        RESEARCH_TOOL,
        {"questions": [{"question": q, **({"links": links} if links else {})} for q in questions]},
    )


def run(store, task, model, limits=None):
    runtime = BotRuntime(store, OWNER, lambda _: model, limits=limits or RuntimeLimits())
    runtime.reply_review = None
    runtime.run(task, EventCancellationToken(threading.Event()))
    return runtime, store.task(OWNER, task["id"])


def research_result(model, index=1):
    """The research_helpers result the owl's ``index``-th request carries."""
    (result,) = [
        m
        for m in model.owl_requests[index]["messages"]
        if m["role"] == "tool" and m["tool_call_id"] == "research"
    ]
    return json.loads(result["content"])


def events(final, kind):
    return [e["data"] for e in final["events"] if e["kind"] == kind]


def eventually(check, seconds=10.0):
    until = time.monotonic() + seconds
    while time.monotonic() < until:
        if check():
            return True
        time.sleep(0.02)
    return check()


# ---------------------------------------------------------------------------------------------
# Working at once, with links


def test_three_helpers_work_at_once_and_bring_back_facts_with_their_links(tmp_path, web):
    together = threading.Barrier(MAX_HELPERS, timeout=10)

    def helper(question, steps, seen, kwargs):
        n = QUESTIONS.index(question) + 1
        if steps == 0:
            # Each helper's first call waits for the other two: one at a time never gets past.
            together.wait()
            return response(calls=[ToolCall(f"s{n}", "web_search", {"query": f"topic {n}"})])
        if steps == 1:
            return response(calls=[ToolCall(f"f{n}", "web_fetch", {"url": page(n)})])
        return report(f"Answer {n}", [(f"Fact {n}", page(n))])

    store, task = owl_task(tmp_path)
    model = Model([response(calls=[research_call(*QUESTIONS)]), response("Merged.")], helper)
    _, final = run(store, task, model)

    assert final["status"] == "completed" and final["output"] == "Merged."
    assert RESEARCH_TOOL in model.owl_requests[0]["tools"]
    result = research_result(model)
    assert result["complete"] is True and "note" not in result
    for n, (question, helper_report) in enumerate(
        zip(QUESTIONS, result["helpers"], strict=True), start=1
    ):
        assert helper_report["question"] == question and helper_report["state"] == "answered"
        assert helper_report["answer"] == f"Answer {n}"
        assert helper_report["facts"] == [{"fact": f"Fact {n}", "link": page(n), "opened": True}]
        assert helper_report["opened_links"] == [page(n)]
        assert helper_report["searches"] == [f"topic {n}"]
    assert result["opened_links"] == [page(n) for n in (1, 2, 3)]
    # Three threads of their own, none of them the turn's.
    assert len({r["thread"] for r in model.helper_requests}) == MAX_HELPERS
    # Each page was read at the helpers' page size, through the owl's own web tools.
    assert sorted(web["reads"]) == [(page(n), HELPER_PAGE_CHARS) for n in (1, 2, 3)]
    # Every helper call is one of the task's own calls, in the same ledger.
    assert final["calls"] == len(model.owl_requests) + len(model.helper_requests) == 11
    assert len(final["usage"]) == 11
    assert all(u["status"] == "completed" for u in final["usage"])
    # The steps show the helpers, what each looked into and how each ended.
    (started,) = events(final, "research_helpers_started")
    assert started == {
        "count": 3,
        "helpers": [{"helper": n, "question": q} for n, q in enumerate(QUESTIONS, start=1)],
    }
    steps = events(final, "research_helper_step")
    assert {(s["helper"], s["name"], s.get("topic"), s.get("host")) for s in steps} == {
        *((n, "web_search", f"topic {n}", None) for n in (1, 2, 3)),
        *((n, "web_fetch", None, "pages.example") for n in (1, 2, 3)),
    }
    finished = sorted(events(final, "research_helper_finished"), key=lambda e: e["helper"])
    assert finished == [
        {"helper": n, "state": "answered", "facts": 1, "opened": 1} for n in (1, 2, 3)
    ]
    # The helpers' own web steps are not the owl's: its live step stays the research call.
    assert [e["name"] for e in events(final, "tool_started")] == [RESEARCH_TOOL]


def test_a_link_the_helper_did_not_read_is_not_counted_as_opened(tmp_path, web):
    def helper(question, steps, seen, kwargs):
        if steps == 0:
            return response(calls=[ToolCall("s", "web_search", {"query": "ticket price"})])
        if steps == 1:
            return response(calls=[ToolCall("f", "web_fetch", {"url": page(1) + "/"})])
        # Page 2 only appeared in the search results; the answer cites page 3 in its text.
        return report(
            f"12 EUR, see {page(3)}.", [("Ticket 12 EUR", page(1)), ("Free on Sundays", page(2))]
        )

    store, task = owl_task(tmp_path)
    model = Model(
        [response(calls=[research_call(QUESTIONS[0], links=[page(9)])]), response("Done.")],
        helper,
    )
    run(store, task, model)

    result = research_result(model)
    (helper_report,) = result["helpers"]
    # The page was read by another spelling of the same address.
    assert helper_report["facts"] == [
        {"fact": "Ticket 12 EUR", "link": page(1), "opened": True},
        {"fact": "Free on Sundays", "link": page(2), "opened": False},
    ]
    assert helper_report["opened_links"] == [page(1) + "/"]
    assert helper_report["unopened_links"] == [page(2), page(3)]
    assert result["opened_links"] == [page(1) + "/"]
    assert "did not read" in result["note"]
    # The seed link was offered to start from; it was never read.
    assert json.loads(model.helper_requests[0]["messages"][1]["content"])["start_from"] == [page(9)]
    assert page(9) not in [url for url, _ in web["reads"]]

    # Compacted into a progress record, the step's receipt lists only the page it read.
    messages = [
        *[m for m in model.owl_requests[1]["messages"] if m["role"] != "system"],
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "next",
                    "type": "function",
                    "function": {"name": "web_fetch", "arguments": encode({"url": page(4)})},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "next", "content": encode({"text": "More."})},
    ]

    class Summariser:
        model = "summary-fixture"

        def chat(self, **kwargs):
            return response(json.dumps({"goal": "Prices", "work_done": []}))

    # The next message's turn compacts them (a finished task makes no more calls).
    store.enqueue(OWNER, task["chat_id"], TaskInput(message="More.", request_id=uuid4().hex))
    made = TurnProgress(store, OWNER, TaskResults(store, OWNER)).compact(
        store.claim(OWNER),
        messages,
        Summariser(),
        EventCancellationToken(threading.Event()),
        RuntimeLimits(),
        estimate_request_tokens,
        lambda exc, attempts, token: provider_retry_wait(exc, attempts, token),
    )
    (receipt,) = made["message"][TURN_PROGRESS_KEY]["steps"][0]["receipts"]
    assert receipt["tool"] == RESEARCH_TOOL
    assert receipt["result"]["opened_links"] == [page(1) + "/"]
    assert "links" not in receipt
    shown = encode(receipt)
    assert page(2) not in shown and page(3) not in shown and page(9) not in shown
    # A result moved to storage keeps the same receipt.
    assert result_receipt(encode(result))["opened_links"] == [page(1) + "/"]


# ---------------------------------------------------------------------------------------------
# Language


def test_each_helper_reports_in_the_language_of_its_question(tmp_path, web):
    greek, chinese = QUESTIONS[1], QUESTIONS[2]
    answers = {
        greek: ("Ανοιχτό 9:00-17:00 τις Κυριακές.", "Κυριακή: 9:00-17:00", "Ώρες αργιών"),
        chinese: ("周一闭馆。", "每周一闭馆", "节假日开放时间"),
    }

    def helper(question, steps, seen, kwargs):
        if steps == 0:
            return response(calls=[ToolCall("f", "web_fetch", {"url": page(1)})])
        answer, fact, missing = answers[question]
        return report(answer, [(fact, page(1))], not_found=[missing])

    store, task = owl_task(tmp_path, message="Πότε είναι ανοιχτό; 什么时候开放？")
    model = Model([response(calls=[research_call(greek, chinese)]), response("OK")], helper)
    _, final = run(store, task, model)

    for question in (greek, chinese):
        (first, *_) = model.helper_steps(question)
        # Each helper gets its question as written and is told to answer in its language.
        assert json.loads(first["messages"][1]["content"])["question"] == question
        assert "in the language the question is written in" in first["messages"][0]["content"]
    by_question = {h["question"]: h for h in research_result(model)["helpers"]}
    for question, (answer, fact, missing) in answers.items():
        assert by_question[question]["answer"] == answer
        assert by_question[question]["facts"][0]["fact"] == fact
        assert by_question[question]["not_found"] == [missing]
    # The owl reads them in their own script, not escaped.
    carried = next(m["content"] for m in model.owl_requests[1]["messages"] if m.get("tool_call_id"))
    assert answers[greek][0] in carried and answers[chinese][0] in carried
    (started,) = events(final, "research_helpers_started")
    assert [h["question"] for h in started["helpers"]] == [greek, chinese]


# ---------------------------------------------------------------------------------------------
# Stop and allowances


def test_stop_cancels_running_helpers(tmp_path, web):
    inside, entered = threading.Semaphore(0), []

    def helper(question, steps, seen, kwargs):
        token = kwargs["cancellation_token"]
        entered.append(question)
        inside.release()
        while not token.is_cancelled:
            time.sleep(0.01)
        entered.remove(question)
        raise CooperativeCancellationError()

    store, task = owl_task(tmp_path)
    model = Model([response(calls=[research_call(*QUESTIONS)])], helper)
    runtime = BotRuntime(store, OWNER, lambda _: model)
    runtime.reply_review = None
    turn = threading.Thread(target=runtime.run, args=(task, runtime.task_token(task["id"])))
    turn.start()
    for _ in QUESTIONS:
        assert inside.acquire(timeout=10)
    runtime.cancel(task["id"])
    turn.join(10)
    assert not turn.is_alive()

    final = store.task(OWNER, task["id"])
    assert final["status"] == "cancelled"
    assert eventually(lambda: not entered)
    # No helper made another call, and none of their calls is left open in the ledger.
    assert len(model.helper_requests) == len(QUESTIONS) and len(model.owl_requests) == 1
    assert eventually(
        lambda: all(u["status"] != "started" for u in store.task(OWNER, task["id"])["usage"])
    )
    assert eventually(
        lambda: (
            len(events(store.task(OWNER, task["id"]), "research_helper_finished")) == len(QUESTIONS)
        )
    )
    finished = events(store.task(OWNER, task["id"]), "research_helper_finished")
    assert {e["state"] for e in finished} == {"stopped"}


@pytest.mark.parametrize(
    "limits",
    [RuntimeLimits(max_calls=6), RuntimeLimits(calls_per_task=6)],
    ids=["workspace allowance", "task ceiling"],
)
def test_an_allowance_used_up_midway_stops_helpers_with_a_plain_partial_result(
    tmp_path, web, limits
):
    together = threading.Barrier(MAX_HELPERS, timeout=10)

    def helper(question, steps, seen, kwargs):
        together.wait()
        n = QUESTIONS.index(question) + 1
        return response(calls=[ToolCall(f"s{n}", "web_search", {"query": f"topic {n}"})])

    store, task = owl_task(tmp_path)
    model = Model([response(calls=[research_call(*QUESTIONS)]), response("Partial.")], helper)
    _, final = run(store, task, model, limits)

    # One owl call and one per helper leave exactly the calls kept for the owl's answer.
    assert len(model.helper_requests) == 3
    assert final["status"] == "completed" and final["output"] == "Partial."
    assert final["calls"] == 5 <= 6
    result = research_result(model)
    assert result["complete"] is False
    assert "allowance ran out" in result["note"] and "partial" in result["note"]
    for n, helper_report in enumerate(result["helpers"], start=1):
        assert helper_report["state"] == "stopped"
        assert helper_report["note"] == "It stopped when this task's model allowance ran out."
        assert helper_report["searches"] == [f"topic {n}"]
    finished = events(final, "research_helper_finished")
    assert {(e["state"], e["reason"]) for e in finished} == {("stopped", "allowance")}


def test_a_usage_limit_from_the_provider_stops_every_helper(tmp_path, web):
    def helper(question, steps, seen, kwargs):
        error = LLMError("The account's quota is used up.")
        error.provider_error_code = "insufficient_quota"
        raise error

    store, task = owl_task(tmp_path)
    model = Model([response(calls=[research_call(*QUESTIONS)]), response("Said so.")], helper)
    _, final = run(store, task, model)

    result = research_result(model)
    assert result["complete"] is False and "allowance ran out" in result["note"]
    assert {h["state"] for h in result["helpers"]} == {"stopped"}
    # Each helper tried once; nothing was sent again into a used-up account.
    assert len(model.helper_requests) <= len(QUESTIONS)
    assert all(u["status"] in ("completed", "unknown") for u in final["usage"])


def test_no_helper_starts_without_a_call_to_spare(tmp_path, web):
    store, task = owl_task(tmp_path)
    model = Model(
        [response(calls=[research_call(*QUESTIONS)]), response("Answered.")],
        lambda *args: pytest.fail("no helper may start"),
    )
    _, final = run(store, task, model, RuntimeLimits(calls_per_task=1 + KEEP_FOR_OWL))

    result = research_result(model)
    assert result["complete"] is False and "No helper started" in result["note"]
    assert {h["state"] for h in result["helpers"]} == {"not_started"}
    assert final["status"] == "completed" and not events(final, "research_helpers_started")


def test_a_helper_out_of_time_reports_and_one_still_running_is_stopped(tmp_path, web, monkeypatch):
    monkeypatch.setattr(research_helpers, "HELPER_SECONDS", 0.0)
    monkeypatch.setattr(research_helpers, "BATCH_SECONDS", 1.0)
    running = []

    def helper(question, steps, seen, kwargs):
        if question == QUESTIONS[0]:
            return report("Γρήγορη απάντηση.", [])
        token = kwargs["cancellation_token"]
        running.append(question)
        while not token.is_cancelled:
            time.sleep(0.01)
        running.remove(question)
        raise CooperativeCancellationError()

    store, task = owl_task(tmp_path)
    model = Model(
        [response(calls=[research_call(*QUESTIONS[:2])]), response("Μερική απάντηση.")], helper
    )
    _, final = run(store, task, model)

    assert final["status"] == "completed" and final["output"] == "Μερική απάντηση."
    # Out of working time, a helper's call is offered no tools and asks for its report.
    (quick,) = model.helper_steps(QUESTIONS[0])
    assert quick["tools"] == [] and quick["messages"][-1]["content"] == REPORT_NOW
    result = research_result(model)
    assert result["complete"] is False and "Not every helper finished" in result["note"]
    answered, stopped = result["helpers"]
    assert answered["state"] == "answered" and answered["answer"] == "Γρήγορη απάντηση."
    assert stopped["state"] == "stopped"
    assert stopped["note"] == "It stopped when its working time ran out."
    assert eventually(lambda: not running)


# ---------------------------------------------------------------------------------------------
# What a helper can do


def test_helpers_can_only_search_and_read_pages(tmp_path, web, monkeypatch):
    run_tools = []
    real_tool = BotRuntime._tool

    def spy(self, task, name, arguments, allowed, **kwargs):
        run_tools.append(name)
        return real_tool(self, task, name, arguments, allowed, **kwargs)

    monkeypatch.setattr(BotRuntime, "_tool", spy)
    elsewhere = [
        ("save_file", {"name": "notes.md", "content": "x"}),
        ("browser_navigate", {"url": page(1)}),
        ("local_command", {"cwd": "/", "command": "ls"}),
        ("read_memory", {"memory_id": "m1"}),
        ("list_files", {}),
        (RESEARCH_TOOL, {"questions": [{"question": "more"}]}),
    ]

    def helper(question, steps, seen, kwargs):
        if steps == 0:
            return response(
                calls=[ToolCall(f"x{n}", name, args) for n, (name, args) in enumerate(elsewhere)]
            )
        if steps == 1:
            return response(calls=[ToolCall("f", "web_fetch", {"url": page(1)})])
        return report("Found.", [("Fact", page(1))])

    store, task = owl_task(tmp_path, message="Ψάξε τις τιμές, σε παρακαλώ.")
    model = Model([response(calls=[research_call(QUESTIONS[0])]), response("OK")], helper)
    _, final = run(store, task, model)

    # Offered only the two web tools, the helper's other calls were refused unrun.
    assert [r["tools"] for r in model.helper_requests] == [["web_search", "web_fetch"]] * 3
    refusals = [
        json.loads(m["content"])
        for m in model.helper_requests[1]["messages"]
        if m["role"] == "tool"
    ]
    assert len(refusals) == len(elsewhere)
    assert all(r["executed"] is False and "only web_search" in r["error"] for r in refusals)
    assert run_tools.count("web_fetch") == 1 and set(run_tools) == {"web_fetch"}
    assert store.list_files(OWNER, task["bot_id"]) == []
    assert final["calls"] == len(model.owl_requests) + len(model.helper_requests)
    # A helper sees its question, never the owl's conversation or guidance.
    for request in model.helper_requests:
        shown = encode(request["messages"])
        assert "Ψάξε τις τιμές" not in shown and "## Who you are" not in shown
    assert research_result(model)["helpers"][0]["state"] == "answered"


def test_a_helpers_requests_fit_the_turns_context_allowance(tmp_path, web):
    """Pages that no longer fit leave the request oldest first; the facts stay sourced."""
    for n in range(1, 9):
        web["texts"][page(n)] = f"Page {n} " + "word " * 3000
    limits = RuntimeLimits(context_tokens=9000)

    def helper(question, steps, seen, kwargs):
        if steps < 2:
            first = 4 * steps + 1
            return response(
                calls=[
                    ToolCall(f"f{n}", "web_fetch", {"url": page(n)})
                    for n in range(first, first + 4)
                ]
            )
        return report("Read them.", [("Fact", page(8))])

    store, task = owl_task(tmp_path)
    model = Model([], helper)
    runtime = BotRuntime(store, OWNER, lambda _: model, limits=limits)
    result = runtime.research.run(
        task,
        ResearchRequest.model_validate({"questions": [{"question": QUESTIONS[0]}]}),
        model,
        EventCancellationToken(threading.Event()),
    )

    assert result["complete"] is True
    assert result["helpers"][0]["facts"] == [{"fact": "Fact", "link": page(8), "opened": True}]
    assert result["helpers"][0]["opened_links"] == [page(n) for n in range(1, 9)]
    last = model.helper_requests[-1]
    assert estimate_request_tokens(last["messages"], last["schemas"]) <= limits.context_tokens
    pages = [m for m in last["messages"] if m["role"] == "tool"]
    left_out = [json.loads(m["content"]) for m in pages if "left out" in m["content"]]
    assert left_out and left_out[0]["url"] == page(1)
    assert f"Page 8 {'word ' * 10}" in pages[-1]["content"]


# ---------------------------------------------------------------------------------------------
# When it is offered


def test_offered_only_with_web_tools_and_a_model_that_calls_tools():
    def offered(**turn):
        return RESEARCH_TOOL in tool_names(Turn(**{"answers_person": True, **turn}))

    assert offered(web_lookup=True, tool_calling=True)
    assert not offered(web_lookup=False, tool_calling=True)
    assert not offered(web_lookup=True, tool_calling=False)
    # Background work and an activation's review start no other work.
    assert not offered(web_lookup=True, tool_calling=True, background=True, answers_person=False)
    assert not offered(web_lookup=True, tool_calling=True, activation=True, answers_person=False)
    # A routine's run and a room turn look things up like a chat turn.
    assert offered(web_lookup=True, tool_calling=True, scheduled=True, answers_person=False)
    assert offered(web_lookup=True, tool_calling=True, room=True)


@pytest.mark.parametrize(
    ("web_tools", "tool_calling", "expected"),
    [(True, True, True), (False, True, False), (True, False, False)],
)
def test_a_turn_offers_it_from_its_service_and_its_models_declared_capability(
    tmp_path, monkeypatch, web_tools, tool_calling, expected
):
    monkeypatch.setattr("alysis_code.bots.runtime.web_tools_available", lambda: web_tools)
    store, task = owl_task(tmp_path, message="Βρες μου τιμές εισιτηρίων.")
    model = Model([response("Αμέσως.")], None)
    model.supports_tool_calling = tool_calling
    run(store, task, model)
    assert (RESEARCH_TOOL in model.owl_requests[0]["tools"]) is expected
    assert ("web_search" in model.owl_requests[0]["tools"]) is web_tools


# ---------------------------------------------------------------------------------------------
# Two models with different declared capabilities

ENDPOINT = "https://api.z.ai/api/coding/paas/v4"
# The registry declares reasoning and a large context and reply for this one; nothing for the
# other, which gets the defaults.
REASONING = "glm-5.3-flash"
PLACE = "place-model-unknown-to-the-registry"


def connection(model, *, default=False):
    settings = SimpleNamespace(
        model=model, base_url=ENDPOINT, protocol="openai_compat", provider_key=None
    )
    settings.output_tokens = None
    return ModelConnection(model, model, settings, default=default)


def test_helpers_think_with_the_owls_own_model_and_its_declared_capacities(tmp_path, web):
    def helper(question, steps, seen, kwargs):
        if steps == 0:
            return response(calls=[ToolCall("f", "web_fetch", {"url": page(1)})])
        return report("Found.", [("Fact", page(1))])

    store = Store(tmp_path / "owls.db")
    models = {}

    def client(model):
        models.setdefault(
            model,
            Model(
                [response(calls=[research_call(*QUESTIONS[:2])]), response("Merged.")],
                helper,
                model=model,
            ),
        )
        return models[model]

    runtime = BotRuntime(store, OWNER, lambda _task: client(PLACE))
    connections = [connection(PLACE, default=True), connection(REASONING)]
    runtime.models = PlaceModels(lambda: connections, lambda _task, s: client(s.model))
    runtime.reply_review = None
    for name, picked in (("Σκέψη", REASONING), ("Τόπος", None)):
        owl = store.save_bot(OWNER, BotCreateInput(name=name, model=picked))
        chat = store.create_chat(OWNER, owl["id"], "Chat")
        store.enqueue(OWNER, chat["id"], TaskInput(message="Ψάξε.", request_id=uuid4().hex))
        task = store.claim(OWNER)
        runtime.run(task, EventCancellationToken(threading.Event()))
        assert store.task(OWNER, task["id"])["status"] == "completed"

    fitted = RuntimeLimits.for_model(model=REASONING, base_url=ENDPOINT)
    place = RuntimeLimits()
    assert fitted.output_tokens != place.output_tokens
    for model, output_tokens, effort in (
        (REASONING, fitted.output_tokens, "medium"),
        (PLACE, place.output_tokens, None),
    ):
        requests = models[model].helper_requests
        assert len(requests) == 4  # two helpers, two calls each, on this owl's model only
        assert {r["max_tokens"] for r in requests} == {output_tokens}
        # At work strength: the work effort for the model that declares reasoning, none else.
        assert {r["effort"] for r in requests} == {effort}
        assert {r["effort"] for r in models[model].owl_requests} == {effort}
        assert research_result(models[model])["complete"] is True
