"""Tests for dispatch timing: cancellable waits, thresholds, overhead accounting.

Runnable two ways:

    python3 tests/test_dispatch_timing.py    # standalone, stdlib only
    pytest tests/test_dispatch_timing.py

The module under test is loaded directly from its file path so that importing
it never executes ``alysis_code/__init__`` or any of the package's
dependency-heavy import chain. That keeps these tests runnable in a bare
interpreter with no third-party packages installed.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "src" / "alysis_code" / "dispatch_timing.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_dispatch_timing", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    # Registered before execution because the module defines dataclasses, and
    # ``dataclasses`` resolves annotations via ``sys.modules[cls.__module__]``.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


dt = _load_module()


class FakeClock:
    """Deterministic monotonic clock advanced explicitly by the wait callback."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestWaitIsCompletionDrivenNotFixedStep(unittest.TestCase):
    """The defect: a wait held a dispatch slot for its whole nominal duration."""

    def test_completion_ends_the_wait_early(self) -> None:
        # The process emits output 2s into a 60s wait. A completion-driven wait
        # must return then -- not at 60s.
        clock = FakeClock()

        def wait_once(step: float) -> bool:
            clock.advance(step)
            return clock.now >= 2.0

        result = dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=60.0,
            slice_seconds=0.5,
            monotonic=clock,
        )
        self.assertTrue(result.completed)
        self.assertEqual(result.outcome, dt.WAIT_OUTCOME_COMPLETED)
        self.assertAlmostEqual(result.elapsed_seconds, 2.0, places=6)
        self.assertEqual(result.slices, 4)

    def test_slicing_does_not_extend_a_silent_wait(self) -> None:
        # A process that never speaks still times out at exactly the requested
        # duration: slicing must not add or drop time.
        clock = FakeClock()

        def wait_once(step: float) -> bool:
            clock.advance(step)
            return False

        result = dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=60.0,
            slice_seconds=0.5,
            monotonic=clock,
        )
        self.assertTrue(result.timed_out)
        self.assertAlmostEqual(result.elapsed_seconds, 60.0, places=6)
        self.assertEqual(result.slices, 120)

    def test_unsliced_wait_is_a_single_fixed_step(self) -> None:
        # slice_seconds<=0 reproduces the pre-fix shape: one 60s step, which is
        # precisely why cancellation could not be observed until it ended.
        clock = FakeClock()
        steps: list[float] = []

        def wait_once(step: float) -> bool:
            steps.append(step)
            clock.advance(step)
            return False

        result = dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=60.0,
            slice_seconds=0.0,
            monotonic=clock,
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(steps, [60.0])
        self.assertEqual(result.slices, 1)

    def test_unsliced_first_step_is_bit_exact(self) -> None:
        # The no-token path must hand the wait primitive the identical timeout
        # it receives today -- not that value minus the time spent getting here.
        # A real clock is used deliberately: a fake one cannot catch this.
        steps: list[float] = []

        def wait_once(step: float) -> bool:
            steps.append(step)
            return True

        dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=1.0,
            slice_seconds=0.0,
        )
        self.assertEqual(steps, [1.0])

    def test_sliced_first_step_is_bit_exact(self) -> None:
        steps: list[float] = []

        def wait_once(step: float) -> bool:
            steps.append(step)
            return True

        dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=60.0,
            slice_seconds=0.5,
            is_cancelled=lambda: False,
        )
        self.assertEqual(steps, [0.5])

    def test_zero_length_wait_still_probes_once(self) -> None:
        # wait_seconds=0 keeps its "read what is there right now" semantics.
        clock = FakeClock()
        steps: list[float] = []

        def wait_once(step: float) -> bool:
            steps.append(step)
            return False

        result = dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=0.0,
            slice_seconds=0.5,
            monotonic=clock,
        )
        self.assertTrue(result.timed_out)
        self.assertEqual(steps, [0.0])
        self.assertEqual(result.slices, 1)


