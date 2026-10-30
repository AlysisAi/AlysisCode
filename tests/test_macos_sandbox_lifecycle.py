"""Adversarial live checks against the actual Seatbelt and host admission paths."""

from __future__ import annotations

import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from alysis_code.agent.sandbox_access_store import SandboxAccessStore
from alysis_code.agent.scoped_access import ScopedAccess
from alysis_code.background_runner import SeatbeltBackgroundRunner
from alysis_code.config import ConfigError
from alysis_code.host_file_policy import HostFilePolicy
from alysis_code.process_reaping import (
    CommandCancelledError,
    CommandCleanupError,
    ProcessGroupRegistry,
)
from alysis_code.sandbox_runner import SeatbeltShellRunner, build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.surface import ApprovalDecision
from alysis_code.terminal_manager import TerminalManager
from alysis_code.terminal_ownership import TerminalOwnershipLedger

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="real macOS Seatbelt required")


def python(source):
    return shlex.join([sys.executable, "-I", "-B", "-c", source])


def until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("Live probe did not reach its expected state")


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


@pytest.fixture
def live(tmp_path, monkeypatch):
    root, assets = tmp_path / "project", tmp_path / "assets"
    root.mkdir()
    assets.mkdir()
    (assets / "canary").write_text("synthetic")
    monkeypatch.setenv("ALYSIS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("ALYSIS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("ALYSIS_DESKTOP_CONTROL_PATHS", "[]")
    monkeypatch.setenv("ALYSIS_DESKTOP_RUNTIME_ROOTS", "[]")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    settings = ShellSandboxSettings(backend="seatbelt")

    def make(session="first"):
        return ScopedAccess(
            root=root,
            settings=settings,
            enabled=True,
            approve=lambda _: ApprovalDecision(True),
            policy=HostFilePolicy.capture(),
            record=lambda *_: None,
            events_since=lambda c: ([], c),
            access_store=SandboxAccessStore(root=root, session_id=session, settings=settings),
        )

    return root, assets, make


def access(manager, proposal=None, *, command="true", tool="shell_run"):
    return manager.prepare(proposal, command=command, tool=tool)


def can_read(manager, path, proposal=None):
    cmd = python(f"import os; print(os.access({str(path)!r},os.R_OK))")
    grant = access(manager, proposal, command=cmd)
    grant.check()
    result = build_shell_runner_from_settings(grant.settings, manager.root).run(
        root=manager.root, cwd=manager.root, cmd=cmd, timeout_s=8
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip() == "True"


@pytest.mark.parametrize("initial_scope", ["turn", "session"])
@pytest.mark.parametrize("extended_scope", ["turn", "session"])
def test_extending_profile_grant_does_not_detach_it_from_revocation(
    live, initial_scope, extended_scope
):
    _, assets, make = live
    owner, reader = make(), make("reader")
    access(owner, {"read_folders": [str(assets)], "save_as": "assets"})
    assert can_read(reader, assets / "canary", {"profile": "assets", "scope": initial_scope})
    access(reader, {"local_preview": True, "scope": extended_scope})
    if extended_scope == "session":
        reader = make("reader")
    assert can_read(reader, assets / "canary")
    owner.manage({"action": "revoke", "profile": "assets"})
    assert not can_read(reader, assets / "canary"), (
        "Revoked profile remained in an extended turn grant"
    )


def test_session_profile_replaces_an_older_broader_turn_grant(live):
    _, assets, make = live
    owner, reader = make(), make("reader")
    access(owner, {"network_domains": ["registry.npmjs.org"], "save_as": "network"})
    access(reader, {"read_folders": [str(assets)], "scope": "turn"})
    assert can_read(reader, assets / "canary")
    assert not can_read(reader, assets / "canary", {"profile": "network", "scope": "session"})
    assert not can_read(reader, assets / "canary"), (
        "An older turn grant overrode the new session profile"
    )


def test_external_file_reuse_rechecks_revocation_during_review(live):
    _, assets, make = live
    manager = make()
    access(manager, {"read_folders": [str(assets)], "scope": "session"})
    manager.validate_reuse = lambda _: make().manage({"action": "revoke"})
    with pytest.raises(ConfigError, match="changed during approval"):
        manager.external_file_root("fs_read", {"path": str(assets / "canary")})


def spawn_child(source, mode):
    argv = f"[sys.executable,'-I','-B','-c',{source!r}]"
    if mode.startswith("spawn_"):
        flags = "setsid=True" if mode == "spawn_setsid" else "setpgroup=0"
        return f"os.posix_spawn(sys.executable,{argv},os.environ,{flags})"
    flags = {"same_group": "", "setsid": ",start_new_session=True", "setpgid": ",process_group=0"}[
        mode
    ]
    return (
        f"subprocess.Popen({argv}{flags},stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).pid"
    )


@pytest.mark.parametrize(
    "mode", ["same_group", "setsid", "setpgid", "spawn_setsid", "spawn_setpgid"]
)
@pytest.mark.parametrize("ending", ["cancel", "timeout"])
def test_cancel_stops_descendants_and_closes_proxy_and_scratch(live, mode, ending):
    root, assets, _ = live
    marker = root / "ready.json"
    child_source = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path('child-access').write_text(str(os.access({str(assets / 'canary')!r},os.R_OK))); "
        "time.sleep(15)"
    )
    source = f"""import subprocess,sys,time,os,json
from pathlib import Path
child={spawn_child(child_source, mode)}
while not Path('child-access').exists(): time.sleep(.01)
Path('ready.json').write_text(json.dumps({{'child':child,'scratch':os.environ['TMPDIR'],'proxy':__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(os.environ['HTTPS_PROXY']).port}}))
time.sleep(15)"""
    info = None
    try:
        with pytest.raises(
            CommandCancelledError if ending == "cancel" else subprocess.TimeoutExpired
        ):
            SeatbeltShellRunner(network="on", network_domains=("registry.npmjs.org",)).run(
                root=root,
                cwd=root,
                cmd=python(source),
                # Include native profile and child-interpreter startup.
                timeout_s=5,
                cancel_check=marker.exists if ending == "cancel" else None,
            )
        info = json.loads(marker.read_text())
        assert (root / "child-access").read_text() == "False"
        assert not Path(info["scratch"]).exists()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", info["proxy"]), timeout=0.3)
        until(lambda: not alive(info["child"]), timeout=2)
    finally:
        if marker.exists():
            info = json.loads(marker.read_text())
            if alive(info["child"]):
                os.kill(info["child"], signal.SIGKILL)


@pytest.mark.parametrize("detached", [False, True])
def test_background_stop_removes_entire_tree_proxy_and_scratch(live, detached):
    root, _, _ = live
    marker = root / "ready.json"
    source = """import subprocess,sys,time,os,json
from pathlib import Path
child=subprocess.Popen([sys.executable,'-I','-B','-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(15)'])
Path('ready.json').write_text(json.dumps({'child':child.pid,'scratch':os.environ['TMPDIR'],'proxy':__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(os.environ['HTTPS_PROXY']).port}))
print('READY',flush=True)
time.sleep(15)"""
    if detached:
        source = source.replace("time.sleep(15)'])", "time.sleep(15)'],start_new_session=True)")
    settings = ShellSandboxSettings(
        backend="seatbelt",
        network="on",
        network_domains=("registry.npmjs.org",),
        background_kill_timeout_s=0.4,
    )
    manager = TerminalManager(
        runner=SeatbeltBackgroundRunner(network="on", network_domains=settings.network_domains),
        settings=settings,
        ownership_ledger=TerminalOwnershipLedger(root.parent / "ownership"),
    )
    try:
        pid = manager.start(root=root, cwd=root, cmd=python(source))
        until(marker.exists)
        info = json.loads(marker.read_text())
        result = manager.kill(pid)
        assert result.status == "killed", result
        until(lambda: not alive(info["child"]))
        assert not Path(info["scratch"]).exists()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", info["proxy"]), timeout=0.3)
    finally:
        manager.shutdown_all()


def test_sandbox_cannot_signal_host_or_another_command_but_can_stop_its_child(live):
    root, _, _ = live
    decoy = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    other = SeatbeltBackgroundRunner().start(
        root=root, cwd=root, cmd=python("import time; time.sleep(30)")
    )
    try:
        source = f"""import os,signal,subprocess,sys,json
denied=[]
for pid in {[decoy.pid, other.popen.pid]!r}:
    try: os.kill(pid,signal.SIGTERM); denied.append(False)
    except PermissionError: denied.append(True)
child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
child.terminate(); child.wait(timeout=3)
os.kill(os.getpid(),0)
print(json.dumps(denied))
"""
        result = SeatbeltShellRunner().run(root=root, cwd=root, cmd=python(source), timeout_s=5)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [True, True]
        assert decoy.poll() is None and other.popen.poll() is None
    finally:
        decoy.terminate()
        decoy.wait(timeout=5)
        other.cleanup()
        other.popen.wait(timeout=5)


def test_unconfirmed_cleanup_is_not_reported_as_successful_cancellation(live, monkeypatch):
    root, _, _ = live
    registry = ProcessGroupRegistry()
    monkeypatch.setattr(registry, "release_if_finished", lambda _: False)
    source = """import time
from pathlib import Path
print('diagnostic output',flush=True)
Path('ready').touch()
time.sleep(15)
"""
    try:
        with pytest.raises(CommandCleanupError) as failure:
            SeatbeltShellRunner(process_group_registry=registry).run(
                root=root,
                cwd=root,
                cmd=python(source),
                timeout_s=3,
                cancel_check=lambda: (root / "ready").exists(),
            )
        assert "diagnostic output" in failure.value.stdout
    finally:
        registry.clear()


@pytest.mark.parametrize("background", [False, True])
def test_restart_recovers_live_owned_root_detached_child_and_scratch(live, monkeypatch, background):
    root, _, _ = live
    owners = root.parent / "owners"
    monkeypatch.setenv("ALYSIS_TERMINAL_OWNERSHIP_DIR", str(owners))
    child = "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)"
    command = python(f"""import os,sys,subprocess,time,json
from pathlib import Path
child=subprocess.Popen([sys.executable,'-I','-B','-c',{child!r}],start_new_session=True)
Path('ready.json').write_text(json.dumps({{'parent':os.getpid(),'child':child.pid,'scratch':os.environ['TMPDIR'],'proxy':__import__('urllib.parse',fromlist=['urlsplit']).urlsplit(os.environ['HTTPS_PROXY']).port}}))
time.sleep(30)
""")
    helper = f"""import time
from pathlib import Path
from alysis_code.background_runner import SeatbeltBackgroundRunner
from alysis_code.sandbox_runner import SeatbeltShellRunner
from alysis_code.sandbox_settings import ShellSandboxSettings
from alysis_code.terminal_manager import TerminalManager
from alysis_code.terminal_ownership import TerminalOwnershipLedger
root=Path({str(root)!r})
if {background!r}:
    manager=TerminalManager(runner=SeatbeltBackgroundRunner(network='on',network_domains=('registry.npmjs.org',)),settings=ShellSandboxSettings(backend='seatbelt'),ownership_ledger=TerminalOwnershipLedger())
    manager.start(root=root,cwd=root,cmd={command!r})
    time.sleep(30)
else:
    SeatbeltShellRunner(network='on',network_domains=('registry.npmjs.org',)).run(root=root,cwd=root,cmd={command!r},timeout_s=30)
"""
    owner = subprocess.Popen(
        [sys.executable, "-c", helper], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    info = None
    try:
        until(lambda: (root / "ready.json").exists() and list(owners.glob("*.json")))
        info = json.loads((root / "ready.json").read_text())
        owner.kill()
        owner.wait(timeout=5)
        assert alive(info["parent"]) and alive(info["child"])
        result = TerminalOwnershipLedger(owners).scavenge_stale()
        assert result and all(item.action in {"terminated", "absent"} for item in result)
        until(lambda: not alive(info["parent"]) and not alive(info["child"]))
        assert not Path(info["scratch"]).exists()
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", info["proxy"]), timeout=0.3)
        assert not list(owners.glob("*.json"))
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=5)
        if (root / "ready.json").exists():
            info = json.loads((root / "ready.json").read_text())
            for pid in (info["parent"], info["child"]):
                if alive(pid):
                    os.kill(pid, signal.SIGKILL)
        TerminalOwnershipLedger(owners).scavenge_stale()


@pytest.mark.parametrize("approve_target", [False, True])
def test_redirect_rechecks_the_destination_endpoint(live, approve_target):
    root, _, _ = live
    requests = []
    redirects = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"UNAPPROVED")

        def log_message(self, *_):
            pass

    target = ThreadingHTTPServer(("127.0.0.1", 0), Destination)

    class Redirect(BaseHTTPRequestHandler):
        def do_GET(self):
            redirects.append(self.path)
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_port}/private")
            self.end_headers()

        def log_message(self, *_):
            pass

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Redirect)
    workers = [threading.Thread(target=s.serve_forever, daemon=True) for s in (target, origin)]
    for t in workers:
        t.start()
    try:
        endpoints = (f"127.0.0.1:{origin.server_port}",) + (
            (f"127.0.0.1:{target.server_port}",) if approve_target else ()
        )
        result = SeatbeltShellRunner(network="on", private_network_endpoints=endpoints).run(
            root=root,
            cwd=root,
            cmd=f"curl --silent --show-error --fail --location --max-time 4 http://127.0.0.1:{origin.server_port}/",
            timeout_s=6,
        )
        assert redirects == ["/"]
        if approve_target:
            assert result.returncode == 0, result.stderr
            assert requests == ["/private"]
        else:
            assert result.returncode != 0 and "403" in result.stderr
            assert requests == []
    finally:
        for s in (origin, target):
            s.shutdown()
            s.server_close()
        for t in workers:
            t.join(timeout=2)


def test_concurrent_auth_scope_does_not_leak_into_ordinary_command(live, monkeypatch):
    root, _, _ = live
    monkeypatch.setenv("REGISTRY_TOKEN", "placeholder-only")
    cmd = python(
        "import os,time,json; time.sleep(.1); print(json.dumps('REGISTRY_TOKEN' in os.environ))"
    )

    def run(granted):
        runner = SeatbeltShellRunner(forward_env=("REGISTRY_TOKEN",) if granted else ())
        # Four native profiles prepare concurrently on shared CI hardware.
        result = runner.run(root=root, cwd=root, cmd=cmd, timeout_s=15)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(run, [True, False, True, False])) == [True, False, True, False]
