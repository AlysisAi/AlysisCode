"""Windows private computers (local_hcs): lifecycle, ownership and the service pipe contract.

Windows calls are replaced with fakes, so these run on every platform; the real path is exercised
by the qualification run on a Windows PC with the service installed.
"""

from __future__ import annotations

import io
import json
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from alysis_code.bots import local_hcs
from alysis_code.bots.compute import (
    ComputeCreationRefused,
    ComputeError,
    ComputerIdentity,
    ComputerSpec,
)
from alysis_code.bots.computer import ComputerLocks


class FakeSession:
    def __init__(self):
        self.calls = []
        self.generation = 1
        self.states = {}
        self.fail = None

    def call(self, payload):
        self.calls.append(payload)
        if self.fail:
            raise self.fail
        op = payload["op"]
        if op == "create":
            self.states[payload["vm_id"]] = "running"
            return {
                "vm_id": payload["vm_id"],
                "runtime_id": payload["vm_id"],
                "address": "10.211.200.5",
            }
        if op == "state":
            return {"state": self.states.get(payload["vm_id"], "missing")}
        if op == "stop":
            if payload["vm_id"] not in self.states:
                raise KeyError(payload["vm_id"])
            self.states[payload["vm_id"]] = "stopped"
            return {"state": "stopped"}
        raise AssertionError(op)

    def close(self):
        self.closed = True


def provider(tmp_path, monkeypatch, session=None):
    value = object.__new__(local_hcs.WindowsVmComputers)
    image = tmp_path / "bundle" / "disk.vhdx"
    image.parent.mkdir()
    image.write_bytes(b"base")
    value.image = image
    value.kernel = tmp_path / "bundle" / "vmlinuz"
    value.initrd = tmp_path / "bundle" / "initrd.img"
    value.root_partition = "PARTUUID=138f3e8b-bdeb-47ac-84b8-f9df1662abc8"
    value.root = (tmp_path / "computers").resolve()
    value.root.mkdir()
    value.scope = str(value.root)
    value._session = session or FakeSession()
    value._running = {}
    value._lock = threading.RLock()
    value._operations = ComputerLocks()
    value._starting = set()
    granted = []
    monkeypatch.setattr(
        local_hcs, "_differencing_disk", lambda path, parent: path.write_bytes(b"child")
    )
    monkeypatch.setattr(local_hcs, "_grant_vm", lambda vm_id, path: granted.append((vm_id, path)))
    monkeypatch.setattr(
        local_hcs.shutil, "disk_usage", lambda _: SimpleNamespace(free=100 * 1024**3)
    )
    value.granted = granted
    return value


def guest(monkeypatch, replies=None, calls=None):
    def call(runtime_id, payload, timeout):
        if calls is not None:
            calls.append((runtime_id, payload))
        if payload["operation"] == "ping":
            return {"ok": True, "result": {"protocol": 1, "ready": True}}
        return {"ok": True, "result": (replies or {}).get(payload["operation"], {})}

    monkeypatch.setattr(local_hcs, "_guest_call", call)


def identity():
    return ComputerIdentity("local", "bot", uuid.uuid4().hex)


SPEC = ComputerSpec("bundled-linux", cpu=2, memory_mib=2048, internet=True)


def test_create_boots_a_private_child_disk_and_talks_to_the_guest(tmp_path, monkeypatch):
    computers = provider(tmp_path, monkeypatch)
    calls = []
    guest(monkeypatch, {"run": {"exit_code": 0, "stdout": "ok\n", "stderr": ""}}, calls)
    ref = computers.create(identity(), SPEC)
    directory = computers.root / ref.resource_id
    vm_id = json.loads((directory / "vm.json").read_text())["vm_id"]
    create = computers._session.calls[0]
    assert create == {
        "op": "create",
        "vm_id": vm_id,
        "kernel": str(computers.kernel),
        "initrd": str(computers.initrd),
        "root": computers.root_partition,
        "disks": [{"path": str(directory / "disk.vhdx")}],
        "memory_mib": 2048,
        "cpus": 2,
        "internet": True,
    }
    # Only this VM is granted its own disk; the shared image is never handed out per VM.
    assert computers.granted == [(vm_id, directory / "disk.vhdx")]
    assert computers.status(ref).state == "running"
    receipt = computers.run(ref, ["/usr/bin/true"], timeout=5)
    assert receipt.exit_code == 0 and receipt.stdout == "ok\n"
    assert calls[-1] == (vm_id, {"operation": "run", "argv": ["/usr/bin/true"], "timeout": 5})


