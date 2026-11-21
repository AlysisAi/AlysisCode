"""A long task keeps going when its own steps fill the owl's context.

Scripted provider and pages only. The fixtures check what the service keeps and sends,
not how good a model's summary is: the scripted summary deliberately leaves out every
link and id, so whatever survives was kept by the service itself.
"""

from __future__ import annotations

import copy
import json
import threading
import time
from dataclasses import replace
from uuid import uuid4

import pytest

import alysis_code.bots.runtime as runtime_module
from alysis_code.bots import web_lookup
from alysis_code.bots.context_summary import (
    TURN_PROGRESS_INSTRUCTIONS,
    TURN_PROGRESS_NOTE,
    progress_record,
)
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_review import REVIEW_INSTRUCTIONS, ReplyReview
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import LimitReached, Store, encode
from alysis_code.bots.task_results import ReadToolResult, TaskResults
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.types import LLMError, LLMResponse, LLMUsage, ToolCall
from alysis_code.request_estimation import estimate_request_tokens

PAGES = 40
CONTEXT_ALLOWANCE = "The task reached its context allowance."


def link(number):
    return f"https://pages.example/{number:03d}"


def response(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


def summary(words):
    """The engine compactor's summary shape, with no link or id in it."""
    return {
        "goal": words[0],
        "constraints": [],
        "decisions": [],
        "work_done": [{"summary": words[1], "results": words[2]}],
        "open_threads": [],
        "next_steps": [words[3]],
    }


CARD_AND_FILE = [
    [
        ToolCall(
            "card",
            "propose_action",
            {
                "question": "Publish the table?",
                "instructions": "Publish the finished table.",
                "offline_computer": False,
            },
        )
    ],
    [ToolCall("save", "save_file", {"name": "notes.md", "content": "Draft"})],
]


class Researcher:
    """Takes the ``before`` steps, reads the pages two at a time, then answers.

    A request carrying the progress instructions is a compaction request: it gets
    ``written`` back, after raising each of ``failures`` in turn (an exception, or a
    callable that returns one). A request carrying the reply checker's instructions
    gets ``judge``'s verdict. Every other request is a step of the task, and the last
    steps are ``drafts``, one per request.
    """

    model = "fixture"
    supports_tool_calling = True

    def __init__(
        self,
        pages,
        written,
        *,
        before=CARD_AND_FILE,
        failures=(),
        drafts=("Final answer.",),
        judge=None,
    ):
        self.script = [
            *before,
            *(
                [
                    ToolCall(f"read-{n}", "web_fetch", {"url": link(n)})
                    for n in range(first, min(first + 2, pages + 1))
                ]
                for first in range(1, pages + 1, 2)
            ),
        ]
        self.written, self.failures = written, list(failures)
        self.drafts, self.judge = list(drafts), judge
        self.steps, self.compactions, self.reviews = [], [], []

    def chat(self, *, messages, tools=None, **kwargs):
        sent = json.loads(json.dumps(messages))
        if sent[0]["content"] == TURN_PROGRESS_INSTRUCTIONS:
            assert tools is None
            self.compactions.append(sent)
            if self.failures:
                failure = self.failures.pop(0)
                raise failure() if callable(failure) else failure
            return response(json.dumps(self.written, ensure_ascii=False))
        if sent[0]["content"] == REVIEW_INSTRUCTIONS:
            self.reviews.append(sent)
            return response(json.dumps(self.judge(sent), ensure_ascii=False))
        self.steps.append({"messages": sent, "tools": tools})
        if self.script:
            return response(calls=self.script.pop(0))
        return response(self.drafts.pop(0) if len(self.drafts) > 1 else self.drafts[0])


@pytest.fixture
def pages(monkeypatch):
    """Each page is long enough to be moved to storage once the context fills."""
    texts = {}
    monkeypatch.setattr(web_lookup, "web_tools_available", lambda: True)
    monkeypatch.setattr("alysis_code.bots.runtime.web_tools_available", lambda: True)

    def fetch(url, chars):
        return {"url": url, "title": "Page " + url[-3:], "text": texts[url]}

    monkeypatch.setattr("alysis_code.bots.runtime.web_fetch", fetch)
    return texts


def owl_task(directory, request):
    directory.mkdir(parents=True, exist_ok=True)
    store = Store(directory / "owls.db")
    bot = store.save_bot("alice", BotInput(name="Mira", purpose="Research"))
    chat = store.create_chat("alice", bot["id"], "Chat")
    store.enqueue("alice", chat["id"], TaskInput(message=request, request_id=uuid4().hex))
    return store, store.claim("alice")


def run(store, task, model, limits):
    runtime = BotRuntime(store, "alice", lambda _: model, limits=limits)
    runtime.reply_review = None
    runtime.run(task, EventCancellationToken(threading.Event()))
    return store.task("alice", task["id"])


def first_request_tokens(directory, request, model_args):
    """What this owl's first request costs, with no step taken yet."""
    store, task = owl_task(directory, request)
    model = Researcher(0, *model_args, before=[])
    run(store, task, model, RuntimeLimits())
    first = model.steps[0]
    return estimate_request_tokens(first["messages"], first["tools"])


def assert_paired(messages):
    """Every call's results follow it at once, and every result answers a call."""
    waiting = set()
    for message in messages:
        if message["role"] == "tool":
            assert message["tool_call_id"] in waiting
            waiting.discard(message["tool_call_id"])
            continue
        assert not waiting
        if message["role"] == "assistant" and message.get("tool_calls"):
            waiting = {call["id"] for call in message["tool_calls"]}
    assert not waiting


def progress_records(messages):
    return [
        json.loads(m["content"].removeprefix(TURN_PROGRESS_NOTE))
        for m in messages
        if m["role"] == "user" and m["content"].startswith(TURN_PROGRESS_NOTE)
    ]


@pytest.mark.parametrize(("room", "compactions"), [(7000, 2), (14000, 1)])
def test_a_long_research_turn_compacts_its_finished_steps_and_finishes(
    tmp_path, pages, room, compactions
):
    for n in range(1, PAGES + 1):
        pages[link(n)] = f"Fact {n}: the value is {n * 7}. " * 120
    written = summary(["Compare values", "Read pages", "Values found", "Read the rest"])
    limit = first_request_tokens(tmp_path / "probe", "Compare the values", [written]) + room
    store, task = owl_task(tmp_path / "turn", "Compare the values")
    model = Researcher(PAGES, written)

    final = run(store, task, model, RuntimeLimits(context_tokens=limit))

    assert final["status"] == "completed", final["error"]
    assert final["output"] == "Final answer."
    # Without compaction this turn stops. A smaller room compacts more often.
    assert len(model.compactions) >= compactions
    assert final["calls"] == len(model.steps) + len(model.compactions)
    windows = [e for e in final["events"] if e["kind"] == "context_window"]
    assert len(windows) == len(model.compactions)
    for step in model.steps:
        assert estimate_request_tokens(step["messages"], step["tools"]) <= limit
        assert_paired(step["messages"])
    # Every compaction request fits the same model.
    for request in model.compactions:
        assert estimate_request_tokens(request, None) <= limit

    last = model.steps[-1]["messages"]
    shown = encode(last)
    (record,) = progress_records(last)
    assert record["summary"]["goal"] == "Compare values"
    # The open card, the saved file and every page read are still in front of the model.
    proposal = BotRuntime(store, "alice", lambda _: None).proposals.for_task(task)
    assert proposal["status"] == "pending" and proposal["id"] in shown
    (saved,) = store.list_files("alice", task["bot_id"])
    assert saved["id"] in shown
    assert all(link(n) in shown for n in range(1, PAGES + 1))
    receipts = [r for group in record["steps"] for r in group["receipts"]]
    assert {"id": proposal["id"], "state": "pending", "revision": 1} == {
        k: v
        for k, v in next(r for r in receipts if r["tool"] == "propose_action")["result"].items()
        if k in ("id", "state", "revision")
    }
    assert next(r for r in receipts if r["tool"] == "save_file")["result"]["id"] == saved["id"]
    # The exact steps are stored: each group's id reads back what those steps returned.
    results = TaskResults(store, "alice")
    for group in record["steps"]:
        stored = results.read(task["id"], ReadToolResult(result_id=group["stored_result_id"]))
        assert stored["tool"] == "task_steps" and stored["characters"] > 0
    first_page = next(r for r in receipts if r.get("arguments", {}).get("url") == link(1))
    page = results.read(
        task["id"],
        ReadToolResult(result_id=first_page["result"]["stored_result_id"], find="Fact 1:"),
    )
    assert page["content"].startswith("Fact 1: the value is 7.")


def test_compaction_requests_carry_the_conversations_own_language(tmp_path, pages):
    greek = "Σύγκρινε τις τιμές των εισιτηρίων στα μουσεία της Αθήνας."
    english = "Compare the ticket prices of the museums in Athens."
    greek_summary = summary(
        [
            "Σύγκριση τιμών εισιτηρίων",
            "Διάβασα τις σελίδες των μουσείων",
            "Το Μουσείο Ακρόπολης κοστίζει 20 ευρώ",
            "Διάβασε τις υπόλοιπες σελίδες",
        ]
    )
    english_summary = summary(
        ["Compare ticket prices", "Read museum pages", "Entry costs 20 euros", "Read the rest"]
    )
    for n in range(1, 13):
        pages[link(n)] = f"Μουσείο {n}: το εισιτήριο κοστίζει {n + 10} ευρώ. " * 120
    runs = {}
    for name, request, written in (
        ("greek", greek, greek_summary),
        ("english", english, english_summary),
    ):
        limit = first_request_tokens(tmp_path / name / "probe", request, [written]) + 5000
        store, task = owl_task(tmp_path / name / "turn", request)
        model = Researcher(12, written, before=[])
        final = run(store, task, model, RuntimeLimits(context_tokens=limit))
        assert final["status"] == "completed", final["error"]
        assert model.compactions
        runs[name] = model

    greek_request = runs["greek"].compactions[0]
    # The instructions are the same text whatever the language: the code adds nothing
    # that depends on it, and assumes no language of its own.
    assert greek_request[0]["content"] == runs["english"].compactions[0][0]["content"]
    assert "english" not in greek_request[0]["content"].casefold()
    # The person's words and the pages reach the model as written, not escaped.
    assert greek in greek_request[1]["content"]
    payload = json.loads(greek_request[1]["content"])
    assert payload["request"] == greek
    assert "Μουσείο 1: το εισιτήριο κοστίζει 11 ευρώ." in greek_request[1]["content"]
    # What the model wrote comes back in its own words, with only the fixed note added.
    after = next(
        step["messages"] for step in runs["greek"].steps if progress_records(step["messages"])
    )
    progress = next(m for m in after if m["content"].startswith(TURN_PROGRESS_NOTE))
    (record,) = progress_records(after)
    assert record["summary"]["goal"] == "Σύγκριση τιμών εισιτηρίων"
    assert "Το Μουσείο Ακρόπολης κοστίζει 20 ευρώ" in progress["content"]
    assert progress["content"] == TURN_PROGRESS_NOTE + encode(record)


def transcript(store, task, runtime, steps, page_text):
    """The task's context followed by ``steps`` single-call exchanges with long results."""
    messages = runtime._context(task, mark_history=True)
    for n in range(1, steps + 1):
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
                "content": encode({"url": link(n), "title": f"Page {n}", "text": page_text}),
                "_tool_name": "web_fetch",
            },
        ]
    return messages


