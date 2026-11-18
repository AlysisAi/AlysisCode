from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from alysis_code.runtime_artifacts import is_runtime_artifact_path
from alysis_code.task_scope import (
    extract_repo_path_hints,
    inspect_existing_test_edits,
    inspect_workspace_git_diff,
    is_existing_test_layout_path,
    is_non_material_untracked_path,
    is_untracked_source_path,
    list_untracked_entries,
    normalize_repo_path_entry,
    resolve_workspace_git_base,
    restore_existing_test_paths,
    scope_path_matches_pattern,
)


def test_normalize_repo_path_entry_rejects_prose_and_accepts_repo_paths() -> None:
    assert normalize_repo_path_entry("index.html") == "index.html"
    assert normalize_repo_path_entry("docs/") == "docs/**"
    assert normalize_repo_path_entry("Add responsive nav in index.html") is None
    assert normalize_repo_path_entry("/tmp/absolute.txt") is None


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


def test_scope_path_matches_globstar_direct_child_and_nested_paths() -> None:
    assert scope_path_matches_pattern("tests/test_coupon.py", "tests/**/*.py") is True
    assert scope_path_matches_pattern("tests/unit/test_coupon.py", "tests/**/*.py") is True
    assert scope_path_matches_pattern("tests/data/coupon.json", "tests/**/*.py") is False


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


def _git_repo_with_readme(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "fixture@example.invalid"],
        ["config", "user.name", "fixture"],
    ):
        subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    (root / ".gitignore").write_text("out/\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-q", "-m", "fixture"], check=True)
    return root


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_document_or_data_file_written_this_turn_is_a_change(tmp_path: Path) -> None:
    root = _git_repo_with_readme(tmp_path)
    (root / "notes").mkdir()
    (root / "notes" / "summary.md").write_text("- one\n", encoding="utf-8")
    (root / "out").mkdir()
    (root / "out" / "result.txt").write_text("ALPHA\n", encoding="utf-8")  # ignored by Git
    (root / "chart.png").write_bytes(b"\x89PNG")

    # Not written by this turn: prose and data keep the source-only rule.
    assert inspect_workspace_git_diff(root).empty

    state = inspect_workspace_git_diff(
        root, turn_touched_paths={"notes/summary.md", "out/result.txt", "chart.png"}
    )
    assert not state.empty
    assert state.untracked_turn_paths == ("chart.png", "notes/summary.md", "out/result.txt")
    assert state.to_payload()["untracked_turn_paths"] == list(state.untracked_turn_paths)


