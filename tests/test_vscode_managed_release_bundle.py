from __future__ import annotations

import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

import pytest
from _release_test_helpers import PACKAGE_VERSION, RELEASE_TAG
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from scripts.qa import validate_vscode_promotion_bundle as validator
from scripts.release.build_managed_cli_manifest import TARGETS, ArtifactInput, build_manifest
from scripts.release.build_vscode_vsix_sbom import build_vsix_sbom

SOURCE_SHA = "a" * 40


def _release_bundle(tmp_path: Path) -> tuple[Path, Path, Path]:
    managed = tmp_path / "managed"
    candidates = tmp_path / "candidates"
    source = tmp_path / "source"
    managed.mkdir()
    candidates.mkdir()
    source.mkdir()
    private_key = ec.generate_private_key(ec.SECP256R1())
    private_path = tmp_path / "private.pem"
    private_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    public_path = tmp_path / "public.pem"
    public_path.write_bytes(
        private_key.public_key().public_bytes(
            serialization.Encoding.PEM,
            serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    artifacts: list[ArtifactInput] = []
    for target in TARGETS:
        executable = f"alysis-{target}" + (".exe" if target.startswith("win32-") else "")
        runtime = managed / executable
        runtime.write_bytes(f"runtime:{target}".encode())
        dependency = managed / f"{target}.cdx.json"
        dependency.write_text(
            json.dumps(_dependency_payload(target), sort_keys=True) + "\n", encoding="utf-8"
        )
        if target.startswith("win32-"):
            identity = "sha256:" + "b" * 64
        elif target.startswith("darwin-"):
            identity = "Developer ID Application: Alysis Code Test (TEAM123456)"
        else:
            identity = "not-applicable"
        kind = (
            "authenticode"
            if target.startswith("win32-")
            else "developer-id-notarization"
            if target.startswith("darwin-")
            else "linux-hash-and-provenance"
        )
        evidence_path = managed / f"{target}.native-signature.json"
        evidence: dict[str, object] = {
            "schemaVersion": 2,
            "target": target,
            "executable": executable,
            "executableSha256": hashlib.sha256(runtime.read_bytes()).hexdigest(),
            "kind": kind,
            "status": "verified",
            "signerIdentity": identity,
        }
        if target.startswith("win32-"):
            evidence.update(
                signerThumbprint="c" * 40,
                timestampSignerIdentity="sha256:" + "d" * 64,
            )
        elif target.startswith("darwin-"):
            evidence["submissionId"] = "12345678-1234-1234-1234-123456789abc"
        evidence_path.write_text(
            json.dumps(evidence, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        artifacts.append(
            ArtifactInput(
                target=target,
                path=runtime,
                url=(
                    f"https://github.com/AlysisAi/AlysisCode/releases/download/"
                    f"{RELEASE_TAG}/{executable}"
                ),
                sbom_path=dependency,
                native_evidence_path=evidence_path,
            )
        )
    manifest = build_manifest(
        repository_root=Path(__file__).resolve().parents[1],
        signing_key_path=private_path,
        artifacts=artifacts,
        release_tag=RELEASE_TAG,
        source_repository="https://github.com/AlysisAi/AlysisCode",
        source_commit=SOURCE_SHA,
        signing_key_id="test-release-key",
        provenance_issuer="https://token.actions.githubusercontent.com",
        provenance_builder_id=(
            "https://github.com/AlysisAi/AlysisCode/.github/workflows/"
            f"managed-cli-vsix-release.yml@refs/tags/{RELEASE_TAG}"
        ),
    )
    manifest_path = managed / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    for target in TARGETS:
        executable = f"alysis-{target}" + (".exe" if target.startswith("win32-") else "")
        vsix = candidates / f"vscode-alysis-{target}.vsix"
        with ZipFile(vsix, "w") as archive:
            archive.writestr(
                "extension/package.json",
                json.dumps(
                    {
                        "publisher": "alysisai",
                        "name": "vscode-alysis",
                        "version": "0.1.1",
                    }
                ),
            )
            archive.writestr(
                "extension/resources/managed-cli/manifest.json", manifest_path.read_bytes()
            )
            archive.writestr(
                f"extension/resources/managed-cli/{executable}",
                (managed / executable).read_bytes(),
            )
            archive.writestr(
                "extension/resources/managed-cli-release-public.pem", public_path.read_bytes()
            )
        sbom = build_vsix_sbom(
            vsix_path=vsix,
            target=target,
            manifest_path=manifest_path,
            public_key_path=public_path,
            dependency_sbom_path=managed / f"{target}.cdx.json",
        )
        (candidates / f"vscode-alysis-{target}.cdx.json").write_text(
            json.dumps(sbom, sort_keys=True), encoding="utf-8"
        )
    return managed, candidates, public_path


def _dependency_payload(target: str) -> dict[str, object]:
    root = f"alysis-code@{PACKAGE_VERSION}:{target}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "metadata": {
            "component": {
                "type": "library",
                "bom-ref": root,
                "name": "alysis-code",
                "version": PACKAGE_VERSION,
            }
        },
        "components": [],
        "dependencies": [{"ref": root, "dependsOn": []}],
    }


def _validate(managed: Path, candidates: Path, public_key: Path) -> None:
    validator._validate_managed_runtime_release(
        managed,
        candidates,
        release_tag=RELEASE_TAG,
        source_sha=SOURCE_SHA,
        trusted_public_key_path=public_key,
    )


def test_managed_release_bundle_binds_all_public_assets_and_canonical_sboms(
    tmp_path: Path,
) -> None:
    managed, candidates, public_key = _release_bundle(tmp_path)

    _validate(managed, candidates, public_key)


def test_managed_release_bundle_rejects_runtime_substitution(tmp_path: Path) -> None:
    managed, candidates, public_key = _release_bundle(tmp_path)
    (managed / "alysis-linux-x64").write_bytes(b"substituted")

    with pytest.raises(validator.PromotionBundleValidationError, match="signed linux-x64 record"):
        _validate(managed, candidates, public_key)


def test_managed_release_bundle_rejects_noncanonical_final_sbom(tmp_path: Path) -> None:
    managed, candidates, public_key = _release_bundle(tmp_path)
    sbom_path = candidates / "vscode-alysis-linux-x64.cdx.json"
    payload = json.loads(sbom_path.read_text(encoding="utf-8"))
    payload["metadata"]["component"]["licenses"] = [{"license": {"id": "MIT"}}]
    sbom_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(validator.PromotionBundleValidationError, match="not the canonical"):
        _validate(managed, candidates, public_key)


def test_managed_release_bundle_rejects_native_evidence_mismatch(tmp_path: Path) -> None:
    managed, candidates, public_key = _release_bundle(tmp_path)
    record_path = managed / "darwin-arm64.native-signature.json"
    payload = json.loads(record_path.read_text(encoding="utf-8"))
    payload["signerIdentity"] = "Developer ID Application: Substituted"
    record_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(validator.PromotionBundleValidationError, match="inconsistent"):
        _validate(managed, candidates, public_key)
