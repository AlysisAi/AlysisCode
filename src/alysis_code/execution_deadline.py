from __future__ import annotations

import math
import statistics
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS = 1.0

# Ceiling for a single LLM attempt when a run deadline is active. One hung read
# must not consume the whole remaining budget: bounding the attempt turns a
# stuck provider into an ordinary retryable timeout while the deadline still
# owns the overall budget. Generous on purpose - legitimate long generations
# (reasoning models) routinely run several minutes.
DEFAULT_LLM_ATTEMPT_TIMEOUT_CEILING_SECONDS = 900.0
_LLM_ATTEMPT_TIMEOUT_ENV = "ALYSIS_LLM_ATTEMPT_TIMEOUT_S"
_ATTEMPT_CEILING_OFF_WORDS = frozenset({"off", "unlimited", "none", "never", "0", "disabled"})
MINIMUM_OPERATION_TIMEOUT_SECONDS = 0.05
MINIMUM_LLM_START_SECONDS = 0.25
MINIMUM_TOOL_START_SECONDS = 0.05
MINIMUM_FORCED_SUMMARY_SECONDS = 2.0
MINIMUM_SUBAGENT_START_SECONDS = 2.0
DEFAULT_FINALIZATION_MINIMUM_SECONDS = 1.0
DEFAULT_FINALIZATION_MAX_SECONDS = 120.0
DEFAULT_FINALIZATION_MAX_FRACTION = 0.25
DEADLINE_ESTIMATE_WINDOW_SIZE = 5

# Wall-clock budget applied to non-interactive runs that did not configure one.
# Generous on purpose: it exists to bound a run that has stopped converging, not
# to cut short a long job a user deliberately started.
DEFAULT_RUN_DEADLINE_SECONDS = 3600.0

# Fractions of the configured budget at which the run degrades. Both are
# elapsed-time fractions, so they are monotonic: once a stage is reached the run
# never returns to a less degraded one.
DEFAULT_CONVERGENCE_ELAPSED_FRACTION = 0.75
DEFAULT_WRAP_UP_ELAPSED_FRACTION = 0.90

_MISSING = object()


class DeadlinePhase(StrEnum):
    NORMAL = "normal"
    CONVERGENCE = "convergence"
    WRAP_UP = "wrap_up"
    FINALIZATION_WINDOW = "finalization_window"
    EXHAUSTED = "exhausted"


class DeadlineSource(StrEnum):
    EXPLICIT_CLI = "explicit_cli"
    ENVIRONMENT = "environment"
    CONFIG = "config"
    RUNTIME_DEFAULT = "runtime_default"
    INHERITED_PARENT = "inherited_parent"
    SUBAGENT_FALLBACK = "subagent_fallback"
    ABSENT = "absent"
    UNKNOWN = "unknown"


class DeadlineOperation(StrEnum):
    MAIN_LLM = "main_llm"
    MAIN_LLM_RETRY = "main_llm_retry"
    COMPACTION_LLM = "compaction_llm"
    ADAPTIVE_RETRY_LLM = "adaptive_retry_llm"
    SUBAGENT = "subagent"
    VERIFICATION = "verification"
    SHELL_TOOL = "shell_tool"
    SHELL_BACKGROUND = "shell_background"
    EXPLORATION_TOOL = "exploration_tool"
    MUTATION_TOOL = "mutation_tool"
    PROVIDER_RETRY_SLEEP = "provider_retry_sleep"
    LOCAL_FINAL_SUMMARY = "local_final_summary"
    TOOL_DISPATCH = "tool_dispatch"


_FINALIZATION_BLOCKED_OPERATIONS = frozenset(
    {
        DeadlineOperation.MAIN_LLM_RETRY.value,
        DeadlineOperation.COMPACTION_LLM.value,
        DeadlineOperation.ADAPTIVE_RETRY_LLM.value,
        DeadlineOperation.SUBAGENT.value,
        DeadlineOperation.SHELL_BACKGROUND.value,
        DeadlineOperation.EXPLORATION_TOOL.value,
        DeadlineOperation.PROVIDER_RETRY_SLEEP.value,
    }
)

# Convergence closes the operations that open a *new* line of work. Reads and
# edits stay available: the run still has to drive what it already started to a
# verifiable state, and it cannot do that with its hands tied.
_CONVERGENCE_BLOCKED_OPERATIONS = frozenset(
    {
        DeadlineOperation.SUBAGENT.value,
        DeadlineOperation.SHELL_BACKGROUND.value,
    }
)

# Wrap-up closes editing and exploration. Verification, foreground shell
# commands, and the main model call stay open, because they are exactly what
# "run final verification and write the summary" needs.
_WRAP_UP_BLOCKED_OPERATIONS = frozenset(
    {
        *_CONVERGENCE_BLOCKED_OPERATIONS,
        DeadlineOperation.EXPLORATION_TOOL.value,
        DeadlineOperation.MUTATION_TOOL.value,
    }
)

_DEGRADATION_BLOCKED_OPERATIONS: dict[str, frozenset[str]] = {
    DeadlinePhase.CONVERGENCE.value: _CONVERGENCE_BLOCKED_OPERATIONS,
    DeadlinePhase.WRAP_UP.value: _WRAP_UP_BLOCKED_OPERATIONS,
}


class DeadlineExhausted(RuntimeError):
    """Internal control-flow marker for run deadline exhaustion."""


