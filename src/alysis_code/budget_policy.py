"""Run-budget policy: sizing, stop-reason, watchdog, and progress checkpoint.

This module is deliberately dependency-light -- standard library only, and no
imports from the rest of the package. Two reasons:

* it is loaded by ``agent/turn/core.py`` and ``agent/session.py`` on the hot
  path, and must not drag anything in;
* it holds the decision logic that has to be unit-testable in a bare
  interpreter, without executing ``alysis_code/__init__`` (which pulls
  in httpx/pydantic/rich). ``tests/test_budget_policy.py`` loads this file
  directly by path.

Environment lookups stay local rather than importing ``branding`` for the same
reason. The local helper reads canonical ALYSIS_* settings so standalone and
package execution keep the same behavior.

Why a watchdog exists at all
----------------------------
Every pre-existing budget check in the engine is a *start gate*: it is
evaluated before an operation begins (``ExecutionDeadline.start_decision``,
``deadline_timeout_or_raise``). Nothing re-checks the clock while an operation
is in flight, so any blocking call that does not bound itself by the deadline
can outlive the budget indefinitely -- which is exactly what was observed in
production, where runs recorded ``exhausted: true`` and then kept going for
hours. :class:`BudgetWatchdog` is the stop gate: a daemon timer armed once at
run start that trips a cancellation event at ``budget + grace``, regardless of
what the main thread is blocked on.
"""

from __future__ import annotations

import math
import os
import threading
from collections.abc import Callable, Mapping
from typing import Any

# Wall-clock budget applied to a non-interactive run that did not configure
# one. Unchanged from the value this replaced, so default behaviour is
# identical; it is now overridable per-run instead of being a literal.
DEFAULT_RUN_BUDGET_SECONDS = 3600.0

# How long past the deadline an in-flight operation is given to unwind
# cooperatively before the watchdog cancels it.
DEFAULT_BUDGET_GRACE_SECONDS = 60.0

# Fraction of the budget at which a run with nothing to show for itself is
# told so.
DEFAULT_BUDGET_CHECKPOINT_FRACTION = 0.33

RUN_BUDGET_SECONDS_ENV = "ALYSIS_RUN_BUDGET_SECONDS"
BUDGET_GRACE_SECONDS_ENV = "ALYSIS_BUDGET_GRACE_SECONDS"
BUDGET_CHECKPOINT_FRACTION_ENV = "ALYSIS_BUDGET_CHECKPOINT_FRACTION"

# Machine-readable marker for "the run stopped because it ran out of budget".
# Written to the session log's final event and to the run_finished
# crash-diagnostic event so a harness can tell a budget stop from a crash
# without parsing prose.
STOP_REASON_RUN_BUDGET_EXHAUSTED = "run_budget_exhausted"

# The model stopped returning usable responses and the targeted recovery for
# that was spent. The run then reports what it has and ends. Same shape of
# outcome as a budget stop: chosen by the run, not a crash.
STOP_REASON_EMPTY_RESPONSE_ANOMALY_RETRY_EXHAUSTED = "empty_response_anomaly_retry_exhausted"

# The provider returned a token-limited response and bounded continuation was
# exhausted. This is a deliberate incomplete stop, not a runtime crash.
STOP_REASON_OUTPUT_TOKEN_LIMIT = "output_token_limit"

PROGRESS_CHECKPOINT_FAILED_EVENT = "progress_checkpoint_failed"

# A budget stop is a normal outcome, not a failure: the agent did what it could
# in the time it was given and reported honestly. Exiting non-zero made every
# harness record it as NonZeroAgentExitCodeError.
BUDGET_STOP_EXIT_CODE = 0
DEFAULT_FAILURE_EXIT_CODE = 1

# The single model-visible string introduced by this module. Kept as one
# constant so it is greppable and cannot drift.
BUDGET_CHECKPOINT_NOTICE = (
    "Budget checkpoint: no material progress recorded yet. "
    "Reassess approach or report the concrete blocker."
)

