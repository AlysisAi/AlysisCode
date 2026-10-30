"""E1: a Computer Use host request also expires at its original (lease-clamped) wall
expires_at, not only at the monotonic deadline derived from it.

Shorten-only hardening, not a continuous-clock solution. Clocks are injected per thread
through a proxy for stdio_bridge's ``time`` module; nothing sleeps or changes real clocks.
"""

from __future__ import annotations

import threading
import time as real_time
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from typing import Any

import pytest
from test_ide_computer_use_host import (
    _bridge_session,
    _invoke_host_request,
    _lines,
    _request,
    _wait_for_event,
)

import alysis_code.ide.stdio_bridge as stdio_bridge_module
from alysis_code.ide.stdio_bridge import BridgeCancellationError
from alysis_code.task_budget import iso as budget_iso
from alysis_code.task_budget import stamp as budget_stamp

pytestmark = pytest.mark.usefixtures("mac_computer_use_protocol")

TIMED_OUT = {
    "error": "computer_use_timeout",
    "message": "The Computer Use host did not respond before expiry.",
}
ARRIVED_LATE = {
    "error": "computer_use_timeout",
    "message": "The Computer Use host response arrived after expiry.",
}
ADOPTED_LATE = {
    "error": "computer_use_timeout",
    "message": "The Computer Use host response was not adopted before expiry.",
}
HOST_RESULT = {"snapshot_id": "snapshot-2", "elements": [{"id": "e-1", "label": "Late secret"}]}


class _Clocks:
    """Per-thread wall/monotonic overrides; every other attribute is the real time module."""

    def __init__(self) -> None:
        self.wall: dict[threading.Thread, Callable[[], float]] = {}
        self.mono: dict[threading.Thread, Callable[[], float]] = {}

    def time(self) -> float:
        override = self.wall.get(threading.current_thread())
        return override() if override else real_time.time()

    def monotonic(self) -> float:
        override = self.mono.get(threading.current_thread())
        return override() if override else real_time.monotonic()

    def __getattr__(self, name: str) -> Any:
        return getattr(real_time, name)


@pytest.fixture
def clocks(monkeypatch: pytest.MonkeyPatch) -> _Clocks:
    proxy = _Clocks()
    monkeypatch.setattr(stdio_bridge_module, "time", proxy)
    return proxy


class _Exchange:
    def __init__(self, tmp_path: Any, *, budget_lease_seconds: float | None = None) -> None:
        self.bridge, self.out, self.session, self.job = _bridge_session(tmp_path)
        if budget_lease_seconds is not None:
            lease = budget_iso(real_time.time() + budget_lease_seconds)
            self.session.computer_use_host = replace(
                self.session.computer_use_host, task_budget={"leaseExpiresAt": lease}
            )
            self.session.task_budget_authority = SimpleNamespace(  # type: ignore[assignment]
                binding={"leaseExpiresAt": lease, "leaseId": "lease-1"},
                ensure_lease=lambda: None,
            )
        self.result: dict[str, Any] = {}
        self.failures: list[BaseException] = []
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()
        self.event = _wait_for_event(self.out, "computer_use_request")
        self.payload = self.event["payload"]
        self.pending = self.session.pending_computer_use[self.payload["request_id"]]
        self.expiry = budget_stamp(self.payload["expires_at"])

    def _run(self) -> None:
        try:
            _invoke_host_request(self.bridge, self.session, "computer_state", {}, self.result)
        except BaseException as exc:  # cancellation is an expected outcome in some cases
            self.failures.append(exc)

    def late_after_done(self, value: Callable[[], float]) -> Callable[[], float]:
        return lambda: value() if self.pending.done.is_set() else real_time.time()

    def respond(self, key: str, **overrides: Any) -> dict[str, Any]:
        params = {
            "session_id": self.session.session_id,
            "request_id": self.payload["request_id"],
            "response_nonce": self.payload["response_nonce"],
            "operation_id": self.payload["operation_id"],
            "job_id": self.event["job_id"],
            "tool_call_id": self.payload["tool_call_id"],
            "tool_name": self.payload["tool_name"],
            "arguments_fingerprint": self.payload["arguments_fingerprint"],
            "expires_at": self.payload["expires_at"],
            "ok": True,
            "result": HOST_RESULT,
            **overrides,
        }
        self.bridge.process_line(_request("computer.respond", params, key) + "\n")
        return next(line for line in _lines(self.out) if line.get("id") == key)

    def finish(self) -> None:
        self.worker.join(timeout=2)
        if self.worker.is_alive():  # never leave a waiter behind a failed assertion
            self.bridge._cancel_pending_computer_use(self.session, "test_cleanup")  # noqa: SLF001
            self.worker.join(timeout=2)
            raise AssertionError("the Computer Use waiter did not finish")


@pytest.mark.parametrize("past", [0.0, 1.0], ids=["equal", "beyond"])
def test_wall_time_at_or_after_original_expiry_rejects_a_response_with_unchanged_monotonic(
    tmp_path: Any, clocks: _Clocks, past: float
) -> None:
    exchange = _Exchange(tmp_path)
    clocks.wall[threading.current_thread()] = lambda: exchange.expiry + past
    acknowledgement = exchange.respond("late-wall")
    exchange.finish()
    assert acknowledgement["error"]["code"] == "expired_computer_request"
    assert exchange.result == ARRIVED_LATE


def test_timely_success_is_still_adopted(tmp_path: Any, clocks: _Clocks) -> None:
    exchange = _Exchange(tmp_path)
    just_before = lambda: exchange.expiry - 0.001  # noqa: E731
    clocks.wall[threading.current_thread()] = just_before
    clocks.wall[exchange.worker] = exchange.late_after_done(just_before)
    acknowledgement = exchange.respond("timely")
    exchange.finish()
    assert acknowledgement["ok"] is True and acknowledgement["result"]["status"] == "resolved"
    assert exchange.result == HOST_RESULT


