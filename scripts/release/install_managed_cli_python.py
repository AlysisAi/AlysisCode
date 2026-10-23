"""Install the checksum-pinned, security-patched native packaging interpreter."""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

LOCK = Path(__file__).with_name("managed_cli_python.lock.json")


def verify_archive(path: Path, pin: dict) -> None:
    if path.stat().st_size != pin["size"]:
        raise ValueError("Python distribution size differs from the release pin")
    with path.open("rb") as archive:
        digest = hashlib.file_digest(archive, "sha256").hexdigest()
    if digest != pin["sha256"]:
        raise ValueError("Python distribution SHA-256 differs from the release pin")


def download_verified_archive(path: Path, pin: dict) -> None:
    """Retry incomplete transfers while requiring the same size and digest every time."""
    for attempt in range(3):
        try:
            with (
                urllib.request.urlopen(pin["url"], timeout=120) as response,
                path.open("xb") as output,
            ):
                while chunk := response.read(1024 * 1024):
                    output.write(chunk)
                    if output.tell() > pin["size"]:
                        raise ValueError("Python download exceeds the pinned size")
            verify_archive(path, pin)
            return
        except (urllib.error.URLError, http.client.IncompleteRead, TimeoutError, ValueError):
            path.unlink(missing_ok=True)
            if attempt == 2:
                raise
            print(
                "Python archive download failed verification; retrying pinned download.",
                file=sys.stderr,
            )
            time.sleep(attempt + 1)


def validate_runtime(facts: dict, target: str, version: str) -> None:
    expected_platform = target.split("-", 1)[0]
    expected_arch = "arm64" if target.endswith("-arm64") else "x64"
    actual_arch = {"amd64": "x64", "x86_64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(
        str(facts.get("machine", "")).lower()
    )
    if (
        facts.get("version") != version
        or facts.get("platform") != expected_platform
        or actual_arch != expected_arch
    ):
        raise ValueError("Installed Python version or native target differs from the release pin")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--destination", required=True, type=Path)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    pin = lock["targets"][args.target]
    if not pin["url"].startswith(
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
    ):
        raise ValueError("Python download is outside the approved upstream repository")
    destination = args.destination.resolve()
    destination.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="alysis-python-download-") as temporary:
        archive = Path(temporary) / "python.tar.gz"
        download_verified_archive(archive, pin)
        # Only the complete hash-verified upstream archive is extracted.
        with tarfile.open(archive, "r:gz") as bundle:
            bundle.extractall(destination, filter="data")
    executable = (
        destination
        / "python"
        / ("python.exe" if args.target.startswith("win32-") else "bin/python3")
    )
    probe = (
        "import json,platform,ssl,sys,pyexpat; "
        "print(json.dumps(dict(version=platform.python_version(),platform=sys.platform,"
        "machine=platform.machine(),openssl=ssl.OPENSSL_VERSION,expat=pyexpat.EXPAT_VERSION)))"
    )
    facts = json.loads(
        subprocess.run(
            [str(executable), "-I", "-c", probe],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    )
    validate_runtime(facts, args.target, lock["version"])
    if args.github_output:
        with args.github_output.open("a", encoding="utf-8") as output:
            output.write(f"executable={executable.as_posix()}\n")
    receipt = {"target": args.target, "distribution": lock["distribution"], "pin": pin, **facts}
    if args.receipt:
        args.receipt.parent.mkdir(parents=True, exist_ok=True)
        args.receipt.write_text(json.dumps(receipt, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(receipt))


if __name__ == "__main__":
    main()
