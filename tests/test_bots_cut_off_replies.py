"""A model reply the output limit cut off: an owl runs none of it and shows none of it (cut_off.py).

Real store and runtime with scripted models. A cut-off reply arrives the three ways providers send
one: a chat-completions reply that stopped with finish_reason "length", an Anthropic Messages
reply that stopped with stop_reason "max_tokens" (both ordinary responses), and the Responses
client's LLMOutputLimitError. Reply texts in Greek and Chinese; two models, one with a catalogued
output ceiling above the owl's allowance and one without.
"""

from __future__ import annotations

import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from test_bots_coding_strength import PLAIN, STEP, Connection, coding, declare, stop_token
from test_bots_cooperation import peers, run_peer, submit
from test_bots_desktop_runtime import study
from test_bots_research_helpers import (
    QUESTIONS,
    events,
    owl_task,
    page,
    report,
    research_call,
    research_result,
    web,
)
from test_bots_research_helpers import (
    Model as ResearchModel,
)
from test_bots_research_helpers import run as run_research

from alysis_code.bots import cut_off
from alysis_code.bots.cut_off import (
    MAX_RECOVERIES,
    OUTPUT_LIMIT_FAILURE,
    CutOffReplies,
    written_tool_names,
)
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.reply_review import ReplyVerdict
from alysis_code.bots.research_helpers import HELPER_PAGE_CHARS
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import LimitReached, Store
from alysis_code.cancellation import EventCancellationToken
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.types import LLMOutputLimitError, LLMResponse, LLMUsage, ToolCall
from alysis_code.request_estimation import estimate_request_tokens

# Fixtures imported from other test modules, used by name below.
__all__ = ["coding", "peers", "study", "web"]

OWNER = "alice"
# What the cut-off reply said before its call: it must never be published.
INTERIM = "Ολοκλήρωσα την έρευνα για τη σύγκριση· γράφω τώρα το έγγραφο."
FINAL = "已将比较保存为 比较.docx,并核对了每一节。"
# The half-written call's arguments: they must never reach an event or a note.
SECRET_ARGUMENT = "Πίνακας σύγκρισης 東京"
SAVE = ToolCall("save", "save_file", {"name": "σύγκριση.txt", "content": SECRET_ARGUMENT})


def usage():
    return LLMUsage(120, 4096, 4216)


def finished(text=FINAL):
    raw = {
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": text}}]
    }
    return LLMResponse(text, [], raw, usage=usage())


def chat_completions_cut_off(text=INTERIM, *, parsed=True):
    """A chat-completions reply that spent its whole allowance writing a call. Parsed: the
    call's arguments happen to parse, so it could otherwise run; not parsed: the client dropped
    the half-written call and only the provider's own message names it."""
    call = (
        {"name": SAVE.name, "arguments": json.dumps(SAVE.arguments, ensure_ascii=False)}
        if parsed
        else {
            "name": "create_document",
            "arguments": '{"spec": {"paragraphs": ["' + SECRET_ARGUMENT,
        }
    )
    message = {
        "role": "assistant",
        "content": text,
        "tool_calls": [{"id": SAVE.id, "type": "function", "function": call}],
    }
    raw = {"choices": [{"finish_reason": "length", "message": message}]}
    return LLMResponse(text, [SAVE] if parsed else [], raw, usage=usage())


def messages_cut_off(text=INTERIM):
    """An Anthropic Messages reply that stopped at max_tokens while writing a tool call."""
    raw = {
        "stop_reason": "max_tokens",
        "content": [
            {"type": "text", "text": text},
            {"type": "tool_use", "id": "save", "name": "save_file", "input": SAVE.arguments},
        ],
    }
    return LLMResponse(text, [SAVE], raw, usage=usage())


def responses_cut_off(text=INTERIM):
    """The Responses client's error for a reply that stopped at max_output_tokens."""
    raw = {
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
        "output": [
            {"type": "message", "content": [{"type": "output_text", "text": text}]},
            {"type": "function_call", "name": "save_file", "arguments": '{"name": "σύ'},
        ],
    }
    return LLMOutputLimitError(LLMResponse(text, [], raw, usage=usage()))


CUT_OFFS = {
    "chat_completions": chat_completions_cut_off,
    "messages": messages_cut_off,
    "responses_error": responses_cut_off,
}


