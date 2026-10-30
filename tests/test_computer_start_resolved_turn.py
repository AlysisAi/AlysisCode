"""Raw synthetic HTTP/SSE through actual clients and production run_turn."""

import copy
import json
from pathlib import Path

import httpx
import pytest
from test_computer_use_skill import isolated_paths as isolated_paths

from alysis_code import token_budget
from alysis_code.agent.browser_start import BrowserStartHandoff
from alysis_code.agent.session import create_session
from alysis_code.config import AppConfig
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

CORPUS = json.loads(
    (Path(__file__).parent / "fixtures/computer_use_start_resolved_v1.json").read_text(
        encoding="utf-8"
    )
)
NAME = "computer_start_resolved_v1"


def body(protocol, stream, args, *, duplicate_parent=False):
    if protocol == "responses":
        call = {
            "type": "function_call",
            "id": "fc-1",
            "call_id": "call-1",
            "name": NAME,
            "arguments": args,
        }
        final = {"id": "resp-fixture", "status": "completed", "output": [call]}
        if stream:
            events = [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**call, "arguments": ""},
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 0,
                    "delta": args,
                },
                {"type": "response.completed", "response": final},
            ]
            raw = "".join("data: " + json.dumps(e) + "\n\n" for e in events)
        else:
            raw = json.dumps(final)
    else:
        call = {"id": "call-1", "type": "function", "function": {"name": NAME, "arguments": args}}
        if stream:
            events = [
                {
                    "choices": [
                        {"delta": {"tool_calls": [{**call, "index": 0}]}, "finish_reason": None}
                    ]
                },
                {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            ]
            raw = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
        else:
            raw = json.dumps(
                {
                    "choices": [
                        {
                            "message": {"role": "assistant", "content": None, "tool_calls": [call]},
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            )
    if duplicate_parent:
        raw = raw.replace('"name":', '"name":"other","name":')
    return raw.encode()


def session_for(tmp_path, monkeypatch, protocol, stream, outputs, *, auto_invoke=True):
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    calls = []
    requests = []

    def handler(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        requests.append(json.loads(request.content))
        value = outputs[min(len(requests) - 1, len(outputs) - 1)]
        value = value.replace(b"call-1", f"call-{len(requests)}".encode()).replace(
            b"fc-1", f"fc-{len(requests)}".encode()
        )
        return httpx.Response(
            200,
            content=value,
            headers={"content-type": "text/event-stream" if stream else "application/json"},
        )

    cls = OpenAIResponsesClient if protocol == "responses" else OpenAICompatClient
    client = cls(
        base_url="https://fixture.invalid/v1",
        api_key="fixture",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    # Factory port is fake before session construction, actual parser/client remains intact.
    monkeypatch.setattr("alysis_code.agent.session._make_session_llm_client", lambda **_: client)
    session = create_session(
        cfg=AppConfig(
            model="test-model",
            routing_mode="code_only",
            web_search_mode="off",
            stream=stream,
            skills_auto_invoke=auto_invoke,
        ),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=4,
        no_log=True,
        api_key_override="fixture",
        surface=NoopSurface(),
        computer_start_resolved_host=copy.deepcopy(CORPUS["bases"]["descriptor"]),
        computer_start_resolved_request=lambda p, s, c: (
            calls.append((p, s, c)) or BrowserStartHandoff("fixture")
        ),
    )
    return session, calls, requests


@pytest.mark.parametrize("protocol", ["responses", "compat"])
@pytest.mark.parametrize("stream", [False, True])
def test_real_raw_provider_to_run_turn_handoff(tmp_path, monkeypatch, protocol, stream):
    plan = CORPUS["bases"]["plan"]
    session, calls, requests = session_for(
        tmp_path, monkeypatch, protocol, stream, [body(protocol, stream, json.dumps(plan))]
    )
    assert session.run_turn("Read the authorized Alpha view without editing.") == 0
    assert len(calls) == 1
    assert calls[0][:2] == (plan, CORPUS["bases"]["selection"])
    assert len(requests) == 1
    assert session.last_turn_outcome["reason"] == "browser_start_handoff"
    serialized = json.dumps(requests[0])
    assert NAME in serialized
    from alysis_code.agent.browser_start import COMPUTER_ACTIONS

    schema = session.tools[NAME].as_openai_tool()["function"]["parameters"]
    assert schema["properties"]["app_steps"]["items"]["properties"]["actions"]["items"][
        "enum"
    ] == list(COMPUTER_ACTIONS)
    assert "a preparatory skill read is not required" not in serialized
    assert not any("image_url" in str(r) for r in requests)
    session.close()


@pytest.mark.parametrize("protocol", ["responses", "compat"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("bad", ["duplicate", "parent", "object", "size"])
def test_real_raw_invalid_never_dispatches(tmp_path, monkeypatch, protocol, stream, bad):
    args = json.dumps(CORPUS["bases"]["plan"])
    if bad == "duplicate":
        args = args.replace('"goal":', '"goal":"hidden","goal":')
    if bad == "object":
        args = CORPUS["bases"]["plan"]
    if bad == "size":
        args += " " * 131073
    session, calls, _ = session_for(
        tmp_path,
        monkeypatch,
        protocol,
        stream,
        [body(protocol, stream, args, duplicate_parent=bad == "parent")],
    )
    try:
        session.run_turn("Read Alpha.")
    except (ValueError, __import__("alysis_code.llm.types", fromlist=["LLMError"]).LLMError):
        pass
    assert not calls
    session.close()


@pytest.mark.parametrize("protocol", ["responses", "compat"])
@pytest.mark.parametrize("stream", [False, True])
def test_actual_next_request_has_same_capture_correction(tmp_path, monkeypatch, protocol, stream):
    plan = CORPUS["bases"]["plan"]
    bad = copy.deepcopy(plan)
    bad["app_steps"][0]["app"] = "Unknown"
    session, calls, requests = session_for(
        tmp_path,
        monkeypatch,
        protocol,
        stream,
        [body(protocol, stream, json.dumps(bad)), body(protocol, stream, json.dumps(plan))],
    )
    session.run_turn("Read Alpha.")
    assert len(calls) == 1
    assert len(requests) == 2
    second = json.dumps(requests[1])
    assert "available_targets" in second and "Alpha" in second
    assert "Call computer_discover now" not in second
    assert "computer_start_resolved_v1" in second
    session.close()


def test_production_privacy_missing_proof_and_no_screenshots(tmp_path, monkeypatch):
    plan = copy.deepcopy(CORPUS["bases"]["plan"])
    plan["goal"] = "PRIVATE_GOAL_CANARY"
    plan["constraints"] = ["PRIVATE_CONSTRAINT_CANARY"]
    session, calls, requests = session_for(
        tmp_path, monkeypatch, "compat", False, [body("compat", False, json.dumps(plan))]
    )
    durable = []
    display = []
    monkeypatch.setattr(
        session.store, "append", lambda *a, **kw: durable.append(copy.deepcopy((a, kw)))
    )
    monkeypatch.setattr(
        session.surface, "emit_tool_call_started", lambda *a, **kw: display.append((a, kw))
    )
    monkeypatch.setattr(session.surface, "on_tool_start", lambda event: display.append(repr(event)))
    session.run_turn("Read the authorized view.")
    assert len(calls) == 1
    for value in [durable, display, session.messages]:
        assert "PRIVATE_GOAL_CANARY" not in str(value)
        assert "PRIVATE_CONSTRAINT_CANARY" not in str(value)
        assert "_resolved_entry_proof" not in str(value)
    assert session.resolved_entry.descriptor is None
    assert not any("image_url" in str(request) for request in requests)
    session.close()


@pytest.mark.parametrize("protocol", ["responses", "compat"])
def test_missing_proof_fails_in_production_dispatch(tmp_path, monkeypatch, protocol):
    from alysis_code.llm import resolved_entry

    session, calls, requests = session_for(
        tmp_path,
        monkeypatch,
        protocol,
        False,
        [body(protocol, False, json.dumps(CORPUS["bases"]["plan"]))],
    )
    monkeypatch.setattr(resolved_entry, "arguments", lambda raw, **kw: (json.loads(raw), None))
    assert session.run_turn("Read the authorized view.") == 0
    assert not calls and len(requests) == 1
    assert session.last_turn_outcome["reason"] == "computer_start_response_invalid"
    session.close()


@pytest.mark.parametrize("change", ["client", "model", "stream"])
def test_pre_request_mutation_never_contacts_provider(tmp_path, monkeypatch, change):
    from alysis_code.llm.types import LLMError

    session, calls, requests = session_for(
        tmp_path,
        monkeypatch,
        "compat",
        False,
        [body("compat", False, json.dumps(CORPUS["bases"]["plan"]))],
    )
    if change == "client":
        session.client = OpenAICompatClient(
            base_url="https://fixture.invalid",
            api_key="fixture",
            model="test-model",
            transport=httpx.MockTransport(lambda _: pytest.fail("unexpected HTTP")),
        )
    elif change == "model":
        session.cfg.model = "changed"
    else:
        session.stream = True
    with pytest.raises(LLMError, match="computer_start_capability_lost"):
        session.run_turn("Read the authorized view.")
    assert not requests and not calls
    assert session.resolved_entry.descriptor is None
    session.close()


def test_production_correction_exhaustion_never_discovers(tmp_path, monkeypatch):
    p = copy.deepcopy(CORPUS["bases"]["plan"])
    p["app_steps"][0]["app"] = "Unknown"
    session, calls, requests = session_for(
        tmp_path, monkeypatch, "compat", False, [body("compat", False, json.dumps(p))]
    )
    session.run_turn("Read the authorized view.")
    assert not calls and len(requests) <= 3
    assert session.last_turn_outcome["reason"] == "computer_selection_recovery_exhausted"
    assert "Call computer_discover now" not in str(requests)
    session.close()


@pytest.mark.parametrize("kind", ["custom", "cumulative", "instance_override"])
def test_actual_client_eligibility_before_tool_assembly(tmp_path, monkeypatch, kind):
    from types import SimpleNamespace

    from alysis_code.agent.computer_start_resolved import ProtocolUnavailable

    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    client = OpenAICompatClient(
        base_url="https://fixture.invalid",
        api_key="fixture",
        model="test-model",
        transport=httpx.MockTransport(lambda _: pytest.fail("unexpected network")),
    )
    if kind == "custom":
        client = SimpleNamespace()
    elif kind == "cumulative":
        client.stream_assembly = "cumulative"
    else:
        client.chat = lambda **_: pytest.fail("unqualified client used")
    monkeypatch.setattr("alysis_code.agent.session._make_session_llm_client", lambda **_: client)
    with pytest.raises(ProtocolUnavailable, match="selected model protocol"):
        create_session(
            cfg=AppConfig(model="test-model", routing_mode="code_only", web_search_mode="off"),
            root=tmp_path,
            mode="review",
            yes=False,
            max_steps=2,
            no_log=True,
            api_key_override="fixture",
            surface=NoopSurface(),
            computer_start_resolved_host=copy.deepcopy(CORPUS["bases"]["descriptor"]),
            computer_start_resolved_request=lambda *_: pytest.fail("host reached"),
        )


@pytest.mark.parametrize("auto_invoke", [False, True])
def test_explicit_skill_read_remains_visible_on_v_route(tmp_path, monkeypatch, auto_invoke):
    first = body("compat", False, json.dumps({"name": "computer-use"})).replace(
        NAME.encode(), b"skill_read"
    )
    second = body("compat", False, json.dumps(CORPUS["bases"]["plan"]))
    session, calls, requests = session_for(
        tmp_path, monkeypatch, "compat", False, [first, second], auto_invoke=auto_invoke
    )
    assert session.skills_auto_invoke is auto_invoke
    session.run_turn("Read the computer-use skill, then read the authorized view without editing.")
    assert len(requests) == 2 and len(calls) == 1
    assert "skill_read" in json.dumps(requests[1])
    assert "computer_start_resolved_v1" in json.dumps(requests[1])
    assert "a preparatory skill read is not required" not in json.dumps(requests)
    session.close()
