from __future__ import annotations

import os
import shlex
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from alysis_code import git_evidence
from alysis_code.agent import subagent_workspace
from alysis_code.agent.subagent_workspace import SubagentWorkspaceProvider
from alysis_code.session_store import SessionStore


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", os.fspath(root), "--no-optional-locks", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.fixture
def workspace(tmp_path: Path) -> Iterator[tuple[Path, SubagentWorkspaceProvider]]:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    # Byte-preservation assertions must not inherit a developer's CRLF policy.
    _git(root, "config", "core.autocrlf", "false")
    (root / "app.txt").write_text("base\n", encoding="utf-8")
    (root / "dirty.txt").write_text("clean\n", encoding="utf-8")
    (root / "deleted.txt").write_text("delete me\n", encoding="utf-8")
    (root / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    _git(root, "add", "--all")
    _git(
        root,
        "-c",
        "user.name=Alysis Tests",
        "-c",
        "user.email=tests@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--no-gpg-sign",
        "-qm",
        "initial",
    )
    store = SessionStore(
        enabled=True,
        sessions_dir=tmp_path / "sessions",
        session_id="parent",
        cwd=os.fspath(root),
        repo_root=os.fspath(root),
    )
    provider = SubagentWorkspaceProvider(root=root, store=store)
    try:
        yield root, provider
    finally:
        provider.close()
        store.close()


def _prepare(provider: SubagentWorkspaceProvider, run_id: str = "child") -> Path:
    result = provider.prepare(run_id)
    assert result["ok"], result
    return Path(result["worktree_path"])


def _filter_command(marker: Path) -> str:
    # A real external filter: invocation leaves evidence outside the workspace.
    # Fail immediately after recording invocation, including for the process
    # protocol, so an unguarded invocation cannot hang waiting for a handshake.
    return shlex.join(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "Path(sys.argv[1]).write_text('executed'); "
            "sys.exit(1)",
            os.fspath(marker),
        ]
    )


@pytest.mark.parametrize("kind", ["clean", "smudge", "process"])
@pytest.mark.parametrize(
    "attributes_source", ["working", "index", "index_non_racy", "info", "global"]
)
def test_prepare_refuses_external_filters_without_running_them(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    kind: str,
    attributes_source: str,
) -> None:
    root, provider = workspace
    driver = "Example.Roundtrip-v2"
    attributes = f"*.txt filter={driver}\n"
    if attributes_source in {"working", "index", "index_non_racy"}:
        path = root / ".gitattributes"
        path.write_text(attributes, encoding="utf-8")
        if attributes_source.startswith("index"):
            _git(root, "add", ".gitattributes")
            path.unlink()
    elif attributes_source == "info":
        (root / ".git" / "info" / "attributes").write_text(attributes, encoding="utf-8")
    else:
        path = root.parent / "global-attributes"
        path.write_text(attributes, encoding="utf-8")
        _git(root, "config", "core.attributesFile", os.fspath(path))
    marker = root.parent / "host-filter-ran"
    _git(root, "config", f"filter.{driver}.{kind}", _filter_command(marker))
    (root / "app.txt").write_text("parent changes\n", encoding="utf-8")
    if attributes_source == "index_non_racy":
        # Git can identify the changed file by size without hashing its content.
        # Avoid racy-index rehashes of unchanged files accidentally exercising the
        # filter guard before the snapshot switches to its separate HEAD index.
        index_path = root / ".git" / "index"
        metadata = index_path.stat()
        os.utime(index_path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 5_000_000_000))
    index = (root / ".git" / "index").read_bytes()
    head = _git(root, "rev-parse", "HEAD")

    result = provider.prepare("child")

    assert result["ok"] is False
    assert "external git filters" in result["error"].lower()
    assert not marker.exists()
    assert provider.get("child") is None
    assert not (provider.worktrees_root / "child").exists()
    assert (root / ".git" / "index").read_bytes() == index
    assert _git(root, "rev-parse", "HEAD") == head
    assert (root / "app.txt").read_text(encoding="utf-8") == "parent changes\n"


