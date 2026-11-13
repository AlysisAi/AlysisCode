"""Download the corresponding source of every package in an owl computer image.

Reads the image's ``sources.json`` (written by ``image_notices.py``) and saves:

* every Debian source package from Debian's snapshot archive, checked against the SHA-1 the
  archive publishes for each file, under ``<out>/debian/<source>_<version>/``;
* every Python package's source distribution from PyPI, checked against its SHA-256, under
  ``<out>/python/``.

Files are only kept once their checksum matches, so an interrupted run resumes where it
stopped. Packages whose source could not be fetched are listed in ``<out>/missing.json``.
Host the output next to the published image if your license review chooses to offer source
from the same place as the image.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

SNAPSHOT = "https://snapshot.debian.org"
PYPI = "https://pypi.org"
Opener = Callable[[str], object]


def _open(url: str):
    request = urllib.request.Request(url, headers={"User-Agent": "alysis-image-sources/1"})
    return urllib.request.urlopen(request, timeout=120)


def _retry(work, attempts=3, delay=2.0):
    for attempt in range(attempts):
        try:
            return work()
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            status = getattr(error, "code", None)
            if status is not None and status < 500 and status != 429:
                raise
            if attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))


def safe_name(name: str) -> str:
    if not name or name in (".", "..") or any(char in name for char in "/\\\0"):
        raise ValueError(f"Refusing an unsafe file name from the archive: {name!r}")
    return name


def get_json(url: str, opener: Opener) -> dict:
    def work():
        with opener(url) as response:
            return json.loads(response.read())

    return _retry(work)


def fetch(url: str, target: Path, algorithm: str, expected: str, opener: Opener) -> bool:
    """Download ``url`` to ``target`` unless a verified copy exists. True if downloaded."""
    if target.exists() and hashlib.new(algorithm, target.read_bytes()).hexdigest() == expected:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".part")

    def work():
        digest = hashlib.new(algorithm)
        with opener(url) as response, partial.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
                out.write(chunk)
        return digest.hexdigest()

    try:
        actual = _retry(work)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise
    if actual != expected:
        partial.unlink(missing_ok=True)
        raise ValueError(f"{target.name} does not match its published checksum.")
    partial.replace(target)
    return True


def debian_source(entry: dict, out: Path, opener: Opener) -> int:
    source, version = entry["source"], entry["version"]
    listing = get_json(
        f"{SNAPSHOT}/mr/package/{quote(source, safe='')}/{quote(version, safe='')}/srcfiles?fileinfo=1",
        opener,
    )
    folder = out / "debian" / safe_name(f"{source}_{version.split(':', 1)[-1]}")
    hashes = [item["hash"] for item in listing.get("result", [])]
    if not hashes:
        raise LookupError("the snapshot archive lists no source files")
    for file_hash in hashes:
        name = safe_name(listing["fileinfo"][file_hash][0]["name"])
        fetch(f"{SNAPSHOT}/file/{file_hash}", folder / name, "sha1", file_hash, opener)
    return len(hashes)


def python_source(entry: dict, out: Path, opener: Opener) -> int:
    name, version = entry["name"], entry["version"]
    release = get_json(f"{PYPI}/pypi/{quote(name, safe='')}/{quote(version, safe='')}/json", opener)
    sdists = [item for item in release.get("urls", []) if item.get("packagetype") == "sdist"]
    if not sdists:
        raise LookupError("PyPI has no source distribution for this release")
    for item in sdists:
        fetch(
            item["url"],
            out / "python" / safe_name(item["filename"]),
            "sha256",
            item["digests"]["sha256"],
            opener,
        )
    return len(sdists)


def run(sources: dict, out: Path, opener: Opener = _open, pause: float = 0.2) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    missing, fetched = [], 0
    jobs = [("debian", entry, debian_source) for entry in sources.get("debian", [])] + [
        ("python", entry, python_source) for entry in sources.get("python", [])
    ]
    for kind, entry, work in jobs:
        try:
            fetched += work(entry, out, opener)
        except (LookupError, ValueError, KeyError, OSError, urllib.error.URLError) as error:
            label = entry.get("source") or entry.get("name")
            missing.append(
                {"kind": kind, "package": label, "version": entry["version"], "reason": str(error)}
            )
        time.sleep(pause)  # the archives are shared public services
    (out / "missing.json").write_text(
        json.dumps(missing, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {"files": fetched, "missing": len(missing)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    summary = run(json.loads(args.sources.read_text(encoding="utf-8")), args.out)
    print(json.dumps(summary))


if __name__ == "__main__":
    main()
