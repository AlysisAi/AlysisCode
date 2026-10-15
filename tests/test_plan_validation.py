from __future__ import annotations

import pytest

from alysis_code.failure_category import FailureCategory
from alysis_code.plan_validation import (
    PlannerFailedError,
    find_plan_acceptance_issues,
    raise_for_execution_ready_plan,
    validate_plan,
    validate_plan_against_assets,
)
from alysis_code.task_readiness import find_execution_unready_mutating_tasks


def test_validate_plan_warns_unknown_deps_and_missing_fields() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix task 1",
                "dependencies": ["T99"],
                "acceptance_criteria": [],
                "estimated_files": [],
                "write_scope": [],
            },
            {
                "id": "T02",
                "title": "Task 2",
                "dependencies": ["T01"],
                "acceptance_criteria": ["ok"],
                "estimated_files": ["src/t2.py"],
            },
        ]
    }

    warnings = validate_plan(plan)
    assert "Task T01 has unknown dependency id: T99" in warnings
    assert "Task T01 is missing acceptance_criteria" in warnings
    assert any(
        "runnable or ambiguous task lacks runnable estimated_files/write_scope" in w
        for w in warnings
    )


def test_validate_plan_accepts_v1_and_v2_schema_versions() -> None:
    plan = {
        "schema_version": 1,
        "tasks": [
            {
                "id": "T01",
                "title": "Report",
                "description": "Read-only report.",
                "dependencies": [],
                "acceptance_criteria": ["Done."],
                "estimated_files": [],
                "write_scope": [],
            }
        ],
    }

    with pytest.warns(DeprecationWarning):
        assert validate_plan(plan) == []
    plan["schema_version"] = 2
    assert validate_plan(plan) == []


def test_validate_plan_accepts_valid_asset_briefing_shape() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Use asset",
                "description": "Read-only report.",
                "dependencies": [],
                "acceptance_criteria": ["Done."],
                "estimated_files": [],
                "write_scope": [],
                "asset_briefing": {
                    "primary": [
                        {
                            "asset_id": "ast_aaaaaaaa",
                            "rationale": "Primary evidence",
                            "expected_use": "Use in analysis",
                        }
                    ],
                    "may_need": [],
                },
            }
        ]
    }

    assert not any("asset_briefing" in warning for warning in validate_plan(plan))


def test_validate_plan_asset_briefing_shape_errors() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Use asset",
                "description": "Read-only report.",
                "dependencies": [],
                "acceptance_criteria": ["Done."],
                "estimated_files": [],
                "write_scope": [],
                "asset_briefing": {
                    "primary": [
                        {
                            "asset_id": "ast_aaaaaaaa",
                            "rationale": "Primary evidence",
                            "expected_use": "Use in analysis",
                        }
                    ],
                    "may_need": [
                        {
                            "asset_id": "ast_aaaaaaaa",
                            "rationale": "Duplicate",
                            "expected_use": "Use again",
                        }
                    ],
                },
            }
        ]
    }

    warnings = validate_plan(plan)

    assert any("same asset in primary and may_need" in warning for warning in warnings)


def test_validate_plan_against_assets_reports_missing_and_deleted_refs() -> None:
    class Record:
        def __init__(self, asset_id: str, deleted_at: str | None) -> None:
            self.id = asset_id
            self.deleted_at = deleted_at

    plan = {
        "tasks": [
            {
                "id": "T01",
                "asset_briefing": {
                    "primary": [
                        {
                            "asset_id": "ast_deleted",
                            "rationale": "Deleted",
                            "expected_use": "Use it",
                        }
                    ],
                    "may_need": [
                        {
                            "asset_id": "ast_missing",
                            "rationale": "Missing",
                            "expected_use": "Use it",
                        }
                    ],
                },
            }
        ]
    }

    warnings = validate_plan_against_assets(
        plan,
        {"ast_deleted": Record("ast_deleted", "2026-05-03T00:00:00+00:00")},
    )

    assert "Task T01 references deleted asset id: ast_deleted" in warnings
    assert "Task T01 references missing asset id: ast_missing" in warnings


