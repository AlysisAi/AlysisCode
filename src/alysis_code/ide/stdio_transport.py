"""Shared input framing and private streams for IDE bridge processes."""

from __future__ import annotations

import io
import os
import sys
from abc import ABC, abstractmethod
from typing import Any, TextIO

from .protocol import MAX_REQUEST_BYTES, error_message


def _private_protocol_stream(stream: TextIO, mode: str) -> TextIO:
    """Duplicate a standard stream onto a descriptor no child process can inherit."""
    descriptor = os.dup(stream.fileno())
    os.set_inheritable(descriptor, False)
    return io.TextIOWrapper(
        open(descriptor, mode),
        encoding="utf-8",
        errors="strict",
        # Match CPython's own stdio wrappers: universal newlines on Windows, "\n" elsewhere.
        newline=None if os.name == "nt" else "\n",
    )


def claim_protocol_streams() -> tuple[TextIO, TextIO]:
    """Make the JSONL transport private to the bridge.

    Child processes (git, search tools, hooks) inherit descriptors 0 and 1 unless the
    caller overrides them. Pointing at the protocol pipes, a child could consume a
    request meant for the bridge or write into the response stream. On Windows the
    pipes are synchronous, so the system serialises every operation on them behind the
    bridge's pending read: a child that merely inspects its inherited stdin at start-up
    blocks until the next request arrives, and a turn waiting for that child never
    finishes. The bridge keeps private duplicates, and descriptors 0 and 1 (with the
    Windows standard handles the C runtime keeps in step with them) move to the null
    device.
    """
    sys.stdout.flush()
    protocol_stdin = _private_protocol_stream(sys.stdin, "rb")
    protocol_stdout = _private_protocol_stream(sys.stdout, "wb")
    null_descriptor = os.open(os.devnull, os.O_RDWR)
    try:
        os.dup2(null_descriptor, sys.stdin.fileno())
        os.dup2(null_descriptor, sys.stdout.fileno())
    finally:
        os.close(null_descriptor)
    return protocol_stdin, protocol_stdout


class JsonLineBridge(ABC):
    stdin: TextIO
    _closed: bool

    def run(self) -> int:
        try:
            while True:
                line = self.stdin.readline(MAX_REQUEST_BYTES + 1)
                if not line:
                    return 0
                if len(line) > MAX_REQUEST_BYTES and not line.endswith("\n"):
                    self._discard_oversized_request_line()
                    self._write(
                        error_message(
                            None,
                            "request_too_large",
                            "Request exceeds the maximum supported size.",
                        )
                    )
                    continue
                self.process_line(line)
                if self._closed:
                    return 0
        finally:
            self.close()

    def _discard_oversized_request_line(self) -> None:
        """Drain one rejected JSONL record so the next request stays aligned."""
        while True:
            remainder = self.stdin.readline(MAX_REQUEST_BYTES + 1)
            if not remainder or remainder.endswith("\n"):
                return

    @abstractmethod
    def process_line(self, line: str) -> None: ...

    @abstractmethod
    def _write(self, payload: dict[str, Any]) -> None: ...

    @abstractmethod
    def close(self) -> None: ...