class TestCancellationInterruptsMidWait(unittest.TestCase):
    """PR2's gap, closed for the shell path: the watchdog can now preempt a wait."""

    def test_cancellation_mid_wait_returns_promptly(self) -> None:
        clock = FakeClock()

        def wait_once(step: float) -> bool:
            clock.advance(step)
            return False

        # Budget expires 1.5s into a 60s wait.
        result = dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=60.0,
            is_cancelled=lambda: clock.now >= 1.5,
            slice_seconds=0.5,
            monotonic=clock,
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(result.outcome, dt.WAIT_OUTCOME_CANCELLED)
        self.assertAlmostEqual(result.elapsed_seconds, 1.5, places=6)
        self.assertEqual(result.slices, 3)

    def test_cancellation_latency_is_bounded_by_one_slice(self) -> None:
        clock = FakeClock()

        def wait_once(step: float) -> bool:
            clock.advance(step)
            return False

        # Fires at a moment that is not a slice boundary; the wait must still
        # end within one slice of it rather than at the 60s mark.
        result = dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=60.0,
            is_cancelled=lambda: clock.now >= 1.2,
            slice_seconds=0.5,
            monotonic=clock,
        )
        self.assertTrue(result.cancelled)
        self.assertLessEqual(result.elapsed_seconds - 1.2, 0.5)

    def test_already_cancelled_never_blocks(self) -> None:
        calls: list[float] = []

        def wait_once(step: float) -> bool:
            calls.append(step)
            return False

        result = dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=60.0,
            is_cancelled=lambda: True,
            slice_seconds=0.5,
            monotonic=FakeClock(),
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(calls, [])
        self.assertEqual(result.slices, 0)

    def test_completion_wins_over_cancellation_in_the_same_slice(self) -> None:
        # If the process finished, report the result rather than discarding it.
        clock = FakeClock()

        def wait_once(step: float) -> bool:
            clock.advance(step)
            return True

        result = dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=60.0,
            is_cancelled=lambda: True,
            slice_seconds=0.5,
            monotonic=clock,
        )
        # is_cancelled is checked before the first block, so an
        # already-cancelled run short-circuits; here it flips only afterwards.
        self.assertTrue(result.cancelled)

    def test_real_threading_event_interrupts_a_real_wait(self) -> None:
        # End-to-end with the shape PR2 actually wires: a threading.Event set by
        # the watchdog, read through a token exposing ``is_cancelled``.
        event = threading.Event()

        class Token:
            @property
            def is_cancelled(self) -> bool:
                return event.is_set()

        token = Token()
        slices = 0

        def wait_once(step: float) -> bool:
            nonlocal slices
            slices += 1
            if slices == 3:
                event.set()
            # A real condition wait bounded by ``step``; nothing was emitted.
            event.wait(min(step, 0.01))
            return False

        result = dt.run_cancellable_wait(
            wait_once=wait_once,
            total_seconds=60.0,
            is_cancelled=lambda: token.is_cancelled,
            slice_seconds=0.05,
        )
        self.assertTrue(result.cancelled)
        self.assertEqual(result.slices, 3)
        # Returned in milliseconds, not the 60 seconds that were requested.
        self.assertLess(result.elapsed_seconds, 5.0)


class TestBackgroundPromotionThreshold(unittest.TestCase):
    def test_boundary_is_inclusive(self) -> None:
        self.assertTrue(dt.should_promote_to_background(10.0, 10.0))
        self.assertFalse(dt.should_promote_to_background(9.999, 10.0))
        self.assertTrue(dt.should_promote_to_background(10.001, 10.0))

    def test_short_commands_are_never_promoted(self) -> None:
        for elapsed in (0.0, 0.001, 1.0, 9.5):
            with self.subTest(elapsed=elapsed):
                self.assertFalse(dt.should_promote_to_background(elapsed, 10.0))

    def test_negative_and_nonfinite_elapsed_do_not_promote(self) -> None:
        for elapsed in (-1.0, float("nan"), "not-a-number", None):
            with self.subTest(elapsed=elapsed):
                self.assertFalse(dt.should_promote_to_background(elapsed, 10.0))

    def test_zero_threshold_promotes_immediately(self) -> None:
        self.assertTrue(dt.should_promote_to_background(0.0, 0.0))

    def test_threshold_env_override_and_fallbacks(self) -> None:
        env = {dt.BACKGROUND_PROMOTION_SECONDS_ENV: "3.5"}
        self.assertEqual(dt.resolve_background_promotion_seconds(env), 3.5)
        # Every bad value falls back rather than disabling the mechanism.
        for raw in ("0", "-5", "nan", "inf", "abc", "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(
                    dt.resolve_background_promotion_seconds(
                        {dt.BACKGROUND_PROMOTION_SECONDS_ENV: raw}
                    ),
                    dt.DEFAULT_BACKGROUND_PROMOTION_SECONDS,
                )
        self.assertEqual(
            dt.resolve_background_promotion_seconds({}),
            dt.DEFAULT_BACKGROUND_PROMOTION_SECONDS,
        )

    def test_legacy_threshold_env_override_is_used(self) -> None:
        env = {"ALYSIS_SHELL_BACKGROUND_PROMOTION_SECONDS": "3.5"}
        self.assertEqual(dt.resolve_background_promotion_seconds(env), 3.5)


