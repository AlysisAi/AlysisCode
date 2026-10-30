"""Local host evidence for user-named output paths, independent of the writing tool.

Only metadata is observed. A file's existence/change is not evidence that its
contents satisfy a format, visual, or behavioral requirement. No MCP receipt or
model-provided path can enroll an output in this observer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..surface import ApprovalRequest
from ..tools.fs import classify_sensitive_path
from .artifact_metadata_io import FileObservation, observe_metadata
from .checkpoint_worker import _close_worker_streams

if TYPE_CHECKING:
    from .acceptance_contract import AcceptanceContract


ArtifactOutputAccess = Callable[[str, bool], bool]


@dataclass(frozen=True, slots=True)
class FileMetadataApproval(ApprovalRequest):
    """Host-only request for the fixed lstat worker, never file contents or code."""


def _eligible_path(path: str) -> bool:
    candidate = Path(path)
    return not (
        not candidate.is_absolute()
        or str(path).startswith(("\\\\", "//"))
        or ".." in candidate.parts
        or (os.name == "nt" and ":" in str(path)[2:])
        or classify_sensitive_path(path).sensitive
    )


def observe_file(path: str) -> FileObservation:
    """Do not traverse links, junctions, device paths, or network locations."""
    if not _eligible_path(path):
        return FileObservation("unavailable")
    return observe_metadata(path)


def _metadata_worker_command(paths: list[str]) -> list[str] | None:
    # sys.executable can be a Windows venv/uv launcher that spawns a descendant.
    # The runtime's base interpreter, unlike PATH or an environment override,
    # identifies the installation currently executing this trusted host code.
    executable = getattr(sys, "_base_executable", None)
    if not isinstance(executable, str) or not executable:
        return None
    candidate = Path(executable)
    if (
        not candidate.is_absolute()
        or executable.startswith(("\\\\", "//"))
        or ".." in candidate.parts
        or (os.name == "nt" and ":" in executable[2:])
    ):
        return None
    # -S also excludes installed .pth/sitecustomize code. This fixed sibling uses
    # only the standard library and never launches another process. Missing or
    # unusable interpreter files fail closed at Popen; do not fall back to a shim.
    return [
        executable,
        "-I",
        "-S",
        str(Path(__file__).with_name("artifact_metadata_io.py")),
        json.dumps(paths),
    ]


def _stop_metadata_leaf(process: subprocess.Popen[bytes], *, stop_at: float) -> bool:
    """Reap only the fixed base-interpreter/-I/-S metadata leaf within its budget.

    Do not reuse this for checkpoint workers or arbitrary commands: they still
    need descendant cleanup. Global process inventory can consume the metadata
    worker's small cleanup reserve before its own termination signal is sent.
    """
    try:
        process.kill()
    except OSError:
        pass  # A concurrent exit is successful only if the final wait confirms it.
    try:
        process.wait(timeout=max(0.0, stop_at - time.monotonic()))
    except (subprocess.TimeoutExpired, OSError):
        return False
    return True


class ArtifactOutputObserver:
    """One turn's approved, exact-path metadata observations. Never serialized."""

    def __init__(
        self,
        contract: AcceptanceContract,
        access: ArtifactOutputAccess,
        *,
        enroll_external: bool = True,
        remaining_seconds: Callable[[], float | None] = lambda: None,
        cancelled: Callable[[], bool] = lambda: False,
    ):
        from .acceptance_contract import AcceptancePathKind, AcceptancePathRole

        self.task_id = contract.task_id
        self.access = access
        self.remaining_seconds = remaining_seconds
        self.cancelled = cancelled
        self._io_cleanup_pending = False
        self.baseline: dict[str, FileObservation] = {}
        self.current: dict[str, FileObservation] = {}
        self.changed_paths: set[str] = set()
        self.changed_loci: set[str] = set()
        self.loci_by_path: dict[str, set[str]] = {}
        self.workspace_paths: set[str] = set()
        self.generation = -1
        for ref in contract.path_refs:
            # Prose extraction is a hint, not a reason to inspect an external
            # input. Wait for semantic path binding before requesting authority.
            if ref.path_kind == AcceptancePathKind.ABSOLUTE_EXTERNAL and not enroll_external:
                continue
            if (
                ref.path_kind != AcceptancePathKind.UNRESOLVED
                and ref.role == AcceptancePathRole.REQUIRED_OUTPUT
                and ref.absolute_path
            ):
                path = ref.absolute_path
                # Enroll only native local paths; never ask to inspect credentials.
                if not _eligible_path(path):
                    continue
                self.loci_by_path.setdefault(path, set()).add(
                    ref.workspace_relative_path or ref.display_path
                )
                if ref.path_kind != AcceptancePathKind.ABSOLUTE_EXTERNAL:
                    self.workspace_paths.add(path)
                if path in self.baseline:
                    continue
                if not self._stopped() and access(path, True):
                    self.baseline[path] = FileObservation("unavailable")
        self.baseline.update(self._observe_batch(list(self.baseline)))

    def _stopped(self) -> bool:
        remaining = self.remaining_seconds()
        return self.cancelled() or (remaining is not None and remaining <= 0)

    def _observe_batch(self, paths: list[str]) -> dict[str, FileObservation]:
        unavailable = {path: FileObservation("unavailable") for path in paths}
        if not paths or self._stopped() or self._io_cleanup_pending:
            return unavailable
        remaining = self.remaining_seconds()
        budget = min(3.0, remaining if remaining is not None else 3.0)
        stop_at = time.monotonic() + budget
        work_stop_at = stop_at - min(0.15, budget * 0.2)
        process = None
        communication_finished = False
        try:
            # A thread cannot interrupt lstat on an unavailable mounted drive.
            # Use one disposable child and one total budget for the whole batch.
            command = _metadata_worker_command(paths)
            if command is None:
                return unavailable
            # Bound the Windows command line. Avoid stdin writes: even
            # communicate(timeout=...) can block writing a full pipe on Windows.
            if len(subprocess.list2cmdline(command)) > 30000:
                return unavailable
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            while not self._stopped():
                wait = min(0.05, work_stop_at - time.monotonic())
                if wait <= 0:
                    break
                try:
                    output, _ = process.communicate(timeout=wait)
                    communication_finished = True
                    if process.returncode or self._stopped() or time.monotonic() >= work_stop_at:
                        return unavailable
                    values = json.loads(output)
                    if set(values) != set(paths):
                        return unavailable
                    observations = {}
                    for path, value in values.items():
                        state, stamp = value["state"], value["stamp"]
                        if state not in {"file", "directory", "missing", "unavailable"}:
                            return unavailable
                        if state == "file" and (
                            not isinstance(stamp, list)
                            or len(stamp) != 5
                            or any(type(item) is not int for item in stamp)
                        ):
                            return unavailable
                        observations[path] = FileObservation(state, tuple(stamp) if stamp else None)
                    return observations
                except subprocess.TimeoutExpired:
                    pass
        except (OSError, ValueError, KeyError, TypeError):
            return unavailable
        finally:
            if process is not None:
                if not communication_finished:
                    self._io_cleanup_pending = not _stop_metadata_leaf(process, stop_at=stop_at)
                _close_worker_streams(process, deferred=not communication_finished)
        return unavailable

    def refresh(self, contract: AcceptanceContract) -> None:
        from .acceptance_contract import EvidenceOrigin

        self.current.clear()
        self.changed_paths.clear()
        self.changed_loci.clear()
        self.generation = contract.generation
        if contract.task_id != self.task_id:
            return
        # Denied/expired enrollment must also suppress synchronous workspace
        # fallbacks. Unknown metadata never certifies a requested output.
        self.current.update({path: FileObservation("unavailable") for path in self.loci_by_path})
        approved = [
            path for path in self.baseline if not self._stopped() and self.access(path, False)
        ]
        observed = self._observe_batch(approved)
        for path, before in self.baseline.items():
            # Reapply current deny rules on every read; approval is scoped to the
            # baseline/final observations of this exact output during this turn.
            after = observed.get(path, FileObservation("unavailable"))
            self.current[path] = after
            file_changed = (
                after.state == "file" and before.state in {"missing", "file"} and before != after
            )
            # Local path criteria already admit directories. A newly observed
            # directory addresses creation of that exact locus, not its contents
            # or changes inside a pre-existing directory. External file criteria
            # remain file-only, with their independent access checks unchanged.
            directory_created = (
                path in self.workspace_paths
                and before.state == "missing"
                and after.state == "directory"
            )
            if file_changed or directory_created:
                self.changed_paths.add(path)
                self.changed_loci.update(self.loci_by_path[path])
            contract.add_evidence(
                origin=EvidenceOrigin.HOST_AUTHORITATIVE,
                summary=f"Requested output metadata: {after.state}; contents not verified",
                passed=after.state == "file" or directory_created,
                paths=(path,),
                category="artifact_file_metadata",
                tool_name="host_file_metadata",
                evidence_allowed=True,
            )

    def current_for(self, contract: AcceptanceContract) -> bool:
        return self.task_id == contract.task_id and self.generation == contract.generation
