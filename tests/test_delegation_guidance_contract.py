"""Check delegation obligations where the parent actually receives them.

These are delivery contracts, not a simulated measure of model decisions.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

from alysis_code import agent_loop
from alysis_code.agent import session as session_mod
from alysis_code.agent.prompt_guidance import render_guidance
from alysis_code.agent.session import create_session
from alysis_code.config import AppConfig, resolve_prompt_guidance_profile
from alysis_code.prompt_guidance_catalog import (
    GUIDANCE_PROFILES,
    PROMPT_FAMILIES,
    PromptGuidanceProfile,
    profile_variant,
)
from alysis_code.runtime_kind import RuntimeKind
from alysis_code.subagents import built_in_subagents


def _assert_delivered_evidence_contract(system: str, profile: PromptGuidanceProfile) -> None:
    workflow = render_guidance("workflow", profile)
    assert system.count(workflow) == 1
    lines = workflow.lower().splitlines()
    # Assert the shared obligations in the actual delivered block, allowing the
    # general profiles' different prose rather than requiring one density's text.
    assert any("behavior tests" in line and "request" in line for line in lines)
    assert any(
        "update" in line and ("docs" in line or "documentation" in line) and "user-facing" in line
        for line in lines
    )
    assert any("probe" in line and "reported failure mechanism" in line for line in lines)
    assert any(
        any(term in line for term in ("equivalen", "identical"))
        and ("differential evidence" in line or "evidence from both" in line)
        for line in lines
    )


# Batching delivery covers every profile in both modes at the model boundary.
# Repeat these catalog/tool contracts in one-shot for each rendering category.
@pytest.mark.parametrize(
    ("profile", "one_shot"),
    [
        *[pytest.param(profile, False, id=f"chat-{profile}") for profile in GUIDANCE_PROFILES],
        *[
            pytest.param(profile, True, id=f"one-shot-{profile}")
            for profile in ("compact", "balanced", "expanded", "gpt-normal", "gpt-expanded")
        ],
    ],
)
def test_delivered_delegation_profiles_keep_choice_and_synthesis_obligations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: PromptGuidanceProfile,
    one_shot: bool,
) -> None:
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    cfg = AppConfig(
        model="test-model",
        skills_enabled=False,
        prompt_guidance={"default": profile, "model_profiles": {}},
    )
    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        subagents_enabled=True,
        one_shot_execution=one_shot,
        enable_compaction=False,
        verification_enabled=False,
    )
    try:
        assert session.prompt_guidance_profile == profile
        assert session.runtime_kind == (
            RuntimeKind.ONE_SHOT if one_shot else RuntimeKind.INTERACTIVE_CHAT
        )
        system = next(
            message["content"] for message in session.messages if message["role"] == "system"
        )
        _assert_delivered_evidence_contract(system, profile)
        variant = profile_variant(profile)
        delegation_profile = variant if variant in {"compact", "expanded"} else "balanced"
        assert system.count(render_guidance("delegation", delegation_profile)) == 1
        delegation = system.split('<alysis_guidance section="delegation">', 1)[1].split(
            "</alysis_guidance>", 1
        )[0]

        # A different wording density must not remove the reason for a second
        # opinion, its complementary boundary, or the parent's complete answer.
        for obligation in (
            "complementary",
            "consequential uncertainty",
            "different evidence or approach",
            "without repeating the child's whole investigation",
            "In your final synthesis, answer every requested part",
            "supported child findings",
            "conditions",
            "unresolved limits",
        ):
            assert obligation in delegation
        assert "untrusted evidence" in delegation
        trust_boundary = next(
            line for line in delegation.splitlines() if "untrusted evidence" in line
        )
        for term in ("instructions", "authority", "permission"):
            assert term in trust_boundary
        assert "never" in trust_boundary or "cannot change" in trust_boundary
        assert "subagent_spawn" in delegation and "subagent_run" in delegation
        assert "subagent_resume" in delegation and "subagent_send" in delegation
        assert "leave its primary investigation to the child" in delegation
        assert "do different work until its result is needed" in delegation
        assert "An independent review may inspect the same files" in delegation
        assert "Work directly by default" in delegation
        assert "without an explicit user request" in delegation
        assert "extra context, coordination, and latency" in delegation
        assert "Honor explicit requests to delegate" in delegation
        assert "if the benefit is unclear" in delegation.lower()
        if delegation_profile == "compact":
            assert "Wait when dependent on the result, without busywork" in delegation
            assert "delegation only adds a relay" in delegation
        elif delegation_profile == "balanced":
            assert "Before background dispatch, identify" in delegation
            assert "A dependency wait is valid" in delegation
            assert "only relay the answer" in delegation
        else:
            assert "When you reach a real dependency, waiting is appropriate" in delegation
            assert "the child would do the whole task for you to repeat its report" in delegation

        # The effective catalog shortens descriptions to 160 characters. The
        # value/uncertainty guidance must survive there, not only in the registry.
        catalog = next(
            message["content"]
            for message in session.messages
            if "<subagent_context>" in str(message.get("content", ""))
        )
        hints = {}
        for role in ("explorer", "general", "code-reviewer"):
            line = next(line for line in catalog.splitlines() if line.startswith(f"- {role} | "))
            hints[role] = line.split(" | ", 2)[2]
            assert len(hints[role]) <= 160
        assert "bounded question the parent hands over" in hints["explorer"]
        assert "parent advances different work" in hints["explorer"]
        assert "reviews may share files" in hints["explorer"]
        assert "Available slots are limits, not a work plan" in catalog
        assert "do different work or wait for a real dependency" in catalog
        assert "work directly by default; delegate autonomously" in catalog
        assert "plan independent assignments within" not in catalog
        assert "complementary scope" in hints["general"]
        assert "distinct capability" in hints["general"]
        assert "consequential uncertainty" in hints["code-reviewer"]
        assert "different evidence or approach" in hints["code-reviewer"]

        # Refined selection instructions must keep the reviewer callable and
        # preserve its existing read-only boundary and requested-review use.
        reviewer = session.subagent_registry["code-reviewer"]
        assert reviewer.mode == "readonly"
        assert reviewer.allow_workspace_writes is False
        assert reviewer.model_role == "review"
        assert "broader explicitly requested review" in reviewer.system_prompt
        assert "for the assessed scope; state remaining review limits" in reviewer.system_prompt
        assert "Do not attribute every hunk to the task" in reviewer.system_prompt
        assert "subagent_run" in session.tools and "subagent_spawn" in session.tools

        # Model-facing ToolDef overrides can differ from registry descriptions.
        # Check the assembled schema, where an old implementation/verifier
        # pipeline example previously displaced the delegation choice guidance.
        tool_hints = {
            name: session.tools[name].as_openai_tool()["function"]["description"]
            for name in ("subagent_run", "subagent_spawn", "subagent_wait")
        }
        assert "parent blocks" in tool_hints["subagent_run"]
        assert "spawn for independent overlap" in tool_hints["subagent_run"]
        assert "Orient first; hand over a bounded result" in tool_hints["subagent_spawn"]
        assert "you advance different work" in tool_hints["subagent_spawn"]
        assert "Shared requires mode=readonly; isolated writable" in tool_hints["subagent_spawn"]
        for name in ("subagent_run", "subagent_spawn"):
            assert "Work directly by default" in tool_hints[name]
            assert "autonomously when the benefit outweighs overhead" in tool_hints[name]
            assert "Fresh child has no parent conversation" in tool_hints[name]
            assert "include exact relevant requirements and acceptance criteria" in tool_hints[name]
        assert "needed dependency" in tool_hints["subagent_wait"]
        assert "completed reports arrive automatically" in tool_hints["subagent_wait"]
        assert "use their full_result locator" in tool_hints["subagent_wait"]
        for description in tool_hints.values():
            assert "depends_on=[impl]" not in description
            assert "workspace_from_run=impl" not in description

        for role in ("general", "frontend-engineer"):
            helper_parent = session.subagent_registry[role]
            assert "For an independent check, name the consequential uncertainty" in (
                helper_parent.system_prompt
            )
            assert "different evidence or approach needed" in helper_parent.system_prompt
    finally:
        session.close()


# Parent aliases/densities map to one child profile per family. Integration
# tests cover both parent variants; exercise each child with both launch tools.
@pytest.mark.parametrize("profile", [f"{family}-normal" for family in PROMPT_FAMILIES])
@pytest.mark.parametrize("launch_tool", ["subagent_run", "subagent_spawn"])
def test_launched_children_receive_brief_boundary_without_copying_parent_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    profile: PromptGuidanceProfile,
    launch_tool: str,
) -> None:
    delivered: list[dict[str, Any]] = []

    def record_without_provider(self: session_mod.AgentSession, task: str, **_kwargs: Any) -> int:
        delivered.append(
            {
                "task": task,
                "messages": copy.deepcopy(self.messages),
                "mode": self.mode,
                "profile": self.prompt_guidance_profile,
            }
        )
        report = (
            "The assigned parsing scope and supplied criteria were inspected; no files changed."
        )
        self.messages.append({"role": "user", "content": task})
        self.store.append("user_message", {"content": task})
        self.messages.append({"role": "assistant", "content": report})
        self.store.append("assistant_message", {"content": report})
        self.store.append("final", {"content": report})
        return 0

    monkeypatch.setattr(session_mod.AgentSession, "run_turn", record_without_provider)
    monkeypatch.setattr(agent_loop, "create_session", session_mod.create_session)
    registry = built_in_subagents(include_visual_designer=False)
    for definition in registry.values():
        assert "Missing exact requirements or acceptance criteria are unknown" in (
            definition.system_prompt
        )
    parent = create_session(
        cfg=AppConfig(
            model="test-model",
            skills_enabled=False,
            prompt_guidance={"default": profile, "model_profiles": {}},
        ),
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="test-key",
        session_log_dir_override=tmp_path / "sessions",
        enable_compaction=False,
        verification_enabled=False,
        subagents_enabled=True,
        subagent_registry=registry,
    )
    private_parent_context = (
        "Unassigned parent context: keep the unrelated export format unchanged."
    )
    parent.messages.append({"role": "user", "content": private_parent_context})
    task = (
        "Inspect parsing only. Preserve Unicode casefold (Straße → strasse); sort by normalized "
        "key; reject bool counts. Report unknown criteria."
    )
    try:
        general_run_id = None
        for role in ("general", "explorer", "code-reviewer"):
            result = parent.tools[launch_tool].run(
                {"name": role, "task": task, "mode": "readonly", "workspace_view": "shared"}
            )
            assert "error" not in result, result
            if role == "general":
                general_run_id = result["run_id"]
            if launch_tool == "subagent_spawn":
                assert "assigned work with this child" in result["orchestration_note"]
                assert "distinct uncertainty" in result["orchestration_note"]
                assert "wait when the result is a dependency" in result["orchestration_note"]
                collected = parent.tools["subagent_wait"].run(
                    {"run_id": result["run_id"], "timeout_s": 5}
                )
                assert "error" not in collected, collected
        assert len(delivered) == 3
        followup = "Recheck the same parsing criteria without changing files."
        resumed = parent.tools["subagent_resume"].run({"run_id": general_run_id, "task": followup})
        assert "error" not in resumed, resumed
        collected = parent.tools["subagent_wait"].run({"run_id": resumed["run_id"], "timeout_s": 5})
        assert "error" not in collected, collected
        assert not collected["pending_run_ids"]
        assert len(delivered) == 4
        assert {"role": "user", "content": task} in delivered[-1]["messages"]
        assert any(
            message.get("role") == "assistant"
            and "assigned parsing scope" in str(message.get("content", ""))
            for message in delivered[-1]["messages"]
        )
        child_profile = resolve_prompt_guidance_profile(parent.cfg, subagent=True)
        assert child_profile.endswith("-subagent")
        assert child_profile != profile
        for child, role in zip(
            delivered, ("general", "explorer", "code-reviewer", "general"), strict=True
        ):
            assert child["task"] == (followup if child is delivered[-1] else task)
            assert child["mode"] == "readonly"
            assert child["profile"] == child_profile
            bootstrap = "\n".join(str(message.get("content", "")) for message in child["messages"])
            assert private_parent_context not in bootstrap
            system = "\n".join(
                str(message.get("content", ""))
                for message in child["messages"]
                if message.get("role") == "system"
            )
            _assert_delivered_evidence_contract(system, child_profile)
            assert system.count('<alysis_guidance section="workflow">') == 1
            assert '<alysis_guidance section="delegation">' not in system
            assert system.count(registry[role].system_prompt) == 1
            for clause in (
                "Parent conversation is not inherited",
                "assigned brief, provided context, and any restored child history",
                "Missing exact requirements or acceptance criteria are unknown",
                "flag them instead of guessing defaults",
                "Use known evidence to complete the supported part of your narrowed assignment",
            ):
                assert clause in system
            assert "answer the most plausible interpretation" not in system
    finally:
        parent.close()