class Compactor:
    model = "fixture"
    supports_tool_calling = True

    def __init__(self, answer):
        self.answer, self.requests = answer, []

    def chat(self, *, messages, tools=None, **kwargs):
        self.requests.append(messages)
        return response(self.answer)


def direct(tmp_path, answer, **limits):
    store, task = owl_task(tmp_path, "Compare the values")
    compactor = Compactor(answer)
    runtime = BotRuntime(store, "alice", lambda _: compactor, limits=RuntimeLimits(**limits))
    return store, task, runtime, compactor


def test_a_transcript_rebuilt_for_every_request_is_compacted_once(tmp_path):
    store, task, runtime, compactor = direct(tmp_path, encode(summary(["g", "s", "r", "n"])))
    messages = transcript(store, task, runtime, 6, "Value 7. " * 40)
    full = runtime._fit_context(task, copy.deepcopy(messages), [])
    runtime.limits = replace(runtime.limits, context_tokens=estimate_request_tokens(full, []) - 600)
    first = runtime._fit_context(task, copy.deepcopy(messages), [])
    assert len(compactor.requests) == 1
    assert estimate_request_tokens(first, []) <= runtime.limits.context_tokens
    assert_paired(first)
    # The newest step stays word for word, right after the record.
    said = [m for m in first if m["role"] != "system"]
    assert said[-3]["content"].startswith(TURN_PROGRESS_NOTE)
    assert said[-2]["tool_calls"][0]["id"] == "read-6" and said[-1]["tool_call_id"] == "read-6"
    # A caller that rebuilds its request from the same transcript gets the same record
    # back without paying for another summary.
    assert runtime._fit_context(task, copy.deepcopy(messages), []) == first
    assert len(compactor.requests) == 1
    assert store.task("alice", task["id"])["calls"] == 1