class Model:
    """Plays a scripted step per call: a response, or an exception to raise."""

    supports_tool_calling = True

    def __init__(self, steps, model="cut-off-fixture"):
        self.model = model
        self.steps = list(steps)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(
            {
                "messages": json.loads(json.dumps(kwargs["messages"])),
                "tools": [t["function"]["name"] for t in kwargs.get("tools") or []],
                "max_tokens": kwargs.get("max_tokens"),
            }
        )
        step = self.steps.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class Reviewer:
    """A reply checker that passes every draft and keeps what it was shown."""

    def __init__(self):
        self.drafts = []

    def check(self, task, client, token, messages, schemas, draft, *args, **kwargs):
        self.drafts.append(draft)
        return ReplyVerdict(decision="ready", reason="Matches the receipts.")


def chat_turn(tmp_path, steps, *, limits=None, model="cut-off-fixture", review=True):
    store = Store(tmp_path / "owls.sqlite3")
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα", purpose="Συγκρίσεις"))
    chat = store.create_chat(OWNER, bot["id"], "Σύγκριση")
    store.enqueue(
        OWNER,
        chat["id"],
        TaskInput(message="Σύγκρινε τα δύο και φτιάξε έγγραφο.", request_id="request_1"),
    )
    task = store.claim(OWNER)
    client = Model(steps, model=model)
    runtime = BotRuntime(store, OWNER, lambda _: client, limits=limits)
    reviewer = Reviewer() if review else None
    runtime.reply_review = reviewer
    runtime.run(task, EventCancellationToken(threading.Event()))
    return SimpleNamespace(
        store=store,
        bot=bot,
        chat=chat,
        task=store.task(OWNER, task["id"]),
        client=client,
        reviewer=reviewer,
    )


def kinds(task):
    return [e["kind"] for e in task["events"]]


def data(task, kind):
    return [e["data"] for e in task["events"] if e["kind"] == kind]


def system_notes(request):
    return [m["content"] for m in request["messages"] if m["role"] == "system"]


def notes_in(request):
    return [n for n in system_notes(request) if "cut off at the output limit" in n]


def note_in(request):
    """The latest cut-off note a request carries, if any."""
    found = notes_in(request)
    return found[-1] if found else None


def shown(run):
    """Every text the person could see: the task's output, the chat's assistant messages and
    progress lines."""
    texts = [run.task["output"] or ""]
    texts += [
        m["content"] or ""
        for m in run.store.conversation(OWNER, run.chat["id"])["messages"]
        if m["role"] == "assistant"
    ]
    texts += [d.get("text", "") for d in data(run.task, "progress_note")]
    return texts


# ---------------------------------------------------------------------------------------------
# The chat turn


@pytest.mark.parametrize("arrives_as", list(CUT_OFFS))
def test_a_cut_off_reply_runs_nothing_shows_nothing_and_the_next_reply_is_published(
    tmp_path, arrives_as
):
    run = chat_turn(tmp_path, [CUT_OFFS[arrives_as](), finished()])

    assert run.task["status"] == "completed" and run.task["output"] == FINAL
    # Not one of its calls ran, not even one whose arguments parsed: no step, no file.
    assert "tool_started" not in kinds(run.task)
    assert run.store.list_files(OWNER, run.bot["id"]) == []
    # Its text was never published, shown as progress or reviewed as a reply.
    assert not any(INTERIM in text for text in shown(run))
    assert run.reviewer.drafts == [FINAL]
    # The model was told, in the next request only, and its cut-off reply was not replayed.
    first, second = run.client.requests
    assert note_in(first) is None
    note = note_in(second)
    assert "Nothing from it ran" in note and "edit_document" in note
    assert "save_file" in note
    assert SECRET_ARGUMENT not in json.dumps(second["messages"], ensure_ascii=False)
    assert not any(
        m["role"] == "assistant" and INTERIM in (m.get("content") or "") for m in second["messages"]
    )
    # The call was charged and recorded as incomplete; the event names tools, never arguments.
    assert [u["status"] for u in run.task["usage"]] == ["incomplete", "completed"]
    assert run.task["usage"][0]["usage"]["completion_tokens"] == 4096
    assert data(run.task, "output_cut_off") == [
        {"tools": ["save_file"], "attempt": 1, "retry": True}
    ]
    assert SECRET_ARGUMENT not in json.dumps(run.task["events"], ensure_ascii=False)
    # A recovery, not a resend of the same request after a provider error.
    assert "provider_retry" not in kinds(run.task)


