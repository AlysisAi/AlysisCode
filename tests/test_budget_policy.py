"""Tests for run-budget policy: sizing, stop-reason, watchdog, checkpoint.

Runnable two ways:

    python3 tests/test_budget_policy.py     # standalone, stdlib only
    pytest tests/test_budget_policy.py

The module under test is loaded directly from its file path so that importing
it never executes ``alysis_code/__init__`` or any of the package's
dependency-heavy import chain. That keeps these tests runnable in a bare
interpreter with no third-party packages installed.
"""

from __future__ import annotations

import importlib.util
import threading
import time
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "src" / "alysis_code" / "budget_policy.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_budget_policy", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bp = _load_module()


class TestExitCodeDecisionTable(unittest.TestCase):
    """The defect: a budget stop exited 1 and harnesses called it a crash."""

    def test_budget_stop_exits_zero(self) -> None:
        self.assertEqual(bp.exit_code_for_stop("run_budget_exhausted"), 0)
        self.assertEqual(bp.exit_code_for_stop(bp.STOP_REASON_RUN_BUDGET_EXHAUSTED), 0)

    def test_normal_completion_exits_zero(self) -> None:
        for reason in ("", None, "completed", "session_close"):
            with self.subTest(reason=reason):
                self.assertEqual(bp.exit_code_for_stop(reason), 0)

    def test_empty_response_anomaly_retry_exhausted_exits_zero(self) -> None:
        # The second graceful stop found in the wild: trial-1 gpt2-codegolf
        # ended "Stopped after empty_response_anomaly_retry_exhausted" and
        # exited 1, so the harness recorded NonZeroAgentExitCodeError.
        self.assertEqual(bp.exit_code_for_stop("empty_response_anomaly_retry_exhausted"), 0)
        self.assertEqual(
            bp.exit_code_for_stop(bp.STOP_REASON_EMPTY_RESPONSE_ANOMALY_RETRY_EXHAUSTED),
            0,
        )

    def test_genuine_errors_stay_nonzero(self) -> None:
        for reason in (
            "terminal_error",
            "provider_unavailable",
            "empty_response_stall",
            "step_budget_exhausted",
            "cancelled_by_user",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(bp.exit_code_for_stop(reason), 1)

    def test_unknown_reasons_stay_nonzero(self) -> None:
        # The registry is a whitelist. Anything not on it -- including a
        # near-miss spelling of a member, and a reason invented later -- is a
        # failure until someone deliberately adds it.
        for reason in (
            "empty_response_anomaly_budget_exhausted",
            "empty_response_anomaly",
            "retry_exhausted",
            "run_budget",
            "transport_connection_failure",
            "some_future_reason",
        ):
            with self.subTest(reason=reason):
                self.assertEqual(bp.exit_code_for_stop(reason), 1)

    def test_caller_supplied_failure_code_is_honoured(self) -> None:
        # Infrastructure failures keep EX_TEMPFAIL rather than collapsing to 1.
        self.assertEqual(
            bp.exit_code_for_stop("provider_unavailable", failure_exit_code=75),
            75,
        )
        # ...but a budget stop is still a clean exit regardless of that code.
        self.assertEqual(
            bp.exit_code_for_stop("run_budget_exhausted", failure_exit_code=75),
            0,
        )

    def test_reason_matching_is_case_and_space_insensitive(self) -> None:
        self.assertEqual(bp.exit_code_for_stop("  Run_Budget_Exhausted  "), 0)

    def test_is_budget_stop_predicate(self) -> None:
        self.assertTrue(bp.is_budget_stop("run_budget_exhausted"))
        self.assertFalse(bp.is_budget_stop("cancelled_by_user"))
        self.assertFalse(bp.is_budget_stop(None))
        # "completed" is a graceful exit but it is NOT a budget stop; only the
        # budget stop may claim the run_budget_exhausted marker.
        self.assertFalse(bp.is_budget_stop("completed"))
        # ...and neither may the other clean stop.
        self.assertFalse(bp.is_budget_stop("empty_response_anomaly_retry_exhausted"))


class TestCleanStopRegistry(unittest.TestCase):
    """Adding a graceful stop must be a constant, not new plumbing."""

    def test_registry_membership(self) -> None:
        self.assertEqual(
            bp.CLEAN_STOP_REASONS,
            frozenset(
                {
                    "run_budget_exhausted",
                    "empty_response_anomaly_retry_exhausted",
                    "output_token_limit",
                }
            ),
        )

    def test_is_clean_stop_accepts_every_member(self) -> None:
        for reason in bp.CLEAN_STOP_REASONS:
            with self.subTest(reason=reason):
                self.assertTrue(bp.is_clean_stop(reason))
                self.assertEqual(bp.exit_code_for_stop(reason), 0)

    def test_is_clean_stop_rejects_non_members(self) -> None:
        for reason in ("cancelled_by_user", "terminal_error", "boom", None):
            with self.subTest(reason=reason):
                self.assertFalse(bp.is_clean_stop(reason))

    def test_ordinary_completion_is_not_a_self_stop(self) -> None:
        # It exits zero, but nothing should record it as a stop the run chose:
        # is_clean_stop gates the stop_reason marker, and "the turn ended" is
        # not a stop reason.
        for reason in ("", "completed", "session_close", None):
            with self.subTest(reason=reason):
                self.assertFalse(bp.is_clean_stop(reason))
                self.assertEqual(bp.exit_code_for_stop(reason), 0)

    def test_clean_stop_matching_is_case_and_space_insensitive(self) -> None:
        self.assertTrue(bp.is_clean_stop("  Empty_Response_Anomaly_Retry_Exhausted  "))

    def test_clean_stops_exit_zero_even_with_a_custom_failure_code(self) -> None:
        # A clean stop outranks the caller's failure code, the same way a
        # budget stop already did.
        for reason in bp.CLEAN_STOP_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(bp.exit_code_for_stop(reason, failure_exit_code=75), 0)

    def test_graceful_set_is_the_union(self) -> None:
        self.assertEqual(
            bp.GRACEFUL_STOP_REASONS,
            bp.NORMAL_COMPLETION_REASONS | bp.CLEAN_STOP_REASONS,
        )
        # The two halves are disjoint: a completion is not a self-stop.
        self.assertFalse(bp.NORMAL_COMPLETION_REASONS & bp.CLEAN_STOP_REASONS)

    def test_registry_entries_are_lowercase_and_stripped(self) -> None:
        # is_clean_stop lowercases its input before the lookup, so a member
        # that was not already lowercase could never match.
        for reason in bp.CLEAN_STOP_REASONS:
            with self.subTest(reason=reason):
                self.assertEqual(reason, reason.strip().lower())


class TestBudgetCancellationToken(unittest.TestCase):
    def setUp(self) -> None:
        self.event = threading.Event()
        self.token = bp.BudgetCancellationToken(self.event)

    def test_quiet_until_the_event_is_set(self) -> None:
        self.assertFalse(self.token.is_cancelled)
        self.token.throw_if_cancelled()  # must not raise

    def test_raises_once_the_event_is_set(self) -> None:
        self.event.set()
        self.assertTrue(self.token.is_cancelled)
        with self.assertRaises(bp.RunBudgetCancelled):
            self.token.throw_if_cancelled()

    def test_caller_supplied_reason_is_overridden(self) -> None:
        # The turn engine passes the literal "cancelled_by_user" at every
        # checkpoint. If that won, a budget stop would be finalized as a user
        # abort and exit non-zero -- the exact defect being fixed.
        self.event.set()
        with self.assertRaises(bp.RunBudgetCancelled) as caught:
            self.token.throw_if_cancelled("cancelled_by_user")
        self.assertEqual(caught.exception.reason, "run_budget_exhausted")
        self.assertTrue(bp.is_budget_cancellation(caught.exception))

    def test_injected_error_class_is_used(self) -> None:
        # Production injects CooperativeCancellationError so existing handlers
        # still catch it; simulate that contract here.
        class FakeCooperativeCancellationError(Exception):
            def __init__(self, reason: str = "cancelled_by_user") -> None:
                super().__init__(reason)
                self.reason = reason

        event = threading.Event()
        event.set()
        token = bp.BudgetCancellationToken(event, error_class=FakeCooperativeCancellationError)
        with self.assertRaises(FakeCooperativeCancellationError) as caught:
            token.throw_if_cancelled()
        self.assertTrue(bp.is_budget_cancellation(caught.exception))

    def test_user_cancellation_is_not_a_budget_cancellation(self) -> None:
        class UserCancel(Exception):
            reason = "cancelled_by_user"

        self.assertFalse(bp.is_budget_cancellation(UserCancel()))
        self.assertFalse(bp.is_budget_cancellation(RuntimeError("boom")))
        self.assertFalse(bp.is_budget_cancellation(None))


class TestWatchdogFiresDuringBlockedOperation(unittest.TestCase):
    """The enforcement defect: nothing re-checks the clock mid-operation."""

    def test_fires_while_the_main_thread_is_blocked(self) -> None:
        # Simulates the production hang: an operation that would block far
        # longer than the budget (a collect(timeout_s=None) on a subagent, a
        # tool with no deadline-derived timeout). No cooperative checkpoint is
        # reachable from inside it, so only an external timer can stop it.
        watchdog = bp.BudgetWatchdog(budget_seconds=0.05, grace_seconds=0.05)
        released = threading.Event()

        def blocking_operation() -> float:
            start = time.monotonic()
            # Would block for 30s; the watchdog must cut it short.
            released.wait(timeout=30.0)
            return time.monotonic() - start

        with watchdog:
            self.assertFalse(watchdog.fired)
            worker_result: list[float] = []

            def worker() -> None:
                worker_result.append(blocking_operation())

            thread = threading.Thread(target=worker, daemon=True)
            thread.start()
            # The watchdog's event is what unblocks the operation.
            self.assertTrue(watchdog.event.wait(timeout=10.0))
            released.set()
            thread.join(timeout=10.0)

        self.assertTrue(watchdog.fired)
        self.assertTrue(worker_result)
        self.assertLess(
            worker_result[0],
            10.0,
            "watchdog did not interrupt the simulated blocked operation",
        )

    def test_token_driven_checkpoint_unblocks_the_run(self) -> None:
        # End-to-end shape of the fix: watchdog -> event -> token -> raise at
        # the next cooperative checkpoint.
        watchdog = bp.BudgetWatchdog(budget_seconds=0.01, grace_seconds=0.01)
        token = bp.BudgetCancellationToken(watchdog.event)
        with watchdog:
            self.assertTrue(watchdog.event.wait(timeout=10.0))
            with self.assertRaises(bp.RunBudgetCancelled):
                token.throw_if_cancelled("cancelled_by_user")

    def test_on_fire_callback_runs_and_swallows_errors(self) -> None:
        calls: list[int] = []

        def on_fire() -> None:
            calls.append(1)
            raise RuntimeError("telemetry exploded")

        watchdog = bp.BudgetWatchdog(budget_seconds=0.01, grace_seconds=0.0, on_fire=on_fire)
        with watchdog:
            self.assertTrue(watchdog.event.wait(timeout=10.0))
        # The event is still set even though the callback raised: a broken
        # telemetry sink must never disable enforcement.
        self.assertTrue(watchdog.fired)
        self.assertEqual(calls, [1])

    def test_disarm_before_expiry_never_fires(self) -> None:
        # The overwhelmingly common case: the run finishes well inside budget.
        watchdog = bp.BudgetWatchdog(budget_seconds=30.0, grace_seconds=30.0)
        watchdog.arm()
        self.assertTrue(watchdog.armed)
        watchdog.disarm()
        self.assertFalse(watchdog.armed)
        self.assertFalse(watchdog.fired)
        time.sleep(0.05)
        self.assertFalse(watchdog.fired)

    def test_arming_twice_schedules_one_timer(self) -> None:
        started: list[float] = []

        class FakeTimer:
            def __init__(self, interval: float, function) -> None:
                self.interval = interval
                self.function = function
                self.daemon = False

            def start(self) -> None:
                started.append(self.interval)

            def cancel(self) -> None:
                pass

        watchdog = bp.BudgetWatchdog(
            budget_seconds=10.0, grace_seconds=5.0, timer_factory=FakeTimer
        )
        watchdog.arm()
        watchdog.arm()
        self.assertEqual(started, [15.0])


class TestGraceWindowBounds(unittest.TestCase):
    def test_default_grace_is_sixty_seconds(self) -> None:
        self.assertEqual(bp.DEFAULT_BUDGET_GRACE_SECONDS, 60.0)
        self.assertEqual(bp.resolve_budget_grace_seconds({}), 60.0)

    def test_fire_delay_is_budget_plus_grace(self) -> None:
        watchdog = bp.BudgetWatchdog(budget_seconds=3600.0, grace_seconds=60.0)
        self.assertEqual(watchdog.fire_delay_seconds(), 3660.0)

    def test_zero_grace_means_cancel_at_the_deadline(self) -> None:
        self.assertEqual(bp.resolve_budget_grace_seconds({"ALYSIS_BUDGET_GRACE_SECONDS": "0"}), 0.0)
        watchdog = bp.BudgetWatchdog(budget_seconds=100.0, grace_seconds=0.0)
        self.assertEqual(watchdog.fire_delay_seconds(), 100.0)

    def test_explicit_grace_is_honoured(self) -> None:
        self.assertEqual(
            bp.resolve_budget_grace_seconds({"ALYSIS_BUDGET_GRACE_SECONDS": "12.5"}),
            12.5,
        )

    def test_negative_and_unparseable_grace_falls_back(self) -> None:
        for raw in ("-1", "-0.5", "nan", "inf", "-inf", "soon", "", "   "):
            with self.subTest(raw=raw):
                self.assertEqual(
                    bp.resolve_budget_grace_seconds({"ALYSIS_BUDGET_GRACE_SECONDS": raw}),
                    60.0,
                )

    def test_negative_grace_cannot_pull_the_firing_time_earlier(self) -> None:
        # Defensive: even if a negative value reached the constructor it is
        # clamped, so the watchdog can never fire before the budget is spent.
        watchdog = bp.BudgetWatchdog(budget_seconds=100.0, grace_seconds=-50.0)
        self.assertEqual(watchdog.fire_delay_seconds(), 100.0)


class TestRunBudgetParsing(unittest.TestCase):
    def test_default_matches_previous_hardcoded_behaviour(self) -> None:
        self.assertEqual(bp.DEFAULT_RUN_BUDGET_SECONDS, 3600.0)
        self.assertEqual(bp.resolve_run_budget_seconds({}), 3600.0)

    def test_env_override_is_used(self) -> None:
        self.assertEqual(
            bp.resolve_run_budget_seconds({"ALYSIS_RUN_BUDGET_SECONDS": "10800"}),
            10800.0,
        )
        self.assertEqual(
            bp.resolve_run_budget_seconds({"ALYSIS_RUN_BUDGET_SECONDS": " 900.5 "}),
            900.5,
        )

    def test_invalid_values_fall_back_to_the_default(self) -> None:
        # A misconfigured budget must never abort a run, and must never
        # silently become unlimited.
        for raw in ("0", "-1", "-3600", "nan", "inf", "-inf", "abc", "", "   ", "1e", "None"):
            with self.subTest(raw=raw):
                self.assertEqual(
                    bp.resolve_run_budget_seconds({"ALYSIS_RUN_BUDGET_SECONDS": raw}),
                    3600.0,
                )

    def test_unset_variable_uses_the_default(self) -> None:
        self.assertEqual(
            bp.resolve_run_budget_seconds({"UNRELATED": "5"}),
            3600.0,
        )

    def test_checkpoint_fraction_parsing(self) -> None:
        self.assertEqual(bp.DEFAULT_BUDGET_CHECKPOINT_FRACTION, 0.33)
        self.assertEqual(bp.resolve_checkpoint_fraction({}), 0.33)
        self.assertEqual(
            bp.resolve_checkpoint_fraction({"ALYSIS_BUDGET_CHECKPOINT_FRACTION": "0.5"}),
            0.5,
        )
        self.assertEqual(
            bp.resolve_checkpoint_fraction({"ALYSIS_BUDGET_CHECKPOINT_FRACTION": "1"}),
            1.0,
        )

    def test_out_of_range_checkpoint_fraction_falls_back(self) -> None:
        # >1 would arm the checkpoint past the end of the budget; <=0 would
        # fire it before the run had a chance to do anything.
        for raw in ("0", "-0.2", "1.5", "100", "nan", "inf", "half", ""):
            with self.subTest(raw=raw):
                self.assertEqual(
                    bp.resolve_checkpoint_fraction({"ALYSIS_BUDGET_CHECKPOINT_FRACTION": raw}),
                    0.33,
                )


class TestProgressCheckpoint(unittest.TestCase):
    def _checkpoint(self, fraction: float = 0.33) -> object:
        return bp.ProgressCheckpoint(fraction=fraction)

    def test_does_not_fire_before_the_threshold(self) -> None:
        checkpoint = self._checkpoint()
        for elapsed in (0.0, 0.1, 0.32, 0.3299):
            with self.subTest(elapsed=elapsed):
                self.assertFalse(
                    checkpoint.check(
                        elapsed_fraction=elapsed,
                        material_edit_count=0,
                        verification_attempt_count=0,
                    )
                )
        self.assertFalse(checkpoint.fired)

    def test_fires_at_the_threshold_with_zero_material_actions(self) -> None:
        checkpoint = self._checkpoint()
        self.assertTrue(
            checkpoint.check(
                elapsed_fraction=0.33,
                material_edit_count=0,
                verification_attempt_count=0,
            )
        )
        self.assertTrue(checkpoint.fired)

    def test_fires_exactly_once(self) -> None:
        checkpoint = self._checkpoint()
        self.assertTrue(
            checkpoint.check(
                elapsed_fraction=0.4,
                material_edit_count=0,
                verification_attempt_count=0,
            )
        )
        for elapsed in (0.5, 0.75, 0.9, 1.0, 2.0):
            with self.subTest(elapsed=elapsed):
                self.assertFalse(
                    checkpoint.check(
                        elapsed_fraction=elapsed,
                        material_edit_count=0,
                        verification_attempt_count=0,
                    )
                )

    def test_never_fires_when_material_progress_exists(self) -> None:
        for edits, verifications in ((1, 0), (0, 1), (3, 2), (0, 7)):
            with self.subTest(edits=edits, verifications=verifications):
                checkpoint = self._checkpoint()
                self.assertFalse(
                    checkpoint.check(
                        elapsed_fraction=0.9,
                        material_edit_count=edits,
                        verification_attempt_count=verifications,
                    )
                )
                self.assertFalse(checkpoint.fired)

    def test_a_run_that_starts_working_late_is_left_alone(self) -> None:
        # Crosses the threshold with nothing done, but an edit lands in the
        # same step: no nudge, because there is now material progress.
        checkpoint = self._checkpoint()
        self.assertFalse(
            checkpoint.check(
                elapsed_fraction=0.35,
                material_edit_count=1,
                verification_attempt_count=0,
            )
        )
        self.assertFalse(checkpoint.fired)

    def test_unbudgeted_run_never_fires(self) -> None:
        # elapsed_fraction is None when no finite budget applies.
        checkpoint = self._checkpoint()
        self.assertFalse(
            checkpoint.check(
                elapsed_fraction=None,
                material_edit_count=0,
                verification_attempt_count=0,
            )
        )
        self.assertFalse(checkpoint.fired)

    def test_non_numeric_elapsed_fraction_is_ignored(self) -> None:
        checkpoint = self._checkpoint()
        for elapsed in ("later", object()):
            with self.subTest(elapsed=elapsed):
                self.assertFalse(
                    checkpoint.check(
                        elapsed_fraction=elapsed,
                        material_edit_count=0,
                        verification_attempt_count=0,
                    )
                )

    def test_fraction_defaults_from_environment(self) -> None:
        checkpoint = bp.ProgressCheckpoint(environ={"ALYSIS_BUDGET_CHECKPOINT_FRACTION": "0.5"})
        self.assertEqual(checkpoint.fraction, 0.5)
        self.assertFalse(
            checkpoint.check(
                elapsed_fraction=0.4,
                material_edit_count=0,
                verification_attempt_count=0,
            )
        )
        self.assertTrue(
            checkpoint.check(
                elapsed_fraction=0.5,
                material_edit_count=0,
                verification_attempt_count=0,
            )
        )


class TestCheckpointNoticeText(unittest.TestCase):
    """The one model-visible string this PR adds. Pinned verbatim."""

    def test_exact_text(self) -> None:
        self.assertEqual(
            bp.BUDGET_CHECKPOINT_NOTICE,
            "Budget checkpoint: no material progress recorded yet. "
            "Reassess approach or report the concrete blocker.",
        )

    def test_stop_reason_marker(self) -> None:
        self.assertEqual(bp.STOP_REASON_RUN_BUDGET_EXHAUSTED, "run_budget_exhausted")
        self.assertEqual(bp.PROGRESS_CHECKPOINT_FAILED_EVENT, "progress_checkpoint_failed")
        # Must match the trigger string the turn engine already passes around,
        # or the stop stays non-zero and nothing joins up.
        self.assertEqual(
            bp.STOP_REASON_EMPTY_RESPONSE_ANOMALY_RETRY_EXHAUSTED,
            "empty_response_anomaly_retry_exhausted",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
