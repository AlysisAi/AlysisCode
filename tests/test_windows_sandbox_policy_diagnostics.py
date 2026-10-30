from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

from alysis_code import windows_sandbox as native

SPEC = importlib.util.spec_from_file_location(
    "windows_policy_diagnostics",
    Path(__file__).resolve().parents[1] / "scripts/windows_sandbox_policy_diagnostics.py",
)
assert SPEC is not None and SPEC.loader is not None
diagnostics = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(diagnostics)


@pytest.mark.parametrize("preference", [None, "AllSigned", "Restricted", "Undefined"])
def test_minimal_environment_matches_production_query(tmp_path, monkeypatch, preference):
    shell = tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    if preference is None:
        monkeypatch.delenv("PSExecutionPolicyPreference", raising=False)
    else:
        monkeypatch.setenv("PSExecutionPolicyPreference", preference)
    monkeypatch.setenv("PRIVATE_DIAGNOSTIC_CANARY", "private-value")
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, b"Restricted", b"")

    monkeypatch.setattr(subprocess, "run", run)
    assert native._powershell_execution_policy(shell) == "Restricted"
    environment = diagnostics.minimal_environment(shell, dict(os.environ))
    assert environment == calls[0][1]["env"]
    result = diagnostics.query(shell, environment, "policy")
    assert result["category"] == "success"
    assert calls[1][0] == calls[0][0]
    assert calls[1][1] == {**calls[0][1], "timeout": 30}
    assert "private-value" not in json.dumps(result)


@pytest.mark.parametrize(
    "outcome,category",
    [
        ((0, b"private-output", b""), "unexpected_stdout"),
        ((0, b"private-output-\xff", b""), "invalid_response"),
        ((0, b"RemoteSigned", b"private-stderr"), "stderr_present"),
        ((5, b"private-output", b"private-stderr"), "nonzero_exit"),
        (subprocess.TimeoutExpired("private-command", 30, b"private-output"), "timeout"),
        (OSError(5, "private-error"), "start_failed"),
        (subprocess.SubprocessError("private-error"), "process_failed"),
    ],
)
def test_diagnostic_failure_output_is_sanitized(tmp_path, monkeypatch, outcome, category):
    def run(*_args, **_kwargs):
        if isinstance(outcome, Exception):
            raise outcome
        code, stdout, stderr = outcome
        return subprocess.CompletedProcess([], code, stdout, stderr)

    monkeypatch.setattr(subprocess, "run", run)
    report = diagnostics.query(tmp_path / "powershell.exe", {}, "policy")
    assert report["category"] == category
    assert "private-" not in json.dumps(report)
    assert report["duration_seconds"] >= 0


def test_diagnostics_run_only_six_fixed_bounded_controls(tmp_path, monkeypatch):
    shell = tmp_path / "Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
    inherited = {"PRIVATE_DIAGNOSTIC_CANARY": "private-value"}
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        value = (
            diagnostics.STARTUP_MARKER if argv[-1] == diagnostics.STARTUP_QUERY else "RemoteSigned"
        )
        return subprocess.CompletedProcess(argv, 0, value.encode(), b"")

    monkeypatch.setattr(subprocess, "run", run)
    report = diagnostics.diagnose(shell, inherited)
    assert len(calls) == len(report["cases"]) == 6
    assert [case["environment"] for case in report["cases"]] == [
        "minimal",
        "minimal",
        "minimal",
        "inherited",
        "inherited",
        "inherited",
    ]
    assert [case["query"] for case in report["cases"]] == [
        "startup",
        "policy",
        "unqualified_policy",
        "startup",
        "policy",
        "unqualified_policy",
    ]
    for argv, kwargs in calls:
        assert argv[:5] == [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Command"]
        assert argv[-1] in {
            diagnostics.STARTUP_QUERY,
            native._EXECUTION_POLICY_QUERY,
            diagnostics.UNQUALIFIED_POLICY_QUERY,
        }
        assert kwargs["stdin"] == subprocess.DEVNULL and kwargs["timeout"] == 30
        assert kwargs["creationflags"] == getattr(subprocess, "CREATE_NO_WINDOW", 0)
        assert kwargs["cwd"] == shell.parent
    assert calls[3][1]["env"] == inherited
    assert "PRIVATE_DIAGNOSTIC_CANARY" not in calls[0][1]["env"]
    assert "private-value" not in json.dumps(report)


def test_invalid_process_preference_starts_no_controls(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("must not start"))
    report = diagnostics.diagnose(tmp_path / "a/b/c/d.exe", {"PSExecutionPolicyPreference": "bad"})
    assert report == {"schema": 1, "category": "invalid_host_policy_preference", "cases": []}
