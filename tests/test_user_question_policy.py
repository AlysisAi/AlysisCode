from __future__ import annotations

import pytest

from alysis_code.agent.prompt_context import (
    _BASE_CLARIFICATION_RULE,
    _SYSTEM_PROMPT_QUESTION_SECTION,
    SYSTEM_PROMPT,
    _compose_session_system_prompt,
)


@pytest.mark.parametrize("one_shot", [False, True], ids=["chat", "one-shot"])
@pytest.mark.parametrize("question_ui", [False, True], ids=["without-question-ui", "question-ui"])
def test_question_policy_follows_runtime_and_ui_availability(one_shot, question_ui):
    # This section is selected by these two flags, independently of the model
    # profile. Family rendering/delivery and alias equivalence have their own
    # exhaustive matrices; repeating them here does not exercise another branch.
    prompt = _compose_session_system_prompt(
        base_prompt=SYSTEM_PROMPT,
        trusted_prompt_append=None,
        include_write_guidance=True,
        include_skill_discovery_guidance=False,
        include_skill_lifecycle_guidance=False,
        include_subagent_guidance=False,
        include_one_shot_guidance=one_shot,
        include_question_guidance=question_ui,
    )
    assert prompt.count(_BASE_CLARIFICATION_RULE) == (0 if one_shot else 1)
    if one_shot:
        assert "never ask a question and wait" in prompt
    if question_ui and not one_shot:
        assert prompt.count(_SYSTEM_PROMPT_QUESTION_SECTION.strip()) == 1
        assert "ask one concise clarifying question before starting" not in prompt
        assert "Clear tasks need no questions" in prompt
        assert "new consequential uncertainty" in prompt
        assert "permissions, secrets, or essential inputs" in prompt
        assert "name the missing input and hold dependent actions" in prompt
    else:
        assert _SYSTEM_PROMPT_QUESTION_SECTION.strip() not in prompt
