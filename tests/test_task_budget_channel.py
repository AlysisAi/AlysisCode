import copy
import threading
import time

import pytest

from alysis_code.ide.task_budget_channel import TaskBudgetChannel
from alysis_code.task_budget import (
    TaskBudgetAuthority,
    TaskBudgetRefused,
    iso,
    parse_binding,
)


def binding():
    return dict(
        version=1,
        taskId="task",
        envelopeId="envelope",
        authorityGeneration=1,
        deadlineAt=iso(time.time() + 60),
        leaseId="lease",
        leaseExpiresAt=iso(time.time() + 30),
    )


def pending():
    seen, result = [], []
    channel = TaskBudgetChannel(
        binding(), "operation", emit=seen.append, assert_current=lambda: "job"
    )

    def work():
        try:
            result.append(channel.exchange("attempt", {"generated": True}, channel.binding))
        except BaseException as error:
            result.append(error)

    thread = threading.Thread(target=work)
    thread.start()
    until = time.monotonic() + 1
    while not seen and time.monotonic() < until:
        time.sleep(0.005)
    assert seen
    data = seen[0]
    params = dict(
        session_id="session",
        operation_id="operation",
        job_id="job",
        request_id=data["requestId"],
        response_nonce=data["responseNonce"],
        lease_id="lease",
        ok=True,
    )
    return channel, params, thread, result


def test_exact_response_wakes_only_after_actual_stdio_flush_and_cannot_replay():
    channel, params, thread, result = pending()
    response, flushed = channel.respond(params, "session")
    assert response["status"] == "resolved" and result == []
    with pytest.raises(TaskBudgetRefused):
        channel.respond(params, "session")
    flushed()
    thread.join(1)
    assert result == [{"ok": True}]
    with pytest.raises(TaskBudgetRefused):
        channel.respond(params, "session")


@pytest.mark.parametrize(
    "field", ["session_id", "operation_id", "job_id", "request_id", "response_nonce", "lease_id"]
)
def test_wrong_identity_never_unblocks_actual_attempt(field):
    channel, params, thread, result = pending()
    try:
        with pytest.raises(TaskBudgetRefused):
            channel.respond({**params, field: "substituted"}, "session")
        assert result == []
    finally:
        channel.close()
        thread.join(1)
    assert len(result) == 1 and isinstance(result[0], TaskBudgetRefused)


def test_renewal_rotates_only_lease_preserving_original_deadline_and_sequence():
    clock = [time.time()]
    original = binding()
    original["leaseExpiresAt"] = iso(clock[0] + 0.1)
    records = []

    def exchange(kind, payload, current):
        records.append((kind, copy.deepcopy(payload)))
        if kind == "renew":
            return {
                "ok": True,
                "task_budget": {
                    **current,
                    "leaseId": "new-lease",
                    "leaseExpiresAt": iso(clock[0] + 5),
                },
            }
        return {"ok": True}

    authority = TaskBudgetAuthority(
        original,
        exchange=exchange,
        assert_current=lambda: None,
        cancelled=lambda: False,
        now=lambda: clock[0],
    )
    with authority.activate():
        first = authority.begin(
            method="POST", url="https://fixture.invalid", headers=[], body=b"generated"
        )
        authority.settled(first)
        clock[0] += 1
        second = authority.begin(
            method="POST", url="https://fixture.invalid", headers=[], body=b"generated"
        )
        authority.settled(second)
        assert authority.binding["deadlineAt"] == original["deadlineAt"]
    assert [kind for kind, _ in records] == ["attempt", "settled", "renew", "attempt", "settled"]
    assert records[3][1]["sequence"] == 2 and records[3][1]["leaseId"] == "new-lease"


