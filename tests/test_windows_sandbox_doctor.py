"""Recovery-sensitive native diagnostics; no installed helper is invoked."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from alysis_code import sandbox_doctor as doctor
from alysis_code import windows_sandbox as native
from alysis_code.config import AppConfig


@pytest.fixture
def diagnostic(monkeypatch, tmp_path):
    cfg = AppConfig(model="test")
    cfg.extra_fields = {"shell_sandbox": {"backend": "windows-native"}}
    local = tmp_path / "local"
    local.mkdir()
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setattr(
        native,
        "helper_status",
        lambda: {
            "user": {
                "user": {"exists": True},
                "cred_present": True,
                "marker_version": 3,
                "real_user_sid": "owner",
                "owner_user_sid": "owner",
            },
            "ambient": {"paths": [{"present": True}]},
        },
    )
    mkdtemp = doctor.tempfile.mkdtemp
    monkeypatch.setattr(doctor.tempfile, "mkdtemp", lambda **kw: mkdtemp(dir=tmp_path, **kw))
    return cfg, local / "alysis-sandbox" / "execution-pending.json"


def _success():
    return subprocess.CompletedProcess([], 0, "alysis-sandbox-ready\n", "")


def _retain_assertion(result, workspace):
    assert not result.ready
    assert workspace.is_dir()
    retained = next(c for c in result.checks if c.name == "retained diagnostic workspace")
    assert str(workspace) in retained.detail
    assert "recover-native before removing" in retained.detail


def test_successful_native_smoke_removes_only_completed_workspace(diagnostic, monkeypatch):
    cfg, _ = diagnostic
    roots = []

    def run(self, *, root, **kwargs):
        roots.append(root)
        return _success()

    monkeypatch.setattr(native.WindowsNativeRunner, "run", run)
    result = doctor.diagnose_sandbox(cfg, include_smoke=True)
    assert result.ready and result.status == "experimental_ready"
    assert len(roots) == 1 and not roots[0].exists()
    assert not any(c.name == "retained diagnostic workspace" for c in result.checks)
    compatibility = next(c for c in result.checks if c.name == "Node subprocess compatibility")
    assert compatibility.status == "skipped" and "can hang" in compatibility.detail


@pytest.mark.parametrize("pending_record", [False, True])
def test_native_timeout_retains_original_workspace(diagnostic, monkeypatch, pending_record):
    cfg, journal = diagnostic
    roots = []

    def run(self, *, root, **kwargs):
        roots.append(root)
        (root / "recovery-canary").write_text("original object")
        if pending_record:
            journal.parent.mkdir()
            journal.write_text("pending synthetic diagnostic")
        raise subprocess.TimeoutExpired("helper", 45)

    monkeypatch.setattr(native.WindowsNativeRunner, "run", run)
    result = doctor.diagnose_sandbox(cfg, include_smoke=True)
    _retain_assertion(result, roots[0])
    assert (roots[0] / "recovery-canary").read_text() == "original object"
    if pending_record:
        assert journal.read_text() == "pending synthetic diagnostic"
        retry = doctor.diagnose_sandbox(cfg, include_smoke=True)
        assert not retry.ready and len(roots) == 1


def test_success_marker_with_pending_recovery_is_not_ready(diagnostic, monkeypatch):
    cfg, journal = diagnostic
    roots = []

    def run(self, *, root, **kwargs):
        roots.append(root)
        journal.parent.mkdir()
        journal.write_text("still pending")
        return _success()

    monkeypatch.setattr(native.WindowsNativeRunner, "run", run)
    result = doctor.diagnose_sandbox(cfg, include_smoke=True)
    _retain_assertion(result, roots[0])
    assert journal.read_text() == "still pending"


def test_unreadable_recovery_state_prevents_smoke_cleanup(diagnostic, monkeypatch):
    cfg, journal = diagnostic
    roots = []
    original_lstat = Path.lstat

    def lstat(path, *args, **kwargs):
        if path == journal and roots:
            raise PermissionError("cannot inspect recovery journal")
        return original_lstat(path, *args, **kwargs)

    def run(self, *, root, **kwargs):
        roots.append(root)
        return _success()

    monkeypatch.setattr(Path, "lstat", lstat)
    monkeypatch.setattr(native.WindowsNativeRunner, "run", run)
    result = doctor.diagnose_sandbox(cfg, include_smoke=True)
    _retain_assertion(result, roots[0])
    assert any("cannot inspect recovery journal" in c.detail for c in result.checks)


@pytest.mark.parametrize(
    "code,stdout", [(125, "alysis-sandbox-ready\n"), (0, "Write-Output 'alysis-sandbox-ready'\n")]
)
def test_failed_or_echoed_smoke_is_not_readiness(diagnostic, monkeypatch, code, stdout):
    cfg, _ = diagnostic
    roots = []

    def run(self, *, root, **kwargs):
        roots.append(root)
        return subprocess.CompletedProcess([], code, stdout, "failed or echoed command")

    monkeypatch.setattr(native.WindowsNativeRunner, "run", run)
    result = doctor.diagnose_sandbox(cfg, include_smoke=True)
    _retain_assertion(result, roots[0])


def test_replaced_smoke_path_is_not_deleted(diagnostic, monkeypatch):
    cfg, _ = diagnostic
    roots = []

    def run(self, *, root, **kwargs):
        roots.append(root)
        root.rename(root.with_name(root.name + "-original"))
        root.mkdir()
        (root / "other-owner-canary").write_text("replacement")
        return _success()

    monkeypatch.setattr(native.WindowsNativeRunner, "run", run)
    result = doctor.diagnose_sandbox(cfg, include_smoke=True)
    _retain_assertion(result, roots[0])
    assert (roots[0] / "other-owner-canary").read_text() == "replacement"
    assert any("identity changed" in c.detail for c in result.checks)


def test_keyboard_interrupt_preserves_smoke_and_reports_its_path(diagnostic, monkeypatch):
    cfg, _ = diagnostic
    roots = []

    def run(self, *, root, **kwargs):
        roots.append(root)
        raise KeyboardInterrupt()

    monkeypatch.setattr(native.WindowsNativeRunner, "run", run)
    with pytest.raises(KeyboardInterrupt) as caught:
        doctor.diagnose_sandbox(cfg, include_smoke=True)
    assert roots[0].is_dir()
    assert str(roots[0]) in "\n".join(caught.value.__notes__)


def test_relative_local_app_data_refuses_to_launch(diagnostic, monkeypatch):
    cfg, _ = diagnostic
    monkeypatch.setenv("LOCALAPPDATA", "relative-store")

    def unexpected(*args, **kwargs):
        pytest.fail("must not launch without an absolute recovery location")

    monkeypatch.setattr(native.WindowsNativeRunner, "run", unexpected)
    result = doctor.diagnose_sandbox(cfg, include_smoke=True)
    assert not result.ready
    assert any("absolute LOCALAPPDATA" in c.detail for c in result.checks)
