"""Real Git fixtures, inspected without invoking Git or changing any file."""

import hashlib
import os
import subprocess

import pytest

pytest.importorskip("dulwich")
from alysis_code.tools import git_readonly as reader
from alysis_code.tools.git import GitError


def git(root, *args):
    return subprocess.check_output(["git", "-C", str(root), *args], stderr=subprocess.PIPE).decode(
        "utf-8"
    )


def snapshot(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        if p.is_file()
        else None
        for p in root.rglob("*")
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    root = tmp_path / "Repository with spaces"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Fixture Author")
    git(root, "config", "user.email", "fixture@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    git(root, "config", "core.filemode", "false")
    return root


def seed(root):
    for name in ["notes.txt", "staged.txt", "delete.txt", "report[1].txt", "report1.txt", "HEAD"]:
        (root / name).write_bytes(b"original\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial commit")
    for name in ["report[1].txt", "report1.txt"]:
        (root / name).write_bytes(b"updated\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Προσθήκη δοκιμής")


def test_inspection_matches_git_without_subprocesses_or_file_changes(repo, monkeypatch):
    seed(repo)
    (repo / "staged.txt").write_bytes(b"original\nstaged change\n")
    git(repo, "add", "staged.txt")
    (repo / "notes.txt").write_bytes(b"edited notes\n")
    (repo / "delete.txt").unlink()
    (repo / "untracked.txt").write_bytes(b"untracked\n")
    (repo / "AGENTS.md").write_bytes(b"User-owned instructions\n")
    expected = git(repo, "--no-optional-locks", "status", "--porcelain=v1", "-b")
    head = git(repo, "rev-parse", "HEAD").strip()
    before = snapshot(repo)

    def reject(*a, **kw):
        raise AssertionError("Inspection must not launch programs")

    monkeypatch.setattr(subprocess, "Popen", reject)
    assert reader.git_status(root=repo)["status"] == expected
    assert "+edited notes" in reader.git_diff(root=repo)["diff"]
    assert "+staged change" in reader.git_diff(root=repo, staged=True)["diff"]
    log = reader.git_history(root=repo, mode="log", limit=1)
    assert log["commits"][0]["subject"] == "Προσθήκη δοκιμής"
    assert log["commits"][0]["commit"] == head
    assert log["truncated"]
    shown = reader.git_history(root=repo, mode="show", ref="HEAD", path="report[1].txt")
    assert "report[1].txt" in shown["patch_excerpt"]
    assert "report1.txt" not in shown["patch_excerpt"]
    blame = reader.git_history(root=repo, mode="blame", path="notes.txt", start_line=1, end_line=1)
    assert blame["lines"][0]["commit"] == "0" * 40
    assert snapshot(repo) == before


def test_unborn_repository_and_first_staged_diff_are_read_only(repo):
    (repo / "first.txt").write_bytes(b"first change\n")
    git(repo, "add", ".")
    before = snapshot(repo)
    assert reader.git_history(root=repo, mode="log")["commits"] == []
    assert "No commits yet on main" in reader.git_status(root=repo)["status"]
    assert "+first change" in reader.git_diff(root=repo, staged=True)["diff"]
    with pytest.raises(GitError, match="invalid revision"):
        reader.git_history(root=repo, mode="log", ref="missing")
    assert snapshot(repo) == before


def test_crlf_binary_and_diff_pagination(repo):
    seed(repo)
    git(repo, "config", "core.autocrlf", "true")
    (repo / "notes.txt").write_bytes(b"original\r\n")
    assert reader.git_diff(root=repo, path="notes.txt")["diff"] == ""
    (repo / "notes.txt").write_bytes(b"changed\0binary\r\n")
    assert "Binary files" in reader.git_diff(root=repo, path="notes.txt")["diff"]
    (repo / "notes.txt").write_text("long line\n" * 3000)
    page = reader.git_diff(root=repo)
    assert page["truncated"]
    next_page = reader.git_diff(root=repo, offset=page["next_offset"], diff_id=page["diff_id"])
    assert not next_page["truncated"]
    (repo / "notes.txt").write_text("different\n")
    with pytest.raises(GitError, match="diff changed"):
        reader.git_diff(root=repo, offset=page["next_offset"], diff_id=page["diff_id"])


def test_history_filters_ranges_and_clean_blame(repo):
    seed(repo)
    first = git(repo, "rev-parse", "HEAD~1").strip()
    assert reader.git_history(root=repo, mode="show", ref="HEAD~1")["commit"]["commit"] == first
    assert (
        len(reader.git_history(root=repo, mode="log", grep="Initial", author="Fixture")["commits"])
        == 1
    )
    blame = reader.git_history(root=repo, mode="blame", path="notes.txt", start_line=1, end_line=1)
    assert blame["lines"][0]["commit"] == first
    for ref in ["--all", "bad\0revision", "missing"]:
        with pytest.raises(GitError, match="invalid revision"):
            reader.git_history(root=repo, mode="show", ref=ref)
    with pytest.raises(GitError, match="escapes root"):
        reader.git_diff(root=repo, path="../outside")


def test_external_filter_is_refused_without_execution(repo, monkeypatch):
    seed(repo)
    (repo / ".gitattributes").write_text("notes.txt filter=evil\n")
    git(repo, "add", ".gitattributes")
    git(repo, "commit", "-m", "attributes")
    git(repo, "config", "filter.evil.clean", "would-create-a-file")

    def reject(*a, **kw):
        raise AssertionError("A filter was executed")

    monkeypatch.setattr(subprocess, "Popen", reject)
    before = snapshot(repo)
    with pytest.raises(GitError, match="git_content_filter_required"):
        reader.git_status(root=repo)
    assert reader.git_diff(root=repo, staged=True)["diff"] == ""
    assert snapshot(repo) == before


def test_conflict_status_is_reported_without_modifying_index(repo):
    seed(repo)
    git(repo, "checkout", "-b", "other")
    (repo / "notes.txt").write_bytes(b"other\n")
    git(repo, "commit", "-am", "other")
    git(repo, "checkout", "main")
    (repo / "notes.txt").write_bytes(b"main\n")
    git(repo, "commit", "-am", "main")
    subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True)
    before = snapshot(repo)
    assert "UU notes.txt" in reader.git_status(root=repo)["status"]
    assert "Unmerged path notes.txt" in reader.git_diff(root=repo)["diff"]
    assert snapshot(repo) == before


@pytest.mark.parametrize("unborn", [False, True])
def test_porcelain_v2_matches_git_without_writes(repo, unborn, monkeypatch):
    if not unborn:
        seed(repo)
        (repo / "notes.txt").write_bytes(b"dirty\n")
        (repo / "delete.txt").unlink()
    (repo / "staged-new.txt").write_bytes(b"staged\n")
    git(repo, "add", "staged-new.txt")
    (repo / "untracked.txt").write_bytes(b"untracked\n")
    expected = git(repo, "--no-optional-locks", "status", "--porcelain=v2", "--untracked-files=all")
    before = snapshot(repo)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("Reader launched a process")
    )
    actual = reader.git_status(
        root=repo, porcelain_v2=True, untracked_files="all", branch_header=False
    )["status"]
    assert actual == expected
    assert snapshot(repo) == before


