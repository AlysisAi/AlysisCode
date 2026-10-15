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