def test_stop_keeps_the_disk_and_delete_removes_it(tmp_path, monkeypatch):
    computers = provider(tmp_path, monkeypatch)
    guest(monkeypatch)
    ref = computers.create(identity(), SPEC)
    assert computers.stop(ref).state == "stopped"
    assert (computers.root / ref.resource_id / "disk.vhdx").exists()
    assert computers.find(ref.identity) == ref
    assert computers.delete(ref).state == "missing"
    assert not (computers.root / ref.resource_id).exists()
    assert computers.find(ref.identity) is None


def test_a_lost_service_session_means_the_computer_stopped(tmp_path, monkeypatch):
    computers = provider(tmp_path, monkeypatch)
    guest(monkeypatch)
    ref = computers.create(identity(), SPEC)
    computers._session.generation += 1  # the pipe was reconnected: the old session's VMs ended
    assert computers.status(ref).state == "stopped"
    with pytest.raises(ComputeError, match="not running"):
        computers.run(ref, ["/usr/bin/true"])


def test_disk_refusal_precedes_all_mutations(tmp_path, monkeypatch):
    computers = provider(tmp_path, monkeypatch)
    monkeypatch.setattr(local_hcs.shutil, "disk_usage", lambda _: SimpleNamespace(free=1))
    with pytest.raises(ComputeCreationRefused) as refused:
        computers.create(identity(), SPEC)
    assert refused.value.code == "insufficient_local_storage"
    assert computers._session.calls == []
    assert list(computers.root.iterdir()) == []


def test_missing_service_is_a_clear_refusal_not_a_generic_failure(tmp_path, monkeypatch):
    session = FakeSession()
    session.fail = ComputeCreationRefused("set up", code="windows_computer_service_missing")
    computers = provider(tmp_path, monkeypatch, session)
    guest(monkeypatch)
    with pytest.raises(ComputeCreationRefused) as refused:
        computers.create(identity(), SPEC)
    assert refused.value.code == "windows_computer_service_missing"


def test_ownership_is_checked(tmp_path, monkeypatch):
    computers = provider(tmp_path, monkeypatch)
    guest(monkeypatch)
    ref = computers.create(identity(), SPEC)
    other = ComputerIdentity("local", "other-bot", ref.identity.allocation_id)
    with pytest.raises(ComputeError):
        computers.find(other)
    foreign = local_hcs.ComputerRef(
        "apple-virtualization", computers.scope, ref.resource_id, ref.identity
    )
    with pytest.raises(ComputeError):
        computers.status(foreign)


