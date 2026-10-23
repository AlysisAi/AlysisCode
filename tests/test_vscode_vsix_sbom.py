from __future__ import annotations

import hashlib
import json
from pathlib import Path
from uuid import RFC_4122, UUID
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from _release_test_helpers import PACKAGE_VERSION, RELEASE_TAG
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from scripts.release.build_managed_cli_manifest import TARGETS, ArtifactInput, build_manifest
from scripts.release.build_vscode_vsix_sbom import VsixSbomError, build_vsix_sbom


def test_vsix_sbom_binds_final_package_to_verified_managed_runtime(tmp_path: Path) -> None:
    vsix, manifest_path, public_key_path, dependency_sbom_path = _candidate(tmp_path)

    sbom = build_vsix_sbom(
        vsix_path=vsix,
        target="linux-x64",
        manifest_path=manifest_path,
        public_key_path=public_key_path,
        dependency_sbom_path=dependency_sbom_path,
    )
    repeated = build_vsix_sbom(
        vsix_path=vsix,
        target="linux-x64",
        manifest_path=manifest_path,
        public_key_path=public_key_path,
        dependency_sbom_path=dependency_sbom_path,
    )

    extension = sbom["metadata"]["component"]
    managed = next(item for item in sbom["components"] if item["name"] == "alysis-code")
    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.5"
    document_id = UUID(sbom["serialNumber"])
    assert document_id.urn == sbom["serialNumber"]
    assert document_id.variant == RFC_4122
    assert sbom == repeated
    assert extension["hashes"] == [
        {"alg": "SHA-256", "content": hashlib.sha256(vsix.read_bytes()).hexdigest()}
    ]
    assert extension["bom-ref"].endswith("?target=linux-x64")
    assert managed["version"] == PACKAGE_VERSION
    assert managed["hashes"][0]["content"] == hashlib.sha256(b"linux runtime").hexdigest()
    assert {item["name"]: item["value"] for item in managed["properties"]}[
        "alysis:dependency-sbom-sha256"
    ] == hashlib.sha256(dependency_sbom_path.read_bytes()).hexdigest()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    linux_artifact = next(item for item in manifest["artifacts"] if item["target"] == "linux-x64")
    assert {item["name"]: item["value"] for item in managed["properties"]}[
        "alysis:native-evidence-sha256"
    ] == linux_artifact["nativeSignature"]["evidenceSha256"]
    assert [item["bom-ref"] for item in sbom["components"]] == sorted(
        [managed["bom-ref"], "pkg:pypi/alpha@1.0.0", "pkg:pypi/bravo@2.0.0"]
    )
    dependency_graph = {item["ref"]: item for item in sbom["dependencies"]}
    assert dependency_graph[extension["bom-ref"]]["dependsOn"] == [managed["bom-ref"]]
    assert dependency_graph[managed["bom-ref"]]["dependsOn"] == ["pkg:pypi/alpha@1.0.0"]
    assert dependency_graph["pkg:pypi/alpha@1.0.0"]["dependsOn"] == ["pkg:pypi/bravo@2.0.0"]
    assert dependency_graph["pkg:pypi/bravo@2.0.0"] == {"ref": "pkg:pypi/bravo@2.0.0"}


def test_vsix_sbom_document_id_changes_with_final_package_bytes(tmp_path: Path) -> None:
    vsix, manifest_path, public_key_path, dependency_sbom_path = _candidate(tmp_path)
    inputs = {
        "vsix_path": vsix,
        "target": "linux-x64",
        "manifest_path": manifest_path,
        "public_key_path": public_key_path,
        "dependency_sbom_path": dependency_sbom_path,
    }
    original = build_vsix_sbom(**inputs)
    with ZipFile(vsix) as archive:
        package = json.loads(archive.read("extension/package.json"))
    package["description"] = "Updated extension listing"
    _replace_zip_entry(vsix, "extension/package.json", json.dumps(package).encode())

    changed = build_vsix_sbom(**inputs)

    assert changed["serialNumber"] != original["serialNumber"]
    assert changed["components"] == original["components"]
    assert changed["dependencies"] == original["dependencies"]


@pytest.mark.parametrize("mutation", ["runtime", "manifest", "signature", "dependency-sbom"])
def test_vsix_sbom_rejects_unbound_candidate_bytes(tmp_path: Path, mutation: str) -> None:
    vsix, manifest_path, public_key_path, dependency_sbom_path = _candidate(tmp_path)
    if mutation == "runtime":
        _replace_zip_entry(
            vsix,
            "extension/resources/managed-cli/alysis-linux-x64",
            b"tampered runtime",
        )
    elif mutation == "manifest":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        payload["release"]["sourceCommit"] = "b" * 40
        manifest_path.write_text(json.dumps(payload), encoding="utf-8")
    elif mutation == "signature":
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        target = next(item for item in payload["artifacts"] if item["target"] == "linux-x64")
        target["signature"] = "AAAA"
        changed = json.dumps(payload).encode()
        manifest_path.write_bytes(changed)
        _replace_zip_entry(
            vsix,
            "extension/resources/managed-cli/manifest.json",
            changed,
        )
    else:
        dependency_sbom_path.write_bytes(dependency_sbom_path.read_bytes() + b"\n")

    with pytest.raises(VsixSbomError):
        build_vsix_sbom(
            vsix_path=vsix,
            target="linux-x64",
            manifest_path=manifest_path,
            public_key_path=public_key_path,
            dependency_sbom_path=dependency_sbom_path,
        )


