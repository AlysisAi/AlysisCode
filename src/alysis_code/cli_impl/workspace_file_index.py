"""Bounded, background-only workspace discovery for composer path references."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import tempfile
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from ..git_safe import build_git_process_env

GENERATED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "node_modules",
        "__pycache__",
        ".venv",
        "venv",
        ".pytest_cache",
        ".ruff_cache",
        ".mypy_cache",
        ".tox",
        "dist",
        "build",
        "coverage",
        ".next",
        ".alysis",
    }
)


@dataclass(frozen=True)
class FileIndexSnapshot:
    paths: tuple[str, ...] = ()
    visited: int = 0
    truncated: bool = False
    unavailable: bool = False


def _is_link(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def scan_workspace(
    root: Path,
    *,
    max_entries: int = 20_000,
    max_depth: int = 24,
    max_seconds: float = 4.0,
    cancelled=lambda: False,
) -> FileIndexSnapshot:
    """Scan at most max_entries directory entries, never following a link.

    Git supplies exact ignore semantics (including nested rules and exclusions).
    A temporary bare Git directory supplies those semantics for non-Git folders.
    If Git is unavailable, fail closed rather than expose ignored files.
    """
    deadline = time.monotonic() + max_seconds
    visited = 0
    paths: list[str] = []
    truncated = False
    pending = deque([(root, 0)])
    env = build_git_process_env()
    command = ["git", "-C", str(root)]

    def run(args, data=None):
        remaining = deadline - time.monotonic()
        if remaining <= 0 or cancelled():
            raise TimeoutError
        return subprocess.run(
            args,
            input=data,
            capture_output=True,
            check=False,
            env=env,
            timeout=min(remaining, 2.0),
        )

    try:
        root = root.resolve(strict=True)
        if not root.is_dir():
            return FileIndexSnapshot(unavailable=True)
        pending = deque([(root, 0)])
        with tempfile.TemporaryDirectory(prefix="alysis-mention-git-") as scratch:
            probe = run([*command, "rev-parse", "--git-dir"])
            if probe.returncode != 0:
                initialized = run(["git", "init", "--bare", "--quiet", scratch])
                if initialized.returncode != 0:
                    return FileIndexSnapshot(unavailable=True)
                command += [f"--git-dir={scratch}", f"--work-tree={root}"]
            while pending and visited < max_entries:
                directory, depth = pending.popleft()
                if cancelled() or time.monotonic() >= deadline:
                    truncated = True
                    break
                # Recheck queued paths: a directory may have become a junction.
                if _is_link(directory) or not directory.resolve().is_relative_to(root):
                    continue
                with os.scandir(directory) as entries:
                    exhausted = False
                    while not exhausted and visited < max_entries:
                        batch: list[tuple[Path, str, bool]] = []
                        for _ in range(min(128, max_entries - visited)):
                            if cancelled() or time.monotonic() >= deadline:
                                raise TimeoutError
                            entry = next(entries, None)
                            if entry is None:
                                exhausted = True
                                break
                            visited += 1
                            path = Path(entry.path)
                            try:
                                if _is_link(path):
                                    continue
                                is_dir = entry.is_dir(follow_symlinks=False)
                                if is_dir and entry.name in GENERATED_DIRS:
                                    continue
                                if not is_dir and not entry.is_file(follow_symlinks=False):
                                    continue
                                rel = path.relative_to(root).as_posix() + ("/" if is_dir else "")
                                batch.append((path, rel, is_dir))
                            except OSError:
                                continue
                        if not batch:
                            continue
                        result = run(
                            [*command, "check-ignore", "--no-index", "--stdin", "-z"],
                            b"\0".join(os.fsencode(rel) for _, rel, _ in batch) + b"\0",
                        )
                        if result.returncode not in (0, 1):
                            return FileIndexSnapshot(visited=visited, unavailable=True)
                        ignored = {
                            os.fsdecode(p).replace("\\", "/") for p in result.stdout.split(b"\0")
                        }
                        for path, rel, is_dir in batch:
                            if rel in ignored:
                                continue
                            paths.append(rel)
                            if is_dir:
                                if depth < max_depth:
                                    pending.append((path, depth + 1))
                                else:
                                    truncated = True
                    if not exhausted:
                        truncated = True
            truncated |= bool(pending) or visited >= max_entries
    except (subprocess.TimeoutExpired, TimeoutError):
        truncated = True
    except (OSError, ValueError):
        return FileIndexSnapshot(visited=visited, unavailable=True)
    return FileIndexSnapshot(
        tuple(sorted(paths, key=lambda p: (p.casefold(), p))), visited, truncated
    )


class WorkspaceFileIndex:
    """One-root cache; lookups never touch disk, scans run on one worker.

    Root switches and expiration discard the old snapshot immediately. Call
    invalidate after known filesystem changes. Otherwise refresh after ttl seconds.
    """

    def __init__(self, *, ttl: float = 5.0, scanner=scan_workspace) -> None:
        self._ttl = ttl
        self._scanner = scanner
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="file-mentions")
        self._lock = threading.Lock()
        self._root: str | None = None
        self._future: Future | None = None
        self._expires = 0.0
        self._generation = 0

    def _request(self, root: Path) -> Future:
        key = os.path.normcase(os.path.abspath(root))
        with self._lock:
            if key != self._root or (
                self._future is not None
                and self._future.done()
                and time.monotonic() >= self._expires
            ):
                self._generation += 1
                if self._future is not None:
                    self._future.cancel()
                self._future = None
                self._root = key
            if self._future is None:
                generation = self._generation

                def scan():
                    value = self._scanner(
                        Path(root), cancelled=lambda: generation != self._generation
                    )
                    self._expires = time.monotonic() + self._ttl
                    return value

                self._future = self._executor.submit(scan)
            return self._future

    def cached(self, root: Path) -> FileIndexSnapshot:
        future = self._request(root)
        return future.result() if future.done() and not future.cancelled() else FileIndexSnapshot()

    async def ready(self, root: Path) -> FileIndexSnapshot:
        future = self._request(root)
        try:
            result = await asyncio.shield(asyncio.wrap_future(future))
        except asyncio.CancelledError:
            raise
        with self._lock:
            return result if future is self._future else FileIndexSnapshot()

    def invalidate(self) -> None:
        with self._lock:
            self._generation += 1
            if self._future is not None:
                self._future.cancel()
            self._future = None

    def close(self) -> None:
        self.invalidate()
        self._executor.shutdown(wait=False, cancel_futures=True)