def test_a_turn_stops_when_even_its_compacted_turn_cannot_fit(tmp_path):
    store, task, runtime, compactor = direct(tmp_path, encode(summary(["g", "s", "r", "n"])))
    messages = transcript(store, task, runtime, 4, "Value 7. " * 40)
    newest_alone = runtime._fit_context(task, [*messages[:-8], *messages[-2:]], [])
    runtime.limits = replace(
        runtime.limits, context_tokens=estimate_request_tokens(newest_alone, []) - 1
    )
    with pytest.raises(LimitReached, match=CONTEXT_ALLOWANCE):
        runtime._fit_context(task, messages, [])
    # Nothing could make room, so no summary was paid for.
    assert compactor.requests == []
    assert store.task("alice", task["id"])["calls"] == 0


def test_an_unusable_summary_stops_the_task_and_keeps_its_steps(tmp_path):
    store, task, runtime, compactor = direct(tmp_path, "Here is a summary in prose.")
    messages = transcript(store, task, runtime, 6, "Value 7. " * 40)
    full = runtime._fit_context(task, copy.deepcopy(messages), [])
    runtime.limits = replace(runtime.limits, context_tokens=estimate_request_tokens(full, []) - 600)
    kept = copy.deepcopy(messages)
    with pytest.raises(LimitReached, match=CONTEXT_ALLOWANCE):
        runtime._fit_context(task, kept, [])
    assert len(compactor.requests) == 1
    assert [m.get("tool_call_id") for m in kept] == [m.get("tool_call_id") for m in messages]
    failed = [
        e for e in store.task("alice", task["id"])["events"] if e["kind"].startswith("context_")
    ]
    assert [e["kind"] for e in failed] == ["context_compaction_failed"]
    assert failed[0]["data"]["reason"] == "InvalidSummary"


