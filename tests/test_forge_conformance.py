from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

import alysis_code.forge_conformance as conformance_module
from alysis_code.forge_conformance import (
    KNOWN_PROTOCOL_GAPS,
    SURFACE_CAPTURE_ADAPTERS,
    SURFACE_PARITY_STATUS,
    ForgeConformanceInputError,
    ForgeTraceConformanceError,
    assert_forge_host_traces_equal,
    build_forge_host_trace,
    compare_forge_host_traces,
)
from alysis_code.forge_host_protocol import (
    EventEnvelope,
    ProtocolLedger,
    ProtocolValidationError,
    RequestDisposition,
    RequestEnvelope,
    UnsafeSerializationError,
    approval_action_fingerprint,
    approval_scope_fingerprint,
    execution_admission_fingerprint,
    validate_envelope,
)

FIXTURES = Path(__file__).parents[1] / "schemas" / "forge-host-v2" / "fixtures" / "valid"


def _load(name: str) -> dict[str, Any]:
    value = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def _validated(name: str) -> RequestEnvelope | EventEnvelope:
    envelope = validate_envelope(_load(name))
    assert isinstance(envelope, (RequestEnvelope, EventEnvelope))
    return envelope


def _event_with_sequence(
    name: str,
    sequence: int,
    *,
    operation_id: str | None = None,
) -> EventEnvelope:
    wire = _load(name)
    wire["sequence"] = sequence
    if operation_id is not None:
        wire["operationId"] = operation_id
    envelope = validate_envelope(wire)
    assert isinstance(envelope, EventEnvelope)
    return envelope


def _request_with_operation(name: str, operation_id: str) -> RequestEnvelope:
    wire = _load(name)
    wire["operationId"] = operation_id
    envelope = validate_envelope(wire)
    assert isinstance(envelope, RequestEnvelope)
    return envelope


def _recompute_approval_authority(wire: dict[str, Any]) -> None:
    payload = wire["payload"]
    identity = wire["identity"]
    scope = payload["scope"]
    scope["fingerprint"] = approval_scope_fingerprint(scope)
    payload["actionFingerprint"] = approval_action_fingerprint(
        operation_id=wire["operationId"],
        session_id=identity["sessionId"],
        generation_id=identity["generationId"],
        run_id=identity["runId"],
        revision=identity["revision"],
        approval_id=payload["approvalId"],
        action=payload["action"],
        scope_fingerprint=scope["fingerprint"],
        expires_at=payload["expiresAt"],
        risk=payload["risk"],
        summary=payload["summary"],
    )


def _ledger_accepted_run(
    *,
    run_operation_id: str = "operation_run00001",
) -> tuple[list[RequestEnvelope | EventEnvelope], ProtocolLedger]:
    planner_input = _validated("planner-input-request.json")
    plan = _validated("plan-ready-event.json")
    approval_required = _event_with_sequence("approval-required-event.json", 2)
    approval_decision = _validated("approval-decision-request.json")
    run_start = _request_with_operation("run-start-request.json", run_operation_id)
    progress = _event_with_sequence("run-progress-event.json", 3, operation_id=run_operation_id)
    evidence = _event_with_sequence("evidence-ready-event.json", 4, operation_id=run_operation_id)
    terminal = _event_with_sequence("terminal-event.json", 5, operation_id=run_operation_id)
    assert isinstance(plan, EventEnvelope)
    assert isinstance(planner_input, RequestEnvelope)
    assert isinstance(approval_decision, RequestEnvelope)
    assert isinstance(run_start, RequestEnvelope)
    assert isinstance(progress, EventEnvelope)
    assert isinstance(evidence, EventEnvelope)

    ledger = ProtocolLedger(
        plan.identity,
        clock=lambda: datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc),
    )
    assert ledger.accept_request(planner_input) is RequestDisposition.ACCEPTED
    ledger.accept_event(plan)
    ledger.accept_event(approval_required)
    assert ledger.accept_request(approval_decision) is RequestDisposition.ACCEPTED
    assert ledger.accept_request(run_start) is RequestDisposition.ACCEPTED
    ledger.bind_run(evidence.identity)
    ledger.accept_event(progress)
    ledger.accept_event(evidence)
    ledger.accept_event(terminal)
    ledger.require_terminal()

    return [
        plan,
        approval_required,
        approval_decision,
        run_start,
        progress,
        evidence,
        terminal,
    ], ledger


