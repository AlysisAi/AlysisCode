"""Regression coverage adapted from the Claude S2b review: resolved entry through the real bridge and real session.

Real StdioBridge.process_line, real create_session, real run_turn, actual Responses /
OpenAI-compatible clients over raw synthetic HTTP/SSE (httpx MockTransport), and a host
responder that answers computer_start_resolved.respond through process_line. Only the
client factory is a port. Canary labels/goal/constraints/nonces are then searched in every
file under the test root, in stdout (minus the two private owner channels) and in stderr.
"""

from __future__ import annotations

import copy
import io
import json
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from test_computer_start_resolved_turn import CORPUS, body
from test_computer_use_skill import isolated_paths as isolated_paths

from alysis_code import token_budget
from alysis_code.config import AppConfig
from alysis_code.ide import stdio_bridge
from alysis_code.llm.openai_compat import OpenAICompatClient
from alysis_code.llm.openai_responses import OpenAIResponsesClient

NAME = "computer_start_resolved_v1"
APP = "ZetaCanaryApp"
DUP = "DupCanaryApp"
GOAL = "GOALCANARY read the zeta window 7f3a"
CONSTRAINT = "CONSTRAINTCANARY never edit 91b2"


@pytest.fixture(autouse=True)
def mac_w7_bridge_harness(monkeypatch):
    from alysis_code import computer_use_platform_policy as policy

    monkeypatch.setattr(policy, "sys", SimpleNamespace(platform="darwin"))


def descriptor():
    d = copy.deepcopy(CORPUS["bases"]["descriptor"])
    t = d["discovery"]["targets"]
    t[0]["name"] = APP
    t[1]["name"] = DUP
    t.append({"target_id": "10000000-0000-4000-8000-000000000004", "name": DUP, "route": "native"})
    return d


def plan(app=APP, actions=()):
    return json.dumps(
        {
            "goal": GOAL,
            "constraints": [CONSTRAINT],
            "operates_apps": False,
            "app_steps": [{"app": app, "actions": list(actions)}],
        },
        separators=(",", ":"),
    )


