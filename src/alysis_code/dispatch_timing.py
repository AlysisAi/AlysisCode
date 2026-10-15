"""Dispatch timing policy: cancellable waits, promotion thresholds, overhead.

This module is deliberately dependency-light -- standard library only, and no
imports from the rest of the package -- for the same two reasons as
``budget_policy``:

* it is loaded by ``agent/tools_assembly.py`` on the shell dispatch hot path
  and must not drag anything in;
* it holds the decision logic that has to be unit-testable in a bare
  interpreter, without executing ``alysis_code/__init__`` (which pulls
  in httpx/pydantic/rich). ``tests/test_dispatch_timing.py`` loads this file
  directly by path.

What went wrong in production
----------------------------
Terminal-Bench telemetry showed ``tool_dispatch`` averaging 30.4s over 72
dispatches on ``compile-compcert`` -- 2,190s of a 3,484s run -- with the
maximum pinned at 60.08s. That bucket is not generic dispatch overhead:
``_deadline_operation_for_tool_name`` special-cases only ``shell_run`` and
``shell_background``, so **``shell_wait`` falls through into
``tool_dispatch``**. The 60.08s ceiling is ``shell_wait``'s own cap
(``min(wait_seconds, 60.0)``, schema ``maximum: 60``).

Two defects follow from that, and this module addresses both:

1. **A wait in flight could not be preempted.** The terminal manager waits on a
   ``threading.Condition`` that knows nothing about the run budget, so PR2's
   watchdog -- which sets a ``threading.Event`` -- could not interrupt a wait
   already running. A 60s wait armed just before the deadline outlived it.
   :func:`run_cancellable_wait` fixes this by driving the *existing*
   completion-driven wait in short slices and re-reading the cancellation token
   between them, so a budget stop is observed within one slice instead of at
   the end of the full wait. The wait stays completion-driven: each slice
   returns the instant the process emits output or exits, so nothing is made
   slower, and no busy-spinning is introduced.

2. **Dispatch overhead was unmeasurable.** ``shell_tool`` timed the whole
   dispatch, including the workspace walks that mutation detection performs
   before and after every command, so the cost of the machinery could not be
   separated from the cost of the command.
   :class:`DispatchOverheadAccount` does that subtraction, and the result is
   reported under :data:`DISPATCH_OVERHEAD_OPERATION` as its own
   ``duration_observations`` category.

Environment lookups go through :data:`os.environ` rather than
``branding.env_get`` to keep this module free of package imports; ``env_get``
is itself just ``os.environ.get``, so the behaviour is identical.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

# How long a foreground command may hold a blocking dispatch slot before it is
# considered "long running". Small on purpose: the point of the threshold is
# that a build or install stops occupying the agent loop almost immediately.
DEFAULT_BACKGROUND_PROMOTION_SECONDS = 10.0

# Granularity at which a long wait re-reads the cancellation token. Each slice
# is a real completion-driven wait, not a sleep: it returns early the moment
# output arrives or the process exits, so the slice size bounds *cancellation
# latency* only, never how quickly a finished command is noticed.
DEFAULT_WAIT_SLICE_SECONDS = 0.5

BACKGROUND_PROMOTION_SECONDS_ENV = "ALYSIS_SHELL_BACKGROUND_PROMOTION_SECONDS"
WAIT_SLICE_SECONDS_ENV = "ALYSIS_SHELL_WAIT_SLICE_SECONDS"

# ``duration_observations`` category for time spent inside the dispatch
# machinery, excluding the runtime of the command being dispatched. Kept as one
# constant so the telemetry key cannot drift from the allowlist in
# ``crash_diagnostics``.
DISPATCH_OVERHEAD_OPERATION = "dispatch_overhead_seconds"

WAIT_OUTCOME_COMPLETED = "completed"
WAIT_OUTCOME_TIMED_OUT = "timed_out"
WAIT_OUTCOME_CANCELLED = "cancelled"


def _env_value(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    source: Mapping[str, str] = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None:
        return None
    cleaned = str(raw).strip()
    return cleaned or None


def _positive_float(raw: str | None, fallback: float) -> float:
    """Parse a strictly positive finite float, falling back on anything else.

    A misconfigured knob must never be able to disable the mechanism it
    configures: every bad value -- unparseable, negative, zero, NaN, inf --
    resolves to the documented default.
    """
    if raw is None:
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(value) or value <= 0.0:
        return fallback
    return value


def _finite_or(value: object, fallback: float) -> float:
    """Coerce ``value`` to a finite float, else ``fallback``."""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(number):
        return fallback
    return number


def resolve_background_promotion_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Seconds a foreground command may block dispatch before it counts as long.

    ``ALYSIS_SHELL_BACKGROUND_PROMOTION_SECONDS`` overrides
    :data:`DEFAULT_BACKGROUND_PROMOTION_SECONDS`.
    """
    return _positive_float(
        _env_value(BACKGROUND_PROMOTION_SECONDS_ENV, environ),
        DEFAULT_BACKGROUND_PROMOTION_SECONDS,
    )