def _canonical_run_trace(*, label: str = "canonical"):
    envelopes, _ledger = _ledger_accepted_run()
    return build_forge_host_trace(envelopes, label=label)


def test_scope_explicitly_claims_no_real_surface_capture_or_parity() -> None:
    assert SURFACE_CAPTURE_ADAPTERS == ()
    assert SURFACE_PARITY_STATUS == "not_evaluated"
    gaps = " ".join(KNOWN_PROTOCOL_GAPS).casefold()
    assert "recovery/checkpoint" in gaps
    assert "preview" in gaps
    assert "computer use" in gaps


def test_primary_canonical_trace_is_statefully_accepted_by_protocol_ledger() -> None:
    envelopes, ledger = _ledger_accepted_run()
    trace = build_forge_host_trace(envelopes)

    assert ledger.terminal_seen
    plan = envelopes[0]
    assert isinstance(plan, EventEnvelope)
    assert ledger.host_plan_fingerprint == plan.payload.host_plan_fingerprint
    assert [record.message_type for record in trace.records] == [
        "plan.ready",
        "approval.required",
        "approval.decision",
        "run.start",
        "run.progress",
        "evidence.ready",
        "run.terminal",
    ]


def test_identical_validated_canonical_traces_are_equal() -> None:
    expected = _canonical_run_trace(label="canonical-a")
    actual = _canonical_run_trace(label="canonical-b")

    assert compare_forge_host_traces(expected, actual) == ()
    assert_forge_host_traces_equal(expected, actual)


def test_operation_correlation_authority_is_preserved_and_compared() -> None:
    expected_envelopes, expected_ledger = _ledger_accepted_run()
    actual_envelopes, actual_ledger = _ledger_accepted_run(run_operation_id="operation_run99999")
    assert expected_ledger.terminal_seen
    assert actual_ledger.terminal_seen

    expected = build_forge_host_trace(expected_envelopes)
    actual = build_forge_host_trace(actual_envelopes)
    mismatches = compare_forge_host_traces(expected, actual)

    assert [(mismatch.record_index, mismatch.path) for mismatch in mismatches] == [
        (3, ("operationId",)),
        (4, ("operationId",)),
        (5, ("operationId",)),
        (6, ("operationId",)),
    ]
    assert expected.records[3].operation_id == "operation_run00001"
    assert actual.records[3].operation_id == "operation_run99999"


def test_trace_ignores_only_envelope_transport_metadata() -> None:
    original_plan = _validated("plan-ready-event.json")
    noisy_plan = _load("plan-ready-event.json")
    noisy_plan["messageId"] = "msg_transport01"
    noisy_plan["sentAt"] = "2027-01-02T03:04:05Z"
    noisy_plan["sequence"] = 77
    validated_noisy_plan = validate_envelope(noisy_plan)
    assert isinstance(validated_noisy_plan, EventEnvelope)

    expected = build_forge_host_trace([original_plan])
    actual = build_forge_host_trace([validated_noisy_plan])

    assert compare_forge_host_traces(expected, actual) == ()


