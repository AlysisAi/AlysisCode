from __future__ import annotations

import pytest

from alysis_code.agent.prompt_context import (
    _BASE_CLARIFICATION_RULE,
    _SYSTEM_PROMPT_QUESTION_SECTION,
    SYSTEM_PROMPT,
    _compose_session_system_prompt,
)
from alysis_code.prompt_guidance_catalog import ALL_GUIDANCE_PROFILES


@pytest.mark.parametrize("profile", ALL_GUIDANCE_PROFILES)
def test_question_policy_is_shared_across_model_families_and_densities(profile):
    prompt = _compose_session_system_prompt(
        base_prompt=SYSTEM_PROMPT,
        trusted_prompt_append=None,
        guidance_profile=profile,
        include_write_guidance=True,
        include_skill_discovery_guidance=False,
        include_skill_lifecycle_guidance=False,
        include_subagent_guidance=False,
        include_one_shot_guidance=False,
        include_question_guidance=True,
    )
    assert prompt.count(_SYSTEM_PROMPT_QUESTION_SECTION.strip()) == 1
    assert prompt.count(_BASE_CLARIFICATION_RULE) == 1
    assert "ask one concise clarifying question before starting" not in prompt
    assert "Clear tasks need no questions" in prompt
    assert "new consequential uncertainty" in prompt
    assert "permissions, secrets, or essential inputs" in prompt
    assert "name the missing input and hold dependent actions" in prompt


def test_one_shot_composition_never_suggests_waiting_for_question_ui():
    prompt = _compose_session_system_prompt(
        base_prompt=SYSTEM_PROMPT,
        trusted_prompt_append=None,
        include_write_guidance=True,
        include_skill_discovery_guidance=False,
        include_skill_lifecycle_guidance=False,
        include_subagent_guidance=False,
        include_one_shot_guidance=True,
        include_question_guidance=True,
    )
    assert _SYSTEM_PROMPT_QUESTION_SECTION.strip() not in prompt
    assert _BASE_CLARIFICATION_RULE not in prompt
    assert "never ask a question and wait" in prompt