def validate_deadline_seconds(value: Any, *, key: str = "run_deadline_seconds") -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a finite number > 0") from exc
    if parsed <= 0 or not math.isfinite(parsed):
        raise ValueError(f"{key} must be a finite number > 0")
    return parsed


def _clamp_fraction(value: Any, fallback: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return fallback
    if not math.isfinite(parsed) or parsed <= 0.0 or parsed > 1.0:
        return fallback
    return parsed


@dataclass(frozen=True)
class DeadlineDegradationPolicy:
    """When a run stops opening new work, and when it stops editing.

    Both thresholds are fractions of the configured budget. ``enabled`` is the
    kill-switch: with it off the deadline behaves exactly as it did before
    phased degradation existed.
    """

    enabled: bool = True
    convergence_fraction: float = DEFAULT_CONVERGENCE_ELAPSED_FRACTION
    wrap_up_fraction: float = DEFAULT_WRAP_UP_ELAPSED_FRACTION

    def normalized(self) -> DeadlineDegradationPolicy:
        wrap_up = _clamp_fraction(self.wrap_up_fraction, DEFAULT_WRAP_UP_ELAPSED_FRACTION)
        convergence = _clamp_fraction(
            self.convergence_fraction,
            DEFAULT_CONVERGENCE_ELAPSED_FRACTION,
        )
        # Convergence can never come after wrap-up: a misconfigured pair
        # collapses to a single threshold rather than inverting the ladder.
        convergence = min(convergence, wrap_up)
        return DeadlineDegradationPolicy(
            enabled=bool(self.enabled),
            convergence_fraction=convergence,
            wrap_up_fraction=wrap_up,
        )

    def as_payload(self) -> dict[str, Any]:
        normalized = self.normalized()
        return {
            "enabled": normalized.enabled,
            "convergence_fraction": normalized.convergence_fraction,
            "wrap_up_fraction": normalized.wrap_up_fraction,
        }


def run_deadline_degradation_enabled(cfg: Any | None) -> bool:
    """Kill-switch for phased budget degradation.

    ``ALYSIS_RUN_BUDGET_DEGRADATION`` (off/0/false/no/disabled) wins over the
    config value; default is on. When off, the wall-clock budget still applies
    but the run runs flat out until the existing finalization window.
    """
    from .branding import env_get

    env_value = env_get("ALYSIS_RUN_BUDGET_DEGRADATION")
    if env_value is not None:
        normalized = str(env_value).strip().lower()
        if normalized in {"off", "0", "false", "no", "disabled"}:
            return False
        if normalized in {"on", "1", "true", "yes", "enabled"}:
            return True
    return bool(getattr(cfg, "run_deadline_degradation_enabled", True))


def resolve_deadline_degradation_policy(cfg: Any | None) -> DeadlineDegradationPolicy:
    """Build the degradation policy from config, falling back on bad values."""
    return DeadlineDegradationPolicy(
        enabled=run_deadline_degradation_enabled(cfg),
        convergence_fraction=_clamp_fraction(
            getattr(cfg, "run_deadline_convergence_fraction", None),
            DEFAULT_CONVERGENCE_ELAPSED_FRACTION,
        ),
        wrap_up_fraction=_clamp_fraction(
            getattr(cfg, "run_deadline_wrap_up_fraction", None),
            DEFAULT_WRAP_UP_ELAPSED_FRACTION,
        ),
    ).normalized()


@dataclass(frozen=True)
class DeadlineFinalizationPolicy:
    minimum_reserve_seconds: float = DEFAULT_FINALIZATION_MINIMUM_SECONDS
    cleanup_reserve_seconds: float = DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS
    max_reserve_seconds: float = DEFAULT_FINALIZATION_MAX_SECONDS
    max_reserve_fraction: float = DEFAULT_FINALIZATION_MAX_FRACTION
    llm_latency_multiplier: float = 1.5
    verification_latency_multiplier: float = 1.25
    tool_latency_multiplier: float = 1.1
    local_cleanup_seconds: float = DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS

    def normalized(self) -> DeadlineFinalizationPolicy:
        return DeadlineFinalizationPolicy(
            minimum_reserve_seconds=max(0.0, float(self.minimum_reserve_seconds)),
            cleanup_reserve_seconds=max(0.0, float(self.cleanup_reserve_seconds)),
            max_reserve_seconds=max(0.0, float(self.max_reserve_seconds)),
            max_reserve_fraction=max(0.0, min(1.0, float(self.max_reserve_fraction))),
            llm_latency_multiplier=max(0.0, float(self.llm_latency_multiplier)),
            verification_latency_multiplier=max(0.0, float(self.verification_latency_multiplier)),
            tool_latency_multiplier=max(0.0, float(self.tool_latency_multiplier)),
            local_cleanup_seconds=max(0.0, float(self.local_cleanup_seconds)),
        )


@dataclass
class DeadlineDurationObservation:
    count: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0
    recent_seconds: list[float] = field(default_factory=list)

    def record(self, duration_seconds: float) -> None:
        duration = max(0.0, float(duration_seconds))
        self.count += 1
        self.total_seconds += duration
        self.max_seconds = max(self.max_seconds, duration)
        self.recent_seconds.append(duration)
        del self.recent_seconds[:-DEADLINE_ESTIMATE_WINDOW_SIZE]

    @property
    def average_seconds(self) -> float:
        if self.count <= 0:
            return 0.0
        return self.total_seconds / float(self.count)

    def estimate_seconds(self) -> float:
        if not self.recent_seconds:
            return 0.0
        return float(statistics.median(self.recent_seconds))

    def telemetry_snapshot(self) -> dict[str, Any]:
        return {
            "count": self.count,
            "average_seconds": round(self.average_seconds, 6),
            "max_seconds": round(self.max_seconds, 6),
            "estimate_strategy": "median_recent",
            "estimate_window_size": DEADLINE_ESTIMATE_WINDOW_SIZE,
            "estimate_window_seconds": [round(duration, 6) for duration in self.recent_seconds],
            "estimated_seconds": round(self.estimate_seconds(), 6),
        }


@dataclass(frozen=True)
class DeadlineStartDecision:
    operation: str
    allowed: bool
    phase: DeadlinePhase
    reason: str
    remaining_seconds: float | None
    normal_work_remaining_seconds: float | None
    finalization_reserve_seconds: float
    minimum_required_seconds: float
    estimated_duration_seconds: float | None

    def telemetry_snapshot(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "allowed": self.allowed,
            "phase": self.phase.value,
            "reason": self.reason,
            "remaining_seconds": (
                None if self.remaining_seconds is None else round(self.remaining_seconds, 6)
            ),
            "normal_work_remaining_seconds": (
                None
                if self.normal_work_remaining_seconds is None
                else round(self.normal_work_remaining_seconds, 6)
            ),
            "finalization_reserve_seconds": round(self.finalization_reserve_seconds, 6),
            "minimum_required_seconds": round(max(0.0, self.minimum_required_seconds), 6),
            "estimated_duration_seconds": (
                None
                if self.estimated_duration_seconds is None
                else round(max(0.0, self.estimated_duration_seconds), 6)
            ),
        }


@dataclass
class ExecutionDeadline:
    started_at_monotonic: float
    deadline_monotonic: float | None
    configured_duration_seconds: float | None = None
    source: DeadlineSource | str = DeadlineSource.UNKNOWN
    finalization_policy: DeadlineFinalizationPolicy = field(
        default_factory=DeadlineFinalizationPolicy,
        repr=False,
        compare=False,
    )
    degradation_policy: DeadlineDegradationPolicy = field(
        default_factory=DeadlineDegradationPolicy,
        repr=False,
        compare=False,
    )
    _clock: Callable[[], float] = field(default=time.monotonic, repr=False, compare=False)
    _duration_observations: dict[str, DeadlineDurationObservation] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )
    _finalization_reason: str | None = field(default=None, repr=False, compare=False)
    _finalization_entered_at_monotonic: float | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    _finalization_directive_sent: bool = field(default=False, repr=False, compare=False)
    _finalization_llm_started: bool = field(default=False, repr=False, compare=False)
    _degradation_stages_entered: dict[str, float] = field(
        default_factory=dict,
        repr=False,
        compare=False,
    )

    @classmethod
    def from_duration(
        cls,
        duration_seconds: float | None,
        *,
        clock: Callable[[], float] = time.monotonic,
        source: DeadlineSource | str = DeadlineSource.UNKNOWN,
        finalization_policy: DeadlineFinalizationPolicy | None = None,
        degradation_policy: DeadlineDegradationPolicy | None = None,
    ) -> ExecutionDeadline:
        started = float(clock())
        if duration_seconds is None:
            return cls(
                started_at_monotonic=started,
                deadline_monotonic=None,
                configured_duration_seconds=None,
                source=source,
                finalization_policy=finalization_policy or DeadlineFinalizationPolicy(),
                degradation_policy=degradation_policy or DeadlineDegradationPolicy(),
                _clock=clock,
            )
        duration = validate_deadline_seconds(duration_seconds)
        return cls(
            started_at_monotonic=started,
            deadline_monotonic=started + duration,
            configured_duration_seconds=duration,
            source=source,
            finalization_policy=finalization_policy or DeadlineFinalizationPolicy(),
            degradation_policy=degradation_policy or DeadlineDegradationPolicy(),
            _clock=clock,
        )

    @classmethod
    def from_absolute(
        cls,
        *,
        started_at_monotonic: float,
        deadline_monotonic: float | None,
        configured_duration_seconds: float | None = None,
        clock: Callable[[], float] = time.monotonic,
        source: DeadlineSource | str = DeadlineSource.INHERITED_PARENT,
        finalization_policy: DeadlineFinalizationPolicy | None = None,
        degradation_policy: DeadlineDegradationPolicy | None = None,
    ) -> ExecutionDeadline:
        if deadline_monotonic is not None and not math.isfinite(float(deadline_monotonic)):
            raise ValueError("deadline_monotonic must be finite when provided")
        if configured_duration_seconds is not None:
            validate_deadline_seconds(configured_duration_seconds)
        return cls(
            started_at_monotonic=float(started_at_monotonic),
            deadline_monotonic=(
                float(deadline_monotonic) if deadline_monotonic is not None else None
            ),
            configured_duration_seconds=configured_duration_seconds,
            source=source,
            finalization_policy=finalization_policy or DeadlineFinalizationPolicy(),
            degradation_policy=degradation_policy or DeadlineDegradationPolicy(),
            _clock=clock,
        )

    @property
    def enabled(self) -> bool:
        return self.deadline_monotonic is not None

    def elapsed_seconds(self) -> float:
        return max(0.0, float(self._clock()) - self.started_at_monotonic)

    def remaining_seconds(self) -> float | None:
        if self.deadline_monotonic is None:
            return None
        return max(0.0, self.deadline_monotonic - float(self._clock()))

    def is_exhausted(self) -> bool:
        remaining = self.remaining_seconds()
        return remaining is not None and remaining <= 0.0

    def is_exhausted_after(self, grace_seconds: float = 0.0) -> bool:
        """Return whether the hard deadline plus a bounded grace has elapsed."""
        grace = float(grace_seconds)
        if grace < 0.0 or not math.isfinite(grace):
            raise ValueError("grace_seconds must be a finite number >= 0")
        if self.deadline_monotonic is None:
            return False
        return float(self._clock()) >= self.deadline_monotonic + grace

    def can_start(self, minimum_remaining_seconds: float = 0.0) -> bool:
        remaining = self.remaining_seconds()
        if remaining is None:
            return True
        return remaining >= max(0.0, float(minimum_remaining_seconds))

    def clamp_timeout(
        self,
        configured_timeout_seconds: float | None,
        *,
        reserve_seconds: float = DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS,
        minimum_timeout_seconds: float = MINIMUM_OPERATION_TIMEOUT_SECONDS,
    ) -> float | None:
        remaining = self.remaining_seconds()
        if remaining is None:
            return configured_timeout_seconds
        safe_remaining = remaining - max(0.0, float(reserve_seconds))
        if safe_remaining < max(0.0, float(minimum_timeout_seconds)):
            return None
        if configured_timeout_seconds is None:
            return safe_remaining
        configured = validate_deadline_seconds(
            configured_timeout_seconds,
            key="configured_timeout_seconds",
        )
        return max(minimum_timeout_seconds, min(configured, safe_remaining))

    def observe_duration(self, operation: str | DeadlineOperation, duration_seconds: float) -> None:
        key = _normalize_operation(operation)
        observation = self._duration_observations.setdefault(
            key,
            DeadlineDurationObservation(),
        )
        observation.record(duration_seconds)

    def estimated_duration_seconds(
        self,
        operation: str | DeadlineOperation,
        *,
        configured_timeout_seconds: float | None = None,
        default_seconds: float | None = None,
    ) -> float | None:
        key = _normalize_operation(operation)
        observed = self._duration_observations.get(key)
        candidates: list[float] = []
        if observed is not None and observed.count > 0:
            candidates.append(observed.estimate_seconds())
        if default_seconds is not None:
            candidates.append(max(0.0, float(default_seconds)))
        if configured_timeout_seconds is not None:
            configured = validate_deadline_seconds(
                configured_timeout_seconds,
                key="configured_timeout_seconds",
            )
            clamped = self.clamp_timeout(configured)
            candidates.append(configured if clamped is None else clamped)
        if not candidates:
            return None
        return max(candidates)

    def robust_llm_estimate_snapshot(self) -> dict[str, Any]:
        """Summarize recent per-call LLM latency for nested-work admission."""
        observations = {
            name: observation.telemetry_snapshot()
            for name, observation in sorted(self._duration_observations.items())
            if "llm" in name and observation.count > 0
        }
        estimates = [float(snapshot["estimated_seconds"]) for snapshot in observations.values()]
        return {
            "estimate_strategy": "max_of_recent_operation_medians",
            "estimated_seconds": max(estimates, default=0.0),
            "operations": observations,
        }

    def finalization_reserve_seconds(
        self,
        policy: DeadlineFinalizationPolicy | None = None,
    ) -> float:
        if not self.enabled:
            return 0.0
        effective = (policy or self.finalization_policy).normalized()
        observed_llm = max(
            (
                observation.estimate_seconds()
                for name, observation in self._duration_observations.items()
                if "llm" in name and observation.count > 0
            ),
            default=0.0,
        )
        observed_verification = max(
            (
                observation.estimate_seconds()
                for name, observation in self._duration_observations.items()
                if ("verification" in name or "verify" in name) and observation.count > 0
            ),
            default=0.0,
        )
        observed_tool = max(
            (
                observation.estimate_seconds()
                for name, observation in self._duration_observations.items()
                if "llm" not in name
                and "verification" not in name
                and "verify" not in name
                and observation.count > 0
            ),
            default=0.0,
        )
        raw_reserve = max(
            effective.minimum_reserve_seconds,
            effective.cleanup_reserve_seconds,
            effective.local_cleanup_seconds,
            observed_llm * effective.llm_latency_multiplier,
            observed_verification * effective.verification_latency_multiplier,
            observed_tool * effective.tool_latency_multiplier,
        )
        if self.configured_duration_seconds is None:
            relative_cap = effective.max_reserve_seconds
        else:
            relative_cap = max(
                MINIMUM_OPERATION_TIMEOUT_SECONDS,
                self.configured_duration_seconds * effective.max_reserve_fraction,
            )
        cap = max(
            MINIMUM_OPERATION_TIMEOUT_SECONDS,
            min(effective.max_reserve_seconds, relative_cap),
        )
        return max(0.0, min(raw_reserve, cap))

    def normal_work_remaining_seconds(self) -> float | None:
        remaining = self.remaining_seconds()
        if remaining is None:
            return None
        return max(0.0, remaining - self.finalization_reserve_seconds())

    def budget_span_seconds(self) -> float | None:
        """The budget the degradation fractions are measured against.

        A deadline built from absolute bounds may carry no configured duration
        (an inherited parent ceiling), so its own span is the fallback.
        """
        if self.deadline_monotonic is None:
            return None
        configured = self.configured_duration_seconds
        if configured is not None and configured > 0:
            return float(configured)
        span = self.deadline_monotonic - self.started_at_monotonic
        return span if span > 0 else None

    def elapsed_fraction(self) -> float | None:
        span = self.budget_span_seconds()
        if span is None:
            return None
        return max(0.0, self.elapsed_seconds() / span)

    def degradation_stage(self) -> DeadlinePhase:
        """How far the run has degraded, from elapsed budget alone.

        This is deliberately independent of the finalization reserve: the
        reserve tracks observed latency and can open at any point past 75%,
        while these stages are fixed fractions of the budget. Keeping them
        separate is what makes the ladder monotonic.
        """
        policy = self.degradation_policy.normalized()
        if not policy.enabled:
            return DeadlinePhase.NORMAL
        fraction = self.elapsed_fraction()
        if fraction is None:
            return DeadlinePhase.NORMAL
        if fraction >= policy.wrap_up_fraction:
            return DeadlinePhase.WRAP_UP
        if fraction >= policy.convergence_fraction:
            return DeadlinePhase.CONVERGENCE
        return DeadlinePhase.NORMAL

    def degradation_blocked_operations(self) -> frozenset[str]:
        """Operations closed by the current degradation stage, cumulatively."""
        return _DEGRADATION_BLOCKED_OPERATIONS.get(
            self.degradation_stage().value,
            frozenset(),
        )

    def maybe_enter_degradation_stage(self) -> DeadlinePhase | None:
        """Report a degradation stage the first time it is reached.

        Returns ``None`` on every later call for the same stage, so a caller can
        log one transition per stage without tracking that itself. A run that
        jumps a stage (one long tool call spanning both thresholds) reports only
        the stage it actually landed in; the restrictions are cumulative either
        way.
        """
        stage = self.degradation_stage()
        if stage is DeadlinePhase.NORMAL:
            return None
        if stage.value in self._degradation_stages_entered:
            return None
        self._degradation_stages_entered[stage.value] = float(self._clock())
        return stage

    @property
    def degradation_stages_entered(self) -> tuple[str, ...]:
        return tuple(
            stage
            for stage, _entered_at in sorted(
                self._degradation_stages_entered.items(),
                key=lambda item: item[1],
            )
        )

    def phase(self) -> DeadlinePhase:
        if self.is_exhausted():
            return DeadlinePhase.EXHAUSTED
        remaining = self.remaining_seconds()
        if remaining is None:
            return DeadlinePhase.NORMAL
        if remaining <= self.finalization_reserve_seconds():
            return DeadlinePhase.FINALIZATION_WINDOW
        return self.degradation_stage()

    def maybe_enter_finalization(self, reason: str = "reserve_reached") -> bool:
        phase = self.phase()
        if phase != DeadlinePhase.FINALIZATION_WINDOW:
            return False
        if self._finalization_reason is None:
            self._finalization_reason = str(reason or "reserve_reached")
            self._finalization_entered_at_monotonic = float(self._clock())
            return True
        return False

    @property
    def finalization_reason(self) -> str | None:
        if self.phase() == DeadlinePhase.FINALIZATION_WINDOW:
            return self._finalization_reason or "reserve_reached"
        return self._finalization_reason

    @property
    def finalization_directive_sent(self) -> bool:
        return self._finalization_directive_sent

    def mark_finalization_directive_sent(self) -> None:
        self._finalization_directive_sent = True

    @property
    def finalization_llm_started(self) -> bool:
        return self._finalization_llm_started

    def mark_finalization_llm_started(self) -> None:
        self._finalization_llm_started = True

    def start_decision(
        self,
        operation: str | DeadlineOperation,
        *,
        minimum_remaining_seconds: float = 0.0,
        estimated_duration_seconds: float | None = None,
        configured_timeout_seconds: float | None = None,
        allow_during_finalization: bool = False,
    ) -> DeadlineStartDecision:
        operation_name = _normalize_operation(operation)
        remaining = self.remaining_seconds()
        reserve = self.finalization_reserve_seconds()
        normal_remaining = self.normal_work_remaining_seconds()
        phase = self.phase()
        minimum = max(0.0, float(minimum_remaining_seconds))
        estimate = estimated_duration_seconds
        if estimate is None:
            estimate = self.estimated_duration_seconds(
                operation_name,
                configured_timeout_seconds=configured_timeout_seconds,
                default_seconds=minimum,
            )
        if remaining is None:
            return DeadlineStartDecision(
                operation=operation_name,
                allowed=True,
                phase=phase,
                reason="deadline_unconfigured",
                remaining_seconds=None,
                normal_work_remaining_seconds=None,
                finalization_reserve_seconds=reserve,
                minimum_required_seconds=minimum,
                estimated_duration_seconds=estimate,
            )
        if phase == DeadlinePhase.EXHAUSTED:
            return DeadlineStartDecision(
                operation=operation_name,
                allowed=False,
                phase=phase,
                reason="deadline_exhausted",
                remaining_seconds=remaining,
                normal_work_remaining_seconds=normal_remaining,
                finalization_reserve_seconds=reserve,
                minimum_required_seconds=minimum,
                estimated_duration_seconds=estimate,
            )
        if remaining < minimum:
            return DeadlineStartDecision(
                operation=operation_name,
                allowed=False,
                phase=phase,
                reason="insufficient_hard_remaining",
                remaining_seconds=remaining,
                normal_work_remaining_seconds=normal_remaining,
                finalization_reserve_seconds=reserve,
                minimum_required_seconds=minimum,
                estimated_duration_seconds=estimate,
            )
        if estimate is not None and estimate > remaining:
            return DeadlineStartDecision(
                operation=operation_name,
                allowed=False,
                phase=phase,
                reason="insufficient_hard_remaining",
                remaining_seconds=remaining,
                normal_work_remaining_seconds=normal_remaining,
                finalization_reserve_seconds=reserve,
                minimum_required_seconds=minimum,
                estimated_duration_seconds=estimate,
            )
        degradation_blocked = self.degradation_blocked_operations()
        if phase == DeadlinePhase.FINALIZATION_WINDOW:
            blocked = operation_name in _FINALIZATION_BLOCKED_OPERATIONS
            if blocked and not allow_during_finalization:
                return DeadlineStartDecision(
                    operation=operation_name,
                    allowed=False,
                    phase=phase,
                    reason="finalization_disallows_operation",
                    remaining_seconds=remaining,
                    normal_work_remaining_seconds=normal_remaining,
                    finalization_reserve_seconds=reserve,
                    minimum_required_seconds=minimum,
                    estimated_duration_seconds=estimate,
                )
            if operation_name in degradation_blocked:
                # The finalization carve-out lets an operation finish the run,
                # but it cannot reopen something an earlier stage already
                # closed: the reserve window can begin before wrap-up, so
                # without this an edit blocked at 90% would be legal again at
                # 98%.
                return DeadlineStartDecision(
                    operation=operation_name,
                    allowed=False,
                    phase=phase,
                    reason="budget_degradation_disallows_operation",
                    remaining_seconds=remaining,
                    normal_work_remaining_seconds=normal_remaining,
                    finalization_reserve_seconds=reserve,
                    minimum_required_seconds=minimum,
                    estimated_duration_seconds=estimate,
                )
            return DeadlineStartDecision(
                operation=operation_name,
                allowed=True,
                phase=phase,
                reason="finalization_allowed",
                remaining_seconds=remaining,
                normal_work_remaining_seconds=normal_remaining,
                finalization_reserve_seconds=reserve,
                minimum_required_seconds=minimum,
                estimated_duration_seconds=estimate,
            )
        if operation_name in degradation_blocked:
            return DeadlineStartDecision(
                operation=operation_name,
                allowed=False,
                phase=phase,
                reason="budget_degradation_disallows_operation",
                remaining_seconds=remaining,
                normal_work_remaining_seconds=normal_remaining,
                finalization_reserve_seconds=reserve,
                minimum_required_seconds=minimum,
                estimated_duration_seconds=estimate,
            )
        if estimate is not None and normal_remaining is not None and estimate > normal_remaining:
            return DeadlineStartDecision(
                operation=operation_name,
                allowed=False,
                phase=phase,
                reason="insufficient_normal_work_remaining",
                remaining_seconds=remaining,
                normal_work_remaining_seconds=normal_remaining,
                finalization_reserve_seconds=reserve,
                minimum_required_seconds=minimum,
                estimated_duration_seconds=estimate,
            )
        return DeadlineStartDecision(
            operation=operation_name,
            allowed=True,
            phase=phase,
            reason="normal_work_allowed",
            remaining_seconds=remaining,
            normal_work_remaining_seconds=normal_remaining,
            finalization_reserve_seconds=reserve,
            minimum_required_seconds=minimum,
            estimated_duration_seconds=estimate,
        )

    def telemetry_snapshot(self) -> dict[str, Any]:
        remaining = self.remaining_seconds()
        phase = self.phase()
        normal_work_remaining = self.normal_work_remaining_seconds()
        elapsed_fraction = self.elapsed_fraction()
        return {
            "enabled": self.enabled,
            "configured_seconds": self.configured_duration_seconds,
            "source": _normalize_source(self.source),
            "deadline_monotonic": self.deadline_monotonic,
            "elapsed_seconds": round(self.elapsed_seconds(), 6),
            "elapsed_fraction": (None if elapsed_fraction is None else round(elapsed_fraction, 6)),
            "degradation_stage": self.degradation_stage().value,
            "degradation_stages_entered": list(self.degradation_stages_entered),
            "degradation_policy": self.degradation_policy.as_payload(),
            "degradation_blocked_operations": sorted(self.degradation_blocked_operations()),
            "remaining_seconds": None if remaining is None else round(remaining, 6),
            "normal_work_remaining_seconds": (
                None if normal_work_remaining is None else round(normal_work_remaining, 6)
            ),
            "finalization_reserve_seconds": round(self.finalization_reserve_seconds(), 6),
            "phase": phase.value,
            "finalization_reason": self.finalization_reason,
            "finalization_directive_sent": self.finalization_directive_sent,
            "finalization_llm_started": self.finalization_llm_started,
            "exhausted": self.is_exhausted(),
            "duration_observations": {
                key: observation.telemetry_snapshot()
                for key, observation in sorted(self._duration_observations.items())
            },
        }


