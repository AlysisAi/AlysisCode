from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

import pytest

from alysis_code import windows_mxc as mxc
from alysis_code import windows_sandbox as native
from alysis_code.config import ConfigError
from alysis_code.sandbox_settings import ShellSandboxSettings


@pytest.fixture
def policy(tmp_path, monkeypatch):
    if os.name != "nt":
        pytest.skip("Windows request admission")
    base = tmp_path / "python"
    (base / "Lib").mkdir(parents=True)
    (base / "python.exe").write_bytes(b"synthetic interpreter")
    (base / "Lib" / "os.py").write_text("# synthetic stdlib")
    monkeypatch.setattr(sys, "base_prefix", str(base))
    workspace = tmp_path / "project"
    workspace.mkdir()
    helper = tmp_path / "helper" / "alysis-sandbox.exe"
    helper.parent.mkdir()
    helper.write_bytes(b"fixture")
    monkeypatch.setattr(mxc, "resolve_helper", lambda: helper)
    monkeypatch.setattr(native, "_powershell_execution_policy", lambda _: "RemoteSigned")
    return base, workspace, mxc.WindowsMxcRunner(ShellSandboxSettings(backend="windows-mxc"))


def prepared(runner, workspace, **kwargs):
    _, encoded = runner._prepare(root=workspace, cwd=workspace, cmd="python --version", **kwargs)
    return json.loads(encoded)


def test_base_python_is_read_only_fallback_not_engine_virtualenv(policy, monkeypatch):
    base, workspace, runner = policy
    monkeypatch.setenv("PATH", r"C:\project-tools;C:\unrelated-private-tool")
    request = prepared(runner, workspace)
    env = dict(request["env"])
    assert env["PATH"].endswith(";" + str(base))
    assert str(base) in request["read"] and str(base) in request["deny_write"]
    assert str(base) not in request["write"]
    assert str(Path(sys.prefix)) not in request["read"]
    assert r"C:\unrelated-private-tool" not in request["read"]
    assert env["PYTHONNOUSERSITE"] == "1"
    assert "PYTHONPATH" not in env


def test_request_path_does_not_choose_another_granted_python(policy):
    base, workspace, runner = policy
    request = prepared(runner, workspace, env_overrides={"PATH": r"C:\attacker-python"})
    # Tool discovery supplies only admitted roots; a request cannot insert a
    # different Python installation ahead of the trusted base interpreter.
    assert dict(request["env"])["PATH"].endswith(";" + str(base))
    assert r"C:\attacker-python" not in dict(request["env"])["PATH"].split(os.pathsep)
    assert r"C:\attacker-python" not in request["read"]


def test_python_tool_read_root_still_rejects_hardlinks(policy):
    base, workspace, runner = policy
    private = workspace.parent / "private.txt"
    private.write_text("unchanged")
    (base / "alias.txt").hardlink_to(private)
    with pytest.raises(ConfigError, match="hardlinked"):
        prepared(runner, workspace)
    assert private.read_text() == "unchanged"


def test_python_tool_cannot_be_the_workspace(policy):
    base, _, runner = policy
    with pytest.raises(ConfigError, match="trusted host Python"):
        prepared(runner, base)


def test_python_tool_never_grants_the_user_profile(policy, monkeypatch):
    base, _, _ = policy
    monkeypatch.setattr(Path, "home", classmethod(lambda _: base))
    with pytest.raises(ConfigError, match="user profile"):
        mxc._python_tool_root()


def test_frozen_and_missing_python_do_not_invent_a_fallback(policy, monkeypatch):
    base, _, _ = policy
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    assert mxc._python_tool_root() is None
    monkeypatch.setattr(sys, "frozen", False)
    (base / "python.exe").unlink()
    assert mxc._python_tool_root() is None


@pytest.mark.skipif(
    os.name != "nt" or not os.environ.get("ALYSIS_TEST_WINDOWS_MXC_HELPER"),
    reason="explicit local native helper required",
)
def test_real_python_project_and_child_keep_runtime_and_outside_read_only(tmp_path, monkeypatch):
    import hashlib
    import shutil

    helper = tmp_path / "helper" / "alysis-sandbox.exe"
    helper.parent.mkdir()
    shutil.copyfile(os.environ["ALYSIS_TEST_WINDOWS_MXC_HELPER"], helper)
    monkeypatch.setenv("ALYSIS_WINDOWS_MXC_HELPER", str(helper))
    monkeypatch.setenv("ALYSIS_WINDOWS_MXC_SHA256", hashlib.sha256(helper.read_bytes()).hexdigest())
    # Force the ordinary command to use the fallback, not this machine's PATH.
    monkeypatch.setenv("PATH", str(Path(os.environ["SystemRoot"]) / "System32"))
    root = tmp_path / "project"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("private marker")
    base = mxc._python_tool_root()
    assert base is not None
    runtime_marker = base / f"alysis-denied-{uuid.uuid4().hex}.txt"
    (root / "probe.py").write_text(
        "import pathlib, subprocess, sys\n"
        "pathlib.Path('result.txt').write_text('project ok')\n"
        f"for path, mode in [(pathlib.Path({str(outside)!r}), 'r'), (pathlib.Path({str(runtime_marker)!r}), 'w')]:\n"
        "    try:\n"
        "        with path.open(mode): pass\n"
        "    except PermissionError: pass\n"
        "    else: raise AssertionError('boundary escaped')\n"
        "child = subprocess.run([sys.executable, '-c', 'print(42)'], capture_output=True, text=True, check=True)\n"
        "assert child.stdout.strip() == '42'\n"
        "print('PYTHON_PROJECT_AND_CHILD_OK')\n"
    )
    runner = mxc.WindowsMxcRunner(ShellSandboxSettings(backend="windows-mxc"))
    result = runner.run(
        root=root, cwd=root, cmd="python probe.py; exit $LASTEXITCODE", timeout_s=30
    )
    assert result.returncode == 0, result.stderr
    assert "PYTHON_PROJECT_AND_CHILD_OK" in result.stdout
    assert (root / "result.txt").read_text() == "project ok"
    assert outside.read_text() == "private marker"
    assert not runtime_marker.exists()
