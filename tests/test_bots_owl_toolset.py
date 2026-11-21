"""Which tools an owl's turn is offered (owl_toolset.py).

A turn gets the tools it can use, decided from its kind and the features its owl and service
have, never from the words of the message. When the list is too much for one request (too many
tools, too much of the model's window, or of its route's own bound) or the model's declared
window is small, some tools are reached through tool_details and use_tool instead: connected
apps' tools first, then all but the everyday tools. A call through use_tool runs the hidden tool
by exactly the path and checks of a direct call. Real store and runtime; scripted model; messages
and app tools in Greek and Japanese; two declared windows where the window matters.
"""

from __future__ import annotations

import itertools
import json
import threading
from dataclasses import replace
from uuid import uuid4

import httpx
import pytest

from alysis_code.bots.browser_tools import DEFINITIONS as BROWSER_TOOLS
from alysis_code.bots.desktop import DEFINITIONS as DESKTOP_TOOLS
from alysis_code.bots.desktop_connectors import CatalogInput, DesktopConnectors
from alysis_code.bots.models import BotInput, TaskInput
from alysis_code.bots.owl_tools import (
    ROOM_TURN_LEFT_OUT,
    ROUTINE_TOOLS,
    TOOL_DEFINITIONS,
    tool_schema,
)
from alysis_code.bots.owl_toolset import (
    ACTIVATION_REVIEW_TOOLS,
    APPS_BY_LOOKUP,
    CALL_THROUGH_TOOL,
    EVERYDAY,
    EVERYDAY_TOOLS,
    FULL,
    LOOKUP_TOOL,
    MAX_DIRECT_TOOLS,
    NOT_IN_BACKGROUND,
    ROUTE_BASE_SHARE,
    ROUTE_OVER_SHARE,
    SCHEMAS_OVER_SHARE,
    SMALL_CONTEXT_WINDOW_TOKENS,
    SMALL_WINDOW,
    TOO_MANY_TOOLS,
    TOOL_SCHEMA_WINDOW_SHARE,
    ToolRoom,
    Turn,
    app_tools,
    offer,
    route_share,
    tool_names,
)
from alysis_code.bots.runtime import BotRuntime, RuntimeLimits
from alysis_code.bots.store import NotFound, Store
from alysis_code.bots.tool_parameters import tool_parameters, without_titles
from alysis_code.cancellation import EventCancellationToken
from alysis_code.cloud_connections import BoundConnections, ConnectionStore
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.types import LLMResponse, LLMUsage, ToolCall
from alysis_code.request_estimation import estimate_tool_schema_tokens

OWNER = "owner"
GREEK = "Θύμισέ μου σε μία ώρα να ποτίσω τα φυτά."
JAPANESE = "一時間後に植物に水をやるように知らせて。"
MEMORY_ONLY = {"memory_history", "memory_source_read", "propose_memory", "list_memories"}
FILE_ONLY = {"list_files", "read_file", "save_file", "import_local_file", "export_local_file"}
CODING_ONLY = {"coding_task", "read_coding_result", "update_coding_task", "stop_coding_task"}
PEERS = {"list_peer_bots", "request_peer", "read_peer_exchange"}
ROUTINE_ARGUMENTS = {
    "name": "Πότισμα",
    "prompt": "Θύμισέ μου να ποτίσω τα φυτά.",
    "timing": {"kind": "after", "delay_seconds": 3600},
}


# ---------------------------------------------------------------------------------------------
# The rules, from the turn's own state and features


def test_memory_and_its_receipt_tools_come_only_with_memory_on():
    on = tool_names(Turn(answers_person=True, memory=True))
    off = tool_names(Turn(answers_person=True, memory=False))
    assert MEMORY_ONLY <= set(on)
    assert not MEMORY_ONLY & set(off)
    assert "search_history" not in off and "save_working_note" not in off


def test_files_coding_and_web_lookup_come_only_with_those_features():
    bare = set(tool_names(Turn(answers_person=True)))
    full = set(tool_names(Turn(answers_person=True, files=True, coding=True, web_lookup=True)))
    assert not (FILE_ONLY | CODING_ONLY | {"web_search", "web_fetch"}) & bare
    assert FILE_ONLY | CODING_ONLY | {"web_search", "web_fetch"} <= full


