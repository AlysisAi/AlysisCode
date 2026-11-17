"""Reproduce the local MIT-licensed picomatch security rebuild; never publish to npm."""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import tarfile
import urllib.request
from pathlib import Path

VERSION = "4.0.8-alysis.1"
UPSTREAM_URL = "https://registry.npmjs.org/picomatch/-/picomatch-4.0.7.tgz"
UPSTREAM_INTEGRITY = (
    "qcJu88Q2IWqJsDD529JKMdwGm/dvInW4HvQnRwiH9JtihJvzGOscDtHE3x1pBKeUOTysQ8kVmLnJ2kJu7yhcGA=="
)
PATCH_SOURCE = "https://github.com/micromatch/picomatch/pull/254"
PATCH_COMMIT = "c63a5bf4e61dbdb81bc41e97dc2616c30acce337"
BEFORE = "    let output = token.close + (opts.capture ? ')' : '');"
AFTER = """    // A single wildcard branch is closed under concatenation: P*S repeated
    // one or more times still matches P*S. Remove the outer repeat instead of
    // nesting its unbounded quantifier around the wildcard's own quantifier.
    const bodyTokens = tokens.slice(token.tokensIndex + 2);
    const singleWildcard = opts.maxExtglobRecursion !== false &&
      (token.type === 'plus' || token.type === 'star') &&
      splitTopLevel(body).length === 1 &&
      bodyTokens.some(part => part.type === 'star' && !part.extglob) &&
      bodyTokens.every(part => !part.extglob && !part.suffix &&
        (part.type === 'text' || part.type === 'star' || part.type === 'qmark' || part.type === 'bracket'));
    let output = (singleWildcard ? (token.type === 'star' ? ')?' : ')') : token.close) + (opts.capture ? ')' : '');"""


def build(archive: bytes) -> tuple[bytes, dict]:
    if base64.b64encode(hashlib.sha512(archive).digest()).decode() != UPSTREAM_INTEGRITY:
        raise ValueError("Upstream picomatch archive integrity mismatch")
    files: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as source:
        for entry in source.getmembers():
            if (
                not entry.isfile()
                or not entry.name.startswith("package/")
                or ".." in Path(entry.name).parts
            ):
                raise ValueError("Unexpected upstream archive member")
            handle = source.extractfile(entry)
            assert handle is not None
            files[entry.name] = handle.read()
    original = files["package/lib/parse.js"]
    text = original.decode()
    if text.count(BEFORE) != 1:
        raise ValueError("Security patch context changed")
    patched = text.replace(BEFORE, AFTER).encode()
    files["package/lib/parse.js"] = patched
    manifest = json.loads(files["package/package.json"])
    if manifest["name"] != "picomatch" or manifest["version"] != "4.0.7":
        raise ValueError("Unexpected upstream identity")
    manifest["version"] = VERSION
    provenance = {
        "upstream_version": "4.0.7",
        "upstream_integrity": "sha512-" + UPSTREAM_INTEGRITY,
        "patch_source": PATCH_SOURCE,
        "patch_commit": PATCH_COMMIT,
        "advisory": "GHSA-fc5q-39xc-vjpf",
        "original_parser_sha256": hashlib.sha256(original).hexdigest(),
        "patched_parser_sha256": hashlib.sha256(patched).hexdigest(),
        "distribution": "Local Alysis security rebuild; not an official upstream or npm-registry release.",
    }
    manifest["alysisSecurityRebuild"] = provenance
    files["package/package.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    files["package/ALYSIS_SECURITY_REBUILD.json"] = (
        json.dumps(provenance, indent=2) + "\n"
    ).encode()
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as target:
            for name, content in sorted(files.items()):
                entry = tarfile.TarInfo(name)
                entry.size, entry.mode, entry.mtime = len(content), 0o644, 0
                target.addfile(entry, io.BytesIO(content))
    return output.getvalue(), provenance


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-archive", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    if args.upstream_archive:
        archive = args.upstream_archive.read_bytes()
    else:
        with urllib.request.urlopen(UPSTREAM_URL, timeout=30) as response:
            archive = response.read(100_000)
    artifact, provenance = build(archive)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(artifact)
    print(json.dumps({"sha256": hashlib.sha256(artifact).hexdigest(), "provenance": provenance}))


if __name__ == "__main__":
    main()
