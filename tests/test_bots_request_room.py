"""An owl's requests fit the model's real request bound, and a refusal as too long makes room.

Scripted models and pages, no network. A route that admits a request by its size on the wire
reports that size through the client's own ``request_capacity_ratio``: the real compatible
client on the hosted route, or a scripted model that counts a request's JSON bytes as a hosted
gateway does. Pages in Greek and Chinese, whose bytes outgrow their token estimate; models with
different context windows.
"""

from __future__ import annotations

import copy
import json
import threading
from uuid import uuid4

import httpx
import pytest
from test_bots_browser_runtime import ClientFixture
from test_bots_desktop_runtime import study as execution_study
from test_bots_research_helpers import QUESTIONS, page, report
from test_bots_research_helpers import Model as Researcher
from test_bots_research_helpers import response as helper_response
from test_bots_research_helpers import web as research_web

import alysis_code.bots.runtime as runtime_module
from alysis_code.bots import failures, web_lookup
from alysis_code.bots.background import BackgroundStore
from alysis_code.bots.context_summary import TURN_PROGRESS_INSTRUCTIONS
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_tools import tool_schema
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview
from alysis_code.bots.reply_style import ACK_REMINDER, MILESTONE_REMINDER, PLAN_REMINDER
from alysis_code.bots.research_helpers import HELPER_PAGE_CHARS, ResearchRequest
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.scheduling import ScheduleInput
from alysis_code.bots.store import Store, encode
from alysis_code.bots.task_results import ReadToolResult, TaskResults
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.hosted_request_limits import validate_hosted_chat_capacity
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.provider_limits import ProviderRetrySettings
from alysis_code.llm.types import LLMError, LLMResponse, LLMUsage, ToolCall
from alysis_code.request_estimation import estimate_request_tokens

study = execution_study
web = research_web

OWNER = "alice"
PLAIN_FAILURE = (
    "The model or runtime could not finish this task. Check the model connection and try again. "
    "Saved results remain available."
)
HOSTED = "https://gateway.example.invalid/v1"
# On the route whose gateway admits a request by its size: models with a 262,144 and a
# 1,000,000 input bound, and different declared context windows.
NARROW, WIDE = "nemotron-3.5-super-vl", "glm-5.3-flash"
REMINDERS = {ACK_REMINDER, PLAN_REMINDER, MILESTONE_REMINDER}
# A progress record's summary, as a model writes one when a turn's steps are compacted.
SUMMARY = {
    "goal": "Σύγκριση τιμών 比较价格",
    "constraints": [],
    "decisions": [],
    "work_done": [{"summary": "Διάβασα τις σελίδες", "results": "Τιμές 价格"}],
    "open_threads": [],
    "next_steps": ["Απάντηση"],
}


def link(number):
    return f"https://pages.example/{number:02d}"


