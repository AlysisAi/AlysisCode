from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# These remain the default limits for the opt-in ``limited`` policy and for
# backwards-compatible configuration files.  The default ``autonomous`` policy
# does not consume them.
DEFAULT_CHAT_MAX_STEPS = 80
DEFAULT_SUBAGENT_MAX_STEPS = 28

AUTONOMOUS_STEP_BUDGET_POLICY = "autonomous"
LIMITED_STEP_BUDGET_POLICY = "limited"

# ``adaptive`` and ``fixed`` are legacy spellings.  Keeping them loadable avoids
# breaking existing installations while giving them the new, explicit contract:
# adaptive -> autonomous, fixed -> limited.
_STEP_BUDGET_POLICY_ALIASES = {
    "adaptive": AUTONOMOUS_STEP_BUDGET_POLICY,
    "fixed": LIMITED_STEP_BUDGET_POLICY,
}
VALID_STEP_BUDGET_POLICIES = {
    AUTONOMOUS_STEP_BUDGET_POLICY,
    LIMITED_STEP_BUDGET_POLICY,
    *_STEP_BUDGET_POLICY_ALIASES,
}


def _positive_int(value: Any, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return max(1, int(default))
    return max(1, parsed)


def normalize_step_budget_policy(raw_value: Any) -> str:
    normalized = str(raw_value or "").strip().lower()
    normalized = _STEP_BUDGET_POLICY_ALIASES.get(normalized, normalized)
    if normalized in {AUTONOMOUS_STEP_BUDGET_POLICY, LIMITED_STEP_BUDGET_POLICY}:
        return normalized
    return AUTONOMOUS_STEP_BUDGET_POLICY


def step_budget_is_autonomous(raw_value: Any) -> bool:
    return normalize_step_budget_policy(raw_value) == AUTONOMOUS_STEP_BUDGET_POLICY


def resolve_subagent_step_profile(subagent_name: Any) -> str | None:
    normalized = str(subagent_name or "").strip().lower()
    if not normalized:
        return None
    if normalized in {
        "explorer",
        "general",
        "frontend-engineer",
        "debugger",
        "verifier",
        "code-reviewer",
        "visual-designer",
    }:
        return normalized
    return "default"


@dataclass(frozen=True)
class StepBudgetRequest:
    kind: str
    policy: str
    hard_cap: int | None
    fixed_override: int | None = None
    mode: str | None = None
    route: str | None = None
    one_shot_execution: bool = False
    one_shot_turn_intent: str | None = None
    verification_enabled: bool = False
    subagents_enabled: bool = False
    subagent_name: str | None = None
    parent_turn_budget: int | None = None
    image_count: int = 0
    explicit_path_count: int = 0


@dataclass(frozen=True)
class StepBudgetResolution:
    kind: str
    policy: str
    hard_cap: int | None
    resolved_max_steps: int | None
    override_applied: bool
    reason: str
    signals_used: dict[str, Any]
    base_steps: int | None
    profile: str | None = None

    @property
    def unlimited(self) -> bool:
        return self.resolved_max_steps is None

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "policy": self.policy,
            "hard_cap": self.hard_cap,
            "resolved_max_steps": self.resolved_max_steps,
            "unlimited": self.unlimited,
            "override_applied": self.override_applied,
            "reason": self.reason,
            "signals_used": dict(self.signals_used),
            "base_steps": self.base_steps,
            "profile": self.profile,
        }


@dataclass
class StepBudgetRuntime:
    active_turn_budget: int | None = None
    last_resolution: StepBudgetResolution | None = None


def _default_cap(kind: str) -> int:
    return {
        "chat_turn": DEFAULT_CHAT_MAX_STEPS,
        "subagent": DEFAULT_SUBAGENT_MAX_STEPS,
    }.get(kind, DEFAULT_CHAT_MAX_STEPS)


def _resolve_limited_cap(request: StepBudgetRequest, *, kind: str) -> int:
    hard_cap = _positive_int(request.hard_cap, default=_default_cap(kind))
    if kind == "subagent" and request.parent_turn_budget is not None:
        hard_cap = min(
            hard_cap,
            _positive_int(request.parent_turn_budget, default=hard_cap),
        )
    return max(1, hard_cap)


def _request_signals(request: StepBudgetRequest) -> dict[str, Any]:
    """Retain useful task-shape telemetry without using it to terminate work."""

    return {
        "mode": request.mode,
        "route": request.route,
        "one_shot_execution": bool(request.one_shot_execution),
        "one_shot_turn_intent": request.one_shot_turn_intent,
        "verification_enabled": bool(request.verification_enabled),
        "subagents_enabled": bool(request.subagents_enabled),
        "subagent_name": request.subagent_name,
        "parent_turn_budget": request.parent_turn_budget,
        "image_count": request.image_count,
        "explicit_path_count": request.explicit_path_count,
    }


def resolve_step_budget(request: StepBudgetRequest) -> StepBudgetResolution:
    """Resolve an optional safety ceiling for one autonomous agent loop.

    Autonomous execution is intentionally unbounded.  A finite ceiling exists
    only when the caller supplies an explicit override or selects the opt-in
    limited policy.  Completion, cancellation, blockers, fatal errors, and an
    optional wall-clock deadline remain independent termination conditions.
    """

    kind = str(request.kind or "").strip().lower() or "chat_turn"
    policy = normalize_step_budget_policy(request.policy)
    profile = resolve_subagent_step_profile(request.subagent_name) if kind == "subagent" else None

    if request.fixed_override is not None:
        explicit_limit = _positive_int(
            request.fixed_override,
            default=_default_cap(kind),
        )
        return StepBudgetResolution(
            kind=kind,
            policy=policy,
            hard_cap=explicit_limit,
            resolved_max_steps=explicit_limit,
            override_applied=True,
            reason="explicit_limit",
            signals_used={
                **_request_signals(request),
                "policy": policy,
                "explicit_limit": explicit_limit,
            },
            base_steps=explicit_limit,
            profile=profile,
        )

    if policy == LIMITED_STEP_BUDGET_POLICY:
        hard_cap = _resolve_limited_cap(request, kind=kind)
        return StepBudgetResolution(
            kind=kind,
            policy=policy,
            hard_cap=hard_cap,
            resolved_max_steps=hard_cap,
            override_applied=False,
            reason="limited_policy",
            signals_used={
                **_request_signals(request),
                "policy": policy,
                "hard_cap": hard_cap,
            },
            base_steps=hard_cap,
            profile=profile,
        )

    return StepBudgetResolution(
        kind=kind,
        policy=AUTONOMOUS_STEP_BUDGET_POLICY,
        hard_cap=None,
        resolved_max_steps=None,
        override_applied=False,
        reason="autonomous_unbounded",
        signals_used={
            **_request_signals(request),
            "policy": AUTONOMOUS_STEP_BUDGET_POLICY,
            "configured_legacy_cap": request.hard_cap,
        },
        base_steps=None,
        profile=profile,
    )
