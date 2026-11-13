"""Docker-free Windows computers using a verified, downloaded Linux VM image.

The Windows twin of ``local_vm.py`` (macOS). This is a controller backend, never a model tool.
Each computer boots the verified base disk through its own private copy-on-write child disk;
host directories and credentials are never attached to a task VM.

Windows reserves creating VMs for administrators, so the small "Alysis Owls computer service"
(``alysis-owls-computer.exe`` in the app repo, installed once with an "Allow" prompt) creates and
stops VMs on request over a local pipe. Everything else stays in this process, as the user:
the child disks, granting each VM access to its own files, and the guest connection (a Hyper-V
socket to the same ``vm_guest.py`` the Mac image runs). Computers are tied to this process's
pipe session, so they end when it ends.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import socket
import sys
import threading
import time
import uuid
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

PIPE = r"\\.\pipe\alysis-owls-computer"
SERVICE_NAME = "AlysisOwlsComputer"
PROTOCOL = 1
MAX_FRAME = 64 * 1024
# Hyper-V socket service id that Linux sees as vsock port PORT (Microsoft's documented template).
GUEST_SERVICE = f"{PORT:08x}-facb-11e6-bd58-64006a7986d3"
ROOT_PARTITION = re.compile(
    r"PARTUUID=[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
)

_MICROSOFT_VENDOR = uuid.UUID("ec984aec-a0f9-47e9-901f-71415a66345b")
_VHDX = 3
_ERROR_FILE_NOT_FOUND = 2
_ERROR_PIPE_BUSY = 231
_ERROR_SERVICE_DOES_NOT_EXIST = 1060
_ERROR_SERVICE_ALREADY_RUNNING = 1056


# --- Windows boundary (ctypes). Small module-level functions so tests can replace them. -------


class _Guid(ctypes.Structure):
    _fields_ = [("raw", ctypes.c_ubyte * 16)]


class _StorageType(ctypes.Structure):
    _fields_ = [("device_id", ctypes.c_ulong), ("vendor_id", _Guid)]


class _CreateV2(ctypes.Structure):
    _fields_ = [
        ("unique_id", _Guid),
        ("maximum_size", ctypes.c_ulonglong),
        ("block_size", ctypes.c_ulong),
        ("sector_size", ctypes.c_ulong),
        ("physical_sector_size", ctypes.c_ulong),
        ("parent_path", ctypes.c_wchar_p),
        ("source_path", ctypes.c_wchar_p),
        ("open_flags", ctypes.c_int),
        ("parent_type", _StorageType),
        ("source_type", _StorageType),
        ("resiliency_guid", _Guid),
    ]


class _CreateParameters(ctypes.Structure):
    # CREATE_VIRTUAL_DISK_PARAMETERS is a versioned union; the spare bytes cover later versions.
    _fields_ = [("version", ctypes.c_int), ("version2", _CreateV2), ("_spare", ctypes.c_ubyte * 64)]


def _dll(name):
    return ctypes.WinDLL(name, use_last_error=True)


def _differencing_disk(path: Path, parent: Path):
    """Create ``path`` as a copy-on-write child of ``parent`` (both VHDX). No admin needed."""
    virtdisk = _dll("virtdisk")
    storage = _StorageType(_VHDX)
    ctypes.memmove(storage.vendor_id.raw, _MICROSOFT_VENDOR.bytes_le, 16)
    parameters = _CreateParameters()
    parameters.version = 2
    parameters.version2.parent_path = str(parent)
    handle = ctypes.c_void_p()
    error = virtdisk.CreateVirtualDisk(
        ctypes.byref(storage),
        ctypes.c_wchar_p(str(path)),
        0,
        None,
        0,
        0,
        ctypes.byref(parameters),
        None,
        ctypes.byref(handle),
    )
    if handle.value:
        _dll("kernel32").CloseHandle(handle)
    if error:
        raise ComputeError(f"Windows could not create the computer's disk ({error}).")


def _grant_vm(vm_id: str, path: Path):
    """Let exactly one VM open ``path`` (an ACL entry for that VM's own account)."""
    result = _dll("computecore").HcsGrantVmAccess(
        ctypes.c_wchar_p(vm_id), ctypes.c_wchar_p(str(path))
    )
    if result < 0:
        raise ComputeError(
            f"Windows could not let the computer use its disk (0x{result & 0xFFFFFFFF:08x})."
        )


def _grant_all_vms(path: Path):
    """Let VMs read a shared, never-written file (base disk, kernel, initrd)."""
    result = _dll("computecore").HcsGrantVmGroupAccess(ctypes.c_wchar_p(str(path)))
    if result < 0:
        raise ComputeError(
            f"Windows could not share the computer image (0x{result & 0xFFFFFFFF:08x})."
        )


def _service_handle(access):
    advapi32 = _dll("advapi32")
    advapi32.OpenSCManagerW.restype = ctypes.c_void_p
    advapi32.OpenServiceW.restype = ctypes.c_void_p
    advapi32.OpenServiceW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_ulong]
    advapi32.CloseServiceHandle.argtypes = [ctypes.c_void_p]
    manager = advapi32.OpenSCManagerW(None, None, 0x0001)  # SC_MANAGER_CONNECT
    if not manager:
        raise ComputeError("Windows services are unavailable.")
    try:
        service = advapi32.OpenServiceW(manager, SERVICE_NAME, access)
        if not service:
            if ctypes.get_last_error() == _ERROR_SERVICE_DOES_NOT_EXIST:
                raise ComputeCreationRefused(
                    "This PC's private computer service is not set up yet. Set it up in Settings > Owls.",
                    code="windows_computer_service_missing",
                )
            raise ComputeError("The private computer service could not be opened.")
        return advapi32, service
    finally:
        advapi32.CloseServiceHandle(manager)


