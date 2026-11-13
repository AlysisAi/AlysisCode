"""Operator-side computer lifecycle, independent of the model and cloud vendor.

These methods are not agent tools or an authorization service. A trusted
controller supplies identity, image and commands. Browser/desktop task grants
remain separate. Resource handles are replaceable; owner/bot IDs are not.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Literal, Protocol
from uuid import UUID


class ComputeError(RuntimeError):
    pass


class ComputeUncertain(ComputeError):
    """Dispatch may have happened. Reconcile; never replay automatically."""


class ComputeStopped(ComputeError):
    """The provider stopped this computer on purpose (for example, its device was almost out of
    disk space) and refused the request before sending it. The message is safe to show."""


class ComputeCreationRefused(ComputeError):
    """A provider refused creation before dispatching any resource mutation."""

    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code

    def receipt(self):
        return {
            "error": str(self),
            "code": self.code,
            "computer_created": False,
            "retryable": False,
        }


MAX_TRANSFER_BYTES = 50 * 1024 * 1024
TRANSFER_CHUNK_BYTES = 64 * 1024
TRANSFER_SECONDS = 60


def validate_transfer(guest_path: str, size: int):
    if type(size) is not int or not 0 <= size <= MAX_TRANSFER_BYTES:
        raise ValueError("Transfer size must be 0–50 MiB.")
    try:
        path_bytes = guest_path.encode("utf-8") if isinstance(guest_path, str) else b""
    except UnicodeError:
        raise ValueError("Guest file path must be valid UTF-8.") from None
    if (
        not isinstance(guest_path, str)
        or not guest_path.startswith("/")
        or guest_path.startswith("//")
        or len(path_bytes) > 4096
        or any(ord(c) < 32 or ord(c) == 127 for c in guest_path)
        or ".." in guest_path.split("/")
        or str(PurePosixPath(guest_path)) != guest_path
        or guest_path == "/"
    ):
        raise ValueError("Use a normalized absolute guest file path without traversal.")


def transfer_checkpoint(cancel, deadline, error=ComputeError):
    try:
        cancelled = cancel()
    except Exception:
        raise error("Transfer cancellation state is unavailable.") from None
    if cancelled:
        raise error("Transfer cancelled; do not replay a partial upload.")
    if time.monotonic() >= deadline:
        raise error("Transfer timed out; do not replay a partial upload.")


def transfer_chunks(chunks, size, check, error=ComputeError):
    """Bound each yielded chunk and require exact exhaustion of the source."""
    total = 0
    end = object()
    try:
        iterator = iter(chunks)
    except Exception:
        raise error("Transfer source is unavailable.") from None
    while True:
        check()
        try:
            chunk = next(iterator, end)
        except Exception:
            raise error("Transfer source failed; its contents are unconfirmed.") from None
        if chunk is end:
            break
        if not isinstance(chunk, bytes) or total + len(chunk) > size:
            raise error("Transfer source exceeds its declared size or is not bytes.")
        total += len(chunk)
        for offset in range(0, len(chunk), TRANSFER_CHUNK_BYTES):
            check()
            yield chunk[offset : offset + TRANSFER_CHUNK_BYTES]
    check()
    if total != size:
        raise error("Transfer source does not match its declared size.")


@dataclass(frozen=True)
class ComputerIdentity:
    owner_id: str = field(repr=False)
    bot_id: str
    allocation_id: str

    def __post_init__(self):
        if not self.owner_id or not self.bot_id or max(len(self.owner_id), len(self.bot_id)) > 256:
            raise ValueError("An explicit owner and bot identity are required.")
        if UUID(self.allocation_id).hex != self.allocation_id:
            raise ValueError("Allocation identity must be a canonical UUID hex value.")

    @property
    def labels(self) -> dict[str, str]:
        digest = hashlib.sha256(
            json.dumps([self.owner_id, self.bot_id], ensure_ascii=False).encode()
        ).digest()
        # Cloud label values are limited to 63 characters. Base32 preserves
        # the complete SHA-256 identity in 52 label-safe characters.
        owner = base64.b32encode(digest).decode("ascii").rstrip("=").lower()
        return {
            "alysis-managed": "compute-v1",
            "alysis-owner": owner,
            "alysis-allocation": self.allocation_id,
        }


@dataclass(frozen=True)
class ComputerSpec:
    # Provider-specific image reference: OCI digest locally; imported disk ID
    # on Azure. It is deployment configuration, never part of the bot identity.
    image: str
    cpu: int = 1
    memory_mib: int = 2048
    idle_seconds: int = 60
    internet: bool = False
    suspend_mode: Literal["Disk", "Memory"] = "Disk"

    def __post_init__(self):
        if not self.image or len(self.image) > 512 or any(c.isspace() for c in self.image):
            raise ValueError("An explicit image reference is required.")
        if type(self.cpu) is not int or not 1 <= self.cpu <= 4:
            raise ValueError("Pilot CPU allowance is 1–4 cores.")
        if type(self.memory_mib) is not int or not 512 <= self.memory_mib <= 8192:
            raise ValueError("Pilot memory allowance is 512–8192 MiB.")
        if type(self.idle_seconds) is not int or not 60 <= self.idle_seconds <= 900:
            raise ValueError("Idle suspend allowance is 60–900 seconds.")
        if type(self.internet) is not bool:
            raise ValueError("Internet access must be an explicit operator boolean.")
        if self.suspend_mode not in {"Disk", "Memory"}:
            raise ValueError("Use an explicit disk or memory suspension mode.")


@dataclass(frozen=True)
class ComputerRef:
    provider: str
    scope: str
    resource_id: str
    identity: ComputerIdentity


@dataclass(frozen=True)
class ComputerStatus:
    state: Literal["running", "stopped", "paused", "transitioning", "missing", "unknown"]
    raw_state: str
    # Disk restart is the portable baseline. No claim that process memory,
    # browser tabs, network connections, or unsaved documents survived.
    resume_kind: Literal["disk-restart", "memory"] = "disk-restart"


@dataclass(frozen=True)
class CommandReceipt:
    exit_code: int
    stdout: str = field(repr=False)
    stderr: str = field(repr=False)


def validate_command(argv: list[str], timeout: int):
    if (
        not isinstance(argv, list)
        or not argv
        or len(argv) > 64
        or any(not isinstance(x, str) or "\0" in x for x in argv)
        or sum(len(x.encode()) for x in argv) > 65536
    ):
        raise ValueError("Use bounded command arguments without null bytes.")
    if type(timeout) is not int or not 1 <= timeout <= 60:
        raise ValueError("Command timeout must be 1–60 seconds.")


class ComputerProvider(Protocol):
    def create(self, identity: ComputerIdentity, spec: ComputerSpec) -> ComputerRef: ...
    def status(self, ref: ComputerRef) -> ComputerStatus: ...
    def start(self, ref: ComputerRef) -> ComputerStatus: ...
    def stop(self, ref: ComputerRef) -> ComputerStatus: ...
    def delete(self, ref: ComputerRef) -> ComputerStatus: ...
    def run(self, ref: ComputerRef, argv: list[str], *, timeout: int = 30) -> CommandReceipt: ...
    def find(self, identity: ComputerIdentity) -> ComputerRef | None: ...
    def upload(
        self,
        ref: ComputerRef,
        guest_path: str,
        chunks: Iterable[bytes],
        *,
        size: int,
        cancel: Callable[[], bool] = lambda: False,
    ) -> None: ...
    def download(
        self,
        ref: ComputerRef,
        guest_path: str,
        *,
        size: int,
        cancel: Callable[[], bool] = lambda: False,
    ) -> Iterator[bytes]: ...


def owned(labels: dict, identity: ComputerIdentity):
    if not isinstance(labels, dict) or any(labels.get(k) != v for k, v in identity.labels.items()):
        raise ComputeError("Computer ownership does not match.")


def wait_for(provider: ComputerProvider, ref: ComputerRef, state: str, *, timeout=30):
    """Poll observations only. Never retry a mutation or run an implicit resume."""
    if state not in ("running", "stopped", "missing") or not 0 < timeout <= 60:
        raise ValueError("Use a known target and a bounded wait.")
    deadline = time.monotonic() + timeout
    while True:
        result = provider.status(ref)
        if result.state == state:
            return result
        if result.state in ("missing", "unknown", "paused") or time.monotonic() >= deadline:
            raise ComputeUncertain("Lifecycle target not observed; reconcile before continuing.")
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
