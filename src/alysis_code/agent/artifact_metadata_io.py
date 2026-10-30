"""Filesystem-only metadata child. No workspace imports or task-selected code."""

from __future__ import annotations

import json
import stat
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class FileObservation:
    state: str
    stamp: tuple[int, int, int, int, int] | None = None


def observe_metadata(path: str) -> FileObservation:
    candidate = Path(path)
    try:
        # The owning host admits exact paths. Still refuse links/reparse points
        # here before traversing each component; never read file contents.
        for part in [*reversed(candidate.parents), candidate]:
            info = part.lstat()
            if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
                return FileObservation("unavailable")
        if stat.S_ISDIR(info.st_mode):
            return FileObservation("directory")
        if not stat.S_ISREG(info.st_mode):
            return FileObservation("unavailable")
        return FileObservation(
            "file", (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        )
    except FileNotFoundError:
        return FileObservation("missing")
    except (OSError, ValueError):
        return FileObservation("unavailable")


if __name__ == "__main__":
    paths = json.loads(sys.argv[1])
    if not isinstance(paths, list) or len(paths) > 40 or any(not isinstance(p, str) for p in paths):
        raise ValueError("invalid metadata batch")
    print(json.dumps({path: asdict(observe_metadata(path)) for path in paths}))