class FakePipe(io.RawIOBase):
    """The service end: answers frames from a script."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.pending = b""

    def readable(self):
        return True

    def writable(self):
        return True

    def write(self, data):
        size = int.from_bytes(data[:4], "big")
        self.sent.append(json.loads(data[4 : 4 + size]))
        reply = json.dumps(self.replies.pop(0)).encode() if self.replies else b""
        self.pending += (len(reply).to_bytes(4, "big") + reply) if reply else b""
        return len(data)

    def read(self, count=-1):
        part, self.pending = self.pending[:count], self.pending[count:]
        return part


def test_session_says_hello_then_maps_service_errors():
    pipe = FakePipe(
        [
            {"ok": True, "result": {"protocol": 1, "version": "0.1.0"}},
            {"ok": True, "result": {"state": "running"}},
            {"ok": False, "error": {"code": "not_found", "message": "No such computer."}},
            {"ok": False, "error": {"code": "limit", "message": "At most 8."}},
        ]
    )
    session = local_hcs._ServiceSession(opener=lambda: pipe)
    vm = str(uuid.uuid4())
    assert session.call({"op": "state", "vm_id": vm}) == {"state": "running"}
    assert pipe.sent[0] == {"op": "hello"} and session.generation == 1
    with pytest.raises(KeyError):
        session.call({"op": "stop", "vm_id": vm})
    with pytest.raises(ComputeCreationRefused) as refused:
        session.call({"op": "create"})
    assert refused.value.code == "local_computer_limit"


def test_session_refuses_an_outdated_service_and_reconnects_after_loss():
    old = FakePipe([{"ok": True, "result": {"protocol": 0}}])
    with pytest.raises(ComputeCreationRefused) as refused:
        local_hcs._ServiceSession(opener=lambda: old).call({"op": "list"})
    assert refused.value.code == "windows_computer_service_outdated"

    pipes = [
        FakePipe([{"ok": True, "result": {"protocol": 1}}]),  # dies after hello
        FakePipe(
            [{"ok": True, "result": {"protocol": 1}}, {"ok": True, "result": {"computers": []}}]
        ),
    ]
    session = local_hcs._ServiceSession(opener=lambda: pipes.pop(0))
    with pytest.raises(ComputeError, match="reconcile"):
        session.call({"op": "list"})
    assert session.call({"op": "list"}) == {"computers": []}
    assert session.generation == 2


def test_guest_service_id_matches_the_vsock_port():
    assert local_hcs.GUEST_SERVICE == "00002a25-facb-11e6-bd58-64006a7986d3"
    assert local_hcs.ROOT_PARTITION.fullmatch("PARTUUID=138f3e8b-bdeb-47ac-84b8-f9df1662abc8")
    assert not local_hcs.ROOT_PARTITION.fullmatch("PARTUUID=x init=/bin/sh")


def test_provider_refuses_other_platforms(tmp_path, monkeypatch):
    monkeypatch.setattr(local_hcs.sys, "platform", "darwin")
    with pytest.raises(ComputeError, match="requires Windows"):
        local_hcs.WindowsVmComputers(Path(tmp_path), Path(tmp_path))


@pytest.mark.skipif(local_hcs.sys.platform != "win32", reason="Windows memory query")
def test_device_budget_reads_windows_memory():
    # Local Owls size their computers from this; os.sysconf does not exist on Windows.
    from alysis_code.bots.local_resources import LocalResources, host_resources

    cores, memory_mib = host_resources()
    assert cores >= 1 and memory_mib >= 1024
    assert LocalResources.parse(None).memory_mib > 0


def test_open_pipe_starts_the_idle_service_once_and_checks_the_server(monkeypatch):
    stream = io.BytesIO()
    attempts = [(None, 2), (None, 2), (None, 231), (stream, 0)]
    starts, waits = [], []
    monkeypatch.setattr(local_hcs, "_connect_pipe", lambda: attempts.pop(0))
    monkeypatch.setattr(local_hcs, "_start_service", lambda: starts.append(True))
    monkeypatch.setattr(local_hcs, "_wait_for_pipe", lambda ms: waits.append(ms))
    monkeypatch.setattr(local_hcs.time, "sleep", lambda _: None)
    monkeypatch.setattr(local_hcs, "_pipe_server_process_id", lambda _: 4242)
    monkeypatch.setattr(local_hcs, "_service_process_id", lambda: 4242)
    assert local_hcs._open_pipe() is stream
    assert starts == [True] and waits == [2000]


def test_open_pipe_refuses_an_impostor_and_unexpected_errors(monkeypatch):
    monkeypatch.setattr(local_hcs.time, "sleep", lambda _: None)
    monkeypatch.setattr(local_hcs, "_connect_pipe", lambda: (io.BytesIO(), 0))
    monkeypatch.setattr(local_hcs, "_pipe_server_process_id", lambda _: 1111)
    monkeypatch.setattr(local_hcs, "_service_process_id", lambda: 2222)
    with pytest.raises(ComputeError, match="not served by the Alysis service"):
        local_hcs._open_pipe()
    monkeypatch.setattr(local_hcs, "_connect_pipe", lambda: (None, 5))
    with pytest.raises(ComputeError, match="could not be reached"):
        local_hcs._open_pipe()


def test_virtualization_off_is_explained_not_generic():
    pipe = FakePipe(
        [
            {"ok": True, "result": {"protocol": 1, "version": "0.1.0"}},
            {"ok": False, "error": {"code": "no_hypervisor", "message": "0x80370102"}},
        ]
    )
    session = local_hcs._ServiceSession(opener=lambda: pipe)
    with pytest.raises(ComputeCreationRefused) as refused:
        session.call({"op": "state", "vm_id": str(uuid.uuid4())})
    assert refused.value.code == "virtualization_off"
    assert "BIOS or UEFI" in str(refused.value)
