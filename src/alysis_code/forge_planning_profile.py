"""Canonical planning profiles shared by every Forge presentation surface.

Profiles are generation settings, not alternate planners.  They tune the one
Python planner's question budget, clarification loop and prompt policy while
leaving plan validation, approval and execution authority unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, cast

PlanningProfile = Literal["quick", "deep"]


@dataclass(frozen=True, slots=True)
class ForgePlanningProfilePolicy:
    name: PlanningProfile
    max_questions_per_turn: int
    max_clarification_rounds: int | None
    question_repair_enabled: bool
    refresh_workspace_context: bool
    prompt_guidance: str


_POLICIES: dict[PlanningProfile, ForgePlanningProfilePolicy] = {
    "quick": ForgePlanningProfilePolicy(
        name="quick",
        max_questions_per_turn=1,
        max_clarification_rounds=1,
        question_repair_enabled=False,
        refresh_workspace_context=False,
        prompt_guidance=(
            "Planning profile: QUICK. Produce an executable draft in this turn whenever it is "
            "safe. Ask at most one question and only when a missing answer blocks a correct plan. "
            "Keep decomposition lean, normally 1-4 tasks, and avoid optional expansion while "
            "still covering required tests and user-facing documentation."
        ),
    ),
    "deep": ForgePlanningProfilePolicy(
        name="deep",
        max_questions_per_turn=3,
        max_clarification_rounds=None,
        question_repair_enabled=True,
        refresh_workspace_context=True,
        prompt_guidance=(
            "Planning profile: DEEP. Trace cross-component effects, compatibility, edge cases, "
            "migration and rollback concerns, verification, and user-facing documentation. Use "
            "a thorough but non-duplicative decomposition, normally 3-7 tasks. Ask up to three "
            "targeted questions when their answers would materially change the design."
        ),
    ),
}


def normalize_planning_profile(
    value: object, *, default: PlanningProfile = "quick"
) -> PlanningProfile:
    """Return one closed profile value or fail before a generation starts."""

    normalized = str(value or "").strip().casefold()
    if not normalized:
        return default
    if normalized not in _POLICIES:
        raise ValueError("Forge planning profile must be quick or deep")
    return cast(PlanningProfile, normalized)


def planning_profile_policy(value: object) -> ForgePlanningProfilePolicy:
    return _POLICIES[normalize_planning_profile(value)]


def clarification_round_limit(configured: int, profile: object) -> int:
    """Apply the profile cap without increasing a stricter configured limit."""

    safe_configured = max(int(configured or 0), 0)
    policy_limit = planning_profile_policy(profile).max_clarification_rounds
    if policy_limit is None:
        return safe_configured
    return min(safe_configured, policy_limit)


__all__ = [
    "ForgePlanningProfilePolicy",
    "PlanningProfile",
    "clarification_round_limit",
    "normalize_planning_profile",
    "planning_profile_policy",
]
