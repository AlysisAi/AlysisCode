from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

from alysis_code.tools import fs as fs_mod
from alysis_code.tools.fs import FsError, fs_list
from alysis_code.tools.registry import iter_builtin_tool_metadata


def _write(root: Path, name: str) -> None:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("source\n", encoding="utf-8")


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _paths(result: dict[str, Any]) -> list[str]:
    return [entry["path"] for entry in result["entries"]]


def test_shallow_listing_reports_files_only_scope_without_implying_nested_absence(
    tmp_path: Path,
) -> None:
    for name in ["指南.md", ".κρυφό", "πηγή/χειριστής.py", "应用/路由.py"]:
        _write(tmp_path, name)

    result = fs_list(root=tmp_path, globs=["*", ".*"])

    assert set(_paths(result)) == {"指南.md", ".κρυφό"}
    assert result["returned_count"] == 2
    assert result["truncated"] is False
    assert result["scope"]["globs"] == ["*", ".*"]
    assert result["scope"]["entry_kind"] == "files"
    assert "matching files after these exclusions" in result["scope"]["completeness"]
    assert "does not establish repository-wide absence" in result["scope"]["completeness"]
    assert result["scope"]["gitignore"]["git_repository_resolved"] is False
    assert result["scope"]["gitignore"]["evidence_sources"] == []


@pytest.mark.parametrize("globs", [None, [], ["**/*"]])
def test_recursive_default_finds_nested_unicode_files_and_omits_directories(
    tmp_path: Path, globs: list[str] | None
) -> None:
    expected = {"指南.md", ".κρυφό", "πηγή/χειριστής.py", "应用/路由.py"}
    for name in expected:
        _write(tmp_path, name)
    (tmp_path / "empty-directory").mkdir()

    result = fs_list(root=tmp_path, globs=globs)

    assert set(_paths(result)) == expected
    assert result["scope"]["globs"] == ["**/*"]
    assert result["truncated"] is False


@pytest.mark.parametrize(("limit", "truncated"), [(1, True), (2, False), (3, False)])
def test_overlapping_patterns_count_unique_paths_before_the_result_limit(
    tmp_path: Path, limit: int, truncated: bool
) -> None:
    _write(tmp_path, "a.py")
    _write(tmp_path, "b.py")
    result = fs_list(root=tmp_path, globs=["a.py", "a.py", "b.py", "*.py"], max_results=limit)
    assert _paths(result) == ["a.py", "b.py"][:limit]
    assert result["returned_count"] == min(2, limit)
    assert result["max_results"] == limit
    assert result["truncated"] is truncated


def test_overlapping_patterns_across_batches_do_not_consume_the_visible_file_budget(
    tmp_path: Path,
) -> None:
    expected = {f"file-{index:03d}.py" for index in range(180)}
    for name in expected:
        _write(tmp_path, name)
    result = fs_list(root=tmp_path, globs=["*.py", "**/*.py"], max_results=200)
    assert set(_paths(result)) == expected
    assert result["returned_count"] == 180
    assert result["truncated"] is False


def test_scope_exposes_exact_ignore_components_and_fixed_exclusions(tmp_path: Path) -> None:
    for name in ["src/keep.py", "cache/drop.py", "src/build/drop.py"]:
        _write(tmp_path, name)
    result = fs_list(root=tmp_path, ignore=["cache"])
    assert _paths(result) == ["src/keep.py"]
    assert result["scope"]["ignored_path_components"] == ["cache"]
    assert "build" in result["scope"]["default_ignored_path_components"]

    # The existing API takes component names, not ignore globs. Its returned
    # scope and tool schema must make this distinction explicit.
    glob_ignore = fs_list(root=tmp_path, ignore=["cache/**"])
    assert set(_paths(glob_ignore)) == {"src/keep.py", "cache/drop.py"}
    assert glob_ignore["scope"]["ignored_path_components"] == ["cache/**"]


def test_plain_listing_resolves_visible_paths_and_stops_after_the_first_extra_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    names = ["build/ignored.txt", "first.txt", "extra.txt", "later.txt"]
    for name in names:
        _write(tmp_path, name)
    resolved = []
    original = Path.resolve

    def observe(path, *args, **kwargs):
        if path.suffix == ".txt":
            resolved.append(path.name)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", observe)
    result = fs_list(root=tmp_path, globs=names, max_results=1)
    assert _paths(result) == ["first.txt"]
    assert result["truncated"] is True
    assert resolved == ["first.txt", "extra.txt"]


def test_authoritative_git_negative_does_not_fall_back_to_fnmatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("πηγή/*.py\n", encoding="utf-8")
    _write(tmp_path, "πηγή/deep/α.py")

    def forbid_fallback(*_args: Any) -> set[str]:
        pytest.fail("definitive Git evidence must not invoke the approximate matcher")

    monkeypatch.setattr(fs_mod, "_fallback_gitignore_ignored_untracked", forbid_fallback)
    result = fs_list(root=tmp_path, globs=["**/*.py"])
    assert _paths(result) == ["πηγή/deep/α.py"]
    assert result["scope"]["gitignore"]["evidence_sources"] == ["git_check_ignore"]
    assert "limitation" not in result["scope"]["gitignore"]


