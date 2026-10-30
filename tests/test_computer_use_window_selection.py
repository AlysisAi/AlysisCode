"""W1 frozen shared vectors through production tools, bridge and protected turns."""

import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_computer_interaction_v2 import call, done, state
from test_computer_use_completion_display import _Surface
from test_computer_use_recovery import _ScriptedComputerUseClient

from alysis_code import computer_use_platform_policy
from alysis_code.agent import computer_use_tools as cu
from alysis_code.agent.sensitive_output import (
    collect_computer_use_private_identifiers,
    redact_sensitive_response_taints,
    redact_sensitive_tool_arguments,
)
from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.ide.event_stream import EventContext, ProtocolEventSurface
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import _computer_use_host_param
from alysis_code.llm.types import LLMResponse

VECTORS = json.loads(
    (Path(__file__).parent / "fixtures/computer_use_window_selection_v1.json").read_text(
        encoding="utf-8"
    )
)
REF = VECTORS["selection_multiple"]["windows"][1]["window_ref"]
INVENTORY = VECTORS["selection_multiple"]["inventory_id"]
ASSERTIONS = [{"role": "AXButton", "name": "OK", "property": "enabled", "expected": True}]


@pytest.fixture(autouse=True)
def legacy_mac_window_contract(monkeypatch):
    """Run inert Mac protocol vectors on every CI host; never qualify a native route."""
    monkeypatch.setattr(computer_use_platform_policy, "sys", SimpleNamespace(platform="darwin"))


def inventory():
    return deepcopy(VECTORS["selection_multiple"])


def bound(ref=REF, n=2):
    return {
        **state(n),
        **deepcopy(VECTORS["select"]["observation_required_fields"]),
        "window_selection": {"window_ref": ref, "inventory_id": INVENTORY},
    }


def build(host, **kwargs):
    return cu.build_computer_use_tools(
        host, interaction_v2=True, observation_available=True, window_selection_v1=True, **kwargs
    )


def pending(tools):
    return cu.computer_use_window_selection_pending(tools)


def test_negotiation_advertises_and_roundtrips_exact_shared_binding():
    assert capabilities_payload()["features"]["computer_window_selection_v1"] == {
        "supported": True,
        "version": 1,
    }
    raw = VECTORS["negotiation"]["host_binding_valid"]
    binding = _computer_use_host_param({"computer_use_host": raw}, request_id="w1")
    assert binding.window_selection_version == 1 and binding.to_protocol() == raw
    with pytest.raises(ValueError):
        cu.build_computer_use_tools(lambda *_: {}, window_selection_v1=True)


@pytest.mark.parametrize(
    "patch",
    VECTORS["negotiation"]["host_binding_invalid_patches"]
    + [
        {"window_selection_version": None},
        {"window_selection_version": 1.0},
    ],
)
def test_negotiation_rejects_invalid_or_incomplete_versions(patch):
    raw = deepcopy(VECTORS["negotiation"]["host_binding_valid"])
    # Prove this decoder supports the negotiated binding before testing a refusal;
    # an implementation that rejects every version must not satisfy these cases.
    assert (
        _computer_use_host_param(
            {"computer_use_host": raw}, request_id="w1"
        ).window_selection_version
        == 1
    )
    for k, v in patch.items():
        if v is None and k == "interaction_version":
            raw.pop(k, None)
        else:
            raw[k] = v
    with pytest.raises(ProtocolError):
        _computer_use_host_param({"computer_use_host": raw}, request_id="w1")


@pytest.mark.parametrize("case", VECTORS["argument_cases"])
def test_shared_argument_cases(case):
    calls = []

    def host(name, args):
        calls.append((name, args))
        return bound(args["window_ref"]) if "window_ref" in args else inventory()

    tools = build(host)
    tools["computer_state"].run({})
    result = tools["computer_state"].run(deepcopy(case["arguments"]))
    if case["engine"] in {"dispatch", "strip_list_windows"}:
        assert "error" not in result
        assert len(calls) == 2
        assert calls[-1][1]["interaction_version"] == 2
        assert "snapshot_id" not in calls[-1][1]
        if case["engine"] == "strip_list_windows":
            assert "list_windows" not in calls[-1][1]
    else:
        assert isinstance(result, cu.LocalObservationRefusal)
        assert result["error"] == case["engine"] and len(calls) == 1