def test_validate_plan_against_assets_reports_malformed_briefing_without_raising() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "asset_briefing": {"primary": {"asset_id": "ast_bad"}},
            }
        ]
    }

    warnings = validate_plan_against_assets(plan, {})

    assert any("Task T01 has invalid asset_briefing" in warning for warning in warnings)


def test_validate_plan_allows_non_mutating_scope_free_task() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Investigate login issue",
                "description": "Read-only analysis only; report findings.",
                "dependencies": [],
                "acceptance_criteria": ["Findings documented."],
                "estimated_files": [],
                "write_scope": [],
            }
        ]
    }

    warnings = validate_plan(plan)
    assert not any("runnable estimated_files/write_scope" in warning for warning in warnings)


def test_validate_plan_allows_report_only_task_with_likely_runnable_noun() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Compare build options",
                "description": "Report findings only.",
                "dependencies": [],
                "acceptance_criteria": ["Findings documented."],
                "estimated_files": [],
                "write_scope": [],
                "status": "planned",
            }
        ]
    }

    warnings = validate_plan(plan)
    assert not any("runnable estimated_files/write_scope" in warning for warning in warnings)


def test_validate_plan_reports_mutating_task_with_invalid_scope_entries() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix login bug",
                "description": "Update auth flow.",
                "dependencies": [],
                "acceptance_criteria": ["Login works."],
                "estimated_files": ["fix login bug"],
                "write_scope": [".alysis/current_run.json"],
            }
        ]
    }

    warnings = validate_plan(plan)

    assert any(
        "runnable or ambiguous task lacks runnable estimated_files/write_scope" in w
        for w in warnings
    )


def test_missing_write_scope_raises_planner_failed_before_execution() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix calculator division",
                "description": "Update implementation code.",
                "dependencies": [],
                "acceptance_criteria": ["Division by zero raises ValueError."],
                "estimated_files": ["fix calculator division"],
                "write_scope": [],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    assert exc_info.value.failure_category == FailureCategory.PLANNER_FAILED
    assert "Execution blocked:" in str(exc_info.value)
    assert "R4" in str(exc_info.value)
    assert "write_scope" in str(exc_info.value)


def test_execution_readiness_allows_report_only_execution_candidates() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Investigate login issue",
                "description": "Read-only analysis only; report findings.",
                "dependencies": [],
                "acceptance_criteria": ["Findings documented."],
                "estimated_files": [],
                "write_scope": [],
                "status": "planned",
            }
        ]
    }

    raise_for_execution_ready_plan(plan)


def test_execution_readiness_allows_support_task_dependent_on_implementation() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement habit persistence",
                "description": "Update habit.py to save and load habit data.",
                "dependencies": [],
                "acceptance_criteria": ["Habits are persisted."],
                "estimated_files": ["habit.py", "habits.json"],
                "write_scope": ["habit.py", "habits.json"],
                "status": "planned",
            },
            {
                "id": "T02",
                "title": "Add persistence regression tests",
                "description": "Add tests covering save/load behavior and the habits.json fixture.",
                "dependencies": ["T01"],
                "acceptance_criteria": ["Regression tests cover persistence."],
                "estimated_files": ["test_habit.py", "habits.json"],
                "write_scope": ["test_habit.py", "habits.json"],
                "status": "planned",
            },
        ]
    }

    issues = find_plan_acceptance_issues(plan)

    assert not issues
    raise_for_execution_ready_plan(plan)


