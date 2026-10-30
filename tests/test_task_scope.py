from __future__ import annotations

import subprocess
from pathlib import Path

from alysis_code.runtime_artifacts import is_runtime_artifact_path
from alysis_code.task_scope import (
    SCOPE_CLASS_DANGEROUS_UNRELATED,
    SCOPE_CLASS_EXPECTED_COMPANION,
    SCOPE_CLASS_FORBIDDEN,
    SCOPE_CLASS_LIKELY_MISSING_SCOPE,
    assess_scope_changes,
    check_scope,
    companion_generated_paths_for,
    extract_forbidden_repo_path_hint_records,
    extract_forbidden_repo_path_hints,
    extract_repo_path_hints,
    inspect_existing_test_edits,
    inspect_workspace_git_diff,
    is_agent_internal_scope_path,
    is_existing_test_layout_path,
    is_explicit_repo_path_pattern,
    is_internal_alysis_path,
    is_non_material_untracked_path,
    is_untracked_source_path,
    list_changed_files_including_untracked,
    list_untracked_packaging_metadata_paths,
    normalize_claimed_scope_patterns,
    normalize_repo_path_entry,
    normalize_scope_patterns,
    relocate_known_scratch_artifacts,
    restore_existing_test_paths,
    scope_path_matches_pattern,
)


def test_normalize_scope_patterns_prefers_write_scope() -> None:
    task = {
        "estimated_files": ["src/ignored.py"],
        "write_scope": ["src/core.py", "docs/", "tests/**/*.py"],
    }
    assert normalize_scope_patterns(task) == [
        "src/core.py",
        "docs/**",
        "tests/**/*.py",
        "src/ignored.py",
    ]


def test_normalize_scope_patterns_falls_back_to_estimated_files() -> None:
    task = {
        "estimated_files": ["src/main.py", "pkg/", "scripts/*.sh", "src/main.py"],
        "write_scope": [],
    }
    assert normalize_scope_patterns(task) == ["src/main.py", "pkg/**", "scripts/*.sh"]