def test_event_authority_identity_is_preserved_and_compared() -> None:
    changed = _load("approval-required-event.json")
    changed["identity"]["sessionId"] = "ses_otherauth1"
    changed["identity"]["generationId"] = "gen_otherauth1"
    _recompute_approval_authority(changed)
    other_authority = validate_envelope(changed)
    assert isinstance(other_authority, EventEnvelope)

    expected = build_forge_host_trace([_validated("approval-required-event.json")])
    actual = build_forge_host_trace([other_authority])
    mismatches = compare_forge_host_traces(expected, actual)

    paths = [mismatch.path for mismatch in mismatches]
    assert paths == [
        ("authority",),
        ("identity",),
        ("payload", "actionFingerprint"),
    ]
    authority = mismatches[paths.index(("authority",))]
    assert authority.expected == ("ses_abcdefgh", "gen_abcdefgh", None, 1)
    assert authority.actual == ("ses_otherauth1", "gen_otherauth1", None, 1)


def test_trace_revalidates_model_copy_and_model_construct_bypasses() -> None:
    plan = _validated("plan-ready-event.json")
    assert isinstance(plan, EventEnvelope)
    invalid_copy_payload = plan.payload.model_copy(update={"host_plan_fingerprint": "0" * 64})
    invalid_copy = plan.model_copy(update={"payload": invalid_copy_payload})
    invalid_construct_payload = type(plan.payload).model_construct(
        type="plan.ready",
        plan_definition=plan.payload.plan_definition,
        host_plan_fingerprint="0" * 64,
        native_approval_definition=plan.payload.native_approval_definition,
        native_approval_fingerprint=plan.payload.native_approval_fingerprint,
        verification_contract=plan.payload.verification_contract,
        execution_admission=plan.payload.execution_admission,
        approval_policy=plan.payload.approval_policy,
    )
    invalid_construct = plan.model_copy(update={"payload": invalid_construct_payload})

    for bypass in (invalid_copy, invalid_construct):
        with pytest.raises(ForgeConformanceInputError, match="wire revalidation"):
            build_forge_host_trace([bypass])


def test_trace_and_record_constructors_are_not_public_or_callable() -> None:
    assert not hasattr(conformance_module, "ForgeHostTrace")
    assert not hasattr(conformance_module, "ForgeHostTraceRecord")
    trace = build_forge_host_trace([_validated("plan-ready-event.json")])

    with pytest.raises(TypeError, match="created only"):
        type(trace)()
    with pytest.raises(TypeError, match="created only"):
        type(trace.records[0])()


def test_comparator_revalidates_private_wire_storage() -> None:
    trace = build_forge_host_trace([_validated("plan-ready-event.json")])
    object.__setattr__(trace.records[0], "_envelope_json", '{"not":"an envelope"}')

    with pytest.raises(ForgeConformanceInputError, match="revalidation"):
        compare_forge_host_traces(trace, trace)