def test_compaction_off_keeps_the_previous_stop(tmp_path):
    store, task, runtime, compactor = direct(
        tmp_path, encode(summary(["g", "s", "r", "n"])), compaction_enabled=False
    )
    messages = transcript(store, task, runtime, 6, "Value 7. " * 40)
    full = runtime._fit_context(task, copy.deepcopy(messages), [])
    runtime.limits = replace(runtime.limits, context_tokens=estimate_request_tokens(full, []) - 600)
    with pytest.raises(LimitReached, match=CONTEXT_ALLOWANCE):
        runtime._fit_context(task, messages, [])
    assert compactor.requests == []


# The summary call survives a provider hiccup the way the task's own steps do.


def provider_error(status):
    error = LLMError(f"LLM error {status}: upstream said no")
    error.provider_status_code = status
    return error


def short_turn(tmp_path, model):
    """A turn of twelve pages in a context that holds only some of them."""
    request = "Compare the values"
    limit = first_request_tokens(tmp_path / "probe", request, [model.written]) + 5000
    store, task = owl_task(tmp_path / "turn", request)
    runtime = BotRuntime(
        store, "alice", lambda _: model, limits=RuntimeLimits(context_tokens=limit)
    )
    runtime.reply_review = None
    return store, task, runtime


@pytest.fixture
def twelve_pages(pages):
    for n in range(1, 13):
        pages[link(n)] = f"Fact {n}: the value is {n * 7}. " * 120
    return pages