def test_routines_are_set_up_only_while_answering_the_person():
    answering = set(tool_names(Turn(answers_person=True)))
    assert ROUTINE_TOOLS | {"propose_schedule", "propose_setup"} <= answering
    for kind in (
        Turn(answers_person=False, background=True),
        Turn(answers_person=False, activation=True),
        Turn(answers_person=False, scheduled=True),
    ):
        assert not ROUTINE_TOOLS & set(tool_names(kind))
    # A first hello or another answer to the same message offers no routine.
    assert "propose_schedule" not in tool_names(Turn(answers_person=True, first_hello=True))
    assert "propose_schedule" not in tool_names(Turn(answers_person=True, regenerated=True))


def test_each_kind_of_turn_keeps_only_what_it_may_do():
    background = tool_names(Turn(answers_person=False, background=True, coding=True))
    assert not NOT_IN_BACKGROUND & set(background)
    assert "read_coding_result" in background
    review = tool_names(Turn(answers_person=False, activation=True, memory=True, files=True))
    assert set(review) <= ACTIVATION_REVIEW_TOOLS and "read_memory" in review
    assert "ask_clarification" not in tool_names(Turn(answers_person=False, scheduled=True))
    room = tool_names(Turn(answers_person=True, room=True, other_owls=True, about_me=True))
    assert not ROOM_TURN_LEFT_OUT & set(room)


def test_other_owls_browser_drafts_and_handoff_need_what_they_work_with():
    alone = set(tool_names(Turn(answers_person=True)))
    assert not PEERS & alone
    assert "read_browser_draft" not in alone and "request_computer_control" not in alone
    together = set(tool_names(Turn(answers_person=True, other_owls=True, browser=True)))
    assert PEERS <= together and "read_browser_draft" in together
    assert "request_computer_control" in tool_names(
        Turn(answers_person=True, computer_handoff=True)
    )


def test_about_me_suggestions_need_the_persons_own_words():
    assert "suggest_about_me" in tool_names(Turn(answers_person=True, about_me=True))
    for turn in (
        Turn(answers_person=True, about_me=True, first_hello=True),
        Turn(answers_person=True, about_me=True, skipped_question=True),
        Turn(answers_person=True, about_me=False),
    ):
        assert "suggest_about_me" not in tool_names(turn)


# ---------------------------------------------------------------------------------------------
# A small declared window: the everyday tools, and two that reach the rest


def _schemas(names):
    return [tool_schema(name) for name in names]


def test_a_large_window_gets_every_tool_unchanged():
    schemas = _schemas(tool_names(Turn(answers_person=True, memory=True, files=True)))
    assert offer(schemas, context_window=SMALL_CONTEXT_WINDOW_TOKENS).schemas == schemas
    assert not offer(schemas, context_window=SMALL_CONTEXT_WINDOW_TOKENS).hidden


def test_a_small_window_gets_the_everyday_tools_and_reaches_the_rest():
    schemas = _schemas(tool_names(Turn(answers_person=True, memory=True, files=True)))
    small = offer(schemas, context_window=SMALL_CONTEXT_WINDOW_TOKENS - 1)
    offered = [s["function"]["name"] for s in small.schemas]
    assert set(offered) - {LOOKUP_TOOL, CALL_THROUGH_TOOL} <= EVERYDAY_TOOLS
    assert offered[-2:] == [LOOKUP_TOOL, CALL_THROUGH_TOOL]
    assert set(small.hidden) == {s["function"]["name"] for s in schemas} - EVERYDAY_TOOLS
    # Every hidden tool is named in the lookup and allowed by both tools' inputs.
    lookup = small.schemas[-2]["function"]
    for name in small.hidden:
        assert f"{name}: " in lookup["description"]
    assert lookup["parameters"]["properties"]["name"]["enum"] == list(small.hidden)
    assert len(json.dumps(small.schemas)) < len(json.dumps(schemas)) / 3


def test_nothing_to_hide_means_nothing_added():
    schemas = _schemas(["propose_action", "withdraw_action"])
    assert offer(schemas, context_window=8000).schemas == schemas


