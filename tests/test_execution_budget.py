from __future__ import annotations

import pytest

from alysis_code.agent_loop import create_session
from alysis_code.config import AppConfig
from alysis_code.execution_budget import (
    DEFAULT_EXECUTION_HEADROOM_RESERVE_TOKENS,
    DEFAULT_EXECUTION_IMAGE_RESERVE_TOKENS_PER_IMAGE,
    DEFAULT_EXECUTION_RESPONSE_RESERVE_TOKENS,
    DEFAULT_EXECUTION_SAFETY_MARGIN_TOKENS,
    DEFAULT_MINIMUM_EXECUTION_INSTRUCTION_BUDGET_TOKENS,
    compute_execution_prompt_budget,
)
from alysis_code.execution_shared import build_task_execution_instruction_bundle
from alysis_code.request_estimation import (
    estimate_message_tokens,
    estimate_tool_schema_tokens,
)


def _dense_execution_plan() -> dict:
    return {
        "project_goal": "Ship a production-safe managed execution startup budget",
        "summary": "Dense plan used to stress the first managed-exec request budget",
        "requirements": [
            f"requirement {i:03d}: preserve reporting, task packs, verification, and runtime safety"
            for i in range(80)
        ],
        "tasks": [
            {
                "id": f"T{i:02d}",
                "title": (
                    f"Task {i}: preserve managed execution compaction headroom with deterministic "
                    "startup budgeting"
                ),
                "dependencies": [f"T{i - 1:02d}"] if i > 1 else [],
            }
            for i in range(1, 48)
        ],
        "assets": [
            {
                "stored_path": f".alysis/runs/r/plan/assets/spec_{i:03d}.md",
                "text_copy_path": f".alysis/runs/r/plan/assets_text/spec_{i:03d}.txt",
            }
            for i in range(36)
        ],
    }


@pytest.mark.parametrize("skills_enabled", [True, False])
def test_compute_execution_prompt_budget_matches_real_session_prefix_and_tools(
    tmp_path,
    skills_enabled: bool,
) -> None:
    (tmp_path / "CONVENTIONS.md").write_text("Prefer minimal diffs.\n", encoding="utf-8")
    cfg = AppConfig(
        model="budget-model",
        verify_commands=["pytest -q"],
        subagents_enabled=True,
        skills_enabled=skills_enabled,
    )
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "budget-model": {
                    # Leave room for the bundled document skill and full reserves;
                    # the floor-reclamation cases are exercised separately below.
                    "context_window_tokens": 13000,
                    "max_output_tokens": 1500,
                    "supports_vision": False,
                }
            }
        }
    }

    budget = compute_execution_prompt_budget(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        api_key="override-key",
        deny_write_prefixes=["custom/"],
        allow_write_globs=["src/**", "README.md"],
        non_interactive=True,
        verification_enabled=True,
        authoritative_verification_commands=["pytest -q"],
        subagents_enabled=False,
    )

    session = create_session(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        max_steps=1,
        no_log=True,
        api_key_override="override-key",
        deny_write_prefixes=["custom/"],
        allow_write_globs=["src/**", "README.md"],
        non_interactive=True,
        verification_enabled=True,
        authoritative_verification_commands=["pytest -q"],
        subagents_enabled=False,
    )
    try:
        assert budget.model == "budget-model"
        assert budget.context_window_tokens == 13000
        assert budget.max_output_tokens == 1500
        assert budget.safety_margin_tokens == DEFAULT_EXECUTION_SAFETY_MARGIN_TOKENS
        assert budget.trusted_system_prompt_override_applied is False
        assert budget.trusted_system_prompt_append_applied is False
        assert budget.untrusted_prompt_prelude_applied is False
        assert budget.subagents_enabled is False
        assert (
            budget.requested_execution_response_reserve_tokens
            == DEFAULT_EXECUTION_RESPONSE_RESERVE_TOKENS
        )
        assert (
            budget.effective_execution_response_reserve_tokens
            == DEFAULT_EXECUTION_RESPONSE_RESERVE_TOKENS
        )
        assert budget.execution_headroom_reserve_tokens == DEFAULT_EXECUTION_HEADROOM_RESERVE_TOKENS
        assert (
            budget.requested_execution_headroom_reserve_tokens
            == DEFAULT_EXECUTION_HEADROOM_RESERVE_TOKENS
        )
        assert (
            budget.effective_execution_headroom_reserve_tokens
            == DEFAULT_EXECUTION_HEADROOM_RESERVE_TOKENS
        )
        assert (
            budget.minimum_instruction_budget_tokens
            == DEFAULT_MINIMUM_EXECUTION_INSTRUCTION_BUDGET_TOKENS
        )
        assert budget.reserve_adjustment_applied is False
        assert budget.reserve_adjustment_reason is None
        assert budget.image_count == 0
        assert budget.image_budget_reserve_tokens == 0
        assert budget.pinned_prefix_token_estimate == estimate_message_tokens(session.messages)
        assert budget.tool_schema_token_estimate == estimate_tool_schema_tokens(session.tool_list)
        assert budget.final_instruction_budget == (
            budget.context_window_tokens
            - budget.safety_margin_tokens
            - budget.pinned_prefix_token_estimate
            - budget.tool_schema_token_estimate
            - budget.effective_execution_response_reserve_tokens
            - budget.execution_headroom_reserve_tokens
        )
    finally:
        session.close()


