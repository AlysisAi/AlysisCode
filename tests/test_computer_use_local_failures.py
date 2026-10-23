from __future__ import annotations

import json

import pytest
from test_ide_computer_use_host import _lab_state, _RecordingSurface, _ScriptedComputerUseClient

from alysis_code.agent_loop import create_session
from alysis_code.computer_use_diagnostics import computer_use_local_result_failure_code
from alysis_code.config import AppConfig
from alysis_code.llm.types import LLMResponse, ToolCall


@pytest.mark.parametrize(
    "tool, error, expected",
    [
        (
            "computer_verify",
            "invalid_verification_proposal",
            "computer_use_verification_arguments_invalid",
        ),
        ("computer_verify", "invalid_host_response", "computer_use_verification_response_invalid"),
        ("computer_state", "invalid_host_response", "computer_use_observation_response_invalid"),
        ("computer_action", "invalid_host_response", "computer_use_observation_response_invalid"),
        ("computer_wait", "invalid_host_response", "computer_use_observation_response_invalid"),
        ("computer_state", "invalid_host_workflow", "computer_use_workflow_response_invalid"),
        (
            "computer_action",
            "computer_use_fresh_observation_required",
            "computer_use_fresh_observation_required",
        ),
        ("computer_action", "computer_use_approval_expired", "computer_use_approval_expired"),
        ("computer_state", "computer_use_approval_expired", None),
        ("computer_action", "computer_use_approval_expired PRIVATE_CANARY", None),
        ("computer_verify", "invalid_host_workflow", None),
        ("computer_action", "invalid_verification_proposal", None),
        ("shell", "invalid_host_response", None),
        ("computer_verify", "invalid_verification_proposal PRIVATE_CANARY", None),
        ("computer_state", {"private": "PRIVATE_CANARY"}, None),
    ],
)
def test_result_mapping_is_closed_without_reading_message_or_details(tool, error, expected):
    class Unreadable:
        def __str__(self):
            raise AssertionError("private payload inspected")

    result = {"error": error, "message": Unreadable(), "details": Unreadable()}
    assert computer_use_local_result_failure_code(tool, result) == expected


def call(name, arguments=None, index=0):
    return LLMResponse(
        content="",
        tool_calls=[
            ToolCall(
                id=f"call-{index}", name=name, arguments={} if arguments is None else arguments
            )
        ],
        raw={},
    )


@pytest.mark.parametrize(
    "scenario, expected",
    [
        ("arguments", "computer_use_verification_arguments_invalid"),
        ("response", "computer_use_verification_response_invalid"),
        ("snapshot", "computer_use_observation_response_invalid"),
        ("workflow", "computer_use_workflow_response_invalid"),
        ("fresh", "computer_use_fresh_observation_required"),
        ("expired", "computer_use_approval_expired"),
        ("unknown", None),
    ],
)
def test_real_protected_turn_emits_only_terminal_local_code_before_generic(
    tmp_path, scenario, expected
):
    host_calls = []

    def host_request(name, arguments):
        host_calls.append(name)
        if scenario == "expired":
            return (
                _lab_state("fresh", "READY")
                if name == "computer_state"
                else {"error": "computer_use_approval_expired", "message": "PRIVATE_CANARY"}
            )
        if scenario == "response":
            return {"verification": {"version": 1, "status": "PRIVATE_CANARY"}}
        if scenario == "snapshot":
            return {"title": "PRIVATE_CANARY", "snapshot_id": None}
        if scenario == "workflow":
            return {
                **_lab_state("snapshot-1", "PRIVATE_CANARY"),
                "alysis_workflow": {"version": 999},
            }
        if scenario == "unknown":
            return {"error": "PRIVATE_CANARY", "message": "PRIVATE_PATH /private/canary"}
        raise AssertionError("local refusal must not reach host")

    response = (
        call("computer_verify", {"operation": "evaluate", "predicate": None})
        if scenario == "arguments"
        else (
            call("computer_verify", {"operation": "evaluate"})
            if scenario == "response"
            else call("computer_state")
        )
    )
    responses = [response]
    if scenario == "fresh":
        responses = [
            call("computer_action", {"action": "press", "element_id": f"PRIVATE_CANARY_{i}"}, i)
            for i in range(3)
        ]
    if scenario == "expired":
        responses = [
            call("computer_state"),
            call("computer_action", {"action": "press", "element_id": "observed"}, 1),
        ]
    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=6,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=host_request,
        computer_use_verification_available=True,
        surface=surface,
    )
    client = _ScriptedComputerUseClient(responses)
    session.client = client
    try:
        assert session.run_turn("Verify the requested changes.") == 1
        durable = json.dumps(session.messages)
    finally:
        session.close()
    expected_codes = ([expected] if expected else []) + ["computer_use_host_failure"]
    assert [code for code, _message, _recoverable in surface.closed_errors] == expected_codes
    assert all(not recoverable for _code, _message, recoverable in surface.closed_errors)
    assert "PRIVATE_CANARY" not in json.dumps(surface.closed_errors) + durable
    assert "PRIVATE_PATH" not in json.dumps(surface.closed_errors) + durable
    assert len(client.calls) == (3 if scenario == "fresh" else 2 if scenario == "expired" else 1)
    if scenario in ("arguments", "fresh"):
        assert not host_calls
    assert not any(tmp_path.iterdir())


@pytest.mark.parametrize("later_failure", [False, True])
def test_recovered_local_guard_cannot_poison_success_or_later_unknown_failure(
    tmp_path, later_failure
):
    def host_request(name, arguments):
        if name == "computer_state":
            return _lab_state("fresh", "READY")
        return {"error": "PRIVATE_LATER_ERROR"}

    surface = _RecordingSurface()
    session = create_session(
        cfg=AppConfig(model="computer-use-test-model", routing_mode="code_only"),
        root=tmp_path,
        mode="review",
        yes=False,
        max_steps=6,
        no_log=True,
        api_key_override="test-key",
        sensitive_media=True,
        computer_use_host_request=host_request,
        surface=surface,
    )
    session.client = _ScriptedComputerUseClient(
        [
            call("computer_action", {"action": "press", "element_id": "observed"}, 1),
            call("computer_state", index=2),
            call("computer_action", {"action": "press", "element_id": "observed"}, 3)
            if later_failure
            else LLMResponse(content="The current page was observed.", tool_calls=[], raw={}),
        ]
    )
    try:
        assert session.run_turn("Read the current page.") == (1 if later_failure else 0)
    finally:
        session.close()
    assert [code for code, _message, _recoverable in surface.closed_errors] == (
        ["computer_use_host_failure"] if later_failure else []
    )
    assert not any(tmp_path.iterdir())
