"""Tests for connection-drop classification, backoff, and retry budget.

Runnable two ways:

    python3 tests/test_transport_retry.py     # standalone, stdlib only
    pytest tests/test_transport_retry.py

The module under test is loaded directly from its file path so that importing
it never executes ``alysis_code/__init__`` or the httpx-dependent
``failure_category`` chain. That keeps these tests runnable in a bare
interpreter with no third-party packages installed.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "src" / "alysis_code" / "transport_retry.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_transport_retry", _MODULE_PATH)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise RuntimeError(f"cannot load {_MODULE_PATH}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tr = _load_module()


# The exact string both trials died on, from the regex-chess run artifacts.
PRODUCTION_FAILURE = (
    "LLM request failed: peer closed connection without sending complete "
    "message body (incomplete chunked read)"
)


class TestProductionSignature(unittest.TestCase):
    """The whole point: this exact message must be retryable."""

    def test_exact_production_string_matches(self) -> None:
        self.assertTrue(tr.is_connection_drop_message(PRODUCTION_FAILURE))

    def test_exact_production_string_as_an_exception(self) -> None:
        self.assertTrue(tr.is_connection_drop_error(RuntimeError(PRODUCTION_FAILURE)))

    def test_production_string_wrapped_the_way_the_client_wraps_it(self) -> None:
        # The transport raises RemoteProtocolError; the client re-raises it as
        # LLMError with the original as __cause__. Neither the outer type nor
        # a bare str() of the outer error is reliable, so the chain is walked.
        class RemoteProtocolError(Exception):
            pass

        class LLMError(Exception):
            pass

        try:
            try:
                raise RemoteProtocolError(
                    "peer closed connection without sending complete message body"
                )
            except RemoteProtocolError as cause:
                raise LLMError("LLM request failed") from cause
        except LLMError as wrapped:
            self.assertTrue(tr.is_connection_drop_error(wrapped))

    def test_reason_is_the_documented_marker(self) -> None:
        self.assertEqual(
            tr.STOP_REASON_TRANSPORT_CONNECTION_FAILURE,
            "transport_connection_failure",
        )
        self.assertEqual(
            tr.RETRY_REASON_TRANSPORT_CONNECTION_DROP,
            "transport_connection_drop",
        )


class TestSignatureMatching(unittest.TestCase):
    def test_positive_messages(self) -> None:
        for message in (
            "peer closed connection without sending complete message body",
            "incomplete chunked read",
            "Server disconnected without sending complete message body",
            "server disconnected",
            "RemoteProtocolError: connection closed",
            "Connection reset by peer",
            "connection aborted",
            "response body ended early",
            "http.client.IncompleteRead(0 bytes read)",
            "requests.exceptions.ChunkedEncodingError",
        ):
            with self.subTest(message=message):
                self.assertTrue(tr.is_connection_drop_message(message))

    def test_matching_is_case_insensitive(self) -> None:
        self.assertTrue(tr.is_connection_drop_message("PEER CLOSED CONNECTION"))
        self.assertTrue(tr.is_connection_drop_message("Incomplete Chunked Read"))

    def test_negative_messages(self) -> None:
        # These are real failures with different fixes. Matching them here
        # would hand the extra budget to errors that will never succeed.
        for message in (
            "",
            "   ",
            "LLM error 401: invalid api key",
            "LLM error 429: rate limit exceeded",
            "context length exceeded",
            "LLM error 400: unsupported parameter",
            "Name or service not known",
            "temporary failure in name resolution",
            "connect timeout",
            "read operation timed out",
            "docker daemon is not running",
        ):
            with self.subTest(message=message):
                self.assertFalse(tr.is_connection_drop_message(message))

    def test_none_is_not_a_match(self) -> None:
        self.assertFalse(tr.is_connection_drop_message(None))
        self.assertFalse(tr.is_connection_drop_error(None))

    def test_builtin_connection_errors_match_by_type(self) -> None:
        self.assertTrue(tr.is_connection_drop_error(ConnectionResetError()))
        self.assertTrue(tr.is_connection_drop_error(ConnectionAbortedError()))

    def test_httpx_style_types_match_by_name_without_importing_httpx(self) -> None:
        for name in sorted(tr.CONNECTION_DROP_TYPE_NAMES):
            with self.subTest(name=name):
                exc_type = type(name, (Exception,), {})
                self.assertTrue(tr.is_connection_drop_error(exc_type("no detail")))

    def test_unrelated_exceptions_do_not_match(self) -> None:
        for error in (
            ValueError("bad value"),
            RuntimeError("LLM error 401: invalid api key"),
            TimeoutError("read operation timed out"),
        ):
            with self.subTest(error=repr(error)):
                self.assertFalse(tr.is_connection_drop_error(error))

    def test_a_cyclic_cause_chain_terminates(self) -> None:
        # Defensive: a self-referential __context__ must not hang the walk.
        first = RuntimeError("one")
        second = RuntimeError("two")
        first.__cause__ = second
        second.__cause__ = first
        self.assertFalse(tr.is_connection_drop_error(first))


class TestBackoffSchedule(unittest.TestCase):
    def test_documented_schedule(self) -> None:
        self.assertEqual(
            tr.connection_drop_backoff_schedule(5),
            [2.0, 4.0, 8.0, 16.0, 32.0],
        )

    def test_schedule_length_follows_the_retry_count(self) -> None:
        self.assertEqual(tr.connection_drop_backoff_schedule(0), [])
        self.assertEqual(tr.connection_drop_backoff_schedule(3), [2.0, 4.0, 8.0])

    def test_default_retry_count_matches_the_schedule(self) -> None:
        self.assertEqual(tr.DEFAULT_CONNECTION_DROP_MAX_RETRIES, 5)
        self.assertEqual(
            len(tr.connection_drop_backoff_schedule()),
            tr.DEFAULT_CONNECTION_DROP_MAX_RETRIES,
        )

    def test_total_sleep_is_bounded(self) -> None:
        # Worst case must stay small enough to be obviously worth it against
        # losing the task outright.
        self.assertEqual(sum(tr.connection_drop_backoff_schedule()), 62.0)

    def test_delay_is_capped(self) -> None:
        # Beyond the schedule the delay flattens rather than doubling forever.
        for index in (5, 6, 10, 40):
            with self.subTest(index=index):
                self.assertEqual(tr.connection_drop_delay_seconds(index), 32.0)

    def test_jitter_is_symmetric_around_the_raw_delay(self) -> None:
        # 25% ratio: at sample 0.0 the delay is 0.75x, at 1.0 it is 1.25x.
        self.assertAlmostEqual(tr.connection_drop_delay_seconds(0, jitter_sample=0.0), 1.5)
        self.assertAlmostEqual(tr.connection_drop_delay_seconds(0, jitter_sample=1.0), 2.5)
        self.assertAlmostEqual(tr.connection_drop_delay_seconds(0, jitter_sample=0.5), 2.0)

    def test_jitter_never_escapes_the_cap_or_goes_negative(self) -> None:
        for sample in (-5.0, 0.0, 0.5, 1.0, 5.0):
            with self.subTest(sample=sample):
                delay = tr.connection_drop_delay_seconds(4, jitter_sample=sample)
                self.assertGreaterEqual(delay, 0.0)
                self.assertLessEqual(delay, 32.0)

    def test_negative_index_is_clamped(self) -> None:
        self.assertEqual(tr.connection_drop_delay_seconds(-3), 2.0)


class TestRetryBudget(unittest.TestCase):
    def test_budget_floor_is_applied(self) -> None:
        # The general default is 1 retry; a connection drop gets 5.
        self.assertEqual(tr.connection_drop_retry_budget(1), 5)
        self.assertEqual(tr.connection_drop_retry_budget(0), 5)

    def test_a_larger_configured_budget_is_preserved(self) -> None:
        # A campaign that deliberately raised the provider budget keeps it;
        # this only guarantees a floor.
        self.assertEqual(tr.connection_drop_retry_budget(9), 9)

    def test_exhaustion_predicate(self) -> None:
        self.assertFalse(tr.connection_drop_budget_exhausted(0, 5))
        self.assertFalse(tr.connection_drop_budget_exhausted(4, 5))
        self.assertTrue(tr.connection_drop_budget_exhausted(5, 5))
        self.assertTrue(tr.connection_drop_budget_exhausted(6, 5))

    def test_a_full_run_of_the_budget(self) -> None:
        # Walk the loop the way provider_limits does: retry until exhausted,
        # collecting the delay each time.
        allowed = tr.connection_drop_retry_budget(1)
        delays: list[float] = []
        used = 0
        while not tr.connection_drop_budget_exhausted(used, allowed):
            delays.append(tr.connection_drop_delay_seconds(used))
            used += 1
        self.assertEqual(used, 5)
        self.assertEqual(delays, [2.0, 4.0, 8.0, 16.0, 32.0])

    def test_zero_budget_retries_nothing(self) -> None:
        self.assertTrue(tr.connection_drop_budget_exhausted(0, 0))


class TestFailureLabelling(unittest.TestCase):
    def test_untagged_connection_drop_is_still_labelled(self) -> None:
        # A drop that never reached the retry loop is still a transport
        # failure and should be named as one.
        error = RuntimeError(PRODUCTION_FAILURE)
        self.assertEqual(
            tr.transport_connection_failure_reason(error),
            "transport_connection_failure",
        )

    def test_tagging_labels_an_otherwise_anonymous_error(self) -> None:
        error = RuntimeError("something went wrong")
        self.assertIsNone(tr.transport_connection_failure_reason(error))
        tr.mark_transport_connection_failure(error)
        self.assertEqual(
            tr.transport_connection_failure_reason(error),
            "transport_connection_failure",
        )

    def test_a_tag_on_a_cause_is_found_through_the_chain(self) -> None:
        cause = RuntimeError("dropped")
        tr.mark_transport_connection_failure(cause)
        wrapper = RuntimeError("wrapped")
        wrapper.__cause__ = cause
        self.assertEqual(
            tr.transport_connection_failure_reason(wrapper),
            "transport_connection_failure",
        )

    def test_unrelated_errors_are_not_labelled(self) -> None:
        for error in (
            None,
            ValueError("bad value"),
            RuntimeError("LLM error 401: invalid api key"),
        ):
            with self.subTest(error=repr(error)):
                self.assertIsNone(tr.transport_connection_failure_reason(error))

    def test_tagging_never_raises_on_an_unassignable_exception(self) -> None:
        # Some exception types refuse attribute assignment. Losing the label
        # is acceptable; raising from inside a failure path is not.
        class RejectsAttributes(Exception):
            def __setattr__(self, name: str, value: object) -> None:
                raise AttributeError("immutable")

        error = RejectsAttributes("nope")
        tr.mark_transport_connection_failure(error)  # must not raise
        self.assertIsNone(tr.transport_connection_failure_reason(error))

    def test_tagging_none_is_a_no_op(self) -> None:
        tr.mark_transport_connection_failure(None)  # must not raise


if __name__ == "__main__":
    unittest.main(verbosity=2)
