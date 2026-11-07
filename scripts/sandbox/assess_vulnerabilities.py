"""Bind reviewed applicability evidence to one scanned image; never waive new findings."""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess
from pathlib import Path
from typing import Any

# Run in the exact image, read-only and without network or host mounts. Include
# bytecode and the venv seed wheel, so the assessment covers executable artifacts.
FINGERPRINT = r"""
import hashlib, json, os, pathlib, sys
result = {}
for root in json.loads(sys.argv[1]):
    p = pathlib.Path(root)
    if not p.exists():
        raise SystemExit('Missing assessment artifact: ' + root)
    h = hashlib.sha256()
    for f in ([p] if p.is_file() else sorted(p.rglob('*'))):
        name = str(f.relative_to(p) if f != p else f.name)
        if f.is_symlink():
            h.update(name.encode() + b'\0symlink\0' + os.readlink(f).encode())
            continue
        if f.is_file():
            h.update(name.encode() + b'\0' + hashlib.sha256(f.read_bytes()).digest())
    result[root] = h.hexdigest()
print(json.dumps(result))
"""


def assess(
    report: dict[str, Any],
    bom: dict[str, Any],
    policy: dict[str, Any],
    image_id: str,
    fingerprints: dict[str, str],
) -> dict[str, Any]:
    """Reject changed evidence and unexpected copies before creating scoped OpenVEX."""
    if report["Metadata"]["ImageID"] != image_id:
        raise ValueError("The report does not describe the inspected image")
    root = bom["metadata"]["component"]
    properties = {item["name"]: item["value"] for item in root["properties"]}
    if properties.get("aquasecurity:trivy:ImageID") != image_id:
        raise ValueError("The SBOM does not describe the inspected image")
    product = root["purl"]
    if not product.startswith("pkg:oci/") or "@sha256:" not in product:
        raise ValueError("OpenVEX must bind an immutable container image")

    packages = [p for r in report["Results"] for p in r.get("Packages", [])]
    os_packages = [
        p for r in report["Results"] if r.get("Class") == "os-pkgs" for p in r.get("Packages", [])
    ]
    findings = {
        (v["PkgIdentifier"]["PURL"], v["VulnerabilityID"])
        for r in report["Results"]
        for v in r.get("Vulnerabilities", [])
    }
    statements = []
    for component in policy["components"]:
        relevant = [
            rule
            for rule in component["assessments"]
            if any((rule["purl"], cve) in findings for cve in rule["vulnerabilities"])
        ]
        if not relevant:
            continue
        providers = [
            p
            for p in os_packages
            if p["Name"] == component["provider"] and p["Version"] == component["version"]
        ]
        if len(providers) != 1:
            raise ValueError(f"Unreviewed provider: {component['provider']}")
        layer = providers[0].get("Layer", {}).get("DiffID")
        if not layer:
            raise ValueError("Missing provider layer provenance")
        for artifact, expected in component["artifacts"].items():
            if fingerprints.get(artifact) != expected:
                raise ValueError(f"Assessment artifacts changed: {artifact}")
        for rule in relevant:
            copies = [p for p in packages if p.get("Identifier", {}).get("PURL") == rule["purl"]]
            if not copies:
                raise ValueError(f"Missing package evidence: {rule['purl']}")
            for copy in copies:
                allowed_paths = rule["analyzers"].get(copy.get("AnalyzedBy"), [])
                if (
                    copy.get("FilePath", "") not in allowed_paths
                    or copy.get("Layer", {}).get("DiffID") != layer
                ):
                    raise ValueError(f"Unreviewed package copy: {rule['purl']}")
            for cve in rule["vulnerabilities"]:
                if (rule["purl"], cve) in findings:
                    statements.append(
                        {
                            "vulnerability": {"name": cve},
                            "products": [
                                {"@id": product, "subcomponents": [{"@id": rule["purl"]}]}
                            ],
                            "status": "not_affected",
                            "justification": rule["justification"],
                            "impact_statement": rule["impact_statement"],
                        }
                    )
    return {
        "@context": "https://openvex.dev/ns/v0.2.0",
        "@id": f"https://github.com/AlysisAi/AlysisCode/sandbox-assessment/{image_id}",
        "author": "Alysis Code sandbox maintainers",
        "timestamp": datetime.datetime.now(datetime.UTC).isoformat(),
        "version": 1,
        "statements": statements,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--bom", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    policy = json.loads(Path(__file__).with_name("vulnerability-assessments.json").read_text())
    report = json.loads(args.report.read_text())
    image_id = subprocess.check_output(
        ["docker", "image", "inspect", "--format", "{{.Id}}", args.image], text=True
    ).strip()
    # Inspect by content ID, not a tag that could be changed between calls.
    roots = {
        artifact
        for component in policy["components"]
        if any(
            p["Name"] == component["provider"]
            for r in report["Results"]
            if r.get("Class") == "os-pkgs"
            for p in r.get("Packages", [])
        )
        for artifact in component["artifacts"]
    }
    fingerprints = json.loads(
        subprocess.check_output(
            [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--cap-drop",
                "ALL",
                "--security-opt",
                "no-new-privileges",
                "--entrypoint",
                "python",
                image_id,
                "-I",
                "-B",
                "-c",
                FINGERPRINT,
                json.dumps(sorted(roots)),
            ],
            text=True,
        )
    )
    vex = assess(report, json.loads(args.bom.read_text()), policy, image_id, fingerprints)
    args.output.write_text(json.dumps(vex, indent=2) + "\n")
    print(f"Bound {len(vex['statements'])} reviewed statements to {image_id}")


if __name__ == "__main__":
    main()