@pytest.mark.parametrize(
    "field,value",
    [("deadlineAt", "2099-01-01T00:00:00.000Z"), ("taskId", "foreign"), ("authorityGeneration", 2)],
)
def test_renewal_cannot_extend_or_rebind_original_authority(field, value):
    clock = [time.time()]
    original = binding()
    original["leaseExpiresAt"] = iso(clock[0] + 0.1)

    def exchange(kind, payload, current):
        return {
            "ok": True,
            "task_budget": {
                **current,
                "leaseId": "new",
                "leaseExpiresAt": iso(clock[0] + 2),
                field: value,
            },
        }

    authority = TaskBudgetAuthority(
        original,
        exchange=exchange,
        assert_current=lambda: None,
        cancelled=lambda: False,
        now=lambda: clock[0],
    )
    with authority.activate():
        clock[0] += 1
        with pytest.raises(TaskBudgetRefused):
            authority.begin(
                method="POST", url="https://fixture.invalid", headers=[], body=b"generated"
            )


def test_binding_parser_refuses_unbounded_extra_and_noncanonical_fields():
    for value in [
        {**binding(), "approval": True},
        {**binding(), "authorityGeneration": True},
        {**binding(), "leaseId": "../path"},
    ]:
        with pytest.raises(TaskBudgetRefused):
            parse_binding(value)


def test_host_mutation_refusal_requires_actual_one_use_private_renewal_ack():
    original = binding()
    calls = []

    def exchange(kind, payload, current):
        calls.append((kind, payload))
        return {"ok": True, "task_budget": {**current, "leaseId": "renewed"}}

    authority = TaskBudgetAuthority(
        original, exchange=exchange, assert_current=lambda: None, cancelled=lambda: False
    )
    # The old lease is still within its time allowance; only a genuine host
    # mutation-count refusal requests the extra bounded verification exchange.
    with authority.activate():
        assert not authority.consume_host_renewal({"error": "computer_use_lease_renewal_required"})
        receipt = authority.renew_after_host_refusal()
        assert calls == [("renew", {})]
        assert authority.binding["leaseId"] == "renewed"
        assert authority.binding["deadlineAt"] == original["deadlineAt"]
        assert not authority.consume_host_renewal({**receipt, "extra": True})
        assert authority.consume_host_renewal(receipt)
        assert not authority.consume_host_renewal(receipt)
    assert not authority.consume_host_renewal(receipt)


@pytest.mark.parametrize("mode", ["lost-ack", "cancel", "rebound"])
def test_refused_cancelled_or_rebound_host_renewal_never_arms_recovery(mode):
    cancelled = [False]

    def exchange(kind, payload, current):
        assert kind == "renew"
        if mode == "lost-ack":
            raise TaskBudgetRefused()
        if mode == "cancel":
            cancelled[0] = True
        changed = {**current, "leaseId": "renewed"}
        if mode == "rebound":
            changed["authorityGeneration"] += 1
        return {"ok": True, "task_budget": changed}

    authority = TaskBudgetAuthority(
        binding(), exchange=exchange, assert_current=lambda: None, cancelled=lambda: cancelled[0]
    )
    with authority.activate():
        with pytest.raises(TaskBudgetRefused):
            authority.renew_after_host_refusal()
        assert authority.is_cancelled
        assert not authority.consume_host_renewal({"error": "computer_use_lease_renewal_required"})


def test_newer_lease_or_cancellation_invalidates_unconsumed_host_renewal():
    for mode in ("newer", "cancel"):
        cancelled = [False]
        count = [0]

        def exchange(kind, payload, current, count=count):
            count[0] += 1
            return {"ok": True, "task_budget": {**current, "leaseId": f"renewed-{count[0]}"}}

        authority = TaskBudgetAuthority(
            binding(),
            exchange=exchange,
            assert_current=lambda: None,
            cancelled=lambda cancelled=cancelled: cancelled[0],
        )
        with authority.activate():
            receipt = authority.renew_after_host_refusal()
            if mode == "newer":
                authority.ensure_lease(force=True)
            else:
                cancelled[0] = True
            with pytest.raises(TaskBudgetRefused):
                authority.consume_host_renewal(receipt)
            assert not authority.consume_host_renewal(receipt)
