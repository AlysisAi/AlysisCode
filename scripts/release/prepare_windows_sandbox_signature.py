"""Validate a native build before signing, then bind OS-verified signed bytes.

The protected workflow supplies the signature receipt from Windows' live
Authenticode verification. This script never creates a signature or release pin.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from alysis_code.windows_mxc_candidate_builder import source_inventory  # noqa: E402
from alysis_code.windows_mxc_contract import helper_contract  # noqa: E402


def validate_candidate(
    bundle: Path, expected_inventory: dict, *, signature: dict | None = None
) -> dict:
    names = {"alysis-sandbox.exe", "SOURCE.json", "manifest.json", "LICENSE.mxc"}
    if {p.name for p in bundle.iterdir()} != names:
        raise ValueError("Candidate must contain exactly the four bundle files")
    if any((bundle / name).is_symlink() or not (bundle / name).is_file() for name in names):
        raise ValueError("Candidate inputs must be ordinary files")
    source = (bundle / "SOURCE.json").read_bytes()
    manifest = json.loads((bundle / "manifest.json").read_bytes())
    if (
        json.loads(source) != expected_inventory
        or manifest.get("source_commit") != expected_inventory["source_commit"]
        or manifest.get("source_inventory_sha256") != hashlib.sha256(source).hexdigest()
    ):
        raise ValueError("Candidate provenance does not match the exact checked-out source")
    if manifest.get("platform") != "win32-x64" or any(
        manifest.get(key) != value for key, value in helper_contract().items()
    ):
        raise ValueError("Candidate contract differs from the engine")
    digest = hashlib.sha256((bundle / "alysis-sandbox.exe").read_bytes()).hexdigest()
    if signature is None:
        if manifest.get("sha256") != digest:
            raise ValueError("Unsigned candidate differs from its build receipt")
    elif (
        signature.get("schemaVersion") != 2
        or signature.get("status") != "verified"
        or signature.get("kind") != "authenticode"
        or signature.get("target") != "win32-x64"
        or signature.get("executable") != "alysis-sandbox.exe"
        or signature.get("executableSha256") != digest
        or not re.fullmatch(r"[0-9a-f]{40}", str(signature.get("signerThumbprint")))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(signature.get("signerIdentity")))
        or not re.fullmatch(r"sha256:[0-9a-f]{64}", str(signature.get("timestampSignerIdentity")))
    ):
        raise ValueError("Signature receipt does not identify these verified signed bytes")
    return {**manifest, "sha256": digest}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--expected-commit", required=True)
    parser.add_argument("--signature", type=Path)
    args = parser.parse_args()
    inventory = source_inventory(ROOT)
    if not re.fullmatch(r"[0-9a-f]{40}", args.expected_commit) or (
        inventory["source_commit"] != args.expected_commit
    ):
        raise ValueError("Checkout does not match the independently selected commit")
    if (args.bundle / "LICENSE.mxc").read_bytes() != (
        ROOT / "src/alysis_code/assets/windows-mxc/LICENSE.mxc"
    ).read_bytes():
        raise ValueError("Candidate license differs from the source checkout")
    signature = json.loads(args.signature.read_bytes()) if args.signature else None
    manifest = validate_candidate(args.bundle, inventory, signature=signature)
    if signature is not None:
        (args.bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(
        "Signed candidate digest bound"
        if signature
        else "Unsigned candidate source and bytes verified"
    )


if __name__ == "__main__":
    main()
