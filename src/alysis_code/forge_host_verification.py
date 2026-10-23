"""Exact, argv-based verification for Forge Host Protocol v2.

The legacy verifier intentionally supports many command families and shell
expressions.  Host Protocol v2 Phase 0 has a much smaller authority surface:
the admitted command is exactly ``pytest`` or ``pytest -q``.  This module keeps
that boundary separate so a Host run can never fall through to the legacy
string-shell execution path.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from .atomic_io import atomic_write_text
from .error_text import sanitize_error_summary
from .execution_deadline import (
    DeadlineExhausted,
    DeadlineOperation,
    ExecutionDeadline,
    deadline_timeout_or_raise,
)
from .forge_host_protocol import VerificationCommand
from .process_reaping import (
    ProcessGroupRegistry,
    process_group_exists,
    process_groups_supported,
    reap_process_group,
    run_in_tracked_process_group,
)
from .verification_command_analysis import (
    VerificationCommandEvidentiaryCapability,
    analyze_verification_command,
)
from .verify_gate import assess_verification_command_execution

_ADMITTED_ARGV = frozenset({("pytest",), ("pytest", "-q")})
_SAFE_ENV_KEYS = ("LANG", "LC_ALL", "LC_CTYPE", "TZ")
_PYTEST_ADDOPTS = "-p no:cacheprovider"
_SYSTEM_PATH_DIRS = (
    "/usr/local/sbin",
    "/usr/local/bin",
    "/usr/sbin",
    "/usr/bin",
    "/sbin",
    "/bin",
)

HostVerificationStatus = Literal["PASSED", "FAILED", "TIMED_OUT", "ERROR"]


class HostVerificationError(RuntimeError):
    """The caller supplied authority that this Phase-0 runner cannot execute."""


@runtime_checkable
class PytestExecutableIdentity(Protocol):
    """Import-neutral shape of the executable identity frozen at admission."""

    resolved_path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CapturedPytestExecutable:
    """Canonical private copy of an admitted pytest executable identity."""

    resolved_path: Path
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


@dataclass(frozen=True, slots=True)
class RetainedArtifact:
    """An immutable-on-return file binding suitable for Host evidence."""

    path: Path
    digest: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class HostVerificationBinding:
    """Canonical immutable ID/argv authority derived from a frozen plan command."""

    command_id: str
    argv: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HostVerificationResult:
    """Truthful execution facts for exactly one admitted verification command."""

    command_id: str
    argv: tuple[str, ...]
    status: HostVerificationStatus
    real_execution: bool
    exit_code: int | None
    timed_out: bool
    summary: str
    artifact: RetainedArtifact
    started_at: str
    finished_at: str
    elapsed_ms: int
    termination_signal: int | None = None

    @property
    def passed(self) -> bool:
        return (
            self.status == "PASSED"
            and self.real_execution
            and self.exit_code == 0
            and not self.timed_out
        )


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _beneath(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolve_workspace_directory(*, workspace_root: Path, pinned_cwd: Path) -> tuple[Path, Path]:
    try:
        root = workspace_root.expanduser().resolve(strict=True)
    except OSError as exc:
        raise HostVerificationError("the pinned workspace is unavailable") from exc
    if not root.is_dir():
        raise HostVerificationError("the pinned workspace is not a directory")

    candidate = pinned_cwd if pinned_cwd.is_absolute() else root / pinned_cwd
    try:
        cwd = candidate.expanduser().resolve(strict=True)
    except OSError as exc:
        raise HostVerificationError("the pinned verification cwd is unavailable") from exc
    if not cwd.is_dir() or not _beneath(cwd, root):
        raise HostVerificationError("the pinned verification cwd is outside the workspace")
    return root, cwd


def _resolve_artifact_path(*, workspace_root: Path, artifact_path: Path) -> Path:
    candidate = artifact_path if artifact_path.is_absolute() else workspace_root / artifact_path
    lexical = Path(os.path.abspath(os.fspath(candidate)))
    # Validate the closest existing ancestor *before* creating directories.
    # Resolving after mkdir is too late: an existing symlink component could
    # already have caused mkdir to create attacker-chosen directories outside
    # the workspace before the escape was noticed.  Rebuild the missing suffix
    # from that resolved ancestor so platform aliases such as macOS
    # ``/var`` -> ``/private/var`` compare in one namespace.
    if lexical.is_symlink():
        raise HostVerificationError("the verification artifact path is a symlink")
    ancestor = lexical.parent
    missing_parts: list[str] = []
    while not ancestor.exists() and not ancestor.is_symlink():
        parent = ancestor.parent
        if parent == ancestor:
            break
        missing_parts.append(ancestor.name)
        ancestor = parent
    try:
        resolved_ancestor = ancestor.resolve(strict=True)
    except OSError as exc:
        raise HostVerificationError("the verification artifact directory is unavailable") from exc
    if not _beneath(resolved_ancestor, workspace_root):
        raise HostVerificationError("the verification artifact directory escapes the workspace")
    resolved_parent_candidate = resolved_ancestor.joinpath(*reversed(missing_parts))
    if not _beneath(resolved_parent_candidate, workspace_root):
        raise HostVerificationError("the verification artifact is outside the workspace")
    try:
        resolved_parent_candidate.mkdir(parents=True, exist_ok=True)
        parent = resolved_parent_candidate.resolve(strict=True)
    except OSError as exc:
        raise HostVerificationError("the verification artifact directory is unavailable") from exc
    if not _beneath(parent, workspace_root):
        raise HostVerificationError("the verification artifact directory escapes the workspace")
    if lexical.is_symlink():
        raise HostVerificationError("the verification artifact path is a symlink")
    return parent / lexical.name


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def capture_pytest_executable_identity(
    executable: Path,
    *,
    workspace_root: Path,
) -> CapturedPytestExecutable:
    """Capture a stable executable identity suitable for admission tests/bridges."""

    try:
        trusted = executable.expanduser().resolve(strict=True)
        root = workspace_root.expanduser().resolve(strict=True)
        before = os.stat(trusted, follow_symlinks=False)
        digest = _sha256_file(trusted)
        after = os.stat(trusted, follow_symlinks=False)
    except OSError as exc:
        raise HostVerificationError("the pinned pytest executable is unavailable") from exc
    identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if identity_before != identity_after:
        raise HostVerificationError("the pinned pytest executable changed while being read")
    if not stat.S_ISREG(after.st_mode) or not os.access(trusted, os.X_OK):
        raise HostVerificationError("the pinned pytest executable is not executable")
    if _beneath(trusted, root):
        raise HostVerificationError("the pinned pytest executable must be outside the workspace")
    if not trusted.name.casefold().startswith(("pytest", "py.test")):
        raise HostVerificationError("the pinned executable is not pytest")
    return CapturedPytestExecutable(
        resolved_path=trusted,
        device=after.st_dev,
        inode=after.st_ino,
        size=after.st_size,
        mtime_ns=after.st_mtime_ns,
        sha256=digest,
    )


def _canonical_pytest_identity(
    identity: PytestExecutableIdentity,
) -> CapturedPytestExecutable:
    try:
        resolved_path = identity.resolved_path
        device = identity.device
        inode = identity.inode
        size = identity.size
        mtime_ns = identity.mtime_ns
        digest = identity.sha256
    except (AttributeError, TypeError) as exc:
        raise HostVerificationError("the pytest executable identity is incomplete") from exc
    if not isinstance(resolved_path, Path):
        raise HostVerificationError("the pytest executable identity path is invalid")
    integer_values = (device, inode, size, mtime_ns)
    if any(isinstance(value, bool) or not isinstance(value, int) for value in integer_values):
        raise HostVerificationError("the pytest executable identity metadata is invalid")
    normalized_digest = str(digest)
    if (
        len(normalized_digest) != 64
        or normalized_digest != normalized_digest.lower()
        or any(character not in "0123456789abcdef" for character in normalized_digest)
    ):
        raise HostVerificationError("the pytest executable identity digest is invalid")
    return CapturedPytestExecutable(
        resolved_path=resolved_path,
        device=device,
        inode=inode,
        size=size,
        mtime_ns=mtime_ns,
        sha256=normalized_digest,
    )


def _revalidate_pytest_identity(
    identity: CapturedPytestExecutable,
    *,
    workspace_root: Path,
) -> Path:
    current = capture_pytest_executable_identity(
        identity.resolved_path,
        workspace_root=workspace_root,
    )
    if current != identity:
        raise HostVerificationError("the pinned pytest executable identity changed")
    return current.resolved_path


def _controlled_path(
    *,
    pytest_executable: CapturedPytestExecutable,
    workspace_root: Path,
) -> tuple[Path, str]:
    trusted = _revalidate_pytest_identity(
        pytest_executable,
        workspace_root=workspace_root,
    )

    entries: list[str] = [os.fspath(trusted.parent)]
    for raw in _SYSTEM_PATH_DIRS:
        path = Path(raw)
        if path.is_dir() and raw not in entries:
            entries.append(raw)
    path_value = os.pathsep.join(entries)
    resolved = shutil.which("pytest", path=path_value)
    if resolved is None:
        raise HostVerificationError("controlled PATH cannot resolve the pinned pytest executable")
    try:
        resolved_path = Path(resolved).resolve(strict=True)
    except OSError as exc:
        raise HostVerificationError(
            "controlled PATH resolved an unavailable pytest executable"
        ) from exc
    if resolved_path != trusted:
        raise HostVerificationError("controlled PATH resolves pytest to a different executable")
    return trusted, path_value


def _revalidate_controlled_pytest(
    identity: CapturedPytestExecutable,
    *,
    workspace_root: Path,
    path_value: str,
) -> None:
    trusted = _revalidate_pytest_identity(identity, workspace_root=workspace_root)
    resolved = shutil.which("pytest", path=path_value)
    if resolved is None:
        raise HostVerificationError("controlled PATH no longer resolves pytest")
    try:
        resolved_path = Path(resolved).resolve(strict=True)
    except OSError as exc:
        raise HostVerificationError("controlled PATH pytest became unavailable") from exc
    if resolved_path != trusted:
        raise HostVerificationError("controlled PATH pytest identity changed")


def _controlled_environment(
    *,
    path_value: str,
    base_environment: Mapping[str, str] | None,
) -> dict[str, str]:
    source = base_environment if base_environment is not None else os.environ
    env = {
        key: str(source[key])
        for key in _SAFE_ENV_KEYS
        if key in source and str(source[key]).strip()
    }
    env.update(
        {
            "PATH": path_value,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            # Phase 0 admits an exact repository-native argv.  Cache suppression
            # is a fixed Host execution-profile property, not caller-controlled
            # command authority, and prevents pytest from mutating the checkout.
            "PYTEST_ADDOPTS": _PYTEST_ADDOPTS,
        }
    )
    return env


def retained_artifact_matches(
    artifact: RetainedArtifact,
    *,
    containment_root: Path,
) -> bool:
    """Return whether a retained artifact still matches its live file binding.

    The pre-merge gate calls this immediately before accepting verification.
    It rejects missing files, final-component symlinks, non-regular files,
    paths outside the protected verification directory, and changed content.
    A before/after stat comparison also detects replacement during the read.
    """

    try:
        root = containment_root.expanduser().resolve(strict=True)
        candidate = artifact.path.expanduser()
        if not candidate.is_absolute() or candidate.is_symlink():
            return False
        before = candidate.stat()
        resolved = candidate.resolve(strict=True)
        if not _beneath(resolved, root) or not stat.S_ISREG(before.st_mode):
            return False
        data = candidate.read_bytes()
        after = candidate.stat()
    except (OSError, RuntimeError):
        return False
    stable_identity = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    return (
        stable_identity
        and len(data) == artifact.size_bytes
        and hashlib.sha256(data).hexdigest() == artifact.digest
    )


def _validate_admitted_command(
    command_id: str,
    command: VerificationCommand,
) -> tuple[str, tuple[str, ...]]:
    if not isinstance(command, VerificationCommand):
        raise HostVerificationError("verification command binding has the wrong type")
    # Pydantic's frozen model prevents attribute replacement, but nested lists
    # remain mutable.  Serialize once, then strict-validate a private clone so
    # later caller mutation (including from another thread) cannot change the
    # ID/argv/profile facts this invocation observes.
    try:
        command = VerificationCommand.model_validate_json(
            command.model_dump_json(by_alias=True),
            strict=True,
        )
    except Exception as exc:  # noqa: BLE001 - fail closed at the authority boundary
        raise HostVerificationError("verification command binding is not canonical") from exc
    command_key = str(command_id).strip()
    if not command_key or command.command_id != command_key:
        raise HostVerificationError("verification command ID does not match its admitted binding")
    admitted = tuple(command.argv)
    if admitted not in _ADMITTED_ARGV or any(not isinstance(item, str) for item in admitted):
        raise HostVerificationError("verification argv is not admitted by Phase 0")
    canonical_text = shlex.join(admitted)
    analysis = analyze_verification_command(
        canonical_text,
        source="repo_scan.likely_test_commands",
        contract_type="repo_native",
        trusted=True,
    )
    if (
        command.original_text != canonical_text
        or command.display_text != canonical_text
        or command.execution_mode != "ARGV"
        or command.provenance not in {"PREEXISTING_REPO_NATIVE", "HOST_AUTHORITATIVE"}
        or command.trust_level != "TRUSTED"
        or command.requirement != "REQUIRED"
        or command.validation_status != "VALID"
        or bool(command.rejection_reason)
        or command.evidentiary_capability != "ASSERTIVE"
        or analysis.parts != admitted
        or analysis.command_family != "pytest"
        or analysis.shell_control_flow != "none"
        or not analysis.is_valid_verifier
        or analysis.rejection_reason
        or analysis.evidentiary_capability != VerificationCommandEvidentiaryCapability.ASSERTIVE
    ):
        raise HostVerificationError("verification argv failed authoritative analysis")
    return command_key, admitted


def bind_host_verification_command(
    command_id: str,
    command: VerificationCommand,
) -> HostVerificationBinding:
    """Create the immutable ID/argv binding used at the pre-merge gate."""
    command_key, argv = _validate_admitted_command(command_id, command)
    return HostVerificationBinding(command_id=command_key, argv=argv)


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _reap_new_groups(registry: ProcessGroupRegistry, before: frozenset[int]) -> tuple[int, ...]:
    survivors: list[int] = []
    for record in registry.live():
        if record.pgid in before:
            continue
        try:
            reap_process_group(record)
        except Exception:  # noqa: BLE001 - report the survivor in retained evidence
            pass
        if process_group_exists(record.pgid):
            survivors.append(record.pgid)
        else:
            registry.release(record.pgid)
    return tuple(survivors)


def _artifact_payload(
    *,
    command_id: str,
    argv: tuple[str, ...],
    status: HostVerificationStatus,
    real_execution: bool,
    exit_code: int | None,
    timed_out: bool,
    summary: str,
    stdout: str,
    stderr: str,
    started_at: str,
    finished_at: str,
    elapsed_ms: int,
    termination_signal: int | None,
) -> dict[str, object]:
    return {
        "schema": "alysis.forge-host.verification-artifact/v1",
        "commandId": command_id,
        "argv": list(argv),
        "status": status,
        "realExecution": real_execution,
        "exitCode": exit_code,
        "timedOut": timed_out,
        "summary": summary,
        "startedAt": started_at,
        "finishedAt": finished_at,
        "elapsedMs": elapsed_ms,
        "terminationSignal": termination_signal,
        "stdout": stdout,
        "stderr": stderr,
    }


def _retain_artifact(path: Path, payload: Mapping[str, object]) -> RetainedArtifact:
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, text)
    return _bind_artifact(path)


def _bind_artifact(path: Path) -> RetainedArtifact:
    data = path.read_bytes()
    return RetainedArtifact(
        path=path,
        digest=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
    )


def run_host_verification(
    command_id: str,
    *,
    admitted_command: VerificationCommand,
    workspace_root: Path,
    pinned_cwd: Path,
    artifact_path: Path,
    execution_deadline: ExecutionDeadline,
    pytest_executable: PytestExecutableIdentity,
    base_environment: Mapping[str, str] | None = None,
    process_registry: ProcessGroupRegistry | None = None,
) -> HostVerificationResult:
    """Execute one exact Phase-0 pytest argv and retain its complete artifact.

    ``admitted_command`` is the immutable command from the approved
    :class:`PlanReady` snapshot.  Its ID and argv stay one binding and the
    caller supplies no alternate argv.  The argv is reported exactly as
    admitted.  Executable pinning happens only
    through the controlled ``PATH`` after proving that literal ``pytest``
    resolves to the executable identity captured at admission.  No shell,
    wrapper, retry, fallback, focused
    target, or plugin option is available at this boundary.
    """

    started_at = _now_iso()
    started_elapsed = execution_deadline.elapsed_seconds()
    binding = bind_host_verification_command(command_id, admitted_command)
    command_key = binding.command_id
    admitted = binding.argv
    executable_identity = _canonical_pytest_identity(pytest_executable)
    if not execution_deadline.enabled:
        raise HostVerificationError("Phase 0 verification requires a bounded run deadline")
    if not process_groups_supported():  # pragma: no cover - Windows fail-closed boundary
        raise HostVerificationError("safe verification process-tree cleanup is unavailable")
    root, cwd = _resolve_workspace_directory(
        workspace_root=workspace_root,
        pinned_cwd=pinned_cwd,
    )
    retained_path = _resolve_artifact_path(
        workspace_root=root,
        artifact_path=artifact_path,
    )
    _trusted, path_value = _controlled_path(
        pytest_executable=executable_identity,
        workspace_root=root,
    )
    env = _controlled_environment(
        path_value=path_value,
        base_environment=base_environment,
    )

    registry = process_registry or ProcessGroupRegistry()
    before_groups = frozenset(record.pgid for record in registry.tracked())
    stdout = ""
    stderr = ""
    exit_code: int | None = None
    status: HostVerificationStatus = "ERROR"
    real_execution = False
    timed_out = False
    termination_signal: int | None = None
    summary = "pytest verification did not execute"
    cleanup_survivors: tuple[int, ...] = ()

    try:
        timeout = deadline_timeout_or_raise(
            execution_deadline,
            None,
            reserve_seconds=0.0,
            operation=DeadlineOperation.VERIFICATION.value,
        )
        try:
            # The model phase may be long.  Revalidate both bytes/metadata and
            # literal PATH resolution at the last boundary before subprocess
            # creation so admission-time executable replacement fails closed.
            _revalidate_controlled_pytest(
                executable_identity,
                workspace_root=root,
                path_value=path_value,
            )
            completed = run_in_tracked_process_group(
                list(admitted),
                shell=False,
                cwd=os.fspath(cwd),
                env=env,
                timeout=timeout,
                registry=registry,
                origin="forge_host_v2:verification",
                command_label=shlex.join(admitted),
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired as exc:
            stdout = _text(exc.stdout)
            stderr = _text(exc.stderr)
            timed_out = True
            real_execution = True
            status = "TIMED_OUT"
            summary = "pytest verification timed out"
        except OSError as exc:
            stderr = sanitize_error_summary(str(exc))
            status = "ERROR"
            summary = "pytest verification could not start"
        else:
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
            # Do not accept a zero exit from an executable that was replaced
            # while its process was running (or by a compromised runner shim).
            _revalidate_controlled_pytest(
                executable_identity,
                workspace_root=root,
                path_value=path_value,
            )
            raw_returncode = int(completed.returncode)
            termination_signal = -raw_returncode if raw_returncode < 0 else None
            exit_code = raw_returncode if 0 <= raw_returncode <= 255 else None
            assessment = assess_verification_command_execution(
                command=shlex.join(admitted),
                exit_code=raw_returncode,
                output=stdout + stderr,
            )
            real_execution = (
                assessment.real_execution is True
                or raw_returncode == 1
                or termination_signal is not None
            )
            timed_out = execution_deadline.is_exhausted()
            if timed_out:
                status = "TIMED_OUT"
                summary = "pytest verification exceeded the run deadline"
            elif raw_returncode == 0 and real_execution:
                status = "PASSED"
                summary = "pytest verification passed"
            elif termination_signal is not None:
                status = "FAILED"
                summary = f"pytest verification terminated by signal {termination_signal}"
            else:
                status = "FAILED"
                summary = f"pytest verification failed with exit code {raw_returncode}"
    except DeadlineExhausted:
        timed_out = True
        status = "TIMED_OUT"
        summary = "run deadline exhausted before pytest verification"
    except Exception as exc:  # noqa: BLE001 - return a retained failure fact
        stderr = sanitize_error_summary(str(exc))
        status = "ERROR"
        summary = "pytest verification failed internally"
    finally:
        cleanup_survivors = _reap_new_groups(registry, before_groups)

    if cleanup_survivors:
        status = "ERROR"
        summary = "pytest verification process cleanup did not complete"

    # Retention is part of verification, not post-processing outside its
    # deadline.  Write a provisional complete artifact first, then measure the
    # shared deadline again and atomically finalize both result and artifact
    # with the same overall timing/status facts.
    provisional_elapsed = max(
        0.0,
        execution_deadline.elapsed_seconds() - started_elapsed,
    )
    provisional_elapsed_ms = max(0, int(round(provisional_elapsed * 1000.0)))
    provisional_finished_at = _now_iso()
    provisional_summary = sanitize_error_summary(summary, max_chars=320)
    _retain_artifact(
        retained_path,
        _artifact_payload(
            command_id=command_key,
            argv=admitted,
            status=status,
            real_execution=real_execution,
            exit_code=exit_code,
            timed_out=timed_out,
            summary=provisional_summary,
            stdout=stdout,
            stderr=stderr,
            started_at=started_at,
            finished_at=provisional_finished_at,
            elapsed_ms=provisional_elapsed_ms,
            termination_signal=termination_signal,
        ),
    )

    # A final write or digest read can itself cross the deadline.  Finalize
    # until the terminal facts observed after persistence match what was
    # written.  The bounded retry normally also converges elapsedMs after a
    # one-off slow write.  If ordinary write latency keeps advancing the rounded
    # clock, the persisted pre-write timing remains authoritative so the result
    # and retained artifact can never disagree.
    artifact: RetainedArtifact | None = None
    finished_at = provisional_finished_at
    elapsed_seconds = provisional_elapsed
    elapsed_ms = provisional_elapsed_ms
    terminal_signature_changed = False
    for attempt in range(4):
        finished_elapsed = execution_deadline.elapsed_seconds()
        written_elapsed_seconds = max(0.0, finished_elapsed - started_elapsed)
        written_elapsed_ms = max(0, int(round(written_elapsed_seconds * 1000.0)))
        if execution_deadline.is_exhausted():
            timed_out = True
            if status != "ERROR":
                status = "TIMED_OUT"
                summary = "pytest verification exceeded the run deadline"
        written_finished_at = _now_iso()
        summary = sanitize_error_summary(summary, max_chars=320)
        written_signature = (status, timed_out)
        final_payload = _artifact_payload(
            command_id=command_key,
            argv=admitted,
            status=status,
            real_execution=real_execution,
            exit_code=exit_code,
            timed_out=timed_out,
            summary=summary,
            stdout=stdout,
            stderr=stderr,
            started_at=started_at,
            finished_at=written_finished_at,
            elapsed_ms=written_elapsed_ms,
            termination_signal=termination_signal,
        )
        atomic_write_text(
            retained_path,
            json.dumps(final_payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        )
        artifact = _bind_artifact(retained_path)

        post_elapsed = max(
            0.0,
            execution_deadline.elapsed_seconds() - started_elapsed,
        )
        post_timed_out = timed_out or execution_deadline.is_exhausted()
        post_status = "TIMED_OUT" if post_timed_out and status != "ERROR" else status
        post_elapsed_ms = max(0, int(round(post_elapsed * 1000.0)))
        terminal_signature_changed = (post_status, post_timed_out) != written_signature
        if terminal_signature_changed:
            status = post_status
            timed_out = post_timed_out
            if status == "TIMED_OUT":
                summary = "pytest verification exceeded the run deadline"
            continue
        if post_elapsed_ms != written_elapsed_ms and attempt < 3:
            # Persist the newly observed overall timing.  This converges in the
            # common case while preserving a hard bound for pathological I/O.
            continue
        status, timed_out = written_signature
        finished_at = written_finished_at
        elapsed_seconds = written_elapsed_seconds
        elapsed_ms = written_elapsed_ms
        terminal_signature_changed = False
        break

    if terminal_signature_changed:
        # Deadline state changes only once.  If it happened on the last bounded
        # attempt, persist that monotonic terminal state one final time and use
        # the exact pre-write timing stored in the artifact.
        finished_elapsed = execution_deadline.elapsed_seconds()
        elapsed_seconds = max(0.0, finished_elapsed - started_elapsed)
        elapsed_ms = max(0, int(round(elapsed_seconds * 1000.0)))
        timed_out = timed_out or execution_deadline.is_exhausted()
        if timed_out and status != "ERROR":
            status = "TIMED_OUT"
            summary = "pytest verification exceeded the run deadline"
        finished_at = _now_iso()
        summary = sanitize_error_summary(summary, max_chars=320)
        atomic_write_text(
            retained_path,
            json.dumps(
                _artifact_payload(
                    command_id=command_key,
                    argv=admitted,
                    status=status,
                    real_execution=real_execution,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    summary=summary,
                    stdout=stdout,
                    stderr=stderr,
                    started_at=started_at,
                    finished_at=finished_at,
                    elapsed_ms=elapsed_ms,
                    termination_signal=termination_signal,
                ),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        artifact = _bind_artifact(retained_path)
    assert artifact is not None
    execution_deadline.observe_duration(DeadlineOperation.VERIFICATION, elapsed_seconds)
    return HostVerificationResult(
        command_id=command_key,
        argv=admitted,
        status=status,
        real_execution=real_execution,
        exit_code=exit_code,
        timed_out=timed_out,
        summary=summary,
        artifact=artifact,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=elapsed_ms,
        termination_signal=termination_signal,
    )


__all__ = [
    "CapturedPytestExecutable",
    "HostVerificationBinding",
    "HostVerificationError",
    "HostVerificationResult",
    "HostVerificationStatus",
    "PytestExecutableIdentity",
    "RetainedArtifact",
    "bind_host_verification_command",
    "capture_pytest_executable_identity",
    "retained_artifact_matches",
    "run_host_verification",
]