def test_monotonic_expiry_still_rejects_when_wall_time_regressed(
    tmp_path: Any, clocks: _Clocks
) -> None:
    exchange = _Exchange(tmp_path)
    regressed = lambda: real_time.time() - 3600  # noqa: E731
    clocks.wall[threading.current_thread()] = regressed
    clocks.mono[threading.current_thread()] = lambda: exchange.pending.deadline
    acknowledgement = exchange.respond("monotonic-expired")
    exchange.finish()
    assert acknowledgement["error"]["code"] == "expired_computer_request"
    assert exchange.result == ARRIVED_LATE

    waiting = _Exchange(tmp_path)
    clocks.wall[waiting.worker] = regressed
    clocks.mono[waiting.worker] = lambda: waiting.pending.deadline + 1
    waiting.finish()
    assert waiting.result == TIMED_OUT


def test_earlier_clamped_lease_expiry_governs(tmp_path: Any, clocks: _Clocks) -> None:
    exchange = _Exchange(tmp_path, budget_lease_seconds=5)
    lease = exchange.session.task_budget_authority.binding["leaseExpiresAt"]
    assert exchange.payload["expires_at"] == lease
    assert exchange.payload["budget_lease_id"] == "lease-1"
    clocks.wall[threading.current_thread()] = lambda: budget_stamp(lease)
    acknowledgement = exchange.respond("lease-expired")
    exchange.finish()
    assert acknowledgement["error"]["code"] == "expired_computer_request"
    assert exchange.result == ARRIVED_LATE


@pytest.mark.parametrize("clock", ["wall", "monotonic"])
def test_response_accepted_before_expiry_is_not_adopted_by_a_late_waking_caller(
    tmp_path: Any, clocks: _Clocks, clock: str
) -> None:
    exchange = _Exchange(tmp_path)
    if clock == "wall":
        clocks.wall[exchange.worker] = exchange.late_after_done(lambda: exchange.expiry)
    else:
        clocks.mono[exchange.worker] = lambda: (
            exchange.pending.deadline if exchange.pending.done.is_set() else real_time.monotonic()
        )
    acknowledgement = exchange.respond("accepted-in-time")
    exchange.finish()
    assert acknowledgement["ok"] is True and acknowledgement["result"]["status"] == "resolved"
    assert exchange.result == ADOPTED_LATE
    assert "Late secret" not in repr(exchange.result)
    assert exchange.session.pending_computer_use == {}
    assert exchange.respond("after-adoption")["error"]["code"] == "duplicate_response"


def test_no_response_wait_expires_on_wall_time(tmp_path: Any, clocks: _Clocks) -> None:
    exchange = _Exchange(tmp_path)
    clocks.wall[exchange.worker] = lambda: exchange.expiry
    exchange.finish()
    assert exchange.result == TIMED_OUT
    assert exchange.respond("after-timeout")["error"]["code"] == "duplicate_response"


def test_engine_cancellation_first_cause_is_not_replaced_by_expiry(
    tmp_path: Any, clocks: _Clocks
) -> None:
    stopped = _Exchange(tmp_path)
    clocks.wall[stopped.worker] = stopped.late_after_done(lambda: stopped.expiry + 1)
    stopped.bridge.process_line(
        _request(
            "session.cancel",
            {"session_id": stopped.session.session_id, "reason": "user_stop"},
            "stop",
        )
        + "\n"
    )
    stopped.finish()
    assert stopped.result == {}
    assert len(stopped.failures) == 1 and isinstance(stopped.failures[0], BridgeCancellationError)
    assert str(stopped.failures[0]) == "user_stop"

    shutdown = _Exchange(tmp_path)
    clocks.wall[shutdown.worker] = shutdown.late_after_done(lambda: shutdown.expiry + 1)
    shutdown.bridge._cancel_pending_computer_use(shutdown.session, "bridge_shutdown_interrupted")  # noqa: SLF001
    shutdown.finish()
    assert shutdown.result == {
        "error": "computer_use_cancelled",
        "message": "bridge_shutdown_interrupted",
    }


def test_host_error_accepted_before_expiry_is_kept_for_a_late_waking_caller(
    tmp_path: Any, clocks: _Clocks
) -> None:
    exchange = _Exchange(tmp_path)
    clocks.wall[exchange.worker] = exchange.late_after_done(lambda: exchange.expiry + 1)
    refusal = {"code": "computer_use_invalid_keys_not_dispatched", "message": "Not dispatched."}
    acknowledgement = exchange.respond("host-error", ok=False, result=None, error=refusal)
    exchange.finish()
    assert acknowledgement["ok"] is True
    assert exchange.result == {"error": refusal["code"], "message": refusal["message"]}


def test_binding_mismatch_and_duplicate_precede_expiry_unchanged(
    tmp_path: Any, clocks: _Clocks
) -> None:
    exchange = _Exchange(tmp_path)
    clocks.wall[threading.current_thread()] = lambda: exchange.expiry + 1
    for field, forged in (("response_nonce", "forged"), ("expires_at", "2099-01-01T00:00:00.000Z")):
        mismatch = exchange.respond(f"forged-{field}", **{field: forged})
        assert mismatch["error"]["code"] == "computer_use_binding_mismatch"
        assert exchange.pending.resolved is False and exchange.worker.is_alive()
    assert exchange.respond("genuine-late")["error"]["code"] == "expired_computer_request"
    assert exchange.respond("duplicate-late")["error"]["code"] == "duplicate_response"
    exchange.finish()
    assert exchange.result == ARRIVED_LATE