@pytest.mark.parametrize(
    "args",
    [
        {"list_windows": True},
        {"list_windows": False},
        {"window_ref": REF},
        {"list_windows": 1},
        {"window_ref": None},
    ],
)
def test_unnegotiated_arguments_are_refused_without_dispatch(args):
    calls = []
    tools = cu.build_computer_use_tools(lambda *a: calls.append(a))
    result = tools["computer_state"].run(args)
    assert isinstance(result, cu.LocalObservationRefusal)
    assert result["error"] == "invalid_observation_arguments" and not calls
    assert "list_windows" not in tools["computer_state"].parameters["properties"]


@pytest.mark.parametrize(
    "variant", ["selection_multiple", "selection_unreadable", "stale_reselection", "truncated"]
)
def test_shared_inventory_is_nonactionable_and_not_assertion_evidence(variant):
    if variant == "stale_reselection":
        value = deepcopy(VECTORS[variant]["app_to_engine_result"])
    elif variant == "truncated":
        value = inventory()
        value.update(
            reason="list_requested",
            inventory_id="window_inventory_00000000-0000-4000-8000-000000000004",
            inventory_complete=False,
            inventory_reasons=["window_limit"],
            truncated=True,
            omitted_count=3,
        )
        base = deepcopy(value["windows"][0])
        value["windows"] = [
            {
                **base,
                "window_ref": f"window_ref_00000000-0000-4000-8000-0000000001{i:02x}",
                "index": i,
                "title": f"Doc {i}",
            }
            for i in range(32)
        ]
    else:
        value = deepcopy(VECTORS[variant])
    calls = []
    tools = build(lambda *args: calls.append(args) or deepcopy(value))
    result = tools["computer_state"].run({"assertions": ASSERTIONS, "include_image": True})
    assert result == value and pending(tools)
    assert (
        tools["computer_action"].run({"action": "press", "element_id": "old"})["error"]
        == "computer_use_fresh_observation_required"
    )
    assert len(calls) == 1
    assert tools["computer_state"].metadata["_computer_use_snapshot_binding"].snapshot_id is None


@pytest.mark.parametrize(
    "tamper",
    [
        "extra",
        "image",
        "visual",
        "snapshot",
        "version_bool",
        "interaction_bool",
        "required_false",
        "duplicate_ref",
        "bad_ref",
        "index",
        "title_redacted",
        "redacted_size",
        "hint",
        "size_bool",
        "size_nan",
        "flag_int",
        "role_int",
        "too_many",
        "complete",
        "reason",
        "unsorted_reasons",
        "omitted_bool",
        "unreadable_nonempty",
        "pid_bool",
        "target_extra",
        "times",
    ],
)
def test_malformed_inventory_revokes_previous_snapshot(tamper):
    value = inventory()
    candidate = value["windows"][0]
    if tamper in {"extra", "image", "visual", "snapshot"}:
        value[{"snapshot": "snapshot_id"}.get(tamper, tamper)] = "injected"
    elif tamper == "version_bool":
        value["window_selection_version"] = True
    elif tamper == "interaction_bool":
        value["interaction_version"] = True
    elif tamper == "required_false":
        value["selection_required"] = False
    elif tamper == "duplicate_ref":
        value["windows"][1]["window_ref"] = candidate["window_ref"]
    elif tamper == "bad_ref":
        candidate["window_ref"] = "foreign"
    elif tamper == "index":
        candidate["index"] = 7
    elif tamper == "title_redacted":
        candidate["title_status"] = "redacted"
    elif tamper == "redacted_size":
        value["windows"][2]["size"] = {"width": 10, "height": 20}
    elif tamper == "hint":
        candidate["document_hint"] = "secret"
    elif tamper == "size_bool":
        candidate["size"]["width"] = True
    elif tamper == "size_nan":
        candidate["size"]["width"] = float("nan")
    elif tamper == "flag_int":
        candidate["focused"] = 1
    elif tamper == "role_int":
        candidate["role"] = 7
    elif tamper == "too_many":
        value["windows"] *= 9
    elif tamper == "complete":
        value["inventory_complete"] = False
    elif tamper == "reason":
        value["reason"] = "injected"
    elif tamper == "unsorted_reasons":
        value.update(
            inventory_complete=False,
            inventory_reasons=["window_unverified", "window_limit"],
            truncated=True,
        )
    elif tamper == "omitted_bool":
        value["omitted_count"] = True
    elif tamper == "unreadable_nonempty":
        value.update(
            inventory_complete=False,
            inventory_reasons=["window_list_unreadable"],
            omitted_count=None,
        )
    elif tamper == "pid_bool":
        value["target"]["pid"] = True
    elif tamper == "target_extra":
        value["target"]["injected"] = True
    elif tamper == "times":
        value["expires_at"] = value["observed_at"]
    responses = iter([state(), value])
    tools = build(lambda *_: next(responses))
    tools["computer_state"].run({})
    assert tools["computer_state"].run({})["error"] == "invalid_host_response"
    assert (
        tools["computer_action"].run({"action": "press"})["error"]
        == "computer_use_fresh_observation_required"
    )


