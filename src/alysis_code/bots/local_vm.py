"""Docker-free macOS computers using a verified, bundled Linux VM image.

This is a controller backend, never a model tool. Only image/runtime files are
cloned; host directories and credentials are never mounted in a task VM.
"""

from __future__ import annotations

import base64
import hashlib
import json
import platform
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from ..cancellation import CooperativeCancellationError
from .compute import (
    CommandReceipt,
    ComputeCreationRefused,
    ComputeError,
    ComputerIdentity,
    ComputerRef,
    ComputerSpec,
    ComputerStatus,
    ComputeStopped,
    transfer_chunks,
    validate_transfer,
)
from .computer import ComputerLocks
from .local_resources import DiskGuarded, disk_required, storage_refusal, sweep_leftover_computers
from .vm_guest import MAX_WIRE, PORT, read_exact


class LocalVmComputers(DiskGuarded):
    # A computer is its folder here: no folder means nothing exists (cloud_desktop relies on it).
    find_is_complete = True

    name = "apple-virtualization"

    def __init__(self, bundle: Path, root: Path):
        if platform.system() != "Darwin":
            raise ComputeError("This local computer backend requires macOS.")
        self.bundle = bundle.resolve(strict=True)
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        if manifest.get("schema") != 1 or manifest.get("architecture") != platform.machine():
            raise ComputeError("The local computer image does not match this Mac.")
        self.helper = self._verified(manifest["helper"])
        self.image = self._verified(manifest["image"])
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        sweep_leftover_computers(self.root)
        self.scope = str(self.root)
        self._processes = {}
        self._lock = threading.RLock()
        self._operations = ComputerLocks()
        self._starting = set()

    def _verified(self, item):
        name, digest = item["name"], item["sha256"]
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise ComputeError("Invalid local computer image manifest.")
        path = self.bundle / name
        if path.is_symlink() or not path.is_file():
            raise ComputeError("The local computer image is missing or aliased.")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise ComputeError("The local computer image failed verification.")
        return path

    def _directory(self, identity):
        if not isinstance(identity, ComputerIdentity):
            raise ComputeError("A controller-owned computer identity is required.")
        return self.root / identity.allocation_id

    def _owned(self, ref):
        if (
            ref.provider != self.name
            or ref.scope != self.scope
            or ref.resource_id != ref.identity.allocation_id
        ):
            raise ComputeError("The local computer belongs to another controller.")
        directory = self._directory(ref.identity)
        if directory.is_symlink() or directory.parent != self.root:
            raise ComputeError("Invalid local computer directory.")
        if directory.exists():
            record = json.loads((directory / "owner.json").read_text())
            if record != ref.identity.labels:
                raise ComputeError("Local computer ownership does not match.")
        return directory

    def create(self, identity, spec):
        if not isinstance(spec, ComputerSpec):
            raise ComputeError("A controller-owned computer specification is required.")
        directory = self._directory(identity)
        with self._operations.hold(identity.allocation_id):
            with self._lock:
                running = sum(
                    process.poll() is None
                    for key, process in self._processes.items()
                    if key not in self._starting
                ) + len(self._starting)
                required = disk_required(running)
                free = shutil.disk_usage(self.root).free
                if free < required:
                    raise ComputeCreationRefused(
                        storage_refusal(required, free),
                        code="insufficient_local_storage",
                    )
                self._starting.add(identity.allocation_id)
            try:
                directory.mkdir(mode=0o700)
                (directory / "owner.json").write_text(json.dumps(identity.labels))
                try:
                    # APFS clonefile preserves the verified base and uses space only
                    # for changed blocks. No shell interpolation or shared host disk.
                    subprocess.run(
                        ["/bin/cp", "-c", str(self.image), str(directory / "disk.raw")],
                        check=True,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    (directory / "disk.raw").chmod(0o600)
                    ref = ComputerRef(self.name, self.scope, identity.allocation_id, identity)
                    self._boot(ref, spec)
                    self._watch_disk()
                    return ref
                except Exception:
                    self._stop_directory(directory)
                    raise ComputeError(
                        "The local computer could not start; its owned disk was retained for cleanup."
                    ) from None

            finally:
                with self._lock:
                    self._starting.discard(identity.allocation_id)

    def _boot(self, ref, spec):
        directory = self._owned(ref)
        # AF_UNIX paths have a small platform limit. This private directory is
        # short and unpredictable; no global reusable socket or TCP endpoint.
        import tempfile

        transport = Path(tempfile.mkdtemp(prefix="alysis-vm-"))
        transport.chmod(0o700)
        (directory / "transport.json").write_text(json.dumps({"directory": str(transport)}))
        argv = [
            str(self.helper),
            "--cpus",
            str(spec.cpu),
            "--memory",
            str(spec.memory_mib),
            "--bootloader",
            f"efi,variable-store={directory / 'nvram'},create",
            "--device",
            f"virtio-blk,path={directory / 'disk.raw'}",
            "--device",
            "virtio-rng",
            "--device",
            f"virtio-vsock,port={PORT},socketURL={transport / 'computer.sock'},connect",
            "--log-level",
            "error",
        ]
        if spec.internet:
            argv.extend(["--device", "virtio-net,nat"])
        child = subprocess.Popen(
            [sys.executable, "-I", str(Path(__file__).with_name("vm_runner.py"))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            bufsize=0,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(Path.home()),
                "LANG": "C.UTF-8",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        )
        with self._lock:
            self._processes[ref.resource_id] = child
        child.stdin.write(json.dumps({"argv": argv}).encode() + b"\n")
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and child.poll() is None:
            try:
                if self._rpc(ref, {"operation": "ping"}, timeout=2).get("ready") is True:
                    return
            except (OSError, ComputeError):
                pass
            time.sleep(0.25)
        self._stop_directory(directory)
        raise ComputeError("The local computer did not become ready.")

    def _socket(self, directory):
        value = json.loads((directory / "transport.json").read_text())
        transport = Path(value["directory"])
        if transport.parent != Path("/tmp") and transport.parent != Path("/private/tmp"):
            # macOS tempfile uses its per-user private temp directory.
            import tempfile

            if transport.parent.resolve() != Path(tempfile.gettempdir()).resolve():
                raise ComputeError("Invalid local computer transport.")
        if not transport.name.startswith("alysis-vm-") or transport.is_symlink():
            raise ComputeError("Invalid local computer transport.")
        return transport / "computer.sock"

    def _running_ids(self):
        return [key for key, process in self._processes.items() if process.poll() is None]

    def _rpc(self, ref, payload, *, timeout=65):
        directory = self._owned(ref)
        reason = self._space_stop(ref.resource_id)
        if reason and ref.resource_id not in self._processes:
            raise ComputeStopped(reason)
        body = json.dumps(payload).encode()
        if len(body) > MAX_WIRE:
            raise ComputeError("Local computer request exceeds its allowance.")
        try:
            with socket.socket(socket.AF_UNIX) as connection:
                connection.settimeout(timeout)
                connection.connect(str(self._socket(directory)))
                connection.sendall(len(body).to_bytes(4, "big") + body)
                size = int.from_bytes(read_exact(connection, 4), "big")
                if not 0 < size <= MAX_WIRE:
                    raise ComputeError("Invalid local computer reply.")
                result = json.loads(read_exact(connection, size))
            if result.get("ok") is not True:
                raise ComputeError(
                    "Local computer operation failed; do not replay an uncertain action."
                )
            return result["result"]
        except (OSError, ValueError, KeyError):
            raise ComputeError(
                "Local computer connection closed; reconcile before retrying."
            ) from None

    def status(self, ref):
        directory = self._owned(ref)
        if not directory.exists():
            return ComputerStatus("missing", "disk removed")
        child = self._processes.get(ref.resource_id)
        if child and child.poll() is None:
            return ComputerStatus("running", "local VM process")
        return ComputerStatus("stopped", self._space_stop(ref.resource_id) or "local VM process")

    def start(self, ref):
        raise ComputeError("Task computers are recreated from saved workspace checkpoints.")

    def _stop_directory(self, directory):
        with self._lock:
            child = self._processes.pop(directory.name, None)
        if child:
            child.stdin.close()
            try:
                child.wait(timeout=12)
            except subprocess.TimeoutExpired:
                child.terminate()
                child.wait(timeout=8)
            child.stdout.close()
        if (directory / "transport.json").exists():
            socket_path = self._socket(directory)
            if socket_path.parent.exists():
                shutil.rmtree(socket_path.parent)

    def stop(self, ref):
        directory = self._owned(ref)
        with self._operations.hold(ref.resource_id):
            self._stop_directory(directory)
        return self.status(ref)

    def delete(self, ref):
        directory = self._owned(ref)
        with self._operations.hold(ref.resource_id):
            self._stop_directory(directory)
            if directory.exists():
                shutil.rmtree(directory)
            self._space_stops().pop(ref.resource_id, None)
        return ComputerStatus("missing", "owned disk removed")

    def find(self, identity):
        directory = self._directory(identity)
        if not directory.exists():
            return None
        ref = ComputerRef(self.name, self.scope, identity.allocation_id, identity)
        self._owned(ref)
        return ref

    def run(self, ref, argv, *, timeout=30):
        if not 1 <= timeout <= 60:
            raise ValueError("Use a bounded command timeout.")
        value = self._rpc(
            ref, {"operation": "run", "argv": argv, "timeout": timeout}, timeout=timeout + 5
        )
        return CommandReceipt(**value)

    def upload(self, ref, guest_path, chunks, *, size, cancel=lambda: False):
        validate_transfer(guest_path, size)

        def check():
            if cancel():
                raise CooperativeCancellationError("Local computer transfer cancelled.")

        body = b"".join(transfer_chunks(chunks, size, check))
        check()
        self._rpc(
            ref,
            {
                "operation": "upload",
                "path": guest_path,
                "size": size,
                "data": base64.b64encode(body).decode(),
            },
        )

    def download(self, ref, guest_path, *, size, cancel=lambda: False):
        validate_transfer(guest_path, size)
        if cancel():
            raise CooperativeCancellationError("Local computer transfer cancelled.")
        value = self._rpc(ref, {"operation": "download", "path": guest_path, "size": size})
        body = base64.b64decode(value["data"], validate=True)
        if len(body) != size:
            raise ComputeError("Local computer transfer size changed.")
        for offset in range(0, len(body), 65536):
            if cancel():
                raise CooperativeCancellationError("Local computer transfer cancelled.")
            yield body[offset : offset + 65536]

    def close(self):
        with self._lock:
            identities = list(self._processes)
        for identity in identities:
            with self._operations.hold(identity):
                self._stop_directory(self.root / identity)
        self._close_disk_guard()
