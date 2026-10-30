"""Frozen V producer boundary; synthetic raw HTTP only, no provider ports."""

import json

import httpx
import pytest

from alysis_code.llm import resolved_entry
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient

NAME = "computer_start_resolved_v1"
PLAN = {
    "goal": "Read Alpha.",
    "constraints": ["Do not edit."],
    "operates_apps": False,
    "app_steps": [{"app": "Alpha", "actions": []}],
}


def response(protocol, arguments):
    call = {"id": "call-1", "type": "function", "function": {"name": NAME, "arguments": arguments}}
    data = {"choices": [{"message": {"role": "assistant", "content": None, "tool_calls": [call]}}]}
    if protocol == "responses":
        data = {
            "status": "completed",
            "output": [
                {"type": "function_call", "call_id": "call-1", "name": NAME, "arguments": arguments}
            ],
        }
    return httpx.Response(200, content=json.dumps(data).encode())


def parse(protocol, raw):
    if protocol == "responses":
        return OpenAIResponsesClient._parse_chat_response(raw)
    return OpenAICompatClient._parse_non_stream_response(raw, provider_key=None)


@pytest.mark.parametrize("protocol", ["responses", "compat"])
def test_raw_proof_is_owned_exact_and_not_reusable(protocol):
    owner = object()
    with resolved_entry.parser_scope(owner):
        call = parse(protocol, response(protocol, json.dumps(PLAN))).tool_calls[0]
    assert resolved_entry.valid_proof(call, owner, call.arguments)
    assert not resolved_entry.valid_proof(call, object(), call.arguments)
    call.arguments["goal"] = "Changed"
    assert not resolved_entry.valid_proof(call, owner, call.arguments)


@pytest.mark.parametrize("protocol", ["responses", "compat"])
@pytest.mark.parametrize(
    "arguments",
    [
        '{"goal":"first","goal":"last","constraints":[],"operates_apps":false,"app_steps":[]}',
        json.dumps(PLAN) + " " * 131073,
        "[" * 17 + "0" + "]" * 17,
        {"goal": "decoded object"},
        '{"goal":"\\ud800"}',
    ],
    ids=["duplicate", "size", "depth", "object", "surrogate"],
)
def test_raw_invalid_has_no_dispatch_proof(protocol, arguments):
    owner = object()
    with resolved_entry.parser_scope(owner):
        call = parse(protocol, response(protocol, arguments)).tool_calls[0]
    assert not resolved_entry.valid_proof(call, owner, call.arguments)
    assert call.arguments == {}, "invalid raw content must not enter history or diagnostics"


@pytest.mark.parametrize("protocol", ["responses", "compat"])
def test_parent_duplicates_not_erased(protocol):
    raw = response(protocol, json.dumps(PLAN)).content
    raw = raw.replace(b'"name":', b'"name":"other","name":')
    owner = object()
    with resolved_entry.parser_scope(owner):
        call = parse(protocol, httpx.Response(200, content=raw)).tool_calls[0]
    assert not resolved_entry.valid_proof(call, owner, call.arguments)


def test_unnegotiated_legacy_parse_unchanged():
    call = parse("compat", response("compat", '{"a":1,"a":2}')).tool_calls[0]
    assert call.arguments == {"a": 2}


@pytest.mark.parametrize("protocol", ["responses", "compat"])
def test_invalid_utf8_is_not_replaced_in_v_provider_response(protocol):
    raw = response(protocol, json.dumps(PLAN)).content.replace(b"Alpha", b"\xffAlpha")
    with resolved_entry.parser_scope(object()), pytest.raises((ValueError, RuntimeError)):
        parse(protocol, httpx.Response(200, content=raw))


@pytest.mark.parametrize(
    "protocol,selector",
    [
        ("compat", "name"),
        ("compat", "id"),
        ("responses", "name"),
        ("responses", "call_id"),
        ("responses", "id"),
    ],
)
@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("bound", [False, True])
def test_stream_selectors_are_stable_only_in_v_owner(protocol, selector, changed, bound):
    from contextlib import nullcontext

    args = json.dumps(PLAN)
    first = {"id": "fc-1", "call_id": "call-1", "name": NAME}
    if changed:
        first[selector] = "previous"
    if protocol == "responses":
        final = {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": NAME,
            "arguments": args,
        }
        events = [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"type": "function_call", **first, "arguments": ""},
            },
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": args},
            {"type": "response.output_item.done", "output_index": 0, "item": final},
            {"type": "response.completed", "response": {"status": "completed", "output": [final]}},
        ]
    else:
        events = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": first["id"],
                                    "function": {"name": first["name"], "arguments": args[:20]},
                                }
                            ]
                        }
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "fc-1",
                                    "function": {"name": NAME, "arguments": args[20:]},
                                }
                            ]
                        }
                    }
                ]
            },
        ]
    raw = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
    if protocol == "compat":
        raw += "data: [DONE]\n\n"
    cls = OpenAIResponsesClient if protocol == "responses" else OpenAICompatClient
    client = cls(
        base_url="https://fixture.invalid",
        api_key="fixture",
        model="fixture",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=raw.encode(), headers={"content-type": "text/event-stream"}
            )
        ),
    )
    owner = object()
    with resolved_entry.parser_scope(owner) if bound else nullcontext():
        call = client.chat(
            messages=[{"role": "user", "content": "fixture"}], stream=True
        ).tool_calls[0]
    assert resolved_entry.valid_proof(call, owner, call.arguments) is (bound and not changed)
    assert call.arguments == ({} if bound and changed else PLAN)


@pytest.mark.parametrize("selector", ["name", "call_id", "id"])
@pytest.mark.parametrize("stage", ["done", "final", "arguments_done"])
def test_responses_keeps_first_nonempty_selector_across_empty_update(selector, stage):
    from alysis_code.llm.openai_responses import _OpenAIResponsesStreamAccumulator

    acc = _OpenAIResponsesStreamAccumulator(on_text_delta=None, on_reasoning_delta=None)
    initial = {"type": "function_call", "name": NAME, "call_id": "call-1", "id": "fc-1"}
    changed = {**initial, selector: "changed"}
    owner = object()
    with resolved_entry.parser_scope(owner):
        acc._set_output_item(0, initial)
        acc._set_output_item(0, {selector: ""})
        if stage == "done":
            acc._handle_output_item(
                {"type": "response.output_item.done", "output_index": 0, "item": changed}
            )
        elif stage == "final":
            acc._handle_final_response({"response": {"output": [changed]}})
        else:
            if selector == "id":
                changed["item_id"] = changed.pop("id")
            acc._handle_function_arguments_done({"output_index": 0, **changed})
        call = parse("responses", response("responses", json.dumps(PLAN))).tool_calls[0]
    assert not resolved_entry.valid_proof(call, owner, call.arguments)
