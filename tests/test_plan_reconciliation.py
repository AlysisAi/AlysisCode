from __future__ import annotations

import os
from pathlib import Path

import pytest

from alysis_code import plan_reconciliation
from alysis_code.plan_reconciliation import reconcile_plan_with_workspace
from alysis_code.repo_scan import scan_workspace
from alysis_code.workspace_context import resolve_workspace_context


@pytest.mark.parametrize("excluded_name", [".alysis", "node_modules", "BUILD", ".git"])
def test_symbol_scan_never_enters_excluded_directories(tmp_path, monkeypatch, excluded_name):
    excluded = tmp_path / excluded_name
    (excluded / "deep").mkdir(parents=True)
    (excluded / "deep" / "decoy.py").write_text("def decoy(): pass")
    source = tmp_path / "src" / "service.py"
    source.parent.mkdir()
    source.write_text("def service(): pass")
    original_scandir = os.scandir

    def guarded_scandir(path):
        assert not Path(path).is_relative_to(excluded), "excluded content was traversed"
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", guarded_scandir)
    assert plan_reconciliation._iter_symbol_scan_files(tmp_path) == [source]


def test_symbol_scan_prunes_directories_resolving_outside_workspace(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    redirected = root / "redirected"
    redirected.mkdir()
    (redirected / "foreign.py").write_text("def foreign(): pass")
    source = root / "local.py"
    source.write_text("def local(): pass")
    original_resolve = Path.resolve
    original_scandir = os.scandir

    def resolve(path, *args, **kwargs):
        if path.is_relative_to(redirected):
            return tmp_path / "outside" / path.relative_to(redirected)
        return original_resolve(path, *args, **kwargs)

    def guarded_scandir(path):
        assert not Path(path).is_relative_to(redirected), "outside target was traversed"
        return original_scandir(path)

    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(os, "scandir", guarded_scandir)
    assert plan_reconciliation._iter_symbol_scan_files(root) == [source]


def test_symbol_scan_preserves_file_bound_and_resolved_exclusions(tmp_path, monkeypatch):
    for name in ("first.py", "second.py", "third.py", "alias.py", "notes.txt"):
        (tmp_path / name).write_text("synthetic content")
    alias = tmp_path / "alias.py"
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        if path == alias:
            return tmp_path / ".alysis" / "private.py"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    unbounded = plan_reconciliation._iter_symbol_scan_files(tmp_path)
    assert len(unbounded) == 3 and alias not in unbounded
    monkeypatch.setattr(plan_reconciliation, "_MAX_SYMBOL_SCAN_FILES", 2)
    result = plan_reconciliation._iter_symbol_scan_files(tmp_path)
    assert len(result) == 2
    assert alias not in result
    assert all(path.suffix == ".py" and path.parent == tmp_path for path in result)


def _workspace_context_payload(root: Path) -> dict[str, object]:
    return scan_workspace(context=resolve_workspace_context(root)).to_dict()


def test_reconciliation_fills_missing_estimated_files_from_strong_hints(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Update README.md",
                "description": "Document the new workflow in README.md.",
                "acceptance_criteria": [],
                "estimated_files": [],
                "write_scope": [],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == ["README.md"]
    assert plan["tasks"][0]["write_scope"] == ["README.md"]
    assert result.task_updates["T01"]["estimated_files"] == ["README.md"]
    assert any("inferred estimated_files from task text" in warning for warning in result.warnings)


def test_reconciliation_clears_read_only_task_scope_before_execution(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "ConfigLoader.java").write_text("class ConfigLoader {}\n", encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Read current src/ConfigLoader.java",
                "description": "Inspect the current implementation before downstream fixes.",
                "acceptance_criteria": ["Findings recorded."],
                "estimated_files": ["src/ConfigLoader.java"],
                "write_scope": ["src/ConfigLoader.java"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    task = plan["tasks"][0]
    assert result.changed is True
    assert task["estimated_files"] == []
    assert task["write_scope"] == []
    assert task["analysis_only"] is True
    assert task["task_kind"] == "analysis_only"
    assert any("cleared file mutation scope" in warning for warning in result.warnings)


def test_reconciliation_adds_explicit_task_paths_to_existing_wrong_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "service.ts").write_text("export function render() {}\n", encoding="utf-8")
    (root / "package.json").write_text('{"scripts":{"test":"vitest"}}\n', encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix RTL grid alignment in src/service.ts",
                "description": "The implementation change belongs in src/service.ts.",
                "acceptance_criteria": [],
                "estimated_files": ["package.json"],
                "write_scope": ["package.json"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == ["package.json", "src/service.ts"]
    assert plan["tasks"][0]["write_scope"] == ["package.json", "src/service.ts"]
    joined = " | ".join(result.warnings)
    assert "added explicit task path hints to estimated_files: src/service.ts" in joined
    assert "added explicit task path hints to write_scope: src/service.ts" in joined


def test_reconciliation_seeds_write_scope_and_drops_internal_paths(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Update app",
                "description": "Touch src/app.py and internal state.",
                "acceptance_criteria": [],
                "estimated_files": [".alysis/runs/run_1/plan/plan.json", "src/app.py"],
                "write_scope": [],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == ["src/app.py"]
    assert plan["tasks"][0]["write_scope"] == ["src/app.py"]
    joined = " | ".join(result.warnings)
    assert "dropped protected estimated_files entries" in joined
    assert "seeded write_scope from estimated_files" in joined


def test_reconciliation_warns_for_suspicious_missing_paths_without_crashing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T99",
                "title": "Update missing path handling",
                "description": "Touch missing/nested/file.py handling.",
                "acceptance_criteria": [],
                "estimated_files": ["missing/nested/file.py"],
                "write_scope": ["missing/nested/file.py"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is False
    assert plan["tasks"][0]["estimated_files"] == ["missing/nested/file.py"]
    assert plan["tasks"][0]["write_scope"] == ["missing/nested/file.py"]
    assert any("may be suspicious or missing" in warning for warning in result.warnings)


def test_reconciliation_greenfield_treats_declared_paths_as_create_targets(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    workspace_context["greenfield"] = True
    declared_paths = [
        "app/layout.tsx",
        "components/Navbar.tsx",
        "app/cv/page.tsx",
        "data/cv.ts",
        "content/thoughts/**",
        "app/thoughts/[slug]/page.tsx",
    ]
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Build Next.js portfolio",
                "description": "Scaffold the app routes, CV data, and thoughts content.",
                "acceptance_criteria": ["The requested app structure exists."],
                "estimated_files": list(declared_paths),
                "write_scope": list(declared_paths),
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is False
    assert plan["tasks"][0]["estimated_files"] == declared_paths
    assert plan["tasks"][0]["write_scope"] == declared_paths
    joined = " | ".join(result.warnings)
    assert "may be suspicious or missing" not in joined
    assert "ignored suspicious inferred path hint" not in joined


def test_reconciliation_create_task_suppresses_missing_create_target_warning(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Create app/thoughts/[slug]/page.tsx route",
                "description": "Add the new dynamic route page from scratch.",
                "acceptance_criteria": ["The dynamic route renders a thought entry."],
                "estimated_files": ["app/thoughts/[slug]/page.tsx"],
                "write_scope": ["app/thoughts/[slug]/page.tsx"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is False
    assert not any("may be suspicious or missing" in warning for warning in result.warnings)


def test_reconciliation_accepts_globs_and_dynamic_route_segments(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Update routed thoughts implementation",
                "description": "Adjust thought routing and content loading.",
                "acceptance_criteria": ["Thought routes resolve slugs."],
                "estimated_files": ["content/thoughts/**", "app/thoughts/[...slug]/page.tsx"],
                "write_scope": ["content/thoughts/**", "app/thoughts/[...slug]/page.tsx"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is False
    assert not any("may be suspicious or missing" in warning for warning in result.warnings)


def test_reconciliation_does_not_mine_framework_prose_as_path_hint(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Build Next.js site",
                "description": "Use Next.js app router conventions.",
                "acceptance_criteria": ["The app is implemented."],
                "estimated_files": [],
                "write_scope": [],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert "Next.js" not in plan["tasks"][0]["estimated_files"]
    assert "Next.js" not in plan["tasks"][0]["write_scope"]
    assert not any(
        "ignored suspicious inferred path hint" in warning for warning in result.warnings
    )


def test_reconcile_plan_warns_when_mutating_task_still_lacks_runnable_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix login bug",
                "description": "Update auth flow.",
                "acceptance_criteria": ["Login works."],
                "estimated_files": [],
                "write_scope": [],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is False
    assert plan["tasks"][0]["estimated_files"] == []
    assert plan["tasks"][0]["write_scope"] == []
    assert any(
        "runnable or ambiguous task lacks runnable estimated_files/write_scope" in warning
        for warning in result.warnings
    )


def test_reconciliation_preserves_explicit_user_paths_even_when_missing(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Update exact user-requested files",
                "description": (
                    "Touch src/planner/release_planner.py and tests/test_release_planner.py."
                ),
                "acceptance_criteria": [],
                "estimated_files": [
                    "src/planner/release_planner.py",
                    "tests/test_release_planner.py",
                ],
                "write_scope": [
                    "src/planner/release_planner.py",
                    "tests/test_release_planner.py",
                ],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        user_text=(
            "Update src/planner/release_planner.py and tests/test_release_planner.py exactly."
        ),
    )

    assert result.changed is False
    assert plan["tasks"][0]["estimated_files"] == [
        "src/planner/release_planner.py",
        "tests/test_release_planner.py",
    ]
    assert plan["tasks"][0]["write_scope"] == [
        "src/planner/release_planner.py",
        "tests/test_release_planner.py",
    ]
    assert not any("may be suspicious or missing" in warning for warning in result.warnings)


def test_reconciliation_broadens_existing_directory_scope(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "services" / "api" / "src").mkdir(parents=True)
    (root / "services" / "api" / "src" / "config.ts").write_text(
        "export const value = 1;\n",
        encoding="utf-8",
    )
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix API config behavior",
                "description": "Update the API config implementation.",
                "acceptance_criteria": ["API config behavior is correct."],
                "estimated_files": ["services/api"],
                "write_scope": ["services/api"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == ["services/api/**"]
    assert plan["tasks"][0]["write_scope"] == ["services/api/**"]


def test_reconciliation_infers_explicit_new_file_creation_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "package.json").write_text('{"type":"module"}\n', encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Create bin/word-summary.js CLI",
                "description": "Add bin/word-summary.js as a new executable CLI entrypoint.",
                "acceptance_criteria": ["bin/word-summary.js exists."],
                "estimated_files": [],
                "write_scope": [],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == ["bin/word-summary.js"]
    assert plan["tasks"][0]["write_scope"] == ["bin/word-summary.js"]
    assert not any(
        "ignored suspicious inferred path hint" in warning for warning in result.warnings
    )


def test_reconciliation_drops_forbidden_user_anchor_and_restores_implementation_path(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "todo_export.py").write_text("def export():\n    return []\n", encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix todo_export.py to exclude USER_NOTES.md",
                "description": "Modify todo_export.py to exclude USER_NOTES.md from processing.",
                "acceptance_criteria": ["USER_NOTES.md is not modified."],
                "estimated_files": ["USER_NOTES.md"],
                "write_scope": ["USER_NOTES.md"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        user_text="Preserve the untracked USER_NOTES.md file.",
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == ["todo_export.py"]
    assert plan["tasks"][0]["write_scope"] == ["todo_export.py"]
    joined = " | ".join(result.warnings)
    assert "dropped forbidden estimated_files entries: USER_NOTES.md" in joined
    assert "dropped forbidden write_scope entries: USER_NOTES.md" in joined


def test_reconciliation_preserves_implementation_path_when_preserve_targets_behavior(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "migrate_user.py").write_text("def migrate(user):\n    return user\n", encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix migrate_user.py",
                "description": "Preserve unknown fields in migrate_user.py while migrating users.",
                "acceptance_criteria": ["Backward compatibility is preserved."],
                "estimated_files": ["migrate_user.py"],
                "write_scope": ["migrate_user.py"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is False
    assert plan["tasks"][0]["write_scope"] == ["migrate_user.py"]
    assert not any("dropped forbidden" in warning for warning in result.warnings)


def test_reconciliation_drops_suspicious_off_request_paths_when_user_named_exact_files(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T02",
                "title": "Update release planner files",
                "description": (
                    "Edit src/planner/release_planner.py and tests/test_release_planner.py."
                ),
                "acceptance_criteria": [],
                "estimated_files": [
                    "src/planner/planner.py",
                    "tests/test_planner.py",
                ],
                "write_scope": [
                    "src/planner/planner.py",
                    "tests/test_planner.py",
                ],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        user_text=(
            "Update src/planner/release_planner.py and tests/test_release_planner.py exactly."
        ),
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == [
        "src/planner/release_planner.py",
        "tests/test_release_planner.py",
    ]
    assert plan["tasks"][0]["write_scope"] == [
        "src/planner/release_planner.py",
        "tests/test_release_planner.py",
    ]
    joined = " | ".join(result.warnings)
    assert (
        "dropped suspicious estimated_files entries not grounded in the latest user request"
        in joined
    )
    assert (
        "dropped suspicious write_scope entries not grounded in the latest user request" in joined
    )
    assert "restored grounded estimated_files from task text" in joined


def test_reconciliation_restores_explicit_paths_from_prior_user_turn_when_task_text_is_generic(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement parser fix",
                "description": "Fix the parser regression.",
                "acceptance_criteria": ["Regression is covered by tests."],
                "estimated_files": ["src/planner/planner.py"],
                "write_scope": ["src/planner/planner.py"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        user_text="yes",
        transcript_tail=[
            {
                "role": "user",
                "content": (
                    "Update src/planner/release_planner.py and "
                    "tests/test_release_planner.py exactly."
                ),
            },
            {"role": "assistant", "content": "Need one confirmation."},
            {"role": "user", "content": "yes"},
        ],
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == [
        "src/planner/release_planner.py",
        "tests/test_release_planner.py",
    ]
    assert plan["tasks"][0]["write_scope"] == [
        "src/planner/release_planner.py",
        "tests/test_release_planner.py",
    ]
    joined = " | ".join(result.warnings)
    assert "explicit user grounding" in joined


def test_reconciliation_does_not_use_obsolete_direction_path_as_anchor(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "settings.py").write_text("# settings\n", encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Use APP_TIMEOUT_SECONDS env var",
                "description": "Read APP_TIMEOUT_SECONDS in src/settings.py.",
                "acceptance_criteria": ["APP_TIMEOUT_SECONDS controls timeout."],
                "estimated_files": ["src/settings.py"],
                "write_scope": ["src/settings.py"],
                "status": "planned",
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        user_text=(
            "drop TOML from the plan entirely; use APP_TIMEOUT_SECONDS instead of settings.toml"
        ),
        target_task_ids={"T01"},
    )

    assert result.changed is False
    assert result.task_updates == {}
    assert plan["tasks"][0]["estimated_files"] == ["src/settings.py"]
    assert plan["tasks"][0]["write_scope"] == ["src/settings.py"]
    assert "settings.toml" not in " | ".join(result.warnings)


def test_reconciliation_maps_prior_turn_task_id_anchors_to_matching_tasks(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Planner task",
                "description": "Implement planner change.",
                "acceptance_criteria": [],
                "estimated_files": ["src/planner/planner.py"],
                "write_scope": ["src/planner/planner.py"],
            },
            {
                "id": "T02",
                "title": "Test task",
                "description": "Implement test change.",
                "acceptance_criteria": [],
                "estimated_files": ["tests/test_planner.py"],
                "write_scope": ["tests/test_planner.py"],
            },
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        user_text="yes",
        transcript_tail=[
            {
                "role": "user",
                "content": (
                    "T01 update src/planner/release_planner.py\n"
                    "T02 update tests/test_release_planner.py"
                ),
            },
            {"role": "assistant", "content": "Need one confirmation."},
            {"role": "user", "content": "yes"},
        ],
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == ["src/planner/release_planner.py"]
    assert plan["tasks"][0]["write_scope"] == ["src/planner/release_planner.py"]
    assert plan["tasks"][1]["estimated_files"] == ["tests/test_release_planner.py"]
    assert plan["tasks"][1]["write_scope"] == ["tests/test_release_planner.py"]


def test_reconciliation_can_limit_updates_to_selected_tasks(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (root / "README.md").write_text("hello\n", encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Document README.md",
                "description": "Update README.md for the shipped change.",
                "acceptance_criteria": [],
                "estimated_files": [],
                "write_scope": [],
            },
            {
                "id": "T02",
                "title": "Update src/app.py",
                "description": "Touch src/app.py for the follow-up.",
                "acceptance_criteria": [],
                "estimated_files": [],
                "write_scope": [],
            },
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        target_task_ids={"T02"},
    )

    assert result.changed is True
    assert result.updated_task_ids == ["T02"]
    assert plan["tasks"][0]["estimated_files"] == []
    assert plan["tasks"][0]["write_scope"] == []
    assert plan["tasks"][1]["estimated_files"] == ["src/app.py"]
    assert plan["tasks"][1]["write_scope"] == ["src/app.py"]


def test_reconciliation_replaces_wrong_file_scope_with_symbol_definition(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "stats.py").write_text(
        "def median(values):\n    return values[0]\n",
        encoding="utf-8",
    )
    (root / "formatting.py").write_text(
        "def render_summary(stats):\n    return str(stats)\n",
        encoding="utf-8",
    )
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Add optional unit suffix to render_summary",
                "description": "Modify render_summary in stats.py.",
                "acceptance_criteria": ["Existing empty unit output remains byte-for-byte."],
                "estimated_files": ["stats.py"],
                "write_scope": ["stats.py"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == ["formatting.py"]
    assert plan["tasks"][0]["write_scope"] == ["formatting.py"]
    joined = " | ".join(result.warnings)
    assert "replaced symbol-mismatched estimated_files entries" in joined
    assert "replaced symbol-mismatched write_scope entries" in joined


def test_reconciliation_keeps_explicit_new_file_when_named_scan_finds_existing_source(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "src" / "alysis_code").mkdir(parents=True)
    (root / "src" / "alysis_code" / "cli.py").write_text(
        "def main():\n    return None\n",
        encoding="utf-8",
    )
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Create pomo.py CLI",
                "description": "Build the new timer command in pomo.py.",
                "acceptance_criteria": ["pomo.py exposes the requested timer CLI."],
                "estimated_files": ["pomo.py"],
                "write_scope": ["pomo.py"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is False
    assert plan["tasks"][0]["estimated_files"] == ["pomo.py"]
    assert plan["tasks"][0]["write_scope"] == ["pomo.py"]
    joined = " | ".join(result.warnings)
    assert "kept explicit missing estimated_files path(s)" in joined
    assert "kept explicit missing write_scope path(s)" in joined
    assert "src/alysis_code/cli.py" in joined


def test_reconciliation_adds_unique_named_code_file_for_behavior_task_with_tests_only_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "parser.py").write_text(
        "def normalize_tags(raw):\n    return []\n",
        encoding="utf-8",
    )
    (root / "tests").mkdir()
    (root / "tests" / "test_parser.py").write_text(
        "from parser import normalize_tags\n",
        encoding="utf-8",
    )
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement requested repository change",
                "description": (
                    "Don't do a broad parser rewrite. Make the smallest sane patch and "
                    "cover blank entries explicitly."
                ),
                "acceptance_criteria": [
                    "Add or update regression coverage for the requested behavior."
                ],
                "estimated_files": ["tests/**/*.py"],
                "write_scope": ["tests/**/*.py"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is True
    assert plan["tasks"][0]["estimated_files"] == ["tests/**/*.py", "parser.py"]
    assert plan["tasks"][0]["write_scope"] == ["tests/**/*.py", "parser.py"]
    joined = " | ".join(result.warnings)
    assert "added repository symbol definition path(s) to estimated_files: parser.py" in joined
    assert "added repository symbol definition path(s) to write_scope: parser.py" in joined


def test_reconciliation_adds_unique_named_java_file_for_behavior_task_with_tests_only_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
    (root / "src" / "main" / "java" / "com" / "example" / "ConfigLoader.java").write_text(
        "package com.example;\n\npublic final class ConfigLoader {\n}\n",
        encoding="utf-8",
    )
    (root / "src" / "test" / "java" / "com" / "example").mkdir(parents=True)
    (root / "src" / "test" / "java" / "com" / "example" / "ConfigLoaderTest.java").write_text(
        "package com.example;\n\nclass ConfigLoaderTest {\n}\n",
        encoding="utf-8",
    )
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement ConfigLoader defaults",
                "description": "Make ConfigLoader preserve explicit values and cover defaults.",
                "acceptance_criteria": [
                    "Add or update regression coverage for ConfigLoader defaults."
                ],
                "estimated_files": ["src/test/java/**/*.java"],
                "write_scope": ["src/test/java/**/*.java"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is True
    expected_path = "src/main/java/com/example/ConfigLoader.java"
    assert plan["tasks"][0]["estimated_files"] == ["src/test/java/**/*.java", expected_path]
    assert plan["tasks"][0]["write_scope"] == ["src/test/java/**/*.java", expected_path]
    joined = " | ".join(result.warnings)
    assert (
        f"added repository symbol definition path(s) to estimated_files: {expected_path}" in joined
    )
    assert f"added repository symbol definition path(s) to write_scope: {expected_path}" in joined


def test_reconciliation_handles_mixed_language_monorepo_java_symbol_grounding(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "web" / "src").mkdir(parents=True)
    (root / "web" / "src" / "parser.ts").write_text(
        "export function parseCsv(value: string) { return value; }\n",
        encoding="utf-8",
    )
    (root / "service" / "src" / "main" / "java" / "com" / "example").mkdir(parents=True)
    java_path = "service/src/main/java/com/example/CSVParser.java"
    (root / java_path).write_text(
        "package com.example;\n\npublic final class CSVParser {\n}\n",
        encoding="utf-8",
    )
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement CSVParser quoted-field handling",
                "description": "Patch CSVParser in the Java service.",
                "acceptance_criteria": ["CSVParser handles quoted commas."],
                "estimated_files": ["service/src/test/java/**/*.java"],
                "write_scope": ["service/src/test/java/**/*.java"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
    )

    assert result.changed is True
    assert java_path in plan["tasks"][0]["estimated_files"]
    assert java_path in plan["tasks"][0]["write_scope"]
    assert "web/src/parser.ts" not in plan["tasks"][0]["estimated_files"]


def test_reconciliation_does_not_replace_broad_globs_from_planner_boilerplate(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    for dirname in ("qa_reports", "sandbox", "scripts"):
        (root / dirname).mkdir(parents=True)
    (root / "src" / "alysis_code" / "tools").mkdir(parents=True)
    (root / "src" / "alysis_code" / "tools" / "search.py").write_text(
        "def search(query):\n    return []\n",
        encoding="utf-8",
    )
    (root / "src" / "alysis_code" / "cli_impl" / "commands").mkdir(parents=True)
    (root / "src" / "alysis_code" / "cli_impl" / "commands" / "tools.py").write_text(
        "def tools():\n    return None\n",
        encoding="utf-8",
    )
    workspace_context = _workspace_context_payload(root)
    broad_scope = ["qa_reports/**/*.py", "sandbox/**/*.py", "scripts/**/*.py"]
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Implement requested repository change",
                "description": (
                    "Use local search/read tools to locate the existing implementation and tests, "
                    "then make the smallest repository change needed for: "
                    "Can you help me build a new website?"
                ),
                "acceptance_criteria": [
                    "Locate the relevant implementation and tests before editing.",
                    "Add or update regression coverage for the requested behavior.",
                ],
                "estimated_files": list(broad_scope),
                "write_scope": list(broad_scope),
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        user_text="Can you help me build a new website?",
    )

    assert result.changed is False
    assert plan["tasks"][0]["estimated_files"] == broad_scope
    assert plan["tasks"][0]["write_scope"] == broad_scope
    assert not any(
        "search.py" in warning or "commands/tools.py" in warning for warning in result.warnings
    )


def test_reconciliation_records_monorepo_target_and_drops_decoy_scope(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "services" / "api" / "src").mkdir(parents=True)
    (root / "services" / "worker" / "src").mkdir(parents=True)
    (root / "services" / "api" / "package.json").write_text(
        '{"scripts":{"test":"vitest"}}\n',
        encoding="utf-8",
    )
    (root / "services" / "worker" / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "services" / "api" / "src" / "config.ts").write_text("export {}\n", encoding="utf-8")
    (root / "services" / "worker" / "src" / "config.ts").write_text(
        "export {}\n",
        encoding="utf-8",
    )
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Fix API config",
                "description": "Update APP_REGION precedence in the API service.",
                "acceptance_criteria": ["API precedence is correct."],
                "estimated_files": ["services/api/src/config.ts"],
                "write_scope": ["services/api/src/config.ts"],
            },
            {
                "id": "T02",
                "title": "Fix worker config",
                "description": "Update worker config.",
                "acceptance_criteria": ["Worker config changes."],
                "estimated_files": ["services/worker/src/config.ts"],
                "write_scope": ["services/worker/src/config.ts"],
            },
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        user_text="Only fix API. Worker is a decoy and should remain unchanged.",
    )

    constraints = plan["planning_constraints"]
    assert [item["path"] for item in constraints["target_roots"]] == ["services/api"]
    assert [item["path"] for item in constraints["decoy_roots"]] == ["services/worker"]
    assert plan["tasks"][0]["write_scope"] == ["services/api/src/config.ts"]
    assert plan["tasks"][1]["write_scope"] == []
    joined = " | ".join(result.warnings)
    assert "dropped write_scope outside planning constraints" in joined


def test_reconciliation_transcript_tail_can_ground_decoy_constraints(
    tmp_path: Path,
) -> None:
    root = tmp_path / "repo"
    (root / "services" / "api").mkdir(parents=True)
    (root / "services" / "worker").mkdir(parents=True)
    (root / "services" / "api" / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "services" / "worker" / "package.json").write_text("{}\n", encoding="utf-8")
    workspace_context = _workspace_context_payload(root)
    plan = {
        "tasks": [
            {
                "id": "T01",
                "title": "Update worker implementation",
                "description": "Touch services/worker/src/app.ts.",
                "acceptance_criteria": ["Worker changes."],
                "estimated_files": ["services/worker/src/app.ts"],
                "write_scope": ["services/worker/src/app.ts"],
            }
        ]
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        transcript_tail=[
            {
                "role": "user",
                "content": "Stay inside API only. Worker is a decoy.",
            }
        ],
    )

    assert plan["tasks"][0]["estimated_files"] == []
    assert plan["tasks"][0]["write_scope"] == []
    assert any("outside planning constraints" in warning for warning in result.warnings)


def test_reconciliation_latest_retarget_replaces_stale_constraints(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    (root / "services" / "api" / "src").mkdir(parents=True)
    (root / "services" / "worker" / "src").mkdir(parents=True)
    (root / "services" / "api" / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "services" / "worker" / "package.json").write_text("{}\n", encoding="utf-8")
    (root / "services" / "worker" / "src" / "config.ts").write_text(
        "export {}\n",
        encoding="utf-8",
    )
    workspace_context = _workspace_context_payload(root)
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
                "title": "Fix worker config",
                "description": "Update services/worker config.",
                "acceptance_criteria": ["Worker config changes."],
                "estimated_files": ["services/worker/src/config.ts"],
                "write_scope": ["services/worker/src/config.ts"],
            }
        ],
    }

    result = reconcile_plan_with_workspace(
        plan,
        workspace_root=root,
        workspace_context=workspace_context,
        transcript_tail=[
            {"role": "user", "content": "Only fix API. Worker is a decoy."},
            {"role": "user", "content": "Actually switch to fixing Worker now."},
        ],
    )

    constraints = plan["planning_constraints"]
    assert [item["path"] for item in constraints["target_roots"]] == ["services/worker"]
    assert constraints["decoy_roots"] == []
    assert plan["tasks"][0]["write_scope"] == ["services/worker/src/config.ts"]
    assert result.changed is True