def test_execution_readiness_rejects_support_task_with_production_fixture_path() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement settings loader",
                "description": "Update app/settings.py to load settings data.",
                "dependencies": [],
                "acceptance_criteria": ["Settings load correctly."],
                "estimated_files": ["app/settings.py"],
                "write_scope": ["app/settings.py"],
                "status": "planned",
            },
            {
                "id": "T02",
                "title": "Add settings regression tests",
                "description": "Add tests and update the app/settings.json fixture.",
                "dependencies": ["T01"],
                "acceptance_criteria": ["Regression tests cover settings loading."],
                "estimated_files": ["test_settings.py", "app/settings.json"],
                "write_scope": ["test_settings.py", "app/settings.json"],
                "status": "planned",
            },
        ]
    }

    issues = find_plan_acceptance_issues(plan)

    assert [issue.rule_id for issue in issues] == ["R3"]
    assert issues[0].task_id == "T02"


@pytest.mark.parametrize(
    ("title", "description", "path"),
    [
        (
            "Create TODO.md with first tasks",
            "Write a simple markdown todo checklist.",
            "TODO.md",
        ),
        (
            "Update notes/ideas.md with two headings",
            "Organize the note content into simple sections.",
            "notes/ideas.md",
        ),
        (
            "Build landing page HTML in index.html",
            "Create the frontend page structure.",
            "index.html",
        ),
        (
            "Add responsive styles in styles.css",
            "Style the frontend page layout.",
            "styles.css",
        ),
    ],
)
def test_execution_readiness_allows_explicit_content_and_frontend_scopes(
    title: str,
    description: str,
    path: str,
) -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": title,
                "description": description,
                "dependencies": [],
                "acceptance_criteria": ["Requested content is updated."],
                "estimated_files": [path],
                "write_scope": [path],
                "status": "planned",
            }
        ]
    }

    raise_for_execution_ready_plan(plan)


def test_execution_readiness_rejects_mixed_content_and_unknown_scopes_without_primary_impl() -> (
    None
):
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Update generated metadata",
                "description": "Update documentation and helper script metadata.",
                "dependencies": [],
                "acceptance_criteria": ["Documentation and metadata are updated."],
                "estimated_files": ["README.md", "scripts/build.sh"],
                "write_scope": ["README.md", "scripts/build.sh"],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    assert exc_info.value.failure_category == FailureCategory.PLANNER_FAILED
    assert "R3" in str(exc_info.value)
    assert "no code implementation paths" in str(exc_info.value)


def test_execution_readiness_rejects_internal_only_write_scope_fixture() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix calculator seed behavior",
                "description": "Update implementation code.",
                "dependencies": [],
                "acceptance_criteria": ["Calculator seed behavior is implemented."],
                "estimated_files": [".alysis/something.json"],
                "write_scope": [".alysis/something.json"],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    assert exc_info.value.failure_category == FailureCategory.PLANNER_FAILED
    assert "R2" in str(exc_info.value)
    assert "all write_scope paths under .alysis/" in str(exc_info.value)


def test_execution_readiness_rejects_code_task_with_docs_only_write_scope() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix calculator behavior",
                "description": "Update the implementation code.",
                "dependencies": [],
                "acceptance_criteria": ["Calculator behavior is fixed."],
                "estimated_files": ["README.md"],
                "write_scope": ["README.md"],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    assert exc_info.value.failure_category == FailureCategory.PLANNER_FAILED
    assert "R3" in str(exc_info.value)
    assert "write_scope is README/docs only" in str(exc_info.value)


def test_execution_readiness_allows_explicit_docs_only_mutation() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Document calculator behavior",
                "description": "Update README documentation.",
                "dependencies": [],
                "acceptance_criteria": ["Documentation is updated."],
                "estimated_files": ["README.md"],
                "write_scope": ["README.md"],
                "status": "planned",
            }
        ]
    }

    raise_for_execution_ready_plan(plan)


def test_execution_readiness_does_not_treat_substring_test_as_support_marker() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix latest calculator behavior",
                "description": "Update implementation code.",
                "dependencies": [],
                "acceptance_criteria": ["Latest behavior is fixed."],
                "estimated_files": ["README.md"],
                "write_scope": ["README.md"],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    assert "R3" in str(exc_info.value)


