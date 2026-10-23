"""Bounded, read-only working-tree discovery and lazy patch loading.

No session, reviewer, or provider is accepted by this service. Labels describe
Git state only; they make no claim about which task authored a change.
"""

from __future__ import annotations

import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from .code_review import (
    GitReviewError,
    InvalidReviewRequest,
    ReviewLimits,
    _drain_bounded,
    _normalize_repo_path,
    _parse_name_status,
    read_untracked_patch,
)
from .git_safe import build_git_cmd, build_git_process_env


@dataclass(frozen=True)
class ChangedFile:
    section: str
    status: str
    paths: tuple[str, ...]

    @property
    def label(self) -> str:
        return f"{self.section} {self.status}  " + " → ".join(self.paths)


@dataclass(frozen=True)
class ChangeList:
    files: tuple[ChangedFile, ...] = ()
    notices: tuple[str, ...] = ()


@dataclass(frozen=True)
class FilePatch:
    text: str
    truncated: bool = False


@dataclass(frozen=True)
class _Output:
    stdout: bytes
    returncode: int
    truncated: bool


class DiffReader:
    """Synchronous bounded service; call from a worker, never a UI callback."""

    def __init__(self, workspace: Path, *, limits: ReviewLimits | None = None):
        self.workspace = Path(workspace).resolve()
        self.limits = limits or ReviewLimits(max_files=300, git_timeout_s=8)
        self._filters: dict[str, str] = {}

    def _git(self, args: list[str], limit: int) -> _Output:
        config = {
            "color.ui": "false",
            "core.quotepath": "false",
            "core.fsmonitor": "false",
            "core.untrackedCache": "false",
            "diff.renameLimit": "200",
            **self._filters,
        }
        env = build_git_process_env()
        for key in tuple(env):
            if key in {"GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR"}:
                env.pop(key)
        env.update(GIT_OPTIONAL_LOCKS="0", GIT_LITERAL_PATHSPECS="1")
        try:
            process = subprocess.Popen(
                build_git_cmd(self.workspace, args, extra_config=config),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            raise GitReviewError("Git is unavailable or the workspace cannot be read.") from exc
        parts: list[bytes] = []
        state: list[int | bool] = [0, False]
        threads = [
            threading.Thread(
                target=_drain_bounded, args=(process.stdout, parts, limit, state), daemon=True
            ),
            threading.Thread(
                target=_drain_bounded, args=(process.stderr, [], 4096, [0, False]), daemon=True
            ),
        ]
        for thread in threads:
            thread.start()
        try:
            process.wait(timeout=self.limits.git_timeout_s)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            process.wait(timeout=5)
            raise GitReviewError(
                "Git timed out; content is unavailable. Refresh to retry."
            ) from exc
        finally:
            for thread in threads:
                thread.join(timeout=2)
        return _Output(b"".join(parts), process.returncode, bool(state[1]))

    def _prepare(self) -> None:
        result = self._git(["rev-parse", "--show-toplevel"], 16384)
        if result.returncode or result.truncated:
            raise GitReviewError("This directory is not an available Git working tree.")
        self.workspace = Path(os.fsdecode(result.stdout).strip()).resolve()
        # Diff can invoke clean/process filters even with --no-textconv. Refuse
        # conversion rather than executing repository-configured programs.
        result = self._git(
            ["config", "--null", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"], 65536
        )
        if result.returncode not in (0, 1) or result.truncated:
            raise GitReviewError("Git filter configuration is unavailable or too large.")
        self._filters = {}
        for record in result.stdout.split(b"\0"):
            key, _, value = record.partition(b"\n")
            if value:
                driver = key.decode("utf-8", "replace").rsplit(".", 1)[0]
                self._filters.update(
                    {f"{driver}.{kind}": "" for kind in ("clean", "smudge", "process")}
                )
                self._filters[f"{driver}.required"] = "true"

    @staticmethod
    def _diff_args(section: str) -> list[str]:
        return [
            "diff",
            *(["--cached"] if section == "staged" else []),
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "--find-renames",
            "--ignore-submodules=none",
        ]

    def list_changes(self) -> ChangeList:
        self._prepare()
        files: list[ChangedFile] = []
        notices: list[str] = []
        cap = self.limits.max_files
        for section in ("staged", "unstaged", "untracked"):
            args = (
                ["ls-files", "--others", "--exclude-standard", "-z"]
                if section == "untracked"
                else [*self._diff_args(section), "--name-status", "-z", "--"]
            )
            result = self._git(args, max(65536, cap * 2048))
            if result.returncode:
                notices.append(
                    f"{section}: list unavailable (Git failed; configured filters may prevent reading)."
                )
                continue
            raw = result.stdout
            if result.truncated:
                raw = raw[: raw.rfind(b"\0") + 1]
                notices.append(f"{section}: file list truncated; additional changes omitted.")
            if section == "untracked":
                for item in raw.split(b"\0"):
                    if not item:
                        continue
                    try:
                        path = _normalize_repo_path(os.fsdecode(item), workspace=self.workspace)
                    except InvalidReviewRequest:
                        notices.append("Unrepresentable or unsafe file path omitted.")
                        continue
                    files.append(ChangedFile(section, "?", (path,)))
                    if len(files) > cap:
                        break
            else:
                changes, dropped, capped = _parse_name_status(
                    raw, workspace=self.workspace, max_changes=cap + 1
                )
                files.extend(ChangedFile(section, item.status, item.paths) for item in changes)
                if dropped:
                    notices.append("Unrepresentable or unsafe file paths omitted.")
                if capped:
                    notices.append(f"{section}: additional files omitted.")
            if len(files) > cap:
                notices.append(f"File limit ({cap}) reached; additional changes omitted.")
                files = files[:cap]
                break
        return ChangeList(tuple(files), tuple(dict.fromkeys(notices)))

    def read_patch(self, entry: ChangedFile) -> FilePatch:
        if entry.section not in {"staged", "unstaged", "untracked"}:
            raise ValueError("Unknown change section")
        for path in entry.paths:
            _normalize_repo_path(path, workspace=self.workspace)
        if entry.section == "untracked":
            text, truncated, omission = read_untracked_patch(
                self.workspace, entry.paths[-1], limits=self.limits
            )
            if omission:
                text = "Content unavailable: " + omission.replace("_", " ") + "."
        else:
            result = self._git(
                [*self._diff_args(entry.section), "--unified=3", "--", *entry.paths],
                self.limits.max_file_bytes,
            )
            if result.returncode:
                return FilePatch(
                    "Content unavailable: Git failed (the file may have changed or a configured filter refused conversion)."
                )
            text = result.stdout.decode("utf-8", "replace")
            truncated = result.truncated
        if entry.section == "untracked" and "@@ -0,0 +1,0 @@" in text:
            text = text.replace("@@ -0,0 +1,0 @@", "[Empty untracked file; no textual content.]")
        if not text.strip():
            text = "No textual diff is available (empty file, metadata-only change, or changed since refresh)."
        if "Binary files " in text or "GIT binary patch" in text:
            text += "\nBinary content is not displayed."
        if truncated:
            text += f"\n[Diff truncated at {self.limits.max_file_bytes} bytes; remaining content omitted.]"
        # Never allow file content to inject terminal escape/control sequences.
        text = "".join(
            c if c in "\n\t" or (ord(c) >= 32 and not 127 <= ord(c) < 160) else "�" for c in text
        )
        return FilePatch(text, truncated)