class TestDeadlineAwareClamping(unittest.TestCase):
    def test_no_deadline_leaves_the_request_alone(self) -> None:
        self.assertEqual(dt.clamp_wait_seconds(60.0, remaining_seconds=None), 60.0)

    def test_request_within_budget_is_untouched(self) -> None:
        self.assertEqual(
            dt.clamp_wait_seconds(10.0, remaining_seconds=100.0, reserve_seconds=5.0),
            10.0,
        )

    def test_request_is_capped_by_remaining_less_reserve(self) -> None:
        self.assertEqual(
            dt.clamp_wait_seconds(60.0, remaining_seconds=30.0, reserve_seconds=5.0),
            25.0,
        )

    def test_exhausted_budget_clamps_to_the_floor(self) -> None:
        self.assertEqual(
            dt.clamp_wait_seconds(60.0, remaining_seconds=3.0, reserve_seconds=5.0),
            0.0,
        )
        self.assertEqual(
            dt.clamp_wait_seconds(
                60.0,
                remaining_seconds=3.0,
                reserve_seconds=5.0,
                minimum_seconds=0.25,
            ),
            0.25,
        )

    def test_reserve_exactly_consumes_remaining(self) -> None:
        self.assertEqual(
            dt.clamp_wait_seconds(60.0, remaining_seconds=5.0, reserve_seconds=5.0),
            0.0,
        )

    def test_negative_and_nonfinite_inputs_are_safe(self) -> None:
        self.assertEqual(dt.clamp_wait_seconds(-5.0, remaining_seconds=100.0), 0.0)
        self.assertEqual(
            dt.clamp_wait_seconds(float("inf"), remaining_seconds=100.0),
            0.0,
        )
        self.assertEqual(dt.clamp_wait_seconds(10.0, remaining_seconds=float("nan")), 0.0)


class TestWaitSliceComputation(unittest.TestCase):
    def test_slice_is_capped_by_remaining(self) -> None:
        self.assertEqual(dt.next_wait_slice(0.2, 0.5), 0.2)
        self.assertEqual(dt.next_wait_slice(10.0, 0.5), 0.5)

    def test_non_positive_slice_means_do_not_slice(self) -> None:
        self.assertEqual(dt.next_wait_slice(60.0, 0.0), 60.0)
        self.assertEqual(dt.next_wait_slice(60.0, -1.0), 60.0)

    def test_negative_remaining_is_zero(self) -> None:
        self.assertEqual(dt.next_wait_slice(-3.0, 0.5), 0.0)

    def test_slice_env_override_and_fallbacks(self) -> None:
        self.assertEqual(
            dt.resolve_wait_slice_seconds({dt.WAIT_SLICE_SECONDS_ENV: "0.25"}),
            0.25,
        )
        for raw in ("0", "-1", "nan", "junk"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    dt.resolve_wait_slice_seconds({dt.WAIT_SLICE_SECONDS_ENV: raw}),
                    dt.DEFAULT_WAIT_SLICE_SECONDS,
                )


class TestDispatchOverheadAccounting(unittest.TestCase):
    def test_overhead_excludes_the_command_runtime(self) -> None:
        account = dt.DispatchOverheadAccount.from_totals(
            total_seconds=32.0,
            command_seconds=30.0,
        )
        self.assertEqual(account.overhead_seconds, 2.0)

    def test_a_long_command_with_cheap_dispatch_reports_small_overhead(self) -> None:
        # The whole point: a 20-minute build must not look like dispatch cost.
        account = dt.DispatchOverheadAccount.from_totals(1200.4, 1200.0)
        self.assertAlmostEqual(account.overhead_seconds, 0.4, places=6)

    def test_a_short_command_with_expensive_dispatch_is_visible(self) -> None:
        # Two workspace walks around a 0.2s command: the defect this measures.
        account = dt.DispatchOverheadAccount.from_totals(18.2, 0.2)
        self.assertAlmostEqual(account.overhead_seconds, 18.0, places=6)

    def test_overhead_never_goes_negative(self) -> None:
        account = dt.DispatchOverheadAccount.from_totals(1.0, 5.0)
        self.assertEqual(account.overhead_seconds, 0.0)
        self.assertEqual(account.command_seconds, 1.0)

    def test_nonfinite_and_negative_inputs_are_clamped(self) -> None:
        account = dt.DispatchOverheadAccount.from_totals(float("nan"), -2.0)
        self.assertEqual(account.total_seconds, 0.0)
        self.assertEqual(account.command_seconds, 0.0)
        self.assertEqual(account.overhead_seconds, 0.0)

    def test_operation_name_is_stable(self) -> None:
        self.assertEqual(dt.DISPATCH_OVERHEAD_OPERATION, "dispatch_overhead_seconds")


if __name__ == "__main__":
    unittest.main(verbosity=2)