def test_execution_readiness_allows_support_tasks_with_primary_implementation_scope() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement calculator behavior",
                "description": "Update implementation code.",
                "dependencies": [],
                "acceptance_criteria": ["Calculator behavior is fixed."],
                "estimated_files": ["calc.py"],
                "write_scope": ["calc.py"],
                "status": "planned",
            },
            {
                "id": "T02",
                "title": "Add pytest coverage",
                "description": "Add regression tests for calculator behavior.",
                "dependencies": ["T01"],
                "acceptance_criteria": ["Tests cover the fixed behavior."],
                "estimated_files": ["test_calc.py"],
                "write_scope": ["test_calc.py"],
                "status": "planned",
            },
            {
                "id": "T03",
                "title": "Update README examples",
                "description": "Document the new calculator behavior.",
                "dependencies": ["T01"],
                "acceptance_criteria": ["README examples match behavior."],
                "estimated_files": ["README.md"],
                "write_scope": ["README.md"],
                "status": "planned",
            },
        ]
    }

    raise_for_execution_ready_plan(plan)


def test_execution_readiness_allows_described_support_task_after_implementation_task() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement percent style in formatting.py",
                "description": "Add the style dispatch implementation.",
                "dependencies": [],
                "acceptance_criteria": ["Percent style renders expected output."],
                "estimated_files": ["formatting.py"],
                "write_scope": ["formatting.py"],
                "status": "planned",
            },
            {
                "id": "T02",
                "title": "Add focused percent-style tests in dedicated test file",
                "description": "Create tests/test_percent.py with focused pytest cases.",
                "dependencies": ["T01"],
                "acceptance_criteria": ["pytest -q tests/test_percent.py passes."],
                "estimated_files": ["tests/test_percent.py"],
                "write_scope": ["tests/test_percent.py"],
                "status": "planned",
            },
        ]
    }

    raise_for_execution_ready_plan(plan)


def test_execution_readiness_rejects_behavior_task_with_tests_only_scope() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement requested repository change",
                "description": (
                    "Make the smallest sane parser patch and cover blank entries explicitly."
                ),
                "dependencies": [],
                "acceptance_criteria": [
                    "Add or update regression coverage for the requested behavior."
                ],
                "estimated_files": ["tests/test_parser.py"],
                "write_scope": ["tests/test_parser.py"],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    assert "R3" in str(exc_info.value)
    assert "write_scope has no code implementation paths" in str(exc_info.value)


def test_execution_readiness_accepts_java_implementation_scope() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix Java config loader",
                "description": "Implement ConfigLoader behavior.",
                "dependencies": [],
                "acceptance_criteria": ["ConfigLoader handles defaults."],
                "estimated_files": ["src/main/java/com/example/ConfigLoader.java"],
                "write_scope": ["src/main/java/com/example/ConfigLoader.java"],
                "status": "planned",
            }
        ]
    }

    raise_for_execution_ready_plan(plan)


def test_execution_readiness_rejects_java_tests_only_scope_with_kind_diagnostic() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix Java config loader",
                "description": "Implement ConfigLoader behavior.",
                "dependencies": [],
                "acceptance_criteria": ["ConfigLoader handles defaults."],
                "estimated_files": ["src/test/java/com/example/ConfigLoaderTest.java"],
                "write_scope": ["src/test/java/com/example/ConfigLoaderTest.java"],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    message = str(exc_info.value)
    assert "R3" in message
    assert "write_scope has no code implementation paths" in message
    assert "Java test files only" in message


def test_execution_readiness_accepts_mixed_java_implementation_and_test_scope() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix Java config loader",
                "description": "Implement ConfigLoader behavior and cover it.",
                "dependencies": [],
                "acceptance_criteria": ["ConfigLoader handles defaults."],
                "estimated_files": [
                    "src/main/java/com/example/ConfigLoader.java",
                    "src/test/java/com/example/ConfigLoaderTest.java",
                ],
                "write_scope": [
                    "src/main/java/com/example/ConfigLoader.java",
                    "src/test/java/com/example/ConfigLoaderTest.java",
                ],
                "status": "planned",
            }
        ]
    }

    raise_for_execution_ready_plan(plan)


