from __future__ import annotations

import json
import time as real_time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_ide_computer_use_host import _bridge_session

from alysis_code.ide import stdio_bridge as module

BASE = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
TOKEN = "a" * 64
ARGS = {"action": "press", "element_id": "save", "snapshot_id": "original"}


def iso(value: datetime) -> str:
    return value.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class Clock:
    wall = BASE
    monotonic = 1000.0

    def advance(self, seconds: float, *, wall: bool = True, monotonic: bool = True) -> None:
        if wall:
            self.wall += timedelta(seconds=seconds)
        if monotonic:
            self.monotonic += seconds


def fixture(
    tmp_path: Path,
    monkeypatch: Any,
    *,
    transport: float = 0,
    expiry: Any = None,
    required: bool = True,
):
    bridge, _out, session, job = _bridge_session(tmp_path)
    session.surface.with_job(job.job_id)
    session.surface.emit_tool_call_started("expiry-fixture-call", "computer_action", "[redacted]")
    clock = Clock()

    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock.wall if tz else clock.wall.replace(tzinfo=None)

    class LocalTime:
        def monotonic(self):
            return clock.monotonic

        def __getattr__(self, name):
            return getattr(real_time, name)

    monkeypatch.setattr(module, "datetime", FrozenDateTime)
    monkeypatch.setattr(module, "time", LocalTime())
    records, waits, events, calls = [], [], [], []
    Record = module.PendingApprovalRecord

    class ClockEvent:
        flag = False

        def set(self):
            self.flag = True

        def wait(self, timeout=None):
            if self.flag:
                return True
            waits.append(timeout)
            clock.advance(timeout)
            return False

    def record(*args, **kwargs):
        value = Record(*args, **kwargs)
        value.done = ClockEvent()
        records.append(value)
        return value

    monkeypatch.setattr(module, "PendingApprovalRecord", record)
    host_expiry = iso(BASE + timedelta(seconds=30)) if expiry is None else expiry

    def exchange(_session, tool, args):
        calls.append(dict(args))
        if args["approval_phase"] == "prepare":
            clock.advance(transport)
            prepared = {
                "approval_preflight": {
                    "version": 1,
                    "required": required,
                    "token": TOKEN,
                    "scope_digest": "sha256:" + "b" * 64,
                    "expires_at": host_expiry,
                    "preview": "Save in fixture",
                    "reason": "External side effect",
                }
            }
            if expiry == "missing":
                prepared["approval_preflight"].pop("expires_at")
            return prepared
        return {"state": {"snapshot_id": "after"}}

    monkeypatch.setattr(bridge, "_exchange_computer_use", exchange)
    handler = [lambda _event: None]

    def emit(event):
        events.append(event)
        handler[0](event)

    monkeypatch.setattr(bridge, "_write_transient_computer_use_request", emit)

    def allow(*, for_turn=False):
        pending = records[-1]
        bindings = {**module._approval_binding_payload(pending), "expires_at": pending.expires_at}
        return bridge._resolve_approval(
            session=session,
            approval_id=pending.approval_id,
            allow=True,
            allow_for_session=for_turn,
            response_bindings=bindings,
            request_id="fixture-decision",
        )

    def run():
        return bridge._request_computer_use(session, "computer_action", dict(ARGS))

    return bridge, session, job, clock, records, waits, events, calls, handler, allow, run


def test_transport_and_review_time_are_deducted_from_the_same_original_deadline(
    tmp_path, monkeypatch
):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch, transport=11
    )

    def review(event):
        if event["type"] == "approval.required":
            assert event["payload"]["expires_at"] == iso(BASE + timedelta(seconds=30))
            assert records[-1].deadline == 1030
            clock.advance(5)  # First app review already consumed part of the remainder.

    handler[0] = review
    assert run()["error"] == "computer_use_approval_expired"
    assert waits == [14]
    assert len(calls) == 1
    assert not session.pending_approvals
    assert events[-1]["payload"]["status"] == "expired"
    assert TOKEN not in json.dumps(events)


def test_shorter_configured_cap_is_preserved_without_extending_host_expiry(tmp_path, monkeypatch):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch, transport=11
    )
    bridge._approval_timeout_seconds = 3
    result = run()
    assert result["error"] == "computer_use_approval_expired"
    assert events[0]["payload"]["expires_at"] == iso(BASE + timedelta(seconds=14))
    assert records[0].deadline == 1014
    assert waits == [3]
    assert len(calls) == 1


@pytest.mark.parametrize(
    "review_seconds,expected", [(18, "applied"), (19, "expired"), (20, "expired")]
)
def test_real_bound_allow_respects_host_expiry_including_exact_boundary(
    tmp_path, monkeypatch, review_seconds, expected
):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch, transport=11
    )

    def review(event):
        if event["type"] == "approval.required":
            clock.advance(review_seconds)
            assert allow(for_turn=True)["status"] == expected

    handler[0] = review
    result = run()
    assert len(calls) == (2 if expected == "applied" else 1)
    if expected == "applied":
        assert result == {"state": {"snapshot_id": "after"}}
        assert calls[-1] == {**ARGS, "approval_phase": "execute", "approval_token": TOKEN}
    else:
        assert result["error"] == "computer_use_approval_expired"
        assert not session.approved_approval_scopes


@pytest.mark.parametrize("jump", ["wall-forward", "monotonic-only"])
def test_wall_and_monotonic_boundaries_independently_prevent_late_allow(
    tmp_path, monkeypatch, jump
):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch
    )

    def review(event):
        if event["type"] == "approval.required":
            clock.advance(31, wall=jump == "wall-forward", monotonic=jump == "monotonic-only")
            assert allow()["status"] == "expired"

    handler[0] = review
    assert "error" in run()
    assert len(calls) == 1
    assert events[-1]["payload"]["status"] == "expired"


