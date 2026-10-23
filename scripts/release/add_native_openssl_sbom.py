"""Add the verified static OpenSSL build to the native dependency SBOM."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

VERSION = "3.5.9"
SOURCE_SHA256 = "603f5602e2eef00d77fbd429d34dcd5822bb301757a1bc9cdb24c670f1eb859a"
SOURCE_URL = (
    f"https://github.com/openssl/openssl/releases/download/openssl-{VERSION}/"
    f"openssl-{VERSION}.tar.gz"
)


def add_openssl(sbom: dict, actual_version: str) -> None:
    if not actual_version.startswith(f"OpenSSL {VERSION} "):
        raise ValueError("Compiled cryptography is not linked to the pinned OpenSSL version")
    if sbom.get("bomFormat") != "CycloneDX" or sbom.get("specVersion") != "1.5":
        raise ValueError("Expected the locked CycloneDX 1.5 SBOM")
    components = sbom["components"]
    reference = f"pkg:generic/openssl@{VERSION}"
    if any(component.get("bom-ref") == reference for component in components):
        raise ValueError("OpenSSL component already exists")
    crypto = [component for component in components if component.get("name") == "cryptography"]
    if len(crypto) != 1 or not crypto[0].get("bom-ref"):
        raise ValueError("Expected exactly one cryptography component")
    dependency = [item for item in sbom["dependencies"] if item.get("ref") == crypto[0]["bom-ref"]]
    if len(dependency) != 1:
        raise ValueError("Expected exactly one cryptography dependency record")
    components.append(
        {
            "type": "library",
            "name": "openssl",
            "version": VERSION,
            "bom-ref": reference,
            "purl": reference,
            "licenses": [{"license": {"id": "Apache-2.0"}}],
            "externalReferences": [
                {
                    "type": "distribution",
                    "url": SOURCE_URL,
                    "hashes": [{"alg": "SHA-256", "content": SOURCE_SHA256}],
                }
            ],
            "properties": [{"name": "alysis:linkage", "value": "static:cryptography"}],
        }
    )
    dependency[0].setdefault("dependsOn", []).append(reference)
    sbom["dependencies"].append({"ref": reference, "dependsOn": []})


def main() -> None:
    from cryptography.hazmat.backends.openssl.backend import backend

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sbom", type=Path)
    args = parser.parse_args()
    sbom = json.loads(args.sbom.read_text(encoding="utf-8"))
    add_openssl(sbom, backend.openssl_version_text())
    args.sbom.write_text(json.dumps(sbom, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