def test_a_call_through_becomes_the_hidden_tools_own_call():
    small = offer(_schemas(["ask_clarification", "create_schedule"]), context_window=8000)
    call = ToolCall("c1", CALL_THROUGH_TOOL, {"name": "create_schedule", "arguments": {"a": 1}})
    (unwrapped,) = small.unwrap([call])
    assert (unwrapped.id, unwrapped.name, unwrapped.arguments) == (
        "c1",
        "create_schedule",
        {"a": 1},
    )
    # Anything else is left as it came, to be answered or refused.
    stranger = ToolCall("c2", CALL_THROUGH_TOOL, {"name": "local_command", "arguments": {}})
    assert small.unwrap([stranger]) == [stranger]
    assert (
        small.answer(ToolCall("c3", LOOKUP_TOOL, {"name": "create_schedule"}))
        == (tool_schema("create_schedule")["function"])
    )
    with pytest.raises(NotFound, match="create_schedule"):
        small.answer(stranger)


# ---------------------------------------------------------------------------------------------
# Measured: the tool count, the window share and the route's own bound

OWN = Turn(answers_person=True, memory=True, files=True, web_lookup=True)
EVERY_FEATURE = Turn(
    answers_person=True,
    web_lookup=True,
    tool_calling=True,
    browser=True,
    computer_handoff=True,
    files=True,
    documents=True,
    images=True,
    memory=True,
    coding=True,
    other_owls=True,
    about_me=True,
    playbooks=True,
    reply_checked=True,
    own_message=True,
)
INPUTS = {"type": "object", "properties": {"query": {"type": "string"}}}
# An app tool with many described inputs: about 5,500 tokens each.
WIDE_INPUTS = {
    "type": "object",
    "properties": {
        f"field_{n}": {"type": "string", "description": "Ένα πεδίο της σημείωσης. " * 12}
        for n in range(30)
    },
}


def connected_apps(tmp_path, count, *, inputs=INPUTS):
    """The connected tools a turn gets from apps with ``count`` tools between them (at most 96 per
    app), built as the runtime builds them, and the lines the look-up lists the apps' tools by."""
    plugins = [
        {
            "plugin_id": f"alysis.notes{index}",
            "name": f"Σημειώσεις {index}",
            "tools": [
                {
                    "name": f"find-{number}",
                    "description": f"ノート{number}を探します。読むだけです。",
                    "input_schema": inputs,
                    "effect": "read",
                }
                for number in range(start, min(count, start + 96))
            ],
        }
        for index, start in enumerate(range(0, count, 96))
    ]
    hub = DesktopConnectors()
    hub.publish(CatalogInput(revision=1, plugins=plugins))
    store = ConnectionStore(tmp_path / f"connections-{uuid4().hex}.sqlite3")
    tools = BoundConnections(store, OWNER, "bot:owl", None, hub).tools()
    return [tool.as_openai_tool() for tool in tools.values()], app_tools(tools)


def names_of(schemas):
    return [schema["function"]["name"] for schema in schemas]


def byte_route(bound, standing=0):
    """A route whose bound counts request bytes: the standing request with a tool list."""
    return lambda tools: (standing + len(json.dumps(tools).encode())) / bound


@pytest.mark.parametrize("window", [200_000, 1_000_000])
def test_many_app_tools_go_behind_lookup_and_the_owls_own_stay_direct(tmp_path, window):
    own = _schemas(tool_names(OWN))
    connected, lines = connected_apps(tmp_path, 120)
    made = offer([*own, *connected], context_window=window, apps=lines)
    assert made.mode == APPS_BY_LOOKUP and made.reasons == (TOO_MANY_TOOLS,)
    # Every tool of the owl's own, and the list of apps, stays direct; every app tool is hidden.
    assert names_of(made.schemas) == [
        *names_of(own),
        "list_connected_apps",
        LOOKUP_TOOL,
        CALL_THROUGH_TOOL,
    ]
    assert set(made.hidden) == set(lines) and len(lines) == 120
    assert len(made.schemas) <= MAX_DIRECT_TOOLS < made.measures.tools
    # Each app tool is listed by its app and its own words, not the notes all its tools share.
    lookup = made.schemas[-2]["function"]["description"]
    hundredth = next(name for name in lines if name.endswith("find_100"))
    assert f"{hundredth}: Σημειώσεις 1: ノート100を探します。読むだけです。" in lookup
    assert "used through Alysis" not in lookup
    (call,) = made.unwrap(
        [ToolCall("n", CALL_THROUGH_TOOL, {"name": hundredth, "arguments": {"query": "x"}})]
    )
    assert (call.name, call.arguments) == (hundredth, {"query": "x"})