def test_execution_readiness_reports_unknown_extensions_without_silent_invalid() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix generated metadata",
                "description": "Implement behavior in generated metadata.",
                "dependencies": [],
                "acceptance_criteria": ["Behavior is implemented."],
                "estimated_files": ["scripts/build.weird"],
                "write_scope": ["scripts/build.weird"],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    assert "unknown .weird files only" in str(exc_info.value)


def test_execution_readiness_accepts_broad_domain_package_scope_for_implementation() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix billing tax rounding",
                "description": "Implement calculate_tax rounding behavior in the billing package.",
                "dependencies": [],
                "acceptance_criteria": ["calculate_tax returns the rounded total."],
                "estimated_files": ["billing/**"],
                "write_scope": ["billing/**"],
                "status": "planned",
            }
        ]
    }

    raise_for_execution_ready_plan(plan)


def test_execution_readiness_still_rejects_broad_support_scope_for_behavior_fix() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix billing tax rounding",
                "description": "Implement calculate_tax rounding behavior.",
                "dependencies": [],
                "acceptance_criteria": ["Tax rounding is covered."],
                "estimated_files": ["docs/**"],
                "write_scope": ["docs/**"],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    assert "R3" in str(exc_info.value)
    assert "write_scope is README/docs only" in str(exc_info.value)


def test_execution_readiness_treats_packaging_manifest_as_primary_scope() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Add console script entry point",
                "description": "Wire the package CLI entry point through pyproject metadata.",
                "dependencies": [],
                "acceptance_criteria": ["Console script is installable and verified."],
                "estimated_files": ["pyproject.toml", "tests/test_entry_points.py"],
                "write_scope": ["pyproject.toml", "tests/test_entry_points.py"],
                "status": "planned",
            }
        ]
    }

    raise_for_execution_ready_plan(plan)


def test_execution_readiness_rejects_missing_executor_required_field() -> None:
    plan = {
        "tasks": [
            {
                "title": "Fix calculator division",
                "description": "Update implementation code.",
                "dependencies": [],
                "acceptance_criteria": ["Division by zero raises ValueError."],
                "estimated_files": ["src/calc.py"],
                "write_scope": ["src/calc.py"],
                "status": "planned",
            }
        ]
    }

    with pytest.raises(PlannerFailedError) as exc_info:
        raise_for_execution_ready_plan(plan)

    assert exc_info.value.failure_category == FailureCategory.PLANNER_FAILED
    assert "R4" in str(exc_info.value)
    assert "missing field: id" in str(exc_info.value)


def test_validate_plan_flags_ambiguous_scope_empty_execution_candidate() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Task A",
                "description": "Manual planning chat task: Task A",
                "dependencies": [],
                "acceptance_criteria": ["Task is complete."],
                "estimated_files": [],
                "write_scope": [],
                "status": "planned",
            }
        ]
    }

    warnings = validate_plan(plan)

    assert any(
        "runnable or ambiguous task lacks runnable estimated_files/write_scope" in w
        for w in warnings
    )


def test_execution_readiness_blocks_ambiguous_scope_empty_execution_candidate() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Do the work",
                "description": "Manual planning chat task: Do the work",
                "dependencies": [],
                "acceptance_criteria": ["Work is done."],
                "estimated_files": [],
                "write_scope": [],
                "status": "planned",
            }
        ]
    }

    issues = find_execution_unready_mutating_tasks(plan)

    assert len(issues) == 1
    assert issues[0].task_id == "T01"
    assert (
        "runnable or ambiguous task lacks runnable estimated_files/write_scope" in issues[0].warning
    )