@pytest.mark.parametrize(
    "elapsed,expected", [(60, "applied"), (119.999, "applied"), (120, "expired")]
)
def test_two_minute_control_challenge_accepts_review_before_its_exact_boundary(
    tmp_path, monkeypatch, elapsed, expected
):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch, expiry=iso(BASE + timedelta(seconds=120))
    )

    def review(event):
        if event["type"] == "approval.required":
            assert event["payload"]["expires_at"] == iso(BASE + timedelta(seconds=120))
            assert records[-1].deadline == 1120
            clock.advance(elapsed)
            assert allow()["status"] == expected

    handler[0] = review
    result = run()
    assert [call["approval_phase"] for call in calls] == (
        ["prepare", "execute"] if expected == "applied" else ["prepare"]
    )
    assert not session.pending_approvals
    if expected == "applied":
        assert result == {"state": {"snapshot_id": "after"}}
        assert calls[-1] == {**ARGS, "approval_phase": "execute", "approval_token": TOKEN}
    else:
        assert result["error"] == "computer_use_approval_expired"


def test_two_minute_host_window_deducts_transport_and_initial_review_time(tmp_path, monkeypatch):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch, transport=11, expiry=iso(BASE + timedelta(seconds=120))
    )

    def review(event):
        if event["type"] == "approval.required":
            assert records[-1].deadline == 1120
            clock.advance(60)

    handler[0] = review
    assert run()["error"] == "computer_use_approval_expired"
    assert waits == [49]
    assert len(calls) == 1


def test_stop_after_one_minute_of_control_review_cannot_dispatch(tmp_path, monkeypatch):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch, expiry=iso(BASE + timedelta(seconds=120))
    )

    def cancel(event):
        if event["type"] == "approval.required":
            clock.advance(60)
            job.cancellation_event.set()
            bridge._cancel_pending_approvals(session, "cancelled_by_user")

    handler[0] = cancel
    with pytest.raises(module.BridgeCancellationError):
        run()
    assert len(calls) == 1 and not session.pending_approvals
    assert events[-1]["payload"]["status"] == "cancelled:cancelled_by_user"


@pytest.mark.parametrize(
    "variant",
    ["after-valid-allow", "cancel-after-valid-allow", "cached-turn", "cached-turn-monotonic"],
)
def test_final_dispatch_rechecks_expiry_and_cancellation_even_with_an_existing_decision(
    tmp_path, monkeypatch, variant
):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch
    )

    def review(event):
        if event["type"] == "approval.required":
            assert allow(for_turn=True)["status"] == "applied"
        if not variant.startswith("cached-turn") and event["type"] == "prompt_for_input":
            clock.advance(31)
            if variant == "cancel-after-valid-allow":
                job.cancellation_event.set()

    handler[0] = review
    if variant.startswith("cached-turn"):
        assert run() == {"state": {"snapshot_id": "after"}}
        assert session.approved_approval_scopes

        def expire_cached(event):
            # There is no second approval prompt. Expire after cache admission but
            # before _request_computer_use attempts the execute exchange.
            if "approval_auto_allowed" in json.dumps(event):
                clock.advance(31, wall=variant != "cached-turn-monotonic")

        handler[0] = expire_cached
        assert run()["error"] == "computer_use_approval_expired"
        assert [call["approval_phase"] for call in calls] == ["prepare", "execute", "prepare"]
    elif variant == "cancel-after-valid-allow":
        with pytest.raises(module.BridgeCancellationError):
            run()
        assert len(calls) == 1
    else:
        assert run()["error"] == "computer_use_approval_expired"
        assert len(calls) == 1


@pytest.mark.parametrize(
    "value",
    [
        "missing",
        "",
        False,
        30,
        "2026-09-10T12:00:30Z",
        "2026-09-10T12:00:30.000+00:00",
        "2026-02-30T12:00:30.000Z",
        "2026-09-10T12:02:00.001Z",
        "9999-01-01T00:00:00.000Z",
        "2026-09-10T12:00:00.000Z",
    ],
)
@pytest.mark.parametrize("required", [False, True])
def test_missing_malformed_unbounded_and_expired_host_challenges_never_dispatch(
    tmp_path, monkeypatch, value, required
):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch, expiry=value, required=required
    )
    assert "error" in run()
    assert len(calls) == 1
    assert records == [] and events == []


def test_expiry_during_binding_construction_does_not_publish_an_actionable_prompt(
    tmp_path, monkeypatch
):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch, transport=28
    )
    build = module.build_approval_binding

    def delayed_binding(*args, **kwargs):
        clock.advance(3)
        return build(*args, **kwargs)

    monkeypatch.setattr(module, "build_approval_binding", delayed_binding)
    assert run()["error"] == "computer_use_approval_expired"
    assert len(calls) == 1
    assert not events and not waits and not session.pending_approvals


def test_explicit_pending_cancel_wins_when_expiry_is_also_reached(tmp_path, monkeypatch):
    bridge, session, job, clock, records, waits, events, calls, handler, allow, run = fixture(
        tmp_path, monkeypatch
    )

    def cancel(event):
        if event["type"] == "approval.required":
            clock.advance(31)
            job.cancellation_event.set()
            bridge._cancel_pending_approvals(session, "cancelled_by_user")

    handler[0] = cancel
    with pytest.raises(module.BridgeCancellationError):
        run()
    assert events[-1]["payload"]["status"] == "cancelled:cancelled_by_user"
    assert len(calls) == 1 and not session.pending_approvals
