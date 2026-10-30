"""In-process receipts for infrastructure created before a sandbox child starts.

Empty protected instruction files persist as sandbox infrastructure. They are
never a filename-based mutation exclusion: only an exclusive creation receipt
may establish their initial baseline, and later edits remain ordinary changes.
"""

from __future__ import annotations

import os
import stat
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


def record_instruction_creation(path: Path, info: os.stat_result) -> None:
    """Called only with the exclusive creator's retained-handle stat, before launch."""
    captured = _receipts.get()
    if captured is None:
        return
    root, receipts = captured
    if path.parent != root or path.name not in PROTECTED_INSTRUCTION_FILES:
        raise ValueError("Instruction creation receipt is outside its command workspace")
    if not stat.S_ISREG(info.st_mode) or info.st_size != 0 or info.st_nlink != 1 or not info.st_ino:
        raise ValueError("Instruction creation receipt must identify a new empty regular file")
    receipts.append(InstructionCreation(path.name, command_file_signature(info)))
