"""Build a separately distributed preview helper; never change machine security.

Prints the exact helper path and digest for the engine's development overrides.
The build output is not silently inserted into the platform-independent wheel.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("This preview build is qualified for compilation on Windows x64 only.")
    root = Path(__file__).resolve().parents[1] / "native" / "windows-sandbox"
    target = root / "target"
    # Bind Cargo's output to the exact directory consumed below. A caller's
    # CARGO_TARGET_DIR must not make a successful build package an older helper
    # left in this checkout's default target directory.
    subprocess.run(
        [
            "cargo",
            "build",
            "--locked",
            "--target-dir",
            str(target),
            *([] if args.debug else ["--release"]),
        ],
        cwd=root,
        check=True,
    )
    profile = "debug" if args.debug else "release"
    directory = target / "bundle" / profile
    directory.mkdir(parents=True, exist_ok=True)
    helper = directory / "alysis-sandbox.exe"
    # Grant only this small runtime bundle, never the whole Cargo build tree.
    shutil.copyfile(target / profile / helper.name, helper)
    with helper.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    manifest = {
        "protocol": 3,
        "security_revision": 4,
        "version": "0.3.3",
        "network_modes": ["off", "on"],
        "platform": "win32-x64",
        "containment": "appcontainer-write-restricted",
        "sha256": digest,
        "qualification": "experimental-unqualified",
        "upstream": "https://github.com/anthropics/sandbox-runtime",
        "upstream_commit": "117eb928202b53c80d3cb6527d88d1b90e4ca7a9",
    }
    (directory / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    shutil.copyfile(root / "vendor" / "srt-win" / "LICENSE", directory / "LICENSE.srt-win")
    shutil.copyfile(root / "vendor" / "srt-win" / "UPSTREAM.md", directory / "UPSTREAM.md")
    print(
        json.dumps(
            {"ALYSIS_WINDOWS_SANDBOX_HELPER": str(helper), "ALYSIS_WINDOWS_SANDBOX_SHA256": digest},
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