def test_unused_filters_and_overridden_attributes_preserve_normal_workspace_lifecycle(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    root, provider = workspace
    marker = root.parent / "host-filter-ran"
    _git(root, "config", "filter.AnyDriver.process", _filter_command(marker))
    (root / ".gitattributes").write_text(
        "*.txt filter=AnyDriver\n*.txt -filter\nignored/** filter=AnyDriver\n",
        encoding="utf-8",
    )
    (root / "ignored").mkdir()
    (root / "ignored" / "input.txt").write_text("ignored data\n", encoding="utf-8")

    child = _prepare(provider)
    (child / "app.txt").write_text("child changes\n", encoding="utf-8")
    assert provider.capture("child")["material_patch_complete"] is True
    assert provider.apply("child")["ok"] is True
    assert (root / "app.txt").read_text(encoding="utf-8") == "child changes\n"
    assert not child.exists()
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["clean", "process"])
def test_filters_introduced_by_child_cannot_run_during_capture_apply_or_cleanup(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    kind: str,
) -> None:
    root, provider = workspace
    child = _prepare(provider)
    output = child / "αποτελέσματα"
    output.mkdir()
    (output / ".gitattributes").write_text("* filter=Child.Driver\n", encoding="utf-8")
    payload = output / "binary output"
    payload.write_bytes(b"\x00valuable child output")
    marker = root.parent / "host-filter-ran"
    _git(child, "config", f"filter.Child.Driver.{kind}", _filter_command(marker))

    captured = provider.capture("child")
    assert captured["material_patch_complete"] is False
    assert captured["no_changes"] is False
    assert captured["patch_capture_status"] == "failed"
    assert provider.apply("child")["error_code"] == "incomplete_workspace_patch"
    assert provider.close()["retained_output_workspaces"][0]["run_id"] == "child"
    assert payload.read_bytes() == b"\x00valuable child output"
    assert not marker.exists()
    assert provider.release("child", action="discarded")["ok"] is True
    assert not child.exists()
    assert not marker.exists()


