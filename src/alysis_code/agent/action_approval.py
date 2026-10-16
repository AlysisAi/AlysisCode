"""One engine authority for action approvals, including desktop-dispatched tools."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..surface import ApprovalDecision, ApprovalRequest, StandingApproval
from ..surface.types import approval_review_line
from ..tools.fs import classify_sensitive_path
from .errors import AgentRuntimeError, ApprovalReviewDeniedError


def action_needs_person(request: ApprovalRequest, root: Path) -> bool:
    metadata = request.metadata
    if metadata.get("mandatory_explicit_approval") or metadata.get("allow_for_session_disabled"):
        return True
    for raw in request.files:
        if classify_sensitive_path(str(raw)).sensitive:
            return True
        candidate = Path(raw) if Path(raw).is_absolute() else root / raw
        try:
            candidate.resolve(strict=False).relative_to(root)
        except (OSError, ValueError):
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
) -> ApprovalDecision:
    """Reuse modes, standing rules and the existing reviewer; never grant a mode.

    Full Access removes Alysis action prompts, including mandatory-person prompts.
    Explicit deny rules, readonly admission and separate tool/provider/OS constraints
    remain restrictions, not confirmations this function can waive.
    """
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
    if mode == "fullaccess":
        return ApprovalDecision(allow=True, status="allowed_by_mode")
    if not requires_authorization:
        return ApprovalDecision(allow=True, status="allowed_routine_action")
    review = can_ask_person and mode == "auto" and reviewer is not None and reviewer.bound
    if review and not action_needs_person(request, root):
        if standing is not None and standing.outcome == "allow":
            return ApprovalDecision(allow=True, status="allowed_by_policy")
        if standing is None:
            verdict = reviewer.review(request, workspace_root=root)
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
