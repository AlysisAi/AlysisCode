"""Kernel-policy ownership, including ancestry that the engine never observed."""

from __future__ import annotations

import json
import os
import secrets
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

import alysis_code.macos_sandbox_owner as native
from alysis_code.background_runner import SeatbeltBackgroundRunner
from alysis_code.process_reaping import CommandCleanupError
from alysis_code.sandbox_runner import SeatbeltShellRunner
from alysis_code.terminal_ownership import TerminalOwnershipLedger

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="native Seatbelt ownership")


def until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Native fixture did not reach its expected state")


def living(pid):
    identity = native.process_identity(pid)
    return identity is not None and identity.status != 5


def python(source):
    return shlex.join([sys.executable, "-I", "-B", "-c", source])


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = (tmp_path / "project").resolve()
    root.mkdir()
    (tmp_path / "canary").write_text("synthetic")
    for name, value in {
        "ALYSIS_CONFIG_DIR": str(tmp_path / "config"),
        "ALYSIS_DATA_DIR": str(tmp_path / "data"),
        "ALYSIS_TERMINAL_OWNERSHIP_DIR": str(tmp_path / "owners"),
        "ALYSIS_DESKTOP_CONTROL_PATHS": "[]",
        "ALYSIS_DESKTOP_RUNTIME_ROOTS": "[]",
        "GIT_CONFIG_GLOBAL": "/dev/null",
    }.items():
        monkeypatch.setenv(name, value)
    return root


