"""Frozen Q refusal through production turns and the authenticated bridge, no real apps."""

import copy
import json
import threading
import time
from pathlib import Path

import pytest
from test_browser_start import binding
from test_computer_discovery_recovery import _host, _inventory
from test_computer_use_skill import _RecordingClient
from test_ide_computer_use_host import _bridge_session, _lines

from alysis_code.agent.browser_start import BrowserStartHandoff
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.ide.protocol import ProtocolError, ProtocolRequest
from alysis_code.llm.types import LLMResponse, ToolCall
from alysis_code.surface.noop_surface import NoopSurface

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

CODE = "computer_use_target_unverified"
TEXT = (
    "The host could not verify a selected app's installation for this request, so nothing was started. "
    "Do not retry that app in this request. Do not claim it changed, quit, is missing, or will succeed "
    "merely by resending."
)
VECTOR = json.loads(
    (Path(__file__).parent / "fixtures/computer_use_window_selection_v1.json").read_text()
)["admission_refusal_target_unverified"]


def inventory():
    result = _inventory(3)
    for index, target in enumerate(result["targets"]):
        target["target_id"] = f"00000000-0000-4000-8000-0000000000f{index + 1}"
        target["availability"] = "installed"
    return result


def selection(inv, indices=(1, 0), actions=None, **changes):
    return {
        "goal": VECTOR["selection"]["goal"],
        "constraints": [],
        "operates_apps": bool(actions),
        "app_steps": [
            {"app": inv["targets"][i]["name"], "actions": actions or ["activate_application"]}
            for i in indices
        ],
        **changes,
    }


def call(name, args, index):
    return LLMResponse(
        content="", raw={}, tool_calls=[ToolCall(id=f"call-{index}", name=name, arguments=args)]
    )


def run(tmp_path, inv, responses, host, mode="review", surface=None):
    agent = create_session(
        cfg=AppConfig(model="test-model", routing_mode="code_only", web_search_mode="off"),
        root=tmp_path,
        mode=mode,
        yes=False,
        max_steps=16,
        no_log=True,
        api_key_override="fixture",
        surface=surface or NoopSurface(),
        computer_discovery=inv,
        browser_start_host_request=host,
    )
    client = _RecordingClient(responses)
    agent.client = client
    try:
        result = agent.run_turn(
            "Open the requested apps. If an app cannot be verified, use only the other requested app."
        )
        return result, agent, client
    finally:
        agent.close()


def results_seen(client):
    return [
        json.loads(m["content"])
        for m in client.call_records[-1]["messages"]
        if m.get("role") == "tool"
    ]


def assert_fixed(results, count):
    refused = [value for value in results if value.get("error") == CODE]
    assert refused == [{"error": CODE, "message": TEXT}] * count
    assert all("unverified_target_ids" not in value for value in results)
    for forbidden in (
        "fresh_turn_required",
        "send the request again",
        "not temporary",
        "will not change",
        "reinstall",
        "broken seal",
    ):
        assert forbidden not in json.dumps(refused)


@pytest.mark.parametrize("mode", ["review", "fullaccess"])
@pytest.mark.parametrize("cause", ["broken_seal", "signing_read_unavailable"])
def test_mixed_selection_blocks_failed_identity_but_other_target_remains_eligible(
    tmp_path, mode, cause
):
    # Both app facts deliberately have the identical closed wire result; neither permits a permanence claim.
    inv = inventory()
    first = selection(inv)
    retries = [
        selection(inv, (0, 1)),
        selection(
            inv,
            (1, 0),
            ["activate_application", "press"],
            goal="Changed goal",
            constraints=["Keep data intact"],
        ),
        selection(inv, (0,)),
    ]
    good = selection(inv, (1,))
    host_calls = []

    def host(args):
        host_calls.append(copy.deepcopy(args))
        return (
            copy.deepcopy(VECTOR["refusal"])
            if len(host_calls) == 1
            else BrowserStartHandoff("accepted")
        )

    responses = [call("computer_start", first, 0), call("computer_discover", {}, 1)]
    responses += [call("computer_start", args, i + 2) for i, args in enumerate(retries + [good])]
    result, agent, client = run(tmp_path, inv, responses, host, mode)
    assert result == 0
    assert host_calls == [VECTOR["selection"], _host(good, inv)]
    assert len(client.call_records) == len(responses), cause
    assert_fixed(results_seen(client), 4)


@pytest.mark.parametrize("failed_indices", [(0, 1), tuple(range(8))])
def test_every_reported_failed_id_is_remembered_and_unexamined_member_stays_eligible(
    tmp_path, failed_indices
):
    inv = _inventory(9)
    for target in inv["targets"]:
        target["availability"] = "installed"
    first = selection(inv, tuple(range(8)))
    good = selection(inv, (8,))
    calls = []

    def host(args):
        calls.append(copy.deepcopy(args))
        return (
            {
                "error": CODE,
                "unverified_target_ids": [inv["targets"][i]["target_id"] for i in failed_indices],
            }
            if len(calls) == 1
            else BrowserStartHandoff("good")
        )

    responses = [call("computer_start", first, 0)]
    responses += [call("computer_start", selection(inv, (i,)), i + 1) for i in failed_indices]
    responses += [call("computer_start", good, 10)]
    result, _, client = run(tmp_path, inv, responses, host)
    assert result == 0
    assert calls == [_host(first, inv), _host(good, inv)]
    assert_fixed(results_seen(client), 1 + len(failed_indices))


