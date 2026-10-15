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
* Nothing here ever enumerates the process table, and nothing ever matches a
  process by name, cmdline, or any other heuristic. There is no code path that
  can signal a group the runner did not spawn.
* A recorded pgid is refused if it is not a positive integer, if it is this
  process's own group (a defensive check for a host where session creation
  silently failed), or if the group leader's start token changed since
  registration. That last guard reads ``/proc``, so it is active on Linux and
  absent on macOS/BSD; there the residual exposure is a pgid recycled in the
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
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from .branding import env_get
from .command_text import command_text
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

    def register(self, *, pgid: int, command: str, origin: str) -> TrackedProcessGroup | None:
        """Record a group this runner just created. Returns ``None`` if refused."""
        if not _pgid_is_signalable(pgid):
            return None
        record = TrackedProcessGroup(
            pgid=int(pgid),
            command=str(command or ""),
            origin=str(origin or "shell_run"),
            started_at=time.monotonic(),
            start_token=_pid_start_token(int(pgid)),
        )
        with self._lock:
            self._groups[record.pgid] = record
        return record

    def release(self, pgid: int) -> None:
        """Forget a group unconditionally (the caller knows it is finished)."""
        with self._lock:
            self._groups.pop(int(pgid), None)

    def release_if_finished(self, pgid: int) -> bool:
        """Drop the group when it no longer exists; keep it when it is alive.

        Called when a tool call returns. A plain command that ran to completion
        is dropped here; a command that timed out, was backgrounded with ``&``,
        or spawned a daemon leaves a live group and stays tracked for reaping.
        """
        if not process_group_exists(int(pgid)):
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
            if process_group_exists(record.pgid) and _start_token_matches(record):
                alive.append(record)
            else:
                self.release(record.pgid)
        return tuple(alive)

    def clear(self) -> None:
        with self._lock:
            self._groups.clear()


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
    """Linux process start time, used to detect PID reuse. ``None`` elsewhere."""
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
    if not _pgid_is_signalable(record.pgid) or not _start_token_matches(record):
        return ProcessReapOutcome(
            pgid=record.pgid,
            command=record.command,
            origin=record.origin,
            runtime_s=runtime_s,
            signal_used=SIGNAL_NONE,
        )
    if not process_group_exists(record.pgid):
        return ProcessReapOutcome(
            pgid=record.pgid,
            command=record.command,
            origin=record.origin,
            runtime_s=runtime_s,
            signal_used=SIGNAL_NONE,
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
        registry.release(record.pgid)
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


def _stop_windows_process_tree(process: subprocess.Popen[Any]) -> None:  # pragma: no cover
    """End a command and the processes it started, at once (Windows).

    ``taskkill /T`` takes most of a second (about 0.8 s measured), more than half
    of what a Stop may take. ``psutil`` finds the children in one process snapshot
    and ends them directly. We hold the command's process handle, so its pid has
    not been reused; ``taskkill`` stays the fallback.
    """
    from .terminal_manager import _kill_windows_process_tree

    try:
        import psutil

        parent = psutil.Process(process.pid)
        family = [*parent.children(recursive=True), parent]
    except Exception:  # noqa: BLE001 - fall back to the slower, older way
        _kill_windows_process_tree(process)
        return
    for member in family:
        try:
            member.kill()
        except psutil.Error:
            pass


def communicate_until_cancelled(
    process: subprocess.Popen[Any],
    *,
    timeout: float | None,
    cancel_check: Callable[[], bool],
) -> tuple[Any, Any]:
    """``process.communicate(timeout=timeout)`` that reads ``cancel_check`` while it waits.

    ``communicate`` can be called again after it times out without losing output,
    so the wait is taken in slices of :data:`CANCEL_CHECK_INTERVAL_SECONDS`. Raises
    :class:`subprocess.TimeoutExpired` when ``timeout`` runs out, as ``communicate``
    does, and :class:`CommandCancelledError` once ``cancel_check`` returns true.
    Neither stops the process; the caller does.
    """
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        if cancel_check():
            raise CommandCancelledError(process.args)
        step = CANCEL_CHECK_INTERVAL_SECONDS
        if deadline is not None:
            step = min(step, max(0.0, deadline - time.monotonic()))
        try:
            return process.communicate(timeout=step)
        except subprocess.TimeoutExpired:
            if timeout is not None and deadline is not None and time.monotonic() >= deadline:
                raise subprocess.TimeoutExpired(process.args, timeout) from None


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
    if registry is None:
        if cancel_check is None:
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
    # The body mirrors CPython's subprocess.run: the Popen context manager closes
    # the pipes and reaps the child on every exit path, and a timeout kills the
    # direct child before re-raising. The only additions are the process group and
    # its registration.
    with factory(args, shell=shell, **popen_kwargs) as process:
        pgid = resolve_process_group_id(int(getattr(process, "pid", 0) or 0))
        tracked: TrackedProcessGroup | None = None
        if pgid is not None and registry is not None:
            label = str(command_label) if command_label else _describe_command(args)
            tracked = registry.register(pgid=pgid, command=label, origin=origin)
        try:
            if cancel_check is None:
                stdout, stderr = process.communicate(timeout=timeout)
            else:
                stdout, stderr = communicate_until_cancelled(
                    process, timeout=timeout, cancel_check=cancel_check
                )
        except subprocess.TimeoutExpired as exc:
            # Killing only the leader is what orphans the children today. That is
            # left as-is on purpose: the group stays registered, and finalization
            # reaps it under the single audited decision path.
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                # Kill while the owned parent is still alive: once it exits,
                # Windows can no longer walk its children and inherited output
                # pipes can keep communicate() blocked indefinitely.
                from .terminal_manager import _kill_windows_process_tree

                _kill_windows_process_tree(process)
                exc.stdout, exc.stderr = process.communicate(timeout=1.0)
            else:
                process.kill()
                process.wait()
            raise
        except CommandCancelledError as exc:
            # Stop the command and everything it started now, as an interrupt
            # below does, then hand back what it printed so far.
            if os.name == "nt":  # pragma: no cover - exercised on Windows
                _stop_windows_process_tree(process)
            else:
                process.kill()
                if tracked is not None:
                    reap_process_group(tracked, grace_seconds=_CANCEL_REAP_GRACE_SECONDS)
            try:
                exc.stdout, exc.stderr = process.communicate(timeout=1.0)
            except subprocess.TimeoutExpired:
                pass
            raise
        except BaseException:
            # Ctrl-C lands here. Before this change the terminal's SIGINT reached
            # the whole foreground group, so interrupting a command killed its
            # children too; now that the command leads its own session the runner
            # has to do that itself, or an interrupted test run would keep going.
            if os.name == "nt":
                from .terminal_manager import _kill_windows_process_tree

                _kill_windows_process_tree(process)
            else:
                process.kill()
            if tracked is not None:
                reap_process_group(tracked, grace_seconds=1.0)
            raise
        finally:
            if tracked is not None and registry is not None:
                registry.release_if_finished(tracked.pgid)
        returncode = process.poll()
    return subprocess.CompletedProcess(
        args=args,
        returncode=returncode if returncode is not None else 1,
        stdout=stdout,
        stderr=stderr,
    )
