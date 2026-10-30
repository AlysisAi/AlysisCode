"""A successful write followed by a locally refused verification read (e425d20a shape).

The refused computer_state never reaches the host, so nothing was observed or sent. The
turn may ask for a corrected read, but never replays the earlier action and never lets a
host-returned code acquire that authority.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from test_computer_use_recovery import _ScriptedComputerUseClient
from test_ide_computer_use_host import _lab_state, _RecordingSurface

from alysis_code.agent.computer_use_tools import (
    LocalObservationRefusal,
    build_computer_use_tools,
    computer_use_pending_read_code,
    reset_computer_use_snapshot_binding,
)
from alysis_code.agent_loop import create_session
from alysis_code.cancellation import CooperativeCancellationError, EventCancellationToken
from alysis_code.computer_use_call_diagnostics import sanitize_call_diagnostic
from alysis_code.config import AppConfig
from alysis_code.execution_deadline import ExecutionDeadline
from alysis_code.llm.openai_compat import LLMResponse, ToolCall

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

VALID = [{"role": "AXStaticText", "name": "PRIVATE_CANARY", "property": "value", "expected": "x"}]


class _Surface(_RecordingSurface):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []

    def emit_computer_use_call_diagnostic(self, payload):
        self.calls.append(sanitize_call_diagnostic(payload))


def _call(name, arguments, index):
    return LLMResponse(
        content="",
        raw={},
        tool_calls=[ToolCall(id=f"call-{index}", name=name, arguments=arguments)],
    )


def _run(
    tmp_path: Path,
    responses,
    host,
    *,
    forced=True,
    max_steps=10,
    cancel_on_correction=False,
    expire_on_correction=False,
    reasoning=False,
):
    surface = _Surface()
    cancel = threading.Event()
    clock = [0.0]
    deadline = (
        ExecutionDeadline.from_duration(90, clock=lambda: clock[0])
        if expire_on_correction
        else None
    )
    if expire_on_correction:
        surface.on_progress_update = lambda text: (
            clock.__setitem__(0, 91.0) if "malformed" in text else None
        )
    if cancel_on_correction:
        surface.on_progress_update = lambda text: cancel.set() if "malformed" in text else None
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=max_steps,
        execution_deadline=deadline,
        no_log=True,
        api_key_override="test-key",
        surface=surface,
        verification_enabled=False,
        sensitive_media=True,
        computer_use_host_request=host,
    )
    client = _ScriptedComputerUseClient(responses)
    client.supports_forced_tool_choice = forced
    client.reasoning_active = reasoning
    session.client = client
    try:
        try:
            code = session.run_turn(
                "Enter the draft, verify it, and leave it unsent.",
                cancellation_token=EventCancellationToken(cancel),
            )
        except CooperativeCancellationError:
            assert cancel_on_correction and cancel.is_set()
            code = 130
        durable = json.dumps(session.messages)
    finally:
        session.close()
    return code, surface, client, durable


def _host(log, *, refuse_reads_after_action=False):
    def host(name, arguments):
        log.append((name, dict(arguments)))
        if name == "computer_action":
            return {
                "receipt": {"action": "set_value", "performed": True},
                "state": _lab_state("after", "PRIVATE_DRAFT"),
            }
        if refuse_reads_after_action and any(n == "computer_action" for n, _ in log):
            # A host answer that merely reuses the local code string.
            return {"error": "invalid_observation_assertions", "message": "PRIVATE_CANARY"}
        return _lab_state("fresh", "PRIVATE_DRAFT")

    return host


def _write(index):
    return _call(
        "computer_action",
        {"action": "set_value", "element_id": "element_10_0abc", "text": "PRIVATE_DRAFT"},
        index,
    )


def test_wrapper_marks_only_pre_host_observation_refusals():
    sent = []
    tools = build_computer_use_tools(lambda name, args: sent.append(name) or {"snapshot_id": "s"})
    refused = tools["computer_state"].run({"assertions": [{"role": "AXTextArea"}]})
    assert isinstance(refused, LocalObservationRefusal)
    assert refused == {"error": "invalid_observation_assertions"}
    unavailable = tools["computer_state"].run({"include_image": True})
    assert isinstance(unavailable, LocalObservationRefusal)
    assert unavailable == {"error": "computer_observation_unavailable"}
    assert sent == []
    host_error = build_computer_use_tools(lambda *_: {"error": "invalid_observation_assertions"})[
        "computer_state"
    ].run({})
    assert not isinstance(host_error, LocalObservationRefusal)


@pytest.mark.parametrize(
    "failure", ["assertions", "unexpected_image", "invalid_image", "viewer_exception"]
)
@pytest.mark.parametrize("already_pending", [False, True])
def test_failed_observation_projection_revokes_new_snapshot(failure, already_pending):
    calls = []
    bad_projection = True

    class Viewer:
        def status(self):
            return {"state": "ready"}

        def capture(self, _result):
            if failure == "viewer_exception":
                raise RuntimeError("PRIVATE_CAPTURE_FAILURE")
            raise ValueError("PRIVATE_INVALID_IMAGE")

    def host(name, arguments):
        calls.append((name, dict(arguments)))
        if name == "computer_action":
            return {
                "receipt": {"action": "press", "performed": True},
                "state": _lab_state("after", "PRIVATE_DRAFT"),
            }
        result = _lab_state("new-state", "PRIVATE_DRAFT")
        if bad_projection:
            if failure == "assertions":
                result["elements"] = "PRIVATE_INVALID_ELEMENTS"
            else:
                result["image"] = {"PRIVATE_INVALID_IMAGE": True}
        return result

    tools = build_computer_use_tools(host, observation_available=True, viewer=Viewer())
    if already_pending:
        tools["computer_state"].run({"assertions": "invalid"})
    arguments = (
        {"assertions": VALID}
        if failure == "assertions"
        else ({"include_image": True} if failure in ("invalid_image", "viewer_exception") else {})
    )
    if failure == "viewer_exception":
        with pytest.raises(RuntimeError, match="PRIVATE_CAPTURE_FAILURE"):
            tools["computer_state"].run(arguments)
    else:
        error = tools["computer_state"].run(arguments)
        assert "error" in error and not isinstance(error, LocalObservationRefusal)
    refused = tools["computer_action"].run({"action": "press", "element_id": "old-id"})
    assert refused["error"] == "computer_use_observation_recovery_required"
    assert [name for name, _ in calls] == ["computer_state"]
    assert computer_use_pending_read_code(dict(tools))
    bad_projection = False
    assert "error" not in tools["computer_state"].run({})
    assert computer_use_pending_read_code(dict(tools)) is None
    assert "error" not in tools["computer_action"].run({"action": "press", "element_id": "new-id"})
    assert calls[-1][1]["snapshot_id"] == "new-state"


@pytest.mark.parametrize("failure", ["assertions", "unexpected_image"])
def test_normal_turn_already_stops_on_post_host_projection_error(tmp_path, failure):
    calls = []

    def host(name, arguments):
        calls.append(name)
        result = _lab_state("new-state", "PRIVATE_DRAFT")
        result["elements" if failure == "assertions" else "image"] = "PRIVATE_INVALID"
        return result

    code, surface, client, durable = _run(
        tmp_path,
        [
            _call("computer_state", {"assertions": VALID} if failure == "assertions" else {}, 1),
            _write(2),
        ],
        host,
    )
    assert code == 1 and client.calls == 1 and calls == ["computer_state"]
    assert any(code == "computer_use_host_failure" for code, *_ in surface.closed_errors)
    assert "PRIVATE" not in durable


def test_written_draft_survives_refused_read_and_corrected_read_completes(tmp_path):
    log: list = []
    code, surface, client, durable = _run(
        tmp_path,
        [
            _call("computer_state", {}, 1),
            _write(2),
            _call("computer_state", {"assertions": [{"role": "AXTextArea", "name": ""}]}, 3),
            _call("computer_state", {"assertions": VALID}, 4),
            LLMResponse(content="The unsent draft was observed.", tool_calls=[], raw={}),
        ],
        _host(log),
    )
    assert code == 0
    # The refused read never reached the host; the write was dispatched exactly once.
    assert [name for name, _ in log] == ["computer_state", "computer_action", "computer_state"]
    assert surface.closed_errors == []
    correction = client.call_records[3]
    assert "refused that computer_state call's own arguments" in json.dumps(correction["messages"])
    assert "do not repeat any action" in json.dumps(correction["messages"])
    results = [
        (r["callOrdinal"], r["tool"], r["result"], r.get("resultCode"))
        for r in surface.calls
        if r["callPhase"] == "result"
    ]
    assert results == [
        (1, "computer_state", "returned", "none"),
        (2, "computer_action", "returned", "none"),
        (3, "computer_state", "failed", "invalid_observation_assertions"),
        (4, "computer_state", "returned", "none"),
    ]
    assert "PRIVATE" not in json.dumps(surface.calls)
    assert "PRIVATE_DRAFT" not in durable
    assert not any(tmp_path.iterdir())


def test_exhausted_corrections_fail_with_local_code_not_host_failure_alone(tmp_path):
    log: list = []
    code, surface, client, _durable = _run(
        tmp_path,
        [
            _call("computer_state", {}, 1),
            _write(2),
            _call("computer_state", {"assertions": "PRIVATE_1"}, 3),
            _call("computer_state", {"assertions": ["PRIVATE_2"]}, 4),
            _call("computer_state", {"assertions": [{"role": "PRIVATE_3"}]}, 5),
        ],
        _host(log),
    )
    assert code == 1
    assert client.calls == 5
    assert [name for name, _ in log] == ["computer_state", "computer_action"]
    assert [c for c, _m, _r in surface.closed_errors] == [
        "computer_use_observation_arguments_invalid",
    ]
    assert all(not recoverable for _c, _m, recoverable in surface.closed_errors)
    assert "PRIVATE" not in json.dumps(surface.closed_errors) + json.dumps(surface.calls)


def test_host_answer_with_the_same_code_string_gets_no_recovery(tmp_path):
    log: list = []
    code, surface, client, _durable = _run(
        tmp_path,
        [
            _call("computer_state", {}, 1),
            _write(2),
            _call("computer_state", {}, 3),
        ],
        _host(log, refuse_reads_after_action=True),
    )
    assert code == 1
    assert client.calls == 3
    assert [name for name, _ in log] == ["computer_state", "computer_action", "computer_state"]
    assert [c for c, _m, _r in surface.closed_errors] == ["computer_use_host_failure"]


@pytest.mark.parametrize("forced", [True, False])
@pytest.mark.parametrize("ignored", ["action", "prose", "wait"])
def test_ignored_read_correction_cannot_mutate_or_complete(tmp_path, forced, ignored):
    log = []
    next_response = {
        "action": _write(4),
        "prose": LLMResponse(
            content='{"outcome":"completed_unverified","summary":"Done"}', tool_calls=[], raw={}
        ),
        "wait": _call("computer_wait", {"duration_ms": 50}, 4),
    }[ignored]
    responses = [
        _call("computer_state", {}, 1),
        _write(2),
        _call("computer_state", {"assertions": "PRIVATE_INVALID"}, 3),
        next_response,
    ]
    if ignored != "prose":
        responses.append(LLMResponse(content="Done", tool_calls=[], raw={}))
    code, surface, client, durable = _run(tmp_path, responses, _host(log), forced=forced)
    assert code == 1
    assert [name for name, _ in log] == ["computer_state", "computer_action"]
    assert "PRIVATE" not in durable
    if not forced:
        assert client.call_records[3]["tool_choice"] in (None, "auto")
    if ignored != "prose":
        assert surface.calls[-1]["resultCode"] == "computer_use_observation_recovery_required"


def test_identical_malformed_reads_keep_local_diagnosis_at_duplicate_guard(tmp_path):
    log = []
    code, surface, client, _ = _run(
        tmp_path,
        [_call("computer_state", {}, 1), _write(2)]
        + [_call("computer_state", {"assertions": "PRIVATE_IDENTICAL"}, i) for i in range(3, 6)],
        _host(log),
    )
    assert code == 1 and client.calls == 5
    assert [name for name, _ in log] == ["computer_state", "computer_action"]
    assert [r["resultCode"] for r in surface.calls if r["callPhase"] == "result"][-3:] == [
        "invalid_observation_assertions"
    ] * 3
    assert surface.closed_errors[0][0] == "computer_use_observation_arguments_invalid"
    assert "PRIVATE" not in json.dumps(surface.calls) + json.dumps(surface.closed_errors)


@pytest.mark.parametrize(
    "arguments", [{"include_image": "yes"}, {"element_id": 3}, {"assertions": ["invalid"]}]
)
def test_local_read_failure_revokes_old_snapshot_and_only_fresh_read_clears_gate(arguments):
    log = []
    tools = build_computer_use_tools(_host(log), observation_available=True)
    tools["computer_state"].run({})
    tools["computer_action"].run(
        {"action": "set_value", "element_id": "element_10_0abc", "text": "draft"}
    )
    assert isinstance(tools["computer_state"].run(arguments), LocalObservationRefusal)
    assert computer_use_pending_read_code(dict(tools))
    for name, args in [
        (
            "computer_action",
            {"action": "set_value", "element_id": "element_10_0abc", "text": "draft"},
        ),
        ("computer_wait", {"duration_ms": 50}),
    ]:
        assert tools[name].run(args)["error"] == "computer_use_observation_recovery_required"
    assert len(log) == 2
    assert "error" not in tools["computer_state"].run({})
    assert computer_use_pending_read_code(tools) is None
    assert "error" not in tools["computer_action"].run(
        {"action": "set_value", "element_id": "element_10_0abc", "text": "next"}
    )
    assert log[-1][1]["snapshot_id"] == "fresh"


def test_recovery_latch_is_reset_with_operation_but_does_not_restore_old_snapshot():
    tools = build_computer_use_tools(lambda *_: {"snapshot_id": "old"})
    tools["computer_state"].run({})
    tools["computer_state"].run({"assertions": "invalid"})
    reset_computer_use_snapshot_binding(tools)
    assert computer_use_pending_read_code(tools) is None
    assert (
        tools["computer_action"].run({"action": "press"})["error"]
        == "computer_use_fresh_observation_required"
    )


def test_malformed_read_correction_respects_turn_step_limit(tmp_path):
    log = []
    code, surface, client, _ = _run(
        tmp_path,
        [
            _call("computer_state", {}, 1),
            _write(2),
            _call("computer_state", {"assertions": "invalid"}, 3),
        ],
        _host(log),
        max_steps=3,
    )
    assert code == 1 and client.calls == 3
    assert len(log) == 2
    assert surface.closed_errors[0][0] == "computer_use_observation_arguments_invalid"


def test_stop_during_pending_read_recovery_prevents_another_model_or_host_call(tmp_path):
    log = []
    code, _surface, client, _ = _run(
        tmp_path,
        [
            _call("computer_state", {}, 1),
            _write(2),
            _call("computer_state", {"assertions": "invalid"}, 3),
        ],
        _host(log),
        cancel_on_correction=True,
    )
    assert code != 0 and client.calls == 3
    assert [name for name, _ in log] == ["computer_state", "computer_action"]


@pytest.mark.parametrize("kind", ["deadline", "reasoning"])
def test_correction_respects_deadline_and_reasoning_clients(tmp_path, kind):
    log = []
    responses = [
        _call("computer_state", {}, 1),
        _write(2),
        _call("computer_state", {"assertions": "invalid"}, 3),
    ]
    if kind == "reasoning":
        responses += [_write(4), LLMResponse(content="Done", tool_calls=[], raw={})]
    code, _surface, client, _ = _run(
        tmp_path,
        responses,
        _host(log),
        expire_on_correction=kind == "deadline",
        reasoning=kind == "reasoning",
    )
    assert [name for name, _ in log] == ["computer_state", "computer_action"]
    if kind == "deadline":
        # A budget stop uses the runtime's clean-stop code; it is not completion.
        assert code == 0 and client.calls == 3
    else:
        assert code != 0
        assert client.call_records[3]["tool_choice"] in (None, "auto")
