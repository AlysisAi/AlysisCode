"""Build a digest-bound MXC candidate, preserving every earlier bundle.

Does not install, sign, probe native availability, or enable Desktop activation.
Source inventories bind helper and engine inputs for candidate package admission.
They are build provenance, not a substitute for signed release attestation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
from alysis_code.windows_mxc_contract import helper_contract  # noqa: E402

SOURCE_PATHS = [
    "native/windows-sandbox",
    "src/alysis_code",
    "pyproject.toml",
    "uv.lock",
    "scripts/build_windows_mxc.py",
]


def source_inventory(root: Path) -> dict:
    paths = (
        subprocess.check_output(["git", "ls-files", "-z", "--", *SOURCE_PATHS], cwd=root)
        .decode()
        .split("\0")
    )
    files = {}
    for name in sorted(set(filter(None, paths))):
        path = root / name
        if path.resolve() != path.absolute() or not path.is_file() or path.stat().st_nlink != 1:
            raise ValueError(f"Aliased or missing build source: {name}")
        files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    if not files or "scripts/build_windows_mxc.py" not in files:
        raise ValueError("MXC builder must be tracked before creating a candidate")
    return {
        "schema_version": 1,
        "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root)
        .decode()
        .strip(),
        "files": files,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--output", type=Path, required=True, help="New absolute bundle directory")
    args = parser.parse_args()
    if platform.system() != "Windows" or platform.machine().lower() not in {"amd64", "x86_64"}:
        parser.error("MXC candidate builds require native Windows x64")
    if not args.output.is_absolute() or args.output.exists():
        parser.error("--output must be a new absolute directory; earlier evidence is preserved")
    before = source_inventory(REPO)
    target = REPO / "native/windows-sandbox/target-mxc"
    subprocess.run(
        [
            "cargo",
            "+1.90.0",
            "build",
            "--locked",
            "--manifest-path",
            str(REPO / "native/windows-sandbox/Cargo.toml"),
            "--features",
            "mxc",
            "-j",
            "2",
            "--target-dir",
            str(target),
            *([] if args.debug else ["--release"]),
        ],
        check=True,
    )
    helper = target / ("debug" if args.debug else "release") / "alysis-sandbox.exe"
    binary = helper.read_bytes()
    contract = json.loads(subprocess.check_output([str(helper), "mxc-protocol"], timeout=15))
    if contract != helper_contract():
        raise ValueError("Built MXC helper does not match this engine's exact policy contract")
    if source_inventory(REPO) != before or helper.read_bytes() != binary:
        raise ValueError("MXC source or helper changed during build; discard this candidate")
    args.output.mkdir(parents=True, exist_ok=False)
    sources = (json.dumps(before, indent=2) + "\n").encode()
    manifest = {
        **contract,
        "platform": "win32-x64",
        "sha256": hashlib.sha256(binary).hexdigest(),
        "source_commit": before["source_commit"],
        "source_inventory_sha256": hashlib.sha256(sources).hexdigest(),
    }
    for name, data in {
        "alysis-sandbox.exe": binary,
        "SOURCE.json": sources,
        "manifest.json": (json.dumps(manifest, indent=2) + "\n").encode(),
    }.items():
        with (args.output / name).open("xb") as stream:
            stream.write(data)
    print(
        json.dumps(
            {
                "bundle": os.fspath(args.output),
                "sha256": manifest["sha256"],
                "qualification": contract["qualification"],
            }
        )
    )


if __name__ == "__main__":
    main()
