"""Bind the packaged Python distribution and its observed libraries into the SBOM."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from scripts.release.install_managed_cli_python import LOCK, validate_runtime


def add_python(sbom: dict, receipt: dict, lock: dict) -> None:
    target = receipt["target"]
    validate_runtime(receipt, target, lock["version"])
    if receipt["pin"] != lock["targets"][target] or receipt["distribution"] != lock["distribution"]:
        raise ValueError("Runtime receipt differs from the committed distribution pin")
    openssl = re.fullmatch(r"OpenSSL (\d+\.\d+\.\d+) .+", receipt["openssl"])
    expat = re.fullmatch(r"expat_(\d+\.\d+\.\d+)", receipt["expat"])
    if openssl is None or expat is None:
        raise ValueError("Runtime library version evidence is invalid")
    root_ref = sbom["metadata"]["component"]["bom-ref"]
    root_dependencies = [item for item in sbom["dependencies"] if item["ref"] == root_ref]
    if len(root_dependencies) != 1:
        raise ValueError("Expected one project dependency record")
    python_ref = f"pkg:generic/cpython@{lock['version']}?arch={target}"
    if any(item["bom-ref"] == python_ref for item in sbom["components"]):
        raise ValueError("Python runtime component already exists")
    python_dependencies = []
    for name, version in [("openssl", openssl[1]), ("expat", expat[1])]:
        reference = f"pkg:generic/{name}@{version}"
        if not any(item["bom-ref"] == reference for item in sbom["components"]):
            sbom["components"].append(
                {
                    "type": "library",
                    "bom-ref": reference,
                    "name": name,
                    "version": version,
                    "purl": reference,
                }
            )
            sbom["dependencies"].append({"ref": reference, "dependsOn": []})
        python_dependencies.append(reference)
    pin = receipt["pin"]
    sbom["components"].append(
        {
            "type": "framework",
            "bom-ref": python_ref,
            "name": "cpython",
            "version": lock["version"],
            "purl": python_ref,
            "externalReferences": [
                {
                    "type": "distribution",
                    "url": pin["url"],
                    "hashes": [{"alg": "SHA-256", "content": pin["sha256"]}],
                }
            ],
        }
    )
    sbom["dependencies"].append({"ref": python_ref, "dependsOn": python_dependencies})
    root_dependencies[0].setdefault("dependsOn", []).append(python_ref)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path)
    parser.add_argument("receipt", type=Path)
    args = parser.parse_args()
    sbom = json.loads(args.sbom.read_text(encoding="utf-8"))
    add_python(
        sbom,
        json.loads(args.receipt.read_text(encoding="utf-8")),
        json.loads(LOCK.read_text(encoding="utf-8")),
    )
    args.sbom.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