def test_app_tools_go_behind_lookup_when_the_list_takes_too_much_of_the_window(tmp_path):
    own = _schemas(tool_names(OWN))
    connected, lines = connected_apps(tmp_path, 12, inputs=WIDE_INPUTS)
    schemas = [*own, *connected]
    tokens = estimate_tool_schema_tokens(schemas)
    narrow, wide_window = 150_000, 1_000_000
    assert narrow * TOOL_SCHEMA_WINDOW_SHARE < tokens < wide_window * TOOL_SCHEMA_WINDOW_SHARE
    assert len(schemas) <= MAX_DIRECT_TOOLS
    hidden = offer(schemas, context_window=narrow, apps=lines)
    assert hidden.mode == APPS_BY_LOOKUP and hidden.reasons == (SCHEMAS_OVER_SHARE,)
    assert set(hidden.hidden) == set(lines)
    kept = offer(schemas, context_window=wide_window, apps=lines)
    assert kept.mode == FULL and kept.schemas == schemas and not kept.hidden
    # The share alone never hides the owl's own tools: the small-window rule sizes those.
    own_only = offer(own, context_window=SMALL_CONTEXT_WINDOW_TOKENS, apps=lines)
    if estimate_tool_schema_tokens(own) > SMALL_CONTEXT_WINDOW_TOKENS * TOOL_SCHEMA_WINDOW_SHARE:
        assert own_only.reasons == (SCHEMAS_OVER_SHARE,)
    assert own_only.mode == FULL and own_only.schemas == own


@pytest.mark.parametrize("window", [400_000, 1_000_000])
def test_a_route_whose_bound_the_standing_request_fills_gets_lookup(tmp_path, window):
    own = _schemas(tool_names(EVERY_FEATURE))
    full_bytes = len(json.dumps(own).encode())
    # The owl's own tools alone take 87% of the bound: only the everyday tools stay direct.
    tight = byte_route(full_bytes / 0.87)
    made = offer(own, context_window=window, route_share=tight)
    assert made.mode == EVERYDAY and made.reasons == (ROUTE_OVER_SHARE,)
    assert made.measures.route_share == pytest.approx(0.87)
    assert set(names_of(made.schemas)) - {LOOKUP_TOOL, CALL_THROUGH_TOOL} <= EVERYDAY_TOOLS
    assert tight(made.schemas) <= ROUTE_BASE_SHARE
    # With apps connected, hiding their tools is enough when it brings the request under.
    connected, lines = connected_apps(tmp_path, 4, inputs=WIDE_INPUTS)
    with_apps = [*own, *connected]
    roomy = byte_route(len(json.dumps(with_apps).encode()) / 0.6)
    by_app = offer(with_apps, context_window=window, apps=lines, route_share=roomy)
    assert by_app.mode == APPS_BY_LOOKUP and by_app.reasons == (ROUTE_OVER_SHARE,)
    assert set(by_app.hidden) == set(lines)
    assert roomy(by_app.schemas) <= ROUTE_BASE_SHARE


def test_a_route_without_a_bound_and_few_tools_keeps_the_full_list(tmp_path):
    connected, lines = connected_apps(tmp_path, 3)
    schemas = [*_schemas(tool_names(OWN)), *connected]
    for window in (200_000, 1_000_000):
        made = offer(schemas, context_window=window, apps=lines, route_share=lambda _: 0.0)
        assert made.mode == FULL and made.reasons == () and not made.hidden
        assert made.schemas == schemas
        assert made.measures.route_share == 0.0


def test_a_small_window_still_gets_the_everyday_tools_whatever_the_route(tmp_path):
    connected, lines = connected_apps(tmp_path, 3)
    schemas = [*_schemas(tool_names(OWN)), *connected]
    small = SMALL_CONTEXT_WINDOW_TOKENS - 1
    made = offer(schemas, context_window=small, apps=lines, route_share=lambda _: 0.0)
    assert made.mode == EVERYDAY and made.reasons[0] == SMALL_WINDOW
    assert set(made.hidden) == set(names_of(schemas)) - EVERYDAY_TOOLS
    # Exactly the list the small-window rule gave before apps were measured at all.
    assert names_of(made.schemas) == names_of(offer(schemas, context_window=small).schemas)
    assert (
        made.schemas[-2]["function"]["description"]
        != (offer(schemas, context_window=small).schemas[-2]["function"]["description"])
    ), "app tools are listed by their own words"


