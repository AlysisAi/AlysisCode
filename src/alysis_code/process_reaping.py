"""Tracked process-group lifecycle for agent-spawned shell commands.

A shell command that outlives its tool call keeps consuming the machine. The
observed failure mode is a timed-out test run: ``subprocess.run(timeout=...)``
kills the direct child (the shell) and returns, but the shell's *children* --
the pytest workers -- are never signalled and keep running. On a shared or
benchmarked host they then compete with whatever runs next.

The fix is ownership, not discovery. Every command the runner starts is placed
in its own POSIX process group (``start_new_session``), and the resulting pgid
is recorded here at spawn time. Reaping later signals *only* those recorded
pgids.

Hard safety rule, enforced structurally by this module:

* ``ProcessGroupRegistry`` can only learn a pgid through :meth:`register`,
  which the runner calls with the pgid of a process it just created.
* Process names/argv never authorize signals. Seatbelt callers supply an owner
  that queries inherited kernel policy on same-user PIDs, then rechecks creation
  identities before signalling. Legacy callers may retain ancestry observation.
  Neither mechanism is an OS job object or atomic PID handle.
* A recorded pgid is refused if it is not a positive integer, if it is this
  process's own group (a defensive check for a host where session creation
  silently failed), or if the group leader's start token changed since
  registration. That guard reads ``/proc`` on Linux and libproc on macOS;
  on other BSD hosts the residual exposure is a pgid recycled in the
  window between a tool call returning and the group being released, which is
  the same window ``release_if_finished`` closes on the very next statement.

The reaping guarantee is scoped to the session that did the spawning: a session
never signals another session's groups, and an interactive session's own turn
never kills groups that session started. A nested delegation (subagent or
worker) still cleans up what *it* started when it ends -- its work is over,
and the groups belong to it.

Everything except the signalling helpers is pure and unit-testable without
spawning a process. The platform guard is a separate function so the tracking
and decision logic stay exercisable on any host, mirroring the PIPESTATUS
capture guard in :mod:`alysis_code.pipeline_facts`.
"""

from __future__ import annotations

import inspect
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from .branding import env_get
from .command_text import command_text, decode_command_output
from .macos_process_tree import MacProcessTree, process_identity
from .macos_sandbox_owner import MacSandboxOwner
from .runtime_kind import RuntimeKind, normalize_runtime_kind

# Grace period between SIGTERM and SIGKILL for a tracked group.
PROCESS_REAP_GRACE_SECONDS = 5.0
_GROUP_EXIT_POLL_SECONDS = 0.02

# How often a command run with a ``cancel_check`` reads it. The wait still ends
# the moment the command exits, so this only bounds how long a cancelled command
# keeps running; a desktop host gives a Stop 1.5 s to be confirmed.
CANCEL_CHECK_INTERVAL_SECONDS = 0.1
# SIGTERM grace for a cancelled command's group, within that 1.5 s.
_CANCEL_REAP_GRACE_SECONDS = 0.5

SIGNAL_NONE = "none"
SIGNAL_TERM = "SIGTERM"
SIGNAL_KILL = "SIGKILL"


def process_groups_supported() -> bool:
    """True when the host has POSIX process groups that can be signalled.

    Windows has no ``killpg``; command tracking degrades to a no-op there. Kept
    as a separate function so tests can exercise the tracking and reaping paths
    on any host (mirrors ``_pipeline_capture_platform_ok`` in the shell tool).
    """
    return os.name != "nt"


def _process_reaping_enabled(cfg: Any | None) -> bool:
    """Kill-switch for process reaping (step 5).

    ``ALYSIS_PROCESS_REAPING`` (off/0/false/no/disabled) wins over the config
    value; default is on. When off, commands still run in their own process
    group but nothing is ever signalled and no reaping telemetry is emitted --
    the legacy leave-it-running behaviour.
    """
    env_value = env_get("ALYSIS_PROCESS_REAPING")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "process_reaping_enabled", True))