def _commit_all(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "-q",
            "-m",
            message,
        ],
        check=True,
        capture_output=True,
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
@pytest.mark.parametrize("start", ["no repository", "repository without a commit"])
def test_work_committed_where_there_was_no_commit_is_a_change(tmp_path: Path, start: str) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "README.md").write_text("# demo\n", encoding="utf-8")
    if start == "repository without a commit":
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
    base = resolve_workspace_git_base(root)
    assert base == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # Git's empty tree
    # Before any repository exists there is nothing to inspect.
    if start == "no repository":
        assert not inspect_workspace_git_diff(root, base_ref=base).available
        subprocess.run(["git", "-C", str(root), "init", "-q"], check=True, capture_output=True)
    (root / "notes.md").write_text("- one\n", encoding="utf-8")
    _commit_all(root, "first")

    # Everything is committed: against the commit there is no difference left,
    # against where the turn began there is.
    assert inspect_workspace_git_diff(root).empty
    state = inspect_workspace_git_diff(root, base_ref=base)
    assert not state.empty
    assert state.tracked_diff_paths == ("README.md", "notes.md")


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_work_committed_on_top_of_a_commit_keeps_that_commit_as_the_base(tmp_path: Path) -> None:
    root = _git_repo_with_readme(tmp_path)
    base = resolve_workspace_git_base(root)
    assert base and base != "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
    assert inspect_workspace_git_diff(root, base_ref=base).empty
    (root / "notes.md").write_text("- one\n", encoding="utf-8")
    _commit_all(root, "second")
    assert inspect_workspace_git_diff(root, base_ref=base).tracked_diff_paths == ("notes.md",)


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_a_head_that_cannot_be_read_has_no_base_rather_than_an_empty_one(tmp_path: Path) -> None:
    """Only a branch without a commit has nothing behind it."""
    root = _git_repo_with_readme(tmp_path)
    (root / ".git" / "HEAD").write_text("0" * 39 + "1\n", encoding="utf-8")  # no such commit
    assert resolve_workspace_git_base(root) is None


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_untracked_entries_are_listed_as_git_groups_them(tmp_path: Path) -> None:
    root = _git_repo_with_readme(tmp_path)  # ignores out/
    (root / ".gitignore").write_text("out/\n*.log\n", encoding="utf-8")
    (root / "out" / "deep").mkdir(parents=True)
    (root / "out" / "deep" / "a.bin").write_bytes(b"x")
    (root / "run.log").write_text("x", encoding="utf-8")
    (root / "src").mkdir()
    (root / "src" / "new.py").write_text("x = 1\n", encoding="utf-8")
    (root / "src" / "debug.log").write_text("x", encoding="utf-8")
    (root / "empty").mkdir()

    listed = list_untracked_entries(root)
    # A folder that is ignored whole is one entry; Git lists no empty folder.
    assert set(listed) == {"out", "run.log", "src/new.py", "src/debug.log"}
    assert "README.md" not in listed and "empty" not in listed
    assert list_untracked_entries(tmp_path / "no-such-folder") == ()
    assert list_untracked_entries(tmp_path) == ()  # not a repository

    # At the end of a turn, the ones that are gone are the change.
    shutil.rmtree(root / "out")
    (root / "src" / "debug.log").unlink()
    state = inspect_workspace_git_diff(root, present_before_paths=listed)
    assert state.removed_turn_paths == ("out", "src/debug.log")


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_an_entry_the_engine_reserved_itself_is_not_listed(tmp_path: Path) -> None:
    """The Windows sandbox makes empty entries for protected names and takes them back."""
    from alysis_code.sandbox_bookkeeping import note_reservation_file

    root = _git_repo_with_readme(tmp_path)
    reserved, written = root / "AGENTS.md", root / "CLAUDE.md"
    reserved.write_bytes(b"")
    written.write_bytes(b"")
    (root / "notes.txt").write_text("kept\n")
    note_reservation_file(reserved, reserved.stat(), removed=False)
    note_reservation_file(written, written.stat(), removed=False)
    # Written to since it was reserved: another file to this check, so it is listed.
    written.write_text("rules\n")

    assert sorted(list_untracked_entries(root)) == ["CLAUDE.md", "notes.txt"]


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_removed_file_git_does_not_track_is_a_change(tmp_path: Path) -> None:
    root = _git_repo_with_readme(tmp_path)
    (root / "out").mkdir()
    (root / "out" / "kept.bin").write_bytes(b"x")  # ignored by Git, still there
    (root / "kept").mkdir()
    removed = {
        "out/old.bin",  # ignored by Git and gone
        "debug.log",  # root scratch: clearing it out is the work when asked
        "__pycache__/a.pyc",
        "gone-folder",
        "out/kept.bin",  # still there
        "kept",
        "../outside.txt",
        "/etc/hosts",
    }
    assert inspect_workspace_git_diff(root).empty
    state = inspect_workspace_git_diff(root, present_before_paths=removed)
    assert not state.empty
    assert state.removed_turn_paths == (
        "__pycache__/a.pyc",
        "debug.log",
        "gone-folder",
        "out/old.bin",
    )
    assert state.to_payload()["removed_turn_paths"] == list(state.removed_turn_paths)
    assert state.changed_paths == state.removed_turn_paths


@pytest.mark.skipif(shutil.which("git") is None, reason="requires git")
def test_touched_paths_that_left_no_change_do_not_count(tmp_path: Path) -> None:
    root = _git_repo_with_readme(tmp_path)
    (root / "debug.log").write_text("scratch\n", encoding="utf-8")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "a.pyc").write_bytes(b"x")
    (tmp_path / "outside.txt").write_text("x\n", encoding="utf-8")
    (root / "link.txt").symlink_to(tmp_path / "outside.txt")
    state = inspect_workspace_git_diff(
        root,
        turn_touched_paths={
            "README.md",  # tracked and unchanged
            "deleted-helper.txt",  # written, then removed
            "debug.log",  # root scratch
            "__pycache__/a.pyc",  # runtime output
            "../outside.txt",  # not in the workspace
            "/etc/hosts",
            "link.txt",  # a link is not a file the turn produced here
        },
    )
    assert state.empty and state.untracked_turn_paths == ()
