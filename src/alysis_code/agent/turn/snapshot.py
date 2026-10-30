from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from ...runtime_artifacts import is_runtime_artifact_path
from ...sandbox_bookkeeping import capture_instruction_creations, command_file_signature
from ..prompt_context import _normalize_repo_relative_hint_path

_SHELL_MUTATION_SNAPSHOT_METADATA_PREFIX = "meta"


def _list_git_workspace_snapshot_paths(root: Path) -> set[str] | None:
    try:
        proc = subprocess.run(
            [
                "git",
                "-C",
                os.fspath(root),
                "ls-files",
                "-z",
                "--cached",
                "--others",
                "--exclude-standard",
            ],
            check=False,
            capture_output=True,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None

    paths: set[str] = set()
    stdout = proc.stdout or b""
    raw_items: list[str | bytes]
    if isinstance(stdout, str):
        raw_items = stdout.split("\0")
    else:
        raw_items = stdout.split(b"\0")
    for raw_item in raw_items:
        if not raw_item:
            continue
        raw_path = (
            raw_item
            if isinstance(raw_item, str)
            else raw_item.decode("utf-8", errors="surrogateescape")
        )
        normalized = _normalize_repo_relative_hint_path(
            root=root,
            raw=raw_path,
        )
        if not normalized or is_runtime_artifact_path(normalized, root=root):
            continue
        paths.add(normalized)
    return paths


def _walk_workspace_snapshot_paths(root: Path) -> set[str]:
    root_resolved = root.resolve()
    paths: set[str] = set()
    for current_root, dirnames, filenames in os.walk(root_resolved):
        current_path = Path(current_root)
        resolved_current = current_path.resolve()
        rel_dir = (
            resolved_current.relative_to(root_resolved).as_posix()
            if resolved_current != root_resolved
            else ""
        )
        kept_dirnames: list[str] = []
        for dirname in dirnames:
            rel_candidate = dirname if not rel_dir else f"{rel_dir}/{dirname}"
            normalized = _normalize_repo_relative_hint_path(root=root, raw=rel_candidate)
            if normalized and is_runtime_artifact_path(normalized, root=root):
                continue
            kept_dirnames.append(dirname)
        dirnames[:] = kept_dirnames
        for filename in filenames:
            rel_candidate = filename if not rel_dir else f"{rel_dir}/{filename}"
            normalized = _normalize_repo_relative_hint_path(root=root, raw=rel_candidate)
            if not normalized or is_runtime_artifact_path(normalized, root=root):
                continue
            paths.add(normalized)
    return paths


def _workspace_snapshot_signature(path: Path) -> str | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    if not path.is_file():
        return None
    return command_file_signature(stat)


def _snapshot_workspace_for_command_mutation_detection(root: Path) -> dict[str, str]:
    candidate_paths = _list_git_workspace_snapshot_paths(root)
    if candidate_paths is None:
        candidate_paths = _walk_workspace_snapshot_paths(root)
    snapshot: dict[str, str] = {}
    for rel_path in sorted(candidate_paths):
        signature = _workspace_snapshot_signature(root / rel_path)
        if signature is None:
            continue
        snapshot[rel_path] = signature
    return snapshot


def _path_matches_snapshot_ignore(
    rel_path: str,
    *,
    ignored_paths: set[str],
) -> bool:
    return any(
        rel_path == ignored or rel_path.startswith(f"{ignored}/") for ignored in ignored_paths
    )


def _normalize_snapshot_ignore_paths(root: Path, paths: Iterable[Path]) -> set[str]:
    ignored: set[str] = set()
    resolved_root = root.resolve()
    for path in paths:
        try:
            rel_path = path.resolve().relative_to(resolved_root)
        except ValueError:
            continue
        normalized = _normalize_repo_relative_hint_path(root=root, raw=os.fspath(rel_path))
        if normalized:
            ignored.add(normalized)
    return ignored


def _detect_command_mutation_paths(
    *,
    before: dict[str, str],
    after: dict[str, str],
) -> list[str]:
    changed = {
        rel_path
        for rel_path in (set(before) | set(after))
        if before.get(rel_path) != after.get(rel_path)
    }
    return sorted(changed)


def _run_with_command_mutation_detection(
    *,
    root: Path,
    enabled: bool,
    ignored_paths: Iterable[Path] = (),
    operation: Callable[[], Any],
) -> tuple[Any, list[str]]:
    if not enabled:
        return operation(), []
    ignored = _normalize_snapshot_ignore_paths(root, ignored_paths)
    before_snapshot = {
        rel_path: signature
        for rel_path, signature in _snapshot_workspace_for_command_mutation_detection(root).items()
        if not _path_matches_snapshot_ignore(rel_path, ignored_paths=ignored)
    }
    with capture_instruction_creations(root) as created:
        result = operation()
    for receipt in created:
        # Existing baseline paths are never rewritten. Signatures come from the
        # retained exclusive-creation handle, before the native child runs.
        if not _path_matches_snapshot_ignore(receipt.relative_path, ignored_paths=ignored):
            before_snapshot.setdefault(receipt.relative_path, receipt.signature)
    after_snapshot = {
        rel_path: signature
        for rel_path, signature in _snapshot_workspace_for_command_mutation_detection(root).items()
        if not _path_matches_snapshot_ignore(rel_path, ignored_paths=ignored)
    }
    return (
        result,
        _detect_command_mutation_paths(
            before=before_snapshot,
            after=after_snapshot,
        ),
    )
