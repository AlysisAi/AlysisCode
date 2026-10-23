from __future__ import annotations

import base64
import copy
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from _release_test_helpers import ARTIFACT_VERSION, PACKAGE_VERSION, RELEASE_TAG
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from scripts.release.build_managed_cli_manifest import (
    TARGETS,
    ArtifactInput,
    _attestation,
    _signed_record,
    build_manifest,
)

SOURCE_COMMIT = "a" * 40
SIGNING_KEY_ID = "test-release-2026-01"
SOURCE_REPOSITORY = "https://github.com/AlysisAi/AlysisCode"
PROVENANCE_ISSUER = "https://token.actions.githubusercontent.com"
PROVENANCE_BUILDER = (
    "https://github.com/AlysisAi/AlysisCode/.github/workflows/"
    f"managed-cli-vsix-release.yml@refs/tags/{RELEASE_TAG}"
)


def test_manifest_builder_requires_all_targets_and_signs_canonical_v3_records(
    tmp_path: Path,
) -> None:
    private_key, key_path = _key(tmp_path)
    artifacts = _artifacts(tmp_path)

    manifest = _build(key_path, artifacts)

    assert manifest["schemaVersion"] == 3
    assert manifest["cliVersion"] == PACKAGE_VERSION
    assert manifest["artifactVersion"] == ARTIFACT_VERSION
    assert manifest["signingKeyId"] == SIGNING_KEY_ID
    assert manifest["release"] == {
        "tag": RELEASE_TAG,
        "sourceRepository": SOURCE_REPOSITORY,
        "sourceCommit": SOURCE_COMMIT,
    }
    assert manifest["provenance"] == {
        "issuer": PROVENANCE_ISSUER,
        "builderId": PROVENANCE_BUILDER,
    }
    assert len(manifest["artifacts"]) == 6
    for artifact in manifest["artifacts"]:
        source = next(item for item in artifacts if item.target == artifact["target"])
        digest = hashlib.sha256(source.path.read_bytes()).hexdigest()
        sbom_digest = hashlib.sha256(source.sbom_path.read_bytes()).hexdigest()  # type: ignore[union-attr]
        evidence_digest = hashlib.sha256(
            source.native_evidence_path.read_bytes()  # type: ignore[union-attr]
        ).hexdigest()
        assert artifact["sha256"] == digest
        assert artifact["sbomSha256"] == sbom_digest
        assert artifact["nativeSignature"]["evidenceSha256"] == evidence_digest
        record = _record(manifest, artifact)
        private_key.public_key().verify(
            base64.b64decode(artifact["signature"], validate=True),
            _attestation(record),
            ec.ECDSA(hashes.SHA256()),
        )


def test_python_builder_matches_the_runtime_canonical_v3_contract_fixture() -> None:
    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "extensions"
        / "vscode-alysis"
        / "test"
        / "fixtures"
        / "managedCliAttestationV3.json"
    )
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    public_key = serialization.load_pem_public_key(fixture["publicKeyPem"].encode())
    assert isinstance(public_key, ec.EllipticCurvePublicKey)
    public_key.verify(
        base64.b64decode(fixture["signature"], validate=True),
        _attestation(fixture["record"]),
        ec.ECDSA(hashes.SHA256()),
    )


def test_every_release_policy_field_is_cryptographically_bound(tmp_path: Path) -> None:
    private_key, key_path = _key(tmp_path)
    manifest = _build(key_path, _artifacts(tmp_path))
    artifact = manifest["artifacts"][0]
    signature = base64.b64decode(artifact["signature"], validate=True)
    record = _record(manifest, artifact)

    mutations = (
        ("release tag", lambda value: value["release"].update(tag=f"{RELEASE_TAG}-tampered")),
        (
            "source repository",
            lambda value: value["release"].update(sourceRepository="https://example.com/repo"),
        ),
        ("source commit", lambda value: value["release"].update(sourceCommit="b" * 40)),
        (
            "artifact version",
            lambda value: value.update(artifactVersion=f"{ARTIFACT_VERSION}-tampered"),
        ),
        ("CLI version", lambda value: value.update(cliVersion=f"{PACKAGE_VERSION}-tampered")),
        ("compatibility", lambda value: value["compatibility"]["extension"].update(max="9.9.9")),
        ("target", lambda value: value["artifact"].update(target="linux-x64")),
        ("filename", lambda value: value["artifact"].update(executable="other")),
        ("URL", lambda value: value["artifact"].update(url="https://example.com/other")),
        ("size", lambda value: value["artifact"].update(size=999)),
        ("executable digest", lambda value: value["artifact"].update(sha256="b" * 64)),
        ("SBOM digest", lambda value: value["artifact"].update(sbomSha256="c" * 64)),
        (
            "provenance",
            lambda value: value["provenance"].update(builderId="https://example.com/builder"),
        ),
        (
            "native policy",
            lambda value: value["artifact"]["nativeSignature"].update(policy="not-applicable"),
        ),
        (
            "native identity",
            lambda value: value["artifact"]["nativeSignature"].update(signerIdentity="other"),
        ),
        (
            "native evidence digest",
            lambda value: value["artifact"]["nativeSignature"].update(evidenceSha256="d" * 64),
        ),
        ("signing key", lambda value: value.update(signingKeyId="other-key")),
    )
    for _label, mutate in mutations:
        tampered = copy.deepcopy(record)
        mutate(tampered)
        with pytest.raises(InvalidSignature):
            private_key.public_key().verify(
                signature,
                _attestation(tampered),
                ec.ECDSA(hashes.SHA256()),
            )


