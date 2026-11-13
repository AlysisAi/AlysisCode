"""Validate reusable OpenSSL installations for unsigned CI builds only.

The receipt detects stale or damaged cache contents; it is not an attestation.
GitHub's cache scope supplies the trust boundary. Signing jobs build from source.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
from collections.abc import Mapping
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
RECEIPT = ".alysis-openssl-build.json"
TARGETS = {
    "win32-arm64": ("Windows", "ARM64", "alysis-openssl-arm64", "build_windows_arm64_openssl.ps1"),
    "darwin-x64": ("macOS", "X64", "alysis-openssl-x64", "build_macos_x64_openssl.sh"),
}


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def configuration(
    target: str, environment: Mapping[str, str], scripts: Path = SCRIPTS
) -> tuple[Path, dict]:
    runner_os, runner_arch, directory, builder = TARGETS[target]
    required = ("RUNNER_TEMP", "ImageOS", "ImageVersion")
    if (
        environment.get("RUNNER_ENVIRONMENT") != "github-hosted"
        or environment.get("RUNNER_OS") != runner_os
        or environment.get("RUNNER_ARCH") != runner_arch
        or any(not environment.get(name) for name in required)
    ):
        raise ValueError("OpenSSL caching requires the matching hosted runner and image identity")
    # Resolve the parent, never a restored prefix that could itself be a symlink.
    prefix = Path(environment["RUNNER_TEMP"]).resolve() / directory
    identity = {
        "schema": 1,
        "target": target,
        "runner_os": runner_os,
        "runner_arch": runner_arch,
        "image_os": environment["ImageOS"],
        "image_version": environment["ImageVersion"],
        "prefix": prefix.as_posix(),
        # Builder bytes bind the source checksum, version, flags and upstream tests.
        "inputs": {
            builder: digest(scripts / builder),
            "native_openssl_cache.py": digest(scripts / "native_openssl_cache.py"),
        },
    }
    return prefix, identity


def cache_key(identity: dict) -> str:
    encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    return f"managed-cli-openssl-{identity['target']}-{hashlib.sha256(encoded).hexdigest()}"


def inventory(prefix: Path, target: str) -> dict[str, str]:
    if prefix.is_symlink() or not prefix.is_dir():
        raise ValueError("OpenSSL cache prefix must be a real directory")
    files = {}
    for path in sorted(prefix.rglob("*")):
        relative = path.relative_to(prefix).as_posix()
        if path.is_symlink():
            raise ValueError(f"OpenSSL cache contains a symlink: {relative}")
        if path.is_dir() or relative == RECEIPT:
            continue
        if not path.is_file():
            raise ValueError(f"OpenSSL cache contains a non-file: {relative}")
        files[relative] = digest(path)
    libraries = (
        ("lib/libssl.lib", "lib/libcrypto.lib")
        if target == "win32-arm64"
        else ("lib/libssl.a", "lib/libcrypto.a")
    )
    for name in (*libraries, "include/openssl/ssl.h", "include/openssl/opensslv.h"):
        if name not in files or (prefix / name).stat().st_size == 0:
            raise ValueError(f"OpenSSL cache is missing a nonempty required file: {name}")
    return files


def record(prefix: Path, identity: dict) -> None:
    # Called only after the unchanged builder has passed its full upstream suite.
    receipt = {"identity": identity, "files": inventory(prefix, identity["target"])}
    (prefix / RECEIPT).write_text(json.dumps(receipt, sort_keys=True) + "\n", encoding="utf-8")


def verify(prefix: Path, identity: dict) -> None:
    if prefix.is_symlink() or (prefix / RECEIPT).is_symlink():
        raise ValueError("OpenSSL cache receipt must not be redirected")
    receipt = json.loads((prefix / RECEIPT).read_text(encoding="utf-8"))
    if receipt != {"identity": identity, "files": inventory(prefix, identity["target"])}:
        raise ValueError("OpenSSL cache identity or file hashes do not match")


def reuse_or_clear(prefix: Path, identity: dict, exact_hit: bool) -> bool:
    if exact_hit:
        try:
            verify(prefix, identity)
        except (OSError, ValueError) as error:
            print(f"OpenSSL cache rejected; rebuilding from pinned source: {error}")
        else:
            print("Verified exact OpenSSL cache hit; retaining native package and SBOM checks.")
            return True
    # Partial hits and invalid exact hits must not leave stale headers/libraries.
    if prefix.is_symlink():
        prefix.unlink()
    elif prefix.exists():
        shutil.rmtree(prefix)
    print("OpenSSL cache miss; the full source build and upstream tests will run.")
    return False


def write_outputs(path: Path, values: Mapping[str, str]) -> None:
    if any("\n" in value or "\r" in value for value in values.values()):
        raise ValueError("GitHub output values must be single-line")
    with path.open("a", encoding="utf-8") as output:
        for name, value in values.items():
            output.write(f"{name}={value}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "check", "record"))
    parser.add_argument("--target", required=True, choices=TARGETS)
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--cache-hit", choices=("", "true", "false"), default="")
    args = parser.parse_args()
    if args.command != "record" and args.github_output is None:
        parser.error("prepare/check require --github-output")
    prefix, identity = configuration(args.target, os.environ)
    if args.command == "prepare":
        write_outputs(args.github_output, {"key": cache_key(identity), "path": prefix.as_posix()})
    elif args.command == "check":
        reuse = reuse_or_clear(prefix, identity, args.cache_hit == "true")
        write_outputs(args.github_output, {"reuse": str(reuse).lower()})
    else:
        record(prefix, identity)
        print("Recorded OpenSSL installation after its source build and upstream tests passed.")


if __name__ == "__main__":
    main()