def text_body(protocol, stream, text="Finished."):
    if protocol == "responses":
        final = {
            "id": "resp-t",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": text}],
                }
            ],
        }
        if not stream:
            return json.dumps(final).encode()
        events = [
            {"type": "response.output_text.delta", "output_index": 0, "delta": text},
            {"type": "response.completed", "response": final},
        ]
        return "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
    if not stream:
        return json.dumps(
            {
                "choices": [
                    {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
                ]
            }
        ).encode()
    events = [
        {"choices": [{"delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    return (
        "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
    ).encode()


class Out(io.StringIO):
    def lines(self):
        return [json.loads(x) for x in self.getvalue().splitlines() if x.strip()]


def run(tmp_path, monkeypatch, protocol, stream, outputs, decide, *, on_request=None, desc=None):
    """outputs: raw bodies per model request; decide(event, n) -> response params tail or None."""
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    # Synthetic credential for config resolution only; the client is a MockTransport port.
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-not-a-key")
    requests = []
    parser_scopes = []

    def handler(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        from alysis_code.llm import resolved_entry

        parser_scopes.append(resolved_entry.active())
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
    monkeypatch.setattr("alysis_code.agent.session._make_session_llm_client", lambda **_: client)
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(
            model="test-model",
            routing_mode="code_only",
            web_search_mode="off",
            stream=stream,
            subagents_enabled=False,
        ),
    )
    out = Out()
    bridge = stdio_bridge.StdioBridge(stdout=out)
    monkeypatch.setattr(
        bridge, "_managed_browser_factory", lambda **kw: SimpleNamespace(close_all=lambda: None)
    )
    workspace = tmp_path / "ws"
    workspace.mkdir()
    desc = desc or descriptor()
    bridge.process_line(
        json.dumps(
            {
                "protocol_version": "1",
                "id": "create",
                "method": "session.create",
                "params": {
                    "session_id": "s2b-e2e",
                    "workspace": str(workspace),
                    "mode": "review",
                    "computer_start_resolved_host": desc,
                },
            }
        )
    )
    created = next(x for x in out.lines() if x.get("id") == "create")
    receipt = {
        "createOk": created.get("ok"),
        "createError": created.get("error"),
        "echoExact": (created.get("result") or {}).get("computer_start_resolved_host") == desc,
        "hostRequests": [],
        "acks": [],
        "parserScopes": parser_scopes,
    }
    if not created.get("ok"):
        bridge.close()
        return receipt, out, requests, bridge
    stop = threading.Event()

    def responder():
        seen = 0
        while not stop.is_set():
            events = [x for x in out.lines() if x.get("type") == "computer_start_resolved_request"]
            for event in events[seen:]:
                seen += 1
                receipt["hostRequests"].append({k: v for k, v in event["payload"].items()})
                if on_request is not None:
                    on_request(bridge, event, seen)
                tail = decide(event, seen)
                if tail is None:
                    continue
                identities = {
                    k: v for k, v in event["payload"].items() if k not in {"plan", "selection"}
                }
                params = {
                    **identities,
                    "session_id": event["session_id"],
                    "job_id": event["job_id"],
                    **tail,
                }
                rid = f"respond-{seen}"
                bridge.process_line(
                    json.dumps(
                        {
                            "protocol_version": "1",
                            "id": rid,
                            "method": "computer_start_resolved.respond",
                            "params": params,
                        }
                    )
                )
                receipt["acks"].append(next(x for x in out.lines() if x.get("id") == rid))
            time.sleep(0.005)

    thread = threading.Thread(target=responder, daemon=True)
    thread.start()
    bridge.process_line(
        json.dumps(
            {
                "protocol_version": "1",
                "id": "send",
                "method": "chat.send",
                "params": {
                    "session_id": "s2b-e2e",
                    "message": "Please look at the requested window.",
                    "idempotency_key": "k1",
                },
            }
        )
    )
    sent = next(x for x in out.lines() if x.get("id") == "send")
    receipt["sendOk"] = sent.get("ok")
    job = bridge._jobs[sent["result"]["job_id"]]
    deadline = time.monotonic() + 40
    while job.status not in stdio_bridge.FINAL_JOB_STATUSES and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    thread.join(2)
    handoffs = [
        x["payload"]["browser_start_handoff"]
        for x in out.lines()
        if x.get("type") == "info_emitted" and "browser_start_handoff" in (x.get("payload") or {})
    ]
    receipt.update(
        jobStatus=job.status,
        handoff=bool(handoffs),
        handoffs=handoffs,
        handoffMatchesRequest=[h["request_id"] for h in handoffs]
        == [r["request_id"] for r in receipt["hostRequests"]][-len(handoffs) :]
        if handoffs
        else None,
        modelRequests=len(requests),
        replayBuffers=str(bridge._event_buffers),
    )
    agent = bridge._sessions["s2b-e2e"].agent_session
    receipt["entryRetired"] = getattr(getattr(agent, "resolved_entry", None), "retired", "n/a")
    receipt["toolsOffered"] = sorted(
        {
            (t.get("name") or t.get("function", {}).get("name"))
            for r in requests
            for t in r.get("tools", [])
        }
    )
    return receipt, out, requests, bridge


def leaks(tmp_path, out, capfd, desc, replay):
    """Every canary occurrence outside the two private owner channels."""
    canaries = {
        APP,
        DUP,
        GOAL,
        CONSTRAINT,
        "GOALCANARY",
        "CONSTRAINTCANARY",
        desc["inventory_nonce"],
        desc["generation_nonce"],
        desc["host_nonce"],
        *(t["target_id"] for t in desc["discovery"]["targets"]),
    }
    found = []
    for path in tmp_path.rglob("*"):
        if path.is_file():
            data = path.read_bytes()
            for c in canaries:
                if c.encode() in data or json.dumps(c)[1:-1].encode() in data:
                    found.append(("file", str(path.relative_to(tmp_path)), c))
    for line in out.lines():
        if line.get("type") == "computer_start_resolved_request" or line.get("id") == "create":
            continue  # private owner channels: host request event and session.create echo
        if line.get("type") == "info_emitted" and "browser_start_handoff" in (
            line.get("payload") or {}
        ):
            continue  # existing transient private handoff channel (same shape as legacy browser_start)
        text = json.dumps(line, ensure_ascii=False)
        found += [("stdout", line.get("type") or line.get("id"), c) for c in canaries if c in text]
    err = capfd.readouterr().err
    found += [("replay", "", c) for c in canaries if c in replay]
    found += [("stderr", "", c) for c in canaries if c in err]
    return sorted(set(found))


def _show(name, receipt, extra=None):
    shown = {
        k: v
        for k, v in {**receipt, **(extra or {})}.items()
        if k not in {"replayBuffers", "toolsOffered"}
    }
    shown["toolsOfferedEntry"] = [
        t
        for t in receipt.get("toolsOffered", [])
        if "computer" in t or "browser_start" in t or t == "skill_read"
    ]
    print(f"E2E {name} " + json.dumps(shown, sort_keys=True, default=repr)[:3000])


@pytest.mark.parametrize("protocol", ["responses", "compat"])
@pytest.mark.parametrize("stream", [False, True])
def test_e2e_direct_handoff(tmp_path, monkeypatch, capfd, protocol, stream):
    receipt, out, requests, bridge = run(
        tmp_path,
        monkeypatch,
        protocol,
        stream,
        [body(protocol, stream, plan())],
        lambda e, n: {"allow": True},
    )
    try:
        found = leaks(tmp_path, out, capfd, descriptor(), receipt["replayBuffers"])
        _show(f"direct-{protocol}-{stream}", receipt, {"leaks": found})
        assert receipt["createOk"] and receipt["echoExact"] and receipt["sendOk"]
        assert len(receipt["hostRequests"]) == 1 and receipt["handoff"] is True
        payload = receipt["hostRequests"][0]
        assert set(payload) == {
            "version",
            "request_id",
            "tool_call_id",
            "operation_id",
            "host_nonce",
            "generation_nonce",
            "inventory_nonce",
            "plan",
            "selection",
        }
        assert payload["selection"] == {
            "target_ids": ["10000000-0000-4000-8000-000000000001"],
            "allowed_actions": [],
            "goal": GOAL,
            "constraints": [CONSTRAINT],
        }
        assert receipt["acks"][0]["result"] == {
            "status": "accepted",
            "request_id": payload["request_id"],
        }
        assert NAME in receipt["toolsOffered"]
        assert not {"computer_discover", "computer_start"} & set(receipt["toolsOffered"])
        assert found == []
    finally:
        bridge.close()


@pytest.mark.parametrize("protocol", ["responses", "compat"])
def test_e2e_correction_then_handoff(tmp_path, monkeypatch, capfd, protocol):
    outputs = [
        body(protocol, True, plan("Nope")),
        body(protocol, True, plan(DUP)),
        body(protocol, True, plan()),
    ]
    receipt, out, requests, bridge = run(
        tmp_path, monkeypatch, protocol, True, outputs, lambda e, n: {"allow": True}
    )
    try:
        found = leaks(tmp_path, out, capfd, descriptor(), receipt["replayBuffers"])
        delivered = [json.dumps(r, ensure_ascii=False) for r in requests]
        _show(
            f"correction-{protocol}",
            receipt,
            {
                "leaks": found,
                "correctionInRequest": [
                    ("selection_correction" in d, d.count(DUP)) for d in delivered
                ],
            },
        )
        assert (
            len(receipt["hostRequests"]) == 1 and receipt["handoff"] is True and len(requests) == 3
        )
        assert (
            "computer_start_invalid_selection" in delivered[1]
            and "computer_start_ambiguous_selection" in delivered[2]
        )
        assert found == []
    finally:
        bridge.close()


def test_e2e_w3_then_failed_identity_is_blocked_locally(tmp_path, monkeypatch, capfd):
    outputs = [
        body("responses", True, plan()),
        body("responses", True, plan(APP, ["activate_application"])),
        text_body("responses", True),
    ]

    def decide(event, n):
        return {
            "allow": False,
            "code": "computer_use_target_unverified",
            "unverified_target_ids": ["10000000-0000-4000-8000-000000000001"],
        }

    receipt, out, requests, bridge = run(tmp_path, monkeypatch, "responses", True, outputs, decide)
    try:
        found = leaks(tmp_path, out, capfd, descriptor(), receipt["replayBuffers"])
        delivered = [json.dumps(r, ensure_ascii=False) for r in requests]
        outputs_seen = [
            [
                i.get("output")
                for i in r.get("input", [])
                if isinstance(i, dict) and i.get("type") == "function_call_output"
            ]
            for r in requests
        ]
        calls_seen = [
            [
                (i.get("name"), i.get("call_id"))
                for i in r.get("input", [])
                if isinstance(i, dict) and i.get("type") == "function_call"
            ]
            for r in requests
        ]
        _show(
            "w3",
            receipt,
            {
                "leaks": found,
                "requests": len(requests),
                "toolOutputsPerRequest": outputs_seen,
                "toolCallsPerRequest": calls_seen,
            },
        )
        assert (
            len(receipt["hostRequests"]) == 1 and receipt["handoff"] is False and len(requests) == 3
        )
        assert "computer_use_target_unverified" in delivered[1]
        assert '"code": "computer_use_target_unverified"' in json.dumps(
            outputs_seen[2]
        ) or "computer_use_target_unverified" in json.dumps(
            outputs_seen[2]
        )  # local refusal, no 2nd host request
        assert receipt["jobStatus"] == "completed" and found == []
    finally:
        bridge.close()


def test_e2e_in_flight_capability_loss(tmp_path, monkeypatch, capfd):
    """Config mutation while the host exchange is pending (V.4 table: capability_lost, unknown, fresh)."""
    tool_results = []

    def mutate(bridge, event, n):
        agent = bridge._sessions["s2b-e2e"].agent_session
        agent.cfg = copy.deepcopy(agent.cfg)
        agent.cfg.model = "mutated-model"

    receipt, out, requests, bridge = run(
        tmp_path,
        monkeypatch,
        "responses",
        False,
        [body("responses", False, plan()), text_body("responses", False)],
        lambda e, n: None,
        on_request=mutate,
    )
    try:
        agent = bridge._sessions["s2b-e2e"].agent_session
        for m in agent.messages:
            if m.get("role") == "tool":
                tool_results.append(m.get("content"))
        _show(
            "inflight-capability-loss",
            receipt,
            {"toolResults": tool_results, "finalEvents": [x.get("type") for x in out.lines()][-6:]},
        )
        assert len(receipt["hostRequests"]) == 1 and receipt["handoff"] is False
        assert receipt["entryRetired"] == "computer_start_capability_lost"
    finally:
        bridge.close()


@pytest.mark.parametrize("protocol", ["responses", "compat"])
@pytest.mark.parametrize("selector", ["name", "call_id"])
def test_e2e_stream_name_switch(tmp_path, monkeypatch, capfd, protocol, selector):
    """The call's name selector changes mid-stream (other -> V). Report whether V still dispatches."""
    args = plan()
    half = len(args) // 2
    if protocol == "compat":
        events = [
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call-1",
                                    "type": "function",
                                    "function": {"name": "read_file", "arguments": args[:half]},
                                }
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "function": {"name": NAME, "arguments": args[half:]}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        raw = (
            "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
        ).encode()
    else:
        call = {
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
                "item": {**call, "name": "read_file", "arguments": ""},
            },
            {"type": "response.function_call_arguments.delta", "output_index": 0, "delta": args},
            {"type": "response.output_item.done", "output_index": 0, "item": call},
            {
                "type": "response.completed",
                "response": {"id": "r", "status": "completed", "output": [call]},
            },
        ]
        raw = "".join("data: " + json.dumps(e) + "\n\n" for e in events).encode()
    if selector == "call_id":
        raw = raw.replace(b'"name": "read_file"', b'"name": "computer_start_resolved_v1"')
        if protocol == "compat":
            raw = raw.replace(b'"id": "call-1"', b'"id": "call-changed"', 1)
            raw = raw.replace(
                b'"index": 0, "function":', b'"index": 0, "id": "call-1", "function":'
            )
        else:
            raw = raw.replace(b'"call_id": "call-1"', b'"call_id": "call-changed"', 1)
    receipt, out, requests, bridge = run(
        tmp_path,
        monkeypatch,
        protocol,
        True,
        [raw, text_body(protocol, True)],
        lambda e, n: {"allow": True},
    )
    try:
        _show(f"name-switch-{protocol}", receipt)
        assert receipt["hostRequests"] == [] and receipt["handoff"] is False
    finally:
        bridge.close()


@pytest.mark.parametrize("second", [NAME, "browser_start"])
def test_e2e_two_calls_one_response(tmp_path, monkeypatch, capfd, second):
    args = plan()
    calls = [
        {
            "type": "function_call",
            "id": "fc-a",
            "call_id": "call-a",
            "name": NAME,
            "arguments": args,
        },
        {
            "type": "function_call",
            "id": "fc-b",
            "call_id": "call-b",
            "name": second,
            "arguments": args
            if second == NAME
            else json.dumps({"url": "https://example.invalid/"}),
        },
    ]
    raw = json.dumps({"id": "r", "status": "completed", "output": calls}).encode()
    receipt, out, requests, bridge = run(
        tmp_path,
        monkeypatch,
        "responses",
        False,
        [raw, text_body("responses", False)],
        lambda e, n: {"allow": True},
    )
    try:
        _show(f"two-calls-{second}", receipt)
        assert receipt["hostRequests"] == [] and receipt["handoff"] is False
    finally:
        bridge.close()


def test_e2e_second_turn_same_session(tmp_path, monkeypatch, capfd):
    """After a V turn (here: text-only answer, no entry used), a later turn in the same engine session."""
    receipt, out, requests, bridge = run(
        tmp_path,
        monkeypatch,
        "responses",
        False,
        [
            text_body("responses", False, "First answer."),
            text_body("responses", False, "Second answer."),
        ],
        lambda e, n: {"allow": True},
    )
    try:
        bridge.process_line(
            json.dumps(
                {
                    "protocol_version": "1",
                    "id": "send2",
                    "method": "chat.send",
                    "params": {
                        "session_id": "s2b-e2e",
                        "message": "And a normal follow-up question.",
                        "idempotency_key": "k2",
                    },
                }
            )
        )
        sent = next(x for x in out.lines() if x.get("id") == "send2")
        job = bridge._jobs[sent["result"]["job_id"]] if sent.get("ok") else None
        deadline = time.monotonic() + 20
        while (
            job is not None
            and job.status not in stdio_bridge.FINAL_JOB_STATUSES
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        tail = [
            {k: x.get(k) for k in ("type", "payload", "error") if k in x}
            for x in out.lines()
            if x.get("job_id") == getattr(job, "job_id", None)
        ][-4:]
        _show(
            "second-turn",
            receipt,
            {
                "firstJob": receipt["jobStatus"],
                "send2": sent.get("ok"),
                "secondJob": getattr(job, "status", None),
                "modelRequestsTotal": len(requests),
                "secondTail": tail,
            },
        )
        assert receipt["jobStatus"] == "completed" and job.status == "completed"
        assert len(requests) == 2
        assert receipt["parserScopes"] == [True, False]
        assert NAME not in {
            t.get("name") or t.get("function", {}).get("name") for t in requests[1].get("tools", [])
        }
        assert NAME not in bridge._sessions["s2b-e2e"].agent_session.tools
    finally:
        bridge.close()


def test_e2e_second_turn_plain_session_control(tmp_path, monkeypatch, capfd):
    """Control: identical two turns in a session created without the V descriptor."""
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-not-a-key")
    requests = []

    def handler(request):
        if request.url.path.endswith("/input_tokens"):
            return httpx.Response(200, json={"input_tokens": 100})
        requests.append(1)
        return httpx.Response(
            200,
            content=text_body("responses", False, f"Answer {len(requests)}."),
            headers={"content-type": "application/json"},
        )

    client = OpenAIResponsesClient(
        base_url="https://fixture.invalid/v1",
        api_key="fixture",
        model="test-model",
        transport=httpx.MockTransport(handler),
    )
    monkeypatch.setattr("alysis_code.agent.session._make_session_llm_client", lambda **_: client)
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(
            model="test-model",
            routing_mode="code_only",
            web_search_mode="off",
            stream=False,
            subagents_enabled=False,
        ),
    )
    out = Out()
    bridge = stdio_bridge.StdioBridge(stdout=out)
    monkeypatch.setattr(
        bridge, "_managed_browser_factory", lambda **kw: SimpleNamespace(close_all=lambda: None)
    )
    (tmp_path / "ws").mkdir()
    bridge.process_line(
        json.dumps(
            {
                "protocol_version": "1",
                "id": "create",
                "method": "session.create",
                "params": {
                    "session_id": "plain",
                    "workspace": str(tmp_path / "ws"),
                    "mode": "review",
                },
            }
        )
    )
    statuses = []
    try:
        for n in (1, 2):
            bridge.process_line(
                json.dumps(
                    {
                        "protocol_version": "1",
                        "id": f"send{n}",
                        "method": "chat.send",
                        "params": {
                            "session_id": "plain",
                            "message": f"Question {n}.",
                            "idempotency_key": f"k{n}",
                        },
                    }
                )
            )
            sent = next(x for x in out.lines() if x.get("id") == f"send{n}")
            job = bridge._jobs[sent["result"]["job_id"]]
            deadline = time.monotonic() + 20
            while job.status not in stdio_bridge.FINAL_JOB_STATUSES and time.monotonic() < deadline:
                time.sleep(0.01)
            statuses.append(job.status)
        print(
            "E2E plain-control "
            + json.dumps({"statuses": statuses, "modelRequests": len(requests)})
        )
        assert statuses == ["completed", "completed"]
    finally:
        bridge.close()


@pytest.mark.parametrize("kind", ["cumulative_compat", "anthropic"])
def test_e2e_real_prebinding_unavailable(tmp_path, monkeypatch, capfd, kind):
    monkeypatch.setattr(token_budget, "_token_encoder", lambda: None)
    monkeypatch.setenv("OPENAI_API_KEY", "fixture-not-a-key")
    contacted = []
    transport = httpx.MockTransport(lambda r: contacted.append(1) or httpx.Response(500))
    if kind == "anthropic":
        from alysis_code.llm.anthropic_messages import AnthropicMessagesClient

        client = AnthropicMessagesClient(
            base_url="https://fixture.invalid/v1",
            api_key="fixture",
            model="test-model",
            transport=transport,
        )
    else:
        client = OpenAICompatClient(
            base_url="https://fixture.invalid/v1",
            api_key="fixture",
            model="test-model",
            transport=transport,
            stream_assembly="cumulative",
        )
    monkeypatch.setattr("alysis_code.agent.session._make_session_llm_client", lambda **_: client)
    monkeypatch.setattr(
        stdio_bridge,
        "load_config",
        lambda: AppConfig(
            model="test-model",
            routing_mode="code_only",
            web_search_mode="off",
            subagents_enabled=False,
        ),
    )
    out = Out()
    bridge = stdio_bridge.StdioBridge(stdout=out)
    monkeypatch.setattr(
        bridge, "_managed_browser_factory", lambda **kw: SimpleNamespace(close_all=lambda: None)
    )
    (tmp_path / "ws").mkdir()
    try:
        bridge.process_line(
            json.dumps(
                {
                    "protocol_version": "1",
                    "id": "create-1",
                    "method": "session.create",
                    "params": {
                        "session_id": "s2b-e2e",
                        "workspace": str(tmp_path / "ws"),
                        "mode": "review",
                        "computer_start_resolved_host": descriptor(),
                    },
                }
            )
        )
        lines = out.lines()
        reply = next(x for x in lines if x.get("id") == "create-1")
        print(
            "E2E prebinding-"
            + kind
            + " "
            + json.dumps(
                {
                    "reply": reply,
                    "otherLines": [x.get("type") for x in lines if x is not reply],
                    "published": "s2b-e2e" in bridge._sessions,
                    "contacted": len(contacted),
                }
            )
        )
        assert reply == {**CORPUS["bases"]["protocolUnavailable"], "id": "create-1"}
        assert "s2b-e2e" not in bridge._sessions and contacted == []
        assert not any(APP in json.dumps(x) for x in lines)
    finally:
        bridge.close()
