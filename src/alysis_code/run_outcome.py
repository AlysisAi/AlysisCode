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
    trusted_entry_blocked: bool = False,
) -> dict[str, Any]:
    """Classify host state; model claims and exit zero are never verification."""
    state = state or {}
    certificate = state.get("completion_certificate") or {}
    problems = list(certificate.get("problems") or [])
    missing = list(state.get("missing_verification_commands") or [])
    # A repository command can remain available without applying to this turn.
    # Only the host certificate's explicit applicability decision can exclude
    # missing/stale inferred coverage; observed failures still count below.
    verification_expected = state.get("verification_expected") is not False
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
    if verification_expected and missing and "verification_incomplete" not in problems:
        problems.append("verification_incomplete")
    unresolved_failures = state.get("unresolved_observed_verification_failures", 0)
    if type(unresolved_failures) is not int or unresolved_failures < 0:
        problems.append("verification_evidence_unavailable")
    observed_failure = type(unresolved_failures) is int and unresolved_failures > 0
    if (failed or observed_failure) and "verification_failed" not in problems:
        problems.append("verification_failed")
    if unresolved and "acceptance_criteria_unverified" not in problems:
        problems.append("acceptance_criteria_unverified")
    if verification_expected and state.get("verification_coverage_stale"):
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
    elif (
        trusted_entry_blocked
        or blocked
        or reason
        in {
            "blocked",
            "computer_use_goal_blocked",
            "approval_declined",
            "prompt_blocked",
        }
    ):
        outcome = TaskOutcome.BLOCKED
    elif exit_code != 0 or reason in {
        "computer_use_goal_incomplete",
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
        "material_paths": sorted(
            set(state.get("touched_repo_paths") or []).union(
                state.get("touched_external_paths") or []
            )
        ),
        "completion_certificate": certificate,
        "connector_link_evidence": connector_links,
        "artifact_checks": list(state.get("artifact_checks") or []),
        "observed_passing_checks": sum(
            isinstance(item, dict)
            and item.get("real_execution") is True
            and item.get("observed_exit_code") == 0
            and item.get("observed_output") is True
            and item.get("generation", 0) == state.get("verification_relevant_edit_generation", 0)
            and item.get("evidence_category") not in {"COMMAND_EXECUTION", "NOT_VERIFICATION"}
            for item in (
                state.get("executed_verification_evidence")
                or state.get("accepted_verification_evidence")
                or []
            )
        ),
    }


def task_outcome_notice(record: dict[str, Any]) -> str:
    """Surface a concrete host concern, not the absence of formal certification.

    COMPLETED_UNVERIFIED is conservative accounting, not evidence that no check
    ran or that a requested Git/file operation failed. Keep that classification
    in the structured outcome without attaching a blanket negative to the reply.
    Never promote an uncertified result to VERIFIED_SUCCESS to remove a notice.
    """
    if record.get("verified_success"):
        return ""
    terminal_notices = {
        TaskOutcome.CANCELLED: "Task cancelled; completion was not confirmed.",
        TaskOutcome.DEADLINE_EXCEEDED: "The run limit was reached; task completion was not confirmed.",
        TaskOutcome.PROVIDER_FAILURE: "The model service failed; task completion was not confirmed.",
        TaskOutcome.BLOCKED: "The task is blocked; completion was not confirmed.",
        TaskOutcome.INCOMPLETE: "The task is incomplete.",
    }
    if notice := terminal_notices.get(record.get("outcome")):
        return "\n\n" + notice
    problems = record.get("problems") or []
    # No edits alone is an accounting limitation, not an unmet requirement:
    # inspection and validation tasks can intentionally leave every file alone.
    # Keep the record unchanged; explicit missing/failed requirements still warn.
    if not problems or set(problems) == {"no_material_edits"}:
        return ""
    # A passing check never cancels contradictory evidence from another check.
    if set(problems).intersection(
        {
            "verification_failed",
            "acceptance_criteria_failed",
            "regressions_detected",
            "unattributed_failures",
            "repro_failing_after_fix",
            "blast_radius_regressions",
        }
    ):
        return "\n\nNot verified: required or observed checks failed." + (
            " Link availability has not been verified."
            if "connector_links_unverified" in problems
            else ""
        )
    if "connector_links_unverified" in problems:
        other_problems = [
            problem for problem in problems if problem != "connector_links_unverified"
        ]
        return "\n\nLink availability has not been verified." + (
            " Required checks also failed or could not be confirmed." if other_problems else ""
        )
    if record.get("observed_passing_checks", 0) > 0 and not set(problems).intersection(
        {"verification_failed", "verification_incomplete", "verification_stale"}
    ):
        return "\n\nObserved checks passed; complete task requirements remain unconfirmed."
    return "\n\nNot verified: some task requirements or checks remain unconfirmed."


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
