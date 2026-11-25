"""A Cloud computer's reply too large for one command's output, read in parts.

This file runs on the owl's Cloud computer, not on the host: the host sends its source with each
request (cloud_desktop.worker_argv), so it works with every computer image already published.
Standard library only; nothing here may import from alysis_code.

A provider can return only part of a command's output (Azure's sandboxes return at most 1 MiB and
stop the command beyond it). When the computer's reply to a request is larger than that,
run_request() keeps it in a private file and prints a short receipt naming it instead, and
serve_part() then returns the reply one piece at a time. The host checks the whole reply against
the receipt's size and SHA-256 before it uses it. The file goes with its last piece; a file whose
reading was abandoned goes after KEEP_SECONDS, or when more than KEEP_FILES are kept.
"""

import base64
import hashlib
import io
import json
import os
import runpy
import stat
import sys
import time
import uuid
from pathlib import Path

# Where kept replies live: one private folder per computer session, owned by the computer's user.
ROOT = Path("/tmp")
KEEP_SECONDS = 600
KEEP_FILES = 8
# The computer's own user (cloud_desktop_worker): never read or write kept replies as root.
USER = 1000
HEX = frozenset("0123456789abcdef")


def _identifier(value):
    if not isinstance(value, str) or len(value) != 32 or not set(value) <= HEX:
        raise ValueError("Use a 32-character lowercase hex identifier.")
    return value


def folder_for(session):
    return ROOT / ("alysis-replies-" + _identifier(session))


def _private(folder, *, create):
    """The folder, once it is a real directory that only this user can open."""
    if create:
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            pass
    info = os.lstat(folder)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise PermissionError("Kept replies need a private folder.")
    return folder


def _prune(folder, now):
    """Remove replies nobody finished reading: those older than KEEP_SECONDS, then the oldest
    ones beyond KEEP_FILES - 1, so a new one fits."""
    kept = []
    for entry in os.scandir(folder):
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if now - info.st_mtime > KEEP_SECONDS:
            Path(entry.path).unlink(missing_ok=True)
        else:
            kept.append((info.st_mtime, entry.path))
    for _, path in sorted(kept)[: max(0, len(kept) - (KEEP_FILES - 1))]:
        Path(path).unlink(missing_ok=True)


def keep(data, folder):
    """Keep data in a new private file in folder; return the receipt that names it."""
    _private(folder, create=True)
    _prune(folder, time.time())
    name = uuid.uuid4().hex
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    with os.fdopen(os.open(folder / name, flags, 0o600), "wb") as stream:
        stream.write(data)
    return {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def read_part(folder, name, offset, length):
    """One piece of a kept reply, base64-encoded; the file goes once its last piece is read."""
    path = _private(folder, create=False) / _identifier(name)
    if type(offset) is not int or type(length) is not int or offset < 0 or length < 1:
        raise ValueError("Use a non-negative offset and a positive length.")
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC), "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            raise PermissionError("A kept reply must be this user's own file.")
        stream.seek(offset)
        data = stream.read(length)
    if offset + len(data) >= info.st_size:
        path.unlink(missing_ok=True)
    return {
        "bytes": info.st_size,
        "offset": offset,
        "part": base64.b64encode(data).decode("ascii"),
    }


def run_request(module, limit):
    """Run the computer's own bridge client (module) with this command's arguments and print
    its reply, as running the module would. A successful reply over limit bytes is kept, and a
    receipt naming it is printed instead. Exits with the client's own status."""
    captured = io.BytesIO()
    # Kept referenced until the reply is read: a collected wrapper closes what it wraps.
    capturing = io.TextIOWrapper(captured, encoding="utf-8", write_through=True)
    printed, sys.stdout = sys.stdout, capturing
    status = 0
    try:
        runpy.run_module(module, run_name="__main__")
    except SystemExit as stop:
        status = stop.code if isinstance(stop.code, int) else int(stop.code is not None)
    finally:
        capturing.flush()
        sys.stdout = printed
    data = captured.getvalue()
    # The client gives up root before it answers; a reply is never kept by root.
    if status == 0 and len(data) > limit and os.geteuid() != 0:
        try:
            data = json.dumps({"parts": keep(data, folder_for(sys.argv[2]))}).encode() + b"\n"
        except (OSError, ValueError):
            pass  # Printed as it is: the host sees it cut, as without this file.
    printed.buffer.write(data)
    printed.flush()
    sys.exit(status)


def serve_part():
    """Print one piece of a kept reply: arguments session, name, offset, length."""
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(USER)
        os.setuid(USER)
    session, name, offset, length = sys.argv[1:5]
    reply = read_part(folder_for(session), name, int(offset), int(length))
    sys.stdout.write(json.dumps(reply) + "\n")
    sys.stdout.flush()