def _start_service():
    advapi32, service = _service_handle(0x0010 | 0x0004)  # SERVICE_START | SERVICE_QUERY_STATUS
    try:
        advapi32.StartServiceW.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p]
        if not advapi32.StartServiceW(service, 0, None):
            if ctypes.get_last_error() != _ERROR_SERVICE_ALREADY_RUNNING:
                raise ComputeError("The private computer service could not start.")
    finally:
        advapi32.CloseServiceHandle(service)


def _service_process_id() -> int:
    advapi32, service = _service_handle(0x0004)  # SERVICE_QUERY_STATUS
    try:
        buffer = (ctypes.c_ulong * 9)()
        needed = ctypes.c_ulong()
        advapi32.QueryServiceStatusEx.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        if not advapi32.QueryServiceStatusEx(
            service, 0, buffer, ctypes.sizeof(buffer), ctypes.byref(needed)
        ):
            return 0
        return buffer[7]  # SERVICE_STATUS_PROCESS.dwProcessId
    finally:
        advapi32.CloseServiceHandle(service)


def _pipe_server_process_id(stream) -> int:
    import msvcrt

    kernel32 = _dll("kernel32")
    kernel32.GetNamedPipeServerProcessId.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
    ]
    process = ctypes.c_ulong()
    if not kernel32.GetNamedPipeServerProcessId(
        msvcrt.get_osfhandle(stream.fileno()), ctypes.byref(process)
    ):
        return 0
    return process.value


def _connect_pipe():
    """Open the service pipe as a client: (stream, 0) or (None, Windows error code).

    CreateFileW reports the exact Windows error (2 = no pipe: the service is not running,
    231 = all instances busy); Python's open() does not.
    """
    import msvcrt

    kernel32 = _dll("kernel32")
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    handle = kernel32.CreateFileW(
        PIPE, 0xC0000000, 0, None, 3, 0, None
    )  # read/write, OPEN_EXISTING
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return None, ctypes.get_last_error()
    descriptor = msvcrt.open_osfhandle(handle, os.O_RDWR | os.O_BINARY)
    return os.fdopen(descriptor, "r+b", buffering=0), 0


def _wait_for_pipe(milliseconds):
    kernel32 = _dll("kernel32")
    kernel32.WaitNamedPipeW.argtypes = [ctypes.c_wchar_p, ctypes.c_ulong]
    kernel32.WaitNamedPipeW(PIPE, milliseconds)


def _open_pipe():
    """Open the service pipe, starting the service on demand. Refuses an impostor pipe."""
    deadline = time.monotonic() + 30
    started = False
    while True:
        stream, code = _connect_pipe()
        if stream is not None:
            break
        if code == _ERROR_FILE_NOT_FOUND:
            # The service stops itself when idle; signed-in users may start it again.
            if not started:
                _start_service()
                started = True
        elif code == _ERROR_PIPE_BUSY:
            _wait_for_pipe(2000)
        else:
            raise ComputeError("The private computer service could not be reached.")
        if time.monotonic() > deadline:
            raise ComputeError("The private computer service did not answer.")
        time.sleep(0.2)
    server = _pipe_server_process_id(stream)
    if not server or server != _service_process_id():
        stream.close()
        raise ComputeError("The private computer pipe is not served by the Alysis service.")
    return stream