@pytest.mark.parametrize("tamper", [None, "ref", "inventory", "extra", "missing"])
def test_selected_observation_echo_and_trusted_action_snapshot(tamper):
    result = bound()
    if tamper in {"ref", "inventory"}:
        result["window_selection"][{"ref": "window_ref", "inventory": "inventory_id"}[tamper]] = (
            "foreign"
        )
    elif tamper == "extra":
        result["window_selection"]["extra"] = True
    elif tamper == "missing":
        result.pop("window_selection")
    calls = []

    def host(name, args):
        calls.append((name, args))
        if len(calls) == 1:
            return inventory()
        if name == "computer_state":
            return result
        return {"receipt": {"action": "press"}, "state": state(3)}

    tools = build(host)
    tools["computer_state"].run({})
    observed = tools["computer_state"].run({"window_ref": REF})
    if tamper:
        assert observed["error"] == "invalid_host_response"
    else:
        assert observed == result and not pending(tools)
        assert "error" not in tools["computer_action"].run(
            {"action": "press", "snapshot_id": "forged"}
        )
        assert calls[-1][1]["snapshot_id"] == result["snapshot_id"]
        assert tools["computer_state"].run({"window_ref": REF}) == result


@pytest.mark.parametrize("tool", ["computer_state", "computer_wait"])
def test_unsolicited_echo_or_selection_on_wait_is_rejected(tool):
    tools = build(lambda *_: bound() if tool == "computer_state" else inventory())
    assert tools[tool].run({})["error"] == "invalid_host_response"


def test_workflow_is_adopted_but_never_input_authority():
    value = {
        **inventory(),
        "alysis_workflow": {
            "version": 1,
            "required": True,
            "complete": False,
            "next_application": "Other",
            "remaining_applications": ["Other"],
        },
    }
    tools = build(lambda *_: deepcopy(value))
    assert tools["computer_state"].run({}) == value
    assert cu.computer_use_workflow_status(tools)["complete"] is False and pending(tools)


@pytest.mark.parametrize("kind", ["plain", "list", "ref", "bind"])
def test_cap_counts_all_selection_inventories_and_allows_final_binding(kind):
    calls = []

    def host(name, args):
        calls.append((name, args))
        return bound() if kind == "bind" and len(calls) == 9 else inventory()

    tools = build(host)
    for i in range(8):
        args = {"window_ref": REF} if kind == "ref" and i else {}
        assert tools["computer_state"].run(args)["kind"] == "window_selection_required"
    args = (
        {"window_ref": REF}
        if kind in {"ref", "bind"}
        else {"list_windows": True}
        if kind == "list"
        else {}
    )
    result = tools["computer_state"].run(args)
    if kind == "bind":
        assert "snapshot_id" in result and not pending(tools)
    else:
        assert result["error"] == "computer_use_window_selection_exhausted"
        assert len(calls) == (9 if kind == "ref" else 8)
        assert (
            tools["computer_state"].run({"window_ref": REF})["error"]
            == "computer_use_window_selection_exhausted"
        )