def test_a_summary_call_that_hits_a_passing_provider_error_is_sent_again(
    tmp_path, twelve_pages, monkeypatch
):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 0.0)
    written = summary(["Compare values", "Read pages", "Values found", "Read the rest"])
    model = Researcher(12, written, before=[], failures=[provider_error(503)])
    store, task, runtime = short_turn(tmp_path, model)

    runtime.run(task, EventCancellationToken(threading.Event()))

    final = store.task("alice", task["id"])
    assert final["status"] == "completed", final["error"]
    assert model.failures == [] and len(model.compactions) >= 2
    kinds = [e["kind"] for e in final["events"]]
    assert kinds.count("context_window") == len(model.compactions) - 1
    (retry,) = [e["data"] for e in final["events"] if e["kind"] == "provider_retry"]
    assert retry == {"exception_type": "LLMError", "wait_seconds": 0.0}
    # Both attempts are calls of this task: the failed one stays charged as unknown.
    assert final["calls"] == len(model.steps) + len(model.compactions)
    assert [c["status"] for c in final["usage"]].count("unknown") == 1
    # The same request went again.
    assert model.compactions[0] == model.compactions[1]


def test_a_summary_call_refused_by_the_provider_still_stops_the_task(tmp_path, twelve_pages):
    written = summary(["Compare values", "Read pages", "Values found", "Read the rest"])
    model = Researcher(12, written, before=[], failures=[provider_error(400)])
    store, task, runtime = short_turn(tmp_path, model)

    runtime.run(task, EventCancellationToken(threading.Event()))

    final = store.task("alice", task["id"])
    assert final["status"] == "failed"
    assert len(model.compactions) == 1
    assert "provider_retry" not in [e["kind"] for e in final["events"]]


def test_stop_during_the_summary_retry_pause_makes_no_further_call(
    tmp_path, twelve_pages, monkeypatch
):
    monkeypatch.setattr(runtime_module, "PROVIDER_RETRY_DELAY_SECONDS", 10.0)
    written = summary(["Compare values", "Read pages", "Values found", "Read the rest"])
    holder = {}

    def stopped_while_waiting():
        # The person presses Stop shortly after the call fails, during the pause.
        holder["steps"] = len(model.steps)
        threading.Timer(0.1, lambda: holder["runtime"].cancel(holder["task"])).start()
        return provider_error(503)

    model = Researcher(12, written, before=[], failures=[stopped_while_waiting])
    store, task, runtime = short_turn(tmp_path, model)
    holder.update(runtime=runtime, task=task["id"])
    started = time.monotonic()

    runtime.run(task, EventCancellationToken(threading.Event()))

    final = store.task("alice", task["id"])
    assert time.monotonic() - started < 5
    assert final["status"] == "cancelled"
    # No summary call was sent again, and no step came after Stop.
    assert len(model.compactions) == 1 and len(model.steps) == holder["steps"]
    assert final["calls"] == len(model.steps) + 1


# The reply checker reads a progress record as evidence of the steps it replaced.

GREEK_REQUEST = "Βρες πόσο κοστίζει το εισιτήριο σε κάθε μουσείο και κράτα σημειώσεις."
FIRST_FACT = "Μουσείο 1: το εισιτήριο κοστίζει 11 ευρώ"
NOTES = "σημειώσεις.md"


def checker(prompt):
    """A scripted reply checker that applies its instructions to the evidence it is given.

    It counts a progress record only when its instructions say so. A fact is supported
    when the evidence gives it with its link; a saved file when a save_file receipt or
    call names it. Each draft below says which facts and files it claims.
    """
    instructions, payload = prompt[0]["content"], json.loads(prompt[1]["content"])
    context = payload["context_and_receipts"]
    evidence = [m["content"] for m in context if m["role"] == "tool"]
    saved = [
        json.loads(call["function"]["arguments"]).get("name")
        for m in context
        for call in m.get("tool_calls") or []
        if call["function"]["name"] == "save_file"
    ]
    if "Progress record of this same task" in instructions:
        for record in filter(None, map(progress_record, context)):
            evidence.append(encode(record["summary"]))
            saved += [
                r["arguments"].get("name")
                for group in record["steps"]
                for r in group["receipts"]
                if r["tool"] == "save_file" and "error" not in r.get("result", {})
            ]
    claims = DRAFT_CLAIMS[payload["draft_reply"]]
    for fact, source in claims["facts"]:
        if not any(fact in text and source in text for text in evidence):
            return {"decision": "repair", "reason": "Unsupported fact.", "issue": "contradiction"}
    for name in claims["files"]:
        if name not in saved:
            return {
                "decision": "repair",
                "reason": "No receipt shows this file saved.",
                "issue": "contradiction",
                "false_claim": f"Αποθήκευσα επίσης τον πίνακα στο {name}.",
            }
    return {"decision": "ready", "reason": "Every claim matches the receipts."}


