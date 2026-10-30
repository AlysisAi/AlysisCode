"""Best-effort lifecycle ownership of descendants of an explicitly spawned PID.

Seatbelt is inherited across setsid(), but process-group membership is not. Use
libproc's child query (never a machine-wide scan or command-name match) to retain
identities of observed descendants when they detach or their parent exits.

This is not a kernel job object: an intermediate process can fork and disappear
between observations. Neither this observer nor a restart can reconstruct that
lost ancestry. Filesystem/network containment remains the kernel's responsibility.
"""

from __future__ import annotations

import ctypes
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache


class _BsdInfo(ctypes.Structure):
    # Public Darwin sys/proc_info.h, PROC_PIDTBSDINFO (flavor 3).
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("status", ctypes.c_uint32),
        ("xstatus", ctypes.c_uint32),
        ("pid", ctypes.c_uint32),
        ("ppid", ctypes.c_uint32),
        ("credentials", ctypes.c_uint32 * 7),
        ("comm", ctypes.c_char * 16),
        ("name", ctypes.c_char * 32),
        ("files_and_groups", ctypes.c_uint32 * 6),
        ("start_sec", ctypes.c_uint64),
        ("start_usec", ctypes.c_uint64),
    ]


class _SigInfo(ctypes.Structure):
    # Public Darwin sys/signal.h; si_value is a pointer-sized union.
    _fields_ = [
        ("signo", ctypes.c_int),
        ("error", ctypes.c_int),
        ("code", ctypes.c_int),
        ("pid", ctypes.c_int),
        ("uid", ctypes.c_uint),
        ("status", ctypes.c_int),
        ("address", ctypes.c_void_p),
        ("value", ctypes.c_void_p),
        ("band", ctypes.c_long),
        ("reserved", ctypes.c_ulong * 7),
    ]


@lru_cache(maxsize=1)
def _waitid():
    # Some supported Python 3.12 macOS builds do not expose os.waitid.
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    function = library.waitid
    function.argtypes = [ctypes.c_int, ctypes.c_uint, ctypes.POINTER(_SigInfo), ctypes.c_int]
    function.restype = ctypes.c_int
    return function


def _exited_child(pid: int) -> bool:
    info = _SigInfo()
    # sys/wait.h: P_PID=1; WNOHANG=1, WEXITED=4, WNOWAIT=32. Never reap.
    return _waitid()(1, pid, ctypes.byref(info), 1 | 4 | 32) == 0 and info.pid == pid


@dataclass(frozen=True)
class ProcessIdentity:
    pid: int
    ppid: int
    start_sec: int
    start_usec: int
    status: int

    @property
    def token(self) -> str:
        return f"macos:{self.start_sec}:{self.start_usec}"


@lru_cache(maxsize=1)
def _libproc():
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    library.proc_pidinfo.restype = ctypes.c_int
    library.proc_listchildpids.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
    library.proc_listchildpids.restype = ctypes.c_int
    return library


def process_identity(pid: int) -> ProcessIdentity | None:
    if sys.platform != "darwin" or type(pid) is not int or pid <= 1:
        return None
    info = _BsdInfo()
    try:
        size = _libproc().proc_pidinfo(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info))
    except (OSError, AttributeError):
        return None
    if size != ctypes.sizeof(info) or info.pid != pid or not info.start_sec:
        return None
    return ProcessIdentity(pid, info.ppid, info.start_sec, info.start_usec, info.status)


def _children(pid: int) -> tuple[int, ...]:
    library = _libproc()
    capacity = 32
    while True:
        buffer = (ctypes.c_int * capacity)()
        count = library.proc_listchildpids(pid, buffer, ctypes.sizeof(buffer))
        if count <= 0:
            return ()
        if count < capacity:
            return tuple(value for value in buffer[:count] if value > 1)
        capacity *= 2


class MacProcessTree:
    """Observe one spawned process and retain only verified child identities."""

    def __init__(self, root: ProcessIdentity):
        self._identities = {root.pid: root.token}
        self._lock = threading.RLock()
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._observe, name="alysis-child-owner", daemon=True
        )
        self._thread.start()

    @classmethod
    def start(cls, pid: int, *, expected_token: str | None = None) -> MacProcessTree | None:
        root = process_identity(pid)
        if root is None:
            return None
        if expected_token is None:
            if root.ppid != os.getpid():
                return None
        elif root.token != expected_token:
            return None
        return cls(root)

    def _refresh(self) -> None:
        pending = list(self._identities)
        visited: set[int] = set()
        while pending:
            pid = pending.pop()
            if pid in visited:
                continue
            visited.add(pid)
            token = self._identities[pid]
            parent = process_identity(pid)
            if parent is None or parent.token != token or parent.status == 5:  # SZOMB
                continue
            for child_pid in _children(pid):
                child = process_identity(child_pid)
                checked_parent = process_identity(pid)
                if checked_parent is None or checked_parent.token != token:
                    break
                if child is None or child.ppid != pid or child.pid == os.getpid():
                    continue
                if child.pid not in self._identities:
                    self._identities[child.pid] = child.token
                    pending.append(child.pid)

    def _live(self) -> dict[int, str]:
        living = {}
        for pid, token in self._identities.items():
            current = process_identity(pid)
            if current is not None:
                if current.token == token and current.status != 5:
                    living[pid] = token
            else:
                # libproc omits some zombies. Observe an owned direct child's
                # exit without reaping it; Popen/the terminal waiter owns wait().
                try:
                    if _exited_child(pid):
                        continue
                except (OSError, AttributeError):
                    pass
                # Unknown identity cannot authorize a signal or prove cleanup.
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                except OSError:
                    pass
                living[pid] = token
        self._identities = living
        return living

    def alive(self) -> bool:
        with self._lock:
            self._refresh()
            return bool(self._live())

    def signal(self, signum: int) -> None:
        with self._lock:
            self._refresh()
            # Children first, before terminating the parent that proves ancestry.
            for pid, token in reversed(tuple(self._identities.items())):
                current = process_identity(pid)
                if current is None or current.token != token or current.status == 5:
                    continue
                try:
                    os.kill(pid, signum)
                except OSError:
                    pass

    def terminate(self, grace_seconds: float = 0.5) -> bool:
        self.signal(signal.SIGTERM)
        deadline = time.monotonic() + max(0.0, grace_seconds)
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        self.signal(signal.SIGKILL)
        deadline = time.monotonic() + 1.0
        while self.alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        finished = not self.alive()
        if finished:
            self.close()
        return finished

    def close(self) -> None:
        self._closed.set()
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)

    def _observe(self) -> None:
        try:
            while not self._closed.is_set():
                if not self.alive():
                    return
                self._closed.wait(0.02)
        except (OSError, AttributeError):
            # Explicit cleanup still rechecks identities; observer failure must
            # never authorize a broader signal or a host process-table search.
            return