BAD_REFUSALS = VECTOR["invalid_refusals"] + [
    {"error": CODE, "unverified_target_ids": None},
    {"error": CODE, "unverified_target_ids": [{}]},
    {"error": CODE, "unverified_target_ids": ["foreign"]},
    {"error": CODE, "unverified_target_ids": [str(i) for i in range(9)]},
    {
        "error": CODE,
        "unverified_target_ids": [VECTOR["refusal"]["unverified_target_ids"][0], "foreign"],
    },
]


@pytest.mark.parametrize("refused", BAD_REFUSALS)
def test_invalid_correlation_closes_all_further_admission_in_turn(tmp_path, refused):
    inv = inventory()
    calls = []

    def host(args):
        calls.append(copy.deepcopy(args))
        return copy.deepcopy(refused) if len(calls) == 1 else BrowserStartHandoff("must-not-start")

    responses = [
        call("computer_start", selection(inv), 0),
        call("computer_discover", {}, 1),
        call("computer_start", selection(inv, (1,)), 2),
        call("computer_start", selection(inv, (2,)), 3),
        LLMResponse(content="No Computer Use session was started.", raw={}, tool_calls=[]),
    ]
    _, _, client = run(tmp_path, inv, responses, host)
    assert calls == [VECTOR["selection"]]
    results = results_seen(client)
    starts = [value for value in results if "error" in value]
    assert len(starts) == 3
    assert all(value["error"] == "computer_use_start_request_invalid" for value in starts)
    assert all("blocked for this request" in value["message"] for value in starts)
    assert not any(
        "fresh_turn_required" in value or "unverified_target_ids" in value for value in starts
    )
    assert "untrusted host text" not in json.dumps(results)


@pytest.mark.parametrize(
    "code", ["computer_use_target_stale", "computer_use_inventory_unavailable"]
)
def test_real_replacement_and_scan_refusals_keep_existing_terminal_behavior(tmp_path, code):
    inv = inventory()
    calls = []
    result, agent, client = run(
        tmp_path,
        inv,
        [call("computer_start", selection(inv), 0)],
        lambda args: calls.append(args) or {"error": code, "message": "untrusted native text"},
    )
    assert result == 1
    assert len(calls) == len(client.call_records) == 1
    assert "send your request again" in agent.messages[-1]["content"]
    assert "untrusted native text" not in str(agent.messages)


@pytest.mark.parametrize(
    "decision",
    [
        VECTOR["host_decision"],
        *[
            {
                "allow": False,
                "code": value["error"],
                **{key: val for key, val in value.items() if key != "error"},
            }
            for value in BAD_REFUSALS
        ],
    ],
)
def test_real_turn_bridge_preserves_only_closed_correlated_refusal(tmp_path, decision):
    inv = inventory()
    bridge, out, session, job = _bridge_session(tmp_path)
    session.sensitive_media = False
    session.computer_use_host = None
    session.browser_start_host = {**binding(), "discovery": inv}
    calls, errors = [], []
    valid = decision == VECTOR["host_decision"]

    def host(args):
        calls.append(copy.deepcopy(args))
        payload = copy.deepcopy(decision) if len(calls) == 1 else {"allow": True}

        def reply():
            try:
                deadline = time.monotonic() + 2
                while session.pending_browser_start is None and time.monotonic() < deadline:
                    time.sleep(0.001)
                pending = session.pending_browser_start
                assert pending is not None
                for key in pending["identities"]:
                    with pytest.raises(ProtocolError):
                        bridge._dispatch(
                            ProtocolRequest(
                                id="foreign",
                                method="browser_start.respond",
                                protocol_version="1",
                                params={**pending["identities"], **payload, key: "foreign"},
                            )
                        )
                    assert not pending["done"].is_set()
                bridge._dispatch(
                    ProtocolRequest(
                        id="response",
                        method="browser_start.respond",
                        protocol_version="1",
                        params={**pending["identities"], **payload},
                    )
                )
            except BaseException as error:
                errors.append(error)
                job.cancellation_event.set()

        responder = threading.Thread(target=reply)
        responder.start()
        try:
            return bridge._request_browser_start(session, args)
        finally:
            responder.join(3)
            assert not responder.is_alive()

    responses = [
        call("computer_start", selection(inv), 0),
        call("computer_start", selection(inv, (0, 1)), 1),
        call("computer_start", selection(inv, (1,)), 2),
        LLMResponse(content="Admission is blocked.", raw={}, tool_calls=[]),
    ]
    result, _, client = run(tmp_path, inv, responses, host, surface=session.surface)
    assert not errors
    assert calls == [VECTOR["selection"]] + ([_host(selection(inv, (1,)), inv)] if valid else [])
    if valid:
        assert result == 0
        assert_fixed(results_seen(client), 2)
    else:
        assert all(r.get("error") != CODE for r in results_seen(client))
        assert not session.browser_start_consumed
    durable = json.dumps(bridge._event_buffers, default=str)
    assert "unverified_target_ids" not in durable and "untrusted host text" not in durable
    assert "untrusted host text" not in json.dumps(_lines(out))


def test_unknown_code_bridge_remains_closed_and_cannot_carry_host_prose(tmp_path):
    from test_browser_start import respond, setup_exchange

    bridge, _, session, job, worker, result, ids = setup_exchange(tmp_path)
    try:
        with pytest.raises(ProtocolError):
            respond(
                bridge, ids, allow=False, code="unknown_refusal", message="untrusted native text"
            )
        respond(bridge, ids, allow=False, code="unknown_refusal")
        worker.join(2)
        assert result == [{"error": "unknown_refusal"}]
        assert not session.browser_start_consumed
    finally:
        job.cancellation_event.set()
        worker.join(2)