def test_selection_does_not_refill_action_refusal_reads():
    calls = []

    def host(name, args):
        calls.append(name)
        if name == "computer_action":
            return deepcopy(VECTORS["errors"]["window_reselection_required"])
        return state() if len(calls) == 1 else inventory()

    tools = build(host)
    tools["computer_state"].run({})
    tools["computer_action"].run({"action": "press"})
    for _ in range(4):
        assert "error" not in tools["computer_state"].run({})
    assert tools["computer_state"].run({})["error"] == "computer_use_refusal_reads_exhausted"
    assert len(calls) == 6


@pytest.mark.parametrize("action", ["activate_application", "press"])
def test_activation_carrier_preserves_receipt_and_revokes_input(action):
    responses = iter(
        [state(), deepcopy(VECTORS["activation_selection"]["app_to_engine_result"]), bound()]
    )
    tools = build(lambda *_: next(responses))
    tools["computer_state"].run({})
    result = tools["computer_action"].run({"action": action, "application": "Preview"})
    if action != "activate_application":
        assert result["error"] == "invalid_host_response"
    else:
        assert result == VECTORS["activation_selection"]["app_to_engine_result"] and pending(tools)
        assert (
            tools["computer_action"].run({"action": action})["error"]
            == "computer_use_fresh_observation_required"
        )
        assert "snapshot_id" in tools["computer_state"].run({"window_ref": REF})


def test_full_reset_clears_selection_and_allows_new_operation():
    tools = build(lambda *_: inventory())
    for _ in range(8):
        tools["computer_state"].run({})
    cu.reset_computer_use_snapshot_binding(tools)
    assert not pending(tools)
    assert tools["computer_state"].run({"window_ref": REF})["error"] == "window_reference_unknown"
    assert "kind" in tools["computer_state"].run({})


@pytest.mark.parametrize("raw", [False, True])
def test_private_window_references_never_survive_durable_or_display_projection(raw):
    args = {"window_ref": REF, "inventory_id": INVENTORY}
    if raw:
        args = {"_raw_arguments": json.dumps(args)[:-1]}
    identifiers = collect_computer_use_private_identifiers("computer_state", args)
    assert {REF, INVENTORY} <= set(identifiers)
    assert REF not in json.dumps(redact_sensitive_tool_arguments("computer_state", args))
    report = LLMResponse(content=f"{REF} {INVENTORY}", tool_calls=[], raw={})
    projected = redact_sensitive_response_taints(report, set(identifiers))
    assert REF not in projected.content and INVENTORY not in projected.content


def run_turn(tmp_path, host, responses, *, cancellation_token=None, surface=None):
    surface = surface if surface is not None else _Surface()
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=16,
        no_log=True,
        api_key_override="fixture-key",
        surface=surface,
        sensitive_media=True,
        verification_enabled=False,
        computer_use_host_request=host,
        computer_use_response_mode="host_sanitized_text",
        computer_interaction_v2=True,
        computer_observation_available=True,
        computer_window_selection_v1=True,
    )
    client = _ScriptedComputerUseClient(responses)
    session.client = client
    try:
        code = session.run_turn(
            "Read the four boxes in Preview; do not change the documents.",
            cancellation_token=cancellation_token,
        )
        return code, surface, dict(session.last_turn_outcome), client, json.dumps(session.messages)
    finally:
        session.close()


