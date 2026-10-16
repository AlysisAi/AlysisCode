from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

SUCCESS_EXIT_CODE = 0
AGENT_FAILURE_EXIT_CODE = 1
# EX_TEMPFAIL on Unix. Keep the numeric value explicit so it is stable on Windows too.
INFRASTRUCTURE_FAILURE_EXIT_CODE = 75


class RunOutcome(StrEnum):
    SUCCESS = "success"
    FAIL = "fail"
    INFRA_FAIL = "infra_fail"


class TaskOutcome(StrEnum):
    """Evidence-based task result, independent of the process exit convention."""

    VERIFIED_SUCCESS = "verified_success"
    COMPLETED_UNVERIFIED = "completed_unverified"
    INCOMPLETE = "incomplete"
    BLOCKED = "blocked"
    DEADLINE_EXCEEDED = "deadline_exceeded"
    PROVIDER_FAILURE = "provider_failure"
    CANCELLED = "cancelled"


def task_outcome_record(
    *,
    exit_code: int | None,
    reason: str,
    task_id: str = "",
    state: dict[str, Any] | None = None,
    terminal: bool = True,
) -> dict[str, Any]:
    """Classify host state; model claims and exit zero are never verification."""
    state = state or {}
    certificate = state.get("completion_certificate") or {}
    problems = list(certificate.get("problems") or [])
    missing = list(state.get("missing_verification_commands") or [])
    failed = list(state.get("failed_verification_commands") or [])
    controller = state.get("completion_gate_controller") or {}
    snapshot = controller.get("last_snapshot_payload") or {}
    blocked = bool(snapshot.get("accepted_blocker"))
    contract = state.get("acceptance_contract") or {}
    if contract.get("baseline_available") is False:
        problems.append("acceptance_baseline_unavailable")
    hard = [
        item
        for item in contract.get("criteria", [])
        if isinstance(item, dict)
        and item.get("enforcement") == "HARD"
        and item.get("required")
        and item.get("required_for_finalization")
    ]
    unresolved = [
        item.get("id") or item.get("criterion_id")
        for item in hard
        if item.get("status") != "PASSED"
    ]
    if missing and "verification_incomplete" not in problems:
        problems.append("verification_incomplete")
    if failed and "verification_failed" not in problems:
        problems.append("verification_failed")
    if unresolved and "acceptance_criteria_unverified" not in problems:
        problems.append("acceptance_criteria_unverified")
    if state.get("verification_coverage_stale"):
        problems.append("verification_stale")
    connector_links = state.get("connector_link_evidence") or {}
    if connector_links.get("unchecked_links"):
        problems.append("connector_links_unverified")
    if (
        not hard
        and state.get("accepted_verification_evidence")
        and not state.get("material_edit_count")
    ):
        # A passing baseline proves the check ran, not that an otherwise
        # unbound request was delivered. Prior turns' edit generations do not
        # prove this request was implemented. Read-only tasks qualify through
        # explicit hard criteria; do not infer a mutation request from prose.
        problems.append("no_material_edits")
    outcome = TaskOutcome.COMPLETED_UNVERIFIED
    if not terminal:
        outcome = TaskOutcome.INCOMPLETE
    elif reason in {"deadline_exhausted", "run_budget_exhausted", "budget_cancelled"}:
        outcome = TaskOutcome.DEADLINE_EXCEEDED
    elif reason in {"cancelled", "cancelled_by_user", "keyboard_interrupt"}:
        outcome = TaskOutcome.CANCELLED
    elif reason in {"provider_failure", "provider_failure_salvaged", "provider_unavailable"}:
        outcome = TaskOutcome.PROVIDER_FAILURE
    elif blocked or reason in {
        "blocked",
        "approval_declined",
        "prompt_blocked",
    }:
        outcome = TaskOutcome.BLOCKED
    elif exit_code != 0 or reason in {
        "max_steps_exhausted",
        "max_steps_exceeded",
        "output_token_limit",
        "empty_response_stall_salvaged",
        "root_subagent_semantic_repetition_backstop",
        "subagent_repetition_backstop",
    }:
        outcome = TaskOutcome.INCOMPLETE
    elif (
        not problems
        and certificate.get("status") == "SUFFICIENT"
        and (hard or state.get("accepted_verification_evidence"))
    ):
        outcome = TaskOutcome.VERIFIED_SUCCESS
    return {
        "schema_version": 1,
        "terminal": terminal,
        "outcome": outcome.value,
        "verified_success": outcome == TaskOutcome.VERIFIED_SUCCESS,
        "task_id": task_id,
        "acceptance_revision": str(contract.get("acceptance_revision") or ""),
        "generation": state.get("verification_relevant_edit_generation", 0),
        "exit_code": exit_code,
        "reason": reason,
        "problems": list(dict.fromkeys(problems)),
        "unresolved_criterion_ids": unresolved,
        "material_paths": list(state.get("touched_repo_paths") or []),
        "completion_certificate": certificate,
        "connector_link_evidence": connector_links,
    }


