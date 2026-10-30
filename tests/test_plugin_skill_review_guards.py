"""P2 review guard pins at the real ingress, worker, and projection boundaries."""

import io
import json
import threading
import time
from dataclasses import replace

import pytest

from alysis_code.ide import plugin_skill_projection as projection
from alysis_code.ide.stdio_bridge import StdioBridge
from tests.test_custom_plugins import root as root
from tests.test_ide_host_actions import _request, _response_for
from tests.test_plugin_skill_admission import create, send
from tests.test_plugin_skill_admission import setup as setup
from tests.test_plugin_skill_cancel_readiness import hold_ack
from tests.test_plugin_skill_projection import error_reason, params, rpc, selection
from tests.test_plugin_skill_projection import installed as installed


def cleanup_wait(bridge, job):
    job.cancellation_event.set()
    for ready in list(bridge._p2_readiness.values()):
        ready.failed = True
        ready.event.set()
    job.thread.join(2)


def test_failed_ack_stops_already_drain_started_worker(setup, monkeypatch):
    bridge, _, _, _, calls = setup
    bridge._host_action_timeout_seconds = 30
    sid = create(setup)
    waiting = threading.Event()
    enqueue = bridge._p2_enqueue
    captured = {}

    def early_drain(*args):
        queued = enqueue(*args)
        ready = bridge._p2_readiness[queued.item.prompt_id]
        wait = ready.event.wait

        def observed_wait(timeout):
            waiting.set()
            return wait(timeout)

        ready.event.wait = observed_wait
        bridge._drain_prompt_queue(bridge._sessions[sid])
        captured["job"] = bridge._sessions[sid].active_job
        return queued

    write = bridge._write

    def failed(payload):
        if payload.get("id") == "send" and payload.get("ok"):
            assert waiting.wait(2)
            raise OSError("Inert failed ACK")
        write(payload)

    monkeypatch.setattr(bridge, "_p2_enqueue", early_drain)
    monkeypatch.setattr(bridge, "_write", failed)
    ack = send(setup, sid)
    assert not ack["ok"]
    job = captured["job"]
    try:
        job.thread.join(2)
        assert not job.thread.is_alive()
        assert job.status == "failed"
        assert job.result["error_details"] == {"reason": "unavailable"}
        assert calls == []
        assert not any(
            json.loads(s).get("type") == "host_action_requested"
            for s in bridge.stdout.getvalue().splitlines()
        )
    finally:
        cleanup_wait(bridge, job)


def test_deadline_interrupts_held_ack_before_long_host_timeout(setup, monkeypatch):
    from alysis_code.execution_deadline import ExecutionDeadline

    bridge, _, _, _, calls = setup
    bridge._host_action_timeout_seconds = 30
    sid = create(setup)
    waiting = hold_ack(bridge, monkeypatch)
    ack = send(setup, sid)
    assert waiting.wait(2)
    job = bridge._jobs[ack["result"]["job_id"]]
    bridge._sessions[sid].agent_session.execution_deadline = ExecutionDeadline.from_absolute(
        started_at_monotonic=time.monotonic() - 2, deadline_monotonic=time.monotonic() - 1
    )
    try:
        job.thread.join(2)
        assert not job.thread.is_alive(), "Deadline must interrupt the 30-second ACK wait"
        assert job.result["task_outcome"]["reason"] == "deadline_exhausted"
        assert "error_code" not in job.result and calls == []
    finally:
        cleanup_wait(bridge, job)


def test_prefix_skill_name_is_not_exact_selection(setup):
    bridge, _, _, selected, calls = setup
    sid = create(setup)
    selected["skill_name"] = "note"  # A prefix of the real "notes" skill.
    ack = send(setup, sid)
    try:
        assert ack["error"]["details"] == {"reason": "unknown_skill"}
        assert calls == [] and not bridge._jobs
    finally:
        bridge.process_line(_request("session.cancel", dict(session_id=sid), "cleanup"))


