from __future__ import annotations

import pytest

from alysis_code.config import AppConfig
from alysis_code.step_budget import (
    StepBudgetRequest,
    normalize_step_budget_policy,
    resolve_step_budget,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("autonomous", "autonomous"),
        ("adaptive", "autonomous"),
        ("limited", "limited"),
        ("fixed", "limited"),
    ],
)
def test_step_budget_policy_legacy_aliases_are_canonicalized(raw: str, expected: str) -> None:
    assert normalize_step_budget_policy(raw) == expected
    assert AppConfig(step_budget_policy=raw).step_budget_policy == expected


@pytest.mark.parametrize(
    "kind",
    ["chat_turn", "subagent"],
)
def test_autonomous_policy_has_no_step_ceiling(kind: str) -> None:
    resolution = resolve_step_budget(
        StepBudgetRequest(
            kind=kind,
            policy="autonomous",
            hard_cap=3,
            parent_turn_budget=2,
            subagent_name="explorer",
        )
    )

    assert resolution.policy == "autonomous"
    assert resolution.hard_cap is None
    assert resolution.resolved_max_steps is None
    assert resolution.unlimited is True
    assert resolution.reason == "autonomous_unbounded"
    assert resolution.override_applied is False


def test_legacy_adaptive_policy_is_autonomous_not_complexity_sized() -> None:
    resolution = resolve_step_budget(
        StepBudgetRequest(
            kind="chat_turn",
            policy="adaptive",
            hard_cap=4,
            route="repo",
            one_shot_execution=True,
            one_shot_turn_intent="execute",
            verification_enabled=True,
        )
    )

    assert resolution.policy == "autonomous"
    assert resolution.resolved_max_steps is None
    assert resolution.reason == "autonomous_unbounded"


def test_explicit_limit_is_the_effective_safety_ceiling() -> None:
    resolution = resolve_step_budget(
        StepBudgetRequest(
            kind="subagent",
            policy="autonomous",
            hard_cap=16,
            fixed_override=99,
            parent_turn_budget=8,
            subagent_name="code-reviewer",
        )
    )

    assert resolution.policy == "autonomous"
    assert resolution.hard_cap == 99
    assert resolution.resolved_max_steps == 99
    assert resolution.unlimited is False
    assert resolution.reason == "explicit_limit"
    assert resolution.override_applied is True


def test_limited_policy_uses_configured_hard_cap() -> None:
    resolution = resolve_step_budget(
        StepBudgetRequest(
            kind="chat_turn",
            policy="limited",
            hard_cap=17,
            explicit_path_count=9,
        )
    )

    assert resolution.policy == "limited"
    assert resolution.hard_cap == 17
    assert resolution.resolved_max_steps == 17
    assert resolution.reason == "limited_policy"
    assert resolution.override_applied is False


def test_legacy_fixed_policy_maps_to_limited() -> None:
    resolution = resolve_step_budget(
        StepBudgetRequest(
            kind="chat_turn",
            policy="fixed",
            hard_cap=12,
        )
    )

    assert resolution.policy == "limited"
    assert resolution.resolved_max_steps == 12
    assert resolution.reason == "limited_policy"


def test_limited_subagent_respects_parent_limit() -> None:
    resolution = resolve_step_budget(
        StepBudgetRequest(
            kind="subagent",
            policy="limited",
            hard_cap=16,
            parent_turn_budget=9,
            subagent_name="explorer",
        )
    )

    assert resolution.hard_cap == 9
    assert resolution.resolved_max_steps == 9
    assert resolution.profile == "explorer"


def test_retired_subagent_name_uses_default_step_profile() -> None:
    payload = resolve_step_budget(
        StepBudgetRequest(
            kind="subagent",
            policy="autonomous",
            hard_cap=16,
            subagent_name="test-strategist",
        )
    ).to_payload()

    assert payload["policy"] == "autonomous"
    assert payload["hard_cap"] is None
    assert payload["resolved_max_steps"] is None
    assert payload["unlimited"] is True
    assert payload["profile"] == "default"
