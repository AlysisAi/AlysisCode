from __future__ import annotations

import json

import pytest
from jsonschema import Draft202012Validator

from alysis_code.agent.computer_use_tools import (
    COMPUTER_USE_TOOL_NAMES,
    build_computer_use_tools,
)
from alysis_code.agent.computer_use_verification import (
    COMPUTER_USE_VERIFICATION_PARAMETERS,
    validate_computer_use_verification_arguments,
)
from alysis_code.agent.sensitive_output import (
    redact_sensitive_tool_arguments,
    sensitive_tool_boundary,
)
from alysis_code.ide.health import capabilities_payload
from alysis_code.ide.protocol import ProtocolError
from alysis_code.ide.stdio_bridge import ComputerUseHostBinding, _computer_use_host_param
from alysis_code.provider_auth.openai_codex import _sanitize_openai_tool_schema


def test_optional_host_only_tool_is_negotiated_without_changing_legacy_three():
    feature = capabilities_payload()["features"]["computer_use_host"]
    assert feature["tools"] == list(COMPUTER_USE_TOOL_NAMES)
    assert feature["verification_tools"] == ["computer_verify"]
    assert tuple(build_computer_use_tools(lambda *_: {})) == COMPUTER_USE_TOOL_NAMES
    binding = ComputerUseHostBinding(
        operation_id="operation",
        broker_fingerprint="sha256:" + "a" * 64,
        authority_epoch=1,
        scope_digest="sha256:" + "b" * 64,
        verification_available=True,
    )
    raw = binding.to_protocol()
    assert raw["tools"] == [*COMPUTER_USE_TOOL_NAMES, "computer_verify"]
    assert _computer_use_host_param(
        {"computer_use_host": raw}, request_id="r"
    ).verification_available
    for tools in [
        ["computer_verify", *COMPUTER_USE_TOOL_NAMES],
        [*COMPUTER_USE_TOOL_NAMES, "shell"],
        [*COMPUTER_USE_TOOL_NAMES, "computer_verify", "computer_verify"],
    ]:
        with pytest.raises(ProtocolError):
            _computer_use_host_param({"computer_use_host": {**raw, "tools": tools}}, request_id="r")


def test_verified_result_is_host_only_and_always_requires_new_snapshot_before_effect():
    calls = []

    def request(name, args):
        calls.append((name, args))
        return (
            {"snapshot_id": "fresh", "elements": []}
            if name == "computer_state"
            else {"verification": {"version": 1, "status": "verified", "record_count": 5}}
        )

    tools = build_computer_use_tools(request, verification_available=True)
    assert tuple(tools) == (*COMPUTER_USE_TOOL_NAMES, "computer_verify")
    tools["computer_state"].run({})
    assert (
        tools["computer_verify"].run({"operation": "evaluate"})["verification"]["status"]
        == "verified"
    )
    rejected = tools["computer_action"].run({"action": "press", "element_id": "field"})
    assert rejected["error"] == "computer_use_fresh_observation_required"
    assert [name for name, _ in calls] == ["computer_state", "computer_verify"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda p: p.update(expected_count=4),
        lambda p: p.update(task_id="other"),
        lambda p: p.update(snapshot_id="stale"),
        lambda p: p["predicate"].update(expected_value="success"),
        lambda p: p["predicate"].update(scope="current_page"),
    ],
)
def test_model_cannot_supply_authority_expected_results_or_downscope(mutation):
    value = {"operation": "propose", "predicate": {"kind": "field", "element_id": "observed"}}
    mutation(value)
    assert not validate_computer_use_verification_arguments(value)
    calls = []
    tools = build_computer_use_tools(lambda *args: calls.append(args), verification_available=True)
    assert tools["computer_verify"].run(value)["error"] == "invalid_verification_proposal"
    assert not calls


def test_record_mappings_keep_amount_meanings_distinct_and_transient_ids_private():
    columns = {
        name: None
        for name in ["amount", "outstanding_amount", "charged_amount", "status", "date", "merchant"]
    }
    columns["record_id"] = "Invoice number"
    witnesses = {
        name: None
        for name in [
            "status_element_id",
            "merchant_element_id",
            "period_element_id",
            "period_from_element_id",
            "period_through_element_id",
        ]
    }
    witnesses["account_element_id"] = "PRIVATE_ACCOUNT_ID"
    value = {
        "operation": "propose",
        "predicate": {
            "kind": "records",
            "table_id": "PRIVATE_TABLE_ID",
            "columns": columns,
            "witnesses": witnesses,
        },
    }
    assert validate_computer_use_verification_arguments(value)
    boundary = sensitive_tool_boundary("computer_verify", value)
    assert boundary.sensitive
    safe = redact_sensitive_tool_arguments("computer_verify", value, boundary=boundary)
    assert "PRIVATE_TABLE_ID" not in json.dumps(safe)
    assert "PRIVATE_ACCOUNT_ID" not in json.dumps(safe)
    assert not validate_computer_use_verification_arguments(
        {"operation": "evaluate", "predicate": value["predicate"]}
    )