def test_normalize_scope_patterns_adds_rust_entrypoint_support(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n', encoding="utf-8")
    (tmp_path / "src" / "lib.rs").write_text("pub mod duration;\n", encoding="utf-8")

    task = {
        "estimated_files": ["src/duration.rs"],
        "write_scope": ["src/duration.rs"],
    }

    assert normalize_scope_patterns(task, root=tmp_path) == [
        "src/duration.rs",
        "Cargo.lock",
        "src/lib.rs",
    ]


def test_normalize_scope_patterns_does_not_readd_forbidden_support_path(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "Cargo.toml").write_text('[package]\nname = "demo"\n', encoding="utf-8")
    (tmp_path / "src" / "lib.rs").write_text("pub mod duration;\n", encoding="utf-8")

    task = {
        "title": "Update src/duration.rs",
        "description": "Do not touch src/lib.rs while adding parser coverage.",
        "estimated_files": ["src/duration.rs"],
        "write_scope": ["src/duration.rs"],
    }

    assert normalize_scope_patterns(task, root=tmp_path) == ["src/duration.rs", "Cargo.lock"]


def test_normalize_scope_patterns_dedupes_readme_alias_family() -> None:
    task = {
        "estimated_files": ["README.md"],
        "write_scope": ["README"],
    }
    assert normalize_scope_patterns(task) == ["README"]


def test_normalize_scope_patterns_ignores_prose_write_scope_and_falls_back() -> None:
    task = {
        "estimated_files": ["index.html", "styles.css"],
        "write_scope": [
            "Add responsive nav in index.html",
            "Use CSS for mobile layout",
        ],
    }
    assert normalize_scope_patterns(task) == ["index.html", "styles.css"]


def test_explicit_scope_accepts_extensionless_file_without_inference() -> None:
    task = {
        "estimated_files": ["calc"],
        "write_scope": ["calc"],
    }

    assert normalize_repo_path_entry("calc") is None
    assert normalize_repo_path_entry("calc", allow_extensionless_file=True) == "calc"
    assert normalize_scope_patterns(task) == ["calc"]
    assert extract_repo_path_hints("Run calc after updating the command behavior.") == []


def test_explicit_scope_rejects_bare_broad_directory_without_trailing_slash() -> None:
    assert normalize_repo_path_entry("src", allow_extensionless_file=True) is None
    assert normalize_scope_patterns({"estimated_files": ["src"], "write_scope": []}) == []
    assert normalize_scope_patterns({"estimated_files": ["src/"], "write_scope": []}) == ["src/**"]


def test_explicit_scope_broadens_existing_bare_directory_when_root_is_known(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()

    assert (
        normalize_repo_path_entry("src", allow_extensionless_file=True, root=tmp_path) == "src/**"
    )
    assert normalize_scope_patterns(
        {"estimated_files": ["src"], "write_scope": []},
        root=tmp_path,
    ) == ["src/**"]


def test_normalize_scope_patterns_adds_python_support_files_for_explicit_modules() -> None:
    task = {
        "estimated_files": ["tests/test_team_labels.py", "src/pkg/module.py"],
        "write_scope": [],
    }
    assert normalize_scope_patterns(task) == [
        "tests/test_team_labels.py",
        "tests/__init__.py",
        "tests/conftest.py",
        "src/pkg/module.py",
        "src/pkg/__init__.py",
    ]


def test_normalize_scope_patterns_adds_related_rust_lockfile_when_manifest_exists(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    task = {
        "estimated_files": ["src/main.rs"],
        "write_scope": [],
    }
    assert normalize_scope_patterns(task, root=tmp_path) == [
        "src/main.rs",
        "Cargo.lock",
    ]


def test_normalize_scope_patterns_adds_related_rust_lockfile_for_directory_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    task = {
        "estimated_files": ["src/"],
        "write_scope": [],
    }
    assert normalize_scope_patterns(task, root=tmp_path) == [
        "src/**",
        "Cargo.lock",
    ]


def test_normalize_scope_patterns_adds_related_rust_lockfile_for_nested_source_dir_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    task = {
        "estimated_files": ["src/utils/"],
        "write_scope": [],
    }
    assert normalize_scope_patterns(task, root=tmp_path) == [
        "src/utils/**",
        "Cargo.lock",
    ]


def test_normalize_scope_patterns_does_not_add_rust_lockfile_for_non_rust_dir_named_src(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    task = {
        "estimated_files": ["docs/src/assets/"],
        "write_scope": [],
    }
    assert normalize_scope_patterns(task, root=tmp_path) == ["docs/src/assets/**"]


def test_normalize_scope_patterns_does_not_add_workspace_lockfile_for_workspace_only_root_src(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/demo"]\n',
        encoding="utf-8",
    )
    crate_root = tmp_path / "crates" / "demo"
    crate_root.mkdir(parents=True)
    (crate_root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    task = {
        "estimated_files": ["src/utils/"],
        "write_scope": [],
    }
    assert normalize_scope_patterns(task, root=tmp_path) == ["src/utils/**"]


def test_normalize_scope_patterns_adds_workspace_lockfile_for_crate_root_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/demo"]\n',
        encoding="utf-8",
    )
    crate_root = tmp_path / "crates" / "demo"
    crate_root.mkdir(parents=True)
    (crate_root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    task = {
        "estimated_files": ["crates/demo/"],
        "write_scope": [],
    }
    assert normalize_scope_patterns(task, root=tmp_path) == [
        "crates/demo/**",
        "Cargo.lock",
    ]


def test_normalize_scope_patterns_adds_workspace_lockfile_for_nested_crate_source_dir_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/demo"]\n',
        encoding="utf-8",
    )
    crate_root = tmp_path / "crates" / "demo" / "src" / "utils"
    crate_root.mkdir(parents=True)
    (tmp_path / "crates" / "demo" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    task = {
        "estimated_files": ["crates/demo/src/utils/"],
        "write_scope": [],
    }
    assert normalize_scope_patterns(task, root=tmp_path) == [
        "crates/demo/src/utils/**",
        "Cargo.lock",
    ]


def test_normalize_claimed_scope_patterns_keeps_python_support_files_out_of_scheduler_scope() -> (
    None
):
    task = {
        "estimated_files": ["tests/test_team_labels.py", "src/pkg/module.py"],
        "write_scope": [],
    }
    assert normalize_claimed_scope_patterns(task) == [
        "tests/test_team_labels.py",
        "src/pkg/module.py",
    ]


def test_normalize_repo_path_entry_rejects_prose_and_accepts_repo_paths() -> None:
    assert normalize_repo_path_entry("index.html") == "index.html"
    assert normalize_repo_path_entry("docs/") == "docs/**"
    assert normalize_repo_path_entry("Add responsive nav in index.html") is None
    assert normalize_repo_path_entry("/tmp/absolute.txt") is None


def test_is_explicit_repo_path_pattern_distinguishes_files_from_globs() -> None:
    assert is_explicit_repo_path_pattern("src/app.py") is True
    assert is_explicit_repo_path_pattern("README.md") is True
    assert is_explicit_repo_path_pattern("src/") is False
    assert is_explicit_repo_path_pattern("tests/**/*.py") is False


def test_extract_repo_path_hints_finds_paths_in_task_text() -> None:
    text = (
        "Create index.html, styles.css, script.js, and README.md. "
        "Update scripts/verify_site.sh as needed. Mention e.g. python -m http.server, "
        "http://localhost:8000/index.html, and grid/flex."
    )
    assert extract_repo_path_hints(text) == [
        "index.html",
        "styles.css",
        "script.js",
        "README.md",
        "scripts/verify_site.sh",
    ]


def test_extract_forbidden_repo_path_hints_finds_preserved_paths() -> None:
    text = "Preserve the untracked USER_NOTES.md file. Update todo_export.py and tests."

    assert extract_forbidden_repo_path_hints(text) == ["USER_NOTES.md"]


def test_extract_forbidden_repo_path_hints_keeps_file_that_implements_exclusion() -> None:
    text = "Modify todo_export.py to exclude USER_NOTES.md from processing."

    assert extract_forbidden_repo_path_hints(text) == ["USER_NOTES.md"]


def test_extract_forbidden_repo_path_hints_ignores_positive_paths() -> None:
    text = "Update README.md and todo_export.py."

    assert extract_forbidden_repo_path_hints(text) == []


def test_extract_forbidden_repo_path_hints_allows_conditional_bug_scope() -> None:
    text = "Do not change formatting.py itself unless a genuine bug is discovered."

    assert extract_forbidden_repo_path_hints(text) == []


def test_extract_forbidden_repo_path_hints_keeps_future_user_permission_forbidden() -> None:
    text = "Do not change formatting.py unless I say so later."

    assert extract_forbidden_repo_path_hints(text) == ["formatting.py"]


def test_is_internal_alysis_path_detects_internal_prefixes() -> None:
    assert is_internal_alysis_path(".alysis/runs/x/plan.json") is True
    assert is_internal_alysis_path(".alysis_images/cache.png") is True
    assert is_internal_alysis_path("alysis-feedback/report.zip") is True
    assert is_agent_internal_scope_path(".forge/scratch.json") is True
    assert is_internal_alysis_path("src/app.py") is False


def test_check_scope_detects_violations() -> None:
    changed = ["src/main.py", "README.md", "docs/guide.md"]
    allowed = ["src/main.py", "docs/**"]
    ok, violations = check_scope(changed, allowed)
    assert ok is False
    assert violations == ["README.md"]


def test_check_scope_readme_alias_matches_readme_md() -> None:
    ok, violations = check_scope(["README.md"], ["README"])

    assert ok is True
    assert violations == []


def test_check_scope_readme_md_alias_matches_readme() -> None:
    ok, violations = check_scope(["README"], ["README.md"])

    assert ok is True
    assert violations == []


def test_scope_path_matches_globstar_direct_child_and_nested_paths() -> None:
    assert scope_path_matches_pattern("tests/test_coupon.py", "tests/**/*.py") is True
    assert scope_path_matches_pattern("tests/unit/test_coupon.py", "tests/**/*.py") is True
    assert scope_path_matches_pattern("tests/data/coupon.json", "tests/**/*.py") is False


def test_check_scope_allows_globstar_direct_child_paths() -> None:
    ok, violations = check_scope(["tests/test_coupon.py"], ["tests/**/*.py"])

    assert ok is True
    assert violations == []


def test_check_scope_allows_existing_directory_scope_descendants(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()

    ok, violations = check_scope(["tests/test_settings.py"], ["tests"], root=tmp_path)

    assert ok is True
    assert violations == []


def test_check_scope_keeps_extensionless_file_scope_narrow_without_directory(
    tmp_path: Path,
) -> None:
    ok_exact, exact_violations = check_scope(["calc"], ["calc"], root=tmp_path)
    ok_descendant, descendant_violations = check_scope(["calc/helper.py"], ["calc"], root=tmp_path)

    assert ok_exact is True
    assert exact_violations == []
    assert ok_descendant is False
    assert descendant_violations == ["calc/helper.py"]


def test_check_scope_ignores_internal_alysis_artifacts() -> None:
    changed = [
        ".alysis/runs/x/plan/plan.json",
        "./.alysis_images/cache.png",
        "alysis-feedback/report.zip",
        "src/main.py",
    ]
    allowed = ["src/main.py"]
    ok, violations = check_scope(changed, allowed)
    assert ok is True
    assert violations == []


def test_check_scope_allows_python_test_support_files() -> None:
    changed = ["tests/test_team_labels.py", "tests/__init__.py", "tests/conftest.py"]
    allowed = ["tests/test_team_labels.py"]
    ok, violations = check_scope(changed, allowed)
    assert ok is True
    assert violations == []


def test_check_scope_allows_python_package_init_support_file() -> None:
    changed = ["src/pkg/module.py", "src/pkg/__init__.py"]
    allowed = ["src/pkg/module.py"]
    ok, violations = check_scope(changed, allowed)
    assert ok is True
    assert violations == []


def test_check_scope_allows_ancestor_directories_for_explicit_file_targets() -> None:
    changed = [
        "src",
        "src/calcbox",
        "src/calcbox/core.py",
        "tests",
        "tests/test_core.py",
    ]
    allowed = ["src/calcbox/core.py", "tests/test_core.py"]
    ok, violations = check_scope(changed, allowed)
    assert ok is True
    assert violations == []


def test_check_scope_ignores_nested_runtime_cache_directories() -> None:
    changed = [
        "src/pkg/__pycache__/module.cpython-312.pyc",
        "nested/.pytest_cache/v/cache/nodeids",
        "./tools/.ruff_cache/0.9.0/12345",
        ".mypy_cache/3.12/pkg/meta.json",
        "src/main.py",
    ]
    allowed = ["src/main.py"]
    ok, violations = check_scope(changed, allowed)
    assert ok is True
    assert violations == []


def test_is_runtime_artifact_path_detects_rust_target_dirs(tmp_path: Path) -> None:
    assert is_runtime_artifact_path("target/debug/app", root=tmp_path) is False
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    nested_crate = tmp_path / "crates" / "demo"
    nested_crate.mkdir(parents=True)
    (nested_crate / "Cargo.toml").write_text(
        '[package]\nname = "nested"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    assert is_runtime_artifact_path("target/debug/app", root=tmp_path) is True
    assert is_runtime_artifact_path("crates/demo/target/debug/app", root=tmp_path) is True
    assert is_runtime_artifact_path("src/target/generated.rs", root=tmp_path) is False


def test_check_scope_allows_related_rust_lockfile_and_target_runtime_artifacts(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    changed = ["src/main.rs", "Cargo.lock", "target/debug/app"]
    allowed = ["src/main.rs"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is True
    assert violations == []


def test_check_scope_allows_related_rust_lockfile_for_directory_scope(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    changed = ["src/main.rs", "Cargo.lock", "target/debug/app"]
    allowed = ["src/**"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is True
    assert violations == []


def test_check_scope_allows_related_rust_lockfile_for_nested_source_dir_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    changed = ["src/utils/mod.rs", "Cargo.lock", "target/debug/app"]
    allowed = ["src/utils/**"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is True
    assert violations == []


def test_check_scope_still_flags_cargo_lock_for_non_rust_dir_named_src(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    changed = ["docs/src/assets/logo.svg", "Cargo.lock"]
    allowed = ["docs/src/assets/**"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is False
    assert violations == ["Cargo.lock"]


def test_check_scope_still_flags_cargo_lock_for_workspace_only_root_src_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/demo"]\n',
        encoding="utf-8",
    )
    crate_root = tmp_path / "crates" / "demo"
    crate_root.mkdir(parents=True)
    (crate_root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    changed = ["src/utils/mod.rs", "Cargo.lock"]
    allowed = ["src/utils/**"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is False
    assert violations == ["Cargo.lock"]


def test_check_scope_allows_workspace_root_lockfile_for_nested_rust_crate(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/demo"]\n',
        encoding="utf-8",
    )
    nested_crate = tmp_path / "crates" / "demo"
    nested_crate.mkdir(parents=True)
    (nested_crate / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    changed = ["crates/demo/src/lib.rs", "Cargo.lock", "crates/demo/target/debug/demo"]
    allowed = ["crates/demo/src/lib.rs"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is True
    assert violations == []


def test_check_scope_allows_workspace_root_lockfile_for_nested_rust_crate_root_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/demo"]\n',
        encoding="utf-8",
    )
    crate_root = tmp_path / "crates" / "demo"
    crate_root.mkdir(parents=True)
    (crate_root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    changed = ["crates/demo/src/lib.rs", "Cargo.lock", "crates/demo/target/debug/demo"]
    allowed = ["crates/demo/**"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is True
    assert violations == []


def test_check_scope_allows_workspace_root_lockfile_for_nested_rust_subdirectory_scope(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["crates/demo"]\n',
        encoding="utf-8",
    )
    crate_root = tmp_path / "crates" / "demo" / "src" / "utils"
    crate_root.mkdir(parents=True)
    (tmp_path / "crates" / "demo" / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    changed = ["crates/demo/src/utils/lib.rs", "Cargo.lock", "crates/demo/target/debug/demo"]
    allowed = ["crates/demo/src/utils/**"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is True
    assert violations == []


def test_check_scope_still_flags_unrelated_rust_lockfiles(tmp_path: Path) -> None:
    crate_root = tmp_path / "crates" / "demo"
    crate_root.mkdir(parents=True)
    (crate_root / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    other_root = tmp_path / "tools" / "other"
    other_root.mkdir(parents=True)
    (other_root / "Cargo.toml").write_text(
        '[package]\nname = "other"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )

    changed = ["crates/demo/src/lib.rs", "tools/other/Cargo.lock"]
    allowed = ["crates/demo/src/lib.rs"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is False
    assert violations == ["tools/other/Cargo.lock"]


def test_check_scope_still_flags_real_source_changes_next_to_runtime_artifacts() -> None:
    changed = [
        "src/pkg/__pycache__/module.cpython-312.pyc",
        "src/other.py",
    ]
    allowed = ["src/main.py"]
    ok, violations = check_scope(changed, allowed)
    assert ok is False
    assert violations == ["src/other.py"]


def test_check_scope_still_flags_unrelated_rust_source_changes_next_to_lockfile_and_target(
    tmp_path: Path,
) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    changed = [
        "src/main.rs",
        "Cargo.lock",
        "target/debug/app",
        "src/other.rs",
    ]
    allowed = ["src/main.rs"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is False
    assert violations == ["src/other.rs"]


def test_check_scope_still_flags_root_target_in_non_rust_repo(tmp_path: Path) -> None:
    changed = [
        "src/main.py",
        "target/generated.txt",
    ]
    allowed = ["src/main.py"]
    ok, violations = check_scope(changed, allowed, root=tmp_path)
    assert ok is False
    assert violations == ["target/generated.txt"]


def test_cargo_lock_is_not_treated_as_non_material_untracked_path() -> None:
    assert is_non_material_untracked_path("Cargo.lock") is False


def test_root_scratch_outputs_are_non_material_untracked_paths() -> None:
    assert is_non_material_untracked_path("pytest_results.txt") is True
    assert is_non_material_untracked_path("pip_output.txt") is True
    assert is_non_material_untracked_path("pip_err.txt") is True
    assert is_non_material_untracked_path("pip_out4.txt") is True
    assert is_non_material_untracked_path("pip_stdout.txt") is True
    assert is_non_material_untracked_path("pip_stderr2.txt") is True
    assert is_non_material_untracked_path("pip_install_out.txt") is True
    assert is_non_material_untracked_path("pytest_out.txt") is True
    assert is_non_material_untracked_path("pytest_out2.txt") is True
    assert is_non_material_untracked_path("pytest_full.txt") is True
    assert is_non_material_untracked_path("run-output.txt") is True
    assert is_non_material_untracked_path("wheel_log.txt") is True
    assert is_non_material_untracked_path("_tmp_content.txt") is True
    assert is_non_material_untracked_path("_d2.txt") is True
    assert is_non_material_untracked_path("_data.txt") is False
    assert is_non_material_untracked_path("_draft.txt") is False
    assert is_non_material_untracked_path("_document.txt") is False
    assert is_non_material_untracked_path("src/output.txt") is False
    assert is_non_material_untracked_path("README.md") is False


def test_root_runtime_state_dotfiles_are_non_material_without_hiding_config() -> None:
    assert is_non_material_untracked_path(".habits.json") is True
    assert is_non_material_untracked_path(".todos.json") is True
    assert is_non_material_untracked_path(".local_state.json") is True
    assert is_non_material_untracked_path(".eslintrc.json") is False
    assert is_non_material_untracked_path(".prettierrc.json") is False
    assert is_non_material_untracked_path("src/.habits.json") is False


def test_check_scope_still_flags_unrelated_python_sibling_and_helper_files() -> None:
    changed = [
        "tests/test_team_labels.py",
        "tests/other_helper.py",
        "README.md",
        "src/unrelated.py",
    ]
    allowed = ["tests/test_team_labels.py"]
    ok, violations = check_scope(changed, allowed)
    assert ok is False
    assert violations == ["tests/other_helper.py", "README.md", "src/unrelated.py"]


def test_check_scope_still_flags_unrelated_files_next_to_allowed_ancestor_directories() -> None:
    changed = [
        "src",
        "src/calcbox",
        "src/calcbox/core.py",
        "src/other.py",
    ]
    allowed = ["src/calcbox/core.py"]
    ok, violations = check_scope(changed, allowed)
    assert ok is False
    assert violations == ["src/other.py"]


def test_list_changed_files_including_untracked_parses_porcelain(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("alysis_code.task_scope.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        if args[-2:] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=" M src/a.py\n?? notes/todo.txt\nR  old.py -> new.py\n",
                stderr="",
            )
        if args[-4:] == ["ls-files", "--others", "--exclude-standard", "-z"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=b"notes/todo.txt\0",
                stderr=b"",
            )
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr("alysis_code.task_scope.run_capture", fake_run)
    assert list_changed_files_including_untracked(tmp_path) == [
        "src/a.py",
        "new.py",
        "notes/todo.txt",
    ]


def test_list_changed_files_including_untracked_uses_leaf_untracked_files_and_filters_egg_info(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("alysis_code.task_scope.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        if args[-2:] == ["status", "--porcelain"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=" M pyproject.toml\n?? src/\n?? tests/\n",
                stderr="",
            )
        if args[-4:] == ["ls-files", "--others", "--exclude-standard", "-z"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=(
                    b"src/calcbox/__init__.py\0"
                    b"src/calcbox/core.py\0"
                    b"src/calcbox.egg-info/PKG-INFO\0"
                    b"tests/test_core.py\0"
                ),
                stderr=b"",
            )
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr("alysis_code.task_scope.run_capture", fake_run)

    assert list_changed_files_including_untracked(tmp_path) == [
        "pyproject.toml",
        "src/calcbox/__init__.py",
        "src/calcbox/core.py",
        "tests/test_core.py",
    ]


def test_list_untracked_packaging_metadata_paths_returns_exact_leaf_files(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("alysis_code.task_scope.shutil.which", lambda _cmd: "/usr/bin/git")

    def fake_run(args, **_kwargs):  # type: ignore[no-untyped-def]
        if args[-4:] == ["ls-files", "--others", "--exclude-standard", "-z"]:
            return subprocess.CompletedProcess(
                args=args,
                returncode=0,
                stdout=(
                    b"src/calcbox.egg-info/PKG-INFO\0"
                    b"src/calcbox.egg-info/SOURCES.txt\0"
                    b"src/calcbox/core.py\0"
                ),
                stderr=b"",
            )
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr("alysis_code.task_scope.run_capture", fake_run)

    assert list_untracked_packaging_metadata_paths(tmp_path) == [
        "src/calcbox.egg-info/PKG-INFO",
        "src/calcbox.egg-info/SOURCES.txt",
    ]


def test_list_changed_files_including_untracked_returns_empty_on_git_timeout(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setattr("alysis_code.task_scope.shutil.which", lambda _cmd: "/usr/bin/git")
    seen_envs: list[dict[str, str]] = []
    seen_timeouts: list[float] = []

    def fake_run(args, **kwargs):  # type: ignore[no-untyped-def]
        seen_envs.append(dict(kwargs["env"]))
        seen_timeouts.append(float(kwargs["timeout"]))
        raise subprocess.TimeoutExpired(cmd=args, timeout=kwargs["timeout"])

    monkeypatch.setattr("alysis_code.task_scope.run_capture", fake_run)

    assert list_changed_files_including_untracked(tmp_path) == []
    assert seen_timeouts
    assert all(timeout == 5.0 for timeout in seen_timeouts)
    assert all(env["GIT_TERMINAL_PROMPT"] == "0" for env in seen_envs)
    assert all(env["GIT_ASKPASS"] == "" for env in seen_envs)
    assert all(env["SSH_ASKPASS"] == "" for env in seen_envs)


def test_inspect_workspace_git_diff_distinguishes_clean_and_untracked_source(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test User"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
        capture_output=True,
    )
    tracked = tmp_path / "src" / "app.py"
    tracked.parent.mkdir(parents=True)
    tracked.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "src/app.py"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )

    clean = inspect_workspace_git_diff(tmp_path)
    assert clean.available is True
    assert clean.empty is True

    (tmp_path / "pytest_output.txt").write_text("diagnostic\n", encoding="utf-8")
    (tmp_path / ".coverage").write_text("generated\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("prose only\n", encoding="utf-8")
    scratch_only = inspect_workspace_git_diff(tmp_path)
    assert scratch_only.empty is True

    untracked = tmp_path / "tests" / "test_app.py"
    untracked.parent.mkdir(parents=True)
    untracked.write_text("def test_app():\n    assert True\n", encoding="utf-8")
    with_source = inspect_workspace_git_diff(tmp_path)
    assert with_source.empty is False
    assert with_source.untracked_source_paths == ("tests/test_app.py",)

    tracked.write_text("VALUE = 2\n", encoding="utf-8")
    with_tracked_diff = inspect_workspace_git_diff(tmp_path)
    assert with_tracked_diff.tracked_diff_paths == ("src/app.py",)


def test_untracked_source_classifier_excludes_prose_and_runtime_output() -> None:
    assert is_untracked_source_path("src/app.py") is True
    assert is_untracked_source_path("tests/cases/example.rst") is True
    assert is_untracked_source_path("pyproject.toml") is True
    assert is_untracked_source_path("notes.md") is False
    assert is_untracked_source_path(".coverage") is False
    assert is_untracked_source_path("pytest_output.txt") is False


def test_existing_test_layout_path_matches_supported_repo_conventions() -> None:
    assert is_existing_test_layout_path("tests/migrations/test_loader.py") is True
    assert is_existing_test_layout_path("pkg/testing/cases/data.json") is True
    assert is_existing_test_layout_path("test_parser.py") is True
    assert is_existing_test_layout_path("pkg/parser_test.py") is True
    assert is_existing_test_layout_path("pkg/parser.py") is False
    assert is_existing_test_layout_path("docs/test_notes.rst") is False


def test_inspect_existing_test_edits_excludes_new_tests_and_source_edits(
    tmp_path: Path,
) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.name", "Test User"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "config", "user.email", "test@example.com"],
        check=True,
    )
    source = tmp_path / "src" / "app.py"
    existing_test = tmp_path / "tests" / "test_app.py"
    source.parent.mkdir(parents=True)
    existing_test.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n", encoding="utf-8")
    existing_test.write_text("def test_app():\n    assert VALUE == 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "initial"],
        check=True,
    )
    base_ref = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    source.write_text("VALUE = 2\n", encoding="utf-8")
    existing_test.write_text("def test_app():\n    assert VALUE == 2\n", encoding="utf-8")
    new_test = tmp_path / "tests" / "test_regression.py"
    new_test.write_text("def test_regression():\n    assert True\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "tests/test_regression.py"],
        check=True,
    )

    state = inspect_existing_test_edits(tmp_path)

    assert state.available is True
    assert state.paths == ("tests/test_app.py",)
    assert state.edits[0].added_lines == 1
    assert state.edits[0].deleted_lines == 1

    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-qm", "agent work"],
        check=True,
    )
    assert inspect_existing_test_edits(tmp_path).has_edits is False
    assert inspect_existing_test_edits(tmp_path, base_ref=base_ref).paths == ("tests/test_app.py",)
    assert (
        restore_existing_test_paths(
            tmp_path,
            base_ref=base_ref,
            paths=["tests/test_app.py"],
        )
        is True
    )
    assert inspect_existing_test_edits(tmp_path, base_ref=base_ref).has_edits is False
    assert existing_test.read_text(encoding="utf-8") == ("def test_app():\n    assert VALUE == 1\n")


def test_forbidden_parser_does_not_treat_preserve_behavior_as_path_ban() -> None:
    assert extract_forbidden_repo_path_hints("preserve unknown fields in migrate_user.py") == []
    assert (
        extract_forbidden_repo_path_hints("preserve backward compatibility in config_loader.py")
        == []
    )
    assert extract_forbidden_repo_path_hints("keep existing behavior while editing parser.py") == []
    assert extract_forbidden_repo_path_hints("maintain comments in src/Foo.java") == []


def test_forbidden_parser_extracts_direct_path_instructions_with_evidence() -> None:
    records = extract_forbidden_repo_path_hint_records(
        "Do not edit services/worker/app/settings.py. Leave README.md unchanged."
    )

    assert [item.path for item in records] == [
        "services/worker/app/settings.py",
        "README.md",
    ]
    assert records[0].reason_code == "direct_path_forbidden_instruction"
    assert "Do not edit" in records[0].evidence
    assert records[1].reason_code in {
        "direct_path_forbidden_instruction",
        "path_must_remain_unchanged",
    }
    assert extract_forbidden_repo_path_hints("Do not touch package-lock.json") == [
        "package-lock.json"
    ]
    assert extract_forbidden_repo_path_hints("Exclude docs/generated.md") == ["docs/generated.md"]


def test_cargo_manifest_scope_allows_cargo_lock_companion(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text(
        '[package]\nname = "demo"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (tmp_path / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
    task = {
        "title": "Update Rust dependencies",
        "description": "Edit Cargo.toml.",
        "write_scope": ["Cargo.toml"],
        "estimated_files": ["Cargo.toml"],
    }

    allowed = normalize_scope_patterns(task, root=tmp_path)
    assessment = assess_scope_changes(
        ["Cargo.toml", "Cargo.lock"],
        allowed,
        task=task,
        root=tmp_path,
    )

    assert "Cargo.lock" in allowed
    assert assessment.ok is True
    assert any(
        item.classification == SCOPE_CLASS_EXPECTED_COMPANION and item.path == "Cargo.lock"
        for item in assessment.diagnostics
    )


def test_node_package_scope_allows_detected_lockfile_only(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"packageManager":"pnpm@9.0.0"}\n', encoding="utf-8")
    (tmp_path / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")

    assert companion_generated_paths_for("package.json", root=tmp_path) == ["pnpm-lock.yaml"]

    task = {
        "title": "Update package metadata",
        "description": "Edit package.json.",
        "write_scope": ["package.json"],
        "estimated_files": ["package.json"],
    }
    allowed = normalize_scope_patterns(task, root=tmp_path)

    assert "pnpm-lock.yaml" in allowed
    assert "package-lock.json" not in allowed
    assert assess_scope_changes(["pnpm-lock.yaml"], allowed, task=task, root=tmp_path).ok
    blocked = assess_scope_changes(["package-lock.json"], allowed, task=task, root=tmp_path)
    assert blocked.ok is False
    assert blocked.blocking_paths == ["package-lock.json"]


def test_unrelated_lockfile_edit_remains_blocked(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text('{"packageManager":"npm@10.0.0"}\n', encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}\n", encoding="utf-8")
    (tmp_path / "other").mkdir()
    (tmp_path / "other" / "package-lock.json").write_text("{}\n", encoding="utf-8")
    task = {"write_scope": ["package.json"], "estimated_files": ["package.json"]}
    allowed = normalize_scope_patterns(task, root=tmp_path)

    assessment = assess_scope_changes(
        ["other/package-lock.json"],
        allowed,
        task=task,
        root=tmp_path,
    )

    assert assessment.ok is False
    assert assessment.diagnostics[0].classification == SCOPE_CLASS_DANGEROUS_UNRELATED


def test_known_root_scratch_artifacts_are_moved_to_artifacts(tmp_path: Path) -> None:
    subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
    (tmp_path / "_test_dump.txt").write_text("debug\n", encoding="utf-8")
    (tmp_path / "test_lines_dump.txt").write_text("lines\n", encoding="utf-8")
    artifact_dir = tmp_path / ".alysis" / "runs" / "run_1" / "execution" / "scratch" / "T01"

    diagnostics = relocate_known_scratch_artifacts(root=tmp_path, artifact_dir=artifact_dir)

    assert sorted(item.path for item in diagnostics) == ["_test_dump.txt", "test_lines_dump.txt"]
    assert not (tmp_path / "_test_dump.txt").exists()
    assert not (tmp_path / "test_lines_dump.txt").exists()
    assert (artifact_dir / "_test_dump.txt").read_text(encoding="utf-8") == "debug\n"
    assert all(item.allowed for item in diagnostics)


def test_scope_assessment_classifies_missing_scope_and_forbidden_paths(tmp_path: Path) -> None:
    task = {
        "title": "Fix src/app.py",
        "description": "Do not edit secrets.txt.",
        "write_scope": ["src/app.py"],
        "estimated_files": ["src/app.py"],
    }
    assessment = assess_scope_changes(
        ["src/helper.py", "secrets.txt"],
        ["src/app.py"],
        task=task,
        root=tmp_path,
    )

    assert assessment.ok is False
    by_path = {item.path: item for item in assessment.diagnostics}
    assert by_path["src/helper.py"].classification == SCOPE_CLASS_LIKELY_MISSING_SCOPE
    assert by_path["src/helper.py"].recommended_action == "create_scope_delta_proposal"
    assert by_path["secrets.txt"].classification == SCOPE_CLASS_FORBIDDEN
    assert by_path["secrets.txt"].recommended_action == "reject_hard"