@pytest.mark.parametrize("kind", ["smudge", "process"])
def test_checkout_uses_child_conditional_config_and_cleans_up_on_filter_refusal(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    kind: str,
) -> None:
    root, provider = workspace
    # This driver does not exist in the parent. Only the new worktree's Git
    # directory activates it, so guarding the worktree-add parent is insufficient.
    (root / ".gitattributes").write_text("*.txt filter=Child.Only\n", encoding="utf-8")
    marker = root.parent / "host-filter-ran"
    config = root.parent / "child-config"
    _git(
        root,
        "config",
        "--file",
        os.fspath(config),
        f"filter.Child.Only.{kind}",
        _filter_command(marker),
    )
    gitdir_pattern = (root / ".git" / "worktrees").as_posix() + "/"
    _git(root, "config", f"includeIf.gitdir:{gitdir_pattern}.path", os.fspath(config))
    head = _git(root, "rev-parse", "HEAD")
    index = (root / ".git" / "index").read_bytes()

    result = provider.prepare("child")

    assert result["ok"] is False
    assert result["error_code"] == "workspace_prepare_failed"
    assert "external Git filters are disabled" in result["error"]
    assert not marker.exists()
    assert provider.get("child") is None
    assert not (provider.worktrees_root / "child").exists()
    assert _git(root, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert _git(root, "rev-parse", "HEAD") == head
    assert (root / ".git" / "index").read_bytes() == index


def test_dirty_snapshot_preserves_parent_index_head_and_working_files(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    root, provider = workspace
    (root / "dirty.txt").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "dirty.txt")
    (root / "dirty.txt").write_text("working\n", encoding="utf-8")
    (root / "deleted.txt").unlink()
    (root / "νέο.txt").write_text("parent untracked\n", encoding="utf-8")
    (root / "ignored").mkdir()
    (root / "ignored" / "cache").write_bytes(b"ignored")
    head = _git(root, "rev-parse", "HEAD")
    refs = _git(root, "show-ref")
    index = (root / ".git" / "index").read_bytes()
    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all")

    child = _prepare(provider)

    assert (child / "dirty.txt").read_text(encoding="utf-8") == "working\n"
    assert (child / "νέο.txt").read_text(encoding="utf-8") == "parent untracked\n"
    assert not (child / "deleted.txt").exists()
    assert not (child / "ignored").exists()
    assert _git(child, "status", "--porcelain=v1") == ""
    assert _git(root, "rev-parse", "HEAD") == head
    assert _git(root, "show-ref") == refs
    assert (root / ".git" / "index").read_bytes() == index
    assert _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all") == status
    assert (root / "dirty.txt").read_text(encoding="utf-8") == "working\n"
    assert not (root / "deleted.txt").exists()
    record = provider.get("child")
    assert record is not None
    assert record.parent_head_commit == head
    assert record.base_commit != head
    assert _git(child, "rev-parse", "HEAD^") == head


def test_child_delta_applies_over_dirty_baseline_without_restaging_parent(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    root, provider = workspace
    (root / "dirty.txt").write_text("staged\n", encoding="utf-8")
    _git(root, "add", "dirty.txt")
    (root / "dirty.txt").write_text("working\n", encoding="utf-8")
    (root / "deleted.txt").unlink()
    (root / "parent-new.txt").write_text("inherited\n", encoding="utf-8")
    head = _git(root, "rev-parse", "HEAD")
    index = (root / ".git" / "index").read_bytes()
    child = _prepare(provider)
    (child / "dirty.txt").write_text("working plus child\n", encoding="utf-8")
    (child / "parent-new.txt").write_text("inherited plus child\n", encoding="utf-8")
    # Parent progress in another file after launch is preserved by apply.
    (root / "app.txt").write_text("later parent\n", encoding="utf-8")

    captured = provider.capture("child")
    assert captured["paths"] == ["dirty.txt", "parent-new.txt"]
    assert captured["material_patch_complete"] is True
    applied = provider.apply("child")

    assert applied["ok"], applied
    assert (root / "dirty.txt").read_text(encoding="utf-8") == "working plus child\n"
    assert (root / "parent-new.txt").read_text(encoding="utf-8") == "inherited plus child\n"
    assert (root / "app.txt").read_text(encoding="utf-8") == "later parent\n"
    assert not (root / "deleted.txt").exists()
    assert _git(root, "rev-parse", "HEAD") == head
    assert (root / ".git" / "index").read_bytes() == index
    assert _git(root, "show", ":dirty.txt") == "staged"
    assert "parent-new.txt" in _git(root, "ls-files", "--others", "--exclude-standard")


def test_noop_child_does_not_recapture_parent_work(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    root, provider = workspace
    (root / "app.txt").write_text("parent dirty\n", encoding="utf-8")
    (root / "binary-new").write_bytes(b"\x00parent binary")
    child = _prepare(provider)

    captured = provider.capture("child")

    assert captured["no_changes"] is True
    assert captured["paths"] == []
    assert not child.exists()
    assert (root / "app.txt").read_text(encoding="utf-8") == "parent dirty\n"
    assert (root / "binary-new").read_bytes() == b"\x00parent binary"


def test_equal_parent_snapshots_have_stable_identity(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    root, provider = workspace
    (root / "app.txt").write_text("dirty\n", encoding="utf-8")
    first = provider.prepare("first")
    second = provider.prepare("second")
    (root / "app.txt").write_text("different\n", encoding="utf-8")
    third = provider.prepare("third")

    assert all(result["ok"] for result in (first, second, third))
    assert first["base_commit"] == second["base_commit"]
    assert first["base_commit"] != third["base_commit"]
    assert first["parent_head_commit"] == third["parent_head_commit"]


def test_session_artifacts_inside_repo_are_excluded_without_a_filename_classifier(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    root, _ = workspace
    store = SessionStore(
        enabled=True,
        sessions_dir=root / "custom-session-location",
        session_id="active",
        cwd=os.fspath(root),
        repo_root=os.fspath(root),
    )
    provider = SubagentWorkspaceProvider(root=root, store=store)
    try:
        first = _prepare(provider, "first")
        # A second snapshot must not recursively include the first worktree.
        second = _prepare(provider, "second")
        assert not (first / "custom-session-location").exists()
        assert not (second / "custom-session-location").exists()
        assert provider.capture("first")["no_changes"] is True
        assert provider.capture("second")["no_changes"] is True
    finally:
        provider.close()
        store.close()


def test_later_conflicting_parent_edit_refuses_entire_patch_and_retains_child(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    root, provider = workspace
    (root / "app.txt").write_text("dirty baseline\n", encoding="utf-8")
    child = _prepare(provider)
    (child / "app.txt").write_text("child edit\n", encoding="utf-8")
    (child / "new.txt").write_text("child addition\n", encoding="utf-8")
    provider.capture("child")
    (root / "app.txt").write_text("later conflicting parent\n", encoding="utf-8")

    result = provider.apply("child")

    assert result["ok"] is False
    assert result["error_code"] == "merge_conflict"
    assert child.exists()
    assert (child / "new.txt").read_text(encoding="utf-8") == "child addition\n"
    assert not (root / "new.txt").exists()
    assert (root / "app.txt").read_text(encoding="utf-8") == "later conflicting parent\n"


@pytest.mark.parametrize(
    "content",
    [b"\x00\x01new binary\xff", b"large text\n" * 60_000, b"line\r\nnext\r\n"],
    ids=["binary", "large", "crlf"],
)
def test_new_binary_and_large_files_are_retained_and_apply_completely(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    content: bytes,
) -> None:
    root, provider = workspace
    child = _prepare(provider)
    # This directory name was previously excluded by the legacy classifier.
    (child / "target").mkdir()
    (child / "target" / "output").write_bytes(content)

    captured = provider.capture("child")

    assert captured["paths"] == ["target/output"]
    assert captured["patch_capture_status"] == "complete"
    assert captured["material_patch_complete"] is True
    assert captured["no_changes"] is False
    assert captured["candidate_worktree_retained"] is True
    assert child.exists()
    applied = provider.apply("child")
    assert applied["ok"], applied
    assert (root / "target" / "output").read_bytes() == content


def test_capture_is_net_delta_across_child_committed_staged_and_unstaged_edits(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    root, provider = workspace
    child = _prepare(provider)
    (child / "app.txt").write_text("committed\n", encoding="utf-8")
    _git(child, "add", "app.txt")
    _git(
        child,
        "-c",
        "user.name=Alysis Tests",
        "-c",
        "user.email=tests@example.invalid",
        "-c",
        "core.hooksPath=/dev/null",
        "commit",
        "--no-gpg-sign",
        "-qm",
        "child commit",
    )
    (child / "app.txt").write_text("staged\n", encoding="utf-8")
    _git(child, "add", "app.txt")
    (child / "app.txt").write_text("final\n", encoding="utf-8")
    child_index = (Path(_git(child, "rev-parse", "--absolute-git-dir")) / "index").read_bytes()

    captured = provider.capture("child")

    assert captured["paths"] == ["app.txt"]
    assert (
        Path(_git(child, "rev-parse", "--absolute-git-dir")) / "index"
    ).read_bytes() == child_index
    assert provider.apply("child")["ok"] is True
    assert (root / "app.txt").read_text(encoding="utf-8") == "final\n"


def test_rename_reports_both_paths_and_is_independent_of_display_configuration(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    root, provider = workspace
    child = _prepare(provider)
    (child / "app.txt").rename(child / "renamed.txt")
    _git(root, "config", "color.ui", "always")
    _git(root, "config", "diff.noprefix", "true")

    captured = provider.capture("child")

    assert captured["paths"] == ["app.txt", "renamed.txt"]
    applied = provider.apply("child")
    assert applied["ok"], applied
    assert not (root / "app.txt").exists()
    assert (root / "renamed.txt").read_text(encoding="utf-8") == "base\n"


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX special files")
def test_unsupported_only_output_is_incomplete_retained_and_cannot_apply(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    _, provider = workspace
    child = _prepare(provider)
    os.mkfifo(child / "output.pipe")

    captured = provider.capture("child")

    assert captured["no_changes"] is False
    assert captured["material_patch_complete"] is False
    assert captured["patch_capture_status"] == "failed"
    assert captured["capture_reason_codes"] == ["unsupported_workspace_file"]
    assert captured["omitted_material_paths"] == ["output.pipe"]
    assert captured["candidate_worktree_retained"] is True
    assert (child / "output.pipe").exists()
    result = provider.apply("child")
    assert result["ok"] is False
    assert result["error_code"] == "incomplete_workspace_patch"
    assert (child / "output.pipe").exists()
    cleanup = provider.close()
    assert cleanup["retained_output_workspaces"][0]["run_id"] == "child"
    assert (child / "output.pipe").exists()
    assert provider.release("child", action="discarded")["ok"] is True
    assert not child.exists()


def test_unreadable_patch_does_not_prove_no_changes(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    _, provider = workspace
    child = _prepare(provider)
    # Git treats these non-UTF-8 bytes as text; the text patch transport cannot.
    (child / "output").write_bytes(b"\xffinvalid utf8\n")

    captured = provider.capture("child")

    assert captured["no_changes"] is False
    assert captured["material_patch_complete"] is False
    assert captured["candidate_worktree_retained"] is True
    assert (child / "output").read_bytes() == b"\xffinvalid utf8\n"
    assert provider.apply("child")["error_code"] == "incomplete_workspace_patch"
    provider.close()
    assert (child / "output").read_bytes() == b"\xffinvalid utf8\n"


@pytest.mark.parametrize("also_edit_tracked", [False, True], ids=["ignored-only", "mixed"])
def test_ignored_child_output_is_retained_and_reported_without_material_guessing(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    also_edit_tracked: bool,
) -> None:
    root, provider = workspace
    child = _prepare(provider)
    (child / "ignored").mkdir()
    (child / "ignored" / "requested-output").write_bytes(b"\x00deliverable")
    if also_edit_tracked:
        (child / "app.txt").write_text("child\n", encoding="utf-8")

    captured = provider.capture("child")

    assert captured["no_changes"] is False
    assert captured["material_patch_complete"] is True
    assert captured["patch_capture_status"] == "complete"
    assert captured["retained_ignored_paths"] == ["ignored"]
    assert captured["ignored_outputs_integrated"] is False
    assert captured["omitted_material_paths"] == []
    assert captured["paths"] == (["app.txt"] if also_edit_tracked else [])
    assert captured["candidate_worktree_retained"] is True
    assert (child / "ignored" / "requested-output").read_bytes() == b"\x00deliverable"
    applied = provider.apply("child")
    if also_edit_tracked:
        assert applied["ok"], applied
        assert applied["applied_paths"] == ["app.txt"]
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"
    else:
        assert applied["error_code"] == "no_captured_changes"
    assert applied["retained_ignored_paths"] == ["ignored"]
    assert applied["ignored_outputs_integrated"] is False
    assert not (root / "ignored").exists()
    cleanup = provider.close()
    assert cleanup["retained_output_workspaces"][0]["run_id"] == "child"
    assert (child / "ignored" / "requested-output").read_bytes() == b"\x00deliverable"
    assert provider.release("child", action="discarded")["ok"] is True
    assert not child.exists()


@pytest.mark.parametrize(
    ("changed_path", "content"),
    [
        ("app.txt", b"later edit\n"),
        ("later-output", b"\x00later output"),
        ("dirty.txt", None),
        ("ignored/output", b"keep ignored output"),
    ],
    ids=["modified", "added-binary", "deleted", "ignored-output"],
)
def test_changed_canonical_keeps_fresh_candidate_applicable(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    changed_path: str,
    content: bytes | None,
) -> None:
    root, provider = workspace
    canonical = _prepare(provider, "canonical")
    fresh = _prepare(provider, "fresh")
    duplicate = _prepare(provider, "duplicate")
    for child in (canonical, fresh, duplicate):
        (child / "app.txt").write_text("same edit\n", encoding="utf-8")
    assert provider.capture("canonical")["ok"] is True
    changed = canonical / changed_path
    if content is None:
        changed.unlink()
    else:
        changed.parent.mkdir(parents=True, exist_ok=True)
        changed.write_bytes(content)

    captured = provider.capture("fresh")

    assert captured["ok"] is True
    assert "duplicate_of" not in captured
    assert captured["candidate_worktree_retained"] is True
    assert fresh.exists()
    # Further duplicates must use the fresh candidate, not the stale mapping.
    assert provider.capture("duplicate")["duplicate_of"] == "fresh"
    assert not duplicate.exists()
    applied = provider.apply("fresh")
    assert applied["ok"], applied
    assert (root / "app.txt").read_text(encoding="utf-8") == "same edit\n"
    cleanup = provider.close()
    assert [item["run_id"] for item in cleanup["retained_output_workspaces"]] == ["canonical"]
    assert canonical.exists()
    if content is None:
        assert not changed.exists()
    else:
        assert changed.read_bytes() == content


def test_ignored_output_prevents_duplicate_cleanup_and_alias_discard_is_safe(
    workspace: tuple[Path, SubagentWorkspaceProvider],
) -> None:
    _, provider = workspace
    canonical = _prepare(provider, "canonical")
    alias = _prepare(provider, "alias")
    for child in (canonical, alias):
        (child / "app.txt").write_text("same edit\n", encoding="utf-8")
    provider.capture("canonical")
    assert provider.capture("alias")["duplicate_of"] == "canonical"
    (canonical / "ignored").mkdir()
    (canonical / "ignored" / "output").write_bytes(b"keep")
    captured = provider.capture("canonical")
    assert captured["retained_ignored_paths"] == ["ignored"]
    assert provider.release("alias", action="discarded")["ok"] is True
    assert (canonical / "ignored" / "output").read_bytes() == b"keep"
    another = _prepare(provider, "another")
    (another / "app.txt").write_text("same edit\n", encoding="utf-8")
    (another / "ignored").mkdir()
    (another / "ignored" / "output").write_bytes(b"different output")
    assert "duplicate_of" not in provider.capture("another")
    provider.close()
    assert (canonical / "ignored" / "output").read_bytes() == b"keep"
    assert (another / "ignored" / "output").read_bytes() == b"different output"


@pytest.mark.parametrize("apply_first", [False, True], ids=["close", "apply-then-close"])
def test_ignored_verification_output_created_after_capture_survives_cleanup(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    apply_first: bool,
) -> None:
    root, provider = workspace
    child = _prepare(provider)
    (child / "app.txt").write_text("child\n", encoding="utf-8")
    provider.capture("child")
    (child / "ignored").mkdir()
    (child / "ignored" / "verification-output").write_bytes(b"keep")
    if apply_first:
        applied = provider.apply("child")
        assert applied["ok"], applied
        assert applied["retained_ignored_paths"] == ["ignored"]
        assert applied["ignored_outputs_integrated"] is False
        assert (root / "app.txt").read_text(encoding="utf-8") == "child\n"

    cleanup = provider.close()

    assert cleanup["retained_output_workspaces"][0]["run_id"] == "child"
    assert (child / "ignored" / "verification-output").read_bytes() == b"keep"
    assert provider.release("child", action="discarded")["ok"] is True
    assert not child.exists()


@pytest.mark.parametrize("later_change", ["modify", "new-binary", "delete"])
@pytest.mark.parametrize("attempt_apply", [False, True], ids=["close", "apply-then-close"])
def test_later_material_output_refuses_stale_apply_and_survives_close(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    later_change: str,
    attempt_apply: bool,
) -> None:
    root, provider = workspace
    child = _prepare(provider)
    (child / "app.txt").write_text("captured child\n", encoding="utf-8")
    provider.capture("child")
    if later_change == "modify":
        (child / "app.txt").write_text("later child\n", encoding="utf-8")
    elif later_change == "new-binary":
        (child / "later-output").write_bytes(b"\x00later output")
    else:
        (child / "dirty.txt").unlink()

    if attempt_apply:
        applied = provider.apply("child")
        assert applied["ok"] is False
        assert applied["error_code"] == "workspace_changed_after_capture"
        assert applied["candidate_worktree_retained"] is True
    cleanup = provider.close()

    assert cleanup["retained_output_workspaces"][0]["run_id"] == "child"
    assert (root / "app.txt").read_text(encoding="utf-8") == "base\n"
    assert child.exists()
    # An explicit new capture permits review/apply of the actual final output.
    recaptured = provider.capture("child")
    assert recaptured["material_patch_complete"] is True
    assert provider.apply("child")["ok"] is True
    if later_change == "modify":
        assert (root / "app.txt").read_text(encoding="utf-8") == "later child\n"
    elif later_change == "new-binary":
        assert (root / "later-output").read_bytes() == b"\x00later output"
    else:
        assert not (root / "dirty.txt").exists()


def test_patch_artifact_write_failure_does_not_lose_unrecorded_output_on_close(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, provider = workspace
    child = _prepare(provider)
    (child / "output").write_bytes(b"\x00useful output")

    def fail_write(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated artifact persistence failure")

    with monkeypatch.context() as patch:
        patch.setattr(subagent_workspace, "atomic_write_text", fail_write)
        with pytest.raises(OSError, match="simulated artifact persistence failure"):
            provider.capture("child")

    cleanup = provider.close()

    assert cleanup["retained_output_workspaces"][0]["run_id"] == "child"
    assert (child / "output").read_bytes() == b"\x00useful output"
    assert provider.release("child", action="discarded")["ok"] is True
    assert not child.exists()


def test_parent_mutation_during_snapshot_refuses_launch(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, provider = workspace
    snapshot = git_evidence._working_tree_snapshot
    calls = 0

    def change_after_snapshot(*args: object, **kwargs: object) -> str:
        nonlocal calls
        tree = snapshot(*args, **kwargs)
        calls += 1
        if calls == 1:
            (root / "app.txt").write_text("concurrent parent edit\n", encoding="utf-8")
        return tree

    monkeypatch.setattr(git_evidence, "_working_tree_snapshot", change_after_snapshot)

    result = provider.prepare("child")

    assert result["ok"] is False
    assert result["error_code"] == "workspace_changed_during_snapshot"
    assert provider.get("child") is None
    assert (root / "app.txt").read_text(encoding="utf-8") == "concurrent parent edit\n"


@pytest.mark.parametrize("failed_action", ["reattached", "duplicate_retargeted"])
def test_reattach_log_failure_preserves_original_workspace_and_alias_ownership(
    workspace: tuple[Path, SubagentWorkspaceProvider],
    monkeypatch: pytest.MonkeyPatch,
    failed_action: str,
) -> None:
    _, provider = workspace
    original = _prepare(provider, "original")
    alias = _prepare(provider, "alias")
    for child in (original, alias):
        (child / "app.txt").write_text("child\n", encoding="utf-8")
    provider.capture("original")
    assert provider.capture("alias")["duplicate_of"] == "original"
    original_event = provider._event

    def fail_event(run_id: str, action: str, **fields: object) -> None:
        if action == failed_action:
            raise OSError("simulated persistence failure")
        original_event(run_id, action, **fields)

    with monkeypatch.context() as patch:
        patch.setattr(provider, "_event", fail_event)
        with pytest.raises(OSError, match="simulated persistence failure"):
            provider.reattach_for_resume("original", "continuation")

    assert provider.get("continuation") is None
    assert provider.get("original").worktree_path == original
    assert provider.get("alias").duplicate_of == "original"
    assert (original / "app.txt").read_text(encoding="utf-8") == "child\n"
    assert provider.reattach_for_resume("original", "continuation")["ok"] is True
    assert provider.get("original") is None
    assert provider.get("alias").duplicate_of == "continuation"