def _guest_call(runtime_id: str, payload: dict, timeout: float) -> dict:
    """One request to vm_guest.py inside the VM, over a Hyper-V socket (no network involved)."""
    body = json.dumps(payload).encode()
    with socket.socket(socket.AF_HYPERV, socket.SOCK_STREAM, socket.HV_PROTOCOL_RAW) as connection:
        connection.settimeout(timeout)
        connection.connect((runtime_id, GUEST_SERVICE))
        connection.sendall(len(body).to_bytes(4, "big") + body)
        size = int.from_bytes(read_exact(connection, 4), "big")
        if not 0 < size <= MAX_WIRE:
            raise ValueError("Invalid local computer reply.")
        return json.loads(read_exact(connection, size))


# --- The service session ---------------------------------------------------------------------


class _ServiceSession:
    """One pipe connection to the service. Computers created on it end when it closes."""

    def __init__(self, opener=_open_pipe):
        self._opener = opener
        self._stream = None
        self._lock = threading.Lock()
        self.generation = 0

    def _read(self, count):
        data = bytearray()
        while len(data) < count:
            part = self._stream.read(count - len(data))
            if not part:
                raise OSError("Service pipe closed.")
            data.extend(part)
        return bytes(data)

    def _exchange(self, payload):
        body = json.dumps(payload).encode()
        self._stream.write(len(body).to_bytes(4, "big") + body)
        size = int.from_bytes(self._read(4), "big")
        if not 0 < size <= MAX_FRAME:
            raise OSError("Invalid service reply.")
        return json.loads(self._read(size))

    def call(self, payload: dict) -> dict:
        with self._lock:
            if self._stream is None:
                self._stream = self._opener()
                self.generation += 1
                try:
                    hello = self._exchange({"op": "hello"})
                except (OSError, ValueError):
                    self._drop()
                    raise ComputeError("The private computer service did not answer.") from None
                if not hello.get("ok") or hello["result"].get("protocol") != PROTOCOL:
                    self._drop()
                    raise ComputeCreationRefused(
                        "This PC's private computer service needs an update. Set it up again in Settings > Owls.",
                        code="windows_computer_service_outdated",
                    )
            try:
                reply = self._exchange(payload)
            except (OSError, ValueError):
                # The service ended every computer of this session; callers reconcile.
                self._drop()
                raise ComputeError(
                    "Local computer connection closed; reconcile before retrying."
                ) from None
        if reply.get("ok") is not True:
            error = reply.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            if code == "not_found":
                raise KeyError(payload.get("vm_id"))
            if code == "limit":
                raise ComputeCreationRefused(
                    "Too many private computers are running on this PC. Finish another task first.",
                    code="local_computer_limit",
                )
            if code == "no_hypervisor":
                # Windows could not run any VM: virtualization is off in the firmware, the usual
                # cause on a PC that never ran WSL 2 or Hyper-V.
                raise ComputeCreationRefused(VIRTUALIZATION_OFF, code="virtualization_off")
            raise ComputeError(
                "Local computer operation failed; do not replay an uncertain action."
            )
        return reply["result"]

    def _drop(self):
        if self._stream is not None:
            try:
                self._stream.close()
            except OSError:
                pass
        self._stream = None

    def close(self):
        with self._lock:
            self._drop()


# --- The provider ------------------------------------------------------------------------------


VIRTUALIZATION_OFF = (
    "Owls can't start a private computer because virtualization is turned off on this PC. Turn on "
    "virtualization (Intel VT-x or AMD-V/SVM) in your PC's BIOS or UEFI settings, then restart "
    "Windows. Until then, owls can work in Alysis Cloud."
)