def test_a_call_dropped_while_parsing_is_named_from_the_providers_own_message(tmp_path):
    run = chat_turn(tmp_path, [chat_completions_cut_off(parsed=False), finished()])
    assert run.task["status"] == "completed" and run.task["output"] == FINAL
    assert "It was writing a call to create_document." in note_in(run.client.requests[1])
    assert data(run.task, "output_cut_off")[0]["tools"] == ["create_document"]
    assert SECRET_ARGUMENT not in json.dumps(run.task["events"], ensure_ascii=False)


def test_the_reply_as_the_chat_completions_client_parses_it(tmp_path):
    # The real client keeps a half-written call with its unparsed arguments; nothing of it runs.
    body = {
        "model": "cut-off-fixture",
        "choices": [
            {
                "index": 0,
                "finish_reason": "length",
                "message": {
                    "role": "assistant",
                    "content": INTERIM,
                    "tool_calls": [
                        {
                            "id": "doc",
                            "type": "function",
                            "function": {
                                "name": "create_document",
                                "arguments": '{"spec": {"paragraphs": ["' + SECRET_ARGUMENT,
                            },
                        }
                    ],
                },
            }
        ],
        "usage": {"prompt_tokens": 5000, "completion_tokens": 4096, "total_tokens": 9096},
    }
    cut = OpenAICompatClient._parse_non_stream_response(
        httpx.Response(200, json=body), provider_key=None, model="cut-off-fixture"
    )
    run = chat_turn(tmp_path, [cut, finished()])
    assert run.task["status"] == "completed" and run.task["output"] == FINAL
    assert "tool_started" not in kinds(run.task)
    assert run.store.list_files(OWNER, run.bot["id"]) == []
    assert "It was writing a call to create_document." in note_in(run.client.requests[1])
    assert SECRET_ARGUMENT not in json.dumps(run.task["events"], ensure_ascii=False)


def test_a_reply_cut_off_with_only_text_is_not_published_as_final(tmp_path):
    # The case seen live: the call was dropped and its leading text came back as the reply.
    message = {"role": "assistant", "content": INTERIM}
    raw = {"choices": [{"finish_reason": "length", "message": message}]}
    cut = LLMResponse(INTERIM, [], raw, usage=usage())
    run = chat_turn(tmp_path, [cut, finished()])
    assert run.task["output"] == FINAL and run.reviewer.drafts == [FINAL]
    assert data(run.task, "output_cut_off") == [{"tools": [], "attempt": 1, "retry": True}]


def test_cut_offs_past_the_bound_end_the_turn_with_the_plain_reason(tmp_path):
    steps = [chat_completions_cut_off(), messages_cut_off(), responses_cut_off(), finished()]
    run = chat_turn(tmp_path, steps)

    assert len(run.client.requests) == MAX_RECOVERIES + 1 == 3
    assert run.task["status"] == "failed"
    assert run.task["error"] == OUTPUT_LIMIT_FAILURE
    assert run.task["failure"]["kind"] == "model"
    assert [u["status"] for u in run.task["usage"]] == ["incomplete"] * 3
    assert [d["retry"] for d in data(run.task, "output_cut_off")] == [True, True, False]
    assert "tool_started" not in kinds(run.task)
    assert not any(INTERIM in text for text in shown(run))
    assert run.reviewer.drafts == []


def test_a_routine_or_background_turn_recovers_the_same_way(tmp_path):
    # Without the reply checker (as a routine's run, an activation or a peer report) the
    # cut-off reply is no more publishable.
    run = chat_turn(tmp_path, [messages_cut_off(), finished()], review=False)
    assert run.task["status"] == "completed" and run.task["output"] == FINAL
    assert [u["status"] for u in run.task["usage"]] == ["incomplete", "completed"]


# ---------------------------------------------------------------------------------------------
# The output allowance after a cut-off


@pytest.mark.parametrize(
    "model, ceiling, raised",
    [("declares-a-larger-ceiling", 65_536, 65_536), ("declares-no-ceiling", None, 4096)],
)
def test_the_call_after_a_cut_off_may_use_the_catalogued_ceiling_once(
    tmp_path, model, ceiling, raised
):
    limits = RuntimeLimits(output_tokens=4096, output_ceiling=ceiling, context_tokens=300_000)
    steps = [chat_completions_cut_off(), chat_completions_cut_off(), finished()]
    run = chat_turn(tmp_path, steps, limits=limits, model=model)

    assert run.task["status"] == "completed" and run.task["output"] == FINAL
    # Raised for the one call right after the first cut-off; never again in the turn.
    assert [r["max_tokens"] for r in run.client.requests] == [4096, raised, 4096]
    expected = [{"output_tokens": raised, "allowance": 4096}] if ceiling else []
    assert data(run.task, "output_allowance_raised") == expected


