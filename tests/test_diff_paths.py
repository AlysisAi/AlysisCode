"""Git C-quoted patch paths must round-trip to the actual working-tree names."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from alysis_code.diff_paths import (
    iter_patch_paths,
    parse_diff_git_line,
    parse_patch_changed_files,
)


@pytest.mark.parametrize(
    ("quoted", "actual"),
    [
        (r"caf\303\251.txt", "café.txt"),
        (r"\360\237\223\246.txt", "📦.txt"),
        (r"two\tlines.txt", "two\tlines.txt"),
        (r"back\\slash.txt", r"back\slash.txt"),
        (r'quote\"name.txt', 'quote"name.txt'),
        (r"a\nnewline.txt", "a\nnewline.txt"),
    ],
)
def test_git_c_quoted_diff_header_round_trips(quoted: str, actual: str) -> None:
    patch = f'diff --git "a/{quoted}" "b/{quoted}"\n'
    assert parse_diff_git_line(patch.rstrip("\n")) == (actual, actual)
    assert iter_patch_paths(patch) == [actual]
    assert parse_patch_changed_files(patch) == [actual]


@pytest.mark.parametrize(
    ("quoted", "actual"),
    [
        (r"caf\303\251.txt", "café.txt"),
        (r"two\tlines.txt", "two\tlines.txt"),
        (r"back\\slash.txt", r"back\slash.txt"),
    ],
)
def test_git_c_quoted_legacy_headers_round_trip(quoted: str, actual: str) -> None:
    patch = f'--- /dev/null\n+++ "b/{quoted}"\n'
    assert iter_patch_paths(patch) == [actual]
    assert parse_patch_changed_files(patch) == [actual]


def test_unquoted_diff_with_spaces_is_unchanged() -> None:
    patch = "diff --git a/path with spaces.txt b/path with spaces.txt\n"
    assert iter_patch_paths(patch) == ["path with spaces.txt"]
    assert parse_patch_changed_files(patch) == ["path with spaces.txt"]


def test_quoted_diff_with_spaces_is_unchanged() -> None:
    patch = 'diff --git "a/path with spaces.txt" "b/path with spaces.txt"\n'
    assert iter_patch_paths(patch) == ["path with spaces.txt"]
    assert parse_patch_changed_files(patch) == ["path with spaces.txt"]


@pytest.mark.skipif(
    os.name == "nt" or shutil.which("git") is None,
    reason="Needs Git and POSIX filenames containing backslashes/control characters",
)
def test_real_git_quoted_diff_paths_round_trip(tmp_path: Path) -> None:
    names = ["café.txt", "📦.txt", "two\tlines.txt", r"back\slash.txt", 'quote"name.txt']
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    for name in names:
        (tmp_path / name).write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    patch = subprocess.run(
        ["git", "-c", "core.quotePath=true", "diff", "--cached", "--no-color"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert set(iter_patch_paths(patch)) == set(names)
    assert set(parse_patch_changed_files(patch)) == set(names)


@pytest.mark.parametrize(
    "path",
    [
        "some b/f.txt",
        "some b/name b/file.txt",
        "path/file b/2.txt",
        "x b/y",
        "z b/file named b/name.txt",
    ],
)
@pytest.mark.parametrize("quoted", [False, True])
def test_unquoted_b_segment_path_is_not_split_at_directory_component(
    path: str, quoted: bool, tmp_path: Path
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    filename = tmp_path / path
    filename.parent.mkdir(parents=True, exist_ok=True)
    filename.write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    diff = subprocess.run(
        ["git", "-c", f"core.quotePath={str(quoted).lower()}", "diff", "--cached"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert iter_patch_paths(diff) == [path]
    assert parse_patch_changed_files(diff) == [path]
    assert parse_diff_git_line(diff.splitlines()[0]) == (path, path)


@pytest.mark.parametrize(
    ("old", "new", "change_content"),
    [
        ("src b/foo.txt", "dst b/bar.txt", False),
        ("src b/foo.txt", "dst b/bar.txt", True),
        ("src b/foo.txt", "dst b/foo.txt", False),
        ("foo b/foo", "foo b/foo renamed", False),
        ("a/inside b/file.txt", "b/different b/file.txt", True),
    ],
)
def test_real_git_rename_in_b_directory_keeps_both_actual_paths(
    tmp_path: Path, old: str, new: str, change_content: bool
) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout

    git("init", "-q")
    source = tmp_path / old
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("line1\nline2\nline3\n", encoding="utf-8")
    git("add", "-A")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    target = tmp_path / new
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    if change_content:
        target.write_text("line1\nline2\nline3\nline4\n", encoding="utf-8")
    git("add", "-A")
    patch = git("diff", "--cached", "-M", "--no-color")
    assert iter_patch_paths(patch) == [old, new]
    assert parse_patch_changed_files(patch) == [new]


def test_ambiguous_rename_without_metadata_fails_closed() -> None:
    patch = "diff --git a/src b/old.txt b/dst b/new.txt\n"
    assert parse_diff_git_line(patch.rstrip("\n")) == (None, None)
    with pytest.raises(ValueError, match="Ambiguous unquoted"):
        iter_patch_paths(patch)
    with pytest.raises(ValueError, match="Ambiguous unquoted"):
        parse_patch_changed_files(patch)


def test_conflicting_metadata_fails_closed() -> None:
    patch = (
        "diff --git a/src b/old.txt b/dst b/new.txt\n"
        "rename from unrelated\n"
        "rename to dst b/new.txt\n"
    )
    with pytest.raises(ValueError, match="metadata does not match"):
        iter_patch_paths(patch)


def test_ambiguous_second_patch_fails_instead_of_silently_skipping() -> None:
    patch = (
        "diff --git a/normal b/normal\n"
        "diff --git a/src b/old.txt b/dst b/new.txt\n"
    )
    with pytest.raises(ValueError, match="Ambiguous unquoted"):
        iter_patch_paths(patch)


@pytest.mark.parametrize(
    "name", [" space.txt", "space.txt ", " space.txt ", "root b/ folder "]
)
@pytest.mark.parametrize("quoted", [False, True])
def test_real_git_paths_preserve_leading_and_trailing_spaces(
    tmp_path: Path, name: str, quoted: bool
) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    path = tmp_path / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("content\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    patch = subprocess.run(
        ["git", "-c", f"core.quotePath={str(quoted).lower()}", "diff", "--cached"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert iter_patch_paths(patch) == [name]
    assert parse_patch_changed_files(patch) == [name]


def test_real_git_copy_in_b_directory_preserves_source_and_destination(tmp_path: Path) -> None:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", *args], cwd=tmp_path, check=True, capture_output=True, text=True
        ).stdout

    git("init", "-q")
    source = tmp_path / "source b/name.txt"
    source.parent.mkdir(parents=True)
    source.write_text("first\nsecond\nthird\n", encoding="utf-8")
    git("add", "-A")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "base")
    target = tmp_path / "dest b/file.txt"
    target.parent.mkdir(parents=True)
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    git("add", "-A")
    patch = git("diff", "--cached", "-C", "--find-copies-harder")
    assert iter_patch_paths(patch) == ["source b/name.txt", "dest b/file.txt"]
    assert parse_patch_changed_files(patch) == ["dest b/file.txt"]