# ---------------------------------------------------------------------------
# Reaping decision (pure)
# ---------------------------------------------------------------------------


class ReapEvent(StrEnum):
    TURN_FINALIZATION = "turn_finalization"
    SESSION_CLOSE = "session_close"


class ReapAction(StrEnum):
    REAP = "reap"
    REPORT = "report"
    SKIP = "skip"


@dataclass(frozen=True)
class ReapDecision:
    action: ReapAction
    reason: str


def _runtime_kind_is_interactive(kind: RuntimeKind | str | None) -> bool:
    try:
        normalized = normalize_runtime_kind(kind, fallback=RuntimeKind.INTERACTIVE_CHAT)
    except Exception:  # noqa: BLE001 - an unknown kind is treated as interactive (never auto-kill)
        return True
    return normalized is RuntimeKind.INTERACTIVE_CHAT


def resolve_reap_decision(
    *,
    runtime_kind: RuntimeKind | str | None,
    event: ReapEvent | str,
    enabled: bool,
) -> ReapDecision:
    """Decide what to do with tracked live groups at ``event``. Pure.

    The table is deliberately small and asymmetric:

    ==================  ==================  ==============
    runtime kind        turn finalization   session close
    ==================  ==================  ==============
    interactive_chat    report              reap
    everything else     reap                reap
    ==================  ==================  ==============

    An interactive turn is never auto-killed: a user may have asked for a dev
    server and expects it to outlive the turn. Their still-running groups are
    reported instead, and reaped when the session itself ends. Every autonomous
    runtime kind (one-shot, forge, swarm worker, subagent, or conflict resolver)
    declares itself done at turn end, so anything it left
    running is a leak and is terminated.
    """
    if not enabled:
        return ReapDecision(ReapAction.SKIP, "process_reaping_disabled")
    normalized_event = (
        event if isinstance(event, ReapEvent) else ReapEvent(str(event).strip().lower())
    )
    if normalized_event is ReapEvent.SESSION_CLOSE:
        return ReapDecision(ReapAction.REAP, "session_close")
    if _runtime_kind_is_interactive(runtime_kind):
        return ReapDecision(ReapAction.REPORT, "interactive_turn_keeps_user_processes")
    return ReapDecision(ReapAction.REAP, "autonomous_turn_finalization")


# ---------------------------------------------------------------------------
# Tracked groups
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrackedProcessGroup:
    """One process group this runner created and has not seen exit."""

    pgid: int
    command: str
    origin: str
    started_at: float
    start_token: str | None = None
    process_tree: MacProcessTree | MacSandboxOwner | None = field(
        default=None, compare=False, repr=False
    )

    def runtime_s(self, *, now: float | None = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, round(current - self.started_at, 3))

    def payload(self, *, now: float | None = None) -> dict[str, Any]:
        return {
            "pgid": self.pgid,
            "command": self.command,
            "origin": self.origin,
            "runtime_s": self.runtime_s(now=now),
        }


@dataclass(frozen=True)
class ProcessReapOutcome:
    pgid: int
    command: str
    origin: str
    runtime_s: float
    signal_used: str

    def payload(self) -> dict[str, Any]:
        return {
            "pgid": self.pgid,
            "command": self.command,
            "origin": self.origin,
            "runtime_s": self.runtime_s,
            "signal_used": self.signal_used,
        }


