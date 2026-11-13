"""The live harness must retain allowance when owned shutdown is uncertain."""

import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def study(tmp_path, monkeypatch):
    path = Path(__file__).resolve().parents[1] / "scripts/qa/bots_peer_cooperation.py"
    spec = importlib.util.spec_from_file_location("peer_cleanup_study", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "REPORTS", tmp_path / "reports")
    return module


def test_unsettled_or_legacy_usage_cannot_release_reserved_allowance(study):
    study.ledger("prior", reserve=True, cap=40)
    study.ledger("prior", {"calls": 1})
    with pytest.raises(RuntimeError, match="allowance exhausted"):
        study.ledger("new", reserve=True, cap=8)
    study.write_json(
        study.REPORTS / "ledger.json", [{"project": "prior", "cap": 40, "usage": {"calls": 1}}]
    )
    with pytest.raises(RuntimeError, match="allowance exhausted"):
        study.ledger("new", reserve=True, cap=8)
    study.ledger("prior", {"calls": 1}, settled=True)
    study.ledger("new", reserve=True, cap=8)


@pytest.mark.parametrize("failure", ["command", "still_running", "timeout"])
def test_runtime_cleanup_uncertainty_is_not_success(study, monkeypatch, failure):
    def run(argv, **kwargs):
        if failure == "command":
            raise subprocess.CalledProcessError(1, argv, stderr="synthetic private diagnostic")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 30)
        return SimpleNamespace(stdout="true\n")

    monkeypatch.setattr(study.subprocess, "run", run)
    failures = study.cleanup_owned_study(None, ["owned"])
    assert len(failures) == 1 and failures[0]["stage"] == "runtime_stop"
    assert "private" not in str(failures)


def test_desktop_failure_does_not_skip_container_shutdown(study, monkeypatch):
    class Child:
        def poll(self):
            return None

        def terminate(self):
            raise OSError("synthetic private error")

    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout="false\nfalse\n")

    monkeypatch.setattr(study.subprocess, "run", run)
    assert study.cleanup_owned_study(Child(), ["first", "restarted"]) == [
        {"stage": "desktop_stop", "error_type": "OSError"}
    ]
    assert [c[1] for c in calls] == ["stop", "inspect"]
    assert calls[-1][-2:] == ["first", "restarted"]