class WindowsVmComputers(DiskGuarded):
    # A computer is its folder here: no folder means nothing exists (cloud_desktop relies on it).
    find_is_complete = True

    name = "windows-hyperv"

    def __init__(self, bundle: Path, root: Path):
        if sys.platform != "win32":
            raise ComputeError("This local computer backend requires Windows.")
        if not hasattr(socket, "AF_HYPERV"):
            raise ComputeError("Windows computers need Python 3.12 or newer.")
        self.bundle = bundle.resolve(strict=True)
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        if (
            manifest.get("schema") != 1
            or manifest.get("platform") != "windows"
            or manifest.get("architecture") != platform.machine()
        ):
            raise ComputeError("The local computer image does not match this PC.")
        self.image = self._verified(manifest["image"])
        self.kernel = self._verified(manifest["kernel"])
        self.initrd = self._verified(manifest["initrd"])
        if self.image.suffix.lower() != ".vhdx":
            raise ComputeError("Invalid local computer image manifest.")
        root_partition = manifest.get("root")
        if not isinstance(root_partition, str) or not ROOT_PARTITION.fullmatch(root_partition):
            raise ComputeError("Invalid local computer image manifest.")
        self.root_partition = root_partition
        for shared in (self.image, self.kernel, self.initrd):
            _grant_all_vms(shared)
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        sweep_leftover_computers(self.root)
        self.scope = str(self.root)
        self._session = _ServiceSession()
        self._running = {}
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
                running = len(self._running) + len(self._starting)
                required = disk_required(running)
                free = shutil.disk_usage(self.root).free
                if free < required:
                    raise ComputeCreationRefused(
                        storage_refusal(required, free),
                        code="insufficient_local_storage",
                    )
                self._starting.add(identity.allocation_id)
            try:
                directory.mkdir()
                (directory / "owner.json").write_text(json.dumps(identity.labels))
                try:
                    # A copy-on-write child of the verified base: uses space only for changes.
                    _differencing_disk(directory / "disk.vhdx", self.image)
                    ref = ComputerRef(self.name, self.scope, identity.allocation_id, identity)
                    self._boot(ref, spec)
                    self._watch_disk()
                    return ref
                except ComputeCreationRefused:
                    self._stop_directory(directory)
                    raise
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
        disk = directory / "disk.vhdx"
        vm_id = str(uuid.uuid4())
        # The VM's own account may open its own disk; nothing else of the user's.
        _grant_vm(vm_id, disk)
        (directory / "vm.json").write_text(json.dumps({"vm_id": vm_id}))
        result = self._session.call(
            {
                "op": "create",
                "vm_id": vm_id,
                "kernel": str(self.kernel),
                "initrd": str(self.initrd),
                "root": self.root_partition,
                "disks": [{"path": str(disk)}],
                "memory_mib": spec.memory_mib,
                "cpus": spec.cpu,
                "internet": spec.internet,
            }
        )
        with self._lock:
            self._running[ref.resource_id] = {
                "vm_id": vm_id,
                "runtime_id": str(result["runtime_id"]),
                "generation": self._session.generation,
            }
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            try:
                if self._rpc(ref, {"operation": "ping"}, timeout=2).get("ready") is True:
                    return
            except ComputeError:
                if self._record(ref.resource_id) is None:
                    break
            time.sleep(0.25)
        self._stop_directory(directory)
        raise ComputeError("The local computer did not become ready.")

    def _record(self, resource_id):
        with self._lock:
            record = self._running.get(resource_id)
            if record and record["generation"] != self._session.generation:
                # The service session ended, and with it this computer.
                self._running.pop(resource_id, None)
                return None
            return record

    def _running_ids(self):
        return list(self._running)

    def _rpc(self, ref, payload, *, timeout=65):
        self._owned(ref)
        record = self._record(ref.resource_id)
        if record is None:
            reason = self._space_stop(ref.resource_id)
            if reason:
                raise ComputeStopped(reason)
            raise ComputeError("The local computer is not running.")
        if len(json.dumps(payload).encode()) > MAX_WIRE:
            raise ComputeError("Local computer request exceeds its allowance.")
        try:
            result = _guest_call(record["runtime_id"], payload, timeout)
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
        record = self._record(ref.resource_id)
        if record is None:
            return ComputerStatus(
                "stopped", self._space_stop(ref.resource_id) or "local VM service"
            )
        try:
            state = self._session.call({"op": "state", "vm_id": record["vm_id"]})["state"]
        except ComputeError:
            return ComputerStatus("unknown", "local VM service unreachable")
        if state != "running":
            with self._lock:
                self._running.pop(ref.resource_id, None)
            return ComputerStatus("stopped", "local VM service")
        return ComputerStatus("running", "local VM service")

    def start(self, ref):
        raise ComputeError("Task computers are recreated from saved workspace checkpoints.")

    def _stop_directory(self, directory):
        with self._lock:
            record = self._running.pop(directory.name, None)
        if record and record["generation"] == self._session.generation:
            try:
                self._session.call({"op": "stop", "vm_id": record["vm_id"]})
            except (ComputeError, KeyError):
                pass  # Already gone with its session, or powered itself off.

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
            identities = list(self._running)
        for identity in identities:
            with self._operations.hold(identity):
                self._stop_directory(self.root / identity)
        self._close_disk_guard()
        self._session.close()
