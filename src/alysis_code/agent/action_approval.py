"""One engine authority for action approvals, including desktop-dispatched tools."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from ..surface import ApprovalDecision, ApprovalRequest, StandingApproval
from ..surface.types import approval_review_line
from ..tools.fs import classify_sensitive_path
from .errors import AgentRuntimeError, ApprovalReviewDeniedError


def action_needs_person(request: ApprovalRequest, root: Path) -> bool:
    from .artifact_output_observation import FileMetadataApproval
    from .scoped_access import SandboxAccessApproval

    metadata = request.metadata
    authentication = isinstance(request, SandboxAccessApproval) and request.authentication
    if (
        authentication
        or metadata.get("mandatory_explicit_approval")
        or metadata.get("always_ask_person")
        or metadata.get("allow_for_session_disabled")
    ):
        return True
    for raw in request.files:
        if classify_sensitive_path(str(raw)).sensitive:
            return True
        candidate = Path(raw) if Path(raw).is_absolute() else root / raw
        try:
            candidate.resolve(strict=False).relative_to(root)
        except (OSError, ValueError):
            if not isinstance(request, (SandboxAccessApproval, FileMetadataApproval)):
                return True
    return False


def decide_action_approval(
    request: ApprovalRequest,
    *,
    mode: str,
    root: Path,
    surface: Any,
    can_ask_person: bool,
    reviewer: Any = None,
    announce: Callable[[ApprovalRequest, Any], None] | None = None,
    expiry_as_error: bool = True,
    requires_authorization: bool = True,
    operation_approved: bool = False,
) -> ApprovalDecision:
    """Reuse modes, standing rules and the existing reviewer; never grant a mode.

    Full Access removes Alysis action prompts, including mandatory-person prompts,
    except requests marked ``always_ask_person`` (remote connector writes), which
    always reach a person. Explicit deny rules, readonly admission and separate
    tool/provider/OS constraints remain restrictions, not confirmations this
    function can waive.
    """
    from .scoped_access import SandboxAccessApproval

    if isinstance(request, SandboxAccessApproval) and request.authentication:
        request = replace(
            request,
            allow_for_session_scope=None,
            metadata={
                **request.metadata,
                "mandatory_explicit_approval": True,
                "always_ask_person": True,
                "allow_for_session_disabled": True,
            },
        )
    if mode == "readonly" and request.metadata.get("access") != "read":
        return ApprovalDecision(allow=False, status="denied_by_policy")
    standing = None
    handler = getattr(surface, "standing_approval", None)
    if callable(handler):
        try:
            candidate = handler(request)
            if isinstance(candidate, StandingApproval):
                standing = candidate
        except Exception:  # noqa: BLE001 - preserve the existing fail-closed policy boundary
            standing = StandingApproval("deny", "the permission rules could not be read")
    if standing is not None and standing.outcome == "deny":
        return ApprovalDecision(allow=False, status="denied_by_policy")
    if mode == "fullaccess" and request.metadata.get("always_ask_person") is not True:
        return ApprovalDecision(allow=True, status="allowed_by_mode")
    if not requires_authorization and not any(
        request.metadata.get(flag)
        for flag in (
            "mandatory_explicit_approval",
            "always_ask_person",
            "allow_for_session_disabled",
        )
    ):
        return ApprovalDecision(allow=True, status="allowed_routine_action")
    # The exact action already appeared in an approved operation-level sandbox
    # request. Reuse that decision, never a grant from a different command/turn.
    # Explicit ask/deny rules and mandatory-person decisions still win.
    if operation_approved and standing is None and not action_needs_person(request, root):
        return ApprovalDecision(allow=True, status="allowed_by_operation_approval")
    review = can_ask_person and mode == "auto" and reviewer is not None and reviewer.bound
    if review and not action_needs_person(request, root):
        if standing is not None and standing.outcome == "allow":
            return ApprovalDecision(allow=True, status="allowed_by_policy")
        if standing is None:
            verdict = reviewer.review(request, workspace_root=root)
            if verdict.failure == "context_changed":
                raise AgentRuntimeError(
                    verdict.error,
                    result_payload={
                        "dispatch_state": "not_dispatched",
                        "error_code": "approval_context_changed",
                        "recoverable": True,
                    },
                )
            if announce is not None:
                announce(request, verdict)
            else:
                emit = getattr(surface, "emit_approval_reviewed", None)
                if callable(emit):
                    emit(
                        verdict.outcome,
                        request.command or request.kind,
                        verdict.reason or verdict.error,
                        risk=verdict.risk,
                        approval_kind=request.kind,
                    )
                else:
                    surface.emit_info(
                        approval_review_line(
                            verdict.outcome, request.kind, verdict.reason or verdict.error
                        )
                    )
            if verdict.outcome == "allow":
                return ApprovalDecision(allow=True, status="allowed_by_reviewer")
            if verdict.outcome == "deny":
                raise ApprovalReviewDeniedError(
                    request.kind, reason=verdict.reason, risk=verdict.risk
                )
            # Keep uncertainty distinct from a risk finding in the manual card.
            request = replace(
                request,
                reason=request.reason
                + " Automatic review needs your decision: "
                + (verdict.reason or verdict.error),
                metadata={
                    **request.metadata,
                    "approval_review": {
                        "risk": verdict.risk or "unknown",
                        "intent_level": verdict.user_authorization,
                        "failure": verdict.failure,
                    },
                },
            )
    if not can_ask_person:
        raise AgentRuntimeError(
            f"{request.reason or 'This action needs approval'}. Nobody can "
            "be asked in this run, so it did not run.",
            result_payload={"dispatch_state": "not_dispatched"},
        )
    decision = surface.request_approval(request)
    if review:
        reviewer.record_person_decision(allowed=decision.allow)
    if expiry_as_error and not decision.allow and decision.status not in (None, "denied"):
        raise AgentRuntimeError(
            "The action was not dispatched: approval " + decision.status + ".",
            result_payload={
                "error_code": "approval_" + decision.status,
                "dispatch_state": "not_dispatched",
                "recoverable": True,
            },
        )
    return decision