@pytest.mark.parametrize("outcome", ["blocked", "completed_unverified", "incomplete", "prose"])
@pytest.mark.parametrize("after_refusal", [False, True])
def test_real_turn_selection_question_is_not_completion(tmp_path, outcome, after_refusal):
    responses = [call("computer_state", {}, 1)]
    host_results = [inventory()]
    if after_refusal:
        responses += [
            call(
                "computer_action",
                {"action": "press", "target_type": "semantic", "element_id": "button"},
                2,
            ),
            call("computer_state", {}, 3),
        ]
        host_results = [
            state(),
            deepcopy(VECTORS["errors"]["window_reselection_required"]),
            inventory(),
        ]
    text = (
        "Which Preview window should I read?"
        if outcome == "prose"
        else json.dumps({"outcome": outcome, "summary": "Which Preview window should I read?"})
    )
    responses += [LLMResponse(content=text, tool_calls=[], raw={})]
    results = iter(host_results)
    code, surface, final, client, durable = run_turn(tmp_path, lambda *_: next(results), responses)
    assert code == 0
    assert final["outcome"] == ("blocked" if outcome == "blocked" else "incomplete")
    terminal = next(e for e in surface.evidence if e.get("kind") == "terminal")
    assert terminal["reportedOutcome"] != "completed"
    assert surface.messages == ["Which Preview window should I read?"]
    assert REF not in durable and INVENTORY not in durable
    assert any(
        "Window selection is required." in str(m) for m in client.call_records[-1]["messages"]
    )


def test_real_turn_successful_selection_consumes_directive(tmp_path):
    results = iter([inventory(), bound()])
    code, _, final, client, _ = run_turn(
        tmp_path,
        lambda *_: next(results),
        [call("computer_state", {}, 1), call("computer_state", {"window_ref": REF}, 2), done()],
    )
    assert code == 0 and final["outcome"] == "completed_unverified"
    assert not any(
        "Window selection is required." in str(m) for m in client.call_records[-1]["messages"]
    )


def test_real_turn_activation_selection_can_ask_without_replaying(tmp_path):
    results = iter([state(), deepcopy(VECTORS["activation_selection"]["app_to_engine_result"])])
    calls = []

    def host(name, args):
        calls.append(name)
        return next(results)

    code, _, final, _, _ = run_turn(
        tmp_path,
        host,
        [
            call("computer_state", {}, 1),
            call(
                "computer_action", {"action": "activate_application", "application": "Preview"}, 2
            ),
            LLMResponse(
                content='{"outcome":"blocked","summary":"Which Preview window?"}',
                tool_calls=[],
                raw={},
            ),
        ],
    )
    assert code == 0 and final["outcome"] == "blocked"
    assert calls == ["computer_state", "computer_action"]


@pytest.mark.parametrize(
    "feedback",
    [
        {"dispatch": "uncertain", "authority": "valid_for_reobserve"},
        {"dispatch": "dispatched", "authority": "takeover"},
        {"dispatch": "not_dispatched", "authority": "valid_for_reobserve"},
    ],
)
def test_activation_selection_cannot_hide_uncertain_or_revoked_delivery(feedback):
    result = deepcopy(VECTORS["activation_selection"]["app_to_engine_result"])
    result["interaction_feedback"] = feedback
    results = iter([state(), result])
    tools = build(lambda *_: next(results))
    tools["computer_state"].run({})
    assert (
        tools["computer_action"].run({"action": "activate_application", "application": "Preview"})[
            "error"
        ]
        == "invalid_host_response"
    )
    assert (
        tools["computer_action"].run({"action": "press"})["error"]
        == "computer_use_fresh_observation_required"
    )


@pytest.mark.parametrize(
    "code,authority",
    [
        ("user_takeover", "takeover"),
        ("computer_use_cancelled", "invalid"),
        ("computer_use_full_access_revoked", "invalid"),
        ("window_reference_invalid", "invalid"),
    ],
)
def test_refusal_during_selection_stays_terminal_and_never_replays(tmp_path, code, authority):
    error = {
        "error": code,
        "interaction_feedback": {
            "dispatch": "not_dispatched",
            "authority": authority,
            "effect": "unknown",
            "cause": code,
            "phase": "validation",
        },
    }
    results = iter([inventory(), error])
    calls = []

    def host(name, args):
        calls.append((name, args))
        return next(results)

    exit_code, _, final, client, _ = run_turn(
        tmp_path,
        host,
        [call("computer_state", {}, 1), call("computer_state", {"window_ref": REF}, 2)],
    )
    cancelled = code in {"user_takeover", "computer_use_cancelled"}
    assert (exit_code != 0) is cancelled
    assert len(calls) == 2 and client.calls == 2
    if not cancelled:
        assert final["outcome"] == "blocked"
    assert final["outcome"] != "completed_unverified"


