from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from alysis_code.forge_host_protocol import (
    PHASE0_APPROVAL_SCOPE_LABEL,
    PHASE0_APPROVAL_SUMMARY,
    ApprovalStateError,
    CanonicalJSONError,
    EventSequenceError,
    IdempotencyConflictError,
    Identity,
    PlanStateError,
    ProtocolLedger,
    ProtocolValidationError,
    ReferenceResolutionError,
    ReplayUnavailableError,
    RequestDisposition,
    RevisionGapError,
    StaleIdentityError,
    TerminalEventError,
    UnsafeSerializationError,
    approval_action_fingerprint,
    approval_policy_fingerprint,
    approval_scope_fingerprint,
    canonical_fingerprint,
    canonical_json_text,
    execution_admission_fingerprint,
    load_schema,
    negotiate_handshake,
    plan_fingerprint,
    to_wire_dict,
    validate_envelope,
    verification_contract_fingerprint,
)
from alysis_code.forge_host_protocol.ledger import PendingApproval
from alysis_code.forge_host_protocol.models import (
    ENVELOPE_ADAPTER,
    Capability,
    GitBranchName,
    HandshakeRequest,
    NativePlanTask,
    Peer,
    PlanningConstraints,
    PlanTask,
    WorkspaceScope,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "schemas" / "forge-host-v2" / "fixtures"
NOW = datetime(2026, 8, 21, 9, 0, tzinfo=timezone.utc)
EXPIRES = "2026-08-21T09:10:00Z"
ZERO_FP = "0" * 64


def identity(*, run: bool = False, revision: int = 1) -> dict[str, object]:
    return {
        "sessionId": "ses_abcdefgh",
        "generationId": "gen_abcdefgh",
        "runId": "run_abcdefgh" if run else None,
        "revision": revision,
    }


def envelope_base(
    *,
    kind: str,
    message: str,
    operation: str | None,
) -> dict[str, object]:
    return {
        "protocol": "alysis.forge-host",
        "protocolVersion": "2.0",
        "messageId": f"msg_{message}",
        "operationId": operation,
        "sentAt": "2026-08-21T09:00:00Z",
        "kind": kind,
    }


def request(
    payload: dict[str, object],
    *,
    message: str,
    operation: str | None,
    request_id: str | None = None,
    idempotency: str | None = None,
) -> dict[str, object]:
    return {
        **envelope_base(kind="request", message=message, operation=operation),
        "requestId": request_id or f"req_{message}",
        "idempotencyKey": idempotency or f"idem_{message}",
        "payload": payload,
    }


def event(
    payload: dict[str, object],
    *,
    message: str,
    operation: str,
    sequence: int,
    run: bool,
    revision: int,
) -> dict[str, object]:
    return {
        **envelope_base(kind="event", message=message, operation=operation),
        "sequence": sequence,
        "identity": identity(run=run, revision=revision),
        "payload": payload,
    }


def host_plan_definition() -> dict[str, object]:
    return {
        "schema": "alysis.forge-host.plan/v2",
        "projectGoal": "Build the /api route for Café reports.",
        "summary": "Create and verify the scoped report module.",
        "requirements": ["Focused tests pass.", "No private paths leave the host."],
        "planningConstraints": {
            "schemaVersion": 1,
            "targetRoots": [
                {
                    "path": "src",
                    "kind": "target_root",
                    "reasonCode": "user_target_root",
                    "evidence": "The user explicitly scoped implementation to src.",
                }
            ],
            "forbiddenRoots": [
                {
                    "path": "vendor",
                    "kind": "forbidden_root",
                    "reasonCode": "user_forbidden_root",
                    "evidence": "The user required vendor to remain untouched.",
                }
            ],
            "decoyRoots": [],
            "unrelatedRoots": [],
        },
        "assetRefs": ["asset_requirements01"],
        "intent": {
            "kind": "build",
            "summary": "Add one repository-native report module.",
            "affectedScopes": [{"path": "src", "kind": "directory"}],
        },
        "tasks": [
            {
                "taskId": "T01",
                "order": 1,
                "taskKind": "implementation",
                "branch": "feat/T01-cafe-report",
                "dependencies": [],
                "title": "Implement Café report",
                "description": "Create the scoped application behavior.",
                "acceptance": [
                    {
                        "criterionId": "T01:criterion:1",
                        "description": "Focused tests pass.",
                    }
                ],
                "estimatedScopes": [{"path": "src/**", "kind": "directory"}],
                "writeScopes": [{"path": "src/café.py", "kind": "file"}],
            }
        ],
    }


def native_approval_definition() -> dict[str, object]:
    return {
        "schema": "alysis.forge-native-approval/v2",
        "project_goal": "Build the /api route for Café reports.",
        "summary": "Create and verify the scoped report module.",
        "requirements": ["Focused tests pass.", "No private paths leave the host."],
        "planning_constraints": {
            "schema_version": 1,
            "target_roots": [
                {
                    "path": "src",
                    "kind": "target_root",
                    "reason_code": "user_target_root",
                    "evidence": "The user explicitly scoped implementation to src.",
                }
            ],
            "forbidden_roots": [
                {
                    "path": "vendor",
                    "kind": "forbidden_root",
                    "reason_code": "user_forbidden_root",
                    "evidence": "The user required vendor to remain untouched.",
                }
            ],
            "decoy_roots": [],
            "unrelated_roots": [],
        },
        "assets": ["asset_requirements01"],
        "intent_record": {
            "kind": "build",
            "summary": "Add one repository-native report module.",
            "affected_scopes": [{"path": "src", "kind": "directory"}],
        },
        "tasks": [
            {
                "id": "T01",
                "task_kind": "implementation",
                "branch": "feat/T01-cafe-report",
                "dependencies": [],
                "title": "Implement Café report",
                "description": "Create the scoped application behavior.",
                "acceptance_criteria": ["Focused tests pass."],
                "estimated_files": [{"path": "src/**", "kind": "directory"}],
                "write_scope": [{"path": "src/café.py", "kind": "file"}],
            }
        ],
    }


def verification_contract(
    *,
    host_fingerprint: str,
    native_fingerprint: str,
) -> dict[str, object]:
    contract: dict[str, object] = {
        "schema": "alysis.forge.verification-contract/v2",
        "protocolVersion": 2,
        "workspaceRef": "workspace_abcdefgh",
        "approvedPlanFingerprint": native_fingerprint,
        "hostPlanFingerprint": host_fingerprint,
        "scopes": [
            {
                "scopeId": "scope.T01",
                "phase": "task",
                "taskId": "T01",
                "cwd": ".",
                "referencedPaths": ["src/café.py"],
                "allowedOutputs": ["src/café.py"],
                "declaredInputs": [],
            }
        ],
        "requirements": [
            {
                "requirementId": "T01:criterion:1",
                "taskId": "T01",
                "text": "Focused tests pass.",
                "provenance": "HOST_AUTHORITATIVE",
                "trustLevel": "TRUSTED",
                "requirement": "REQUIRED",
            }
        ],
        "commands": [
            {
                "commandId": "command.pytest",
                "taskId": "T01",
                "scopeId": "scope.T01",
                "originalText": "pytest -q",
                "displayText": "pytest -q",
                "executionMode": "ARGV",
                "provenance": "PREEXISTING_REPO_NATIVE",
                "trustLevel": "TRUSTED",
                "requirement": "REQUIRED",
                "timeoutPolicy": "session_verification_timeout",
                "requirementIds": ["T01:criterion:1"],
                "validationStatus": "VALID",
                "rejectionReason": "",
                "evidentiaryCapability": "ASSERTIVE",
                "capabilityReason": "recognized_assertive_verification_command",
                "argv": ["pytest", "-q"],
            }
        ],
        "checks": [
            {
                "checkId": "check.acceptance.T01",
                "domain": "acceptance",
                "taskId": "T01",
                "requirementIds": ["T01:criterion:1"],
                "commandIds": [],
            },
            {
                "checkId": "check.verification.T01",
                "domain": "verification",
                "taskId": "T01",
                "requirementIds": ["T01:criterion:1"],
                "commandIds": ["command.pytest"],
            },
        ],
        "contractFingerprint": ZERO_FP,
    }
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    return contract


def plan_ready_payload(*, required: bool = True) -> dict[str, object]:
    plan = host_plan_definition()
    host_fp = plan_fingerprint(plan)
    native = native_approval_definition()
    native_fp = canonical_fingerprint(native)
    contract = verification_contract(
        host_fingerprint=host_fp,
        native_fingerprint=native_fp,
    )
    scope: dict[str, object] = {
        "schema": "alysis.forge-host.approval-scope/v2",
        "kind": "filesystem",
        "label": PHASE0_APPROVAL_SCOPE_LABEL,
        "workspaceRef": "workspace_abcdefgh",
        "resources": [{"access": "write", "kind": "file", "path": "src/café.py"}],
        "fingerprint": ZERO_FP,
    }
    scope["fingerprint"] = approval_scope_fingerprint(scope)
    summary = PHASE0_APPROVAL_SUMMARY
    risk = "high"
    admission: dict[str, object] = {
        "schema": "alysis.forge-host.execution-admission/v2",
        "workspaceRef": "workspace_abcdefgh",
        "providerBindingRef": "provider_abcdefgh",
        "workspacePathPolicy": {
            "comparison": "case_insensitive",
            "unicodeNormalization": "NFC",
            "symlinkPolicy": "resolve_beneath_workspace",
        },
        "executionStrategy": {
            "mode": "parallel",
            "maxParallelWorkers": 2,
            "integrationPolicy": "collect_then_integrate",
        },
        "perManagedTaskMaxSteps": 64,
        "runTimeoutMs": 600_000,
        "fingerprint": ZERO_FP,
    }
    admission["fingerprint"] = execution_admission_fingerprint(admission)
    policy: dict[str, object] = {
        "required": required,
        "scope": scope if required else None,
        "risk": risk if required else None,
        "summary": summary if required else None,
        "fingerprint": ZERO_FP,
    }
    policy["fingerprint"] = approval_policy_fingerprint(policy)
    return {
        "type": "plan.ready",
        "planDefinition": plan,
        "hostPlanFingerprint": host_fp,
        "nativeApprovalDefinition": native,
        "nativeApprovalFingerprint": native_fp,
        "verificationContract": contract,
        "executionAdmission": admission,
        "approvalPolicy": policy,
    }


def rebind_plan_payload(plan: dict[str, object]) -> dict[str, object]:
    definition = plan["planDefinition"]
    native = plan["nativeApprovalDefinition"]
    contract = plan["verificationContract"]
    admission = plan["executionAdmission"]
    policy = plan["approvalPolicy"]
    assert all(isinstance(item, dict) for item in (definition, native, contract, admission, policy))
    host_fp = plan_fingerprint(definition)
    native_fp = canonical_fingerprint(native)
    plan["hostPlanFingerprint"] = host_fp
    plan["nativeApprovalFingerprint"] = native_fp
    contract["hostPlanFingerprint"] = host_fp
    contract["approvedPlanFingerprint"] = native_fp
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    admission["fingerprint"] = execution_admission_fingerprint(admission)
    scope = policy.get("scope")
    if isinstance(scope, dict):
        scope["fingerprint"] = approval_scope_fingerprint(scope)
    policy["fingerprint"] = approval_policy_fingerprint(policy)
    return plan


def plan_event(
    *, required: bool = True, plan_payload: dict[str, object] | None = None
) -> dict[str, object]:
    return event(
        plan_payload or plan_ready_payload(required=required),
        message="planready001",
        operation="operation_plan0001",
        sequence=1,
        run=False,
        revision=1,
    )


def approval_required_event(
    *,
    approval_id: str = "approval_abcdefgh",
    operation: str = "operation_approve01",
    message: str = "approvalreq1",
    sequence: int = 2,
    revision: int = 1,
    expires_at: str = EXPIRES,
    plan_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    plan = plan_payload or plan_ready_payload(required=True)
    policy = plan["approvalPolicy"]
    assert isinstance(policy, dict)
    contract = plan["verificationContract"]
    assert isinstance(contract, dict)
    admission = plan["executionAdmission"]
    assert isinstance(admission, dict)
    action = {
        "kind": "plan_execute",
        "hostPlanFingerprint": plan["hostPlanFingerprint"],
        "nativeApprovalFingerprint": plan["nativeApprovalFingerprint"],
        "verificationContractFingerprint": contract["contractFingerprint"],
        "executionAdmissionFingerprint": admission["fingerprint"],
        "approvalPolicyFingerprint": policy["fingerprint"],
    }
    action_fp = approval_action_fingerprint(
        operation_id=operation,
        session_id="ses_abcdefgh",
        generation_id="gen_abcdefgh",
        run_id=None,
        revision=revision,
        approval_id=approval_id,
        action=action,
        scope_fingerprint=str(policy["scope"]["fingerprint"]),
        expires_at=expires_at,
        risk=str(policy["risk"]),
        summary=str(policy["summary"]),
    )
    payload = {
        "type": "approval.required",
        "approvalId": approval_id,
        "action": action,
        "actionFingerprint": action_fp,
        "summary": policy["summary"],
        "risk": policy["risk"],
        "scope": policy["scope"],
        "expiresAt": expires_at,
    }
    return event(
        payload,
        message=message,
        operation=operation,
        sequence=sequence,
        run=False,
        revision=revision,
    )


def approval_decision_request(
    *,
    decision: str = "allow_once",
    required_event: dict[str, object] | None = None,
) -> dict[str, object]:
    required = required_event or approval_required_event()
    approval = required["payload"]
    assert isinstance(approval, dict)
    scope = approval["scope"]
    assert isinstance(scope, dict)
    return request(
        {
            "type": "approval.decision",
            "identity": identity(run=False, revision=1),
            "expectedRevision": 1,
            "approvalOperationId": required["operationId"],
            "approvalId": approval["approvalId"],
            "actionFingerprint": approval["actionFingerprint"],
            "scopeFingerprint": scope["fingerprint"],
            "expiresAt": approval["expiresAt"],
            "decision": decision,
        },
        message="approvaldec1",
        operation="operation_decide01",
    )


def run_start_request(
    *,
    idempotency: str = "idem_runstart01",
    plan_payload: dict[str, object] | None = None,
) -> dict[str, object]:
    plan = plan_payload or plan_ready_payload(required=True)
    contract = plan["verificationContract"]
    assert isinstance(contract, dict)
    return request(
        {
            "type": "run.start",
            "identity": identity(run=False, revision=1),
            "expectedRevision": 1,
            "hostPlanFingerprint": plan["hostPlanFingerprint"],
            "nativeApprovalFingerprint": plan["nativeApprovalFingerprint"],
            "verificationContractFingerprint": contract["contractFingerprint"],
            "executionAdmission": plan["executionAdmission"],
        },
        message="runstart0001",
        operation="operation_run00001",
        idempotency=idempotency,
    )


def runtime_approval_required_event(*, sequence: int = 4) -> dict[str, object]:
    scope: dict[str, object] = {
        "schema": "alysis.forge-host.approval-scope/v2",
        "kind": "provider",
        "label": "Use the locked provider for recovery",
        "providerBindingRef": "provider_abcdefgh",
        "operation": "recover",
        "fingerprint": ZERO_FP,
    }
    scope["fingerprint"] = approval_scope_fingerprint(scope)
    action = {"kind": "provider_use", "subjectFingerprint": "b" * 64}
    action_fp = approval_action_fingerprint(
        operation_id="operation_run00001",
        session_id="ses_abcdefgh",
        generation_id="gen_abcdefgh",
        run_id="run_abcdefgh",
        revision=2,
        approval_id="approval_runtime01",
        action=action,
        scope_fingerprint=str(scope["fingerprint"]),
        expires_at=EXPIRES,
        risk="high",
        summary="Permit one provider recovery call.",
    )
    return event(
        {
            "type": "approval.required",
            "approvalId": "approval_runtime01",
            "action": action,
            "actionFingerprint": action_fp,
            "summary": "Permit one provider recovery call.",
            "risk": "high",
            "scope": scope,
            "expiresAt": EXPIRES,
        },
        message="approvalrun1",
        operation="operation_run00001",
        sequence=sequence,
        run=True,
        revision=2,
    )


def progress_event(
    *,
    sequence: int = 3,
    revision: int = 2,
    task_model_steps: int = 3,
) -> dict[str, object]:
    return event(
        {
            "type": "run.progress",
            "phase": "executing",
            "taskId": "T01",
            "step": 1,
            "totalSteps": 2,
            "taskModelSteps": task_model_steps,
            "summary": "Executing the admitted task.",
            "workerId": "worker_abcdefgh",
        },
        message="progress0001",
        operation="operation_run00001",
        sequence=sequence,
        run=True,
        revision=revision,
    )


def evidence_event(*, sequence: int = 4, revision: int = 2) -> dict[str, object]:
    artifact = {
        "artifactId": "artifact_report001",
        "role": "report",
        "kind": "report",
        "status": "created",
        "workspaceRelativePath": "reports/verification.json",
        "digest": "a" * 64,
        "sizeBytes": 321,
    }
    return event(
        {
            "type": "evidence.ready",
            "evidence": [
                {
                    "evidenceId": "evidence_accept01",
                    "role": "acceptance",
                    "kind": "test",
                    "status": "passed",
                    "checkId": "check.acceptance.T01",
                    "taskId": "T01",
                    "requirementIds": ["T01:criterion:1"],
                    "commandIds": [],
                    "realExecution": None,
                    "exitCode": None,
                    "timedOut": None,
                    "summary": "Acceptance behavior passed.",
                    "artifactRefs": ["artifact_report001"],
                },
                {
                    "evidenceId": "evidence_verify01",
                    "role": "verification",
                    "kind": "test",
                    "status": "passed",
                    "checkId": "check.verification.T01",
                    "taskId": "T01",
                    "requirementIds": ["T01:criterion:1"],
                    "commandIds": ["command.pytest"],
                    "realExecution": True,
                    "exitCode": 0,
                    "timedOut": False,
                    "summary": "Repository-native pytest passed.",
                    "artifactRefs": ["artifact_report001"],
                },
            ],
            "artifacts": [artifact],
        },
        message="evidence001",
        operation="operation_run00001",
        sequence=sequence,
        run=True,
        revision=revision,
    )


def terminal_event(
    *, sequence: int = 5, plan_payload: dict[str, object] | None = None
) -> dict[str, object]:
    plan = plan_payload or plan_ready_payload(required=True)
    contract = plan["verificationContract"]
    assert isinstance(contract, dict)
    return event(
        {
            "type": "run.terminal",
            "result": {
                "hostPlanFingerprint": plan["hostPlanFingerprint"],
                "nativeApprovalFingerprint": plan["nativeApprovalFingerprint"],
                "verificationContractFingerprint": contract["contractFingerprint"],
                "executionAdmissionFingerprint": plan["executionAdmission"]["fingerprint"],
                "status": "succeeded",
                "reason": {
                    "code": "completed",
                    "message": "All approved checks passed.",
                    "retryable": False,
                },
                "exitCode": 0,
                "acceptance": {
                    "status": "passed",
                    "checks": [
                        {
                            "checkId": "check.acceptance.T01",
                            "taskId": "T01",
                            "requirementIds": ["T01:criterion:1"],
                            "status": "passed",
                            "summary": "Acceptance passed.",
                            "evidenceRefs": ["evidence_accept01"],
                        }
                    ],
                },
                "verification": {
                    "status": "passed",
                    "checks": [
                        {
                            "checkId": "check.verification.T01",
                            "taskId": "T01",
                            "requirementIds": ["T01:criterion:1"],
                            "status": "passed",
                            "summary": "Verification passed.",
                            "evidenceRefs": ["evidence_verify01"],
                        }
                    ],
                },
                "usage": {
                    "modelCalls": 3,
                    "inputTokens": 1200,
                    "outputTokens": 400,
                    "costKnown": True,
                    "totalCostMicros": 12345,
                },
                "timing": {
                    "startedAt": "2026-08-21T09:01:00Z",
                    "finishedAt": "2026-08-21T09:02:00Z",
                    "elapsedMs": 60_000,
                    "timedOut": False,
                },
                "workers": {"requested": 1, "started": 1, "succeeded": 1, "failed": 0},
                "taskUsage": [{"taskId": "T01", "modelSteps": 3}],
                "artifactRefs": ["artifact_report001"],
            },
        },
        message="terminal0001",
        operation="operation_run00001",
        sequence=sequence,
        run=True,
        revision=3,
    )


def typed(value: dict[str, object]):
    return validate_envelope(value)


def schema_validator():
    jsonschema = pytest.importorskip("jsonschema")
    return jsonschema.Draft202012Validator(load_schema())


def planner_input_request() -> dict[str, object]:
    return json.loads((FIXTURES / "valid/planner-input-request.json").read_text(encoding="utf-8"))


def planner_answer_request(
    question_id: str,
    *,
    operation: str = "operation_answer001",
    message: str = "answer0001",
) -> dict[str, object]:
    return request(
        {
            "type": "planner.input",
            "identity": identity(run=False, revision=1),
            "expectedRevision": 1,
            "input": {
                "kind": "answer",
                "questionId": question_id,
                "text": "Use the repository-native target.",
            },
        },
        message=message,
        operation=operation,
    )


def planner_question_event(
    *,
    question_id: str = "question_abcdefgh",
    operation: str = "operation_plan0001",
    sequence: int = 1,
    required: bool = True,
) -> dict[str, object]:
    return event(
        {
            "type": "planner.question",
            "questionId": question_id,
            "prompt": "Which workspace-relative target should Forge update?",
            "required": required,
        },
        message="question0001",
        operation=operation,
        sequence=sequence,
        run=False,
        revision=1,
    )


def planning_ledger(*, clock: Callable[[], datetime] | None = None) -> ProtocolLedger:
    ledger = ProtocolLedger(
        Identity.model_validate(identity(run=False, revision=1)),
        clock=clock or (lambda: NOW),
    )
    ledger.accept_request(typed(planner_input_request()))
    return ledger


def admitted_ledger(*, approve: bool = True) -> ProtocolLedger:
    ledger = planning_ledger()
    ledger.accept_event(typed(plan_event()))
    ledger.accept_event(typed(approval_required_event()))
    if approve:
        ledger.accept_request(typed(approval_decision_request()))
    return ledger


def running_ledger(*, progress_steps: int | None = 3) -> ProtocolLedger:
    ledger = admitted_ledger()
    ledger.accept_request(typed(run_start_request()))
    ledger.bind_run(Identity.model_validate(identity(run=True, revision=2)))
    if progress_steps is not None:
        ledger.accept_event(typed(progress_event(task_model_steps=progress_steps)))
    return ledger


def test_schema_is_valid_draft_2020_12() -> None:
    validator = schema_validator()
    validator.check_schema(load_schema())


def test_checked_schema_and_typed_models_have_identical_object_boundaries() -> None:
    checked = load_schema()
    generated = ENVELOPE_ADAPTER.json_schema(by_alias=True, mode="validation")
    assert set(generated["$defs"]).issubset(checked["$defs"])
    for name, typed_definition in generated["$defs"].items():
        wire_definition = checked["$defs"][name]
        assert wire_definition.get("additionalProperties") == typed_definition.get(
            "additionalProperties"
        ), name
        assert wire_definition.get("required") == typed_definition.get("required"), name
        assert set(wire_definition.get("properties", {})) == set(
            typed_definition.get("properties", {})
        ), name
        for field, typed_property in typed_definition.get("properties", {}).items():
            wire_property = wire_definition["properties"][field]
            for keyword in (
                "$ref",
                "const",
                "enum",
                "maximum",
                "minimum",
                "maxItems",
                "minItems",
                "maxLength",
                "minLength",
                "type",
            ):
                if keyword in typed_property:
                    assert wire_property.get(keyword) == typed_property[keyword], (
                        name,
                        field,
                        keyword,
                    )


def test_current_app_canonical_json_golden_is_preserved() -> None:
    # Vendored copy of the desktop app's canonical-JSON golden (Alysis Code app
    # tests/fixtures/forge-plan-approval-canonical-golden.json); both sides must agree.
    golden = json.loads(
        (
            Path(__file__).parent
            / "fixtures"
            / "forge_host_v2"
            / "forge-plan-approval-canonical-golden.json"
        ).read_text(encoding="utf-8")
    )
    semantic_projection = json.loads(golden["canonical"])
    assert canonical_json_text(semantic_projection) == golden["canonical"]
    assert canonical_fingerprint(semantic_projection) == golden["fingerprint"]


def test_host_plan_fingerprint_binds_every_authored_field() -> None:
    plan = host_plan_definition()
    fingerprint = plan_fingerprint(plan)
    for mutation in (
        lambda value: value.__setitem__("projectGoal", "Changed goal"),
        lambda value: value["requirements"].append("Changed requirement"),
        lambda value: value["tasks"][0].__setitem__("branch", "feat/changed"),
        lambda value: value["tasks"][0].__setitem__("description", "Changed task"),
        lambda value: value["tasks"][0]["writeScopes"][0].__setitem__("path", "src/other.py"),
    ):
        changed = copy.deepcopy(plan)
        mutation(changed)
        assert plan_fingerprint(changed) != fingerprint


def test_public_task_kinds_and_structured_constraints_round_trip_losslessly() -> None:
    canonical_kinds = {
        "analysis_only",
        "implementation",
        "test_only",
        "verification_only",
        "documentation_only",
        "unknown",
    }
    base_task = host_plan_definition()["tasks"][0]
    for task_kind in canonical_kinds:
        PlanTask.model_validate({**base_task, "taskKind": task_kind})
    for noncanonical in ("verification", "integration", "documentation"):
        with pytest.raises(ValidationError):
            PlanTask.model_validate({**base_task, "taskKind": noncanonical})

    constraints = host_plan_definition()["planningConstraints"]
    constraints["targetRoots"].append(
        {
            "path": "Src",
            "kind": "target_root",
            "reasonCode": "user_target_root",
            "evidence": "A distinct case-sensitive POSIX path.",
        }
    )
    parsed = PlanningConstraints.model_validate(constraints)
    assert [item.path for item in parsed.target_roots] == ["src", "Src"]

    wrong_bucket = copy.deepcopy(constraints)
    wrong_bucket["forbiddenRoots"][0]["kind"] = "target_root"
    with pytest.raises(ValidationError, match="collection"):
        PlanningConstraints.model_validate(wrong_bucket)


def test_native_plan_mapping_preserves_constraints_and_task_lifecycle() -> None:
    payload = typed(plan_event()).payload
    assert payload.type == "plan.ready"
    native_constraints = payload.native_approval_definition.planning_constraints
    assert payload.plan_definition.planning_constraints.model_dump(
        mode="json", by_alias=False
    ) == native_constraints.model_dump(mode="json", by_alias=False)
    native_task = payload.native_approval_definition.tasks[0]
    assert payload.plan_definition.tasks[0].task_kind == native_task.task_kind


def test_plan_ready_rejects_retained_fingerprint_after_semantic_mutation() -> None:
    value = plan_event()
    value["payload"]["planDefinition"]["tasks"][0]["title"] = "Spoofed title"
    with pytest.raises(ProtocolValidationError):
        typed(value)


def test_native_definition_and_contract_fingerprints_are_recomputed() -> None:
    native = plan_event()
    native["payload"]["nativeApprovalDefinition"]["summary"] = "Changed"
    with pytest.raises(ProtocolValidationError):
        typed(native)

    contract = plan_event()
    contract["payload"]["verificationContract"]["commands"][0]["argv"].append("-q")
    with pytest.raises(ProtocolValidationError):
        typed(contract)


def test_scope_and_action_fingerprints_are_recomputed() -> None:
    scope = plan_event()
    scope["payload"]["approvalPolicy"]["scope"]["label"] = "Spoofed scope"
    with pytest.raises(ProtocolValidationError):
        typed(scope)

    action = approval_required_event()
    action["payload"]["summary"] = "Spoofed approval summary"
    with pytest.raises(ProtocolValidationError):
        typed(action)


def test_execution_admission_and_approval_policy_fingerprints_are_recomputed() -> None:
    admission = plan_event()
    admission["payload"]["executionAdmission"]["providerBindingRef"] = "provider_other0001"
    with pytest.raises(ProtocolValidationError):
        typed(admission)

    policy = plan_event()
    policy["payload"]["approvalPolicy"]["risk"] = "critical"
    with pytest.raises(ProtocolValidationError):
        typed(policy)

    action = approval_required_event()
    action["payload"]["action"]["executionAdmissionFingerprint"] = "f" * 64
    with pytest.raises(ProtocolValidationError):
        typed(action)


def test_human_planner_text_is_not_subject_to_path_or_secret_heuristics() -> None:
    value = request(
        {
            "type": "planner.input",
            "identity": identity(run=False, revision=1),
            "expectedRevision": 1,
            "input": {
                "kind": "goal",
                "text": "Implement /api, document file://, and discuss Bearer dummy-token-format.",
            },
        },
        message="planner0001",
        operation="operation_plan0001",
    )
    assert typed(value).payload.type == "planner.input"


@pytest.mark.parametrize(
    "command",
    [
        "python /Users/alice/private/test.py",
        "python C:/Users/alice/private/test.py",
        "python \\\\server\\share\\test.py",
        "curl -H 'Bearer sk-abcdefghijklmnop' https://example.test",
        "echo AKIAABCDEFGHIJKLMNOP",
        "echo eyJabcdefghij.abcdefghij.abcdefghij",
    ],
)
def test_structured_command_secret_and_private_path_boundary_is_fail_closed(command: str) -> None:
    value = plan_event()
    command_spec = value["payload"]["verificationContract"]["commands"][0]
    command_spec["originalText"] = command
    command_spec["displayText"] = command
    command_spec["argv"] = [command]
    with pytest.raises(UnsafeSerializationError):
        typed(value)


def test_validation_error_never_echoes_raw_invalid_value_or_pydantic_text() -> None:
    secret = "sk-supersecretvalue123456789"
    value = request(
        {
            "type": "planner.input",
            "identity": identity(run=False, revision=1),
            "expectedRevision": 1,
            "input": {"kind": "goal", "text": secret * 1000},
        },
        message="planner0002",
        operation="operation_plan0002",
    )
    with pytest.raises(ProtocolValidationError) as caught:
        typed(value)
    rendered = str(caught.value)
    assert caught.value.code == "invalid_message"
    assert secret not in rendered
    assert "input_value" not in rendered


@pytest.mark.parametrize("field", ["workspaceRoot", "worktreePath", "homeDir"])
@pytest.mark.parametrize("private_root", ["/Users/alice/repo", "C:/Users/alice/repo"])
def test_native_approval_projection_rejects_private_host_roots(
    field: str, private_root: str
) -> None:
    value = plan_event()
    value["payload"]["nativeApprovalDefinition"][field] = private_root
    with pytest.raises(UnsafeSerializationError, match="private host path"):
        typed(value)


@pytest.mark.parametrize("field", ["token", "apiToken", "authToken"])
def test_native_approval_projection_rejects_secret_bearing_keys(field: str) -> None:
    value = plan_event()
    value["payload"]["nativeApprovalDefinition"][field] = "redacted-placeholder"
    with pytest.raises(UnsafeSerializationError, match="secret-bearing field"):
        typed(value)


def test_javascript_safe_integer_bounds_and_nfc_paths() -> None:
    huge = approval_decision_request()
    huge["payload"]["expectedRevision"] = 9_007_199_254_740_992
    huge["payload"]["identity"]["revision"] = 9_007_199_254_740_992
    with pytest.raises(ProtocolValidationError):
        typed(huge)

    nfd = evidence_event()
    nfd["payload"]["artifacts"][0]["workspaceRelativePath"] = "reports/cafe\u0301.json"
    with pytest.raises(ProtocolValidationError):
        typed(nfd)


def test_directory_and_recursive_scope_containment_are_valid() -> None:
    task = PlanTask.model_validate(host_plan_definition()["tasks"][0])
    assert task.write_scopes[0].path == "src/café.py"
    detached = PlanTask.model_validate(
        {
            **host_plan_definition()["tasks"][0],
            "estimatedScopes": [{"path": "tests/**", "kind": "directory"}],
        }
    )
    assert detached.estimated_scopes[0].path == "tests/**"

    payload = plan_ready_payload()
    payload["planDefinition"]["tasks"][0]["estimatedScopes"] = [
        {"path": "tests/**", "kind": "directory"}
    ]
    payload["nativeApprovalDefinition"]["tasks"][0]["estimated_files"] = [
        {"path": "tests/**", "kind": "directory"}
    ]
    with pytest.raises(ProtocolValidationError):
        typed(plan_event(plan_payload=rebind_plan_payload(payload)))


def test_same_path_file_directory_substitution_never_widens_scope() -> None:
    estimated = plan_ready_payload()
    estimated["planDefinition"]["tasks"][0]["estimatedScopes"] = [
        {"path": "src/café.py", "kind": "directory"}
    ]
    estimated["nativeApprovalDefinition"]["tasks"][0]["estimated_files"] = [
        {"path": "src/café.py", "kind": "directory"}
    ]
    with pytest.raises(ProtocolValidationError):
        typed(plan_event(plan_payload=rebind_plan_payload(estimated)))

    target = plan_ready_payload()
    target["planDefinition"]["planningConstraints"]["targetRoots"][0]["path"] = "src/café.py"
    target["nativeApprovalDefinition"]["planning_constraints"]["target_roots"][0]["path"] = (
        "src/café.py"
    )
    for definition, intent_key, estimated_key in (
        (target["planDefinition"], "intent", "estimatedScopes"),
        (target["nativeApprovalDefinition"], "intent_record", "estimated_files"),
    ):
        definition[intent_key][
            "affectedScopes" if intent_key == "intent" else "affected_scopes"
        ] = [{"path": "src/café.py", "kind": "file"}]
        definition["tasks"][0][estimated_key] = [{"path": "src/café.py", "kind": "file"}]
    with pytest.raises(ProtocolValidationError):
        typed(plan_event(plan_payload=rebind_plan_payload(target)))

    verification = plan_ready_payload()
    verification["planDefinition"]["tasks"][0]["writeScopes"][0]["kind"] = "directory"
    verification["nativeApprovalDefinition"]["tasks"][0]["write_scope"][0]["kind"] = "directory"
    verification["approvalPolicy"]["scope"]["resources"][0]["kind"] = "directory"
    with pytest.raises(ProtocolValidationError):
        typed(plan_event(plan_payload=rebind_plan_payload(verification)))

    forbidden = plan_ready_payload()
    forbidden["planDefinition"]["planningConstraints"]["forbiddenRoots"][0]["path"] = "src/café.py"
    forbidden["nativeApprovalDefinition"]["planning_constraints"]["forbidden_roots"][0]["path"] = (
        "src/café.py"
    )
    with pytest.raises(ProtocolValidationError):
        typed(plan_event(plan_payload=rebind_plan_payload(forbidden)))

    approval = plan_event()
    policy = approval["payload"]["approvalPolicy"]
    policy["scope"]["resources"][0]["kind"] = "directory"
    policy["scope"]["fingerprint"] = approval_scope_fingerprint(policy["scope"])
    policy["fingerprint"] = approval_policy_fingerprint(policy)
    with pytest.raises(ProtocolValidationError):
        typed(approval)


@pytest.mark.parametrize(
    "branch",
    json.loads((FIXTURES / "git-branch-vectors.json").read_text(encoding="utf-8"))["invalid"],
)
def test_task_branches_match_portable_git_branch_rules(branch: str) -> None:
    host = host_plan_definition()["tasks"][0]
    host["branch"] = branch
    with pytest.raises(ValidationError, match="Git branch"):
        PlanTask.model_validate(host)

    native = native_approval_definition()["tasks"][0]
    native["branch"] = branch
    with pytest.raises(ValidationError, match="Git branch"):
        NativePlanTask.model_validate(native)

    message = plan_event()
    message["payload"]["planDefinition"]["tasks"][0]["branch"] = branch
    message["payload"]["nativeApprovalDefinition"]["tasks"][0]["branch"] = branch
    assert list(schema_validator().iter_errors(message))
    with pytest.raises(ProtocolValidationError):
        typed(message)


def test_git_branch_vectors_match_the_repository_git_authority() -> None:
    vectors = json.loads((FIXTURES / "git-branch-vectors.json").read_text(encoding="utf-8"))
    for branch in vectors["valid"]:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (branch, result.stderr)
    for branch in vectors["invalid"]:
        result = subprocess.run(
            ["git", "check-ref-format", "--branch", branch],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, branch


def test_reserved_head_branch_rejects_after_full_plan_rebinding() -> None:
    with pytest.raises(ValidationError, match="Git branch"):
        TypeAdapter(GitBranchName).validate_python("HEAD", strict=True)

    payload = plan_ready_payload()
    payload["planDefinition"]["tasks"][0]["branch"] = "HEAD"
    payload["nativeApprovalDefinition"]["tasks"][0]["branch"] = "HEAD"
    message = plan_event(plan_payload=rebind_plan_payload(payload))
    assert list(schema_validator().iter_errors(message))
    with pytest.raises(ProtocolValidationError):
        typed(message)


def test_task_branch_aliases_follow_bound_repository_comparison_policy() -> None:
    def two_task_plan(comparison: str) -> dict[str, object]:
        payload = plan_ready_payload()
        host_task = copy.deepcopy(payload["planDefinition"]["tasks"][0])
        host_task.update(
            taskId="T02",
            order=2,
            branch="Feat/T01-cafe-report",
            dependencies=["T01"],
        )
        host_task["acceptance"][0]["criterionId"] = "T02:criterion:1"
        payload["planDefinition"]["tasks"].append(host_task)

        native_task = copy.deepcopy(payload["nativeApprovalDefinition"]["tasks"][0])
        native_task.update(
            id="T02",
            branch="Feat/T01-cafe-report",
            dependencies=["T01"],
        )
        payload["nativeApprovalDefinition"]["tasks"].append(native_task)

        contract = payload["verificationContract"]
        scope = copy.deepcopy(contract["scopes"][0])
        scope.update(scopeId="scope.T02", taskId="T02")
        contract["scopes"].append(scope)
        requirement = copy.deepcopy(contract["requirements"][0])
        requirement.update(requirementId="T02:criterion:1", taskId="T02")
        contract["requirements"].append(requirement)
        command = copy.deepcopy(contract["commands"][0])
        command.update(
            commandId="command.pytest.T02",
            taskId="T02",
            scopeId="scope.T02",
            requirementIds=["T02:criterion:1"],
        )
        contract["commands"].append(command)
        acceptance = copy.deepcopy(contract["checks"][0])
        acceptance.update(
            checkId="check.acceptance.T02",
            taskId="T02",
            requirementIds=["T02:criterion:1"],
        )
        verification = copy.deepcopy(contract["checks"][1])
        verification.update(
            checkId="check.verification.T02",
            taskId="T02",
            requirementIds=["T02:criterion:1"],
            commandIds=["command.pytest.T02"],
        )
        contract["checks"].extend((acceptance, verification))
        payload["executionAdmission"]["workspacePathPolicy"]["comparison"] = comparison
        return rebind_plan_payload(payload)

    vectors = json.loads((FIXTURES / "git-branch-vectors.json").read_text(encoding="utf-8"))
    for vector in vectors["aliasPolicies"]:
        message = plan_event(plan_payload=two_task_plan(vector["comparison"]))
        if vector["valid"]:
            typed(message)
        else:
            with pytest.raises(ProtocolValidationError):
                typed(message)


def test_plan_approval_and_verification_outputs_cannot_widen_write_authority() -> None:
    approval = plan_event()
    policy = approval["payload"]["approvalPolicy"]
    scope = policy["scope"]
    scope["resources"] = [{"access": "write", "kind": "directory", "path": "src/**"}]
    scope["fingerprint"] = approval_scope_fingerprint(scope)
    policy["fingerprint"] = approval_policy_fingerprint(policy)
    with pytest.raises(ProtocolValidationError):
        typed(approval)

    verification = plan_event()
    contract = verification["payload"]["verificationContract"]
    contract_scope = contract["scopes"][0]
    contract_scope["referencedPaths"] = ["vendor/generated.py"]
    contract_scope["allowedOutputs"] = ["vendor/generated.py"]
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    with pytest.raises(ProtocolValidationError):
        typed(verification)


def test_workspace_path_policy_binds_case_and_symlink_authority() -> None:
    def aliased_plan(comparison: str) -> dict[str, object]:
        plan = plan_ready_payload()
        host = plan["planDefinition"]
        native = plan["nativeApprovalDefinition"]
        contract = plan["verificationContract"]
        admission = plan["executionAdmission"]
        policy = plan["approvalPolicy"]
        host["planningConstraints"]["targetRoots"][0]["path"] = "Vendor"
        host["planningConstraints"]["forbiddenRoots"][0]["path"] = "vendor"
        host["intent"]["affectedScopes"] = [{"path": "Vendor", "kind": "directory"}]
        host["tasks"][0]["estimatedScopes"] = [{"path": "Vendor/**", "kind": "directory"}]
        host["tasks"][0]["writeScopes"] = [{"path": "Vendor/pwn.py", "kind": "file"}]
        native["planning_constraints"]["target_roots"][0]["path"] = "Vendor"
        native["planning_constraints"]["forbidden_roots"][0]["path"] = "vendor"
        native["intent_record"]["affected_scopes"] = [{"path": "Vendor", "kind": "directory"}]
        native["tasks"][0]["estimated_files"] = [{"path": "Vendor/**", "kind": "directory"}]
        native["tasks"][0]["write_scope"] = [{"path": "Vendor/pwn.py", "kind": "file"}]
        contract["scopes"][0].update(
            referencedPaths=["Vendor/pwn.py"],
            allowedOutputs=["Vendor/pwn.py"],
        )
        admission["workspacePathPolicy"]["comparison"] = comparison
        policy["scope"]["resources"] = [
            {"access": "write", "kind": "file", "path": "Vendor/pwn.py"}
        ]
        return rebind_plan_payload(plan)

    with pytest.raises(ProtocolValidationError):
        typed(plan_event(plan_payload=aliased_plan("case_insensitive")))
    typed(plan_event(plan_payload=aliased_plan("case_sensitive")))

    invalid_symlink_policy = plan_event()
    admission = invalid_symlink_policy["payload"]["executionAdmission"]
    admission["workspacePathPolicy"]["symlinkPolicy"] = "trust_lexical_path"
    admission["fingerprint"] = execution_admission_fingerprint(admission)
    with pytest.raises(ProtocolValidationError):
        typed(invalid_symlink_policy)


def test_workspace_path_policy_controls_alias_uniqueness_and_containment() -> None:
    def duplicate_target_plan(comparison: str) -> dict[str, object]:
        payload = plan_ready_payload()
        host_constraints = payload["planDefinition"]["planningConstraints"]
        native_constraints = payload["nativeApprovalDefinition"]["planning_constraints"]
        extra_host = copy.deepcopy(host_constraints["targetRoots"][0])
        extra_host["path"] = "Src"
        host_constraints["targetRoots"].append(extra_host)
        extra_native = copy.deepcopy(native_constraints["target_roots"][0])
        extra_native["path"] = "Src"
        native_constraints["target_roots"].append(extra_native)
        payload["executionAdmission"]["workspacePathPolicy"]["comparison"] = comparison
        return rebind_plan_payload(payload)

    with pytest.raises(ProtocolValidationError):
        typed(plan_event(plan_payload=duplicate_target_plan("case_insensitive")))
    typed(plan_event(plan_payload=duplicate_target_plan("case_sensitive")))

    def cross_case_containment(comparison: str) -> dict[str, object]:
        payload = plan_ready_payload()
        payload["planDefinition"]["tasks"][0]["estimatedScopes"] = [
            {"path": "SRC/**", "kind": "directory"}
        ]
        payload["nativeApprovalDefinition"]["tasks"][0]["estimated_files"] = [
            {"path": "SRC/**", "kind": "directory"}
        ]
        payload["executionAdmission"]["workspacePathPolicy"]["comparison"] = comparison
        return rebind_plan_payload(payload)

    typed(plan_event(plan_payload=cross_case_containment("case_insensitive")))
    with pytest.raises(ProtocolValidationError):
        typed(plan_event(plan_payload=cross_case_containment("case_sensitive")))


def test_phase0_approval_is_universal_and_presentation_is_deterministic() -> None:
    write_without_approval = plan_event(required=False)
    with pytest.raises(ProtocolValidationError):
        typed(write_without_approval)

    harmless = plan_event()
    policy = harmless["payload"]["approvalPolicy"]
    policy["risk"] = "low"
    policy["summary"] = "Harmless display-only action."
    policy["scope"]["label"] = "No meaningful changes"
    policy["scope"]["fingerprint"] = approval_scope_fingerprint(policy["scope"])
    policy["fingerprint"] = approval_policy_fingerprint(policy)
    with pytest.raises(ProtocolValidationError):
        typed(harmless)

    no_write = plan_ready_payload()
    no_write["planDefinition"]["tasks"][0]["writeScopes"] = []
    no_write["nativeApprovalDefinition"]["tasks"][0]["write_scope"] = []
    scope = no_write["verificationContract"]["scopes"][0]
    scope["allowedOutputs"] = []
    scope["declaredInputs"] = list(scope["referencedPaths"])
    no_write["approvalPolicy"] = {
        "required": False,
        "scope": None,
        "risk": None,
        "summary": None,
        "fingerprint": ZERO_FP,
    }
    rebind_plan_payload(no_write)
    with pytest.raises(ProtocolValidationError):
        typed(plan_event(plan_payload=no_write))


def test_semantic_uniqueness_timing_workers_and_task_order_are_fail_closed() -> None:
    with pytest.raises(ValidationError, match="capability names"):
        HandshakeRequest.model_validate(
            {
                "type": "handshake.request",
                "client": {
                    "name": "AlysisDesktop",
                    "version": "1.2.3",
                    "hostKind": "desktop",
                },
                "supportedVersions": ["2.0"],
                "capabilities": [
                    {"name": "forge.plan", "version": 1},
                    {"name": "forge.plan", "version": 2},
                ],
                "replayCursor": None,
            }
        )

    reversed_timing = terminal_event()
    timing = reversed_timing["payload"]["result"]["timing"]
    timing["startedAt"] = "2026-08-21T09:03:00Z"
    timing["finishedAt"] = "2026-08-21T09:02:00Z"
    with pytest.raises(ProtocolValidationError):
        typed(reversed_timing)

    incoherent_workers = terminal_event()
    incoherent_workers["payload"]["result"]["workers"] = {
        "requested": 1,
        "started": 2,
        "succeeded": 2,
        "failed": 0,
    }
    with pytest.raises(ProtocolValidationError):
        typed(incoherent_workers)

    duplicate_task = plan_event()
    task = copy.deepcopy(duplicate_task["payload"]["planDefinition"]["tasks"][0])
    task["order"] = 2
    duplicate_task["payload"]["planDefinition"]["tasks"].append(task)
    duplicate_task["payload"]["hostPlanFingerprint"] = plan_fingerprint(
        duplicate_task["payload"]["planDefinition"]
    )
    with pytest.raises(ProtocolValidationError):
        typed(duplicate_task)


def test_handshake_rejects_replay_without_authenticated_checkpoint() -> None:
    request_model = HandshakeRequest.model_validate(
        {
            "type": "handshake.request",
            "client": {"name": "AlysisDesktop", "version": "1.2.3", "hostKind": "desktop"},
            "supportedVersions": ["2.0"],
            "capabilities": [{"name": "forge.plan", "version": 2}],
            "replayCursor": {
                "operationId": "operation_run00001",
                "identity": identity(run=True, revision=3),
                "afterSequence": 4,
            },
        }
    )
    with pytest.raises(ReplayUnavailableError, match="authenticated durable"):
        negotiate_handshake(
            request_model,
            server=Peer(name="AlysisEngine", version="2.0.0", host_kind="engine"),
            server_capabilities=[Capability(name="forge.plan", version=2)],
            identity=Identity.model_validate(identity(run=True, revision=3)),
            replay_through_sequence=7,
        )


def test_ledger_accepts_exact_approval_run_evidence_and_one_terminal() -> None:
    ledger = running_ledger()
    ledger.accept_event(typed(evidence_event()))
    ledger.accept_event(typed(terminal_event()))
    ledger.require_terminal()
    assert ledger.last_sequence == 5
    assert ledger.terminal_seen
    assert ledger.evidence_ids == {"evidence_accept01", "evidence_verify01"}


def test_unsolicited_and_mismatched_approval_decisions_are_rejected() -> None:
    ledger = ProtocolLedger(
        Identity.model_validate(identity(run=False, revision=1)), clock=lambda: NOW
    )
    with pytest.raises(ApprovalStateError, match="no matching"):
        ledger.accept_request(typed(approval_decision_request()))

    ledger = admitted_ledger(approve=False)
    mismatch = approval_decision_request()
    mismatch["payload"]["approvalOperationId"] = "operation_wrong001"
    with pytest.raises(ApprovalStateError, match="operation authority"):
        ledger.accept_request(typed(mismatch))

    for field in ("actionFingerprint", "scopeFingerprint", "expiresAt"):
        ledger = admitted_ledger(approve=False)
        mismatch = approval_decision_request()
        mismatch["payload"][field] = "2026-08-21T09:11:00Z" if field == "expiresAt" else "f" * 64
        with pytest.raises(ApprovalStateError, match="fingerprints"):
            ledger.accept_request(typed(mismatch))

    ledger = admitted_ledger(approve=False)
    wrong_identity = approval_decision_request()
    wrong_identity["payload"]["identity"]["generationId"] = "gen_other0001"
    with pytest.raises(StaleIdentityError):
        ledger.accept_request(typed(wrong_identity))


def test_runtime_approval_required_fails_closed_without_registered_subject() -> None:
    ledger = running_ledger()
    with pytest.raises(ApprovalStateError, match="runtime approval subjects are unavailable"):
        ledger.accept_event(typed(runtime_approval_required_event()))


def test_preplan_generic_runtime_approval_is_rejected() -> None:
    value = runtime_approval_required_event(sequence=1)
    value["operationId"] = "operation_runtime01"
    value["identity"] = identity(run=False, revision=1)
    payload = value["payload"]
    payload["actionFingerprint"] = approval_action_fingerprint(
        operation_id="operation_runtime01",
        session_id="ses_abcdefgh",
        generation_id="gen_abcdefgh",
        run_id=None,
        revision=1,
        approval_id=payload["approvalId"],
        action=payload["action"],
        scope_fingerprint=payload["scope"]["fingerprint"],
        expires_at=payload["expiresAt"],
        risk=payload["risk"],
        summary=payload["summary"],
    )
    ledger = ProtocolLedger(
        Identity.model_validate(identity(run=False, revision=1)), clock=lambda: NOW
    )
    with pytest.raises(ApprovalStateError, match="runtime approval subjects are unavailable"):
        ledger.accept_event(typed(value))


def test_expired_approval_required_and_decision_are_rejected() -> None:
    late = planning_ledger(clock=lambda: NOW + timedelta(hours=1))
    late.accept_event(typed(plan_event()))
    with pytest.raises(ApprovalStateError, match="expired"):
        late.accept_event(typed(approval_required_event()))

    clock = [NOW]
    ledger = planning_ledger(clock=lambda: clock[0])
    ledger.accept_event(typed(plan_event()))
    ledger.accept_event(typed(approval_required_event()))
    clock[0] = NOW + timedelta(hours=1)
    with pytest.raises(ApprovalStateError, match="expired"):
        ledger.accept_request(typed(approval_decision_request()))


def test_plan_approval_can_be_reissued_after_deny_without_redefining_plan() -> None:
    plan = plan_event()
    assert set(plan["payload"]["approvalPolicy"]) == {
        "required",
        "scope",
        "risk",
        "summary",
        "fingerprint",
    }
    ledger = admitted_ledger(approve=False)
    ledger.accept_request(typed(approval_decision_request(decision="deny")))

    reissued = approval_required_event(
        approval_id="approval_reissue01",
        operation="operation_approve02",
        message="approvalreq2",
        sequence=3,
    )
    ledger.accept_event(typed(reissued))
    decision = approval_decision_request(required_event=reissued)
    decision["messageId"] = "msg_approvaldec2"
    decision["requestId"] = "req_approvaldec2"
    decision["idempotencyKey"] = "idem_approvaldec2"
    decision["operationId"] = "operation_decide02"
    ledger.accept_request(typed(decision))
    ledger.accept_request(typed(run_start_request()))


def test_plan_approval_can_be_reissued_after_prior_issuance_expires() -> None:
    clock = [NOW]
    ledger = planning_ledger(clock=lambda: clock[0])
    ledger.accept_event(typed(plan_event()))
    ledger.accept_event(typed(approval_required_event()))
    clock[0] = NOW + timedelta(minutes=20)
    reissued = approval_required_event(
        approval_id="approval_reissue02",
        operation="operation_approve03",
        message="approvalreq3",
        sequence=3,
        expires_at="2026-08-21T09:30:00Z",
    )
    ledger.accept_event(typed(reissued))
    assert "approval_abcdefgh" in ledger.consumed_approval_ids
    assert ledger.pending_approval_ids == {"approval_reissue02"}
    decision = approval_decision_request(required_event=reissued)
    ledger.accept_request(typed(decision))
    ledger.accept_request(typed(run_start_request()))


def test_unused_allow_once_authority_expires_and_can_be_reissued() -> None:
    clock = [NOW]
    ledger = planning_ledger(clock=lambda: clock[0])
    ledger.accept_event(typed(plan_event()))
    ledger.accept_event(typed(approval_required_event()))
    ledger.accept_request(typed(approval_decision_request()))
    clock[0] = NOW + timedelta(minutes=20)
    with pytest.raises(PlanStateError, match="allow-once.*expired"):
        ledger.accept_request(typed(run_start_request()))

    reissued = approval_required_event(
        approval_id="approval_reissue03",
        operation="operation_approve05",
        message="approvalreq5",
        sequence=3,
        expires_at="2026-08-21T09:30:00Z",
    )
    ledger.accept_event(typed(reissued))
    decision = approval_decision_request(required_event=reissued)
    decision["messageId"] = "msg_approvaldec3"
    decision["requestId"] = "req_approvaldec3"
    decision["idempotencyKey"] = "idem_approvaldec3"
    decision["operationId"] = "operation_decide03"
    ledger.accept_request(typed(decision))
    ledger.accept_request(typed(run_start_request()))


def test_active_plan_approval_cannot_be_concurrently_reissued() -> None:
    ledger = admitted_ledger(approve=False)
    concurrent = approval_required_event(
        approval_id="approval_concurrent01",
        operation="operation_approve04",
        message="approvalreq4",
        sequence=3,
    )
    with pytest.raises(ApprovalStateError, match="already pending"):
        ledger.accept_event(typed(concurrent))


def test_allow_once_and_deny_are_consumed_exactly_once() -> None:
    ledger = admitted_ledger()
    decision = typed(approval_decision_request())
    assert ledger.accept_request(decision) == RequestDisposition.REPLAY
    replay_with_new_idempotency = approval_decision_request()
    replay_with_new_idempotency["idempotencyKey"] = "idem_approvaldec2"
    replay_with_new_idempotency["messageId"] = "msg_approvaldec2"
    replay_with_new_idempotency["requestId"] = "req_approvaldec2"
    replay_with_new_idempotency["operationId"] = "operation_decide02"
    with pytest.raises(ApprovalStateError, match="consumed"):
        ledger.accept_request(typed(replay_with_new_idempotency))

    start = typed(run_start_request())
    ledger.accept_request(start)
    assert ledger.accept_request(start) == RequestDisposition.REPLAY
    second = run_start_request(idempotency="idem_runstart02")
    second["messageId"] = "msg_runstart0002"
    second["requestId"] = "req_runstart0002"
    second["operationId"] = "operation_run00002"
    with pytest.raises(PlanStateError, match="consumed"):
        ledger.accept_request(typed(second))

    denied = admitted_ledger(approve=False)
    denied.accept_request(typed(approval_decision_request(decision="deny")))
    with pytest.raises(PlanStateError, match="not been approved"):
        denied.accept_request(typed(run_start_request()))


def test_run_start_requires_all_plan_and_contract_fingerprints() -> None:
    for field in (
        "hostPlanFingerprint",
        "nativeApprovalFingerprint",
        "verificationContractFingerprint",
    ):
        ledger = admitted_ledger()
        value = run_start_request()
        value["payload"][field] = "f" * 64
        with pytest.raises(PlanStateError, match="does not match"):
            ledger.accept_request(typed(value))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda admission: admission.__setitem__("providerBindingRef", "provider_other0001"),
        lambda admission: admission["executionStrategy"].__setitem__("maxParallelWorkers", 3),
        lambda admission: admission.__setitem__("perManagedTaskMaxSteps", 65),
        lambda admission: admission.__setitem__("runTimeoutMs", 700_000),
    ],
)
def test_run_start_cannot_tamper_with_approved_execution_admission(mutation) -> None:
    ledger = admitted_ledger()
    value = run_start_request()
    admission = value["payload"]["executionAdmission"]
    mutation(admission)
    admission["fingerprint"] = execution_admission_fingerprint(admission)
    with pytest.raises(PlanStateError, match="execution admission"):
        ledger.accept_request(typed(value))


def test_duplicate_evidence_and_artifact_ids_cannot_be_redefined() -> None:
    for collection, field in (("evidence", "evidenceId"), ("artifacts", "artifactId")):
        ledger = running_ledger()
        first = evidence_event()
        ledger.accept_event(typed(first))
        duplicate = copy.deepcopy(first)
        duplicate["sequence"] = 5
        duplicate["messageId"] = "msg_evidence002"
        duplicate["identity"]["revision"] = 3
        duplicate["payload"][collection][0][field] = first["payload"][collection][0][field]
        with pytest.raises(ReferenceResolutionError, match="immutable"):
            ledger.accept_event(typed(duplicate))


def test_succeeded_terminal_requires_matching_passed_role_evidence() -> None:
    mutations = (
        lambda value: value["payload"]["result"]["acceptance"].__setitem__("checks", []),
        lambda value: value["payload"]["result"]["acceptance"]["checks"][0].__setitem__(
            "checkId", "check.verification.T01"
        ),
        lambda value: value["payload"]["result"]["acceptance"]["checks"][0].__setitem__(
            "requirementIds", ["unknown.requirement"]
        ),
    )
    for mutation in mutations:
        ledger = running_ledger()
        ledger.accept_event(typed(evidence_event()))
        value = terminal_event()
        mutation(value)
        with pytest.raises((ProtocolValidationError, ReferenceResolutionError)):
            ledger.accept_event(typed(value))

    ledger = running_ledger()
    failed_evidence = evidence_event()
    failed_evidence["payload"]["evidence"][0]["status"] = "failed"
    ledger.accept_event(typed(failed_evidence))
    with pytest.raises(ReferenceResolutionError, match="passed matching evidence"):
        ledger.accept_event(typed(terminal_event()))


@pytest.mark.parametrize(
    "mutation",
    [
        lambda result: result.__setitem__("exitCode", 1),
        lambda result: result["timing"].__setitem__("timedOut", True),
        lambda result: result["workers"].__setitem__("failed", 1),
        lambda result: result["acceptance"].__setitem__("checks", []),
        lambda result: result["acceptance"]["checks"][0].__setitem__("status", "not_run"),
        lambda result: result["verification"]["checks"][0].__setitem__("status", "skipped"),
    ],
)
def test_contradictory_or_evidence_free_success_is_model_invalid(mutation) -> None:
    value = terminal_event()
    mutation(value["payload"]["result"])
    with pytest.raises(ProtocolValidationError):
        typed(value)


def test_provider_cost_can_be_explicitly_unknown_without_becoming_zero() -> None:
    value = terminal_event()
    usage = value["payload"]["result"]["usage"]
    usage["costKnown"] = False
    usage["totalCostMicros"] = None
    typed(value)

    ledger = running_ledger()
    ledger.accept_event(typed(evidence_event()))
    ledger.accept_event(typed(value))
    ledger.require_terminal()

    for known, total in ((False, 0), (True, None)):
        contradictory = terminal_event()
        contradictory_usage = contradictory["payload"]["result"]["usage"]
        contradictory_usage["costKnown"] = known
        contradictory_usage["totalCostMicros"] = total
        with pytest.raises(ProtocolValidationError):
            typed(contradictory)


def test_terminal_fingerprints_are_bound_to_the_admitted_run() -> None:
    ledger = running_ledger()
    ledger.accept_event(typed(evidence_event()))
    value = terminal_event()
    value["payload"]["result"]["hostPlanFingerprint"] = "f" * 64
    with pytest.raises(PlanStateError, match="fingerprints"):
        ledger.accept_event(typed(value))

    admission = terminal_event()
    admission["payload"]["result"]["executionAdmissionFingerprint"] = "f" * 64
    with pytest.raises(PlanStateError, match="fingerprints"):
        ledger.accept_event(typed(admission))


def test_runtime_approval_rejection_does_not_create_pending_authority() -> None:
    ledger = running_ledger()
    with pytest.raises(ApprovalStateError):
        ledger.accept_event(typed(runtime_approval_required_event()))
    assert not ledger.pending_approval_ids


def test_terminal_is_final_not_merely_unique() -> None:
    ledger = running_ledger()
    ledger.accept_event(typed(evidence_event()))
    ledger.accept_event(typed(terminal_event()))
    after = evidence_event()
    after["sequence"] = 6
    after["messageId"] = "msg_evidenceafter"
    after["identity"]["revision"] = 4
    with pytest.raises(TerminalEventError, match="final"):
        ledger.accept_event(typed(after))
    with pytest.raises(TerminalEventError, match="final"):
        ledger.accept_request(typed(run_start_request()))


def test_sequence_revision_identity_and_replay_cursor_invariants() -> None:
    with pytest.raises(EventSequenceError, match="authenticated durable checkpoint"):
        ProtocolLedger(
            Identity.model_validate(identity(run=False, revision=1)),
            after_sequence=4,
            clock=lambda: NOW,
        )
    ledger = ProtocolLedger(
        Identity.model_validate(identity(run=False, revision=1)), clock=lambda: NOW
    )
    value = plan_event()
    value["sequence"] = 2
    with pytest.raises(EventSequenceError):
        ledger.accept_event(typed(value))

    gap = plan_event()
    gap["sequence"] = 1
    gap["identity"]["revision"] = 3
    with pytest.raises(RevisionGapError):
        ledger.accept_event(typed(gap))

    stale = plan_event()
    stale["sequence"] = 1
    stale["identity"]["generationId"] = "gen_other0001"
    with pytest.raises(StaleIdentityError):
        ledger.accept_event(typed(stale))


def test_phase0_ledger_requires_planner_correlation_and_engine_allocated_run() -> None:
    with pytest.raises(StaleIdentityError, match="pre-run identity"):
        ProtocolLedger(Identity.model_validate(identity(run=True, revision=1)))

    unsolicited = ProtocolLedger(Identity.model_validate(identity(run=False, revision=1)))
    with pytest.raises(PlanStateError, match="planner.input operation"):
        unsolicited.accept_event(typed(plan_event()))

    wrong_operation = planning_ledger()
    plan = plan_event()
    plan["operationId"] = "operation_other001"
    with pytest.raises(PlanStateError, match="planner.input operation"):
        wrong_operation.accept_event(typed(plan))

    planning_ledger().accept_event(typed(planner_question_event()))

    bound = running_ledger()
    with pytest.raises(StaleIdentityError, match="exactly once"):
        bound.bind_run(Identity.model_validate(identity(run=True, revision=3)))


def test_required_planner_question_answer_lifecycle_is_exact_and_consumed_once() -> None:
    unsolicited = planning_ledger()
    with pytest.raises(PlanStateError, match="outstanding required question"):
        unsolicited.accept_request(typed(planner_answer_request("question_missing01")))

    ledger = planning_ledger()
    ledger.accept_event(typed(planner_question_event()))

    unrelated = planner_input_request()
    unrelated["messageId"] = "msg_revision0001"
    unrelated["requestId"] = "req_revision0001"
    unrelated["idempotencyKey"] = "idem_revision0001"
    unrelated["operationId"] = "operation_revision01"
    unrelated["payload"]["input"] = {
        "kind": "revision",
        "text": "Use another target instead.",
    }
    with pytest.raises(PlanStateError, match="must be answered first"):
        ledger.accept_request(typed(unrelated))

    with pytest.raises(PlanStateError, match="cannot bypass"):
        blocked_plan = plan_event()
        blocked_plan["sequence"] = 2
        ledger.accept_event(typed(blocked_plan))

    wrong = planner_answer_request("question_wrong0001")
    with pytest.raises(PlanStateError, match="outstanding required question"):
        ledger.accept_request(typed(wrong))

    answer = planner_answer_request("question_abcdefgh")
    ledger.accept_request(typed(answer))

    duplicate = planner_answer_request(
        "question_abcdefgh",
        operation="operation_answer002",
        message="answer0002",
    )
    with pytest.raises(PlanStateError, match="outstanding required question"):
        ledger.accept_request(typed(duplicate))

    stale_plan = plan_event()
    stale_plan["sequence"] = 2
    with pytest.raises(PlanStateError, match="latest planner input operation"):
        ledger.accept_event(typed(stale_plan))

    admitted_plan = plan_event()
    admitted_plan["sequence"] = 2
    admitted_plan["operationId"] = "operation_answer001"
    ledger.accept_event(typed(admitted_plan))

    after_plan = copy.deepcopy(unrelated)
    after_plan["messageId"] = "msg_revision0002"
    after_plan["requestId"] = "req_revision0002"
    after_plan["idempotencyKey"] = "idem_revision0002"
    after_plan["operationId"] = "operation_revision02"
    with pytest.raises(PlanStateError, match="closed after plan.ready"):
        ledger.accept_request(typed(after_plan))

    late_question = planner_question_event(
        question_id="question_late0001",
        operation="operation_answer001",
        sequence=3,
    )
    with pytest.raises(PlanStateError, match="closed after plan.ready"):
        ledger.accept_event(typed(late_question))


def test_phase0_rejects_optional_or_reused_planner_questions() -> None:
    optional = planning_ledger()
    with pytest.raises(PlanStateError, match="optional planner questions"):
        optional.accept_event(typed(planner_question_event(required=False)))

    ledger = planning_ledger()
    ledger.accept_event(typed(planner_question_event()))
    ledger.accept_request(typed(planner_answer_request("question_abcdefgh")))
    reused = planner_question_event(
        operation="operation_answer001",
        sequence=2,
    )
    with pytest.raises(PlanStateError, match="cannot be reused"):
        ledger.accept_event(typed(reused))


def test_phase0_ledger_rejects_unbound_state_snapshots() -> None:
    snapshot = json.loads((FIXTURES / "valid/snapshot-event.json").read_text(encoding="utf-8"))
    with pytest.raises(PlanStateError, match="state.snapshot is unavailable"):
        planning_ledger().accept_event(typed(snapshot))


def test_progress_task_id_must_resolve_to_plan() -> None:
    ledger = running_ledger(progress_steps=None)
    progress = event(
        {
            "type": "run.progress",
            "phase": "executing",
            "taskId": "T99",
            "step": 1,
            "totalSteps": 2,
            "taskModelSteps": 1,
            "summary": "Unknown task",
            "workerId": "worker_abcdefgh",
        },
        message="progress0001",
        operation="operation_run00001",
        sequence=3,
        run=True,
        revision=2,
    )
    with pytest.raises(ReferenceResolutionError, match="taskId"):
        ledger.accept_event(typed(progress))


def test_idempotency_digest_includes_operation_id() -> None:
    ledger = admitted_ledger()
    value = approval_decision_request()
    value["operationId"] = "operation_decide99"
    with pytest.raises(IdempotencyConflictError):
        ledger.accept_request(typed(value))


def test_same_operation_and_payload_is_replay_even_with_new_idempotency_key() -> None:
    ledger = ProtocolLedger(Identity.model_validate(identity(run=False, revision=1)))
    first = request(
        {
            "type": "planner.input",
            "identity": identity(run=False, revision=1),
            "expectedRevision": 1,
            "input": {"kind": "goal", "text": "Implement the /api route."},
        },
        message="plannerop01",
        operation="operation_plan0001",
    )
    assert ledger.accept_request(typed(first)) == RequestDisposition.ACCEPTED
    replay = copy.deepcopy(first)
    replay["messageId"] = "msg_plannerop02"
    replay["requestId"] = "req_plannerop02"
    replay["idempotencyKey"] = "idem_plannerop02"
    assert ledger.accept_request(typed(replay)) == RequestDisposition.REPLAY

    conflict = copy.deepcopy(replay)
    conflict["messageId"] = "msg_plannerop03"
    conflict["requestId"] = "req_plannerop03"
    conflict["idempotencyKey"] = "idem_plannerop03"
    conflict["payload"]["input"]["text"] = "Different semantics"
    with pytest.raises(IdempotencyConflictError):
        ledger.accept_request(typed(conflict))


def test_to_wire_dict_round_trips_aliases_and_null_identity() -> None:
    wire = to_wire_dict(typed(plan_event()))
    assert wire["payload"]["planDefinition"]["tasks"][0]["taskId"] == "T01"
    assert wire["identity"]["runId"] is None
    assert "hostPlanFingerprint" in wire["payload"]


def test_schema_rejects_declarative_invalid_cases() -> None:
    validator = schema_validator()
    cases = []
    wrong_version = plan_event()
    wrong_version["protocolVersion"] = "3.0"
    cases.append(wrong_version)
    huge = plan_event()
    huge["sequence"] = 9_007_199_254_740_992
    cases.append(huge)
    path = evidence_event()
    path["payload"]["artifacts"][0]["workspaceRelativePath"] = "/Users/alice/out.txt"
    cases.append(path)
    empty_pass = terminal_event()
    empty_pass["payload"]["result"]["acceptance"]["checks"] = []
    cases.append(empty_pass)
    for value in cases:
        assert list(validator.iter_errors(value)), value


def test_schema_expresses_new_policy_usage_constraint_and_phase_invariants() -> None:
    validator = schema_validator()

    policy = plan_event()
    policy["payload"]["approvalPolicy"]["required"] = False
    assert list(validator.iter_errors(policy))

    usage = terminal_event()
    usage["payload"]["result"]["usage"]["costKnown"] = False
    usage["payload"]["result"]["usage"]["totalCostMicros"] = 0
    assert list(validator.iter_errors(usage))

    constraint = plan_event()
    constraint["payload"]["planDefinition"]["planningConstraints"]["targetRoots"][0]["kind"] = (
        "forbidden_root"
    )
    assert list(validator.iter_errors(constraint))

    phase = plan_event()
    phase["identity"]["runId"] = "run_abcdefgh"
    assert list(validator.iter_errors(phase))


def test_valid_fixture_corpus_matches_schema_and_python() -> None:
    validator = schema_validator()
    fixtures = sorted((FIXTURES / "valid").glob("*.json"))
    assert fixtures
    for path in fixtures:
        value = json.loads(path.read_text(encoding="utf-8"))
        assert not list(validator.iter_errors(value)), path.name
        validate_envelope(value)


def _repair_fixture(value: dict[str, Any], repair: dict[str, Any]) -> dict[str, Any]:
    repaired = copy.deepcopy(value)
    parts = [
        part.replace("~1", "/").replace("~0", "~") for part in repair["pointer"].split("/")[1:]
    ]
    parent: Any = repaired
    for part in parts[:-1]:
        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
    key = parts[-1]
    if repair.get("delete") is True:
        if isinstance(parent, list):
            del parent[int(key)]
        else:
            del parent[key]
    elif isinstance(parent, list):
        parent[int(key)] = repair["value"]
    else:
        parent[key] = repair["value"]
    return repaired


def test_invalid_fixture_corpus_is_classified_by_manifest() -> None:
    manifest = json.loads((FIXTURES / "conformance-manifest.json").read_text(encoding="utf-8"))
    validator = schema_validator()
    for case in manifest["cases"]:
        value = json.loads((FIXTURES / case["fixture"]).read_text(encoding="utf-8"))
        if case["layer"] == "schema":
            assert list(validator.iter_errors(value)), case["id"]
        elif case["layer"] == "transport":
            with pytest.raises(UnsafeSerializationError):
                validate_envelope(value)
        elif case["layer"] == "model":
            with pytest.raises(ProtocolValidationError):
                validate_envelope(value)

        repaired = _repair_fixture(value, case["repair"])
        assert not list(validator.iter_errors(repaired)), (
            f"{case['id']}: repair is not schema-valid"
        )
        validate_envelope(repaired)


def test_language_neutral_canonical_fingerprint_vectors() -> None:
    corpus = json.loads((FIXTURES / "fingerprint-vectors.json").read_text(encoding="utf-8"))
    assert corpus["profile"] == "forge-nfc-json-v2"
    assert len(corpus["vectors"]) >= 8
    by_id = {vector["id"]: vector for vector in corpus["vectors"]}
    for vector in corpus["vectors"]:
        assert canonical_json_text(vector["input"]) == vector["canonical"], vector["id"]
        assert canonical_fingerprint(vector["input"]) == vector["sha256"], vector["id"]
    plan = plan_event()["payload"]
    required = approval_required_event()["payload"]
    assert by_id["host-plan-v2"]["sha256"] == plan["hostPlanFingerprint"]
    assert by_id["execution-admission-v2"]["sha256"] == plan["executionAdmission"]["fingerprint"]
    assert by_id["plan-approval-policy-v2"]["sha256"] == plan["approvalPolicy"]["fingerprint"]
    assert by_id["approval-action-v2"]["sha256"] == required["actionFingerprint"]


def test_language_neutral_ledger_scenarios() -> None:
    error_types = {
        "ApprovalStateError": ApprovalStateError,
        "PlanStateError": PlanStateError,
        "ReferenceResolutionError": ReferenceResolutionError,
        "StaleIdentityError": StaleIdentityError,
        "TerminalEventError": TerminalEventError,
    }
    manifest = json.loads((FIXTURES / "conformance-manifest.json").read_text(encoding="utf-8"))
    for case in manifest["ledgerCases"]:
        scenario = json.loads((FIXTURES / case["fixture"]).read_text(encoding="utf-8"))
        now = [datetime.fromisoformat(scenario["now"].replace("Z", "+00:00"))]
        ledger = ProtocolLedger(
            Identity.model_validate(scenario["initialIdentity"]),
            after_sequence=scenario["afterSequence"],
            clock=lambda now=now: now[0],
        )
        expected = error_types[case["expectedError"]]
        with pytest.raises(expected):
            for step in scenario["steps"]:
                if step["op"] == "acceptEvent":
                    ledger.accept_event(typed(step["message"]))
                elif step["op"] == "acceptRequest":
                    ledger.accept_request(typed(step["message"]))
                elif step["op"] == "bindRun":
                    ledger.bind_run(Identity.model_validate(step["identity"]))
                elif step["op"] == "setClock":
                    now[0] = datetime.fromisoformat(step["now"].replace("Z", "+00:00"))
                else:  # pragma: no cover - fixture authoring guard
                    raise AssertionError(f"unknown conformance operation: {step['op']}")

    for case in manifest["ledgerSuccessCases"]:
        scenario = json.loads((FIXTURES / case["fixture"]).read_text(encoding="utf-8"))
        now = [datetime.fromisoformat(scenario["now"].replace("Z", "+00:00"))]
        ledger = ProtocolLedger(
            Identity.model_validate(scenario["initialIdentity"]),
            after_sequence=scenario["afterSequence"],
            clock=lambda now=now: now[0],
        )
        for step in scenario["steps"]:
            if step["op"] == "acceptEvent":
                ledger.accept_event(typed(step["message"]))
            elif step["op"] == "acceptRequest":
                ledger.accept_request(typed(step["message"]))
            elif step["op"] == "bindRun":
                ledger.bind_run(Identity.model_validate(step["identity"]))
            elif step["op"] == "setClock":
                now[0] = datetime.fromisoformat(step["now"].replace("Z", "+00:00"))
            else:  # pragma: no cover - fixture authoring guard
                raise AssertionError(f"unknown conformance operation: {step['op']}")


def test_conformance_manifest_declares_stateful_ledger_only_invariants() -> None:
    manifest = json.loads((FIXTURES / "conformance-manifest.json").read_text(encoding="utf-8"))
    ledger_only = set(manifest["ledgerOnlyInvariants"])
    assert {
        "approval_authority_and_consumption",
        "approval_reissuance_after_deny_or_expiry",
        "execution_admission_run_binding",
        "authenticated_checkpoint_required_for_replay",
        "event_sequence_and_operation_authority",
        "identifier_immutability_across_events",
        "no_live_pending_authority_at_terminal",
        "terminal_reference_resolution",
        "single_final_terminal",
        "unregistered_runtime_approvals_rejected",
        "planner_question_answer_lifecycle",
        "succeeded_task_progress_is_complete",
    }.issubset(ledger_only)
    typed_semantic = set(manifest["typedSemanticInvariants"])
    assert {
        "approval_policy_and_usage_consistency",
        "expected_revision_equality",
        "canonical_fingerprint_recomputation",
        "canonical_task_kind_and_structured_planning_constraints",
        "closed_native_host_equivalence",
        "execution_budget_timeout_and_worker_settlement",
        "unique_identifiers_by_semantic_id",
        "timing_order_and_worker_arithmetic",
        "scope_containment_and_task_graph_order",
        "verification_command_execution_evidence",
        "nfc_normalization",
        "portable_git_branch_and_ref_alias_policy",
        "same_path_scope_kind_substitution_is_rejected",
    }.issubset(typed_semantic)


def test_ledger_ingress_revalidates_post_validation_plan_and_command_mutation() -> None:
    envelope = typed(plan_event())
    payload = envelope.payload
    payload.plan_definition.tasks[0].write_scopes.append(
        WorkspaceScope(path="vendor/pwn.py", kind="file")
    )
    payload.native_approval_definition.tasks[0].write_scope.append(
        WorkspaceScope(path="vendor/pwn.py", kind="file")
    )
    payload.verification_contract.commands[0].argv.append("--unexpected")
    ledger = ProtocolLedger(
        Identity.model_validate(identity(run=False, revision=1)), clock=lambda: NOW
    )
    with pytest.raises(ProtocolValidationError):
        ledger.accept_event(envelope)


def test_ledger_keeps_a_private_plan_copy_after_ingress() -> None:
    external = typed(plan_event())
    ledger = planning_ledger()
    ledger.accept_event(external)
    external.payload.plan_definition.tasks[0].write_scopes.append(
        WorkspaceScope(path="vendor/pwn.py", kind="file")
    )
    external.payload.native_approval_definition.tasks[0].write_scope.append(
        WorkspaceScope(path="vendor/pwn.py", kind="file")
    )
    external.payload.verification_contract.commands[0].argv.append("--unexpected")
    ledger.accept_event(typed(approval_required_event()))
    ledger.accept_request(typed(approval_decision_request()))
    ledger.accept_request(typed(run_start_request()))


def test_ledger_revalidates_cleared_terminal_evidence_refs() -> None:
    ledger = running_ledger()
    ledger.accept_event(typed(evidence_event()))
    terminal = typed(terminal_event())
    terminal.payload.result.acceptance.checks[0].evidence_refs.clear()
    terminal.payload.result.verification.checks[0].evidence_refs.clear()
    with pytest.raises(ProtocolValidationError):
        ledger.accept_event(terminal)


def test_host_native_semantic_mismatch_is_rejected_after_recomputed_fingerprints() -> None:
    value = plan_event()
    native = value["payload"]["nativeApprovalDefinition"]
    native["tasks"][0]["estimated_files"] = [{"path": "vendor/**", "kind": "directory"}]
    native["tasks"][0]["write_scope"] = [{"path": "vendor/pwn.py", "kind": "file"}]
    native_fp = canonical_fingerprint(native)
    value["payload"]["nativeApprovalFingerprint"] = native_fp
    contract = value["payload"]["verificationContract"]
    contract["approvedPlanFingerprint"] = native_fp
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    ledger = ProtocolLedger(
        Identity.model_validate(identity(run=False, revision=1)), clock=lambda: NOW
    )
    with pytest.raises(ProtocolValidationError):
        ledger.accept_event(typed(value))


@pytest.mark.parametrize(
    ("_case", "mutate"),
    [
        ("goal", lambda native: native.__setitem__("project_goal", "Different goal")),
        ("summary", lambda native: native.__setitem__("summary", "Different summary")),
        ("requirements", lambda native: native["requirements"].append("Different requirement")),
        (
            "constraint",
            lambda native: native["planning_constraints"]["target_roots"][0].__setitem__(
                "reason_code", "different_reason"
            ),
        ),
        (
            "constraint_path",
            lambda native: native["planning_constraints"]["target_roots"][0].__setitem__(
                "path", "src/sub"
            ),
        ),
        (
            "constraint_evidence",
            lambda native: native["planning_constraints"]["target_roots"][0].__setitem__(
                "evidence", "Different evidence"
            ),
        ),
        ("asset", lambda native: native["assets"].append("asset_other0001")),
        (
            "intent",
            lambda native: native["intent_record"].__setitem__("summary", "Different intent"),
        ),
        (
            "intent_scope",
            lambda native: native["intent_record"]["affected_scopes"][0].__setitem__(
                "path", "src/sub"
            ),
        ),
        ("task_id", lambda native: native["tasks"][0].__setitem__("id", "T99")),
        ("dependencies", lambda native: native["tasks"][0]["dependencies"].append("T00")),
        (
            "task_kind",
            lambda native: native["tasks"][0].__setitem__("task_kind", "test_only"),
        ),
        (
            "branch",
            lambda native: native["tasks"][0].__setitem__("branch", "feat/different"),
        ),
        ("title", lambda native: native["tasks"][0].__setitem__("title", "Different")),
        (
            "description",
            lambda native: native["tasks"][0].__setitem__("description", "Different task"),
        ),
        (
            "criterion",
            lambda native: native["tasks"][0]["acceptance_criteria"].__setitem__(
                0, "Different criterion"
            ),
        ),
        (
            "estimated_scope",
            lambda native: native["tasks"][0]["estimated_files"][0].__setitem__(
                "path", "src/sub/**"
            ),
        ),
        (
            "estimated_scope_kind",
            lambda native: native["tasks"][0]["estimated_files"][0].__setitem__("kind", "file"),
        ),
        (
            "write_scope",
            lambda native: native["tasks"][0]["write_scope"][0].__setitem__("path", "src/other.py"),
        ),
    ],
)
def test_every_closed_native_authority_field_is_cross_bound(
    _case: str, mutate: Callable[[dict[str, object]], None]
) -> None:
    value = plan_event()
    native = value["payload"]["nativeApprovalDefinition"]
    mutate(native)
    native_fp = canonical_fingerprint(native)
    value["payload"]["nativeApprovalFingerprint"] = native_fp
    contract = value["payload"]["verificationContract"]
    contract["approvedPlanFingerprint"] = native_fp
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    with pytest.raises(ProtocolValidationError, match="invalid_message"):
        typed(value)


def test_host_criterion_identity_cannot_be_spoofed_with_fresh_fingerprints() -> None:
    value = plan_event()
    host = value["payload"]["planDefinition"]
    host["tasks"][0]["acceptance"][0]["criterionId"] = "T01:criterion:spoof"
    host_fp = plan_fingerprint(host)
    value["payload"]["hostPlanFingerprint"] = host_fp
    contract = value["payload"]["verificationContract"]
    contract["hostPlanFingerprint"] = host_fp
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    with pytest.raises(ProtocolValidationError):
        typed(value)


@pytest.mark.parametrize(
    "extra_path",
    [
        ("nativeApprovalDefinition", "run_id"),
        ("nativeApprovalDefinition", "created_at"),
        ("nativeApprovalDefinition.tasks.0", "parallel_group"),
        ("nativeApprovalDefinition.tasks.0", "mcp_scope"),
        ("nativeApprovalDefinition.tasks.0", "verification_contract"),
    ],
)
def test_closed_native_migration_projection_rejects_legacy_extensions(
    extra_path: tuple[str, str],
) -> None:
    value = plan_event()
    container_path, field = extra_path
    container = value["payload"]
    for component in container_path.split("."):
        container = container[int(component)] if component.isdecimal() else container[component]
    container[field] = "legacy-value"
    with pytest.raises(ProtocolValidationError):
        typed(value)


def test_invalid_nonassertive_verifier_cannot_enter_the_ledger() -> None:
    envelope = typed(plan_event())
    command = envelope.payload.verification_contract.commands[0]
    envelope.payload.verification_contract.commands[0] = command.model_copy(
        update={
            "validation_status": "INVALID",
            "execution_mode": "INVALID",
            "rejection_reason": "not executable",
            "evidentiary_capability": "NON_ASSERTIVE",
            "trust_level": "UNTRUSTED",
            "requirement": "ADVISORY",
            "argv": [],
        }
    )
    object.__setattr__(
        envelope.payload.verification_contract,
        "contract_fingerprint",
        verification_contract_fingerprint(envelope.payload.verification_contract),
    )
    ledger = ProtocolLedger(
        Identity.model_validate(identity(run=False, revision=1)), clock=lambda: NOW
    )
    with pytest.raises(ProtocolValidationError):
        ledger.accept_event(envelope)


@pytest.mark.parametrize(
    ("text", "argv"),
    [
        ("true", ["true"]),
        ("rm -rf vendor", ["rm", "-rf", "vendor"]),
        ("pytest --collect-only", ["pytest", "--collect-only"]),
        ("pytest -p malicious_plugin -q", ["pytest", "-p", "malicious_plugin", "-q"]),
        ("python -m pytest -q", ["python", "-m", "pytest", "-q"]),
    ],
)
def test_phase0_verification_command_reclassifies_argv_instead_of_trusting_labels(
    text: str,
    argv: list[str],
) -> None:
    value = plan_event()
    command = value["payload"]["verificationContract"]["commands"][0]
    command.update(originalText=text, displayText=text, argv=argv)
    contract = value["payload"]["verificationContract"]
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    assert list(schema_validator().iter_errors(value))
    with pytest.raises(ProtocolValidationError):
        typed(value)
    with pytest.raises(ProtocolValidationError):
        planning_ledger().accept_event(typed(value))


def test_phase0_repo_native_whole_suite_pytest_profiles_are_valid() -> None:
    for argv in (["pytest"], ["pytest", "-q"]):
        value = plan_event()
        text = shlex.join(argv)
        command = value["payload"]["verificationContract"]["commands"][0]
        command.update(originalText=text, displayText=text, argv=argv)
        contract = value["payload"]["verificationContract"]
        contract["contractFingerprint"] = verification_contract_fingerprint(contract)
        typed(value)


def test_language_neutral_phase0_verification_command_vectors() -> None:
    corpus = json.loads(
        (FIXTURES / "verification-command-vectors.json").read_text(encoding="utf-8")
    )
    assert corpus["schemaVersion"] == 1
    assert {vector["id"] for vector in corpus["vectors"]} >= {
        "repo-native-pytest",
        "repo-native-pytest-quiet",
        "vacuous-true",
        "destructive-rm",
        "pytest-collect-only",
        "pytest-plugin-injection",
        "python-wrapper",
    }
    validator = schema_validator()
    for vector in corpus["vectors"]:
        value = plan_event()
        contract = value["payload"]["verificationContract"]
        contract["commands"] = [copy.deepcopy(vector["command"])]
        contract["checks"][1]["commandIds"] = [vector["command"]["commandId"]]
        contract["contractFingerprint"] = verification_contract_fingerprint(contract)
        schema_errors = list(validator.iter_errors(value))
        if vector["valid"]:
            assert not schema_errors, vector["id"]
            typed(value)
        else:
            assert schema_errors, vector["id"]
            with pytest.raises(ProtocolValidationError):
                typed(value)


def test_each_verification_execution_fact_binds_exactly_one_approved_command() -> None:
    plan = plan_ready_payload()
    contract = plan["verificationContract"]
    command = copy.deepcopy(contract["commands"][0])
    command.update(
        commandId="command.pytest.plain",
        originalText="pytest",
        displayText="pytest",
        argv=["pytest"],
    )
    contract["commands"].append(command)
    contract["checks"][1]["commandIds"].append("command.pytest.plain")
    rebind_plan_payload(plan)
    typed(plan_event(plan_payload=plan))

    invalid_evidence = evidence_event()
    invalid_evidence["payload"]["evidence"][1]["commandIds"].append("command.pytest.plain")
    assert list(schema_validator().iter_errors(invalid_evidence))
    with pytest.raises(ProtocolValidationError):
        typed(invalid_evidence)

    def ledger_with_evidence() -> ProtocolLedger:
        ledger = planning_ledger()
        ledger.accept_event(typed(plan_event(plan_payload=plan)))
        required = approval_required_event(plan_payload=plan)
        ledger.accept_event(typed(required))
        ledger.accept_request(typed(approval_decision_request(required_event=required)))
        ledger.accept_request(typed(run_start_request(plan_payload=plan)))
        ledger.bind_run(Identity.model_validate(identity(run=True, revision=2)))
        ledger.accept_event(typed(progress_event()))
        evidence = evidence_event()
        second = copy.deepcopy(evidence["payload"]["evidence"][1])
        second.update(
            evidenceId="evidence_verify02",
            commandIds=["command.pytest.plain"],
        )
        evidence["payload"]["evidence"].append(second)
        ledger.accept_event(typed(evidence))
        return ledger

    incomplete = ledger_with_evidence()
    with pytest.raises(ReferenceResolutionError, match="one distinct fact"):
        incomplete.accept_event(typed(terminal_event(plan_payload=plan)))

    complete = ledger_with_evidence()
    terminal = terminal_event(plan_payload=plan)
    terminal["payload"]["result"]["verification"]["checks"][0]["evidenceRefs"].append(
        "evidence_verify02"
    )
    complete.accept_event(typed(terminal))
    complete.require_terminal()


def test_acceptance_checks_cannot_claim_executable_command_ids() -> None:
    value = plan_event()
    contract = value["payload"]["verificationContract"]
    contract["checks"][0]["commandIds"] = ["command.pytest"]
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    with pytest.raises(ProtocolValidationError):
        typed(value)


def test_verification_evidence_must_bind_exact_command_and_execution_facts() -> None:
    ledger = running_ledger()
    wrong_command = evidence_event()
    wrong_command["payload"]["evidence"][1]["commandIds"] = ["command.other"]
    with pytest.raises(ReferenceResolutionError, match="approved contract check"):
        ledger.accept_event(typed(wrong_command))

    failed_execution = evidence_event()
    failed_execution["payload"]["evidence"][1]["exitCode"] = 1
    with pytest.raises(ProtocolValidationError):
        typed(failed_execution)


def test_passed_evidence_requires_nonempty_available_artifacts() -> None:
    empty = evidence_event()
    empty["payload"]["evidence"][0]["artifactRefs"] = []
    with pytest.raises(ProtocolValidationError):
        typed(empty)

    failed = evidence_event()
    failed["payload"]["artifacts"][0].update(status="failed", digest=None, sizeBytes=None)
    with pytest.raises(ProtocolValidationError):
        typed(failed)


def test_execution_admission_enforces_model_steps_timeout_and_workers() -> None:
    progress_ledger = running_ledger(progress_steps=None)
    progress = event(
        {
            "type": "run.progress",
            "phase": "executing",
            "taskId": "T01",
            "step": 1,
            "totalSteps": 2,
            "taskModelSteps": 65,
            "summary": "Budget exceeded.",
            "workerId": "worker_abcdefgh",
        },
        message="progress0002",
        operation="operation_run00001",
        sequence=3,
        run=True,
        revision=2,
    )
    with pytest.raises(PlanStateError, match="model steps"):
        progress_ledger.accept_event(typed(progress))

    def exceed_task_step_budget(result: dict[str, Any]) -> None:
        result["taskUsage"][0]["modelSteps"] = 65
        result["usage"]["modelCalls"] = 65

    for mutate, match in (
        (exceed_task_step_budget, "model steps"),
        (lambda result: result["timing"].__setitem__("elapsedMs", 600_001), "timeout"),
    ):
        ledger = running_ledger()
        ledger.accept_event(typed(evidence_event()))
        terminal = terminal_event()
        mutate(terminal["payload"]["result"])
        with pytest.raises(PlanStateError, match=match):
            ledger.accept_event(typed(terminal))

    unsettled = terminal_event()
    unsettled["payload"]["result"]["workers"] = {
        "requested": 2,
        "started": 2,
        "succeeded": 1,
        "failed": 0,
    }
    with pytest.raises(ProtocolValidationError):
        typed(unsettled)

    overallocated = running_ledger()
    overallocated.accept_event(typed(evidence_event()))
    terminal = terminal_event()
    terminal["payload"]["result"]["workers"] = {
        "requested": 2,
        "started": 2,
        "succeeded": 2,
        "failed": 0,
    }
    with pytest.raises(PlanStateError, match="worker allocation"):
        overallocated.accept_event(typed(terminal))


def test_task_model_steps_are_monotonic_and_terminal_usage_is_exact() -> None:
    progress = event(
        {
            "type": "run.progress",
            "phase": "executing",
            "taskId": "T01",
            "step": 1,
            "totalSteps": 2,
            "taskModelSteps": 4,
            "summary": "Task execution advanced.",
            "workerId": "worker_abcdefgh",
        },
        message="progress1001",
        operation="operation_run00001",
        sequence=3,
        run=True,
        revision=2,
    )
    regressing = copy.deepcopy(progress)
    regressing.update(messageId="msg_progress1002", sequence=4)
    regressing["identity"]["revision"] = 3
    regressing["payload"]["taskModelSteps"] = 1

    ledger = running_ledger(progress_steps=None)
    ledger.accept_event(typed(progress))
    with pytest.raises(PlanStateError, match="cannot regress"):
        ledger.accept_event(typed(regressing))

    def progressed_with_evidence() -> ProtocolLedger:
        accepted = running_ledger(progress_steps=None)
        accepted.accept_event(typed(progress))
        evidence = evidence_event()
        evidence.update(messageId="msg_evidence004", sequence=4)
        evidence["identity"]["revision"] = 3
        accepted.accept_event(typed(evidence))
        return accepted

    mismatched = progressed_with_evidence()
    terminal = terminal_event(sequence=5)
    with pytest.raises(PlanStateError, match="last authoritative"):
        mismatched.accept_event(typed(terminal))

    exact = progressed_with_evidence()
    terminal = terminal_event(sequence=5)
    terminal["payload"]["result"]["taskUsage"][0]["modelSteps"] = 4
    terminal["payload"]["result"]["usage"]["modelCalls"] = 4
    exact.accept_event(typed(terminal))
    exact.require_terminal()


def test_succeeded_terminal_requires_progress_for_every_managed_task() -> None:
    ledger = running_ledger(progress_steps=None)
    ledger.accept_event(typed(evidence_event(sequence=3)))
    with pytest.raises(PlanStateError, match="authoritative model-step progress"):
        ledger.accept_event(typed(terminal_event(sequence=4)))

    failed = running_ledger(progress_steps=None)
    failed.accept_event(typed(evidence_event(sequence=3)))
    result = terminal_event(sequence=4)
    result["payload"]["result"].update(status="failed", exitCode=1)
    with pytest.raises(PlanStateError, match="must report zero"):
        failed.accept_event(typed(result))


def test_terminal_usage_and_timeout_cannot_underreport_execution() -> None:
    underreported = terminal_event()
    underreported["payload"]["result"]["usage"]["modelCalls"] = 2
    with pytest.raises(ProtocolValidationError):
        typed(underreported)

    ledger = running_ledger()
    ledger.accept_event(typed(evidence_event()))
    overtime = terminal_event()
    result = overtime["payload"]["result"]
    result.update(status="failed", exitCode=1)
    result["timing"].update(elapsedMs=700_000, timedOut=False)
    with pytest.raises(PlanStateError, match="marked timedOut"):
        ledger.accept_event(typed(overtime))


def test_elapsed_ms_is_timeout_authority_and_wall_clock_is_presentation_only() -> None:
    ledger = running_ledger()
    ledger.accept_event(typed(evidence_event()))
    terminal = terminal_event()
    terminal["payload"]["result"]["timing"].update(
        finishedAt="2026-08-21T10:02:00Z", elapsedMs=60_000
    )
    ledger.accept_event(typed(terminal))
    ledger.require_terminal()


def test_terminal_rejects_any_restored_live_pending_authority() -> None:
    ledger = running_ledger()
    ledger.accept_event(typed(evidence_event()))
    ledger._pending_approvals["approval_checkpoint01"] = PendingApproval(
        operation_id="operation_run00001",
        authority=ledger.identity.stable_key,
        issued_revision=ledger.identity.revision,
        approval_id="approval_checkpoint01",
        action_fingerprint="a" * 64,
        scope_fingerprint="b" * 64,
        expires_at=NOW + timedelta(minutes=5),
        action_kind="future_registered_subject",
    )
    with pytest.raises(ApprovalStateError, match="pending approval"):
        ledger.accept_event(typed(terminal_event()))


@pytest.mark.parametrize(
    "diagnostic",
    [
        "failed:/Users/alice/repo/file.py",
        "Failure at /Users/alice/repo/file.py",
        "Failure at /opt/private/tool",
        'File "/Users/alice/repo/file.py", line 3',
        "Failure (/opt/private/tool)",
        "At `C:/Users/alice/repo/file.py`",
        "See </Users/alice/repo/file.py>",
        "Failure at file:///Users/alice/repo/file.py",
        "Failure at ~/private/file.py",
        r"Failure at \\server\private\file.py",
        "Provider returned sk-abcdefghijklmnop",
        "Provider returned ghp_abcdefghijklmnop",
        "Provider returned gho_abcdefghijklmnop",
        "Provider returned github_pat_abcdefghijklmnop",
        "Provider returned xoxb-abcdefghijklmnop",
        "Provider returned AKIAABCDEFGHIJKLMNOP",
        "Provider returned eyJabcdefghij.abcdefghij.abcdefghij",
        "Provider returned -----BEGIN PRIVATE KEY-----",
        "Provider returned -----BEGIN DSA PRIVATE KEY-----",
        "First line\nProvider returned ghp_abcdefghijklmnop",
        "First line\nFailure at /Users/alice/repo/file.py",
        "Provider URL https://example.invalid/?access_token=abcdefgh",
        "Provider returned Bearer abcdefghijklmnop",
        "api_key=abcdefghijklmnop",
        "password: abcdefghijklmnop",
        "token = abcdefghijklmnop",
        "OPENAI_API_KEY=abcdefghijklmnop",
        "AWS_SECRET_ACCESS_KEY=abcdefghijklmnop",
        "client_secret=abcdefghijklmnop",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "Authorization: Digest abcdefghijklmnop",
        "Cookie: session=abcdefghijklmnop",
        "URL https://alice:secretpass@example.invalid/path",
        "Provider returned glpat-abcdefghijklmnop",
        "Provider returned npm_abcdefghijklmnop",
        "Provider returned pypi-AgEIcHlwaS5vcmcabcdefghijklmnop",
        "Provider returned hf_abcdefghijklmnop",
        "//server/private/share.txt",
        "Failure at //server/private/share.txt",
        "Failure at smb://server/private/share.txt",
        "Failure at afp://server/private/share.txt",
        "Failure at nfs://server/private/share.txt",
    ],
)
def test_terminal_and_cancel_diagnostics_must_be_redacted(diagnostic: str) -> None:
    terminal = terminal_event()
    terminal["payload"]["result"]["reason"]["message"] = diagnostic
    assert list(schema_validator().iter_errors(terminal))
    with pytest.raises(ProtocolValidationError):
        typed(terminal)

    cancel = json.loads((FIXTURES / "valid/run-cancel-request.json").read_text(encoding="utf-8"))
    cancel["payload"]["message"] = diagnostic
    assert list(schema_validator().iter_errors(cancel))
    with pytest.raises(ProtocolValidationError):
        typed(cancel)


@pytest.mark.parametrize(
    "diagnostic",
    [
        "failed:/Users/alice/private.py",
        "Provider returned ghp_abcdefghijklmnop",
        "Provider returned ghs_abcdefghijklmnop",
        "Provider returned github_pat_abcdefghijklmnop",
        "Provider returned xoxb-abcdefghijklmnop",
        "Provider returned AKIAABCDEFGHIJKLMNOP",
        "Provider returned eyJabcdefghij.abcdefghij.abcdefghij",
        "Provider returned -----BEGIN PRIVATE KEY-----",
        "First line\nProvider returned ghp_abcdefghijklmnop",
        "Provider URL https://example.invalid/?token=abcdefgh",
        "api_key=abcdefghijklmnop",
        "password: abcdefghijklmnop",
        "token = abcdefghijklmnop",
        "OPENAI_API_KEY=abcdefghijklmnop",
        "AWS_SECRET_ACCESS_KEY=abcdefghijklmnop",
        "client_secret=abcdefghijklmnop",
        "Authorization: Basic dXNlcjpwYXNzd29yZA==",
        "Cookie: session=abcdefghijklmnop",
        "URL https://alice:secretpass@example.invalid/path",
        "Failure at //server/private/share.txt",
        "Failure at smb://server/private/share.txt",
    ],
)
def test_every_runtime_and_authority_summary_uses_shared_redaction(
    diagnostic: str,
) -> None:
    progress = json.loads((FIXTURES / "valid/run-progress-event.json").read_text(encoding="utf-8"))
    progress["payload"]["summary"] = diagnostic

    evidence = evidence_event()
    evidence["payload"]["evidence"][0]["summary"] = diagnostic

    terminal = terminal_event()
    terminal["payload"]["result"]["acceptance"]["checks"][0]["summary"] = diagnostic

    rejected_command = plan_event()
    contract = rejected_command["payload"]["verificationContract"]
    invalid = copy.deepcopy(contract["commands"][0])
    invalid.update(
        commandId="command.rejected",
        originalText="rejected verifier",
        displayText="rejected verifier",
        executionMode="INVALID",
        provenance="INFERRED_HEURISTIC",
        trustLevel="UNTRUSTED",
        requirement="ADVISORY",
        validationStatus="INVALID",
        rejectionReason=diagnostic,
        evidentiaryCapability="UNKNOWN",
        capabilityReason="Rejected verifier.",
        argv=[],
    )
    contract["commands"].append(invalid)
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)

    capability = copy.deepcopy(rejected_command)
    capability_contract = capability["payload"]["verificationContract"]
    capability_contract["commands"][-1]["rejectionReason"] = "Rejected verifier."
    capability_contract["commands"][-1]["capabilityReason"] = diagnostic
    capability_contract["contractFingerprint"] = verification_contract_fingerprint(
        capability_contract
    )

    authority = plan_event()
    policy = authority["payload"]["approvalPolicy"]
    policy["summary"] = diagnostic
    policy["scope"]["label"] = diagnostic
    policy["scope"]["fingerprint"] = approval_scope_fingerprint(policy["scope"])
    policy["fingerprint"] = approval_policy_fingerprint(policy)

    for value in (progress, evidence, terminal, rejected_command, capability, authority):
        assert list(schema_validator().iter_errors(value))
        with pytest.raises((ProtocolValidationError, UnsafeSerializationError)):
            typed(value)