def test_late_skill_error_cannot_overwrite_accepted_stop(setup, monkeypatch):
    bridge, _, _, _, calls = setup
    sid = create(setup)
    entered, release = threading.Event(), threading.Event()

    def late_error(*args):
        entered.set()
        assert release.wait(3)
        projection.fail("stale")

    monkeypatch.setattr(bridge, "_p2_admit", late_error)
    ack = send(setup, sid)
    assert entered.wait(2)
    job = bridge._jobs[ack["result"]["job_id"]]
    try:
        bridge.process_line(_request("session.cancel", dict(session_id=sid), "stop"))
        assert _response_for(bridge.stdout, "stop")["result"]["status"] == "cancellation_requested"
        release.set()
        job.thread.join(2)
        assert job.status == "cancelled" and job.exit_code == 130
        assert "error_code" not in job.result and "error_details" not in job.result
        assert calls == []
    finally:
        release.set()
        cleanup_wait(bridge, job)


@pytest.mark.parametrize("identifier", ["D" * 32, "d" * 31, "d" * 33, "g" * 32])
def test_admission_id_is_exact_lowercase_hex32(setup, identifier):
    bridge, _, _, selected, calls = setup
    sid = create(setup)
    selected["admission_id"] = identifier
    ack = send(setup, sid)
    try:
        assert ack["error"]["details"] == {"reason": "invalid_request"}
        assert not bridge._jobs and calls == []
    finally:
        bridge.process_line(_request("session.cancel", dict(session_id=sid), "cleanup"))


def test_projection_drops_overlong_version(installed, monkeypatch):
    skills_of = projection.skills._skills_of
    monkeypatch.setattr(
        projection.skills,
        "_skills_of",
        lambda *args: tuple(replace(s, version="v" * 65) for s in skills_of(*args)),
    )
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        reply = rpc(bridge, "plugin.skills.list", params(installed))["result"]
        assert reply["skills"] == []
        assert reply["dropped"] == [{"plugin_id": "custom.tools", "reason": "unavailable"}]
    finally:
        bridge.close()


@pytest.mark.parametrize("where", ["global", "plugin"])
def test_boolean_revision_is_invalid_without_comparison_mask(installed, where):
    value = params(installed)
    if where == "global":
        value["lifecycle_revision"] = True
        value["bound_plugins"][0]["lifecycle_revision"] = 0
    else:
        value["bound_plugins"][0]["lifecycle_revision"] = True
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        assert error_reason(rpc(bridge, "plugin.skills.list", value)) == "invalid_request"
    finally:
        bridge.close()


def test_forged_selection_id_is_invalid_not_stale(installed):
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        value = params(installed)
        picked = selection(rpc(bridge, "plugin.skills.list", value)["result"])
        picked["selection_id"] = "0" * 32
        assert (
            error_reason(rpc(bridge, "plugin.skills.read", {**value, "selection": picked}))
            == "invalid_request"
        )
    finally:
        bridge.close()


def test_row_count_bound_before_list_byte_bound(installed, monkeypatch):
    from alysis_code.ide.protocol import dumps_message, response_message

    original = projection.skills._skills_of
    count = 256

    def many(*args):
        [skill] = original(*args)
        return tuple(
            replace(skill, name=f"custom_tools:n{i}", text="Read.", title="N", description="")
            for i in range(count)
        )

    monkeypatch.setattr(projection.skills, "_skills_of", many)
    bridge = StdioBridge(stdout=io.StringIO(), role="plugin_projection")
    try:
        value = params(installed)
        result = rpc(bridge, "plugin.skills.list", value)["result"]
        assert len(result["skills"]) == 256
        # A 257th ordinary row still fits: this isolates the count guard.
        result["skills"].append(result["skills"][-1])
        assert len(dumps_message(response_message("p2", result)).encode()) < 256 * 1024
        count = 257
        assert error_reason(rpc(bridge, "plugin.skills.list", value)) == "too_large"
    finally:
        bridge.close()