@pytest.mark.parametrize(
    "name",
    [
        "κατάλογος/με κενό.py",
        pytest.param(
            'κατάλογος/με"εισαγωγικά.py',
            marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids quote filenames"),
        ),
        pytest.param(
            "κατάλογος/με\nαλλαγή.py",
            marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids newline filenames"),
        ),
        pytest.param(
            "κατάλογος/με\rεπιστροφή.py",
            marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids CR filenames"),
        ),
        pytest.param(
            "κατάλογος/με\r\nαλλαγή.py",
            marks=pytest.mark.skipif(os.name == "nt", reason="Windows forbids CRLF filenames"),
        ),
    ],
)
def test_authoritative_git_ignores_unicode_and_whitespace_paths_but_keeps_tracked_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("*.py\n", encoding="utf-8")
    _write(tmp_path, name)
    _write(tmp_path, "κατάλογος/γνωστό.py")
    _git(tmp_path, "add", "-f", "κατάλογος/γνωστό.py")

    def forbid_fallback(*_args: Any) -> set[str]:
        pytest.fail("definitive Git evidence must not invoke the approximate matcher")

    monkeypatch.setattr(fs_mod, "_fallback_gitignore_ignored_untracked", forbid_fallback)
    result = fs_list(root=tmp_path, globs=["**/*.py"])
    assert _paths(result) == ["κατάλογος/γνωστό.py"]
    assert result["scope"]["gitignore"]["evidence_sources"] == ["git_check_ignore"]
    assert result["truncated"] is False


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX filename bytes")
@pytest.mark.parametrize("name", ["tracked\r.txt", "tracked\r\n.txt"])
def test_fallback_preserves_git_tracked_path_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("*.txt\n", encoding="utf-8")
    _write(tmp_path, name)
    _write(tmp_path, "untracked.txt")
    _git(tmp_path, "add", "-f", name)
    real_run = fs_mod.subprocess.run

    def unavailable(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "check-ignore" in args:
            return subprocess.CompletedProcess(args, 128, stdout=b"", stderr=b"")
        return real_run(args, **kwargs)

    monkeypatch.setattr(fs_mod.subprocess, "run", unavailable)
    result = fs_list(root=tmp_path, globs=["*.txt"])
    assert _paths(result) == [name]
    assert result["scope"]["gitignore"]["evidence_sources"] == [
        "approximate_root_gitignore_fallback"
    ]


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX filename bytes")
def test_git_ignore_and_tracked_evidence_preserve_non_utf8_filenames(tmp_path: Path) -> None:
    untracked = os.fsdecode(b"untracked-\xff.txt")
    tracked = os.fsdecode(b"tracked-\xfe.txt")
    try:
        _write(tmp_path, untracked)
        _write(tmp_path, tracked)
    except (OSError, UnicodeError) as exc:
        pytest.skip(f"Test filesystem cannot create non-UTF-8 filenames: {type(exc).__name__}")
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("*.txt\n", encoding="utf-8")
    _git(tmp_path, "add", "-f", tracked)

    result = fs_list(root=tmp_path, globs=["*.txt"])
    assert _paths(result) == [tracked]
    assert fs_mod._fallback_gitignore_ignored_untracked(tmp_path, [untracked, tracked]) == {
        untracked
    }


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX surrogateescape encoding")
def test_git_path_protocol_round_trips_raw_bytes_without_newline_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire_names = [b"nested/name-\xff\r.txt", b"nested/name-\xfe\r\n.txt"]
    names = [os.fsdecode(name) for name in wire_names]
    wire = b"\0".join(wire_names) + b"\0"

    def git_response(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        assert not kwargs.get("text")
        assert not kwargs.get("encoding")
        if "check-ignore" in args:
            assert kwargs["input"] == wire
        else:
            assert args[-len(names) :] == names
        return subprocess.CompletedProcess(args, 0, stdout=wire, stderr=b"")

    monkeypatch.setattr(fs_mod.subprocess, "run", git_response)
    assert fs_mod._git_check_ignored(tmp_path, names) == set(names)
    assert fs_mod._git_tracked_paths(tmp_path, names) == set(names)


@pytest.mark.skipif(os.name != "posix", reason="requires POSIX control-character filenames")
def test_git_root_probe_preserves_repository_name_whitespace(tmp_path: Path) -> None:
    root = tmp_path / "repo\r\n "
    root.mkdir()
    _git(root, "init", "-q")
    (root / ".gitignore").write_text("*.txt\n", encoding="utf-8")
    _write(root, "ignored.txt")
    _write(root, "kept.py")

    assert fs_mod._git_repo_root(root, boundary=tmp_path) == root
    result = fs_list(root=root, globs=["*.txt", "*.py"])
    assert _paths(result) == ["kept.py"]
    assert result["scope"]["gitignore"]["evidence_sources"] == ["git_check_ignore"]


@pytest.mark.parametrize("failure", ["exit_error", "os_error", "timeout"])
def test_unavailable_git_evidence_uses_explicitly_limited_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    _git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text("*.txt\n", encoding="utf-8")
    _write(tmp_path, "untracked.txt")
    _write(tmp_path, "tracked.txt")
    _git(tmp_path, "add", "-f", "tracked.txt")
    real_run = fs_mod.subprocess.run

    def unavailable(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
        if "check-ignore" in args:
            if failure == "os_error":
                raise OSError("Git ignore query unavailable")
            if failure == "timeout":
                raise subprocess.TimeoutExpired(args, 1)
            return subprocess.CompletedProcess(args=args, returncode=128, stdout=b"", stderr=b"")
        return real_run(args, **kwargs)

    monkeypatch.setattr(fs_mod.subprocess, "run", unavailable)
    result = fs_list(root=tmp_path, globs=["*.txt"])
    assert _paths(result) == ["tracked.txt"]
    evidence = result["scope"]["gitignore"]
    assert evidence["evidence_sources"] == ["approximate_root_gitignore_fallback"]
    assert "approximate" in evidence["limitation"]
    assert "nested rules" in evidence["limitation"]


def test_listing_preserves_workspace_boundary_and_paths_relative_to_selected_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    _write(root, "πηγή/inside.py")
    _write(tmp_path, "outside.py")
    result = fs_list(root=root, root_path="πηγή")
    assert result["root"] == str(root / "πηγή")
    assert _paths(result) == ["inside.py"]
    with pytest.raises(FsError, match="escapes root"):
        fs_list(root=root, root_path="..")


def test_glob_traversal_stays_inside_selected_root_and_deduplicates_safe_aliases(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _write(root, "πηγή/inside.py")
    _write(root, "sibling.py")
    _write(tmp_path, "outside.py")

    result = fs_list(
        root=root,
        root_path="πηγή",
        globs=["../../*.py", "../*.py", "../πηγή/*.py", "*.py"],
        max_results=1,
    )
    assert _paths(result) == ["inside.py"]
    assert result["truncated"] is False
    assert result["scope"]["path_boundary"] == "resolved targets stay within root_path"


@pytest.mark.skipif(os.name == "nt", reason="requires unprivileged symlink creation")
def test_symlink_directory_globs_and_file_links_stay_inside_selected_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "workspace"
    _write(root, "πηγή/nested/inside.py")
    _write(root, "sibling/sibling.py")
    _write(tmp_path, "external/outside.py")
    selected = root / "πηγή"
    (selected / "internal").symlink_to(selected / "nested", target_is_directory=True)
    (selected / "sibling").symlink_to(root / "sibling", target_is_directory=True)
    (selected / "external").symlink_to(tmp_path / "external", target_is_directory=True)
    (selected / "inside-link.py").symlink_to(selected / "nested/inside.py")
    (selected / "outside-link.py").symlink_to(tmp_path / "external/outside.py")

    result = fs_list(root=root, root_path="πηγή", globs=["*/*.py", "*.py"])
    assert set(_paths(result)) == {"nested/inside.py", "internal/inside.py", "inside-link.py"}
    assert result["truncated"] is False
    with pytest.raises(FsError, match="escapes root"):
        fs_list(root=root, root_path="πηγή/external")


@pytest.mark.skipif(os.name == "nt", reason="requires unprivileged symlink creation")
def test_traversal_through_symlink_reports_the_actual_resolved_file(tmp_path: Path) -> None:
    _write(tmp_path, "nested/actual.py")
    _write(tmp_path, "lexical.py")
    (tmp_path / "nested/deeper").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "nested/deeper", target_is_directory=True)

    result = fs_list(root=tmp_path, globs=["link/../*.py", "nested/*.py"], max_results=1)
    assert _paths(result) == ["nested/actual.py"]
    assert result["truncated"] is False


def test_listing_schema_explains_scope_without_changing_permission_or_parameters() -> None:
    metadata = next(item for item in iter_builtin_tool_metadata() if item.name == "fs_list")
    assert metadata.categories == ("read", "fs")
    assert metadata.built_in_subagent_exposure == "readonly"
    assert set(metadata.parameters["properties"]) == {"root_path", "path_base", "globs", "ignore"}
    assert metadata.parameters["required"] == []
    assert "omitting directories" in metadata.description
    assert "Path.glob" in metadata.parameters["properties"]["globs"]["description"]
    assert "not glob patterns" in metadata.parameters["properties"]["ignore"]["description"]