def test_a_route_measure_that_is_no_share_is_an_error():
    schemas = _schemas(["ask_clarification"])
    for bad in (float("nan"), float("inf"), -0.1, True, "0.2", None):
        with pytest.raises(ValueError):
            offer(schemas, context_window=200_000, route_share=lambda _, bad=bad: bad)


def test_the_route_measures_the_standing_request_without_the_conversation():
    seen = []

    class Bounded:
        def request_capacity_ratio(self, *, messages, tools=None):
            seen.append((messages, tools))
            return 0.25

    messages = [
        {"role": "system", "content": "Οδηγίες"},
        {"role": "system", "content": "Περίληψη", "_bot_summary": 3},
        {"role": "user", "content": GREEK, "_history_task_id": "earlier"},
        {"role": "assistant", "content": "Έγινε."},
        {"role": "system", "content": "Κατάσταση", "_bot_task_state": True, "_memory_input": {}},
        {"role": "user", "content": JAPANESE},
    ]
    tools = _schemas(["ask_clarification"])
    assert route_share(Bounded(), messages)(tools) == 0.25
    assert seen == [
        (
            [
                {"role": "system", "content": "Οδηγίες"},
                {"role": "system", "content": "Κατάσταση"},
            ],
            tools,
        )
    ]
    assert route_share(object(), messages) is None

    # The engine's compatible client measures its hosted route and nothing else.
    def client(provider):
        return OpenAICompatClient(
            base_url="https://gateway.example.invalid/v1",
            api_key="synthetic-test-only",
            model="owl-route-fixture",
            provider_key=provider,
            transport=httpx.MockTransport(lambda _: httpx.Response(500)),
        )

    hosted = route_share(client("alysis"), messages)
    other = route_share(client("together"), messages)
    own = _schemas(tool_names(EVERY_FEATURE))
    assert 0 < hosted([]) < hosted(own) < 1
    assert other(own) == 0.0


def test_a_turn_keeps_its_lookup_once_it_has_used_it(tmp_path):
    own = _schemas(tool_names(OWN))
    connected, lines = connected_apps(tmp_path, 120)
    room = ToolRoom(1_000_000)
    first, note = room.offer([*own, *connected], apps=lines)
    assert first.mode == APPS_BY_LOOKUP
    assert note == {
        "mode": APPS_BY_LOOKUP,
        "reasons": [TOO_MANY_TOOLS],
        "tools": len(own) + 121,
        "direct": len(own) + 3,
        "by_lookup": 120,
        "schema_tokens": first.measures.schema_tokens,
        "context_window": 1_000_000,
        "route_share": 0.0,
    }
    # The same list again is the same offer, with nothing new to record.
    assert room.offer([*own, *connected], apps=lines) == (first, None)
    # Most app tools gone (switched off for the owl): under every limit, still by look-up.
    fewer = [*own, *connected[:4]]
    kept, note = room.offer(fewer, apps=lines)
    assert kept.mode == APPS_BY_LOOKUP and set(kept.hidden) == set(names_of(connected[1:4]))
    assert note["mode"] == APPS_BY_LOOKUP and note["reasons"] == []
    # A new turn measures afresh.
    assert ToolRoom(1_000_000).offer(fewer, apps=lines)[0].mode == FULL
    # A step without tools offers none and records nothing.
    nothing, note = ToolRoom(1_000_000).offer([], apps=lines)
    assert nothing.schemas == [] and not nothing.hidden and note is None


def _turn_kinds():
    every = EVERY_FEATURE
    return [
        Turn(answers_person=True),
        every,
        replace(every, answers_person=False, background=True),
        replace(every, answers_person=False, activation=True),
        replace(every, answers_person=False, scheduled=True),
        replace(every, room=True),
        replace(every, own_message=False, checking_back=True),
    ]