def test_trace_retains_canonical_plan_order_and_all_run_contract_fields() -> None:
    trace = _canonical_run_trace()
    records = {record.message_type: record for record in trace.records}
    plan = records["plan.ready"].payload_dict()
    start = records["run.start"].payload_dict()
    evidence = records["evidence.ready"].payload_dict()
    terminal = records["run.terminal"].payload_dict()["result"]

    tasks = plan["planDefinition"]["tasks"]
    assert [task["taskId"] for task in tasks] == ["T01"]
    assert [task["order"] for task in tasks] == [1]
    assert tasks[0]["dependencies"] == []
    assert tasks[0]["taskKind"] == "implementation"
    assert tasks[0]["acceptance"][0]["criterionId"] == "T01:criterion:1"
    assert tasks[0]["writeScopes"] == [{"path": "src/café.py", "kind": "file"}]
    assert plan["planDefinition"]["planningConstraints"] == {
        "schemaVersion": 1,
        "targetRoots": [
            {
                "kind": "target_root",
                "path": "src",
                "reasonCode": "user_target_root",
                "evidence": "The user explicitly scoped implementation to src.",
            }
        ],
        "forbiddenRoots": [
            {
                "kind": "forbidden_root",
                "path": "vendor",
                "reasonCode": "user_forbidden_root",
                "evidence": "The user required vendor to remain untouched.",
            }
        ],
        "decoyRoots": [],
        "unrelatedRoots": [],
    }
    assert plan["approvalPolicy"]["required"] is True
    assert plan["approvalPolicy"]["fingerprint"]
    assert plan["nativeApprovalDefinition"]["schema"] == ("alysis.forge-native-approval/v2")

    assert start["hostPlanFingerprint"] == plan["hostPlanFingerprint"]
    assert start["nativeApprovalFingerprint"] == plan["nativeApprovalFingerprint"]
    assert (
        start["verificationContractFingerprint"]
        == (plan["verificationContract"]["contractFingerprint"])
    )
    assert start["executionAdmission"] == plan["executionAdmission"]
    admission = start["executionAdmission"]
    assert admission["providerBindingRef"] == "provider_abcdefgh"
    assert admission["executionStrategy"] == {
        "mode": "parallel",
        "maxParallelWorkers": 2,
        "integrationPolicy": "collect_then_integrate",
    }
    assert admission["perManagedTaskMaxSteps"] == 64
    assert admission["runTimeoutMs"] == 600000
    assert admission["fingerprint"]

    assert evidence["evidence"][0]["role"] == "acceptance"
    assert evidence["evidence"][0]["commandIds"] == []
    assert evidence["evidence"][0]["realExecution"] is None
    assert evidence["evidence"][0]["exitCode"] is None
    assert evidence["evidence"][0]["timedOut"] is None
    assert evidence["evidence"][1]["role"] == "verification"
    assert evidence["evidence"][1]["commandIds"] == ["command.pytest"]
    assert evidence["evidence"][1]["realExecution"] is True
    assert evidence["evidence"][1]["exitCode"] == 0
    assert evidence["evidence"][1]["timedOut"] is False
    assert evidence["artifacts"][0]["workspaceRelativePath"] == "reports/verification.json"
    assert evidence["artifacts"][0]["digest"]

    assert terminal["status"] == "succeeded"
    assert terminal["hostPlanFingerprint"] == plan["hostPlanFingerprint"]
    assert terminal["nativeApprovalFingerprint"] == plan["nativeApprovalFingerprint"]
    assert (
        terminal["verificationContractFingerprint"]
        == (plan["verificationContract"]["contractFingerprint"])
    )
    assert terminal["executionAdmissionFingerprint"] == admission["fingerprint"]
    assert terminal["reason"] == {
        "code": "completed",
        "message": "All approved checks passed.",
        "retryable": False,
    }
    assert terminal["acceptance"]["status"] == "passed"
    assert terminal["verification"]["status"] == "passed"
    assert terminal["usage"] == {
        "modelCalls": 3,
        "inputTokens": 1200,
        "outputTokens": 400,
        "costKnown": True,
        "totalCostMicros": 12345,
    }
    assert terminal["timing"] == {
        "startedAt": "2026-08-21T09:01:00Z",
        "finishedAt": "2026-08-21T09:02:00Z",
        "elapsedMs": 60000,
        "timedOut": False,
    }
    assert terminal["taskUsage"] == [{"taskId": "T01", "modelSteps": 3}]
    assert terminal["workers"] == {
        "requested": 1,
        "started": 1,
        "succeeded": 1,
        "failed": 0,
    }


def test_equivalent_aware_datetime_offsets_canonicalize_to_utc() -> None:
    changed = _load("terminal-event.json")
    timing = changed["payload"]["result"]["timing"]
    timing["startedAt"] = "2026-08-21T11:01:00+02:00"
    timing["finishedAt"] = "2026-08-21T11:02:00+02:00"
    shifted = validate_envelope(changed)
    assert isinstance(shifted, EventEnvelope)

    expected = build_forge_host_trace([_validated("terminal-event.json")])
    actual = build_forge_host_trace([shifted])

    assert compare_forge_host_traces(expected, actual) == ()
    assert actual.records[0].payload_dict()["result"]["timing"]["finishedAt"] == (
        "2026-08-21T09:02:00Z"
    )


