"""Adversarial acceptance rules for the native TCP host-boundary qualifier."""

import importlib.util
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