def test_form_proposal_is_original_objective_only_and_cannot_supply_values_or_read_authority():
    proposal = {"operation": "propose", "predicate": {"kind": "form"}}
    assert validate_computer_use_verification_arguments(proposal)
    calls = []

    def request(name, arguments):
        calls.append((name, arguments))
        return {
            "verification": {
                "version": 1,
                "status": "accepted",
                "evidence_scope": "observed_form_after_save_reload",
            }
        }

    tools = build_computer_use_tools(request, verification_available=True)
    assert tools["computer_verify"].run(proposal)["verification"]["status"] == "accepted"
    assert calls == [("computer_verify", proposal)]
    for key, value in [
        ("expected_value", "Alex"),
        ("field_ids", ["one"]),
        ("saved", True),
        ("read_target", "https://other.example"),
        ("receipt", {}),
        ("task_id", "another"),
    ]:
        forged = {"operation": "propose", "predicate": {"kind": "form", key: value}}
        assert not validate_computer_use_verification_arguments(forged)
        assert tools["computer_verify"].run(forged)["error"] == "invalid_verification_proposal"
    assert len(calls) == 1


@pytest.mark.parametrize(
    "response",
    [
        {"proof": "done"},
        {"verification": {"version": 2, "status": "verified"}},
        {"verification": {"version": 1, "status": "model_complete"}},
    ],
)
def test_unversioned_or_model_claimed_response_is_not_accepted(response):
    tools = build_computer_use_tools(lambda *_: response, verification_available=True)
    assert (
        tools["computer_verify"].run({"operation": "evaluate"})["error"] == "invalid_host_response"
    )


@pytest.mark.parametrize("status", ["accepted", "needs_read", "verified", "inconclusive"])
def test_host_verification_statuses_with_controller_workflow_projection_are_compatible(status):
    result = {
        "verification": {
            "version": 1,
            "status": status,
            "reason": "observed_form_after_save_reload",
            "evidence_scope": "observed_form_after_save_reload",
        },
        "alysis_workflow": {
            "version": 1,
            "required": False,
            "complete": True,
            "next_application": None,
            "remaining_applications": [],
        },
    }
    calls = []

    def request(name, arguments):
        calls.append((name, arguments))
        return result

    tools = build_computer_use_tools(request, verification_available=True)
    assert tools["computer_verify"].run({"operation": "evaluate"}) == result
    assert calls == [("computer_verify", {"operation": "evaluate"})]


@pytest.mark.parametrize(
    "value, accepted",
    [
        ({"operation": "evaluate"}, True),
        ({"operation": "propose", "predicate": {"kind": "form"}}, True),
        ({"operation": "propose", "predicate": {"kind": "field", "element_id": "observed"}}, True),
        ({"operation": "propose"}, False),
        ({"operation": "propose", "predicate": None}, False),
        ({"operation": "evaluate", "predicate": None}, False),
        ({"operation": "evaluate", "predicate": {"kind": "form"}}, False),
        ({"operation": "evaluate", "expected_value": "PRIVATE_EXPECTATION"}, False),
        (
            {
                "operation": "propose",
                "predicate": {"kind": "form", "expected_value": "PRIVATE_EXPECTATION"},
            },
            False,
        ),
        (
            {
                "operation": "propose",
                "predicate": {"kind": "field", "element_id": "observed", "saved": True},
            },
            False,
        ),
        ({"operation": "propose", "predicate": {"kind": "field"}}, False),
        ({"operation": "propose", "predicate": {"kind": "other"}}, False),
        ({"operation": "other"}, False),
        ({"operation": "evaluate", "task_id": "another"}, False),
        ({"predicate": {"kind": "form"}}, False),
    ],
)
def test_provider_lowered_verification_grammar_matches_local_admission(value, accepted):
    # Exercise the production subscription schema sanitizer, not a test substitute.
    for schema in [
        COMPUTER_USE_VERIFICATION_PARAMETERS,
        _sanitize_openai_tool_schema(COMPUTER_USE_VERIFICATION_PARAMETERS),
    ]:
        Draft202012Validator.check_schema(schema)
        assert Draft202012Validator(schema).is_valid(value) is accepted
    assert validate_computer_use_verification_arguments(value) is accepted


def test_lowered_record_proposal_preserves_closed_columns_and_witnesses():
    columns = dict.fromkeys(
        ["amount", "outstanding_amount", "charged_amount", "status", "date", "merchant"]
    )
    columns["record_id"] = "Invoice number"
    witnesses = dict.fromkeys(
        [
            "status_element_id",
            "merchant_element_id",
            "period_element_id",
            "period_from_element_id",
            "period_through_element_id",
        ]
    )
    witnesses["account_element_id"] = "observed-account"
    value = {
        "operation": "propose",
        "predicate": {
            "kind": "records",
            "table_id": "observed-table",
            "columns": columns,
            "witnesses": witnesses,
        },
    }
    validator = Draft202012Validator(
        _sanitize_openai_tool_schema(COMPUTER_USE_VERIFICATION_PARAMETERS)
    )
    assert validator.is_valid(value) and validate_computer_use_verification_arguments(value)
    for invalid in [
        {**value, "operation": "evaluate"},
        {**value, "predicate": {**value["predicate"], "expected_count": 4}},
        {**value, "predicate": {**value["predicate"], "columns": {**columns, "private": "other"}}},
        {
            **value,
            "predicate": {
                **value["predicate"],
                "witnesses": {**witnesses, "account_element_id": None},
            },
        },
    ]:
        assert not validator.is_valid(invalid)
        assert not validate_computer_use_verification_arguments(invalid)