def resolve_wait_slice_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Cancellation-check granularity for a long wait, in seconds.

    ``ALYSIS_SHELL_WAIT_SLICE_SECONDS`` overrides
    :data:`DEFAULT_WAIT_SLICE_SECONDS`.
    """
    return _positive_float(
        _env_value(WAIT_SLICE_SECONDS_ENV, environ),
        DEFAULT_WAIT_SLICE_SECONDS,
    )


def should_promote_to_background(
    elapsed_seconds: float,
    threshold_seconds: float | None = None,
) -> bool:
    """True when a command has blocked dispatch for at least the threshold.

    Boundary is inclusive: a command that has run *exactly* the threshold has
    already held the slot for as long as the policy allows.
    """
    threshold = (
        resolve_background_promotion_seconds()
        if threshold_seconds is None
        else _finite_or(threshold_seconds, DEFAULT_BACKGROUND_PROMOTION_SECONDS)
    )
    elapsed = _finite_or(elapsed_seconds, 0.0)
    if elapsed < 0.0:
        return False
    return elapsed >= max(0.0, threshold)


def clamp_wait_seconds(
    requested_seconds: float,
    *,
    remaining_seconds: float | None = None,
    reserve_seconds: float = 0.0,
    minimum_seconds: float = 0.0,
) -> float:
    """Clamp a requested wait to what the deadline can still afford.

    ``remaining_seconds`` of ``None`` means "no deadline applies", in which case
    the request stands. Otherwise the wait is capped at the remaining time less
    ``reserve_seconds`` (the cleanup reserve), floored at ``minimum_seconds`` so
    a caller can always make one non-blocking probe rather than being told to
    wait for a negative duration.
    """
    requested = max(0.0, _finite_or(requested_seconds, 0.0))
    floor = max(0.0, _finite_or(minimum_seconds, 0.0))
    if remaining_seconds is None:
        return max(requested, floor)
    remaining = _finite_or(remaining_seconds, 0.0)
    reserve = max(0.0, _finite_or(reserve_seconds, 0.0))
    affordable = remaining - reserve
    if affordable <= 0.0:
        return floor
    return max(floor, min(requested, affordable))


def next_wait_slice(remaining_seconds: float, slice_seconds: float | None = None) -> float:
    """Duration of the next wait slice.

    Slicing exists solely to bound cancellation latency, so a non-positive
    slice size means "do not slice" and the whole remaining wait is taken in
    one step -- which is exactly the pre-fix behaviour.
    """
    remaining = max(0.0, _finite_or(remaining_seconds, 0.0))
    if slice_seconds is None:
        step = resolve_wait_slice_seconds()
    else:
        step = _finite_or(slice_seconds, 0.0)
    if step <= 0.0:
        return remaining
    return min(remaining, step)


@dataclass(frozen=True)
class WaitResult:
    """Outcome of a sliced, cancellable wait."""

    outcome: str
    elapsed_seconds: float
    slices: int

    @property
    def completed(self) -> bool:
        return self.outcome == WAIT_OUTCOME_COMPLETED

    @property
    def cancelled(self) -> bool:
        return self.outcome == WAIT_OUTCOME_CANCELLED

    @property
    def timed_out(self) -> bool:
        return self.outcome == WAIT_OUTCOME_TIMED_OUT


def run_cancellable_wait(
    *,
    wait_once: Callable[[float], bool],
    total_seconds: float,
    is_cancelled: Callable[[], bool] | None = None,
    slice_seconds: float | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> WaitResult:
    """Wait up to ``total_seconds``, re-reading cancellation between slices.

    ``wait_once`` performs one real completion-driven wait of the given
    duration and returns ``True`` when the condition it waits on is satisfied.
    Because each slice returns early on completion, slicing costs nothing in
    responsiveness to the process; it only bounds how long a cancelled run
    stays parked here.

    ``wait_once`` is always invoked at least once, even for a zero-length wait,
    so a ``wait_seconds=0`` probe keeps its existing "read what is there right
    now" semantics.
    """
    started = monotonic()
    total = max(0.0, _finite_or(total_seconds, 0.0))
    slices = 0
    # Tracked against the original total rather than recomputed from a deadline,
    # so the first slice is exactly what the caller asked for. That matters for
    # the unsliced path: it must hand the wait primitive the identical timeout
    # it received today, not that value minus the microseconds spent getting
    # here.
    remaining = total

    def _elapsed() -> float:
        return max(0.0, monotonic() - started)

    # Cancelled before we ever blocked: report it without touching the process.
    if is_cancelled is not None and is_cancelled():
        return WaitResult(WAIT_OUTCOME_CANCELLED, _elapsed(), slices)

    while True:
        step = next_wait_slice(remaining, slice_seconds)
        slices += 1
        if wait_once(step):
            return WaitResult(WAIT_OUTCOME_COMPLETED, _elapsed(), slices)
        if is_cancelled is not None and is_cancelled():
            return WaitResult(WAIT_OUTCOME_CANCELLED, _elapsed(), slices)
        remaining = total - _elapsed()
        if remaining <= 0.0:
            return WaitResult(WAIT_OUTCOME_TIMED_OUT, _elapsed(), slices)


@dataclass(frozen=True)
class DispatchOverheadAccount:
    """Split a dispatch into the command's own runtime and the machinery around it.

    ``overhead_seconds`` is what the next campaign should watch: it excludes the
    command, so it stays flat when a build legitimately takes twenty minutes and
    rises only when the dispatch path itself is doing expensive work (the
    pre/post workspace walks for mutation detection, evidence classification).
    """

    total_seconds: float
    command_seconds: float

    @property
    def overhead_seconds(self) -> float:
        # Clamped at zero: a command clock read slightly after the dispatch
        # clock must never report negative overhead.
        return max(0.0, self.total_seconds - self.command_seconds)

    @classmethod
    def from_totals(cls, total_seconds: float, command_seconds: float) -> DispatchOverheadAccount:
        total = max(0.0, _finite_or(total_seconds, 0.0))
        command = max(0.0, _finite_or(command_seconds, 0.0))
        # A command cannot have run longer than the dispatch that contains it;
        # if the clocks disagree, trust the enclosing measurement.
        return cls(total_seconds=total, command_seconds=min(command, total))