def test_plan_task_array_retains_validated_protocol_order_without_sorting() -> None:
    trace = build_forge_host_trace([_validated("plan-ready-event.json")])
    tasks = trace.records[0].payload_dict()["planDefinition"]["tasks"]

    assert [(task["order"], task["taskId"]) for task in tasks] == [(1, "T01")]

    invalid = _load("plan-ready-event.json")
    invalid["payload"]["planDefinition"]["tasks"][0]["order"] = 2
    with pytest.raises(ProtocolValidationError):
        validate_envelope(invalid)


def test_record_identities_are_nested_tuples_not_concatenated_strings() -> None:
    trace = build_forge_host_trace([_validated("evidence-ready-event.json")])

    assert trace.records[0].identity == (
        "evidence.ready",
        ("evidence_accept01", "evidence_verify01"),
        ("artifact_report001",),
    )
    assert isinstance(trace.records[0].identity[1], tuple)
    assert isinstance(trace.records[0].identity[2], tuple)


def test_terminal_diagnostics_cover_reason_usage_cost_timing_workers_and_verification() -> None:
    changed = _load("terminal-event.json")
    result = changed["payload"]["result"]
    result["reason"]["retryable"] = True
    result["usage"]["totalCostMicros"] = 42001
    result["timing"]["finishedAt"] = "2026-08-21T09:01:01Z"
    result["timing"]["elapsedMs"] = 1000
    result["workers"]["requested"] = 3
    result["workers"]["started"] = 3
    result["workers"]["succeeded"] = 3
    result["verification"]["checks"][0]["summary"] = "A different canonical summary."
    actual_terminal = validate_envelope(changed)
    assert isinstance(actual_terminal, EventEnvelope)

    mismatches = compare_forge_host_traces(
        build_forge_host_trace([_validated("terminal-event.json")]),
        build_forge_host_trace([actual_terminal]),
    )

    assert [mismatch.path for mismatch in mismatches] == [
        ("payload", "result", "reason", "retryable"),
        ("payload", "result", "timing", "elapsedMs"),
        ("payload", "result", "timing", "finishedAt"),
        ("payload", "result", "usage", "totalCostMicros"),
        ("payload", "result", "verification", "checks", 0, "summary"),
        ("payload", "result", "workers", "requested"),
        ("payload", "result", "workers", "started"),
        ("payload", "result", "workers", "succeeded"),
    ]


def test_acceptance_and_verification_remain_distinct_terminal_domains() -> None:
    changed = _load("terminal-event.json")
    changed["payload"]["result"]["acceptance"]["checks"][0]["summary"] = (
        "A different acceptance summary."
    )
    actual_terminal = validate_envelope(changed)
    assert isinstance(actual_terminal, EventEnvelope)

    mismatches = compare_forge_host_traces(
        build_forge_host_trace([_validated("terminal-event.json")]),
        build_forge_host_trace([actual_terminal]),
    )

    assert [mismatch.path for mismatch in mismatches] == [
        ("payload", "result", "acceptance", "checks", 0, "summary")
    ]
    assert all("verification" not in mismatch.path for mismatch in mismatches)


def test_run_start_budget_timeout_and_worker_strategy_are_compared() -> None:
    changed = _load("run-start-request.json")
    admission = changed["payload"]["executionAdmission"]
    admission["executionStrategy"]["maxParallelWorkers"] = 5
    admission["perManagedTaskMaxSteps"] = 51
    admission["runTimeoutMs"] = 900001
    admission["fingerprint"] = execution_admission_fingerprint(admission)
    actual_start = validate_envelope(changed)
    assert isinstance(actual_start, RequestEnvelope)

    mismatches = compare_forge_host_traces(
        build_forge_host_trace([_validated("run-start-request.json")]),
        build_forge_host_trace([actual_start]),
    )

    assert [mismatch.path for mismatch in mismatches] == [
        ("identity",),
        ("payload", "executionAdmission", "executionStrategy", "maxParallelWorkers"),
        ("payload", "executionAdmission", "fingerprint"),
        ("payload", "executionAdmission", "perManagedTaskMaxSteps"),
        ("payload", "executionAdmission", "runTimeoutMs"),
    ]