@pytest.mark.parametrize(
    "minimum_instruction_budget_tokens",
    [0, DEFAULT_MINIMUM_EXECUTION_INSTRUCTION_BUDGET_TOKENS],
)
def test_compute_execution_prompt_budget_reserves_tokens_for_attached_images(
    tmp_path, minimum_instruction_budget_tokens: int
) -> None:
    cfg = AppConfig(model="budget-model", skills_enabled=False)
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "budget-model": {
                    # Room for the real prompt prefix plus two image reserves without
                    # hitting the allocator's floors, with some slack for prompt growth.
                    "context_window_tokens": 12_200,
                    "max_output_tokens": 1500,
                    "supports_vision": True,
                }
            }
        }
    }

    baseline = compute_execution_prompt_budget(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        subagents_enabled=False,
        image_count=0,
        minimum_instruction_budget_tokens=minimum_instruction_budget_tokens,
    )
    with_images = compute_execution_prompt_budget(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        subagents_enabled=False,
        image_count=2,
        minimum_instruction_budget_tokens=minimum_instruction_budget_tokens,
    )

    assert with_images.subagents_enabled is False
    assert with_images.image_count == 2
    assert (
        with_images.image_budget_reserve_tokens
        == 2 * DEFAULT_EXECUTION_IMAGE_RESERVE_TOKENS_PER_IMAGE
    )

    # Images consume available context even when the instruction floor causes
    # the allocator to reclaim part of the headroom or response reserve.
    def remaining_allocation(budget):
        return (
            budget.final_instruction_budget
            + budget.effective_execution_response_reserve_tokens
            + budget.effective_execution_headroom_reserve_tokens
        )

    assert with_images.final_instruction_budget < baseline.final_instruction_budget
    assert with_images.final_instruction_budget >= minimum_instruction_budget_tokens
    assert (
        remaining_allocation(baseline) - remaining_allocation(with_images)
        == with_images.image_budget_reserve_tokens
    )
    if not with_images.reserve_adjustment_applied:
        assert (
            baseline.final_instruction_budget - with_images.final_instruction_budget
            == with_images.image_budget_reserve_tokens
        )


