from __future__ import annotations

import pytest

from alysis_code.forge_planning_profile import (
    clarification_round_limit,
    normalize_planning_profile,
    planning_profile_policy,
)
from alysis_code.plan_assistant import _planner_user_prompt


def test_planning_profiles_have_real_bounded_policy_differences() -> None:
    quick = planning_profile_policy("quick")
    deep = planning_profile_policy("deep")

    assert quick.max_questions_per_turn == 1
    assert quick.max_clarification_rounds == 1
    assert quick.question_repair_enabled is False
    assert quick.refresh_workspace_context is False

    assert deep.max_questions_per_turn == 3
    assert deep.max_clarification_rounds is None
    assert deep.question_repair_enabled is True
    assert deep.refresh_workspace_context is True

    assert clarification_round_limit(8, "quick") == 1
    assert clarification_round_limit(8, "deep") == 8


def test_planning_profile_is_closed_and_defaults_to_quick_at_host_boundaries() -> None:
    assert normalize_planning_profile(None) == "quick"
    assert normalize_planning_profile(" DEEP ") == "deep"
    with pytest.raises(ValueError, match="quick or deep"):
        normalize_planning_profile("cosmetic")


def test_profile_guidance_is_embedded_in_the_actual_planner_prompt() -> None:
    common = {
        "plan": {"tasks": []},
        "transcript_tail": [],
        "user_text": "Implement the feature.",
    }
    quick_prompt = _planner_user_prompt(**common, planning_profile="quick")
    deep_prompt = _planner_user_prompt(**common, planning_profile="deep")

    assert "Planning profile: QUICK" in quick_prompt
    assert "at most one question" in quick_prompt
    assert "normally 1-4 tasks" in quick_prompt
    assert "Planning profile: DEEP" in deep_prompt
    assert "cross-component effects" in deep_prompt
    assert "up to three targeted questions" in deep_prompt