@pytest.mark.parametrize("ending", ["complete", "restart"])
@pytest.mark.parametrize("background", [False, True])
@pytest.mark.parametrize("keep_pipes", [False, True])
def test_unobserved_double_fork_is_cleaned(workspace, ending, background, keep_pipes):
    root = workspace
    source = f"""import os,time,json,signal
from pathlib import Path
Path('parent-ready').touch()
while not Path('go').exists(): time.sleep(.01)
if os.fork(): os._exit(0)
os.setsid()
if os.fork(): os._exit(0)
signal.signal(signal.SIGTERM,signal.SIG_IGN)
if not {keep_pipes!r}:
 fd=os.open('/dev/null',os.O_RDWR)
 for n in (0,1,2):os.dup2(fd,n)
Path('ready.json').write_text(json.dumps({{'pid':os.getpid(),'scratch':os.environ['TMPDIR'],'canary':os.access({str(root.parent / "canary")!r},os.R_OK)}}))
time.sleep(30)
"""
    helper = f"""from pathlib import Path
from alysis_code.sandbox_runner import SeatbeltShellRunner
from alysis_code.background_runner import SeatbeltBackgroundRunner
from alysis_code.terminal_manager import TerminalManager
from alysis_code.sandbox_settings import ShellSandboxSettings
root=Path({str(root)!r})
if {background!r}:
 manager=TerminalManager(runner=SeatbeltBackgroundRunner(),settings=ShellSandboxSettings(backend="seatbelt"))
 process=manager.start(root=root,cwd=root,cmd={python(source)!r})
 import time
 while manager.list()[0].status=='running':time.sleep(.01)
 assert manager.list()[0].status=='exited',manager.list()[0]
else:
 result=SeatbeltShellRunner().run(root=root,cwd=root,cmd={python(source)!r},timeout_s=15)
 assert result.returncode==0,result.stderr
"""
    engine = subprocess.Popen(
        [sys.executable, "-c", helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    info = None
    try:
        until(lambda: (root / "parent-ready").exists())
        # Pause only our disposable engine. The child forks and loses both
        # ancestors before any host observer can sample those relationships.
        os.kill(engine.pid, signal.SIGSTOP)
        (root / "go").touch()
        until(lambda: (root / "ready.json").exists())
        info = json.loads((root / "ready.json").read_text())
        time.sleep(0.1)
        assert living(info["pid"]) and not info["canary"]
        if ending == "restart":
            engine.kill()
            engine.wait(timeout=5)
            result = TerminalOwnershipLedger().scavenge_stale()
            assert result and all(item.action in {"terminated", "absent"} for item in result)
        else:
            os.kill(engine.pid, signal.SIGCONT)
        out, err = engine.communicate(timeout=8)
        if ending == "complete":
            assert engine.returncode == 0, (out, err)
        until(lambda: not living(info["pid"]))
        assert not Path(info["scratch"]).exists()
        assert not list((root.parent / "owners").glob("*.json"))
    finally:
        if engine.poll() is None:
            os.kill(engine.pid, signal.SIGCONT)
            engine.kill()
        engine.communicate(timeout=5)
        if (root / "ready.json").exists():
            info = json.loads((root / "ready.json").read_text())
            if living(info["pid"]):
                os.kill(info["pid"], signal.SIGKILL)
        TerminalOwnershipLedger().scavenge_stale()


def test_other_sandboxes_and_host_processes_are_not_owned(workspace):
    code = "import os,time;print(os.getpid(),flush=True);time.sleep(30)"
    children = []
    commands = []
    try:
        for profile in (
            None,
            "(version 1)(allow default)",
            "(version 1)(deny default)(allow process* file-read* file-map-executable sysctl-read mach-bootstrap)",
        ):
            argv = [sys.executable, "-I", "-B", "-c", code]
            if profile is not None:
                argv = ["/usr/bin/sandbox-exec", "-p", profile, *argv]
            child = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
            )
            children.append(child)
            assert child.stdout.readline().strip() == str(child.pid)
        for _ in range(2):
            spawn = SeatbeltBackgroundRunner().start(
                root=workspace, cwd=workspace, cmd=python(code)
            )
            commands.append(spawn)
            assert int(spawn.popen.stdout.readline()) > 1
        owner = commands[0].process_tree
        assert owner.alive()
        assert owner.terminate(0)
        assert not owner.alive()
        assert commands[1].process_tree.alive()
        assert all(child.poll() is None for child in children)
    finally:
        for spawn in commands:
            spawn.cleanup()
            spawn.popen.communicate(timeout=5)
        for child in children:
            child.kill()
            child.communicate(timeout=5)


@pytest.mark.parametrize("changed", ["reused", "unknown", "policy_changed"])
def test_revalidation_failure_never_signals(workspace, monkeypatch, changed):
    child = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(20)"])
    identity = native.process_identity(child.pid)
    owner = native.MacSandboxOwner(workspace / f"alysis-seatbelt-{secrets.token_hex(16)}-unit")
    try:
        monkeypatch.setattr(owner, "_members", lambda: (identity,))
        monkeypatch.setattr(owner, "_matches", lambda _: changed != "policy_changed")
        monkeypatch.setattr(
            native,
            "process_identity",
            lambda _: (
                None
                if changed == "unknown"
                else replace(identity, start_usec=identity.start_usec + 1)
            ),
        )
        owner.signal(signal.SIGKILL)
        assert child.poll() is None
    finally:
        child.kill()
        child.wait(timeout=5)


def test_query_failure_is_not_successful_cleanup(workspace, monkeypatch):
    owner = native.MacSandboxOwner(workspace / f"alysis-seatbelt-{secrets.token_hex(16)}-unit")

    def unavailable():
        raise OSError("synthetic API failure")

    monkeypatch.setattr(owner, "_members", unavailable)
    assert owner.alive()
    assert not owner.terminate(0)


def test_cleanup_failure_retains_prelaunch_record(workspace, monkeypatch):
    monkeypatch.setattr(native.MacSandboxOwner, "terminate", lambda *_: False)
    with pytest.raises(CommandCleanupError):
        SeatbeltShellRunner().run(root=workspace, cwd=workspace, cmd="true", timeout_s=5)
    assert list((workspace.parent / "owners").glob("*.json"))


def test_nested_policy_cannot_remove_ownership(workspace):
    source = """import ctypes,os,time
from pathlib import Path
lib=ctypes.CDLL('/usr/lib/libsandbox.dylib')
error=ctypes.c_char_p()
rc=lib.sandbox_init(b'(version 1)(allow default)',0,ctypes.byref(error))
if error:lib.sandbox_free_error(error)
print(os.getpid(),flush=True)
time.sleep(20)
"""
    spawn = SeatbeltBackgroundRunner().start(root=workspace, cwd=workspace, cmd=python(source))
    try:
        pid = int(spawn.popen.stdout.readline())
        assert spawn.process_tree._matches(pid)
        assert spawn.process_tree.terminate(0)
        until(lambda: not living(pid))
    finally:
        spawn.cleanup()
        spawn.popen.communicate(timeout=5)


def test_process_born_during_empty_sweep_is_not_missed(workspace, monkeypatch):
    owner = native.MacSandboxOwner(workspace / f"alysis-seatbelt-{secrets.token_hex(16)}-unit")
    snapshots = iter([(101,), (102,), (102,)])
    monkeypatch.setattr(native, "_user_pids", lambda: next(snapshots))
    marker = object()
    monkeypatch.setattr(owner, "_inspect", lambda pids: () if pids == (101,) else (marker,))
    assert owner._members() == (marker,)


def test_launcher_record_failure_never_releases_command(workspace, monkeypatch):
    original = TerminalOwnershipLedger.track

    def refuse_launcher(self, **kwargs):
        if kwargs["termination_mode"] == "process_group":
            return None
        return original(self, **kwargs)

    monkeypatch.setattr(TerminalOwnershipLedger, "track", refuse_launcher)
    with pytest.raises(OSError, match="launcher ownership"):
        SeatbeltShellRunner().run(
            root=workspace, cwd=workspace, cmd="touch must-not-run", timeout_s=5
        )
    assert not (workspace / "must-not-run").exists()
    assert not list((workspace.parent / "owners").glob("*.json"))


@pytest.mark.parametrize("persisted", [False, True])
def test_engine_death_before_gate_release_never_starts_user_code(workspace, persisted):
    helper = f"""import time,os
from pathlib import Path
from alysis_code.sandbox_runner import SeatbeltShellRunner
from alysis_code.terminal_ownership import TerminalOwnershipLedger
root=Path({str(workspace)!r})
original=TerminalOwnershipLedger.track
def hold(self,**kwargs):
 if kwargs['termination_mode']=='process_group':
  if {persisted!r}:original(self,**kwargs)
  (root/'gate-ready').write_text(str(kwargs['child_pid']))
  time.sleep(30)
 return original(self,**kwargs)
TerminalOwnershipLedger.track=hold
SeatbeltShellRunner().run(root=root,cwd=root,cmd='touch must-not-run',timeout_s=10)
"""
    engine = subprocess.Popen(
        [sys.executable, "-c", helper], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        until(lambda: (workspace / "gate-ready").exists())
        pid = int((workspace / "gate-ready").read_text())
        engine.kill()
        engine.communicate(timeout=5)
        result = TerminalOwnershipLedger().scavenge_stale()
        assert result and all(item.action in {"terminated", "absent"} for item in result)
        until(lambda: not living(pid))
        assert not (workspace / "must-not-run").exists()
        assert not list((workspace.parent / "owners").glob("*.json"))
    finally:
        if engine.poll() is None:
            engine.kill()
        engine.communicate(timeout=5)
        TerminalOwnershipLedger().scavenge_stale()