class ProcessGroupRegistry:
    """Records the process groups this runner created.

    This is the *only* source of pgids that may ever be signalled. It is
    per-session, so a subagent never reaps its parent's groups and vice versa.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._groups: dict[int, TrackedProcessGroup] = {}
        self._command_cleanups: set[_WindowsCommandCleanup] = set()
        self._cleanup_callbacks: list[Callable[[], None]] = []

    def defer_cleanup(self, callback: Callable[[], None]) -> bool:
        """Retain dependent resources until all owned command cleanup completes.

        False leaves cleanup with the caller. True transfers it to the retained
        cleanup owner, including when stopping a process is currently denied.
        """
        with self._lock:
            if not self._command_cleanups:
                return False
            self._cleanup_callbacks.append(callback)
            return True

    def _finish_command_cleanup(self, cleanup: _WindowsCommandCleanup) -> None:
        with self._lock:
            self._command_cleanups.discard(cleanup)
            callbacks = []
            if not self._command_cleanups:
                callbacks, self._cleanup_callbacks = self._cleanup_callbacks, []
        # Keep failed callbacks with their cleanup owner rather than dropping
        # a staging directory whose deletion was denied.
        cleanup.callbacks.extend(callbacks)
        while cleanup.callbacks:
            cleanup.callbacks[0]()
            cleanup.callbacks.pop(0)

    def register(
        self,
        *,
        pgid: int,
        command: str,
        origin: str,
        track_descendants: bool = False,
        process_owner: MacSandboxOwner | None = None,
    ) -> TrackedProcessGroup | None:
        """Record a group this runner just created. Returns ``None`` if refused."""
        if not _pgid_is_signalable(pgid):
            return None
        record = TrackedProcessGroup(
            pgid=int(pgid),
            command=str(command or ""),
            origin=str(origin or "shell_run"),
            started_at=time.monotonic(),
            start_token=_pid_start_token(int(pgid)),
            process_tree=process_owner
            or (MacProcessTree.start(pgid) if track_descendants else None),
        )
        with self._lock:
            self._groups[record.pgid] = record
        return record

    def release(self, pgid: int) -> None:
        """Forget a group unconditionally (the caller knows it is finished)."""
        with self._lock:
            record = self._groups.pop(int(pgid), None)
        if record is not None and record.process_tree is not None:
            record.process_tree.close()

    def release_if_finished(self, pgid: int) -> bool:
        """Drop the group when it no longer exists; keep it when it is alive.

        Called when a tool call returns. A plain command that ran to completion
        is dropped here; a command that timed out, was backgrounded with ``&``,
        or spawned a daemon leaves a live group and stays tracked for reaping.
        """
        with self._lock:
            record = self._groups.get(int(pgid))
        tree_alive = (
            record is not None and record.process_tree is not None and record.process_tree.alive()
        )
        if not tree_alive and not process_group_exists(int(pgid)):
            self.release(pgid)
            return True
        return False

    def tracked(self) -> tuple[TrackedProcessGroup, ...]:
        with self._lock:
            return tuple(self._groups.values())

    def live(self) -> tuple[TrackedProcessGroup, ...]:
        """Tracked groups that still exist, dropping any that have since exited."""
        alive: list[TrackedProcessGroup] = []
        for record in self.tracked():
            if (process_group_exists(record.pgid) and _start_token_matches(record)) or (
                record.process_tree is not None and record.process_tree.alive()
            ):
                alive.append(record)
            else:
                self.release(record.pgid)
        return tuple(alive)

    def clear(self) -> None:
        for record in self.tracked():
            self.release(record.pgid)


# ---------------------------------------------------------------------------
# POSIX signalling (the only impure part)
# ---------------------------------------------------------------------------


def _pgid_is_signalable(pgid: Any) -> bool:
    """Reject anything that must never be signalled, before it is ever recorded."""
    # Strict about the type: a pgid comes from os.getpgid, so anything that is
    # not already an int reached this from somewhere it should not have, and
    # coercing it would launder that mistake into a signal.
    if not isinstance(pgid, int) or isinstance(pgid, bool):
        return False
    candidate = pgid
    if candidate <= 1:
        return False
    if not process_groups_supported():
        return False
    try:
        own_group = os.getpgrp()
    except (AttributeError, OSError):  # pragma: no cover - exercised on Windows
        return False
    # A pgid equal to our own group means session creation did not take effect;
    # signalling it would kill the agent itself.
    return candidate != own_group


def _pid_start_token(pid: int) -> str | None:
    """Kernel process creation time on Linux/macOS, used to detect PID reuse."""
    if sys.platform == "darwin":
        identity = process_identity(pid)
        return identity.token if identity is not None else None
    stat_path = Path("/proc") / str(pid) / "stat"
    try:
        text = stat_path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        after_comm = text.rsplit(") ", 1)[1]
        fields = after_comm.split()
        return f"linux:{fields[19]}"
    except (IndexError, ValueError):
        return None


def _start_token_matches(record: TrackedProcessGroup) -> bool:
    """False when the group leader was replaced by an unrelated recycled PID."""
    if record.start_token is None:
        return True
    current = _pid_start_token(record.pgid)
    if current is None:
        return True
    return current == record.start_token


def resolve_process_group_id(pid: int) -> int | None:
    """Process-group id of a process we started, or ``None`` when unavailable."""
    if not process_groups_supported():  # pragma: no cover - exercised on Windows
        return None
    if pid <= 0:
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        # The leader already exited; with start_new_session its pid is the pgid.
        return pid


def process_group_exists(pgid: int) -> bool:
    if not process_groups_supported():  # pragma: no cover - exercised on Windows
        return False
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def signal_process_group(pgid: int, signum: int) -> bool:
    if not process_groups_supported():  # pragma: no cover - exercised on Windows
        return False
    try:
        os.killpg(pgid, signum)
    except ProcessLookupError:
        return True
    except OSError:
        return False
    return True


def _wait_for_group_exit(pgid: int, *, timeout_s: float) -> bool:
    deadline = time.monotonic() + max(0.0, timeout_s)
    while time.monotonic() < deadline:
        if not process_group_exists(pgid):
            return True
        time.sleep(_GROUP_EXIT_POLL_SECONDS)
    return not process_group_exists(pgid)


def reap_process_group(
    record: TrackedProcessGroup,
    *,
    grace_seconds: float = PROCESS_REAP_GRACE_SECONDS,
) -> ProcessReapOutcome:
    """SIGTERM, bounded grace, then SIGKILL. Only ever called for a tracked group."""
    import signal as signal_module

    runtime_s = record.runtime_s()
    tree_signal = SIGNAL_NONE
    if record.process_tree is not None and record.process_tree.alive():
        record.process_tree.terminate(grace_seconds)
        tree_signal = SIGNAL_KILL
    if not _pgid_is_signalable(record.pgid) or not _start_token_matches(record):
        return ProcessReapOutcome(
            pgid=record.pgid,
            command=record.command,
            origin=record.origin,
            runtime_s=runtime_s,
            signal_used=tree_signal,
        )
    if not process_group_exists(record.pgid):
        return ProcessReapOutcome(
            pgid=record.pgid,
            command=record.command,
            origin=record.origin,
            runtime_s=runtime_s,
            signal_used=tree_signal,
        )
    signal_process_group(record.pgid, signal_module.SIGTERM)
    if _wait_for_group_exit(record.pgid, timeout_s=grace_seconds):
        return ProcessReapOutcome(
            pgid=record.pgid,
            command=record.command,
            origin=record.origin,
            runtime_s=runtime_s,
            signal_used=SIGNAL_TERM,
        )
    signal_process_group(record.pgid, signal_module.SIGKILL)
    _wait_for_group_exit(record.pgid, timeout_s=1.0)
    return ProcessReapOutcome(
        pgid=record.pgid,
        command=record.command,
        origin=record.origin,
        runtime_s=runtime_s,
        signal_used=SIGNAL_KILL,
    )


def reap_tracked_groups(
    registry: ProcessGroupRegistry | None,
    *,
    grace_seconds: float = PROCESS_REAP_GRACE_SECONDS,
) -> tuple[ProcessReapOutcome, ...]:
    """Terminate every tracked group that is still alive. Never raises."""
    if registry is None:
        return ()
    outcomes: list[ProcessReapOutcome] = []
    for record in registry.live():
        try:
            outcome = reap_process_group(record, grace_seconds=grace_seconds)
        except Exception:  # noqa: BLE001 - teardown must never break finalization
            outcome = ProcessReapOutcome(
                pgid=record.pgid,
                command=record.command,
                origin=record.origin,
                runtime_s=record.runtime_s(),
                signal_used=SIGNAL_NONE,
            )
        if record.process_tree is None:
            registry.release(record.pgid)
        else:
            registry.release_if_finished(record.pgid)
        outcomes.append(outcome)
    return tuple(outcomes)


def survivor_payloads(registry: ProcessGroupRegistry | None) -> tuple[dict[str, Any], ...]:
    """Payloads for tracked groups still alive, without touching them."""
    if registry is None:
        return ()
    now = time.monotonic()
    return tuple(record.payload(now=now) for record in registry.live())


# ---------------------------------------------------------------------------
# Spawning inside a tracked group
# ---------------------------------------------------------------------------


def new_process_group_popen_kwargs() -> dict[str, Any]:
    """Popen kwargs that put the child in its own process group.

    POSIX only. On Windows a new console process group would detach the child
    from console Ctrl-C without buying anything back -- there is no ``killpg``,
    so nothing could be reaped -- so the guard makes this a true no-op there and
    foreground commands keep behaving exactly as they did.
    """
    if not process_groups_supported():  # pragma: no cover - exercised on Windows
        return {}
    return {"start_new_session": True}


def _describe_command(args: Any) -> str:
    if isinstance(args, str):
        return args
    try:
        return " ".join(str(item) for item in args)
    except TypeError:
        return str(args)


class CommandCleanupError(RuntimeError):
    """The command failed to stop conclusively; its output remains diagnostic evidence."""

    def __init__(self, cmd: str, *, stdout: str = "", stderr: str = "") -> None:
        super().__init__("Sandbox process cleanup could not be confirmed.")
        self.cmd = cmd
        self.stdout = stdout
        self.stderr = stderr


class CommandCancelledError(subprocess.SubprocessError):
    """A command's ``cancel_check`` fired before the command finished.

    Raised like :class:`subprocess.TimeoutExpired`: the caller stops the command,
    may set ``stdout`` and ``stderr`` to what it printed until then, and re-raises.
    """

    def __init__(self, cmd: Any, stdout: Any = None, stderr: Any = None) -> None:
        super().__init__(cmd)
        self.cmd = cmd
        self.stdout = stdout
        self.stderr = stderr

    def __str__(self) -> str:
        return f"Command '{_describe_command(self.cmd)}' was cancelled"


def runner_takes_cancel_check(runner: Any) -> bool:
    """True when ``runner.run`` accepts ``cancel_check``.

    Callers pass it only then, so runners written before it (test doubles among
    them) keep working.
    """
    try:
        parameters = inspect.signature(runner.run).parameters.values()
    except (AttributeError, TypeError, ValueError):
        return False
    return any(
        parameter.name == "cancel_check" or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _stop_windows_process_tree(
    process: subprocess.Popen[Any],
) -> list[Any] | None:  # pragma: no cover
    """End a command and the processes it started, at once (Windows).

    ``taskkill /T`` takes most of a second (about 0.8 s measured), more than half
    of what a Stop may take. ``psutil`` finds the children in one process snapshot
    and ends them directly. We hold the command's process handle, so its pid has
    not been reused; ``taskkill`` stays the fallback.
    Return the observed children so the cleanup owner can confirm their exit.
    None means the inventory could not be established, even if fallback stops
    the direct process and its pipes close.
    """
    try:
        import psutil

        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
        family = [*reversed(children), parent]
    except Exception:  # noqa: BLE001 - fall back to the slower, older way
        from .terminal_manager import _kill_windows_process_tree

        _kill_windows_process_tree(process)
        return None
    for member in family:
        try:
            member.kill()
        except psutil.Error:
            pass
    return children


_windows_cleanup_lock = threading.Lock()
_windows_command_cleanups: set[_WindowsCommandCleanup] = set()


class _WindowsCommandCleanup:
    """Own handles and dependent resources even if starting a thread fails."""

    def __init__(self, process: subprocess.Popen[Any], registry: ProcessGroupRegistry):
        self.process = process
        self.registry = registry
        self.finished = threading.Event()
        self.clean = False
        self.thread_started = False
        self.output: tuple[Any, Any] | None = None
        self.error: BaseException | None = None
        self.children: list[Any] | None = None
        self.callbacks: list[Callable[[], None]] = []
        with _windows_cleanup_lock:
            _windows_command_cleanups.add(self)
        with registry._lock:
            registry._command_cleanups.add(self)

    def run(self) -> None:
        try:
            try:
                self.children = _stop_windows_process_tree(self.process)
                self.output = self.process.communicate()
            finally:
                self.process.__exit__(*sys.exc_info())
            if self.children is None:
                return  # Unknown descendants remain explicitly pending.
            if self.children:
                import psutil

                _, pending = psutil.wait_procs(self.children)
                if pending:
                    return
            self.registry._finish_command_cleanup(self)
            self.clean = True
            with _windows_cleanup_lock:
                _windows_command_cleanups.discard(self)
        except BaseException as exc:
            self.error = exc
        finally:
            self.finished.set()


def start_windows_command_cleanup(
    process: subprocess.Popen[Any], registry: ProcessGroupRegistry | None = None
) -> _WindowsCommandCleanup:
    """Transfer Windows process/pipe ownership without waiting on discovery or I/O.

    Callers choose their own remaining wait budget. The retained cleanup owner
    remains responsible for handles and descendants even if a thread cannot start.
    """
    cleanup = _WindowsCommandCleanup(process, registry or ProcessGroupRegistry())
    try:
        threading.Thread(target=cleanup.run, name="alysis-command-cleanup", daemon=True).start()
        cleanup.thread_started = True
    except Exception as exc:  # Thread resource exhaustion must not lose ownership.
        cleanup.error = exc
        # Popen.kill uses the owned Windows handle and does not wait or close
        # reader-owned pipes. Retain everything else as pending, without claiming
        # that killing the leader establishes descendant cleanup.
        try:
            process.kill()
        except OSError:
            pass
    return cleanup


def _finish_windows_command(
    process: subprocess.Popen[Any], failure: BaseException, registry: ProcessGroupRegistry
) -> None:
    """Bound caller teardown while retaining ownership until cleanup finishes.

    Windows communicate reader threads retain pipe locks after a timeout.
    Popen.__exit__ closes those streams and waits without a deadline, so it
    must not run on the caller when communication remains incomplete. The
    cleanup task owns the original Popen handle and observed child identities;
    a denied kill or a surviving pipe owner cannot extend the caller's wait.
    """
    cleanup = start_windows_command_cleanup(process, registry)
    failure.cleanup_pending = True
    if not cleanup.thread_started:
        return
    # This is the existing one-second final-drain allowance, now including tree
    # termination and stream closure instead of preceding unbounded __exit__.
    complete = cleanup.finished.wait(1.0)
    failure.cleanup_pending = not complete or not cleanup.clean
    if complete and cleanup.output is not None:
        failure.stdout, failure.stderr = cleanup.output


def communicate_until_cancelled(
    process: subprocess.Popen[Any],
    *,
    timeout: float | None,
    cancel_check: Callable[[], bool],
    input: bytes | str | None = None,
    process_owner: MacSandboxOwner | None = None,
) -> tuple[Any, Any]:
    """``process.communicate(timeout=timeout)`` that reads ``cancel_check`` while it waits.

    ``communicate`` can be called again after it times out without losing output,
    so the wait is taken in slices of :data:`CANCEL_CHECK_INTERVAL_SECONDS`. Raises
    :class:`subprocess.TimeoutExpired` when ``timeout`` runs out, as ``communicate``
    does, and :class:`CommandCancelledError` once ``cancel_check`` returns true.
    Neither stops the process; the caller does. Optional stdin is supplied only
    to the first communicate call; retries continue that same request.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if cancel_check():
            raise CommandCancelledError(process.args)
        step = CANCEL_CHECK_INTERVAL_SECONDS
        if deadline is not None:
            step = min(step, max(0.0, deadline - time.monotonic()))
        try:
            if input is None:
                return process.communicate(timeout=step)
            first_input, input = input, None
            return process.communicate(input=first_input, timeout=step)
        except subprocess.TimeoutExpired as exc:
            if process_owner is not None and process.poll() is not None:
                # Detached grandchildren may hold stdout open after the leader
                # exits. End their command authority before waiting for EOF.
                if not process_owner.terminate():
                    raise CommandCleanupError(_describe_command(process.args)) from None
            if timeout is not None and deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(
                    process.args, timeout, output=exc.output, stderr=exc.stderr
                ) from None