def derive_subagent_deadline(
    parent_deadline: ExecutionDeadline | None,
    fallback_seconds: float | None,
) -> ExecutionDeadline:
    """Return the earlier active parent ceiling or an optional child ceiling.

    With no configured child ceiling, an existing parent deadline is inherited
    exactly.  If neither exists, the child receives a disabled deadline object
    so callers can keep one uniform admission and telemetry path without
    inventing a lifetime limit.
    """

    if fallback_seconds is None:
        if parent_deadline is not None:
            return parent_deadline
        return ExecutionDeadline.from_duration(
            None,
            source=DeadlineSource.ABSENT,
        )

    fallback = validate_deadline_seconds(fallback_seconds, key="subagent_timeout_s")
    if parent_deadline is None:
        return ExecutionDeadline.from_duration(
            fallback,
            source=DeadlineSource.SUBAGENT_FALLBACK,
        )

    clock = parent_deadline._clock
    now = float(clock())
    fallback_deadline_monotonic = now + fallback
    if parent_deadline.enabled and parent_deadline.phase() in {
        DeadlinePhase.FINALIZATION_WINDOW,
        DeadlinePhase.EXHAUSTED,
    }:
        return parent_deadline
    if (
        parent_deadline.deadline_monotonic is not None
        and parent_deadline.deadline_monotonic <= fallback_deadline_monotonic
    ):
        return parent_deadline

    return ExecutionDeadline.from_absolute(
        started_at_monotonic=now,
        deadline_monotonic=fallback_deadline_monotonic,
        configured_duration_seconds=fallback,
        clock=clock,
        source=DeadlineSource.SUBAGENT_FALLBACK,
        finalization_policy=parent_deadline.finalization_policy,
        degradation_policy=parent_deadline.degradation_policy,
    )