def test_select_with_image_uses_existing_exact_frame_delivery(tmp_path):
    from test_computer_interaction_v2 import image_state
    from test_embedded_browser_visual import visual_cfg

    from alysis_code.embedded_browser_visual import ComputerUseViewer

    raw = image_state()
    raw["window_selection"] = {"window_ref": REF, "inventory_id": INVENTORY}
    results = iter([inventory(), raw])
    calls = []

    def host(name, args):
        calls.append(args)
        return next(results)

    viewer = ComputerUseViewer(root=tmp_path, cfg=visual_cfg())
    tools = build(host, viewer=viewer)
    tools["computer_state"].run({})
    result = tools["computer_state"].run({"window_ref": REF, "include_image": True})
    assert "error" not in result and not pending(tools)
    assert calls[-1] == {"window_ref": REF, "include_image": True, "interaction_version": 2}
    assert "image" not in result
    message = viewer.take_visual_message(result["visual_delivery_id"])
    assert raw["image"]["data"] in json.dumps(message)
    assert viewer.take_visual_message(result["visual_delivery_id"]) is None


def test_selected_window_without_model_image_support_gets_no_capture_request():
    calls = []

    def host(name, args):
        calls.append(args)
        return inventory() if len(calls) == 1 else bound()

    tools = build(host)
    tools["computer_state"].run({})
    result = tools["computer_state"].run({"window_ref": REF, "include_image": True})
    assert calls[-1]["include_image"] is False
    assert result["visual"] == {
        "status": "unavailable",
        "reason": "model_image_capability_unavailable",
    }


def test_stop_after_inventory_prevents_next_model_or_host_call(tmp_path):
    import threading

    from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken

    cancel = threading.Event()
    calls = []

    def host(name, args):
        calls.append(name)
        cancel.set()
        return inventory()

    with pytest.raises(CooperativeCancellationError):
        run_turn(
            tmp_path,
            host,
            [call("computer_state", {}, 1)],
            cancellation_token=EventCancellationToken(cancel),
        )
    assert calls == ["computer_state"]