def test_a_raised_allowance_stays_within_the_room_the_request_leaves():
    store = SimpleNamespace(events=[], calls=[])
    store.event = lambda owner, task_id, kind, payload: store.events.append((kind, payload))
    store.finish_call = lambda call_id, status, used: store.calls.append((call_id, status))
    request = [{"role": "user", "content": "Σύγκρινε 東京 και Αθήνα. " * 200}]
    spare = 1500
    limits = SimpleNamespace(
        output_tokens=4096,
        output_ceiling=200_000,
        context_tokens=estimate_request_tokens(request, None) + spare,
    )
    replies = CutOffReplies(store, OWNER, "task", limits)
    assert replies.output_tokens(4096, request, None) == 4096  # nothing cut off yet
    notes = []
    assert replies.settle("call-1", chat_completions_cut_off(), notes)
    assert replies.output_tokens(4096, request, None) == 4096 + spare
    assert replies.output_tokens(4096, request, None) == 4096
    assert store.calls == [("call-1", "incomplete")]
    # A finished reply is left to the loop.
    assert not replies.settle("call-2", finished(), notes)
    assert len(notes) == 1 and store.calls == [("call-1", "incomplete")]


def test_runtime_limits_carry_only_a_catalogued_ceiling(monkeypatch):
    endpoint = "https://models.invalid/v1"
    monkeypatch.setenv("ALYSIS_MAX_OUTPUT_TOKENS", "65536")
    declared = RuntimeLimits.for_model(
        model="owl-fixture-declares-output", base_url=endpoint, output_tokens=8192
    )
    assert (declared.output_tokens, declared.output_ceiling) == (8192, 65_536)
    monkeypatch.delenv("ALYSIS_MAX_OUTPUT_TOKENS")
    guessed = RuntimeLimits.for_model(
        model="owl-fixture-declares-nothing", base_url=endpoint, output_tokens=8192
    )
    assert (guessed.output_tokens, guessed.output_ceiling) == (8192, None)


# ---------------------------------------------------------------------------------------------
# Computer work


def test_computer_work_recovers_from_a_cut_off_reply(study):
    from alysis_code.bots.execution import TaskClient

    store, task, manager, instances = study
    client = Model([responses_cut_off(), chat_completions_cut_off(), finished()])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]))
    tools = [{"type": "function", "function": {"name": "save_file", "parameters": {}}}]
    result = adapter.chat(messages=[{"role": "user", "content": task["message"]}], tools=tools)

    assert result.content == FINAL and adapter.fatal is None
    assert [len(notes_in(r)) for r in client.requests] == [0, 1, 2]
    assert "Nothing from it ran" in note_in(client.requests[2])
    usage_rows = store.task("owner", task["id"])["usage"]
    assert [u["status"] for u in usage_rows] == ["incomplete", "incomplete", "completed"]
    assert not instances  # no computer action ran
    assert "progress_note" not in kinds(store.task("owner", task["id"]))


def test_computer_work_ends_on_one_cut_off_too_many(study):
    from alysis_code.bots.execution import TaskClient

    store, task, manager, _ = study
    client = Model([messages_cut_off() for _ in range(MAX_RECOVERIES + 1)] + [finished()])
    runtime = BotRuntime(store, "owner", lambda _: client, desktop_computers=manager)
    adapter = TaskClient(runtime, task, client, runtime.task_token(task["id"]))
    with pytest.raises(LimitReached, match="response limit") as ended:
        adapter.chat(messages=[{"role": "user", "content": task["message"]}])
    assert ended.value.failure_kind == "model"
    # The engine session cannot swallow it: the work ends on it after the session returns.
    assert adapter.fatal is ended.value
    assert len(client.requests) == MAX_RECOVERIES + 1


# ---------------------------------------------------------------------------------------------
# Research helpers


