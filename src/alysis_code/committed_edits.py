"""Content identities of successful structured mutations, scoped to the owning IDE turn."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .sensitive_paths import sensitive_path_category


@dataclass(frozen=True)
class _Observer:
    root: Path
    emit: Callable[[dict[str, Any]], None]


_observer: ContextVar[_Observer | None] = ContextVar("committed_edit_observer", default=None)
_original: ContextVar[tuple[str, bytes] | None] = ContextVar(
    "committed_edit_original", default=None
)
_MAX_COUNT_BYTES = 8192
TRACKED_WORKER_TOOLS = frozenset({"fs_write_prepared", "fs_edit_prepared", "fs_delete"})


@contextmanager
def observe(root: Path, emit: Callable[[dict[str, Any]], None]) -> Iterator[None]:
    token = _observer.set(_Observer(root, emit))
    original = _original.set(None)
    try:
        yield
    finally:
        _original.reset(original)
        _observer.reset(token)


def observing(root: Path) -> bool:
    observer = _observer.get()
    return observer is not None and observer.root == root


def remember_verified_bytes(path: str, digest: str, data: bytes) -> None:
    if _observer.get() is not None:
        _original.set(
            (digest, data)
            if len(data) <= _MAX_COUNT_BYTES and not sensitive_path_category(path)
            else None
        )


def _line_counts(old: bytes, new: bytes) -> tuple[int, int] | None:
    if b"\0" in old or b"\0" in new:
        return None
    try:
        left = old.decode("utf-8").splitlines(keepends=True)
        right = new.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError:
        return None
    if len(left) + len(right) > 512:
        return None
    from difflib import SequenceMatcher

    added = removed = 0
    for tag, i, j, k, m in SequenceMatcher(None, left, right, autojunk=False).get_opcodes():
        if tag != "equal":
            added += m - k
            removed += j - i
    return added, removed


def committed(root: Path, target: Path, path: str, before: str | None, text: str | None) -> None:
    """Called only after the no-replace publication or guarded unlink has succeeded."""
    observer = _observer.get()
    if observer is None or observer.root != root:
        return
    original = _original.get()
    _original.set(None)
    if not target.is_relative_to(root) or sensitive_path_category(path):
        return
    relative = target.relative_to(root).as_posix()
    if sensitive_path_category(relative) or any(
        p.casefold() in {".git", ".alysis_images"} for p in Path(relative).parts
    ):
        return
    data = text.encode("utf-8") if text is not None else b""
    after = hashlib.sha256(data).hexdigest() if text is not None else None
    if before == after:
        return
    evidence = {"path": relative, "before": before, "after": after}
    old = b"" if before is None else original[1] if original and original[0] == before else None
    if old is not None and len(data) <= _MAX_COUNT_BYTES:
        try:
            counts = _line_counts(old, data)
        except Exception:
            # Optional presentation work follows publication and cannot fail the tool.
            counts = None
        if counts is not None:
            evidence.update(added=counts[0], removed=counts[1])
    relay(root, evidence)


def expected_worker_edit(root: Path, name: str, args: dict[str, Any]) -> dict[str, Any] | None:
    if not observing(root) or name not in TRACKED_WORKER_TOOLS:
        return None
    path, condition = args.get("path"), args.get("precondition")
    if (
        not isinstance(path, str)
        or not isinstance(condition, dict)
        or condition.get("path") != path
    ):
        return None
    target = root / path
    if not target.is_relative_to(root) or ".." in target.parts or sensitive_path_category(path):
        return None
    before = condition.get("content_sha256")
    if condition.get("exists") is True:
        if not isinstance(before, str) or re.fullmatch("[0-9a-f]{64}", before) is None:
            return None
    elif condition.get("exists") is not False or before is not None:
        return None
    after = None
    if name != "fs_delete":
        content = args.get("content" if name == "fs_write_prepared" else "updated_content")
        if not isinstance(content, str):
            return None
        try:
            after = hashlib.sha256(content.encode("utf-8")).hexdigest()
        except UnicodeEncodeError:
            return None
    if before == after:
        return None
    return {"path": target.relative_to(root).as_posix(), "before": before, "after": after}


def relay_worker(root: Path, expected: dict[str, Any], evidence: object) -> None:
    # Admit only a matching identity from this successful fixed worker invocation.
    if not isinstance(evidence, dict) or any(
        evidence.get(k) != v or k not in evidence for k, v in expected.items()
    ):
        return
    keys = evidence.keys() - expected.keys()
    if keys and (
        keys != {"added", "removed"}
        or not all(type(evidence[k]) is int and 0 <= evidence[k] <= 512 for k in keys)
    ):
        return
    relay(root, evidence)


def relay(root: Path, evidence: dict[str, Any]) -> None:
    observer = _observer.get()
    if observer is not None and observer.root == root:
        try:
            observer.emit(evidence)
        except Exception:  # A presentation failure cannot turn a committed mutation into a retry.
            pass