SOURCED = f"Το Μουσείο 1 κοστίζει 11 ευρώ ({link(1)}). Οι σημειώσεις είναι στο {NOTES}."
INVENTED = SOURCED + " Αποθήκευσα επίσης τον πίνακα στο πίνακας.md."
DRAFT_CLAIMS = {
    SOURCED: {"facts": [(FIRST_FACT, link(1))], "files": [NOTES]},
    INVENTED: {"facts": [(FIRST_FACT, link(1))], "files": [NOTES, "πίνακας.md"]},
}


def checked_turn(tmp_path, pages, drafts):
    for n in range(1, 13):
        pages[link(n)] = f"Μουσείο {n}: το εισιτήριο κοστίζει {n + 10} ευρώ. " * 120
    written = summary(
        [
            "Τιμές εισιτηρίων",
            "Διάβασα τις σελίδες των μουσείων",
            f"{FIRST_FACT} ({link(1)})",
            "Διάβασε τις υπόλοιπες σελίδες",
        ]
    )
    before = [[ToolCall("notes", "save_file", {"name": NOTES, "content": "Μουσεία"})]]
    limit = first_request_tokens(tmp_path / "probe", GREEK_REQUEST, [written]) + 5000
    store, task = owl_task(tmp_path / "turn", GREEK_REQUEST)
    model = Researcher(12, written, before=before, drafts=drafts, judge=checker)
    runtime = BotRuntime(
        store, "alice", lambda _: model, limits=RuntimeLimits(context_tokens=limit)
    )
    runtime.reply_review = ReplyReview(runtime)
    runtime.run(task, EventCancellationToken(threading.Event()))
    return store.task("alice", task["id"]), model


def test_the_checker_accepts_a_sourced_fact_found_only_in_compacted_steps(tmp_path, pages):
    final, model = checked_turn(tmp_path, pages, [SOURCED])

    assert final["status"] == "completed", final["error"]
    assert final["output"] == SOURCED
    assert model.compactions
    (review,) = model.reviews
    payload = json.loads(review[1]["content"])
    context = payload["context_and_receipts"]
    # The fact and the saved file are known only through the progress record now.
    assert not any(FIRST_FACT in m["content"] for m in context if m["role"] == "tool")
    assert not any(m.get("tool_calls") and "save_file" in encode(m) for m in context)
    assert any(progress_record(m) for m in context)
    # The checker is told what the compacted steps' tools do.
    assert "save_file" in [d["name"] for d in payload["tool_documentation"]]


def test_the_checker_still_withholds_an_action_no_receipt_shows(tmp_path, pages):
    final, model = checked_turn(tmp_path, pages, [INVENTED, SOURCED])

    assert final["status"] == "completed", final["error"]
    assert final["output"] == SOURCED
    verdicts = [e["data"] for e in final["events"] if e["kind"] == "reply_review_finished"]
    assert [v["decision"] for v in verdicts] == ["repair", "ready"]
    assert verdicts[0]["false_claim"] == "Αποθήκευσα επίσης τον πίνακα στο πίνακας.md."


def test_the_checker_rule_names_the_record_the_service_writes():
    heading = "Progress record of this same task"
    assert TURN_PROGRESS_NOTE.startswith(heading)
    assert f'"{heading}"' in " ".join(REVIEW_INSTRUCTIONS.split())
    record = {"summary": {}, "steps": [{"stored_result_id": "s", "receipts": [{"tool": "x"}]}]}
    sent = {"role": "user", "content": TURN_PROGRESS_NOTE + encode(record)}
    assert progress_record(sent) == record
    # The person's own message is never read as one.
    assert progress_record({"role": "user", "content": "Progress record, by me"}) is None