def test_plan_acceptance_rejects_scope_missing_explicit_path_hint() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix RTL grid alignment in src/service.ts",
                "description": "The implementation change belongs in src/service.ts.",
                "dependencies": [],
                "acceptance_criteria": ["The calendar starts on the correct weekday."],
                "estimated_files": ["package.json"],
                "write_scope": ["package.json"],
                "status": "planned",
            }
        ]
    }

    issues = find_plan_acceptance_issues(plan)

    assert any(
        issue.task_id == "T01"
        and "scope omits explicit task path hints: src/service.ts" in issue.observed
        for issue in issues
    )
    with pytest.raises(PlannerFailedError):
        raise_for_execution_ready_plan(plan)


@pytest.mark.parametrize(
    ("forbidden_path", "mentioned_path"),
    [
        ("tests/hidden_acceptance.py", "tests/hidden_acceptance.py"),
        ("tests/hidden", "tests/hidden/acceptance.py"),
    ],
)
def test_plan_acceptance_does_not_require_global_forbidden_path_in_scope(
    forbidden_path: str,
    mentioned_path: str,
) -> None:
    plan = {
        "planning_constraints": {
            "schema_version": 1,
            "target_roots": [],
            "forbidden_roots": [
                {
                    "path": forbidden_path,
                    "kind": "forbidden_root",
                    "reason_code": "hidden_acceptance_surface",
                    "evidence": f"Do not edit {forbidden_path}.",
                }
            ],
            "decoy_roots": [],
            "unrelated_roots": [],
        },
        "tasks": [
            {
                "id": "T01",
                "title": "Implement src/service.py",
                "description": "Update src/service.py and preserve existing behavior.",
                "dependencies": [],
                "acceptance_criteria": [f"{mentioned_path} passes."],
                "estimated_files": ["src/service.py"],
                "write_scope": ["src/service.py"],
                "status": "planned",
            }
        ],
    }

    assert find_plan_acceptance_issues(plan) == []
    raise_for_execution_ready_plan(plan)


def test_plan_acceptance_still_ignores_task_local_forbidden_path_hint() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement src/service.py",
                "description": (
                    "Update src/service.py and do not edit tests/hidden_acceptance.py."
                ),
                "dependencies": [],
                "acceptance_criteria": ["Existing behavior is preserved."],
                "estimated_files": ["src/service.py"],
                "write_scope": ["src/service.py"],
                "status": "planned",
            }
        ]
    }

    assert find_plan_acceptance_issues(plan) == []
    raise_for_execution_ready_plan(plan)


def test_plan_validation_accepts_superseded_and_invalidated_task_statuses() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement obsolete TOML settings",
                "description": "Update settings.toml behavior.",
                "dependencies": [],
                "acceptance_criteria": [],
                "estimated_files": [],
                "write_scope": [],
                "status": "superseded",
            },
            {
                "id": "T02",
                "title": "Implement obsolete CLI flag settings",
                "description": "Update CLI flag behavior.",
                "dependencies": [],
                "acceptance_criteria": [],
                "estimated_files": [],
                "write_scope": [],
                "status": "invalidated",
            },
        ]
    }

    warnings = validate_plan(plan)

    assert warnings == []
    assert find_execution_unready_mutating_tasks(plan) == []


def test_plan_validation_rejects_monorepo_task_outside_target_root() -> None:
    plan = {
        "planning_constraints": {
            "schema_version": 1,
            "target_roots": [
                {
                    "path": "services/api",
                    "kind": "target_root",
                    "reason_code": "user_target_root",
                    "evidence": "only services/api",
                }
            ],
            "forbidden_roots": [],
            "decoy_roots": [
                {
                    "path": "services/worker",
                    "kind": "decoy_root",
                    "reason_code": "decoy_path_constraint",
                    "evidence": "worker is a decoy",
                }
            ],
            "unrelated_roots": [],
        },
        "tasks": [
            {
                "id": "T01",
                "title": "Fix API config",
                "description": "Update APP_REGION precedence in services/api.",
                "dependencies": [],
                "acceptance_criteria": ["API config precedence is correct."],
                "estimated_files": ["services/api/src/config.ts"],
                "write_scope": ["services/api/src/config.ts"],
                "status": "planned",
            },
            {
                "id": "T02",
                "title": "Fix worker config",
                "description": "Update worker config.",
                "dependencies": [],
                "acceptance_criteria": ["Worker config changes."],
                "estimated_files": ["services/worker/src/config.ts"],
                "write_scope": ["services/worker/src/config.ts"],
                "status": "planned",
            },
        ],
    }

    issues = find_plan_acceptance_issues(plan)

    assert any(
        issue.rule_id == "R5" and issue.task_id == "T02" and "decoy_root" in issue.observed
        for issue in issues
    )
    with pytest.raises(PlannerFailedError):
        raise_for_execution_ready_plan(plan)


