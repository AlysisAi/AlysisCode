"""Identify Seatbelt descendants by inherited kernel policy, including daemons.

Two unpredictable Mach lookup names encode opposite decisions in each command's
profile. Querying those decisions survives exec, reparenting and engine restart.
No process argv, environment or executable-name matching is used. This relies on
macOS's private sandbox_check SPI, not a kernel job object or an atomic PID handle.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import signal
import subprocess
import sys
import time
from functools import lru_cache
from pathlib import Path

from .macos_process_tree import ProcessIdentity, _exited_child, _libproc, process_identity


def ownership_names(scratch: Path) -> tuple[str, str] | None:
    # New launches supply 128 random bits in addition to mkdtemp's suffix.
    # Legacy records have no policy marker and must not authorize discovery.
    if re.fullmatch(r"alysis-seatbelt-[0-9a-f]{32}-[a-zA-Z0-9_-]+", scratch.name) is None:
        return None
    # Do not resolve again: a child replacing a pathname must not change the
    # fingerprint captured in the compiled policy and durable ownership record.
    return tuple(
        "com.alysis.ownership." + hashlib.sha256(f"{index}:{scratch}".encode()).hexdigest()
        for index in (0, 1)
    )


@lru_cache(maxsize=1)
def _sandbox_api():
    if sys.platform != "darwin":
        raise OSError("Seatbelt ownership requires macOS.")
    library = ctypes.CDLL("/usr/lib/libsandbox.dylib", use_errno=True)
    check = library.sandbox_check
    # The function is variadic; explicitly type its fixed arguments on arm64.
    check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
    check.restype = ctypes.c_int
    flags = 2 | ctypes.c_int.in_dll(library, "SANDBOX_CHECK_NO_REPORT").value
    return check, flags  # SANDBOX_FILTER_GLOBAL_NAME | NO_REPORT


def _user_pids() -> tuple[int, ...]:
    library = _libproc()
    function = library.proc_listpids
    function.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_int]
    function.restype = ctypes.c_int
    capacity = 256
    while capacity <= 1 << 20:
        buffer = (ctypes.c_int * capacity)()
        # PROC_UID_ONLY=4; returned size is bytes (unlike proc_listchildpids).
        size = function(4, os.getuid(), buffer, ctypes.sizeof(buffer))
        if size <= 0 or size % ctypes.sizeof(ctypes.c_int):
            raise OSError("Cannot enumerate command ownership.")
        if size < ctypes.sizeof(buffer):
            return tuple(pid for pid in buffer[: size // ctypes.sizeof(ctypes.c_int)] if pid > 1)
        capacity *= 2
    raise OSError("Process ownership enumeration exceeded its bound.")


class MacSandboxOwner:
    def __init__(self, scratch: Path):
        names = ownership_names(scratch)
        if names is None:
            raise ValueError("Missing Seatbelt ownership identity.")
        self.names = names
        self._launch_records = []
        self._launch_identities: dict[int, str] = {}
        _sandbox_api()  # Fail before launch when the required SPI is unavailable.
        _user_pids()

    def _matches(self, pid: int) -> bool:
        check, flags = _sandbox_api()
        decisions = []
        for name in self.names:
            ctypes.set_errno(0)
            value = check(pid, b"mach-lookup", flags, ctypes.c_char_p(name.encode()))
            if value not in (0, 1):
                if ctypes.get_errno() == errno.ESRCH:
                    return False
                raise OSError("Cannot verify command sandbox ownership.")
            decisions.append(value)
        return decisions == [0, 1]

    def _members(self) -> tuple[ProcessIdentity, ...]:
        # A parent may exit after the PID snapshot while forking a child absent
        # from it. Never conclude empty until a second enumeration contains no
        # unseen PIDs. Persistent churn is uncertainty, not completed cleanup.
        for _ in range(4):
            pids = _user_pids()
            members = self._inspect(pids)
            if members or set(_user_pids()).issubset(pids):
                return members
        raise OSError("Process table changed during ownership verification.")

    def _inspect(self, pids: tuple[int, ...]) -> tuple[ProcessIdentity, ...]:
        members = {}
        for pid in pids:
            if pid == os.getpid() or not self._matches(pid):
                continue
            identity = process_identity(pid)
            if identity is None:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                raise OSError("Cannot verify owned process creation identity.")
            if identity.status != 5:
                members[pid] = identity
        # Also own our exact launchers during the interval before sandbox-exec
        # applies the profile. These identities come exclusively from spawn().
        for pid, token in self._launch_identities.items():
            identity = process_identity(pid)
            if identity is None:
                if _exited_child(pid):
                    continue
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    continue
                raise OSError("Cannot verify launcher creation identity.")
            if identity.token == token and identity.status != 5:
                members[pid] = identity
        return tuple(members.values())

    def alive(self) -> bool:
        try:
            return bool(self._members())
        except (OSError, AttributeError, ValueError):
            return True  # Unknown is never evidence of completed cleanup.

    def signal(self, signum: int) -> None:
        try:
            members = self._members()
            for member in members:
                # Recheck both policy and creation token immediately before
                # signalling. macOS exposes no unprivileged atomic PID handle.
                launcher = self._launch_identities.get(member.pid) == member.token
                if not launcher and not self._matches(member.pid):
                    continue
                current = process_identity(member.pid)
                if current is None or current.token != member.token or current.status == 5:
                    continue
                try:
                    os.kill(member.pid, signum)
                except ProcessLookupError:
                    pass
        except (OSError, AttributeError, ValueError):
            return  # terminate/alive retain uncertainty; never broaden a kill.

    def terminate(self, grace_seconds: float = 0.5) -> bool:
        for signum, duration in ((signal.SIGTERM, grace_seconds), (signal.SIGKILL, 1.0)):
            deadline = time.monotonic() + max(0.0, duration)
            while True:
                self.signal(signum)
                if not self.alive():
                    return True
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.02)
        return not self.alive()

    def close(self) -> None:
        # No observer thread or process handle to release.
        pass

    def spawn(self, wrapped: str, **kwargs) -> subprocess.Popen:
        """Do not release the trusted shell gate until its PID is durable.

        If the engine dies before publication, EOF ends the gate. After
        publication, recovery owns the exact launcher even before sandbox-exec
        has applied the policy. The gate descriptor is closed before user code.
        """
        from .terminal_ownership import TerminalOwnershipLedger

        read_fd, write_fd = os.pipe()
        process = None
        try:
            gate = (
                f"read -r _alysis_start <&{read_fd} || exit 125; exec {read_fd}<&-; exec {wrapped}"
            )
            process = subprocess.Popen(gate, pass_fds=(read_fd,), **kwargs)
            os.close(read_fd)
            read_fd = -1
            ledger = TerminalOwnershipLedger()
            handle = ledger.track(
                child_pid=process.pid,
                termination_mode="process_group",
                posix_pgid=process.pid,
            )
            if handle is None:
                raise OSError("Could not persist Seatbelt launcher ownership.")
            self._launch_records.append((ledger, handle))
            identity = process_identity(process.pid)
            if identity is None:
                raise OSError("Could not verify Seatbelt launcher identity.")
            self._launch_identities[identity.pid] = identity.token
            os.write(write_fd, b"start\n")
            return process
        except BaseException:
            if process is not None:
                process.kill()
                process.communicate(timeout=5)
            raise
        finally:
            if read_fd >= 0:
                os.close(read_fd)
            os.close(write_fd)

    def release_records(self) -> None:
        for ledger, handle in self._launch_records:
            ledger.release(handle)
        self._launch_records.clear()