def test_manifest_builder_rejects_incomplete_or_misclassified_native_targets(
    tmp_path: Path,
) -> None:
    _, key_path = _key(tmp_path)
    artifacts = _artifacts(tmp_path)
    with pytest.raises(ValueError, match="six supported targets"):
        _build(key_path, artifacts[:1])

    evidence_path = next(
        artifact.native_evidence_path for artifact in artifacts if artifact.target == "win32-x64"
    )
    assert evidence_path is not None
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload["kind"] = "linux-hash-and-provenance"
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="does not bind the verified executable"):
        _build(key_path, artifacts)


def test_manifest_builder_rejects_missing_or_unbound_native_evidence(tmp_path: Path) -> None:
    _, key_path = _key(tmp_path)
    artifacts = _artifacts(tmp_path)
    missing = [
        replace(artifact, native_evidence_path=None) if artifact.target == "linux-x64" else artifact
        for artifact in artifacts
    ]
    with pytest.raises(ValueError, match="requires an existing native-signature evidence"):
        _build(key_path, missing)

    evidence_path = next(
        artifact.native_evidence_path for artifact in artifacts if artifact.target == "linux-x64"
    )
    assert evidence_path is not None
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload["executableSha256"] = "f" * 64
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="does not bind the verified executable"):
        _build(key_path, artifacts)


def test_manifest_builder_rejects_apple_identity_control_character(tmp_path: Path) -> None:
    _, key_path = _key(tmp_path)
    artifacts = _artifacts(tmp_path)
    evidence_path = next(
        artifact.native_evidence_path for artifact in artifacts if artifact.target == "darwin-x64"
    )
    assert evidence_path is not None
    payload = json.loads(evidence_path.read_text(encoding="utf-8"))
    payload["signerIdentity"] = "Developer ID Application: Legit\nINJECTED=value (TEAMID1234)"
    evidence_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="native signer identity is invalid"):
        _build(key_path, artifacts)


def test_manifest_builder_rejects_unbound_release_identity(tmp_path: Path) -> None:
    _, key_path = _key(tmp_path)
    artifacts = _artifacts(tmp_path)
    with pytest.raises(ValueError, match="release tag"):
        build_manifest(
            **{**_build_args(key_path, artifacts), "release_tag": f"{RELEASE_TAG}-tampered"}
        )
    with pytest.raises(ValueError, match="source commit"):
        build_manifest(**{**_build_args(key_path, artifacts), "source_commit": "not-a-commit"})
    with pytest.raises(ValueError, match="provenance builder"):
        build_manifest(
            **{
                **_build_args(key_path, artifacts),
                "provenance_builder_id": f"https://example.com/workflow@refs/tags/{RELEASE_TAG}",
            }
        )
    wrong_url = [
        replace(artifact, url=f"{SOURCE_REPOSITORY}/releases/download/{RELEASE_TAG}/other")
        if artifact.target == "linux-x64"
        else artifact
        for artifact in artifacts
    ]
    with pytest.raises(ValueError, match="URL must bind"):
        _build(key_path, wrong_url)


def _key(tmp_path: Path) -> tuple[ec.EllipticCurvePrivateKey, Path]:
    private_key = ec.generate_private_key(ec.SECP256R1())
    key_path = tmp_path / "release.pem"
    key_path.write_bytes(
        private_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return private_key, key_path


def _artifacts(tmp_path: Path) -> list[ArtifactInput]:
    artifacts: list[ArtifactInput] = []
    for target in TARGETS:
        executable = f"alysis-{target}" + (".exe" if target.startswith("win32-") else "")
        artifact_path = tmp_path / target / executable
        artifact_path.parent.mkdir(exist_ok=True)
        artifact_path.write_bytes(f"binary:{target}".encode())
        sbom_path = tmp_path / target / f"{target}.cdx.json"
        sbom_path.write_text(f'{{"target":"{target}"}}\n', encoding="utf-8")
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
            "executableSha256": hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
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
                path=artifact_path,
                url=(f"{SOURCE_REPOSITORY}/releases/download/{RELEASE_TAG}/{executable}"),
                sbom_path=sbom_path,
                native_evidence_path=evidence_path,
            )
        )
    return artifacts


def _build(key_path: Path, artifacts: list[ArtifactInput]) -> dict[str, object]:
    return build_manifest(**_build_args(key_path, artifacts))


def _build_args(key_path: Path, artifacts: list[ArtifactInput]) -> dict[str, object]:
    return {
        "repository_root": Path(__file__).resolve().parents[1],
        "signing_key_path": key_path,
        "artifacts": artifacts,
        "release_tag": RELEASE_TAG,
        "source_repository": SOURCE_REPOSITORY,
        "source_commit": SOURCE_COMMIT,
        "signing_key_id": SIGNING_KEY_ID,
        "provenance_issuer": PROVENANCE_ISSUER,
        "provenance_builder_id": PROVENANCE_BUILDER,
    }


def _record(manifest: dict[str, object], artifact: dict[str, object]) -> dict[str, object]:
    unsigned_artifact = {key: value for key, value in artifact.items() if key != "signature"}
    return _signed_record(
        release=manifest["release"],  # type: ignore[arg-type]
        artifact_version=str(manifest["artifactVersion"]),
        cli_version=str(manifest["cliVersion"]),
        compatibility=manifest["compatibility"],  # type: ignore[arg-type]
        signing_key_id=str(manifest["signingKeyId"]),
        provenance=manifest["provenance"],  # type: ignore[arg-type]
        artifact=unsigned_artifact,
    )