def greek(number, characters):
    sentence = f"Σελίδα {number}: το εισιτήριο για ενήλικες κοστίζει {number + 10} ευρώ. "
    return (sentence * (characters // len(sentence) + 1))[:characters]


def chinese(number, characters):
    sentence = f"第{number}页：成人门票价格为{number + 10}欧元，周一闭馆。"
    return (sentence * (characters // len(sentence) + 1))[:characters]


def mixed(number, characters):
    return (greek if number % 2 else chinese)(number, characters)


def response(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


def read(number):
    return [ToolCall(f"read-{number}", "web_fetch", {"url": link(number)})]


def provider_refusal():
    """A provider's own refusal of a request that is too long, as the adapter reports it."""
    return OpenAICompatClient._error_from_response(
        httpx.Response(
            400,
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "message": "The input is longer than this model's context.",
                }
            },
        )
    )


def gateway_refusal(characters=600_000):
    """The hosted gateway's admission check as the client runs it before sending: over the
    input bound, or (with enough characters) over the body size."""
    payload = {"model": WIDE, "messages": [{"role": "user", "content": "λ" * characters}]}
    with pytest.raises(LLMError) as raised:
        validate_hosted_chat_capacity(payload)
    return raised.value


def wire_share(messages, tools, bound):
    """A request's share of a wire bound, counted as a hosted gateway counts: JSON bytes, twice."""
    body = {"messages": messages, "tools": tools or []}
    return len(json.dumps(body, ensure_ascii=False).encode("utf-8")) * 2 / bound


class Model:
    """A scripted model: each step request takes the next item of ``script`` (tool calls to
    make, an exception to raise, or the answer's text); a reply check takes the next of
    ``reviews``. ``bound``: its route's wire bound in bytes; None for a route without one."""

    supports_tool_calling = True

    def __init__(self, name, script, *, reviews=(), bound=None):
        self.model = name
        self.script, self.reviews, self.bound = list(script), list(reviews), bound
        self.requests, self.review_requests, self.compactions = [], [], []

    def request_capacity_ratio(self, *, messages, tools=None):
        return 0.0 if self.bound is None else wire_share(messages, tools, self.bound)

    def chat(self, *, messages, tools=None, **kwargs):
        sent = json.loads(json.dumps(messages))
        if sent[0]["content"] == TURN_PROGRESS_INSTRUCTIONS:
            self.compactions.append(sent)
            return response(json.dumps(SUMMARY, ensure_ascii=False))
        if sent[0]["content"] == REVIEW_INSTRUCTIONS:
            self.review_requests.append(sent)
            item = self.reviews.pop(0)
        else:
            self.requests.append({"messages": sent, "tools": tools})
            item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        if isinstance(item, str):
            return response(item)
        return response(calls=item)


@pytest.fixture
def pages(monkeypatch):
    texts = {}
    monkeypatch.setattr(web_lookup, "web_tools_available", lambda: True)
    monkeypatch.setattr(runtime_module, "web_tools_available", lambda: True)
    monkeypatch.setattr(
        runtime_module,
        "web_fetch",
        lambda url, chars: {"url": url, "title": "Page " + url[-2:], "text": texts[url]},
    )
    return texts


def owl(tmp_path, message="Σύγκρινε τις τιμές των εισιτηρίων. 比较门票价格。"):
    store = Store(tmp_path / "owls.db")
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα", purpose="Research"))
    chat = store.create_chat(OWNER, bot["id"], "Chat")
    store.enqueue(OWNER, chat["id"], TaskInput(message=message, request_id=uuid4().hex))
    return store, bot


def start(store, bot, kind):
    """The turn to run, and whether it runs in the background: a person's message, its work
    carried on in the background, or a routine's run."""
    task = store.claim(OWNER)
    if kind == "background":
        background = BackgroundStore(store, OWNER)
        background.submit(task, "Comparing prices", [{"role": "user", "content": task["message"]}])
        return background.claim(), True
    if kind == "routine":
        store.finish(OWNER, task["id"], "completed", content="Ok.")
        store.save_schedule(
            OWNER,
            bot["id"],
            ScheduleInput.model_validate(
                {
                    "name": "Τιμές",
                    "prompt": "Σύγκρινε τις τιμές των εισιτηρίων. 比较门票价格。",
                    "timing": {"kind": "interval", "every_minutes": 5},
                }
            ),
            now=1000,
        )
        assert store.enqueue_due_schedules(OWNER, max_calls=None, now=1400) == 1
        return store.claim(OWNER), False
    return task, False


def run(store, bot, model, limits, *, kind="chat", review=False):
    runtime = BotRuntime(store, OWNER, lambda _: model, limits=limits)
    runtime.reply_review = ReplyReview(runtime) if review else None
    task, background = start(store, bot, kind)
    runtime.run(task, EventCancellationToken(threading.Event()), background=background)
    return store.task(OWNER, task["id"])


def refusals(task):
    return [
        e["data"]
        for e in task["events"]
        if e["kind"] == "context_window" and e["data"].get("reason") == "request_too_long"
    ]


def tokens(request):
    """A step request's token estimate, without the progress reminder sent beside it."""
    kept = [m for m in request["messages"] if m.get("content") not in REMINDERS]
    return estimate_request_tokens(kept, request["tools"])


def archived(messages):
    return [
        json.loads(m["content"])
        for m in messages
        if m["role"] == "tool" and '"stored_result_id"' in m["content"]
    ]


# ---------------------------------------------------------------------------------------------
# The hosted route: its gateway admits a request by its bytes, not by the token estimate.


class Gateway:
    """The hosted route's real client, with a scripted gateway behind it (no network)."""

    def __init__(self, model, script=()):
        self.script, self.bodies = list(script), []
        self.client = OpenAICompatClient(
            base_url=HOSTED,
            api_key="synthetic-test-only",
            model=model,
            provider_key="alysis",
            transport=httpx.MockTransport(self.respond),
            provider_retry_settings=ProviderRetrySettings(max_retries=0, disable_retries=True),
        )

    def respond(self, request):
        self.bodies.append(json.loads(request.content))
        item = self.script.pop(0)
        message = {"role": "assistant", "content": item if isinstance(item, str) else ""}
        if not isinstance(item, str):
            message["tool_calls"] = [
                {
                    "id": call.id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
                }
                for call in item
            ]
        finish = "stop" if isinstance(item, str) else "tool_calls"
        return httpx.Response(
            200,
            json={
                "choices": [{"message": message, "finish_reason": finish}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            },
        )


@pytest.mark.parametrize(("write", "count"), [(greek, 8), (chinese, 6)])
def test_a_long_research_turn_on_the_hosted_route_stays_within_its_gateways_bound(
    tmp_path, pages, write, count
):
    for n in range(1, count + 1):
        pages[link(n)] = write(n, 30_000)
    answer = "Οι τιμές συγκρίθηκαν. 价格已比较。"
    gateway = Gateway(WIDE, [*(read(n) for n in range(1, count + 1)), answer])
    limits = RuntimeLimits.for_model(model=WIDE, base_url=HOSTED)
    store, bot = owl(tmp_path)

    final = run(store, bot, gateway.client, limits)

    # Before, the client's own check refused the last request before sending it, and the
    # turn failed: the token estimate never saw what the gateway counts.
    assert final["status"] == "completed", final["error"]
    assert final["output"] == answer
    assert len(gateway.bodies) == count + 1
    last = gateway.bodies[-1]
    # The page read last is word for word; earlier ones wait in storage, read back exactly.
    newest = next(m for m in reversed(last["messages"]) if m["role"] == "tool")
    assert json.loads(newest["content"])["text"] == write(count, 30_000)
    stored = archived(last["messages"])
    assert stored and all(s["tool"] == "web_fetch" for s in stored)
    first = TaskResults(store, OWNER).read(
        final["id"], ReadToolResult(result_id=stored[0]["stored_result_id"])
    )
    assert first["characters"] > 30_000 and write(1, 30_000)[:1000] in first["content"]
    # Every page together is well within the token allowance: only the wire bound made room.
    every_page = estimate_request_tokens(
        [{"role": "tool", "content": text} for text in pages.values()], None
    )
    assert every_page + estimate_request_tokens(last["messages"], last["tools"]) < (
        limits.context_tokens
    )


# ---------------------------------------------------------------------------------------------
# Fitting one request

SCHEMAS = [tool_schema("web_fetch"), tool_schema("read_tool_result")]


def transcript(runtime, task, write, count, characters):
    messages = runtime._context(task, mark_history=True)
    for n in range(1, count + 1):
        text = write(n, characters)
        messages += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"read-{n}",
                        "type": "function",
                        "function": {"name": "web_fetch", "arguments": encode({"url": link(n)})},
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": f"read-{n}",
                "content": encode({"url": link(n), "title": f"Page {n}", "text": text}),
                "_tool_name": "web_fetch",
            },
        ]
    return messages


@pytest.mark.parametrize(
    ("model", "write", "characters"), [(NARROW, greek, 10_000), (WIDE, chinese, 30_000)]
)
def test_a_request_over_the_wire_margin_makes_room_though_its_tokens_fit(
    tmp_path, model, write, characters
):
    gateway = Gateway(model)
    limits = RuntimeLimits.for_model(model=model, base_url=HOSTED)
    store, _ = owl(tmp_path)
    runtime = BotRuntime(store, OWNER, lambda _: gateway.client, limits=limits)
    task = store.claim(OWNER)
    messages = transcript(runtime, task, write, 6, characters)

    # Fitted by tokens alone, everything stays, and the gateway would refuse it.
    unmeasured = runtime._fit_context(task, copy.deepcopy(messages), SCHEMAS)
    assert estimate_request_tokens(unmeasured, SCHEMAS) <= limits.context_tokens
    assert gateway.client.request_capacity_ratio(messages=unmeasured, tools=SCHEMAS) >= 1
    assert archived(unmeasured) == []

    room = runtime._request_room(task, gateway.client)
    wire = runtime._fit_context(task, copy.deepcopy(messages), SCHEMAS, room=room)

    assert gateway.client.request_capacity_ratio(messages=wire, tools=SCHEMAS) < 0.9
    stored = archived(wire)
    assert 0 < len(stored) < 6
    # Oldest first; the newest page stays word for word.
    assert link(1) in stored[0]["excerpt"]
    newest = next(m for m in reversed(wire) if m["role"] == "tool")
    assert json.loads(newest["content"])["text"] == write(6, characters)
    assert gateway.bodies == []  # Measured where it runs; nothing was sent.


class Unmeasured:
    """A client with no measure of its own (only tokens decide)."""

    model = "direct-route"


def same_storage(wire):
    """``wire`` with each stored result's id left out: every fit stores its own copy."""
    return [
        {**m, "content": encode({**json.loads(m["content"]), "stored_result_id": None})}
        if m["role"] == "tool" and '"stored_result_id"' in m["content"]
        else m
        for m in wire
    ]


@pytest.mark.parametrize("client", [Model("direct-route", []), Unmeasured()], ids=["zero", "none"])
def test_a_route_without_a_wire_bound_fits_by_tokens_as_before(tmp_path, client):
    store, _ = owl(tmp_path)
    runtime = BotRuntime(store, OWNER, lambda _: client, limits=RuntimeLimits())
    task = store.claim(OWNER)
    messages = transcript(runtime, task, mixed, 6, 6000)

    plain = runtime._fit_context(task, copy.deepcopy(messages), SCHEMAS)
    room = runtime._request_room(task, client)
    assert runtime._fit_context(task, copy.deepcopy(messages), SCHEMAS, room=room) == plain
    assert archived(plain) == []
    # With a smaller allowance, room is made by tokens alone, the same way.
    runtime.limits = RuntimeLimits(context_tokens=estimate_request_tokens(plain, SCHEMAS) - 2000)
    plain = runtime._fit_context(task, copy.deepcopy(messages), SCHEMAS)
    room = runtime._request_room(task, client)
    roomy = runtime._fit_context(task, copy.deepcopy(messages), SCHEMAS, room=room)
    assert archived(plain) and same_storage(roomy) == same_storage(plain)


# ---------------------------------------------------------------------------------------------
# A request refused as too long

# Two models with different context windows, each large enough for every page by tokens.
WINDOWS = {"smaller-window": 120_000, "larger-window": 400_000}
PAGE = 12_000


@pytest.mark.parametrize("kind", ["chat", "background", "routine"])
@pytest.mark.parametrize(
    ("refusal", "name"),
    [
        (provider_refusal, "smaller-window"),
        (provider_refusal, "larger-window"),
        (gateway_refusal, "smaller-window"),
        (lambda: gateway_refusal(4_500_000), "larger-window"),
    ],
    ids=["provider-smaller", "provider-larger", "gateway-input-smaller", "gateway-body-larger"],
)
def test_a_request_refused_as_too_long_makes_room_and_is_sent_again(
    tmp_path, pages, kind, refusal, name
):
    for n in range(1, 7):
        pages[link(n)] = mixed(n, PAGE)
    answer = "Οι τιμές συγκρίθηκαν. 价格已比较。"
    model = Model(name, [*(read(n) for n in range(1, 7)), refusal(), answer])
    store, bot = owl(tmp_path)

    final = run(store, bot, model, RuntimeLimits(context_tokens=WINDOWS[name]), kind=kind)

    assert final["status"] == "completed", final["error"]
    assert final["output"] == answer
    refused, sent = model.requests[-2], model.requests[-1]
    # The refused request held every page whole; the next fits within the smaller size, its
    # oldest pages moved to storage and its newest word for word.
    assert archived(refused["messages"]) == []
    assert tokens(sent) <= tokens(refused) * 0.7
    assert archived(sent["messages"])
    assert mixed(6, PAGE) in str(sent["messages"])
    (made,) = refusals(final)
    assert made["retry"] == 1 and made["message"]
    # Both attempts are the task's calls; the refused one stays charged as unknown.
    assert final["calls"] == len(model.requests)
    assert [c["status"] for c in final["usage"]].count("unknown") == 1
    assert "provider_retry" not in [e["kind"] for e in final["events"]]


def test_the_smaller_size_holds_for_the_rest_of_the_turn(tmp_path, pages):
    for n in range(1, 9):
        pages[link(n)] = mixed(n, PAGE)
    script = [*(read(n) for n in range(1, 5)), provider_refusal()]
    script += [*(read(n) for n in range(5, 9)), "完成。"]
    model = Model("smaller-window", script)
    store, bot = owl(tmp_path)

    final = run(store, bot, model, RuntimeLimits(context_tokens=WINDOWS["smaller-window"]))

    assert final["status"] == "completed", final["error"]
    ceiling = tokens(model.requests[4]) * 0.7
    # Pages read after the refusal never grow a request past what the refusal taught.
    assert len(model.requests) == 10
    assert all(tokens(request) <= ceiling for request in model.requests[5:])
    assert len(refusals(final)) == 1


def test_three_refusals_in_a_row_fail_the_turn_as_before(tmp_path, pages):
    # Enough earlier steps that both retries can shrink by the full margin: a request never
    # goes below its standing part (guidance and the owl's whole tool list) and newest step.
    for n in range(1, 10):
        pages[link(n)] = mixed(n, PAGE)
    script = [*(read(n) for n in range(1, 10)), *(provider_refusal() for _ in range(3)), "Late."]
    model = Model("larger-window", script)
    store, bot = owl(tmp_path)

    final = run(store, bot, model, RuntimeLimits(context_tokens=WINDOWS["larger-window"]))

    assert final["status"] == "failed"
    assert final["error"] == PLAIN_FAILURE
    # The kind of failure a refusal always gave.
    assert final["failure"] == failures.classify(provider_refusal(), account=False, transient=False)
    assert len(model.requests) == 9 + 3 and model.script == ["Late."]
    assert [r["retry"] for r in refusals(final)] == [1, 2]
    sizes = [tokens(r) for r in model.requests[-3:]]
    assert sizes[1] <= sizes[0] * 0.7 and sizes[2] <= sizes[1] * 0.7


def test_a_request_that_cannot_be_made_smaller_is_not_sent_again(tmp_path, pages):
    model = Model("smaller-window", [gateway_refusal(), "Never sent."])
    store, bot = owl(tmp_path, message="Γεια σου, τι κάνεις; 你好吗？")

    final = run(store, bot, model, RuntimeLimits(context_tokens=WINDOWS["smaller-window"]))

    assert final["status"] == "failed" and final["error"] == PLAIN_FAILURE
    assert final["failure"] == failures.classify(gateway_refusal(), account=False, transient=False)
    assert len(model.requests) == 1  # Nothing could go, so the refusal stands.


def test_a_reply_check_refused_as_too_long_makes_room_and_drafts_again(tmp_path, pages):
    for n in range(1, 7):
        pages[link(n)] = mixed(n, PAGE)
    ready = json.dumps({"decision": "ready", "reason": "Τα στοιχεία συμφωνούν."})
    answer = "Οι τιμές συγκρίθηκαν. 价格已比较。"
    model = Model(
        "larger-window",
        [*(read(n) for n in range(1, 7)), "Πρόχειρο.", answer],
        reviews=[provider_refusal(), ready],
    )
    store, bot = owl(tmp_path)

    final = run(
        store, bot, model, RuntimeLimits(context_tokens=WINDOWS["larger-window"]), review=True
    )

    assert final["status"] == "completed", final["error"]
    assert final["output"] == answer
    assert len(model.review_requests) == 2
    assert tokens(model.requests[-1]) <= tokens(model.requests[-2]) * 0.7
    assert len(refusals(final)) == 1


# ---------------------------------------------------------------------------------------------
# Work on the owl's computer


class MeasuredFixture(ClientFixture):
    def __init__(self, responses, bound=None):
        super().__init__(responses)
        self.bound = bound

    def request_capacity_ratio(self, *, messages, tools=None):
        return 0.0 if self.bound is None else wire_share(messages, tools, self.bound)


def steps(write, count, characters):
    messages = [{"role": "user", "content": "Σύγκρινε τις τιμές. 比较价格。"}]
    for n in range(1, count + 1):
        messages += [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"run-{n}",
                        "type": "function",
                        "function": {"name": "computer_run", "arguments": '{"command":"cat"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": f"run-{n}", "content": write(n, characters)},
        ]
    return messages


def test_computer_work_fits_the_wire_bound_and_makes_room_when_refused(study):
    from alysis_code.bots.execution import TaskClient

    store, task, manager, _ = study
    tools = [tool_schema("read_tool_result")]
    messages = steps(chinese, 6, 6000)
    done = response("完成。")
    client = MeasuredFixture([provider_refusal(), done])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    runtime.limits = RuntimeLimits(context_tokens=WINDOWS["larger-window"])
    # Every result fits by tokens; set the wire bound so that they do not fit on the wire.
    unmeasured = runtime._fit_context(task, copy.deepcopy(messages), tools)
    client.bound = int(wire_share(unmeasured, tools, 1) / 1.1)

    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]))
    result = adapter.chat(messages=copy.deepcopy(messages), tools=tools)

    assert result.content == "完成。"
    first, second = client.requests
    assert client.request_capacity_ratio(messages=first["messages"], tools=tools) < 0.9
    assert archived(first["messages"])
    # Refused anyway: the next request is smaller still, and the newest result stays.
    assert tokens(second) <= tokens(first) * 0.7
    assert chinese(6, 6000) in str(second["messages"])
    made = refusals(store.task("owner", task["id"]))
    assert [m["retry"] for m in made] == [1]