# Reasons for which the run *decided* to stop: it hit a limit it owns, wound
# down deliberately, reported what it had, and exited. These are outcomes, not
# failures, and every one of them must exit zero -- a non-zero code makes a
# harness record NonZeroAgentExitCodeError and throw the trial away.
#
# This is a registry rather than a special case per stop path. The budget stop
# was fixed first and got bespoke plumbing; the empty-response stop then had to
# be fixed the same way, separately, because there was nothing to join. A
# future graceful stop joins by adding its constant here.
#
# The bar for membership: the run chose to end and said so honestly. An
# exception escaping, a provider that never came back, a user abort -- none of
# those are self-stops, and they keep their non-zero codes.
CLEAN_STOP_REASONS = frozenset(
    {
        STOP_REASON_RUN_BUDGET_EXHAUSTED,
        STOP_REASON_EMPTY_RESPONSE_ANOMALY_RETRY_EXHAUSTED,
        STOP_REASON_OUTPUT_TOKEN_LIMIT,
    }
)

# Ordinary terminal states, which were never failures to begin with. Separate
# from CLEAN_STOP_REASONS because "the turn ended" is not a self-stop and
# nothing should record it as one.
NORMAL_COMPLETION_REASONS = frozenset(
    {
        "",
        "completed",
        "session_close",
    }
)

# Terminal reasons that are *not* failures. Everything else exits non-zero.
GRACEFUL_STOP_REASONS = NORMAL_COMPLETION_REASONS | CLEAN_STOP_REASONS


class RunBudgetCancelled(Exception):
    """Fallback cancellation error used when no error class is injected.

    Production wires :class:`BudgetCancellationToken` with
    ``cancellation.CooperativeCancellationError`` so that every existing
    ``except CooperativeCancellationError`` clause keeps working. This class
    exists so the module stays importable, and testable, on its own.
    """

    def __init__(self, reason: str = STOP_REASON_RUN_BUDGET_EXHAUSTED) -> None:
        super().__init__(reason)
        self.reason = str(reason or STOP_REASON_RUN_BUDGET_EXHAUSTED)


def _env_value(name: str, environ: Mapping[str, str] | None = None) -> str | None:
    source: Mapping[str, str] = os.environ if environ is None else environ
    raw = source.get(name)
    if raw is None:
        return None
    cleaned = str(raw).strip()
    return cleaned or None


def _positive_float(raw: str | None, fallback: float) -> float:
    """Parse a strictly positive finite float, falling back on anything else.

    A misconfigured budget must never be able to abort a run or, worse, make
    the budget effectively infinite. Every bad value -- unparseable, negative,
    zero, NaN, inf -- resolves to the documented default.
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


def _non_negative_float(raw: str | None, fallback: float) -> float:
    """Like :func:`_positive_float` but admits ``0`` (a zero-length window)."""
    if raw is None:
        return fallback
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(value) or value < 0.0:
        return fallback
    return value


def resolve_run_budget_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Default wall-clock budget for a non-interactive run, in seconds.

    ``ALYSIS_RUN_BUDGET_SECONDS`` overrides
    :data:`DEFAULT_RUN_BUDGET_SECONDS`. This sets only the *default* rung of
    the existing precedence ladder (``--deadline-seconds`` >
    ``ALYSIS_RUN_DEADLINE_SECONDS`` > ``run_deadline_seconds`` config >
    this), so nothing that already configured a budget changes behaviour.
    """
    return _positive_float(
        _env_value(RUN_BUDGET_SECONDS_ENV, environ),
        DEFAULT_RUN_BUDGET_SECONDS,
    )


def resolve_budget_grace_seconds(environ: Mapping[str, str] | None = None) -> float:
    """Grace window between the deadline passing and forced cancellation.

    ``ALYSIS_BUDGET_GRACE_SECONDS``; default
    :data:`DEFAULT_BUDGET_GRACE_SECONDS`. Zero is legal and means "cancel the
    moment the budget is gone".
    """
    return _non_negative_float(
        _env_value(BUDGET_GRACE_SECONDS_ENV, environ),
        DEFAULT_BUDGET_GRACE_SECONDS,
    )


