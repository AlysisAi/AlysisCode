"""Failure fixtures for the disposable Linux study, not takeover qualification."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def study():
    path = Path(__file__).resolve().parents[1] / "scripts/cloud-dev/viewer_control_study.py"
    spec = importlib.util.spec_from_file_location("viewer_cleanup_study", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Resource:
    def __init__(self, name, calls, *, fails=False):
        self.name, self.calls, self.fails = name, calls, fails

    def close(self):
        self.calls.append(self.name)
        if self.fails:
            raise RuntimeError("synthetic private error must not enter the report")

    close_all = close


class Child:
    def __init__(self, name, calls, *, ignores_term=False, unkillable=False):
        self.name, self.calls = name, calls
        self.ignores_term, self.unkillable = ignores_term, unkillable
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.calls.append(self.name + ":terminate")
        if not self.ignores_term:
            self.returncode = -15

    def kill(self):
        self.calls.append(self.name + ":kill")
        if not self.unkillable:
            self.returncode = -9

    def wait(self, timeout):
        assert timeout == 5
        self.calls.append(self.name + ":wait")
        if self.returncode is None:
            raise subprocess.TimeoutExpired("synthetic child", timeout)
        return self.returncode


def test_each_owned_stage_runs_after_prior_cleanup_failure(study):
    calls = []
    failures = study.cleanup_owned_viewer_resources(
        [Resource("socket", calls, fails=True)],
        [Resource("other handoff", calls), Resource("failed handoff", calls, fails=True)],
        Resource("X connection", calls, fails=True),
        Resource("browser", calls, fails=True),
        [
            Child("X server", calls),
            Child("window manager", calls, ignores_term=True, unkillable=True),
        ],
    )
    assert calls == [
        "socket",
        "failed handoff",
        "other handoff",
        "X connection",
        "browser",
        "window manager:terminate",
        "window manager:wait",
        "window manager:kill",
        "window manager:wait",
        "X server:terminate",
        "X server:wait",
    ]
    assert failures == [
        {"stage": stage, "error_type": kind}
        for stage, kind in [
            ("viewer_close", "RuntimeError"),
            ("handoff_close", "RuntimeError"),
            ("x11_connection_close", "RuntimeError"),
            ("browser_close", "RuntimeError"),
            ("child_stop", "TimeoutExpired"),
        ]
    ]
    assert "private" not in str(failures)


def test_owned_child_ignoring_termination_is_killed_and_reaped(study):
    calls = []
    child = Child("X server", calls, ignores_term=True)
    assert study.cleanup_owned_viewer_resources([], [], None, None, [child]) == []
    assert child.returncode == -9
    assert calls == ["X server:terminate", "X server:wait", "X server:kill", "X server:wait"]


def test_handoff_failure_does_not_leave_a_real_owned_child_running(study):
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        calls = []
        failures = study.cleanup_owned_viewer_resources(
            [], [Resource("handoff", calls, fails=True)], None, Resource("browser", calls), [child]
        )
        assert failures == [{"stage": "handoff_close", "error_type": "RuntimeError"}]
        assert calls == ["handoff", "browser"]
        assert child.poll() is not None
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
