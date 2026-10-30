from __future__ import annotations

from typing import Any


class AgentRuntimeError(RuntimeError):
    def __init__(
        self,
        *args: object,
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        self.result_payload = result_payload
        super().__init__(*args)


class ApprovalDeclinedError(AgentRuntimeError):
    def __init__(
        self,
        approval_kind: str,
        *,
        message: str | None = None,
        result_payload: dict[str, Any] | None = None,
    ) -> None:
        self.approval_kind = str(approval_kind or "approval").strip() or "approval"
        super().__init__(
            message or f"User declined: {self.approval_kind}", result_payload=result_payload
        )


class ApprovalReviewDeniedError(AgentRuntimeError):
    """The approval reviewer ("Approve for me") declined an action.

    Unlike :class:`ApprovalDeclinedError` (a person said no, so the turn stops), this is an
    ordinary tool error: the agent reads the reviewer's reason and can take another route.
    """

    def __init__(self, approval_kind: str, *, reason: str, risk: str = "") -> None:
        self.approval_kind = str(approval_kind or "approval").strip() or "approval"
        self.reason = str(reason or "").strip()
        message = (
            f"Blocked by the approval reviewer: {self.reason or 'no reason given'} "
            "Do not try to reach the same result another way; choose a safer approach, or "
            "explain what you need and let the user decide."
        )
        super().__init__(
            message,
            result_payload={
                "error": message,
                "error_code": "approval_review_denied",
                "dispatch_state": "not_dispatched",
                "approval_kind": self.approval_kind,
                "reviewer_reason": self.reason,
                "risk": str(risk or ""),
                "recoverable": True,
            },
        )


class SessionWorkdirError(AgentRuntimeError):
    pass
