"""Probes run inside the real MXC sandbox on this machine, with the app's settings.

A command in this sandbox follows the mode instead of always needing a person,
so what the sandbox refuses is the boundary. Nothing here is mocked: host
listeners, pipes, files and registry values are real, the commands run in the
real sandbox, and the host checks afterwards what actually arrived or changed.

``windows_mxc_containment.REACHABLE_WHEN_CONTAINED`` records what the probes
found still reachable. ``test_record_of_what_stays_reachable_matches_the_sandbox``
fails when the record and the sandbox disagree in either direction, and the
probed security revision must be the one the engine requires.

Run with::

    ALYSIS_RUN_CONTAINED_PROBES=1 python -m pytest tests/test_windows_contained_probes.py

The probes use synthetic data only. They create and remove one value under
``HKCU\\Environment`` and listen on loopback ports while they run.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.sandbox_runner import build_shell_runner_from_settings
from alysis_code.sandbox_settings import ShellSandboxSettings

pytestmark = pytest.mark.skipif(
    os.name != "nt" or os.environ.get("ALYSIS_RUN_CONTAINED_PROBES") != "1",
    reason="real Windows sandbox probes; set ALYSIS_RUN_CONTAINED_PROBES=1 on Windows",
)

FIXTURES = Path(__file__).parent / "fixtures" / "windows_contained"
# What the desktop app passes for "Approve for me" with network access off.
APP_SETTINGS = replace(
    ShellSandboxSettings(),
    mode="strict",
    backend="auto",
    network="off",
    filesystem_access="workspace-write",
)
DENIED = "10013"  # WSAEACCES: the platform refused the socket operation


def _runner(root: Path):
    from alysis_code.windows_mxc import WindowsMxcRunner

    runner = build_shell_runner_from_settings(APP_SETTINGS, root)
    assert type(runner) is WindowsMxcRunner
    return runner


def _contained(root: Path, command: str, timeout: int = 300):
    return _runner(root).run(root=root, cwd=root, cmd=command, timeout_s=timeout)


def _results(root: Path) -> dict[str, tuple[str, str]]:
    log = (root / "results.log").read_text(encoding="utf-8-sig", errors="replace")
    found = {}
    for line in log.splitlines():
        if line.startswith("PROBE|"):
            _, name, status, detail = line.split("|", 3)
            found[name] = (status, detail)
    return found


def _repository(root: Path, layout: str) -> Path:
    """A repository with one commit; returns the folder that holds its metadata."""
    from dulwich import porcelain

    root.mkdir()
    porcelain.init(str(root))
    (root / "a.txt").write_text("a\n")
    porcelain.add(str(root), [str(root / "a.txt")])
    identity = b"Probe <probe@example.invalid>"
    porcelain.commit(str(root), message=b"init", author=identity, committer=identity)
    if layout == "plain":
        return root / ".git"
    if layout == "separate":
        # What `git init --separate-git-dir` leaves: a .git file and the repository elsewhere.
        (root / ".git").rename(root / "meta")
        (root / ".git").write_text("gitdir: meta\n")
        return root / "meta"
    if layout == "separate-named-git":
        # The same, kept in a folder whose own name is .git.
        (root / "store").mkdir()
        (root / ".git").rename(root / "store" / ".git")
        (root / ".git").write_text("gitdir: store/.git\n")
        return root / "store" / ".git"
    # The project is a linked worktree of a repository kept inside the project:
    # the worktree's entry names the shared repository through `commondir`.
    (root / "main").mkdir()
    (root / ".git").rename(root / "main" / ".git")
    entry = root / "main" / ".git" / "worktrees" / "wt"
    entry.mkdir(parents=True)
    (entry / "commondir").write_text("../..\n")
    (entry / "HEAD").write_text("ref: refs/heads/master\n")
    (entry / "gitdir").write_text(str(root / ".git") + "\n")
    (root / ".git").write_text("gitdir: main/.git/worktrees/wt\n")
    return root / "main" / ".git"


def _snapshot(folder: Path) -> dict[str, str]:
    state = {}
    for path in sorted(folder.rglob("*")):
        name = path.relative_to(folder).as_posix()
        state[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "dir"
    return state


# ---- host side: receivers the sandbox must not reach ---------------------------


class _Listeners:
    def __init__(self):
        self.events: list[tuple[str, str]] = []
        self.sockets = {}
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as lookup:
            try:
                lookup.connect(("192.0.2.1", 9))  # a UDP connect sends nothing
                lan = lookup.getsockname()[0]
            except OSError:
                lan = None
        targets = [
            ("tcp4", socket.AF_INET, socket.SOCK_STREAM, "127.0.0.1"),
            ("tcp6", socket.AF_INET6, socket.SOCK_STREAM, "::1"),
            ("udp4", socket.AF_INET, socket.SOCK_DGRAM, "127.0.0.1"),
            ("udp6", socket.AF_INET6, socket.SOCK_DGRAM, "::1"),
        ]
        if lan and not lan.startswith("127."):
            targets += [
                ("tcplan", socket.AF_INET, socket.SOCK_STREAM, lan),
                ("udplan", socket.AF_INET, socket.SOCK_DGRAM, lan),
            ]
        for name, family, kind, address in targets:
            receiver = socket.socket(family, kind)
            receiver.bind((address, 0))
            if kind == socket.SOCK_STREAM:
                receiver.listen(8)
            receiver.settimeout(0.2)
            self.sockets[name] = (receiver, address, receiver.getsockname()[1], kind)
            threading.Thread(target=self._serve, args=(name, receiver, kind), daemon=True).start()

    def _serve(self, name, receiver, kind):
        while True:
            try:
                if kind == socket.SOCK_STREAM:
                    client, _ = receiver.accept()
                    client.settimeout(0.5)
                    try:
                        data = client.recv(200)
                    except OSError:
                        data = b""
                    client.close()
                else:
                    data, _ = receiver.recvfrom(200)
                self.events.append((name, data[:60].decode("latin1")))
            except TimeoutError:
                continue
            except OSError:
                return

    def endpoints(self):
        return {name: [address, port] for name, (_, address, port, _) in self.sockets.items()}

    def reached_by_the_host(self) -> set[str]:
        """Control: the host itself reaches every receiver, so silence means refusal."""
        for _, address, port, kind in self.sockets.values():
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            with socket.socket(family, kind) as client:
                client.settimeout(2)
                if kind == socket.SOCK_STREAM:
                    client.connect((address, port))
                    client.send(b"host-control")
                else:
                    client.sendto(b"host-control", (address, port))
        time.sleep(0.8)
        reached = {name for name, data in self.events if data == "host-control"}
        self.events.clear()
        return reached

    def close(self):
        for receiver, *_ in self.sockets.values():
            receiver.close()


class _Pipes:
    """Host pipe servers with the security a host process might give them."""

    KINDS = {
        "default": None,
        "everyone": "D:(A;;GA;;;WD)",
        "everyone_low_integrity": "D:(A;;GA;;;WD)S:(ML;;NW;;;LW)",
        # What crash handlers of browser-based apps use for their own sandboxed children.
        "every_app_container": "D:(A;;GA;;;WD)(A;;GA;;;AC)S:(ML;;NW;;;LW)",
    }

    def __init__(self):
        from ctypes import wintypes as w

        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        self.kernel.CreateNamedPipeW.restype = w.HANDLE
        self.kernel.CreateNamedPipeW.argtypes = [w.LPCWSTR, *([w.DWORD] * 6), w.LPVOID]
        self.kernel.ConnectNamedPipe.argtypes = [w.HANDLE, w.LPVOID]
        self.kernel.DisconnectNamedPipe.argtypes = [w.HANDLE]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        self.kernel.ReadFile.argtypes = [
            w.HANDLE,
            w.LPVOID,
            w.DWORD,
            ctypes.POINTER(w.DWORD),
            w.LPVOID,
        ]
        self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
            w.LPCWSTR,
            w.DWORD,
            ctypes.POINTER(w.LPVOID),
            w.LPVOID,
        ]
        self.tag = uuid.uuid4().hex[:10]
        self.events: list[tuple[str, str]] = []
        for kind, sddl in self.KINDS.items():
            threading.Thread(target=self._serve, args=(kind, sddl), daemon=True).start()

    def name(self, kind):
        return rf"\\.\pipe\alysis-contained-probe-{self.tag}-{kind}"

    def _serve(self, kind, sddl):
        from ctypes import wintypes as w

        class Attributes(ctypes.Structure):
            _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]

        attributes = None
        if sddl:
            descriptor = w.LPVOID()
            if not self.advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                sddl, 1, ctypes.byref(descriptor), None
            ):
                self.events.append((kind, "could not build the security descriptor"))
                return
            attributes = Attributes(ctypes.sizeof(Attributes), descriptor, False)
        while True:
            handle = self.kernel.CreateNamedPipeW(
                self.name(kind),
                3,
                0,
                255,
                4096,
                4096,
                0,
                ctypes.byref(attributes) if attributes else None,
            )
            if handle == w.HANDLE(-1).value:
                self.events.append((kind, "could not create the pipe"))
                return
            self.kernel.ConnectNamedPipe(handle, None)
            buffer, count = ctypes.create_string_buffer(64), w.DWORD()
            self.kernel.ReadFile(handle, buffer, 64, ctypes.byref(count), None)
            self.events.append((kind, buffer.raw[: count.value].decode("latin1")))
            self.kernel.DisconnectNamedPipe(handle)
            self.kernel.CloseHandle(handle)

    def reached_by_the_host(self) -> set[str]:
        time.sleep(0.3)
        for kind in self.KINDS:
            with open(self.name(kind), "r+b", buffering=0) as pipe:
                pipe.write(b"host-control")
        time.sleep(0.5)
        reached = {kind for kind, data in self.events if data == "host-control"}
        self.events.clear()
        return reached


def _dpapi_protect(data: bytes) -> bytes:
    from ctypes import wintypes as w

    class Blob(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("data", ctypes.POINTER(ctypes.c_char))]

    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    buffer = ctypes.create_string_buffer(data, len(data))
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    protected = Blob()
    if not crypt.CryptProtectData(
        ctypes.byref(source), None, None, None, None, 0, ctypes.byref(protected)
    ):
        raise ctypes.WinError(ctypes.get_last_error())
    return ctypes.string_at(protected.data, protected.size)


# ---- the probes, each run once ---------------------------------------------


@pytest.fixture(scope="module")
def network(tmp_path_factory):
    root = tmp_path_factory.mktemp("network") / "project"
    root.mkdir()
    listeners, pipes = _Listeners(), _Pipes()
    assert listeners.reached_by_the_host() == set(listeners.sockets)
    assert pipes.reached_by_the_host() == set(pipes.KINDS)
    shutil.copy(FIXTURES / "net_probe.py", root / "net_probe.py")
    listen_file = root / "listening-on-"
    spec = {
        "endpoints": listeners.endpoints(),
        "pipes": {kind: pipes.name(kind) for kind in pipes.KINDS},
        "listen_file": str(listen_file),
    }
    (root / "spec.json").write_text(json.dumps(spec))
    inbound: dict[str, str] = {}

    def connect_in():
        """While the command listens, the host tries to connect to it."""
        deadline = time.time() + 180
        while time.time() < deadline and len(inbound) < 2:
            for suffix, address, family in (
                ("4", "127.0.0.1", socket.AF_INET),
                ("6", "::1", socket.AF_INET6),
            ):
                announced = Path(str(listen_file) + suffix)
                if suffix in inbound or not announced.exists():
                    continue
                try:
                    port = int(announced.read_text())
                except (OSError, ValueError):
                    continue
                with socket.socket(family) as client:
                    client.settimeout(2)
                    try:
                        client.connect((address, port))
                        inbound[suffix] = "connected"
                    except OSError:
                        inbound[suffix] = "refused"
            time.sleep(0.1)

    threading.Thread(target=connect_in, daemon=True).start()
    completed = _contained(root, "python net_probe.py spec.json")
    lines = [line for line in completed.stdout.splitlines() if line.startswith("PROBE_JSON ")]
    assert lines, completed.stderr
    time.sleep(1)
    yield SimpleNamespace(
        results={name: tuple(value) for name, value in json.loads(lines[0][11:]).items()},
        arrived=list(listeners.events),
        pipe_clients=list(pipes.events),
        inbound=inbound,
    )
    listeners.close()


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    import winreg

    base = tmp_path_factory.mktemp("host")
    root, outside = base / "project", base / "outside"
    root.mkdir()
    outside.mkdir()
    secret = uuid.uuid4().hex
    (outside / "canary.txt").write_text(secret)
    listeners = _Listeners()
    assert listeners.reached_by_the_host() == set(listeners.sockets)
    tag = "AlysisContainedProbe" + uuid.uuid4().hex[:8]
    startup = Path(os.environ["APPDATA"]) / "Microsoft/Windows/Start Menu/Programs/Startup"
    write_paths = {
        "beside_the_project": base / f"{tag}.txt",
        "outside_folder": outside / f"{tag}.txt",
        "temp": Path(tempfile.gettempdir()) / f"{tag}.txt",
        "home": Path.home() / f"{tag}.txt",
        "appdata": Path(os.environ["APPDATA"]) / f"{tag}.txt",
        "startup": startup / f"{tag}.cmd",
    }
    spec = {
        "port": listeners.endpoints()["tcp4"][1],
        "outside": str(outside),
        "variable": tag,
        "task": tag,
        "host_pid": os.getpid(),
        "dpapi_blob": base64.b64encode(_dpapi_protect(secret.encode())).decode(),
        "write_paths": [{"label": label, "path": str(path)} for label, path in write_paths.items()],
    }
    (root / "spec.json").write_text(json.dumps(spec))
    shutil.copy(FIXTURES / "host_probe.ps1", root / "host_probe.ps1")
    previous = os.environ.get("ALYSIS_PROBE_SECRET")
    os.environ["ALYSIS_PROBE_SECRET"] = secret  # the engine's own environment holds a secret
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, tag, 0, winreg.REG_SZ, secret)  # as `setx NAME value` saves it
    try:
        _contained(root, "& .\\host_probe.ps1 -SpecPath .\\spec.json")
    finally:
        if previous is None:
            del os.environ["ALYSIS_PROBE_SECRET"]
        else:
            os.environ["ALYSIS_PROBE_SECRET"] = previous
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, tag)
    time.sleep(2)
    left_behind = []
    for label, path in write_paths.items():
        if path.exists():
            left_behind.append(f"file:{label}")
            path.unlink()
    for key_path, name in (
        (r"Software\Microsoft\Windows\CurrentVersion\Run", tag),
        ("Environment", tag + "W"),
    ):
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_ALL_ACCESS) as key:
            try:
                winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                continue
            left_behind.append(f"registry:{key_path}")
            winreg.DeleteValue(key, name)
    if subprocess.run(["schtasks", "/query", "/tn", tag], capture_output=True).returncode == 0:
        left_behind.append("scheduled task")
        subprocess.run(["schtasks", "/delete", "/f", "/tn", tag], capture_output=True)
    yield SimpleNamespace(
        results=_results(root),
        secret=secret,
        arrived=list(listeners.events),
        outside=sorted(path.name for path in outside.iterdir()),
        left_behind=left_behind,
    )
    listeners.close()


@pytest.fixture(
    scope="module", params=["plain", "separate", "separate-named-git", "worktree-in-project"]
)
def metadata(request, tmp_path_factory):
    root = tmp_path_factory.mktemp("metadata") / "project"
    folder = _repository(root, request.param)
    before = _snapshot(folder)
    shutil.copy(FIXTURES / "git_probe.ps1", root / "git_probe.ps1")
    _contained(root, f"& .\\git_probe.ps1 -Meta '{folder.relative_to(root)}' -RootPath '{root}'")
    after = _snapshot(folder) if folder.is_dir() else {}
    streams = subprocess.run(
        ["cmd", "/c", "dir", "/r", str(folder)], capture_output=True, text=True
    ).stdout
    return SimpleNamespace(
        results=_results(root),
        changed=sorted(
            name for name in before.keys() | after.keys() if before.get(name) != after.get(name)
        ),
        streams=[line for line in streams.splitlines() if ":$DATA" in line],
        root=root,
        folder=folder,
        nested_hook=(root / "nested" / ".git" / "hooks" / "pre-commit").is_file(),
    )


# ---- no route to a remote ----------------------------------------------------


def test_tcp_is_refused_to_loopback_the_lan_address_and_outside(network):
    for name, (status, detail) in network.results.items():
        if name.startswith("host_tcp") or name == "external_tcp":
            assert status == "ERR" and DENIED in detail, (name, detail)
    assert not [event for event in network.arrived if event[0].startswith("tcp")]


def test_udp_never_arrives(network):
    # The send call returns normally; the host receivers, which the host itself
    # reached a moment earlier, get nothing.
    assert not [event for event in network.arrived if event[0].startswith("udp")]


def test_public_names_do_not_resolve(network):
    assert network.results["dns_public_name"][0] == "ERR"


def test_raw_icmp_gets_no_reply(network):
    assert network.results["raw_icmp_loopback"] != ("OK", "REPLY")


def test_nothing_connects_in_and_the_command_cannot_reach_its_own_listener(network):
    for name in ("listen_loopback4", "listen_loopback6"):
        assert network.results[name] == ("OK", "no connection arrived")
    assert network.inbound == {"4": "refused", "6": "refused"}
    assert network.results["own_loopback"][0] == "ERR"


def test_network_shares_are_refused(network):
    for name in ("unc_loopback_share", "unc_webdav_loopback"):
        assert network.results[name][0] == "ERR"
    assert not network.arrived


def test_host_pipes_are_closed_unless_they_admit_every_app_container(network):
    for kind in ("default", "everyone", "everyone_low_integrity"):
        assert network.results[f"pipe_{kind}"][0] == "ERR"
        assert not [event for event in network.pipe_clients if event[0] == kind]


def test_host_brokers_do_not_act_for_the_command(host):
    for name in ("wmi_process_create", "scheduled_task", "shell_windows_com", "bits_transfer"):
        assert host.results[name][0] == "ERR", (name, host.results[name])
    for name in ("winhttp_com", "web_request"):
        assert host.results[name][0] == "ERR"
    assert not host.arrived  # no broker, browser or shell fetched the loopback URL
    assert host.outside == ["canary.txt"]  # nothing ran outside the sandbox and wrote there
    for name in (
        "host_process_write_memory",
        "host_process_duplicate_handles",
        "host_process_terminate",
    ):
        assert host.results[name][0] == "ERR"


# ---- no credentials ---------------------------------------------------------


def test_the_engines_environment_is_not_passed_on(host):
    assert host.results["environment_secret"][0] == "ERR"
    assert host.secret not in host.results["environment_names"][1]


def test_credential_manager_and_outside_files_are_refused(host):
    assert host.results["credential_manager"][0] == "ERR"
    assert host.results["outside_file_read"][0] == "ERR"


# ---- nothing staged for later -------------------------------------------------


def test_nothing_is_left_behind_outside_the_project(host):
    assert host.results["project_write"] == ("OK", "WRITTEN")  # the probe did run
    assert host.left_behind == []
    for name, (status, _) in host.results.items():
        if name.startswith(("outside_write:", "registry_run_key", "registry_saved_variable_write")):
            assert status == "ERR", name


def test_repository_metadata_cannot_be_changed(metadata):
    assert metadata.results["project_file"] == ("OK", "WROTE")  # the probe did run
    assert metadata.changed == []
    assert metadata.streams == []
    assert metadata.root.is_dir() and metadata.folder.is_dir()
    went_through = {name for name, (status, _) in metadata.results.items() if status == "OK"}
    # A repository below the project root is the one thing still writable; the
    # record test ties that to REACHABLE_WHEN_CONTAINED.
    assert went_through - {"nested_repository"} == {"project_file"}
    assert len(metadata.results) > 35


def test_git_read_job_cannot_write_anything(tmp_path):
    from alysis_code.tools.shell import shell_run

    root = tmp_path / "project"
    folder = _repository(root, "plain")
    before = _snapshot(folder)
    result = shell_run(
        root=root, cmd="git log --output=planted.txt -1", runner=_runner(root), timeout_s=120
    )
    assert result["execution_backend"] == "contained-native-git-readonly"
    assert not (root / "planted.txt").exists()
    assert _snapshot(folder) == before


def test_approved_git_still_writes_a_repository_kept_in_another_folder(tmp_path):
    from alysis_code.tools.shell import shell_run

    root = tmp_path / "project"
    _repository(root, "separate")
    (root / "b.txt").write_text("b\n")
    runner = _runner(root)
    for command in ("git add b.txt", "git commit -m second"):
        result = shell_run(root=root, cmd=command, runner=runner, timeout_s=120)
        assert result["exit_code"] == 0, result
        assert result["execution_backend"] == "contained-native-git"


# ---- the record ---------------------------------------------------------------


def test_record_of_what_stays_reachable_matches_the_sandbox(network, host, metadata):
    from alysis_code.windows_mxc_containment import (
        PROBED_SECURITY_REVISION,
        REACHABLE_WHEN_CONTAINED,
    )
    from alysis_code.windows_mxc_contract import helper_contract

    assert helper_contract()["security_revision"] == PROBED_SECURITY_REVISION
    observed = {
        "host pipes that admit every app container": (
            network.results["pipe_every_app_container"][0] == "OK"
            and ("every_app_container", "sandbox") in network.pipe_clients
        ),
        "the user's saved environment variables in the registry": (
            host.results["registry_saved_variable"] == ("OK", host.secret)
        ),
        "the user's DPAPI key": host.results["dpapi_user_key"] == ("OK", host.secret),
        "repository metadata below the project root": (
            metadata.results["nested_repository"][0] == "OK" and metadata.nested_hook
        ),
    }
    assert {name for name, reached in observed.items() if reached} == set(REACHABLE_WHEN_CONTAINED)
