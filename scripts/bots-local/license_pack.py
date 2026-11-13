"""Copy an image's license pack off its finished disk, for publishing next to the image.

``image_notices.py`` writes the pack inside the image at build time; this reads it straight
from the image's ext4 root (raw or fixed-VHD layout), with no mounting or extra tools.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import disk_formats

PACK = {
    "notices": ("/usr/share/alysis/THIRD-PARTY-NOTICES.txt", "THIRD-PARTY-NOTICES.txt"),
    "sources": ("/usr/share/alysis/sources.json", "sources.json"),
}
GUEST_SCRIPT = "/usr/local/lib/alysis-image-notices.py"


def guest_script() -> str:
    return (Path(__file__).with_name("image_notices.py")).read_text(encoding="utf-8")


def setup_step(python: str = "/opt/alysis/.venv/bin/python") -> str:
    """The setup-script line that writes the pack, after Python packages are installed."""
    return f"{python} -I {GUEST_SCRIPT}\n"


def copy_from_disk(disk: Path, directory: Path, *, prefix: str = "") -> dict:
    """Write the pack into ``directory`` and return manifest entries (name and sha256)."""
    entries = {}
    with disk.open("rb") as stream:
        offset, _size, _partuuid = disk_formats.linux_root(stream)
        filesystem = disk_formats.Ext4(stream, offset)
        for key, (path, name) in PACK.items():
            try:
                data = filesystem.read(filesystem.lookup(path))
            except KeyError:
                raise RuntimeError(
                    f"The built image has no {path}; its license pack was not generated."
                ) from None
            if key == "sources":
                json.loads(data)  # a damaged pack must not be published
            if not data.strip():
                raise RuntimeError(f"The built image's {path} is empty.")
            (directory / name).write_bytes(prefix.encode() + data if key == "notices" else data)
            entries[key] = {
                "name": name,
                "sha256": hashlib.sha256((directory / name).read_bytes()).hexdigest(),
            }
    return entries