def test_compute_execution_prompt_budget_reduces_reserves_to_preserve_instruction_floor(
    tmp_path,
) -> None:
    cfg = AppConfig(model="budget-model")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "budget-model": {
                    # Extra schema semantics must fit, while the requested
                    # reserves still force reduction to reach the 4k floor.
                    "context_window_tokens": 14_500,
                    "max_output_tokens": 4096,
                    "supports_vision": False,
                }
            }
        }
    }

    budget = compute_execution_prompt_budget(
        cfg=cfg,
        root=tmp_path,
        mode="auto",
        yes=True,
        subagents_enabled=False,
        execution_response_reserve_tokens=3000,
        execution_headroom_reserve_tokens=2000,
        minimum_instruction_budget_tokens=4000,
    )

    assert budget.requested_execution_response_reserve_tokens == 3000
    assert budget.requested_execution_headroom_reserve_tokens == 2000
    assert budget.reserve_adjustment_applied is True
    assert budget.effective_execution_headroom_reserve_tokens <= 2000
    assert (
        budget.effective_execution_response_reserve_tokens
        <= budget.requested_execution_response_reserve_tokens
    )
    assert budget.final_instruction_budget >= budget.minimum_instruction_budget_tokens
    if (
        budget.effective_execution_response_reserve_tokens
        < budget.requested_execution_response_reserve_tokens
    ):
        assert budget.effective_execution_headroom_reserve_tokens == 0
    assert budget.reserve_adjustment_reason is not None


def test_execution_instruction_bundle_budget_accounts_for_trusted_system_prompt_override(
    tmp_path,
) -> None:
    cfg = AppConfig(model="budget-model")
    cfg.extra_fields = {
        "model_metadata_overrides": {
            "models": {
                "budget-model": {
                    "context_window_tokens": 12000,
                    "max_output_tokens": 1500,
                    "supports_vision": False,
                }
            }
        }
    }
    plan = {
        "project_goal": "Ship robust execution packs",
        "summary": "Budget test summary",
        "requirements": ["keep assets"],
        "tasks": [{"id": "T01", "title": "Do thing", "dependencies": []}],
        "assets": [],
    }
    task = {
        "id": "T01",
        "title": "Do thing",
        "description": "Short description",
        "acceptance_criteria": ["done"],
        "dependencies": [],
        "estimated_files": ["src/app.py"],
        "write_scope": ["src/app.py"],
        "branch": "feat/t01",
        "status": "planned",
    }

    default_bundle = build_task_execution_instruction_bundle(
        plan=plan,
        task=task,
        root=tmp_path,
        cfg=cfg,
        role_model="budget-model",
        mode="auto",
        yes=True,
        non_interactive=True,
        subagents_enabled=False,
    )
    override_bundle = build_task_execution_instruction_bundle(
        plan=plan,
        task=task,
        root=tmp_path,
        cfg=cfg,
        role_model="budget-model",
        mode="auto",
        yes=True,
        non_interactive=True,
        trusted_system_prompt_override="SHORT EXECUTION OVERRIDE",
        subagents_enabled=False,
    )

    assert default_bundle.budget.trusted_system_prompt_override_applied is False
    assert override_bundle.budget.trusted_system_prompt_override_applied is True
    assert (
        default_bundle.budget.pinned_prefix_token_estimate
        != override_bundle.budget.pinned_prefix_token_estimate
    )
    assert (
        default_bundle.budget.final_instruction_budget
        != override_bundle.budget.final_instruction_budget
    )


def test_authoritative_verification_context_adds_test_only_repair_guidance(tmp_path) -> None:
    task = {
        "id": "T01",
        "title": "Add pytest coverage",
        "description": "Add pytest tests for the CLI.",
        "acceptance_criteria": ["pytest passes"],
        "dependencies": [],
        "estimated_files": ["test_calc.py"],
        "write_scope": ["test_calc.py"],
        "branch": "feat/tests",
        "status": "planned",
    }

    bundle = build_task_execution_instruction_bundle(
        plan={"project_goal": "Test CLI", "tasks": [task], "assets": []},
        task=task,
        root=tmp_path,
        cfg=AppConfig(model="test-model"),
        role_model="test-model",
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        authoritative_verification_commands=["pytest"],
        allow_write_globs=["test_calc.py"],
        subagents_enabled=False,
    )

    assert "For test-only tasks, the tests are part of the deliverable." in bundle.artifact_text
    assert "`capsys`/`monkeypatch`" in bundle.artifact_text
    assert (
        "Do not leave temporary command-output files in the repository root."
        in bundle.artifact_text
    )