def _normalize_operation(operation: str | DeadlineOperation) -> str:
    raw = getattr(operation, "value", operation)
    return str(raw or "operation").strip().lower() or "operation"


def _normalize_source(source: DeadlineSource | str) -> str:
    raw = getattr(source, "value", source)
    cleaned = str(raw or "").strip().lower()
    return cleaned or DeadlineSource.UNKNOWN.value


def resolve_llm_attempt_timeout_ceiling_seconds() -> float | None:
    """Per-attempt LLM timeout ceiling; ``None`` disables the ceiling.

    ``ALYSIS_LLM_ATTEMPT_TIMEOUT_S`` overrides the default. Off-words
    (off/unlimited/none/never/0/disabled) disable the ceiling entirely,
    restoring the pre-ceiling behavior where only the run deadline bounds an
    attempt.
    """
    from .branding import env_get

    raw = env_get(_LLM_ATTEMPT_TIMEOUT_ENV)
    if raw is None:
        return DEFAULT_LLM_ATTEMPT_TIMEOUT_CEILING_SECONDS
    cleaned = str(raw).strip().lower()
    if not cleaned:
        return DEFAULT_LLM_ATTEMPT_TIMEOUT_CEILING_SECONDS
    if cleaned in _ATTEMPT_CEILING_OFF_WORDS:
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return DEFAULT_LLM_ATTEMPT_TIMEOUT_CEILING_SECONDS
    if value <= 0 or not math.isfinite(value):
        return None
    return value


