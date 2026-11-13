"""In-process receipts for infrastructure created before a sandbox child starts.

Empty protected instruction files persist as sandbox infrastructure. They are
never a filename-based mutation exclusion: only an exclusive creation receipt
may establish their initial baseline, and later edits remain ordinary changes.
"""

from __future__ import annotations

import os
import stat
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

PROTECTED_INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md", "CONVENTIONS.md")


def command_file_signature(info: os.stat_result) -> str:
    ctime_ns = getattr(info, "st_ctime_ns", int(info.st_ctime * 1_000_000_000))
    return f"meta:{info.st_size}:{info.st_mtime_ns}:{ctime_ns}:{info.st_dev}:{info.st_ino}"


@dataclass(frozen=True)
class InstructionCreation:
    relative_path: str
    signature: str


_receipts: ContextVar[tuple[Path, list[InstructionCreation]] | None] = ContextVar(
    "native_sandbox_instruction_creations", default=None
)


@contextmanager
def capture_instruction_creations(root: Path) -> Iterator[list[InstructionCreation]]:
    receipts: list[InstructionCreation] = []
    token = _receipts.set((root.resolve(), receipts))
    try:
        yield receipts
    finally:
        _receipts.reset(token)


# Files this engine process made as reservations, and files it took back, by
# path and by what identified the file then. The Windows sandbox makes an empty
# file for each instruction name a project lacks and takes it back later. Both
# can happen while another command is being watched for the files it changes:
# a background command starts, or the last one stops. An entry explains the
# appearing, or the disappearing, of that exact empty file and nothing else. A
# file the engine made and something else deleted is not explained: only the
# engine's own removal is.
_reservations_made: dict[str, dict[str, None]] = {}
_reservations_removed: dict[str, dict[str, None]] = {}
_reservation_lock = threading.Lock()
_RESERVATION_PATHS_KEPT = 256
_RESERVATION_SIGNATURES_KEPT = 16


def _reservation_identity(signature: str) -> str:
    """A signature without its creation time.

    NTFS gives a file that is made under a name deleted moments before the
    creation time of the file that had the name. A reservation taken back and
    made anew therefore shows another creation time by path than it had when
    it was made. Size, modification time, volume and file id still say which
    file it is and that nothing was written to it.
    """
    fields = signature.split(":")
    return ":".join(fields[:3] + fields[4:]) if len(fields) == 6 else signature


def note_reservation_file(path: Path, info: os.stat_result, *, removed: bool) -> None:
    """Called with the stat of an empty reservation the engine just made, or is about to delete."""
    known = _reservations_removed if removed else _reservations_made
    key = os.path.normcase(os.path.abspath(path))
    signature = _reservation_identity(command_file_signature(info))
    with _reservation_lock:
        signatures = known.pop(key, {})
        signatures.pop(signature, None)
        signatures[signature] = None
        while len(signatures) > _RESERVATION_SIGNATURES_KEPT:
            del signatures[next(iter(signatures))]
        known[key] = signatures
        while len(known) > _RESERVATION_PATHS_KEPT:
            del known[next(iter(known))]


def is_reservation_file(path: Path, signature: str | None, *, removed: bool) -> bool:
    """Whether the engine made (or, with ``removed``, took back) the file with ``signature``."""
    if signature is None:
        return False
    known = _reservations_removed if removed else _reservations_made
    key = os.path.normcase(os.path.abspath(path))
    with _reservation_lock:
        return _reservation_identity(signature) in known.get(key, ())


def record_instruction_creation(path: Path, info: os.stat_result) -> None:
    """Called only with the exclusive creator's retained-handle stat, before launch."""
    if path.name in PROTECTED_INSTRUCTION_FILES and info.st_size == 0:
        note_reservation_file(path, info, removed=False)
    captured = _receipts.get()
    if captured is None:
        return
    root, receipts = captured
    if path.parent != root or path.name not in PROTECTED_INSTRUCTION_FILES:
        raise ValueError("Instruction creation receipt is outside its command workspace")
    if not stat.S_ISREG(info.st_mode) or info.st_size != 0 or info.st_nlink != 1 or not info.st_ino:
        raise ValueError("Instruction creation receipt must identify a new empty regular file")
    receipts.append(InstructionCreation(path.name, command_file_signature(info)))