def resolve_checkpoint_fraction(environ: Mapping[str, str] | None = None) -> float:
    """Budget fraction at which the no-progress checkpoint is evaluated.

    ``ALYSIS_BUDGET_CHECKPOINT_FRACTION``; default
    :data:`DEFAULT_BUDGET_CHECKPOINT_FRACTION`. Values outside ``(0, 1]`` fall
    back, so the checkpoint can never be armed at "already past the end".
    """
    value = _positive_float(
        _env_value(BUDGET_CHECKPOINT_FRACTION_ENV, environ),
        DEFAULT_BUDGET_CHECKPOINT_FRACTION,
    )
    if value > 1.0:
        return DEFAULT_BUDGET_CHECKPOINT_FRACTION
    return value


def exit_code_for_stop(
    stop_reason: str | None,
    *,
    failure_exit_code: int = DEFAULT_FAILURE_EXIT_CODE,
) -> int:
    """Process exit code for a run terminating with ``stop_reason``.

    The whole point of this table: a run-budget stop is a *normal* outcome and
    must exit 0, while genuine errors keep their non-zero code. Callers pass
    their own ``failure_exit_code`` so infrastructure failures can still use
    ``INFRASTRUCTURE_FAILURE_EXIT_CODE``.
    """
    reason = str(stop_reason or "").strip().lower()
    if reason in GRACEFUL_STOP_REASONS:
        return BUDGET_STOP_EXIT_CODE
    return int(failure_exit_code)


def is_clean_stop(stop_reason: str | None) -> bool:
    """True when ``stop_reason`` names a stop the run chose for itself.

    The predicate a stop path should consult before deciding an exit code, so
    joining the clean-stop set is a one-line change to
    :data:`CLEAN_STOP_REASONS` rather than new plumbing at the stop site.

    Note this is narrower than :data:`GRACEFUL_STOP_REASONS`: an ordinary
    completion exits zero but is not a self-stop, and must not be recorded as
    one.
    """
    return str(stop_reason or "").strip().lower() in CLEAN_STOP_REASONS


def is_budget_stop(stop_reason: str | None) -> bool:
    return str(stop_reason or "").strip().lower() == STOP_REASON_RUN_BUDGET_EXHAUSTED


def is_budget_cancellation(error: BaseException | None) -> bool:
    """True when ``error`` is a cancellation raised by the budget watchdog.

    Distinguishes a watchdog stop from a user-requested cancellation, which
    share an exception type. Matching on the ``reason`` attribute is what
    ``CooperativeCancellationError`` already exposes.
    """
    if error is None:
        return False
    return is_budget_stop(getattr(error, "reason", None))


class BudgetCancellationToken:
    """Cancellation token whose cancellation means "the run budget is gone".

    Duck-types ``cancellation.EventCancellationToken``: ``is_cancelled`` plus
    ``throw_if_cancelled``. The error class is injected rather than imported so
    this module keeps no package dependencies; production passes
    ``CooperativeCancellationError`` so every existing handler still catches it.
    """

    def __init__(
        self,
        event: threading.Event,
        *,
        error_class: type[BaseException] | None = None,
        reason: str = STOP_REASON_RUN_BUDGET_EXHAUSTED,
    ) -> None:
        self._event = event
        self._error_class: type[BaseException] = error_class or RunBudgetCancelled
        self.reason = str(reason or STOP_REASON_RUN_BUDGET_EXHAUSTED)

    @property
    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def throw_if_cancelled(self, reason: str | None = None) -> None:
        """Raise if cancelled, always attributing the stop to the budget.

        The caller-supplied ``reason`` is ignored on purpose: the turn engine
        passes the literal ``"cancelled_by_user"`` at every checkpoint, and
        this token knows better why it fired. Preserving the real reason is
        what lets the stop be finalized as a clean budget exit rather than a
        user abort.
        """
        _ = reason
        if self.is_cancelled:
            raise self._error_class(self.reason)

    def wait(self, timeout: float | None = None) -> bool:
        """Wait until budget cancellation or ``timeout`` without polling."""

        return self._event.wait(timeout)