def deadline_timeout_or_raise(
    deadline: ExecutionDeadline | None,
    configured_timeout_seconds: float | None,
    *,
    reserve_seconds: float = DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS,
    minimum_timeout_seconds: float = MINIMUM_OPERATION_TIMEOUT_SECONDS,
    operation: str = "operation",
) -> float | None:
    if deadline is None:
        return configured_timeout_seconds
    timeout = deadline.clamp_timeout(
        configured_timeout_seconds,
        reserve_seconds=reserve_seconds,
        minimum_timeout_seconds=minimum_timeout_seconds,
    )
    if timeout is None:
        raise DeadlineExhausted(f"run deadline exhausted before {operation}")
    return timeout


@contextmanager
def temporarily_clamp_client_timeout(
    client: Any,
    deadline: ExecutionDeadline | None,
    *,
    reserve_seconds: float = DEFAULT_DEADLINE_CLEANUP_RESERVE_SECONDS,
    minimum_timeout_seconds: float = MINIMUM_OPERATION_TIMEOUT_SECONDS,
    operation: str = "llm_call",
) -> Iterator[None]:
    if deadline is None or not hasattr(client, "timeout_s"):
        yield
        return
    original = client.timeout_s
    original_retry_deadline = getattr(client, "_provider_retry_deadline_allows", _MISSING)
    original_stream_deadline = getattr(client, "_stream_deadline_exhausted", _MISSING)
    original_limited_timeout = getattr(client, "_run_deadline_limited_timeout_elapsed", _MISSING)
    attempt_ceiling = resolve_llm_attempt_timeout_ceiling_seconds()
    configured = float(original) if original is not None else None
    if attempt_ceiling is not None:
        configured = attempt_ceiling if configured is None else min(configured, attempt_ceiling)
    # Outside the finalization window, a single attempt must also leave the
    # finalization reserve untouched, so a slow provider times out with enough
    # budget left for the wrap-up call. Inside the window (or when the
    # reserve-aware bound would be unusably small) fall back to the plain
    # cleanup-reserve clamp, which is the pre-existing behavior.
    timeout: float | None = None
    effective_reserve = max(0.0, float(reserve_seconds))
    if deadline.phase() not in (DeadlinePhase.FINALIZATION_WINDOW, DeadlinePhase.EXHAUSTED):
        effective_reserve += deadline.finalization_reserve_seconds()
        timeout = deadline.clamp_timeout(
            configured,
            reserve_seconds=effective_reserve,
            minimum_timeout_seconds=minimum_timeout_seconds,
        )
    if timeout is None:
        effective_reserve = max(0.0, float(reserve_seconds))
        timeout = deadline_timeout_or_raise(
            deadline,
            configured,
            reserve_seconds=reserve_seconds,
            minimum_timeout_seconds=minimum_timeout_seconds,
            operation=operation,
        )
    deadline_limited_cutoff = (
        deadline.deadline_monotonic - effective_reserve
        if deadline.deadline_monotonic is not None
        and timeout is not None
        and (configured is None or timeout < configured)
        else None
    )

    def _provider_retry_deadline_allows(wait_seconds: float) -> bool:
        nonlocal deadline_limited_cutoff
        retry_window_seconds = max(0.0, float(wait_seconds)) + max(
            0.0,
            float(minimum_timeout_seconds),
        )
        in_finalization = deadline.phase() == DeadlinePhase.FINALIZATION_WINDOW
        decision = deadline.start_decision(
            DeadlineOperation.PROVIDER_RETRY_SLEEP,
            minimum_remaining_seconds=retry_window_seconds,
            estimated_duration_seconds=retry_window_seconds,
            allow_during_finalization=in_finalization,
        )
        if not decision.allowed:
            return False
        # The client timeout was computed when the call started; by retry time
        # far less budget may remain. A retry is only allowed when the sleep
        # plus a minimally useful attempt still fits, and the next attempt is
        # shrunk to the remaining budget so it cannot outlive the deadline
        # (each provider attempt re-reads client.timeout_s). Outside the
        # finalization window the budget also excludes the finalization
        # reserve, mirroring the entry-time clamp: the reserve belongs to the
        # wrap-up call, not to provider retries.
        remaining = deadline.remaining_seconds()
        if remaining is not None:
            reserve_guard = max(0.0, float(reserve_seconds))
            if not in_finalization:
                reserve_guard += deadline.finalization_reserve_seconds()
            budget_after_sleep = remaining - max(0.0, float(wait_seconds)) - reserve_guard
            floor = max(float(minimum_timeout_seconds), MINIMUM_OPERATION_TIMEOUT_SECONDS)
            if budget_after_sleep < floor:
                return False
            current_timeout = getattr(client, "timeout_s", None)
            if current_timeout is None or float(current_timeout) > budget_after_sleep:
                client.timeout_s = budget_after_sleep
                if deadline.deadline_monotonic is not None:
                    deadline_limited_cutoff = deadline.deadline_monotonic - reserve_guard
        return True

    client.timeout_s = timeout
    client._provider_retry_deadline_allows = _provider_retry_deadline_allows

    def _stream_deadline_exhausted() -> bool:
        return deadline.is_exhausted_after(
            float(getattr(client, "inflight_deadline_grace_s", 10.0))
        )

    client._stream_deadline_exhausted = _stream_deadline_exhausted

    def _run_deadline_limited_timeout_elapsed() -> bool:
        # The same condition the handler below uses to turn a transport
        # timeout into DeadlineExhausted, exposed so the provider can record
        # the attempt as a budget stop rather than a provider stall.
        return (
            deadline_limited_cutoff is not None
            and float(deadline._clock()) >= deadline_limited_cutoff
        )

    client._run_deadline_limited_timeout_elapsed = _run_deadline_limited_timeout_elapsed

    from .task_budget import task_budget_purpose

    purpose = (
        "compaction"
        if "compaction" in operation
        else "main"
        if operation.startswith("main_llm")
        else "auxiliary"
    )
    try:
        with task_budget_purpose(purpose):
            yield
    except Exception as exc:
        # A provider's own timeout remains a provider failure. When this host
        # narrowed the attempt and that exact allowance has elapsed, a typed
        # transport timeout belongs to the run deadline, including its reserve.
        # Do not infer this from error text or a merely nearby hard deadline.
        if (
            deadline_limited_cutoff is not None
            and float(deadline._clock()) >= deadline_limited_cutoff
            and _has_transport_timeout_cause(exc)
        ):
            raise DeadlineExhausted(f"run deadline exhausted during {operation}") from exc
        raise
    finally:
        client.timeout_s = original
        if original_retry_deadline is _MISSING:
            try:
                delattr(client, "_provider_retry_deadline_allows")
            except AttributeError:
                pass
        else:
            client._provider_retry_deadline_allows = original_retry_deadline
        if original_stream_deadline is _MISSING:
            try:
                delattr(client, "_stream_deadline_exhausted")
            except AttributeError:
                pass
        else:
            client._stream_deadline_exhausted = original_stream_deadline
        if original_limited_timeout is _MISSING:
            try:
                delattr(client, "_run_deadline_limited_timeout_elapsed")
            except AttributeError:
                pass
        else:
            client._run_deadline_limited_timeout_elapsed = original_limited_timeout


def _has_transport_timeout_cause(error: BaseException) -> bool:
    import httpx

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.HTTPStatusError) or isinstance(
            getattr(current, "provider_status_code", None), int
        ):
            return False
        if isinstance(current, (TimeoutError, httpx.TimeoutException)):
            return True
        current = current.__cause__ or current.__context__
    return False