@pytest.mark.parametrize("arrives_as", ["chat_completions", "responses_error"])
def test_a_research_helper_recovers_from_a_cut_off_reply(tmp_path, web, arrives_as):
    lock = threading.Lock()
    calls = []

    def helper(question, steps, seen, kwargs):
        with lock:
            calls.append(seen)
            n = len(calls)
        if n == 1:
            if arrives_as == "responses_error":
                raise responses_cut_off()
            cut = chat_completions_cut_off()
            return LLMResponse(
                cut.content, [ToolCall("f1", "web_fetch", {"url": page(1)})], cut.raw, usage=usage()
            )
        if n == 2:
            return LLMResponse(
                "", [ToolCall("f2", "web_fetch", {"url": page(2)})], {}, usage=usage()
            )
        return report("Ανοιχτό 9:00-17:00", [("Ώρες", page(2))])

    store, task = owl_task(tmp_path)
    model = ResearchModel(
        [LLMResponse("", [research_call(QUESTIONS[1])], {}, usage=usage()), finished()], helper
    )
    _, final = run_research(store, task, model)

    assert final["status"] == "completed" and final["output"] == FINAL
    # The call written in the cut-off reply was not run; the next one was.
    assert web["reads"] == [(page(2), HELPER_PAGE_CHARS)]
    assert note_in(calls[1]) is not None and note_in(calls[0]) is None
    (result,) = research_result(model)["helpers"]
    assert result["state"] == "answered" and result["answer"] == "Ανοιχτό 9:00-17:00"
    (event,) = events(final, "output_cut_off")
    assert event["helper"] == 1 and event["retry"] is True
    assert "incomplete" in [u["status"] for u in final["usage"]]


def test_a_research_helper_ends_as_before_past_the_bound(tmp_path, web):
    def helper(question, steps, seen, kwargs):
        return chat_completions_cut_off()

    store, task = owl_task(tmp_path)
    model = ResearchModel(
        [LLMResponse("", [research_call(QUESTIONS[2])], {}, usage=usage()), finished()], helper
    )
    _, final = run_research(store, task, model)
    assert final["status"] == "completed" and final["output"] == FINAL
    (result,) = research_result(model)["helpers"]
    assert result["state"] == "failed"
    assert len(model.helper_requests) == MAX_RECOVERIES + 1
    assert web["reads"] == []


# ---------------------------------------------------------------------------------------------
# A peer's reply and coding work


def test_a_peers_cut_off_reply_is_never_its_answer(peers):
    store, ps, a, b, other, task = peers
    submit(peers)
    script = [chat_completions_cut_off(parsed=False), finished()]
    heard = []

    def respond(kwargs):
        heard.append(kwargs["messages"])
        return script.pop(0)

    run_peer(peers, respond)
    item = ps.for_task(task["id"])
    assert item["status"] == "completed" and item["result"] == FINAL
    assert "Write a shorter, complete reply now." in heard[1][-1]["content"]
    assert [u["status"] for u in store.task("owner", task["id"])["usage"]] == [
        "incomplete",
        "completed",
    ]


def test_coding_work_gets_a_cut_off_reply_as_cut_off(coding, monkeypatch):
    declare(monkeypatch, PLAIN)
    work = coding(Connection(PLAIN, None, [chat_completions_cut_off()]))
    tools = [{"type": "function", "function": {"name": "shell_run", "parameters": {}}}]
    with pytest.raises(LLMOutputLimitError) as cut:
        work.client.chat(messages=STEP, tools=tools, cancellation_token=stop_token())
    # The worker's own turn recovers from it; none of its calls reaches the worker.
    assert cut.value.response.tool_calls == []
    assert cut.value.response.content == INTERIM
    usage_rows = work.store.task(work.owner, work.task["id"])["usage"]
    assert [u["status"] for u in usage_rows] == ["incomplete"]


def test_tool_names_come_from_every_protocol_and_never_their_arguments():
    assert written_tool_names(chat_completions_cut_off()) == ["save_file"]
    assert written_tool_names(chat_completions_cut_off(parsed=False)) == ["create_document"]
    assert written_tool_names(messages_cut_off()) == ["save_file"]
    assert written_tool_names(responses_cut_off().response) == ["save_file"]
    odd = LLMResponse("", [ToolCall("x", "bad name; rm -rf", {})], {}, usage=usage())
    assert written_tool_names(odd) == []
    assert cut_off.cut_off_note([], []).endswith("Write a shorter, complete reply now.")
    several = cut_off.cut_off_note(["web_search", "web_fetch", "save_file"], ["save_file"])
    assert "It was writing calls to web_search, web_fetch and save_file." in several
    assert "edit_document" not in several  # named only when it is offered