def test_redacted_runtime_text_allows_ordinary_https_urls_without_userinfo() -> None:
    diagnostic = "See https://example.invalid/docs/api for details."
    terminal = terminal_event()
    terminal["payload"]["result"]["reason"]["message"] = diagnostic
    assert not list(schema_validator().iter_errors(terminal))
    typed(terminal)

    progress = json.loads((FIXTURES / "valid/run-progress-event.json").read_text(encoding="utf-8"))
    progress["payload"]["summary"] = diagnostic
    assert not list(schema_validator().iter_errors(progress))
    typed(progress)


def test_protocol_error_uses_only_stable_nonreflective_summary() -> None:
    response = {
        **envelope_base(kind="response", message="error00001", operation="operation_plan0001"),
        "replyTo": "req_planner0001",
        "payload": {
            "type": "protocol.error",
            "code": "internal_error",
            "message": "Failure at /Users/alice with sk-abcdefghijklmnop",
            "retryable": False,
            "currentIdentity": None,
        },
    }
    with pytest.raises(ProtocolValidationError):
        typed(response)
    response["payload"]["message"] = "The protocol operation failed."
    assert typed(response).payload.type == "protocol.error"


@pytest.mark.parametrize("provenance", ["PREEXISTING_REPO_NATIVE", "HOST_AUTHORITATIVE"])
def test_schema_and_python_accept_bound_assertive_command_authority(provenance: str) -> None:
    value = plan_event()
    contract = value["payload"]["verificationContract"]
    contract["commands"][0]["provenance"] = provenance
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    assert not list(schema_validator().iter_errors(value))
    assert typed(value).payload.verification_contract.commands[0].provenance == provenance