def test_plan_validation_rejects_decoy_write_scope_even_when_estimated_files_target_api() -> None:
    plan = {
        "planning_constraints": {
            "schema_version": 1,
            "target_roots": [{"path": "services/api", "reason_code": "user_target_root"}],
            "forbidden_roots": [],
            "decoy_roots": [{"path": "services/worker", "reason_code": "decoy_path_constraint"}],
            "unrelated_roots": [],
        },
        "tasks": [
            {
                "id": "T01",
                "title": "Fix API config",
                "description": "Update API behavior.",
                "dependencies": [],
                "acceptance_criteria": ["API behavior is correct."],
                "estimated_files": ["services/api/src/config.ts"],
                "write_scope": ["services/worker/src/config.ts"],
                "status": "planned",
            }
        ],
    }

    issues = find_plan_acceptance_issues(plan)

    assert any(
        issue.rule_id == "R5"
        and issue.task_id == "T01"
        and "services/worker/src/config.ts" in issue.observed
        for issue in issues
    )


def test_plan_validation_allows_explicit_shared_dependency_for_target_root() -> None:
    plan = {
        "planning_constraints": {
            "schema_version": 1,
            "target_roots": [{"path": "packages/web", "reason_code": "user_target_root"}],
            "forbidden_roots": [],
            "decoy_roots": [],
            "unrelated_roots": [],
        },
        "tasks": [
            {
                "id": "T01",
                "title": "Fix shared formatter for web",
                "description": "Update the shared dependency used by packages/web.",
                "dependencies": [],
                "acceptance_criteria": ["packages/web uses the corrected formatter."],
                "estimated_files": ["packages/shared/src/format.ts"],
                "write_scope": ["packages/shared/src/format.ts"],
                "status": "planned",
            }
        ],
    }

    assert [issue for issue in find_plan_acceptance_issues(plan) if issue.rule_id == "R5"] == []


def test_validate_plan_detects_cycle_with_path() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Task 1",
                "dependencies": ["T02"],
                "acceptance_criteria": ["a"],
                "estimated_files": ["src/a.py"],
            },
            {
                "id": "T02",
                "title": "Task 2",
                "dependencies": ["T03"],
                "acceptance_criteria": ["b"],
                "estimated_files": ["src/b.py"],
            },
            {
                "id": "T03",
                "title": "Task 3",
                "dependencies": ["T01"],
                "acceptance_criteria": ["c"],
                "estimated_files": ["src/c.py"],
            },
        ]
    }

    warnings = validate_plan(plan)
    cycle_warnings = [w for w in warnings if w.startswith("Circular dependency detected: ")]
    assert len(cycle_warnings) == 1
    assert "T01" in cycle_warnings[0]
    assert "T02" in cycle_warnings[0]
    assert "T03" in cycle_warnings[0]


def test_validate_plan_warns_for_invalid_task_mcp_scope() -> None:
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Task 1",
                "dependencies": [],
                "acceptance_criteria": ["ok"],
                "estimated_files": ["src/task.py"],
                "mcp_scope": {
                    "allow_resources": "yes",
                    "allowed_tools": [{"server_id": "", "tool_name": "create_issue"}],
                },
            }
        ]
    }

    warnings = validate_plan(plan)

    assert any("mcp_scope.allow_resources" in warning for warning in warnings)
    assert any("server_id cannot be empty" in warning for warning in warnings)