class BudgetWatchdog:
    """Daemon timer that trips a cancellation event at ``budget + grace``.

    Armed once at run start and disarmed in a ``finally``. Firing sets a
    :class:`threading.Event`, which is all a cooperative checkpoint or a
    ``wait``-style blocking call needs to unblock; the watchdog itself never
    touches execution state, so it cannot corrupt a run that was about to
    finish normally.
    """

    def __init__(
        self,
        *,
        budget_seconds: float,
        grace_seconds: float = DEFAULT_BUDGET_GRACE_SECONDS,
        on_fire: Callable[[], None] | None = None,
        event: threading.Event | None = None,
        timer_factory: Callable[..., Any] = threading.Timer,
    ) -> None:
        self.budget_seconds = max(0.0, float(budget_seconds))
        self.grace_seconds = max(0.0, float(grace_seconds))
        self._on_fire = on_fire
        self._event = event if event is not None else threading.Event()
        self._timer_factory = timer_factory
        self._timer: Any | None = None
        self._lock = threading.Lock()

    @property
    def event(self) -> threading.Event:
        return self._event

    @property
    def fired(self) -> bool:
        return self._event.is_set()

    @property
    def armed(self) -> bool:
        return self._timer is not None

    def fire_delay_seconds(self) -> float:
        """Seconds from arming to firing: the whole budget plus the grace."""
        return self.budget_seconds + self.grace_seconds

    def arm(self) -> BudgetWatchdog:
        with self._lock:
            if self._timer is not None:
                return self
            timer = self._timer_factory(self.fire_delay_seconds(), self._fire)
            # Daemon: a watchdog must never be the reason a process refuses to
            # exit once the run has finished on its own.
            try:
                timer.daemon = True
            except AttributeError:  # pragma: no cover - injected fakes may differ
                pass
            self._timer = timer
            timer.start()
        return self

    def disarm(self) -> None:
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            try:
                timer.cancel()
            except Exception:  # noqa: BLE001 - teardown must never raise
                pass

    def _fire(self) -> None:
        self._event.set()
        if self._on_fire is None:
            return
        try:
            self._on_fire()
        except Exception:  # noqa: BLE001 - a telemetry failure must not kill the timer thread
            pass

    def __enter__(self) -> BudgetWatchdog:
        return self.arm()

    def __exit__(self, *_exc: object) -> None:
        self.disarm()


class ProgressCheckpoint:
    """One-shot "no material progress yet" trigger at a fraction of budget.

    Material progress is file modifications or verification attempts -- the two
    counters the runtime summary already reports. Both are monotonic, so a run
    that has done something real can never trip this later.
    """

    def __init__(
        self,
        *,
        fraction: float | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.fraction = (
            resolve_checkpoint_fraction(environ) if fraction is None else float(fraction)
        )
        self._fired = False

    @property
    def fired(self) -> bool:
        return self._fired

    def check(
        self,
        *,
        elapsed_fraction: float | None,
        material_edit_count: int,
        verification_attempt_count: int,
    ) -> bool:
        """Return ``True`` exactly once, the first time the checkpoint fails.

        ``elapsed_fraction`` is ``None`` when no finite budget applies, in
        which case there is no checkpoint to miss.
        """
        if self._fired:
            return False
        if elapsed_fraction is None:
            return False
        try:
            fraction = float(elapsed_fraction)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(fraction) or fraction < self.fraction:
            return False
        if int(material_edit_count or 0) > 0 or int(verification_attempt_count or 0) > 0:
            return False
        self._fired = True
        return True
