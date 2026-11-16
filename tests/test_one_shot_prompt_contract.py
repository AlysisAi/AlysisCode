from __future__ import annotations

import pytest

from alysis_code.agent.prompt_context import (
    _SYSTEM_PROMPT_WRITE_SECTION,
    _compose_session_system_prompt,
)
from alysis_code.agent.turn_contract import TurnOutcome, TurnSemantics
from alysis_code.agent_loop import (
    _SYSTEM_PROMPT_ONE_SHOT_SECTION,
    SYSTEM_PROMPT,
    _completion_gate_nudge_message,
)


def test_requirement_review_and_root_fixing_are_shared_with_chat() -> None:
    # These are task-quality requirements, not a consequence of process lifetime.
    prompt = SYSTEM_PROMPT.casefold()
    assert "original acceptance criteria" in prompt
    assert "definition whose behavior is wrong" in prompt and "direct callers" in prompt


def test_test_preservation_guidance_is_shared_with_chat_and_children() -> None:
    assert "Preserve regression coverage" in _SYSTEM_PROMPT_WRITE_SECTION
    assert "Extend existing tests or add new tests" in _SYSTEM_PROMPT_WRITE_SECTION
    assert "update expectations when the requested behavior warrants it" in (
        _SYSTEM_PROMPT_WRITE_SECTION
    )
    assert "Never weaken, skip, or delete checks merely" in _SYSTEM_PROMPT_WRITE_SECTION
    assert "immutable acceptance evidence" not in _SYSTEM_PROMPT_ONE_SHOT_SECTION
    # The execution-evidence rule remains shared and independent of test placement.
    assert "after your last source edit" in SYSTEM_PROMPT
    assert "observing its output and exit code" in SYSTEM_PROMPT


@pytest.mark.parametrize("one_shot", [False, True])
@pytest.mark.parametrize("guidance", ["compact", "balanced", "expanded"])
@pytest.mark.parametrize("role_append", [None, "Complete the delegated code change."])
def test_editing_guidance_survives_parent_child_and_density_composition(
    one_shot: bool, guidance: str, role_append: str | None
) -> None:
    prompt = _compose_session_system_prompt(
        base_prompt=SYSTEM_PROMPT,
        trusted_prompt_append=role_append,
        include_write_guidance=True,
        include_skill_discovery_guidance=False,
        include_skill_lifecycle_guidance=False,
        include_subagent_guidance=role_append is None,
        include_one_shot_guidance=one_shot,
        guidance_profile=guidance,
    )
    assert prompt.count("Preserve regression coverage") == 1
    assert "Extend existing tests or add new tests" in prompt
    assert "Never weaken, skip, or delete checks merely" in prompt
    assert "tracked existing tests as immutable" not in prompt
    assert "never discard uncommitted work" in prompt.casefold()


def test_no_material_edits_nudge_is_implementation_first() -> None:
    message = _completion_gate_nudge_message(["no_material_edits", "verification_not_attempted"])

    assert message.startswith("Finalization check - one pass before you finish:")
    assert "No file changes are recorded yet" in message
    assert "Expected verification has not been completed" in message
    assert message.index("No file changes are recorded yet") < message.index(
        "Expected verification has not been completed"
    )
    assert "this checklist is advisory" in message


def test_verification_not_attempted_nudge_is_verification_first() -> None:
    message = _completion_gate_nudge_message(["verification_not_attempted"])

    assert "Expected verification has not been completed" in message
    assert "No file changes are recorded yet" not in message
    assert "this checklist is advisory" in message
    assert "A written explanation cannot clear this" not in message


def test_semantic_outcomes_drive_execution_posture() -> None:
    assert TurnSemantics(outcome=TurnOutcome.PLAN).execution_posture == "plan_or_analysis_only"
    assert TurnSemantics(outcome=TurnOutcome.INSPECT).execution_posture == "advisory_non_execution"
    assert TurnSemantics(outcome=TurnOutcome.CHANGE).execution_posture == "execute"