def test_session_close_clears_unconsumed_selection_authority(tmp_path):
    session = create_session(
        cfg=AppConfig(model="test-model"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=4,
        no_log=True,
        api_key_override="fixture-key",
        sensitive_media=True,
        verification_enabled=False,
        computer_use_host_request=lambda *_: inventory(),
        computer_interaction_v2=True,
        computer_window_selection_v1=True,
    )
    tools = session.tools
    binding = tools["computer_state"].metadata["_computer_use_snapshot_binding"]
    try:
        tools["computer_state"].run({})
        assert pending(tools) and binding.window_refs
    finally:
        session.close()
    assert not pending(tools) and not binding.window_refs
    assert binding.window_inventory_id is None and binding.window_selection_reads == 0


@pytest.mark.parametrize("tamper", ["image", "image_frame", "visual", "ok"])
def test_activation_envelope_cannot_smuggle_pixels_or_failed_dispatch(tamper):
    result = deepcopy(VECTORS["activation_selection"]["app_to_engine_result"])
    result[tamper] = False if tamper == "ok" else {"untrusted": "value"}
    results = iter([state(), result])
    tools = build(lambda *_: next(results))
    tools["computer_state"].run({})
    assert (
        tools["computer_action"].run({"action": "activate_application", "application": "Preview"})[
            "error"
        ]
        == "invalid_host_response"
    )
    assert (
        tools["computer_action"].run({"action": "press"})["error"]
        == "computer_use_fresh_observation_required"
    )


@pytest.mark.parametrize("outcome", ["blocked", "incomplete", "completed_unverified", "prose"])
@pytest.mark.parametrize("selected", [False, True])
def test_real_turn_terminal_prose_redacts_window_tokens(tmp_path, outcome, selected):
    calls = []

    def host(name, args):
        calls.append((name, args))
        return bound() if "window_ref" in args else inventory()

    responses = [call("computer_state", {}, 1)]
    if selected:
        responses.append(call("computer_state", {"window_ref": REF}, 2))
    # Include an unissued reference as well: projection must recognize token
    # shapes, not rely on the set of identifiers observed in earlier tool calls.
    unissued = "window_ref_aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    question = f'Use "{REF}" from ({INVENTORY}), or {unissued}?'
    safe_question = 'Use "[window reference]" from ([window reference]), or [window reference]?'
    text = question if outcome == "prose" else json.dumps({"outcome": outcome, "summary": question})
    responses.append(LLMResponse(content=text, tool_calls=[], raw={}))
    events = []
    surface = ProtocolEventSurface(
        context=EventContext(session_id="s", run_id="r", job_id="j"), emit=events.append
    )
    code, _, final, _, durable = run_turn(tmp_path, host, responses, surface=surface)
    assert code == 0
    if outcome == "blocked":
        assert final["outcome"] == "blocked"
    displayed = [e["payload"]["text"] for e in events if e["type"] == "message_end"]
    assert displayed == [safe_question]
    for token in (REF, INVENTORY, unissued):
        assert token not in json.dumps(events) + durable
    if selected:
        assert calls[-1] == ("computer_state", {"window_ref": REF, "interaction_version": 2})


@pytest.mark.parametrize(
    "last_arguments,host_count", [({}, 8), ({"list_windows": True}, 8), ({"window_ref": REF}, 9)]
)
def test_real_turn_exhaustion_emits_closed_terminal_event(tmp_path, last_arguments, host_count):
    fixture = VECTORS["exhaustion_terminal_event"]
    events = []
    surface = ProtocolEventSurface(
        context=EventContext(
            session_id="selection-session", run_id="selection-run", job_id="selection-job"
        ),
        emit=events.append,
    )
    calls = []

    def host(name, args):
        calls.append((name, args))
        return inventory()

    responses = [call("computer_state", {"max_elements": 20 + n}, n) for n in range(8)]
    responses.append(call("computer_state", last_arguments, 9))
    code, _, final, client, durable = run_turn(tmp_path, host, responses, surface=surface)
    errors = [e for e in events if e["type"] == "error_raised"]
    assert code == 1 and client.calls == 9 and len(calls) == host_count
    assert final["outcome"] != "completed_unverified"
    assert len(errors) == 1
    # N5: normalize only illustrative envelope fields; the protocol, event type,
    # entire payload and any unexpected fields still participate in equality.
    runtime_fields = ("session_id", "run_id", "job_id", "sequence", "timestamp")
    assert {**errors[0], **{key: fixture[key] for key in runtime_fields}} == fixture
    sequences = [event["sequence"] for event in events]
    assert all(type(sequence) is int and sequence > 0 for sequence in sequences)
    assert all(
        previous < current for previous, current in zip(sequences, sequences[1:], strict=False)
    )
    assert events[-1] is errors[0]
    assert REF not in json.dumps(events) + durable and INVENTORY not in durable
    if "window_ref" in last_arguments:
        assert calls[-1][1] == {"window_ref": REF, "interaction_version": 2}


@pytest.mark.parametrize("case", VECTORS["unicode_length_cases"], ids=lambda case: case["name"])
def test_shared_unicode_titles_use_code_points(case):
    value = inventory()
    value["windows"][0]["title"] = case["title"]
    assert len(case["title"]) == case["code_points"]
    tools = build(lambda *_: value)
    result = tools["computer_state"].run({})
    if case["accepted"]:
        assert result == value and pending(tools)
    else:
        assert result["error"] == "invalid_host_response" and not pending(tools)


@pytest.mark.parametrize("field,limit", [("role", 128), ("subrole", 128), ("name", 256)])
@pytest.mark.parametrize("over_limit", [False, True])
def test_selection_metadata_code_point_boundaries(field, limit, over_limit):
    value = inventory()
    target = value["target"] if field == "name" else value["windows"][0]
    target[field] = "😀" * (limit + int(over_limit))
    tools = build(lambda *_: value)
    result = tools["computer_state"].run({})
    if over_limit:
        assert result["error"] == "invalid_host_response" and not pending(tools)
    else:
        assert result == value and pending(tools)
