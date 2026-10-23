"""Prepare a hash-pinned local Rust distribution for network-free installation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import urllib.request
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


def input_path(url: str) -> Path:
    parsed = urlsplit(url)
    path = PurePosixPath(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "static.rust-lang.org"
        or parsed.query
        or parsed.fragment
        or ".." in path.parts
        or "%" in parsed.path
        or "\\" in parsed.path
        or not path.is_absolute()
    ):
        raise ValueError("Rust input must be a literal path at the official HTTPS origin")
    return Path(*path.parts[1:])


def prepare(lock_path: Path, architecture: str, output: Path) -> dict:
    lock = json.loads(lock_path.read_text())
    if lock["schema_version"] != 1 or architecture not in ("amd64", "arm64"):
        raise ValueError("Unsupported Rust input lock or architecture")
    selected = lock["architectures"][architecture]
    inputs = selected["inputs"]
    if len(inputs) != 5 or len({item["url"] for item in inputs}) != 5:
        raise ValueError("Rust lock must contain manifest, installer, compiler, cargo, and std")
    manifest = Path("dist") / f"channel-rust-{lock['rust_version']}.toml"
    installer = Path("rustup/archive") / lock["rustup_version"] / selected["target"] / "rustup-init"
    paths = [input_path(item["url"]) for item in inputs]
    if manifest not in paths or installer not in paths:
        raise ValueError("Rust manifest or installer version differs from the lock")
    output.mkdir(parents=True, exist_ok=True)
    receipts = []
    for item, relative in zip(inputs, paths, strict=True):
        if not re.fullmatch(r"[0-9a-f]{64}", item["sha256"]):
            raise ValueError("Rust input digest must be a lowercase SHA-256")
        target = output / relative
        if not target.resolve().is_relative_to(output.resolve()):
            raise ValueError("Rust input escaped the output directory")
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".partial")
        with (
            urllib.request.urlopen(item["url"], timeout=90) as response,
            temporary.open("wb") as stream,
        ):
            while chunk := response.read(1024 * 1024):
                stream.write(chunk)
        with temporary.open("rb") as stream:
            observed = hashlib.file_digest(stream, "sha256").hexdigest()
        if observed != item["sha256"]:
            temporary.unlink()
            raise ValueError("Downloaded Rust input differs from its pinned digest")
        temporary.replace(target)
        target.with_name(target.name + ".sha256").write_text(f"{observed}  {target.name}\n")
        receipts.append(
            {"path": relative.as_posix(), "sha256": observed, "bytes": target.stat().st_size}
        )
    (output / installer).chmod(0o755)
    receipt = {
        "schema_version": 1,
        "architecture": architecture,
        "rust_version": lock["rust_version"],
        "rustup_version": lock["rustup_version"],
        "inputs": receipts,
    }
    (output / "rust-inputs-receipt.json").write_text(json.dumps(receipt, indent=2) + "\n")
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lock", type=Path, required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    receipt = prepare(args.lock, args.arch, args.output)
    print(json.dumps({"architecture": args.arch, "verified_inputs": len(receipt["inputs"])}))


if __name__ == "__main__":
    main()