@pytest.mark.parametrize(
    "entry",
    [
        "extension/.env.production",
        "extension/private/deploy.ppk",
        "extension/src/secret.ts",
        "extension/out/extension.js.map",
        "extension/nested.vsix",
        "extension/resources/managed-cli/alysis-win32-x64.exe",
    ],
)
def test_vsix_sbom_rejects_forbidden_archive_inventory(tmp_path: Path, entry: str) -> None:
    vsix, manifest_path, public_key_path, dependency_sbom_path = _candidate(tmp_path)
    with ZipFile(vsix, "a", compression=ZIP_DEFLATED) as archive:
        archive.writestr(entry, b"must not ship")

    with pytest.raises(VsixSbomError):
        build_vsix_sbom(
            vsix_path=vsix,
            target="linux-x64",
            manifest_path=manifest_path,
            public_key_path=public_key_path,
            dependency_sbom_path=dependency_sbom_path,
        )


def test_vsix_sbom_rejects_public_pem_that_differs_from_pinned_key(tmp_path: Path) -> None:
    vsix, manifest_path, public_key_path, dependency_sbom_path = _candidate(tmp_path)
    _replace_zip_entry(
        vsix,
        "extension/resources/managed-cli-release-public.pem",
        b"public verification key",
    )

    with pytest.raises(VsixSbomError, match="differs from the pinned release key"):
        build_vsix_sbom(
            vsix_path=vsix,
            target="linux-x64",
            manifest_path=manifest_path,
            public_key_path=public_key_path,
            dependency_sbom_path=dependency_sbom_path,
        )


def test_vsix_sbom_rejects_private_pem_as_pinned_key(tmp_path: Path) -> None:
    vsix, manifest_path, public_key_path, dependency_sbom_path = _candidate(tmp_path)
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_key_bytes = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption(),
    )
    public_key_path.write_bytes(private_key_bytes)
    _replace_zip_entry(
        vsix,
        "extension/resources/managed-cli-release-public.pem",
        private_key_bytes,
    )

    with pytest.raises(VsixSbomError, match="unavailable or invalid"):
        build_vsix_sbom(
            vsix_path=vsix,
            target="linux-x64",
            manifest_path=manifest_path,
            public_key_path=public_key_path,
            dependency_sbom_path=dependency_sbom_path,
        )


def test_vsix_sbom_rejects_non_p256_public_key(tmp_path: Path) -> None:
    vsix, manifest_path, public_key_path, dependency_sbom_path = _candidate(tmp_path)
    public_key_bytes = (
        ec.generate_private_key(ec.SECP384R1())
        .public_key()
        .public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    public_key_path.write_bytes(public_key_bytes)
    _replace_zip_entry(
        vsix,
        "extension/resources/managed-cli-release-public.pem",
        public_key_bytes,
    )

    with pytest.raises(VsixSbomError, match="ECDSA P-256 public key"):
        build_vsix_sbom(
            vsix_path=vsix,
            target="linux-x64",
            manifest_path=manifest_path,
            public_key_path=public_key_path,
            dependency_sbom_path=dependency_sbom_path,
        )


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload.update(specVersion="1.4"), "CycloneDX 1.5"),
        (
            lambda payload: payload["dependencies"][0]["dependsOn"].append("missing-ref"),
            "dangling reference",
        ),
        (
            lambda payload: payload["components"].append(
                {
                    "type": "library",
                    "bom-ref": "alpha-conflict",
                    "name": "different-alpha",
                    "version": "1.0.0",
                    "purl": "pkg:pypi/alpha@1.0.0",
                }
            ),
            "conflicting duplicate packages",
        ),
    ],
)
def test_vsix_sbom_rejects_invalid_signed_dependency_inventory(
    tmp_path: Path, mutation: object, message: str
) -> None:
    dependency_payload = _dependency_payload("linux-x64")
    assert callable(mutation)
    mutation(dependency_payload)
    vsix, manifest_path, public_key_path, dependency_sbom_path = _candidate(
        tmp_path, linux_dependency_payload=dependency_payload
    )

    with pytest.raises(VsixSbomError, match=message):
        build_vsix_sbom(
            vsix_path=vsix,
            target="linux-x64",
            manifest_path=manifest_path,
            public_key_path=public_key_path,
            dependency_sbom_path=dependency_sbom_path,
        )


