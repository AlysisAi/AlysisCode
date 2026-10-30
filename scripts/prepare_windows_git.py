"""Reproduce the pinned native Git asset. Refuses to overwrite an existing tree.

Build the separate signer with cargo +1.90.0 build --release --locked in
native/git-ssh-sign. Copy its executable and dependency license notices to
src/alysis_code/assets/git-sign, then review/update its pin in windows_toolchain.
"""

import hashlib
import io
import json
import shutil
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "src/alysis_code/assets/windows-git"
VERSION = "2.56.0.windows.3"
URL = "https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.3/MinGit-2.56.0.3-busybox-64-bit.zip"
ARCHIVE_SHA256 = "ab4aabd3b8b1c2610725f4b48312511b5e4ff7af99f6e0ae1aa286ea19faaf85"
MANIFEST_SHA256 = "3cb8d24f84c6b2b705aa76457ab7ac931ef1ca3fd66492e1302a159c2f1d094c"


def main():
    if TARGET.exists():
        raise SystemExit("Destination already exists; preserve previous binaries/evidence.")
    with urllib.request.urlopen(URL, timeout=60) as response:
        archive = response.read(64 * 1024 * 1024)
    if hashlib.sha256(archive).hexdigest() != ARCHIVE_SHA256:
        raise SystemExit("Git download digest mismatch")
    with zipfile.ZipFile(io.BytesIO(archive)) as zip:
        for member in zip.infolist():
            path = Path(member.filename)
            if (
                path.is_absolute()
                or ".." in path.parts
                or ":" in member.filename
                or "\\" in member.filename
                or (member.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise SystemExit("Unsafe archive member")
        TARGET.mkdir(parents=True, exist_ok=False)
        zip.extractall(TARGET)
    shutil.copyfile(TARGET / "ucrt64/bin/busybox.exe", TARGET / "ucrt64/bin/sh.exe")
    files = {
        p.relative_to(TARGET).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(TARGET.rglob("*"))
        if p.is_file()
    }
    manifest = {
        "version": VERSION,
        "source": "https://github.com/git-for-windows/git/releases/tag/" + VERSION,
        "archive_sha256": ARCHIVE_SHA256,
        "files": files,
        "preparation": "Copy the upstream busybox.exe to sh.exe so Git can invoke sh -c without losing the BusyBox applet name.",
    }
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA256:
        raise SystemExit("Prepared Git inventory mismatch; do not package it")
    (TARGET / "bundle-manifest.json").write_bytes(raw)
    print("Prepared verified Git " + VERSION)


if __name__ == "__main__":
    main()
