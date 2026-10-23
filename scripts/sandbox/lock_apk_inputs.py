#!/usr/bin/env python3
"""Freeze the complete APK graph from a reviewed trial's installed-package receipt.

Downloaded bytes are hash-pinned here. The image builder independently verifies
the signed repository index and installs its authenticated APKs without network.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import urllib.request
from pathlib import Path

REPOSITORY = "https://apk.cgr.dev/chainguard"
ARCHITECTURES = {"amd64": "x86_64", "arm64": "aarch64"}
MAX_APK_BYTES = 256 * 1024 * 1024


def parse_inventory(text: str, architecture: str) -> list[dict[str, str]]:
    """Accept only complete, unique package identities from the APK database."""
    marker = "Installed APK records:\n"
    if marker not in text:
        raise ValueError("Missing installed APK database")
    records = []
    names = set()
    for block in text.split(marker, 1)[1].strip().split("\n\n"):
        fields: dict[str, str] = {}
        for line in block.splitlines():
            if line[:2] in ("P:", "V:", "A:", "C:"):
                key, value = line.split(":", 1)
                if key in fields:
                    raise ValueError("Duplicate APK identity field")
                fields[key] = value
        if set(fields) != {"P", "V", "A", "C"}:
            raise ValueError("Incomplete APK identity")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+_.-]*", fields["P"]):
            raise ValueError("Unsafe APK package name")
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9+_.~-]*-r[0-9]+", fields["V"]):
            raise ValueError("Unsafe APK version")
        if fields["A"] not in (architecture, "noarch"):
            raise ValueError("APK architecture differs from the requested target")
        if not re.fullmatch(r"Q1[A-Za-z0-9+/]{27}=", fields["C"]):
            raise ValueError("Invalid installed APK checksum")
        if fields["P"] in names:
            raise ValueError("Duplicate installed APK package")
        names.add(fields["P"])
        records.append(
            {
                "name": fields["P"],
                "version": fields["V"],
                "architecture": fields["A"],
                "installed_checksum": fields["C"],
            }
        )
    if not records:
        raise ValueError("Empty APK graph")
    return sorted(records, key=lambda row: row["name"])


def download_package(package: dict[str, str], architecture: str, cache: Path) -> dict:
    filename = f"{package['name']}-{package['version']}.apk"
    url = f"{REPOSITORY}/{architecture}/{filename}"
    path = cache / filename
    if not path.exists():
        temporary = path.with_suffix(".apk.part")
        with urllib.request.urlopen(url, timeout=120) as response, temporary.open("xb") as out:
            count = 0
            while chunk := response.read(1024 * 1024):
                count += len(chunk)
                if count > MAX_APK_BYTES:
                    raise ValueError("APK archive exceeds the size limit")
                out.write(chunk)
        temporary.rename(path)
    size = path.stat().st_size
    if not 0 < size <= MAX_APK_BYTES:
        raise ValueError("APK archive size is invalid")
    with path.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    return {**package, "filename": filename, "url": url, "size": size, "sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory", type=Path, required=True)
    parser.add_argument("--arch", choices=ARCHITECTURES, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    args = parser.parse_args()
    architecture = ARCHITECTURES[args.arch]
    inventory = args.inventory.read_bytes()
    packages = parse_inventory(inventory.decode().replace("\r\n", "\n"), architecture)
    args.cache_dir.mkdir(parents=True, exist_ok=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        records = list(
            pool.map(
                lambda package: download_package(package, architecture, args.cache_dir),
                packages,
            )
        )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "packages.lock").write_text(
        "".join(f"{row['name']}={row['version']}\n" for row in records),
        encoding="utf-8",
    )
    (args.output_dir / "SHA256SUMS").write_text(
        "".join(f"{row['sha256']}  {row['filename']}\n" for row in records),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "architecture": architecture,
        "repository": REPOSITORY,
        "inventory_sha256": hashlib.sha256(inventory).hexdigest(),
        "packages": records,
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        json.dumps(
            {
                "architecture": architecture,
                "packages": len(records),
                "archive_bytes": sum(row["size"] for row in records),
            }
        )
    )


if __name__ == "__main__":
    main()