def run_in_tracked_process_group(
    args: Any,
    *,
    shell: bool = False,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    registry: ProcessGroupRegistry | None = None,
    origin: str = "shell_run",
    command_label: str = "",
    stdin: Any | None = None,
    popen_factory: Callable[..., subprocess.Popen[str]] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    track_descendants: bool = False,
    process_owner: MacSandboxOwner | None = None,
) -> subprocess.CompletedProcess[str]:
    """``subprocess.run``-equivalent that starts the command in its own group.

    Semantics match ``subprocess.run(capture_output=True, text=True,
    timeout=...)`` exactly, including raising :class:`subprocess.TimeoutExpired`
    and killing the direct child on expiry. The only additions are that the
    command leads its own process group and that the group is registered while
    it is alive, so a later reap can terminate the children the timeout kill
    orphans today.

    Without a registry there is nothing to track, so there is no reason to hold a
    handle: the call goes straight to ``subprocess.run``. That keeps every
    non-tracking caller -- and the long-standing test seam of patching
    ``subprocess.run`` -- behaving exactly as before.

    With ``cancel_check`` (a Stop, or the run's budget), the wait reads it every
    :data:`CANCEL_CHECK_INTERVAL_SECONDS`. Once it returns true, the command and
    what it started are stopped, as an interrupt stops them, and
    :class:`CommandCancelledError` is raised with the output read so far.
    """
    popen_kwargs: dict[str, Any] = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        **command_text(),
        **new_process_group_popen_kwargs(),
    }
    # Left unset by default so interactive shell work keeps the caller's stdin.
    # Automated callers pass ``subprocess.DEVNULL`` so a command that prompts
    # reads EOF and fails immediately instead of waiting on a terminal nobody
    # is watching.
    if stdin is not None:
        popen_kwargs["stdin"] = stdin
    if cwd is not None:
        popen_kwargs["cwd"] = cwd
    if env is not None:
        popen_kwargs["env"] = env
    local_registry = registry is None and track_descendants
    if registry is None:
        if cancel_check is None and not track_descendants:
            return subprocess.run(
                args,
                shell=shell,
                timeout=timeout,
                check=False,
                **popen_kwargs,
            )
        # A cancelled command must not leave its children running, so its group
        # is tracked here even when the caller keeps no registry.
        registry = ProcessGroupRegistry()
    # Resolved at call time, not bound as a default: `subprocess.Popen` and
    # `subprocess.run` are the seams callers and tests patch, and a default
    # argument would capture the original before any patch could take effect.
    factory = popen_factory if popen_factory is not None else subprocess.Popen
    # Successful communication and POSIX cleanup keep Popen's normal lifetime.
    # Incomplete Windows communication transfers that lifetime to a cleanup
    # task, because Popen.__exit__ itself has no bounded pipe-close/wait path.
    process = factory(args, shell=shell, **popen_kwargs)
    cleanup_transferred = False
    try:
        pgid = resolve_process_group_id(int(getattr(process, "pid", 0) or 0))
        tracked: TrackedProcessGroup | None = None
        if pgid is not None and registry is not None:
            label = str(command_label) if command_label else _describe_command(args)
            tracked = registry.register(
                pgid=pgid,
                command=label,
                origin=origin,
                track_descendants=track_descendants,
                **({"process_owner": process_owner} if process_owner is not None else {}),
            )
        ownership = None
        ownership_handle = None
        cleanup_requested = False
        diagnostic_stdout = diagnostic_stderr = ""
        try:
            if (
                track_descendants
                and process_owner is None
                and sys.platform == "darwin"
                and pgid is not None
                and env is not None
            ):
                from .terminal_ownership import TerminalOwnershipLedger, seatbelt_scratch_cleanup_id

                ownership = TerminalOwnershipLedger()
                ownership_handle = ownership.track(
                    child_pid=process.pid,
                    termination_mode="process_group",
                    posix_pgid=pgid,
                    cleanup_kind="seatbelt-scratch",
                    cleanup_id=seatbelt_scratch_cleanup_id(Path(env["TMPDIR"])),
                )
            if cancel_check is None and process_owner is None:
                stdout, stderr = process.communicate(timeout=timeout)
            else:
                stdout, stderr = communicate_until_cancelled(
                    process,
                    timeout=timeout,
                    cancel_check=cancel_check or (lambda: False),
                    **({"process_owner": process_owner} if process_owner is not None else {}),
                )
        except subprocess.TimeoutExpired as exc:
            cleanup_requested = True
            diagnostic_stdout = decode_command_output(exc.stdout)
            diagnostic_stderr = decode_command_output(exc.stderr)
            # Seatbelt must clean up now, including detached observed children.
            # Other runners retain their existing finalization policy.
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                cleanup_transferred = True
                _finish_windows_command(process, exc, registry)
            else:
                if tracked is not None and tracked.process_tree is not None:
                    tracked.process_tree.signal(signal.SIGTERM)
                process.kill()
                process.wait()
                if track_descendants and tracked is not None:
                    reap_process_group(tracked, grace_seconds=_CANCEL_REAP_GRACE_SECONDS)
            raise
        except CommandCancelledError as exc:
            cleanup_requested = True
            # Stop the command and everything it started now, as an interrupt
            # below does, then hand back what it printed so far.
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                cleanup_transferred = True
                _finish_windows_command(process, exc, registry)
                raise
            else:
                if tracked is not None and tracked.process_tree is not None:
                    tracked.process_tree.signal(signal.SIGTERM)
                process.kill()
                process.wait()
                if tracked is not None:
                    reap_process_group(tracked, grace_seconds=_CANCEL_REAP_GRACE_SECONDS)
            try:
                exc.stdout, exc.stderr = process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            diagnostic_stdout = decode_command_output(exc.stdout)
            diagnostic_stderr = decode_command_output(exc.stderr)
            raise
        except BaseException as exc:
            cleanup_requested = True
            # Ctrl-C lands here. Before this change the terminal's SIGINT reached
            # the whole foreground group, so interrupting a command killed its
            # children too; now that the command leads its own session the runner
            # has to do that itself, or an interrupted test run would keep going.
            if os.name == "nt":
                cleanup_transferred = True
                _finish_windows_command(process, exc, registry)
            else:
                if tracked is not None and tracked.process_tree is not None:
                    tracked.process_tree.signal(signal.SIGTERM)
                process.kill()
                process.wait()
            if tracked is not None:
                reap_process_group(tracked, grace_seconds=1.0)
            raise
        finally:
            if tracked is not None and registry is not None:
                if local_registry or process_owner is not None:
                    reap_process_group(tracked, grace_seconds=_CANCEL_REAP_GRACE_SECONDS)
                finished = registry.release_if_finished(tracked.pgid)
                if finished and ownership is not None:
                    ownership.release(ownership_handle)
                if track_descendants and (local_registry or cleanup_requested) and not finished:
                    raise CommandCleanupError(
                        _describe_command(args), stdout=diagnostic_stdout, stderr=diagnostic_stderr
                    )
        returncode = process.poll()
    finally:
        if not cleanup_transferred:
            process.__exit__(*sys.exc_info())
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode if returncode is not None else 1,
        stdout=stdout,
        stderr=stderr,
    )