def _candidate(
    tmp_path: Path,
    *,
    linux_dependency_payload: dict[str, object] | None = None,
) -> tuple[Path, Path, Path, Path]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_key_path = tmp_path / "release-private.pem"
    private_key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    public_key_path = tmp_path / "release-public.pem"
    public_key_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    artifacts: list[ArtifactInput] = []
    selected_dependency_sbom: Path | None = None
    for target in TARGETS:
        executable = f"alysis-{target}" + (".exe" if target.startswith("win32-") else "")
        path = tmp_path / target / executable
        path.parent.mkdir()
        path.write_bytes(b"linux runtime" if target == "linux-x64" else target.encode())
        dependency_sbom = tmp_path / target / f"{target}.cdx.json"
        dependency_payload = (
            linux_dependency_payload
            if target == "linux-x64" and linux_dependency_payload is not None
            else _dependency_payload(target)
        )
        dependency_sbom.write_text(
            json.dumps(dependency_payload, sort_keys=True) + "\n", encoding="utf-8"
        )
        if target == "linux-x64":
            selected_dependency_sbom = dependency_sbom
        if target.startswith("win32-"):
            identity = "sha256:" + "a" * 64
        elif target.startswith("darwin-"):
            identity = "Developer ID Application: Test (TEAMID1234)"
        else:
            identity = "not-applicable"
        evidence_path = tmp_path / target / f"{target}.native-signature.json"
        evidence: dict[str, object] = {
            "schemaVersion": 2,
            "target": target,
            "executable": executable,
            "executableSha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "kind": (
                "authenticode"
                if target.startswith("win32-")
                else "developer-id-notarization"
                if target.startswith("darwin-")
                else "linux-hash-and-provenance"
            ),
            "status": "verified",
            "signerIdentity": identity,
        }
        if target.startswith("win32-"):
            evidence.update(
                signerThumbprint="b" * 40,
                timestampSignerIdentity="sha256:" + "c" * 64,
            )
        elif target.startswith("darwin-"):
            evidence["submissionId"] = "12345678-1234-1234-1234-123456789abc"
        evidence_path.write_text(json.dumps(evidence, sort_keys=True) + "\n", encoding="utf-8")
        artifacts.append(
            ArtifactInput(
                target=target,
                path=path,
                url=(
                    "https://github.com/AlysisAi/AlysisCode/releases/download/"
                    f"{RELEASE_TAG}/{executable}"
                ),
                sbom_path=dependency_sbom,
                native_evidence_path=evidence_path,
            )
        )
    manifest = build_manifest(
        repository_root=Path(__file__).resolve().parents[1],
        signing_key_path=private_key_path,
        artifacts=artifacts,
        release_tag=RELEASE_TAG,
        source_repository="https://github.com/AlysisAi/AlysisCode",
        source_commit="a" * 40,
        signing_key_id="test-release-key",
        provenance_issuer="https://token.actions.githubusercontent.com",
        provenance_builder_id=(
            "https://github.com/AlysisAi/AlysisCode/.github/workflows/"
            f"managed-cli-vsix-release.yml@refs/tags/{RELEASE_TAG}"
        ),
    )
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    vsix = tmp_path / "vscode-alysis-linux-x64.vsix"
    with ZipFile(vsix, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr(
            "extension/package.json",
            json.dumps({"publisher": "alysisai", "name": "vscode-alysis", "version": "0.1.1"}),
        )
        archive.writestr(
            "extension/resources/managed-cli/manifest.json", manifest_path.read_bytes()
        )
        archive.writestr("extension/resources/managed-cli/alysis-linux-x64", b"linux runtime")
        archive.writestr(
            "extension/resources/managed-cli-release-public.pem",
            public_key_path.read_bytes(),
        )
    assert selected_dependency_sbom is not None
    return vsix, manifest_path, public_key_path, selected_dependency_sbom


def _dependency_payload(target: str) -> dict[str, object]:
    root_ref = f"alysis-code@{PACKAGE_VERSION}:{target}"
    alpha = {
        "type": "library",
        "bom-ref": "pkg:pypi/alpha@1.0.0",
        "name": "alpha",
        "version": "1.0.0",
        "purl": "pkg:pypi/alpha@1.0.0",
    }
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "library",
                "bom-ref": root_ref,
                "name": "alysis-code",
                "version": PACKAGE_VERSION,
            }
        },
        "components": [
            {
                "type": "library",
                "bom-ref": "pkg:pypi/bravo@2.0.0",
                "name": "bravo",
                "version": "2.0.0",
                "purl": "pkg:pypi/bravo@2.0.0",
            },
            alpha,
            {**alpha, "bom-ref": "zz-alpha-alias"},
        ],
        "dependencies": [
            {"ref": "pkg:pypi/alpha@1.0.0", "dependsOn": ["pkg:pypi/bravo@2.0.0"]},
            {"ref": root_ref, "dependsOn": ["pkg:pypi/alpha@1.0.0"]},
            {"ref": "pkg:pypi/alpha@1.0.0", "dependsOn": []},
            {"ref": "zz-alpha-alias", "dependsOn": ["pkg:pypi/bravo@2.0.0"]},
        ],
    }


def _replace_zip_entry(path: Path, name: str, value: bytes) -> None:
    with ZipFile(path) as archive:
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    entries[name] = value
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for entry_name, entry_value in entries.items():
            archive.writestr(entry_name, entry_value)