def test_porcelain_v2_conflicts_match_git(repo):
    seed(repo)
    git(repo, "checkout", "-b", "other")
    (repo / "notes.txt").write_bytes(b"other\n")
    git(repo, "commit", "-am", "other")
    git(repo, "checkout", "main")
    (repo / "notes.txt").write_bytes(b"main\n")
    git(repo, "commit", "-am", "main")
    subprocess.run(["git", "-C", str(repo), "merge", "other"], capture_output=True)
    expected = git(repo, "--no-optional-locks", "status", "--porcelain=v2")
    before = snapshot(repo)
    assert (
        reader.git_status(root=repo, porcelain_v2=True, branch_header=False)["status"] == expected
    )
    assert snapshot(repo) == before


@pytest.mark.parametrize(
    "state", ["clean", "dirty", "committed", "uninitialized", "deleted", "replaced"]
)
def test_submodule_status_and_dirty_diff_are_readonly(repo, tmp_path, monkeypatch, state):
    seed(repo)
    source = tmp_path / "module source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "QA")
    git(source, "config", "user.email", "qa@example.invalid")
    (source / "module.txt").write_bytes(b"base\n")
    git(source, "add", ".")
    git(source, "commit", "-m", "module")
    git(repo, "-c", "protocol.file.allow=always", "submodule", "add", str(source), "module")
    git(repo, "commit", "-m", "submodule")
    module = repo / "module"
    if state in {"dirty", "committed"}:
        (module / "module.txt").write_bytes(b"dirty\n")
        if state == "dirty":
            (module / "untracked.txt").write_bytes(b"untracked\n")
        else:
            git(module, "config", "user.name", "QA")
            git(module, "config", "user.email", "qa@example.invalid")
            git(module, "commit", "-am", "module changed")
    elif state == "uninitialized":
        git(repo, "submodule", "deinit", "-f", "--", "module")
    elif state in {"deleted", "replaced"}:
        import shutil

        assert module.resolve().is_relative_to(repo.resolve())
        shutil.rmtree(module)
        if state == "replaced":
            module.write_bytes(b"replacement file\n")
    expected_v1 = git(repo, "--no-optional-locks", "status", "--porcelain=v1", "-b")
    expected_v2 = git(repo, "--no-optional-locks", "status", "--porcelain=v2")
    expected_diff = git(repo, "diff", "--no-ext-diff")
    before = snapshot(repo)
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("Reader launched a process")
    )
    assert reader.git_status(root=repo)["status"] == expected_v1
    assert (
        reader.git_status(root=repo, porcelain_v2=True, branch_header=False)["status"]
        == expected_v2
    )
    assert reader.git_diff(root=repo)["diff"] == expected_diff
    assert snapshot(repo) == before