def test_unknown_provider_cost_remains_distinct_from_a_known_zero_cost() -> None:
    changed = _load("terminal-event.json")
    usage = changed["payload"]["result"]["usage"]
    usage["costKnown"] = False
    usage["totalCostMicros"] = None
    actual_terminal = validate_envelope(changed)
    assert isinstance(actual_terminal, EventEnvelope)

    mismatches = compare_forge_host_traces(
        build_forge_host_trace([_validated("terminal-event.json")]),
        build_forge_host_trace([actual_terminal]),
    )

    assert [mismatch.path for mismatch in mismatches] == [
        ("payload", "result", "usage", "costKnown"),
        ("payload", "result", "usage", "totalCostMicros"),
    ]


def test_approval_identity_includes_action_scope_and_event_authority() -> None:
    changed = _load("approval-required-event.json")
    changed["payload"]["scope"]["resources"][0]["path"] = "src/other.py"
    _recompute_approval_authority(changed)
    actual_approval = validate_envelope(changed)
    assert isinstance(actual_approval, EventEnvelope)

    expected = build_forge_host_trace([_validated("approval-required-event.json")])
    actual = build_forge_host_trace([actual_approval])
    mismatches = compare_forge_host_traces(expected, actual)

    assert expected.records[0].authority == ("ses_abcdefgh", "gen_abcdefgh", None, 1)
    assert mismatches[0].path == ("identity",)
    assert isinstance(mismatches[0].expected, tuple)
    assert ("payload", "scope", "fingerprint") in {mismatch.path for mismatch in mismatches}


def test_paths_and_opaque_refs_are_preserved_without_normalization() -> None:
    trace = build_forge_host_trace(
        [
            _validated("plan-ready-event.json"),
            _validated("evidence-ready-event.json"),
        ]
    )

    assert (
        trace.records[0].payload_dict()["planDefinition"]["tasks"][0]["writeScopes"][0]["path"]
        == "src/café.py"
    )
    assert trace.records[1].payload_dict()["artifacts"][0]["workspaceRelativePath"] == (
        "reports/verification.json"
    )
    assert trace.records[1].payload_dict()["evidence"][0]["artifactRefs"] == ["artifact_report001"]

    invalid = _load("evidence-ready-event.json")
    invalid["payload"]["artifacts"][0]["workspaceRelativePath"] = "/tmp/report.json"
    with pytest.raises((ProtocolValidationError, UnsafeSerializationError)):
        validate_envelope(invalid)


def test_raw_or_unsupported_envelopes_fail_closed() -> None:
    with pytest.raises(ForgeConformanceInputError, match="raw mappings"):
        build_forge_host_trace([_load("plan-ready-event.json")])  # type: ignore[list-item]

    with pytest.raises(ForgeConformanceInputError, match="outside the focused"):
        build_forge_host_trace([_validated("snapshot-event.json")])


def test_precise_error_reports_structured_path_and_labels() -> None:
    changed = _load("terminal-event.json")
    changed["payload"]["result"]["usage"]["modelCalls"] = 4
    actual_terminal = validate_envelope(changed)
    assert isinstance(actual_terminal, EventEnvelope)

    expected = build_forge_host_trace(
        [_validated("terminal-event.json")], label="canonical-expected"
    )
    actual = build_forge_host_trace([actual_terminal], label="canonical-actual")

    with pytest.raises(ForgeTraceConformanceError) as caught:
        assert_forge_host_traces_equal(expected, actual)

    mismatch = caught.value.mismatches[0]
    assert mismatch.path == ("payload", "result", "usage", "modelCalls")
    assert mismatch.location() == "records[0].payload.result.usage.modelCalls"
    assert "canonical-expected != canonical-actual" in str(caught.value)
    assert "expected 3, actual 4" in str(caught.value)
