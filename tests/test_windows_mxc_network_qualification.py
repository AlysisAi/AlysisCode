"""Adversarial acceptance rules for the native TCP host-boundary qualifier."""

import importlib.util
import os
import socket
import struct
import threading
from pathlib import Path

import pytest

_source = Path(__file__).resolve().parents[1] / "scripts/windows_mxc_network.py"
_spec = importlib.util.spec_from_file_location("mxc_network_qualification", _source)
assert _spec and _spec.loader
network = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(network)


def event(code=2, output="TIMEOUT", **changes):
    return {
        "exit_code": code,
        "stdout": output,
        "host_identical_control_before": True,
        "host_identical_control_after": True,
        "host_accepted_connection": False,
        **changes,
    }


@pytest.mark.parametrize("code,output", [(0, "EACCES"), (0, "EPERM")])
def test_explicit_denial_also_requires_receiver_evidence(code, output):
    assert network.tcp_boundary_outcome(event(code, output)) == "explicit-denial"


@pytest.mark.parametrize("code,output", [(2, "TIMEOUT"), (3, "ETIMEDOUT")])
def test_timeout_is_classified_separately_after_live_controls(code, output):
    assert network.tcp_boundary_outcome(event(code, output)) == "timeout-with-no-host-delivery"


@pytest.mark.parametrize(
    "missing",
    ["host_identical_control_before", "host_identical_control_after", "host_accepted_connection"],
)
@pytest.mark.parametrize("code,output", [(0, "EACCES"), (2, "TIMEOUT")])
def test_missing_control_or_observation_cannot_pass(missing, code, output):
    incomplete = event(code, output)
    del incomplete[missing]
    with pytest.raises(AssertionError):
        network.tcp_boundary_outcome(incomplete)


@pytest.mark.parametrize("code,output", [(0, "EACCES"), (2, "TIMEOUT")])
def test_actual_delivery_overrides_claimed_error_or_timeout(code, output):
    with pytest.raises(AssertionError):
        network.tcp_boundary_outcome(event(code, output, host_accepted_connection=True))


@pytest.mark.parametrize(
    "code,output",
    [
        (1, "CONNECTED"),
        (3, "ECONNREFUSED"),
        (3, "ENETUNREACH"),
        (3, "ENOENT"),
        (0, ""),
        (0, "TIMEOUT"),
        (125, "EACCES"),
        (2, "EACCES"),
    ],
)
def test_failed_launch_dead_listener_wrong_exit_or_connection_are_not_denials(code, output):
    with pytest.raises(AssertionError):
        network.tcp_boundary_outcome(event(code, output))


# The whole check against a real loopback listener: the uncontained control and the contained
# attempt are stand-ins with the same contract (the control's payload must reach the listener).
@pytest.fixture
def listener():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(4)
        yield server


def control_for(server, payload=b"identical-sender"):
    def control():
        with socket.create_connection(server.getsockname(), timeout=3) as client:
            client.sendall(payload)
        server.settimeout(3)
        accepted, _ = server.accept()
        with accepted:
            accepted.settimeout(3)
            assert accepted.recv(64) == payload

    return control


def reaches(server, reset=False):
    """A contained attempt that did reach the host listener, then dropped the connection."""
    with socket.create_connection(server.getsockname(), timeout=3) as client:
        if reset:
            # Linger on, zero seconds: close resets the connection instead of ending it.
            linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)
            client.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)


def attempt(code, output, effect=None):
    def run():
        if effect:
            effect()
        return {"name": "online_host_loopback_denied", "exit_code": code, "stdout": output + "\n"}

    return run


@pytest.mark.parametrize("code,output", [(0, "EACCES"), (0, "EPERM")])
def test_refused_connection_with_silent_listener_is_denied(listener, code, output):
    event = network.tcp_denial_check(listener, control_for(listener), attempt(code, output))
    assert event["boundary_outcome"] == "explicit-denial"
    assert event["host_accepted_connection"] is False
    assert event["host_identical_control_before"] is event["host_identical_control_after"] is True


@pytest.mark.parametrize("code,output", [(2, "TIMEOUT"), (3, "ETIMEDOUT")])
def test_timeout_that_never_reached_the_listener_is_denied(listener, code, output):
    event = network.tcp_denial_check(listener, control_for(listener), attempt(code, output))
    assert event["boundary_outcome"] == "timeout-with-no-host-delivery"
    assert event["host_accepted_connection"] is False


@pytest.mark.parametrize("reset", [False, True], ids=["closed", "reset"])
@pytest.mark.parametrize("code,output", [(2, "TIMEOUT"), (3, "ETIMEDOUT"), (0, "EACCES")])
def test_timeout_or_refusal_after_the_listener_saw_the_connection_is_not_denied(
    listener, code, output, reset
):
    run = attempt(code, output, effect=lambda: reaches(listener, reset=reset))
    with pytest.raises(AssertionError):
        network.tcp_denial_check(listener, control_for(listener), run)


def test_connection_that_arrives_just_after_the_attempt_still_counts(listener):
    late = threading.Timer(0.2, lambda: reaches(listener))
    run = attempt(2, "TIMEOUT", effect=late.start)
    with pytest.raises(AssertionError):
        network.tcp_denial_check(listener, control_for(listener), run)
    late.join()


def test_connected_attempt_is_not_denied(listener):
    run = attempt(1, "CONNECTED", effect=lambda: reaches(listener))
    with pytest.raises(AssertionError):
        network.tcp_denial_check(listener, control_for(listener), run)


def test_bare_timeout_without_a_live_listener_is_not_denied(listener):
    def dead_control():
        raise AssertionError("the uncontained sender did not reach the listener")

    with pytest.raises(AssertionError):
        network.tcp_denial_check(listener, dead_control, attempt(2, "TIMEOUT"))


def test_listener_that_dies_during_the_attempt_is_not_denied(listener):
    calls = []

    def control():
        calls.append(len(calls))
        if len(calls) > 1:
            raise AssertionError("the listener stopped receiving after the attempt")
        control_for(listener)()

    event = {"name": "online_host_loopback_denied", "exit_code": 2, "stdout": "TIMEOUT"}
    with pytest.raises(AssertionError):
        network.tcp_denial_check(listener, control, lambda: event)
    assert event["host_accepted_connection"] is False
    assert "host_identical_control_after" not in event
    assert "boundary_outcome" not in event