@pytest.mark.parametrize("provenance", ["INFERRED_HEURISTIC", "EXPLICIT_USER_COMMAND"])
def test_schema_and_python_reject_unadmitted_command_authority(provenance: str) -> None:
    value = plan_event()
    contract = value["payload"]["verificationContract"]
    contract["commands"][0]["provenance"] = provenance
    contract["contractFingerprint"] = verification_contract_fingerprint(contract)
    assert list(schema_validator().iter_errors(value))
    with pytest.raises(ProtocolValidationError):
        typed(value)


def test_schema_and_python_reject_noncanonical_repeated_argv() -> None:
    value = plan_event()
    command = value["payload"]["verificationContract"]["commands"][0]
    command["originalText"] = "pytest -q -q"
    command["displayText"] = "pytest -q -q"
    command["argv"] = ["pytest", "-q", "-q"]
    value["payload"]["verificationContract"]["contractFingerprint"] = (
        verification_contract_fingerprint(value["payload"]["verificationContract"])
    )
    assert list(schema_validator().iter_errors(value))
    with pytest.raises(ProtocolValidationError):
        typed(value)


def test_checked_schema_has_no_unclassified_python_drift() -> None:
    checked = load_schema()
    generated = ENVELOPE_ADAPTER.json_schema(by_alias=True, mode="validation")
    assert set(checked["$defs"]).difference(generated["$defs"]) == {"protocolVersion"}
    allowed_schema_only = {"allOf", "pattern", "uniqueItems"}
    for name, checked_definition in checked["$defs"].items():
        if name == "protocolVersion":
            continue
        typed_definition = generated["$defs"][name]
        assert set(checked_definition).difference(typed_definition) <= {"allOf"}
        for field, checked_property in checked_definition.get("properties", {}).items():
            typed_property = typed_definition["properties"][field]
            assert set(checked_property).difference(typed_property) <= allowed_schema_only
            if field == "argv":
                assert "uniqueItems" not in checked_property


def test_adversarial_canonical_rejection_vectors() -> None:
    corpus = json.loads((FIXTURES / "fingerprint-vectors.json").read_text(encoding="utf-8"))
    assert {item["id"] for item in corpus["rejectionVectors"]} == {
        "nfc-key-collision",
        "unsafe-integer",
        "unpaired-surrogate",
    }
    for vector in corpus["rejectionVectors"]:
        with pytest.raises(CanonicalJSONError):
            canonical_json_text(vector["input"])


def test_node_and_python_share_canonical_golden_vectors() -> None:
    node = shutil.which("node")
    if node is None:
        candidates = sorted(Path.home().glob(".local/node-*/bin/node"))
        assert candidates and os.access(candidates[-1], os.X_OK), (
            "Node is required for cross-language fingerprint parity"
        )
        node = str(candidates[-1])
    completed = subprocess.run(
        [
            node,
            str(FIXTURES / "verify-fingerprint-vectors.mjs"),
            str(FIXTURES / "fingerprint-vectors.json"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
