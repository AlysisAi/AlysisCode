"""A shared deadline cannot replace a working service with an unfinished candidate."""

from __future__ import annotations

import json
import time
import urllib.request

import pytest
from test_durable_service_ownership import command, free_port, readiness, server
from test_durable_service_ownership import manager as manager

from alysis_code.execution_deadline import DeadlineExhausted, ExecutionDeadline


def test_stalled_replacement_uses_shared_deadline_and_preserves_old_service(manager):  # noqa: F811
    port = free_port()
    previous = manager.start(cmd=server(port), cwd=manager.root, readiness=readiness(port))
    assert previous.payload["readiness"].get("endpoint_owned") is True, json.dumps(
        previous.payload, indent=2
    )
    deadline = ExecutionDeadline.from_duration(2.0)
    started = time.monotonic()
    result = manager.start(
        cmd=command("import time; time.sleep(30)"),
        cwd=manager.root,
        readiness=readiness(free_port(), timeout=20),
        replace_service_id=previous.service_id,
        execution_deadline=deadline,
    )
    assert time.monotonic() - started < 2.5
    assert result.payload["handoff"]["status"] == "rolled_back"
    assert manager._popens[result.service_id].poll() is not None
    assert manager.status(previous.service_id)["readiness"]["endpoint_owned"]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
        assert response.read() == b"owned-service"


def test_zero_timeout_remains_one_immediate_probe_under_shared_deadline(manager, monkeypatch):  # noqa: F811
    timeouts = []
    check = manager._check_readiness

    def record_timeout(**kwargs):
        timeouts.append(kwargs["timeout_s"])
        return check(**kwargs)

    monkeypatch.setattr(manager, "_check_readiness", record_timeout)
    result = manager.start(
        cmd=command("import time; time.sleep(30)"),
        cwd=manager.root,
        readiness={"type": "process_alive", "timeout_s": 0},
        execution_deadline=ExecutionDeadline.from_duration(20),
    )
    assert timeouts == [0]
    assert result.payload["readiness"]["status"] == "ready"


def test_expired_shared_deadline_prevents_even_immediate_probe_launch(manager):  # noqa: F811
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0,
        deadline_monotonic=20,
        configured_duration_seconds=20,
        clock=lambda: 21,
    )
    with pytest.raises(DeadlineExhausted):
        manager.start(
            cmd=command("import time; time.sleep(30)"),
            cwd=manager.root,
            readiness={"type": "process_alive", "timeout_s": 0},
            execution_deadline=deadline,
        )
    assert not manager._popens


@pytest.mark.parametrize("after_readiness", [97.0, 101.0])
def test_deadline_during_handoff_retires_only_candidate(manager, monkeypatch, after_readiness):  # noqa: F811
    port = free_port()
    previous = manager.start(cmd=server(port), cwd=manager.root, readiness=readiness(port))
    assert previous.payload["readiness"].get("endpoint_owned") is True, json.dumps(
        previous.payload, indent=2
    )
    clock = [0.0]
    deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=0,
        deadline_monotonic=100,
        configured_duration_seconds=100,
        clock=lambda: clock[0],
    )
    check = manager._check_readiness

    def ready_then_budget_closes(**kwargs):
        result = check(**kwargs)
        if clock[0] == 0:
            assert result.get("endpoint_owned") is True, json.dumps(result, indent=2)
            clock[0] = after_readiness
        return result

    monkeypatch.setattr(manager, "_check_readiness", ready_then_budget_closes)
    new_port = free_port()
    result = manager.start(
        cmd=server(new_port),
        cwd=manager.root,
        readiness=readiness(new_port),
        replace_service_id=previous.service_id,
        execution_deadline=deadline,
    )
    assert result.payload["handoff"]["status"] == "rolled_back"
    assert manager._popens[result.service_id].poll() is not None
    assert manager.status(previous.service_id)["readiness"]["endpoint_owned"]
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=1) as response:
        assert response.read() == b"owned-service"