def task_outcome_notice(record: dict[str, Any]) -> str:
    """Append a host verdict without rewriting or discarding the model's answer."""
    if record.get("verified_success"):
        return ""
    problems = record.get("problems") or []
    if "connector_links_unverified" in problems:
        other_problems = [
            problem for problem in problems if problem != "connector_links_unverified"
        ]
        return "\n\nLink availability has not been verified." + (
            " Required checks also failed or could not be confirmed." if other_problems else ""
        )
    detail = (
        "required checks failed or could not be confirmed."
        if problems
        else "no test or check confirmed this result."
    )
    return "\n\nNot verified: " + detail


def task_outcome_fields(
    record: Any, *, exit_code: int | None = None, reason: str = "completed"
) -> dict[str, Any]:
    """Project a host verdict across adapters without inferring proof from exit zero."""
    valid = (
        isinstance(record, dict)
        and record.get("schema_version") == 1
        and record.get("terminal") is True
        and record.get("outcome") in {item.value for item in TaskOutcome}
    )
    outcome = dict(record) if valid else task_outcome_record(exit_code=exit_code, reason=reason)
    verified = (
        outcome.get("outcome") == TaskOutcome.VERIFIED_SUCCESS
        and outcome.get("verified_success") is True
    )
    outcome["verified_success"] = verified
    return {"task_outcome": outcome, "verified_success": verified}


def run_outcome_for_exit_code(exit_code: int | None) -> RunOutcome:
    if exit_code == SUCCESS_EXIT_CODE:
        return RunOutcome.SUCCESS
    if exit_code == INFRASTRUCTURE_FAILURE_EXIT_CODE:
        return RunOutcome.INFRA_FAIL
    return RunOutcome.FAIL


def extract_process_exit_code(error: Any) -> int | None:
    """Recover a child-process exit code from common runner exceptions."""

    for attr_name in ("exit_code", "returncode", "return_code", "code"):
        value = _coerce_exit_code(getattr(error, attr_name, None))
        if value is not None:
            return value

    message = str(error or "")
    for pattern in (
        r"\bexit(?:\s+(?:code|status))?\s*[=:]?\s*(-?\d+)\b",
        r"\bexited\s+with\s+(?:code|status)\s+(-?\d+)\b",
        r"\breturn(?:code| code)\s*[=:]?\s*(-?\d+)\b",
    ):
        match = re.search(pattern, message, re.IGNORECASE)
        if match is not None:
            return _coerce_exit_code(match.group(1))
    return None


def run_outcome_metadata(exit_code: int | None) -> dict[str, int | str | None]:
    return {
        "alysis_exit_code": exit_code,
        "alysis_outcome": run_outcome_for_exit_code(exit_code).value,
    }


def _coerce_exit_code(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