# ---------------------------------------------------------------------------------------------
# Research helpers


class MeasuredResearcher(Researcher):
    bound = None

    def request_capacity_ratio(self, *, messages, tools=None):
        return 0.0 if self.bound is None else wire_share(messages, tools, self.bound)


def research(tmp_path, model, question):
    store, _ = owl(tmp_path)
    task = store.claim(OWNER)
    runtime = BotRuntime(
        store, OWNER, lambda _: model, limits=RuntimeLimits(context_tokens=WINDOWS["larger-window"])
    )
    result = runtime.research.run(
        task,
        ResearchRequest.model_validate({"questions": [{"question": question}]}),
        model,
        EventCancellationToken(threading.Event()),
    )
    return store, task, result


def reading(refuse=None):
    """A helper that reads three pages a step for two steps, then reports."""

    def helper(question, steps, seen, kwargs):
        if steps < 2:
            first = 3 * steps + 1
            calls = [
                ToolCall(f"f{n}", "web_fetch", {"url": page(n)}) for n in range(first, first + 3)
            ]
            return helper_response(calls=calls)
        if refuse is not None and refuse(seen):
            raise provider_refusal()
        return report("Βρέθηκαν. 找到了。", [("Γεγονός 事实", page(6))])

    return helper


@pytest.mark.parametrize(("write", "question"), [(greek, QUESTIONS[1]), (chinese, QUESTIONS[2])])
def test_a_helpers_pages_leave_its_request_when_the_wire_bound_fills(
    tmp_path, web, write, question
):
    for n in range(1, 7):
        web["texts"][page(n)] = write(n, HELPER_PAGE_CHARS)
    model = MeasuredResearcher([], reading())
    # About four pages fit the bound; six do not. By tokens, all of them fit.
    model.bound = int(4.5 * 2 * len(write(1, HELPER_PAGE_CHARS).encode()))
    _, _, result = research(tmp_path, model, question)

    assert result["complete"] is True
    last = model.helper_requests[-1]
    assert model.request_capacity_ratio(messages=last["messages"], tools=last["schemas"]) < 0.9
    assert estimate_request_tokens(last["messages"], last["schemas"]) < WINDOWS["larger-window"]
    left_out = [m for m in last["messages"] if m["role"] == "tool" and "left out" in m["content"]]
    assert left_out and json.loads(left_out[0]["content"])["url"] == page(1)


def test_a_helper_refused_as_too_long_leaves_out_more_and_reports(tmp_path, web):
    for n in range(1, 7):
        web["texts"][page(n)] = greek(n, HELPER_PAGE_CHARS)
    refused = []

    def once(seen):
        refused.append(seen)
        return len(refused) == 1

    model = MeasuredResearcher([], reading(once))
    store, task, result = research(tmp_path, model, QUESTIONS[1])

    assert result["complete"] is True and result["helpers"][0]["state"] == "answered"
    before, after = refused
    assert estimate_request_tokens(after["messages"], after["schemas"]) <= 0.7 * (
        estimate_request_tokens(before["messages"], before["schemas"])
    )
    (made,) = refusals(store.task(OWNER, task["id"]))
    assert made["retry"] == 1 and made["helper"] == 1