@pytest.mark.parametrize("app_count", [0, 3, 120])
def test_every_offered_schema_is_one_plain_object_in_every_mode(tmp_path, app_count):
    connected, lines = connected_apps(tmp_path, app_count) if app_count else ([], {})
    browser = [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": description,
                "parameters": model.model_json_schema(),
            },
        }
        for name, (model, description) in BROWSER_TOOLS.items()
    ]
    modes = set()
    for kind, with_browser, window, share in itertools.product(
        _turn_kinds(), (False, True), (SMALL_CONTEXT_WINDOW_TOKENS // 2, 1_000_000), (0.0, 0.9)
    ):
        schemas = [
            *_schemas(tool_names(kind)),
            *connected,
            *(browser if with_browser else []),
        ]
        made = offer(schemas, context_window=window, apps=lines, route_share=lambda _, s=share: s)
        modes.add(made.mode)
        offered = names_of(made.schemas)
        assert len(offered) == len(set(offered)) <= MAX_DIRECT_TOOLS
        for schema in made.schemas:
            parameters = schema["function"]["parameters"]
            assert parameters.get("type") == "object", schema["function"]["name"]
            assert not {"anyOf", "oneOf", "allOf"} & set(parameters), schema["function"]["name"]
        # Every tool is either listed or reachable, never lost.
        assert set(offered) - {LOOKUP_TOOL, CALL_THROUGH_TOOL} | set(made.hidden) == set(
            names_of(schemas)
        )
        if made.hidden:
            lookup, call_through = made.schemas[-2:]
            for reach in (lookup, call_through):
                enum = reach["function"]["parameters"]["properties"]["name"]["enum"]
                assert enum == list(made.hidden)
            for name in made.hidden:
                assert f"{name}: " in lookup["function"]["description"]
    # Few app tools are listed directly or with every other tool; many never all directly.
    assert modes == ({APPS_BY_LOOKUP, EVERYDAY} if app_count > 3 else {FULL, EVERYDAY})


# ---------------------------------------------------------------------------------------------
# Through the runtime


def response(text="", calls=()):
    return LLMResponse(text, list(calls), {}, usage=LLMUsage(10, 5, 15))


class Model:
    model = "toolset-fixture"
    supports_tool_calling = True

    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []

    def chat(self, **kwargs):
        self.requests.append(kwargs)
        return next(self.responses)

    def tools(self, index=0):
        return [t["function"]["name"] for t in self.requests[index].get("tools") or []]

    def results(self, index):
        return [m for m in self.requests[index]["messages"] if m.get("role") == "tool"]


def owl(tmp_path, *, others=0, memory=True):
    store = Store(tmp_path / "bots.db")
    bot = store.save_bot(OWNER, BotInput(name="Γλαύκα", purpose="Βοηθός", memory_enabled=memory))
    for n in range(others):
        store.save_bot(OWNER, BotInput(name=f"Άλλη {n}", purpose="Βοηθός"))
    return store, bot, store.create_chat(OWNER, bot["id"], "Chat")


def turn(store, chat, text, model):
    store.enqueue(OWNER, chat["id"], TaskInput(message=text, request_id=uuid4().hex))
    task = store.claim(OWNER)
    runtime = BotRuntime(store, OWNER, lambda _: model, limits=RuntimeLimits(calls_per_task=8))
    runtime.reply_review = None
    runtime.run(task, EventCancellationToken(threading.Event()))
    return store.task(OWNER, task["id"])


def test_the_words_of_the_message_never_change_the_tools(tmp_path):
    store, _, chat = owl(tmp_path)
    greek, japanese = Model([response("Εντάξει.")]), Model([response("了解です。")])
    turn(store, chat, GREEK, greek)
    turn(store, chat, JAPANESE, japanese)
    assert greek.requests[0]["tools"] == japanese.requests[0]["tools"]


def test_peer_tools_need_another_owl_to_ask(tmp_path):
    store, _, chat = owl(tmp_path / "alone")
    alone = Model([response("Εντάξει.")])
    turn(store, chat, GREEK, alone)
    assert not PEERS & set(alone.tools())
    store, _, chat = owl(tmp_path / "together", others=1)
    together = Model([response("Εντάξει.")])
    turn(store, chat, GREEK, together)
    assert PEERS <= set(together.tools())


def test_a_small_model_reaches_a_hidden_tool_through_the_same_path(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", str(SMALL_CONTEXT_WINDOW_TOKENS // 4))
    store, bot, chat = owl(tmp_path)
    model = Model(
        [
            response(calls=[ToolCall("look", LOOKUP_TOOL, {"name": "create_schedule"})]),
            response(
                calls=[
                    ToolCall(
                        "run",
                        CALL_THROUGH_TOOL,
                        {"name": "create_schedule", "arguments": ROUTINE_ARGUMENTS},
                    )
                ]
            ),
            response("Έτοιμο: η υπενθύμιση περιμένει να την ενεργοποιήσεις."),
        ]
    )
    task = turn(store, chat, GREEK, model)
    assert task["status"] == "completed"
    offered = model.tools()
    assert LOOKUP_TOOL in offered and CALL_THROUGH_TOOL in offered
    assert "create_schedule" not in offered and set(offered) - {LOOKUP_TOOL, CALL_THROUGH_TOOL} <= (
        EVERYDAY_TOOLS
    )
    (looked,) = model.results(1)
    assert json.loads(looked["content"]) == tool_schema("create_schedule")["function"]
    (ran,) = model.results(2)[1:]
    saved = json.loads(ran["content"])
    # The same receipt a direct call gets: a draft for the person to turn on.
    assert saved["state"] == "draft" and "It is not on yet" in saved["message"]
    started = [e["data"]["name"] for e in task["events"] if e["kind"] == "tool_started"]
    assert started == [LOOKUP_TOOL, "create_schedule"]
    assert [s["name"] for s in store.list_schedules(OWNER, bot["id"])] == ["Πότισμα"]


def test_a_call_through_keeps_the_checks_of_a_direct_call(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", str(SMALL_CONTEXT_WINDOW_TOKENS // 4))
    store, _, chat = owl(tmp_path, memory=False)
    model = Model(
        [
            # Memory is off, so memory_history is not reachable at all.
            response(
                calls=[
                    ToolCall("m", CALL_THROUGH_TOOL, {"name": "memory_history", "arguments": {}})
                ]
            ),
            # Invalid arguments fail validation exactly as a direct call would.
            response(
                calls=[
                    ToolCall(
                        "bad",
                        CALL_THROUGH_TOOL,
                        {"name": "create_schedule", "arguments": {"name": "Χωρίς χρόνο"}},
                    )
                ]
            ),
            # A call that must be alone is refused in a batch, as a direct one is.
            response(
                calls=[
                    ToolCall(
                        "bg",
                        CALL_THROUGH_TOOL,
                        {"name": "continue_in_background", "arguments": {"summary": "Δουλειά"}},
                    ),
                    ToolCall("ask", "ask_clarification", {"question": "Πότε;"}),
                ]
            ),
            response("Δεν έγινε τίποτα ακόμα."),
        ]
    )
    task = turn(store, chat, JAPANESE, model)
    assert task["status"] == "completed"
    (refused,) = model.results(1)
    assert "memory_history is not one of the tools" in json.loads(refused["content"])["error"]
    (invalid,) = model.results(2)[1:]
    assert json.loads(invalid["content"])["validation_errors"]
    alone = [json.loads(m["content"]) for m in model.results(3)[2:]]
    assert len(alone) == 2 and all(r["executed"] is False for r in alone)
    assert not store.list_schedules(OWNER, task["bot_id"])
    failed = [e["data"]["name"] for e in task["events"] if e["kind"] == "tool_failed"]
    assert failed == [CALL_THROUGH_TOOL, "create_schedule"]


class Route(Model):
    """A model on a route whose admission bound counts request bytes (None: no bound)."""

    def __init__(self, responses, bound=None):
        super().__init__(responses)
        self.bound = bound
        self.measured = []

    def request_capacity_ratio(self, *, messages, tools=None):
        size = len(json.dumps([messages, tools or []], ensure_ascii=False).encode())
        self.measured.append((size, [t["function"]["name"] for t in tools or []]))
        return 0.0 if self.bound is None else size / self.bound


def offered_events(task):
    return [e["data"] for e in task["events"] if e["kind"] == "tools_offered"]


@pytest.mark.parametrize("window", [400_000, 1_000_000])
def test_a_large_window_on_a_route_its_standing_request_fills_reaches_tools_by_lookup(
    tmp_path, monkeypatch, window
):
    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", str(window))
    # The same owl on a route without a bound of its own: every tool, as before.
    store, _, chat = owl(tmp_path / "unbounded")
    unbounded = Route([response("Εντάξει.")])
    task = turn(store, chat, GREEK, unbounded)
    assert task["status"] == "completed"
    every = unbounded.tools()
    assert "create_schedule" in every and LOOKUP_TOOL not in every
    (note,) = offered_events(task)
    assert note["mode"] == FULL and note["reasons"] == [] and note["route_share"] == 0.0
    assert note["tools"] == note["direct"] == len(every) and note["context_window"] == window
    # A route whose bound the standing request with every tool fills to 87%, as measured on a
    # hosted model with a smaller bound: the everyday tools, and the rest by look-up.
    full_size = next(size for size, names in unbounded.measured if names == every)
    store, _, chat = owl(tmp_path / "bounded")
    bounded = Route(
        [
            response(
                calls=[
                    ToolCall(
                        "run",
                        CALL_THROUGH_TOOL,
                        {"name": "create_schedule", "arguments": ROUTINE_ARGUMENTS},
                    )
                ]
            ),
            response("Έτοιμο."),
        ],
        bound=full_size / 0.87,
    )
    task = turn(store, chat, JAPANESE, bounded)
    assert task["status"] == "completed"
    offered = bounded.tools()
    assert LOOKUP_TOOL in offered and "create_schedule" not in offered
    assert set(offered) - {LOOKUP_TOOL, CALL_THROUGH_TOOL} <= EVERYDAY_TOOLS
    # Both steps of the turn: one decision, recorded once.
    assert bounded.tools(1) == offered
    (note,) = offered_events(task)
    assert note["mode"] == EVERYDAY and note["reasons"] == [ROUTE_OVER_SHARE]
    assert 0.8 < note["route_share"] < 0.95 and note["by_lookup"] == len(every) - len(offered) + 2
    started = [e["data"]["name"] for e in task["events"] if e["kind"] == "tool_started"]
    assert started == ["create_schedule"]
    (saved,) = bounded.results(1)
    assert json.loads(saved["content"])["state"] == "draft"


def test_a_small_window_turn_records_why_its_tools_are_by_lookup(tmp_path, monkeypatch):
    monkeypatch.setenv("ALYSIS_CONTEXT_WINDOW", str(SMALL_CONTEXT_WINDOW_TOKENS // 4))
    store, _, chat = owl(tmp_path)
    model = Route([response("了解です。")])
    task = turn(store, chat, JAPANESE, model)
    (note,) = offered_events(task)
    assert note["mode"] == EVERYDAY and note["reasons"][0] == SMALL_WINDOW
    assert note["context_window"] == SMALL_CONTEXT_WINDOW_TOKENS // 4
    assert set(model.tools()) - {LOOKUP_TOOL, CALL_THROUGH_TOOL} <= EVERYDAY_TOOLS


def test_offered_schemas_carry_no_titles_but_keep_parameters_called_title():
    """A model reads every offered schema on every step: pydantic's labels, which repeat each
    name, are left out, while a parameter that is itself called "title" stays."""

    def labels(node):
        if isinstance(node, list):
            return sum((labels(item) for item in node), [])
        if not isinstance(node, dict):
            return []
        found = ["title"] if isinstance(node.get("title"), str) else []
        for key, value in node.items():
            if key in ("properties", "$defs"):
                found += sum((labels(schema) for schema in value.values()), [])
            elif key not in ("default", "const", "enum", "examples"):
                found += labels(value)
        return found

    schemas = [tool_schema(name)["function"]["parameters"] for name in TOOL_DEFINITIONS]
    schemas += [tool_parameters(model) for model, _ in BROWSER_TOOLS.values()]
    schemas += [tool_parameters(model) for model, _ in DESKTOP_TOOLS.values()]
    assert all(labels(schema) == [] for schema in schemas)
    # Names stay: a "title" input, and data that happens to hold a "title" key.
    kept = without_titles(
        {
            "title": "Note",
            "properties": {"title": {"title": "Title", "type": "string"}},
            "default": {"title": "Πρόχειρο"},
        }
    )
    assert kept == {"properties": {"title": {"type": "string"}}, "default": {"title": "Πρόχειρο"}}