def test_authoritative_verification_context_adds_docs_and_packaging_guidance(tmp_path) -> None:
    docs_task = {
        "id": "T01",
        "title": "Fix README doctest",
        "description": "Fix the README doctest for a local module import.",
        "acceptance_criteria": ["doctest passes"],
        "dependencies": [],
        "estimated_files": ["README.md"],
        "write_scope": ["README"],
        "branch": "feat/readme",
        "status": "planned",
    }
    packaging_task = {
        "id": "T02",
        "title": "Wire console script entry point",
        "description": "Add pyproject console script metadata and keep pip install working.",
        "acceptance_criteria": ["pytest -q passes"],
        "dependencies": [],
        "estimated_files": ["pyproject.toml"],
        "write_scope": ["pyproject.toml"],
        "branch": "feat/packaging",
        "status": "planned",
    }

    docs_bundle = build_task_execution_instruction_bundle(
        plan={"project_goal": "Docs", "tasks": [docs_task], "assets": []},
        task=docs_task,
        root=tmp_path,
        cfg=AppConfig(model="test-model"),
        role_model="test-model",
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        authoritative_verification_commands=["pytest --doctest-glob=README.md -q README.md"],
        allow_write_globs=["README"],
        subagents_enabled=False,
    )
    packaging_bundle = build_task_execution_instruction_bundle(
        plan={"project_goal": "Package", "tasks": [packaging_task], "assets": []},
        task=packaging_task,
        root=tmp_path,
        cfg=AppConfig(model="test-model"),
        role_model="test-model",
        mode="auto",
        yes=True,
        non_interactive=True,
        verification_enabled=True,
        authoritative_verification_commands=["pytest -q"],
        allow_write_globs=["pyproject.toml"],
        subagents_enabled=False,
    )

    assert "Use in-document doctest setup/import-path lines" in docs_bundle.artifact_text
    assert "For packaging/install tasks" in packaging_bundle.artifact_text
    assert "Do not create setup.cfg/setup.py" in packaging_bundle.artifact_text
    assert "already in scope" in packaging_bundle.artifact_text


