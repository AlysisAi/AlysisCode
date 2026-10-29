"""Real directory aliases without requiring Windows symlink privileges."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def directory_link(link: Path, target: Path) -> None:
    if os.name == "nt":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
        assert link.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    else:
        link.symlink_to(target, target_is_directory=True)
        assert link.is_symlink()
    assert link.resolve(strict=True) == target.resolve(strict=True)
