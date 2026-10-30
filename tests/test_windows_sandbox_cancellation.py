"""Windows runner cancellation contract using a bounded trusted host helper.

The fixture speaks the runner's stdin protocol, but does not invoke native
containment. Original native job/descendant cancellation needs installed tests.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from alysis_code import windows_sandbox as native
from alysis_code.process_reaping import CommandCancelledError, CommandCleanupError
from alysis_code.sandbox_runner import LazyShellRunner
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.tools.shell import ShellError, shell_run
from alysis_code.windows_mxc import WindowsMxcRunner

_HELPER = """
import json, pathlib, sys, time
request = json.load(sys.stdin)
assert request['nonce'] == 'request-sent-once'
sys.stdout.buffer.write('partial caf\\u00e9\\n'.encode('utf-8'))
sys.stdout.buffer.flush()
sys.stderr.buffer.write(b'partial stderr \\xff\\n')
sys.stderr.buffer.flush()
pathlib.Path('ready').write_text('ready')
time.sleep(0.25 if request['cmd'] == 'complete' else 30)
sys.stdout.buffer.write(b'complete\\n')
sys.stdout.buffer.flush()
"""


@pytest.fixture(params=[native.WindowsNativeRunner, WindowsMxcRunner], ids=["native", "mxc"])
def runner(request, monkeypatch: pytest.MonkeyPatch):
    runner_type = request.param

    def prepare(self, *, root, cwd, cmd):
        return [sys.executable, "-u", "-c", _HELPER], json.dumps(
            {"nonce": "request-sent-once", "cmd": cmd}
        ).encode()

    monkeypatch.setattr(runner_type, "_prepare", prepare)
    # This portable fixture exercises the helper stdin/cancellation protocol.
    # Writable MXC additionally owns native Windows Git reservation handles,
    # which are outside this synthetic helper's contract.
    return runner_type(ShellSandboxSettings(filesystem_access="read-only"))


@pytest.fixture
def processes(monkeypatch: pytest.MonkeyPatch):
    original = subprocess.Popen
    spawned = []

    def spawn(*args, **kwargs):
        child = original(*args, **kwargs)
        spawned.append(child)
        return child

    monkeypatch.setattr(native.subprocess, "Popen", spawn)
    yield spawned
    for child in spawned:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)


def test_sliced_wait_sends_one_complete_request_and_keeps_output(runner, processes, tmp_path):
    result = runner.run(
        root=tmp_path, cwd=tmp_path, cmd="complete", timeout_s=5, cancel_check=lambda: False
    )
    assert result.returncode == 0
    assert result.stdout == "partial caf\u00e9\ncomplete\n"
    assert result.stderr == "partial stderr \ufffd\n"
    assert len(processes) == 1 and processes[0].poll() == 0


def test_lazy_runner_cancellation_reports_decoded_partial_output(runner, processes, tmp_path):
    with pytest.raises(ShellError) as caught:
        shell_run(
            root=tmp_path,
            cmd="wait",
            runner=LazyShellRunner(lambda: runner),
            timeout_s=5,
            cancel_check=lambda: (tmp_path / "ready").exists(),
        )
    payload = caught.value.result_payload
    assert payload is not None and payload["cancelled"] is True
    assert payload["cmd"] == "wait"
    assert payload["stdout"] == "partial caf\u00e9\n"
    assert payload["stderr"] == "partial stderr \ufffd\n"
    assert isinstance(caught.value.__cause__, CommandCancelledError)
    assert caught.value.__cause__.cmd == "wait"
    assert len(processes) == 1 and processes[0].poll() is not None


def test_timeout_with_cancellation_enabled_reaps_helper_and_keeps_output(
    runner, processes, tmp_path
):
    with pytest.raises(subprocess.TimeoutExpired) as caught:
        runner.run(root=tmp_path, cwd=tmp_path, cmd="wait", timeout_s=1, cancel_check=lambda: False)
    assert caught.value.timeout == 1
    assert caught.value.stdout == "partial caf\u00e9\n"
    assert caught.value.stderr == "partial stderr \ufffd\n"
    assert len(processes) == 1 and processes[0].poll() is not None


def test_callback_exception_reaps_the_original_helper(runner, processes, tmp_path):
    class ProbeFailed(Exception):
        pass

    def check():
        if (tmp_path / "ready").exists():
            raise ProbeFailed("cancel probe failed")
        return False

    with pytest.raises(ProbeFailed, match="cancel probe failed"):
        runner.run(root=tmp_path, cwd=tmp_path, cmd="wait", timeout_s=5, cancel_check=check)
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.parametrize("before_prepare", [True, False], ids=["before-prepare", "before-spawn"])
def test_already_cancelled_command_never_launches(
    runner, processes, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, before_prepare: bool
):
    prepared = []
    original_prepare = type(runner)._prepare

    def prepare(self, **kwargs):
        prepared.append(True)
        return original_prepare(self, **kwargs)

    monkeypatch.setattr(type(runner), "_prepare", prepare)
    with pytest.raises(CommandCancelledError) as caught:
        runner.run(
            root=tmp_path,
            cwd=tmp_path,
            cmd="must not execute",
            timeout_s=5,
            cancel_check=lambda: before_prepare or bool(prepared),
        )
    assert caught.value.cmd == "must not execute"
    assert bool(prepared) is not before_prepare
    assert processes == []


@pytest.mark.skipif(os.name != "nt", reason="Windows lifetime job contract")
def test_job_assignment_failure_never_delivers_the_workload(
    runner, processes, tmp_path, monkeypatch
):
    from alysis_code import _windows_sandbox_job as jobs

    class UnavailableJob:
        def __init__(self, process):
            raise OSError("synthetic assignment failure")

    monkeypatch.setattr(jobs, "WindowsSandboxJob", UnavailableJob)
    with pytest.raises(OSError, match="synthetic assignment failure"):
        runner.run(root=tmp_path, cwd=tmp_path, cmd="wait", timeout_s=5)
    assert not (tmp_path / "ready").exists()
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows lifetime job contract")
def test_failed_drain_reaps_helper_without_acknowledging_cancellation(
    runner, processes, tmp_path, monkeypatch
):
    from alysis_code import _windows_sandbox_job as jobs

    class FailedDrainJob(jobs.WindowsSandboxJob):
        def terminate_and_drain(self):
            raise OSError("synthetic observation failure")

    monkeypatch.setattr(jobs, "WindowsSandboxJob", FailedDrainJob)
    with pytest.raises(CommandCleanupError, match="cleanup could not be confirmed") as caught:
        runner.run(
            root=tmp_path,
            cwd=tmp_path,
            cmd="wait",
            timeout_s=5,
            cancel_check=lambda: (tmp_path / "ready").exists(),
        )
    assert isinstance(caught.value.__cause__, OSError)
    assert caught.value.stdout == "partial caf\u00e9\n"
    assert caught.value.stderr == "partial stderr \ufffd\n"
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.skipif(os.name != "nt", reason="Windows lifetime job contract")
@pytest.mark.parametrize("timeout", [False, True], ids=["cancelled", "timed-out"])
def test_failed_drain_surfaces_output_without_acknowledging_cleanup(
    runner, processes, tmp_path, monkeypatch, timeout
):
    from alysis_code import _windows_sandbox_job as jobs

    class FailedDrainJob(jobs.WindowsSandboxJob):
        def terminate_and_drain(self):
            raise OSError("synthetic observation failure")

    monkeypatch.setattr(jobs, "WindowsSandboxJob", FailedDrainJob)
    with pytest.raises(ShellError) as caught:
        shell_run(
            root=tmp_path,
            cmd="wait",
            runner=LazyShellRunner(lambda: runner),
            timeout_s=1 if timeout else 5,
            cancel_check=lambda: not timeout and (tmp_path / "ready").exists(),
        )
    payload = caught.value.result_payload
    assert payload is not None
    assert payload["cancelled"] is False and payload["cleanup_confirmed"] is False
    assert "cleanup could not be confirmed" in payload["error"]
    assert payload["stdout"] == "partial caf\u00e9\n"
    assert payload["stderr"] == "partial stderr \ufffd\n"
    assert payload["cmd"] == "wait"
    assert payload["cwd"] == str(tmp_path.resolve())
    assert isinstance(caught.value.__cause__, CommandCleanupError)
    assert isinstance(caught.value.__cause__.__cause__, OSError)
    assert len(processes) == 1 and processes[0].poll() is not None


@pytest.mark.parametrize("command", ["python -c pass", "python3 -c pass"])
def test_cleanup_failure_payload_is_bounded_on_explicit_commands(tmp_path, command):
    calls = []

    class FailedCleanupRunner:
        def run(self, **kwargs):
            calls.append(kwargs["cmd"])
            raise CommandCleanupError(
                kwargs["cmd"], stdout="x" * 20_001, stderr="partial caf\u00e9"
            )

    with pytest.raises(ShellError) as caught:
        shell_run(root=tmp_path, cmd=command, runner=FailedCleanupRunner())
    payload = caught.value.result_payload
    assert payload is not None
    assert payload["cancelled"] is False and payload["cleanup_confirmed"] is False
    assert payload["stdout"] == "x" * 20_000 + "...(truncated)"
    assert payload["stderr"] == "partial caf\u00e9"
    assert payload["truncated"] is True
    assert payload["cmd"] == command
    assert calls == [command]