def test_managed_execution_startup_headroom_reduces_first_request_below_trigger(
    tmp_path,
) -> None:
    cfg = AppConfig(model="managed-8k")
    cfg.extra_fields = {
        "compaction": {
            "trigger_ratio": 0.85,
            "target_ratio": 0.75,
        },
        "model_metadata_overrides": {
            "models": {
                "managed-8k": {
                    # Enough for the complete task and required context at the
                    # soft target; only the deliberately dense plan may shrink.
                    "context_window_tokens": 14_000,
                    "max_output_tokens": 1024,
                    "supports_vision": False,
                }
            }
        },
    }
    plan = _dense_execution_plan()
    task = {
        "id": "T47",
        "title": "Keep the first managed request below the compaction trigger",
        "description": (
            "Need the startup pack to preserve execution rules, selected assets, and the active "
            "task while still leaving headroom before execution compaction would fire."
        ),
        "acceptance_criteria": [
            f"criterion {i}: retain the required execution detail" for i in range(24)
        ],
        "dependencies": ["T40", "T41"],
        "estimated_files": [
            "src/alysis_code/execution_budget.py",
            "src/alysis_code/execution_context.py",
            "src/alysis_code/execution_shared.py",
            "src/alysis_code/cli_impl/forge.py",
        ],
        "write_scope": [
            "src/alysis_code/execution_budget.py",
            "src/alysis_code/execution_context.py",
            "src/alysis_code/execution_shared.py",
            "src/alysis_code/cli_impl/forge.py",
        ],
        "branch": "feat/startup-headroom",
        "status": "planned",
    }

    bundle = build_task_execution_instruction_bundle(
        plan=plan,
        task=task,
        root=tmp_path,
        cfg=cfg,
        role_model="managed-8k",
        mode="auto",
        yes=True,
        non_interactive=True,
        subagents_enabled=False,
        managed_execution_startup_headroom=True,
        leading_sections=[
            "## Execution Knowledge\n"
            + "Carry forward the exact forge execution contract and reporting invariants.\n"
        ],
    )

    payload = bundle.to_budget_artifact_payload()

    assert payload["startup_headroom_adjustment_applied"] is True, {
        key: payload.get(key)
        for key in (
            "initial_request_token_estimate",
            "initial_request_token_estimate_before_adjustment",
            "startup_target_tokens",
            "startup_headroom_tokens",
            "startup_headroom_adjustment_reason",
        )
    }
    assert payload["initial_request_token_estimate_before_adjustment"] is not None
    assert (
        payload["initial_request_token_estimate_before_adjustment"]
        >= payload["initial_request_token_estimate"]
    )
    if (
        payload["initial_request_token_estimate_before_adjustment"]
        > payload["compaction_trigger_tokens"]
    ):
        assert payload["initial_request_token_estimate"] < payload["compaction_trigger_tokens"]
    else:
        assert (
            payload["initial_request_token_estimate_before_adjustment"]
            > payload["startup_target_tokens"]
        )
    assert payload["initial_request_token_estimate"] <= payload["startup_target_tokens"]
    assert payload["startup_headroom_tokens"] >= 0
    assert payload["startup_headroom_adjustment_reason"] is not None
    assert payload["truncation_strategy"].startswith("execution_priority")


def test_managed_execution_startup_headroom_skips_adjustment_when_request_already_fits(
    tmp_path,
) -> None:
    cfg = AppConfig(model="managed-8k")
    cfg.extra_fields = {
        "compaction": {
            "trigger_ratio": 0.85,
            "target_ratio": 0.75,
        },
        "model_metadata_overrides": {
            "models": {
                "managed-8k": {
                    # Keep this the already-fitting case after retaining
                    # parameter contracts; the dense case above has more optional context.
                    "context_window_tokens": 14_800,
                    "max_output_tokens": 1024,
                    "supports_vision": False,
                }
            }
        },
    }
    plan = {
        "project_goal": "Keep managed execution startup prompts lean",
        "summary": "Small plan used to confirm startup headroom is a no-op when already safe",
        "requirements": ["preserve the task and execution rules"],
        "tasks": [{"id": "T01", "title": "Do the small task", "dependencies": []}],
        "assets": [],
    }
    task = {
        "id": "T01",
        "title": "Confirm startup headroom no-op",
        "description": "Small task description",
        "acceptance_criteria": ["Keep startup prompts small"],
        "dependencies": [],
        "estimated_files": ["src/example.py"],
        "write_scope": ["src/example.py"],
        "branch": "feat/headroom-noop",
        "status": "planned",
    }

    bundle = build_task_execution_instruction_bundle(
        plan=plan,
        task=task,
        root=tmp_path,
        cfg=cfg,
        role_model="managed-8k",
        mode="auto",
        yes=True,
        non_interactive=True,
        subagents_enabled=False,
        managed_execution_startup_headroom=True,
    )

    payload = bundle.to_budget_artifact_payload()

    assert payload["startup_headroom_adjustment_applied"] is False
    assert payload.get("initial_request_token_estimate_before_adjustment") is None
    assert payload["initial_request_token_estimate"] <= payload["startup_target_tokens"]
    assert payload["startup_headroom_tokens"] >= 0
    assert payload.get("startup_headroom_adjustment_reason") is None
