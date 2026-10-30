"""Bounded, digest-bound tool input staged without following a mutable parent."""

from __future__ import annotations

import os
import uuid
from contextlib import contextmanager
from pathlib import Path

from .host_file_handles import host_file_action, windows_packet_file
from .host_file_policy import HostFilePolicy

MAX_PACKET_BYTES = 32 * 1024 * 1024


@contextmanager
def stage_packet(root: Path, contents: bytes):
    if len(contents) > MAX_PACKET_BYTES:
        raise ValueError("Contained file request exceeds 32 MiB")
    name = ".alysis-file-request-" + uuid.uuid4().hex + ".json"
    target = root / name
    if os.name == "nt":
        with host_file_action(HostFilePolicy.capture()):
            with windows_packet_file(root, name) as stream:
                stream.write(contents)
                stream.flush()
                yield target
    else:
        absolute = Path(os.path.abspath(root))
        parent = os.open(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
        try:
            for part in absolute.parts[1:]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                os.close(parent)
                parent = child
            descriptor = os.open(
                name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
            )
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(contents)
                yield target
            finally:
                try:
                    os.unlink(name, dir_fd=parent)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent)
