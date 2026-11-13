"""Device admission budget for bundled private computers, separate from host commands."""

import os
import platform
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

CPU_PER_COMPUTER = 2
MEMORY_PER_COMPUTER = 3072


GIB = 1024**3
# Disk space for private computers (AL, 2026-11-07). A computer's own disk starts at a few MB
# and grows only with what it writes, so instead of reserving a worst case (the whole image per
# computer) a start needs realistic room, and DiskGuard stops running computers cleanly if the
# device gets critically low while they work.
DISK_HEADROOM = 2 * GIB  # always left for the device itself
DISK_PER_COMPUTER = 1 * GIB  # working room for each running computer
DISK_CRITICAL = 1 * GIB  # below this, running computers are stopped before the disk fills


def disk_required(running: int) -> int:
    """Free space needed to start one more computer while ``running`` already run."""
    return DISK_HEADROOM + (running + 1) * DISK_PER_COMPUTER


def storage_refusal(required: int, free: int) -> str:
    """The person-facing reason a private computer could not start for lack of disk space."""
    return (
        f"Not enough free disk space for another private computer. It needs about "
        f"{-(-required // GIB)} GB free and this device has {free / GIB:.1f} GB. "
        "Free up space or finish the other task, then try again."
    )


def low_space_message(free: int) -> str:
    """The person-facing reason running computers were stopped by DiskGuard."""
    return (
        f"This device is almost out of disk space ({free / GIB:.1f} GB free), so the owl's "
        "computer was stopped before the disk filled up. Files saved before this task are kept. "
        "Free up space, then try again."
    )


class DiskGuard:
    """Stops a provider's running computers before its disk fills up.

    Checks free space every few seconds, only while computers run; the thread ends with the
    last computer and starts again with the next one.
    """

    def __init__(self, path, running, stop_all, *, interval=5.0, critical=DISK_CRITICAL, free=None):
        self._path, self._running, self._stop_all = path, running, stop_all
        self._interval, self._critical = interval, critical
        self._free = free or (lambda target: shutil.disk_usage(target).free)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._thread = None
        self._closed = False

    def watch(self):
        """Call after a computer starts."""
        with self._lock:
            if self._closed or self._thread is not None:
                return
            self._thread = threading.Thread(
                target=self._loop, name="alysis-disk-guard", daemon=True
            )
            self._thread.start()

    def _loop(self):
        while not self._wake.wait(self._interval):
            with self._lock:
                if not self._running():
                    self._thread = None
                    return
            try:
                free = self._free(self._path)
            except OSError:
                continue
            if free < self._critical:
                from . import diagnostics

                diagnostics.record("computers_stopped_low_disk", free_disk_gb=round(free / GIB, 1))
                self._stop_all(low_space_message(free))
        with self._lock:
            self._thread = None

    def close(self):
        with self._lock:
            self._closed = True
        self._wake.set()


_ALLOCATION = re.compile(r"[0-9a-f]{32}")


def sweep_leftover_computers(root: Path) -> list[str]:
    """Remove computer folders a crash or power loss left behind. Call when the provider starts.

    A local computer lives only as long as the engine that made it (the Windows service and the
    Mac runner stop its VM when the engine goes away), so at start nothing in ``root`` is in use.
    A folder that cannot be removed yet (a file still locked) is left for the next start.
    """
    removed = []
    try:
        entries = list(root.iterdir())
    except OSError:
        return removed
    for entry in entries:
        if entry.is_dir() and not entry.is_symlink() and _ALLOCATION.fullmatch(entry.name):
            shutil.rmtree(entry, ignore_errors=True)
            if not entry.exists():
                removed.append(entry.name)
    return removed


class DiskGuarded:
    """Local computer providers: stop running computers before the disk fills, then say why.

    Needs ``root``, ``_lock``, ``_running_ids()`` and ``_stop_directory(directory)``. Its state is
    created on first use, so a provider built without ``__init__`` (as tests do) still works.
    """

    def _space_stops(self):
        return self.__dict__.setdefault("_disk_space_stops", {})

    def _watch_disk(self):
        guard = self.__dict__.get("_disk_guard")
        if guard is None:
            guard = self.__dict__.setdefault(
                "_disk_guard",
                DiskGuard(self.root, lambda: bool(self._running_ids()), self._stop_for_space),
            )
        guard.watch()

    def _stop_for_space(self, message):
        with self._lock:
            identities = list(self._running_ids())
        for identity in identities:
            self._space_stops()[identity] = message
            self._stop_directory(self.root / identity)

    def _space_stop(self, resource_id):
        """Why this computer was stopped for disk space, if it was."""
        return self._space_stops().get(resource_id)

    def _close_disk_guard(self):
        guard = self.__dict__.get("_disk_guard")
        if guard is not None:
            guard.close()


def _windows_physical_memory():
    """Total physical memory in bytes, from Windows (``os.sysconf`` does not exist there)."""
    import ctypes

    class MemoryStatus(ctypes.Structure):
        _fields_ = [
            ("length", ctypes.c_ulong),
            ("memory_load", ctypes.c_ulong),
            ("total_physical", ctypes.c_ulonglong),
            ("available_physical", ctypes.c_ulonglong),
            ("total_page_file", ctypes.c_ulonglong),
            ("available_page_file", ctypes.c_ulonglong),
            ("total_virtual", ctypes.c_ulonglong),
            ("available_virtual", ctypes.c_ulonglong),
            ("available_extended_virtual", ctypes.c_ulonglong),
        ]

    status = MemoryStatus()
    status.length = ctypes.sizeof(MemoryStatus)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        raise OSError("Windows did not report its memory.")
    return status.total_physical


def host_resources():
    if platform.system() == "Darwin":
        memory = int(
            subprocess.check_output(
                ["/usr/sbin/sysctl", "-n", "hw.memsize"],
                timeout=5,
            )
        )
    elif platform.system() == "Windows":
        memory = _windows_physical_memory()
    else:
        memory = os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    return os.cpu_count() or 1, memory // (1024 * 1024)


@dataclass(frozen=True)
class LocalResources:
    cpu_budget: int
    memory_mib: int

    @property
    def max_computers(self):
        return min(16, self.cpu_budget // CPU_PER_COMPUTER, self.memory_mib // MEMORY_PER_COMPUTER)

    @classmethod
    def parse(cls, value, *, host=None):
        cores, memory = host if host is not None else host_resources()
        if value is None:
            # Half of this device's cores and memory (as WSL does by default): a larger
            # machine runs more owls at once; the app sends the same default.
            value = {"cpu_budget": max(2, cores // 2), "memory_mib": memory // 2}
        if not isinstance(value, dict) or set(value) != {"cpu_budget", "memory_mib"}:
            raise ValueError("Set a CPU and RAM budget for private computers.")
        cpu, ram = value["cpu_budget"], value["memory_mib"]
        if type(cpu) is not int or not 2 <= cpu <= cores:
            raise ValueError(
                "The private computer CPU budget must fit this device and be at least 2."
            )
        if type(ram) is not int or not MEMORY_PER_COMPUTER <= ram <= memory - 2048:
            raise ValueError(
                "The private computer RAM budget needs at least 3 GiB and must leave 2 GiB for the host."
            )
        return cls(cpu, ram)
